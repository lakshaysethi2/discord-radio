"""FastAPI shim over the file-provider Service.

Endpoints mirror the bot-side client (provider.client.FileProviderClient):

    GET  /health                        provider health snapshot
    GET  /current                       -> TrackPayload
    POST /next                          -> TrackPayload
    GET  /peek?count=N                  -> list[TrackPayload]
    GET  /tracks/{track_id}             -> TrackPayload (force fetch)
    POST /tracks/{track_id}/played      no-op ack
    GET  /health/torrents               aria2/torrent health
    GET  /torrents                      managed torrents + files
    POST /torrents/magnet               add a magnet link
    POST /torrents/file                 upload a .torrent file
    POST /torrents/{gid}/files/{index}  enable/disable a playlist file
    POST /torrents/{gid}/action         pause/resume/remove
    POST /refresh                       re-scan providers
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from file_provider import config as config_module
from file_provider.providers.base import ProviderFetchError
from file_provider.service import PlaylistEmpty, Service, build_service
from file_provider.torrent_client import TorrentClientError, TorrentClientUnavailable

log = logging.getLogger(__name__)


class MagnetRequest(BaseModel):
    magnet: str


class FileSelectionRequest(BaseModel):
    enabled: bool = True
    force: bool = False


class TorrentActionRequest(BaseModel):
    action: str


def create_app(service: Service | None = None) -> FastAPI:
    if service is None:
        service = build_service(config_module.load())

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        """Start optional clients and scan providers on startup."""
        s: Service = _app.state.service
        s.start()
        if s.db.playlist_length() == 0:
            log.info("playlist empty on startup — running initial scan")
            result = s.refresh_playlist()
            log.info("startup scan complete: %s", result)
        else:
            log.info(
                "playlist already has %d tracks — skipping startup scan", s.db.playlist_length()
            )
        try:
            yield
        finally:
            s.shutdown()

    app = FastAPI(title="Discord TV File Provider", version="0.1.0", lifespan=lifespan)
    app.state.service = service

    def svc() -> Service:  # tiny accessor, lets tests swap easily
        return app.state.service

    @app.get("/health")
    def health() -> dict:
        s = svc()
        return {
            "playlist_length": s.db.playlist_length(),
            "cursor": s.db.get_cursor(),
            "cache_bytes": s.cache.total_bytes(),
            "cache_max_bytes": s.cache.max_bytes,
            "providers": s.db.health_snapshot(),
        }

    @app.get("/health/torrents")
    def torrent_health() -> dict:
        provider = svc().torrent_provider()
        manager = getattr(provider, "manager", None) if provider is not None else None
        if manager is None:
            return {"enabled": False, "available": False, "last_error": "torrent provider disabled"}
        return {
            "enabled": True,
            "available": manager.available,
            "last_error": manager.last_start_error,
            "torrent_count": len(manager.db.list_torrents()),
            "max_size_bytes": manager.max_size_bytes,
            "max_upload_bytes": manager.max_upload_bytes,
        }

    @app.get("/current")
    def get_current() -> JSONResponse:
        try:
            return JSONResponse(svc().current().to_dict())
        except PlaylistEmpty as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ProviderFetchError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.post("/next")
    def post_next() -> JSONResponse:
        try:
            return JSONResponse(svc().next().to_dict())
        except PlaylistEmpty as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ProviderFetchError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.get("/peek")
    def get_peek(count: int = Query(5, ge=1, le=100)) -> JSONResponse:
        return JSONResponse([p.to_dict() for p in svc().peek(count)])

    @app.get("/tracks")
    def list_tracks(
        offset: int = Query(0, ge=0),
        limit: int = Query(100, ge=1, le=500),
        q: str | None = Query(None, description="case-insensitive title substring"),
        provider: str | None = Query(None, description="provider name"),
        media_type: str | None = Query(None, alias="type", description="audio or video"),
        cached: str | None = Query(None, description="ready or missing"),
    ) -> JSONResponse:
        """List tracks without causing downloads, with dashboard filters."""
        if media_type not in (None, "", "all", "audio", "video"):
            raise HTTPException(status_code=422, detail="type must be audio or video")
        if cached not in (None, "", "all", "ready", "missing"):
            raise HTTPException(status_code=422, detail="cached must be ready or missing")
        has_video = True if media_type == "video" else False if media_type == "audio" else None
        ready = True if cached == "ready" else False if cached == "missing" else None
        items, total = svc().list_all(
            offset=offset,
            limit=limit,
            search=q,
            provider=provider or None,
            has_video=has_video,
            ready=ready,
        )
        return JSONResponse(
            {
                "items": [item.to_dict() for item in items],
                "total": total,
                "offset": offset,
                "limit": limit,
            }
        )

    @app.get("/tracks/{track_id}")
    def get_track(track_id: str) -> JSONResponse:
        try:
            return JSONResponse(svc().get_by_id(track_id).to_dict())
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"unknown track {track_id}") from exc
        except ProviderFetchError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.post("/tracks/{track_id}/played")
    def post_played(track_id: str) -> dict:
        svc().mark_played(track_id)
        return {"ok": True}

    @app.post("/jump/{track_id}")
    def post_jump(track_id: str) -> JSONResponse:
        try:
            return JSONResponse(svc().jump_to(track_id).to_dict())
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"unknown track {track_id}") from exc
        except ProviderFetchError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    # ------------------------------------------------------------- torrents
    def torrent_manager():
        provider = svc().torrent_provider()
        manager = getattr(provider, "manager", None) if provider is not None else None
        if manager is None:
            raise HTTPException(status_code=503, detail="torrent provider is disabled")
        if not manager.available and not manager.start():
            raise HTTPException(
                status_code=503,
                detail=manager.last_start_error or "torrent client unavailable",
            )
        return manager

    def torrent_error(exc: Exception) -> HTTPException:
        status = 503 if isinstance(exc, TorrentClientUnavailable) else 400
        return HTTPException(status_code=status, detail=str(exc))

    @app.get("/torrents")
    def get_torrents() -> dict:
        try:
            torrents = torrent_manager().list_torrents()
        except HTTPException:
            raise
        except TorrentClientError as exc:
            raise torrent_error(exc) from exc
        return {"items": [torrent.to_dict() for torrent in torrents]}

    @app.post("/torrents/magnet")
    def post_torrent_magnet(payload: MagnetRequest) -> dict:
        try:
            torrent = torrent_manager().add_magnet(payload.magnet)
        except HTTPException:
            raise
        except TorrentClientError as exc:
            raise torrent_error(exc) from exc
        return {"torrent": torrent.to_dict()}

    @app.post("/torrents/file")
    async def post_torrent_file(file: UploadFile = File(...)) -> dict:
        try:
            manager = torrent_manager()
            # Read with the configured hard limit so an accidental media upload
            # cannot consume all provider memory before aria2 sees it.
            content = await file.read(manager.max_upload_bytes + 1)
            if len(content) > manager.max_upload_bytes:
                raise HTTPException(
                    status_code=413,
                    detail=f".torrent file is larger than {manager.max_upload_bytes // 1024**2} MiB",
                )
            torrent = manager.add_torrent_file(content, file.filename or "upload.torrent")
        except HTTPException:
            raise
        except TorrentClientError as exc:
            log.warning("torrent file upload failed: %s", exc)
            raise torrent_error(exc) from exc
        return {"torrent": torrent.to_dict()}

    @app.post("/torrents/{gid}/files/{file_index}")
    def post_torrent_file_selection(
        gid: str, file_index: int, payload: FileSelectionRequest
    ) -> dict:
        try:
            manager = torrent_manager()
            torrent = manager.set_file_playlist_enabled(
                gid, file_index, payload.enabled, force=payload.force
            )
            # Keep the regular playlist in sync immediately so the queue page
            # can play the newly enabled file without a separate refresh click.
            refresh = svc().refresh_playlist()
        except HTTPException:
            raise
        except (TorrentClientError, KeyError) as exc:
            raise torrent_error(exc) from exc
        return {"torrent": torrent.to_dict(), "refresh": refresh}

    @app.post("/torrents/{gid}/action")
    def post_torrent_action(gid: str, payload: TorrentActionRequest) -> dict:
        try:
            manager = torrent_manager()
            if payload.action.lower().strip() == "remove":
                removed = manager.remove(gid)
                for _track_id, cache_path in removed:
                    if cache_path:
                        try:
                            from pathlib import Path

                            Path(cache_path).unlink(missing_ok=True)
                        except OSError:
                            pass
                return {"removed": True, "refresh": svc().refresh_playlist()}
            torrent = manager.action(gid, payload.action)
        except HTTPException:
            raise
        except TorrentClientError as exc:
            raise torrent_error(exc) from exc
        return {"torrent": torrent.to_dict() if torrent else None}

    @app.post("/refresh")
    def post_refresh() -> dict:
        return svc().refresh_playlist()

    return app


# WSGI/ASGI entry point: `uvicorn file_provider.api.main:app`
app = create_app()

"""FastAPI shim over the file-provider Service.

Endpoints mirror the bot-side client (provider.client.FileProviderClient):

    GET  /health                        provider health snapshot
    GET  /current                       -> TrackPayload
    POST /next                          -> TrackPayload
    GET  /peek?count=N                  -> list[TrackPayload]
    GET  /tracks/{track_id}             -> TrackPayload (force fetch)
    POST /tracks/{track_id}/played      no-op ack
    POST /refresh                       re-scan providers
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse

from file_provider import config as config_module
from file_provider.providers.base import ProviderFetchError
from file_provider.service import PlaylistEmpty, Service, build_service

log = logging.getLogger(__name__)


def create_app(service: Service | None = None) -> FastAPI:
    if service is None:
        service = build_service(config_module.load())

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        """Auto-scan providers on startup so the playlist is ready immediately."""
        s: Service = _app.state.service
        if s.db.playlist_length() == 0:
            log.info("playlist empty on startup — running initial scan")
            result = s.refresh_playlist()
            log.info("startup scan complete: %s", result)
        else:
            log.info(
                "playlist already has %d tracks — skipping startup scan", s.db.playlist_length()
            )
        yield

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
    ) -> JSONResponse:
        """List tracks without causing downloads."""
        items, total = svc().list_all(offset=offset, limit=limit, search=q)
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

    @app.post("/refresh")
    def post_refresh(payload: dict | None = None) -> dict:
        items = payload.get("archive_org_items") if payload else None
        return svc().refresh_playlist(archive_org_items=items)

    return app


# WSGI/ASGI entry point: `uvicorn file_provider.api.main:app`
app = create_app()

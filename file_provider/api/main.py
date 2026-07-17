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

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse

from file_provider import config as config_module
from file_provider.providers.base import ProviderFetchError
from file_provider.service import PlaylistEmpty, Service, build_service

log = logging.getLogger(__name__)


def create_app(service: Service | None = None) -> FastAPI:
    app = FastAPI(title="Discord TV File Provider", version="0.1.0")

    if service is None:
        service = build_service(config_module.load())

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

    @app.post("/refresh")
    def post_refresh() -> dict:
        return svc().refresh_playlist()

    return app


# WSGI/ASGI entry point: `uvicorn file_provider.api.main:app`
app = create_app()

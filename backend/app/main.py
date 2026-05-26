"""FastAPI application entrypoint for FinAlly."""

from __future__ import annotations

import os
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse

from app.api import create_api_router
from app.market import (
    MarketDataSource,
    PriceCache,
    create_market_data_source,
    create_stream_router,
)
from app.services import FinAllyService, Repository, ServiceError, create_repository

MarketSourceFactory = Callable[[PriceCache], MarketDataSource | None]


def create_app(
    *,
    repository: Repository | None = None,
    price_cache: PriceCache | None = None,
    market_source_factory: MarketSourceFactory | None = create_market_data_source,
    static_dir: str | Path | None = None,
) -> FastAPI:
    cache = price_cache or PriceCache()
    repo = repository or create_repository()
    resolved_static_dir = _resolve_static_dir(static_dir)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        service = FinAllyService(repository=repo, price_cache=cache)
        service.initialize()
        source = market_source_factory(cache) if market_source_factory else None
        service.market_source = source
        app.state.service = service
        app.state.price_cache = cache
        app.state.market_source = source

        if source is not None:
            await source.start(await service.tracked_tickers())
        try:
            yield
        finally:
            if source is not None:
                await source.stop()

    app = FastAPI(title="FinAlly API", version="0.1.0", lifespan=lifespan)

    @app.exception_handler(ServiceError)
    async def service_error_handler(_request, exc: ServiceError):
        return JSONResponse(status_code=exc.status_code, content=exc.to_dict())

    app.include_router(create_api_router())
    app.include_router(create_stream_router(cache))

    if resolved_static_dir is not None:
        _add_static_routes(app, resolved_static_dir)

    return app


def _resolve_static_dir(static_dir: str | Path | None) -> Path | None:
    candidates = []
    if static_dir is not None:
        candidates.append(Path(static_dir))
    if os.environ.get("STATIC_DIR"):
        candidates.append(Path(os.environ["STATIC_DIR"]))
    candidates.extend(
        [
            Path("/app/static"),
            Path(__file__).resolve().parents[2] / "frontend" / "out",
        ]
    )
    for candidate in candidates:
        if candidate.exists() and candidate.is_dir():
            return candidate
    return None


def _add_static_routes(app: FastAPI, static_dir: Path) -> None:
    @app.get("/{full_path:path}", include_in_schema=False)
    async def serve_spa(full_path: str):
        requested = (static_dir / full_path).resolve()
        try:
            requested.relative_to(static_dir.resolve())
        except ValueError as exc:
            raise HTTPException(status_code=404) from exc

        if requested.is_file():
            return FileResponse(requested)

        index = static_dir / "index.html"
        if index.is_file():
            return FileResponse(index)

        raise HTTPException(status_code=404)


app = create_app()

"""FastAPI routes for the FinAlly backend API."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from app.schemas import (
    ChatRequest,
    ChatResponse,
    HealthResponse,
    PortfolioHistoryResponse,
    PortfolioResponse,
    TradeExecutionResponse,
    TradeRequest,
    WatchlistAddResponse,
    WatchlistDeleteResponse,
    WatchlistRequest,
    WatchlistResponse,
)
from app.services import FinAllyService, ServiceError


def get_service(request: Request) -> FinAllyService:
    return request.app.state.service


ServiceDep = Annotated[FinAllyService, Depends(get_service)]


def create_api_router() -> APIRouter:
    router = APIRouter(prefix="/api", tags=["api"])

    @router.get("/health", response_model=HealthResponse)
    async def health(service: ServiceDep) -> dict:
        status = None
        if service.market_source is not None:
            status = service.market_source.get_status()
            status = asdict(status) if is_dataclass(status) else status
        return {"status": "ok", "market": status or {"healthy": True, "provider": "none"}}

    @router.get("/watchlist", response_model=WatchlistResponse)
    async def get_watchlist(service: ServiceDep) -> dict:
        return await service.get_watchlist()

    @router.post("/watchlist", response_model=WatchlistAddResponse)
    async def add_watchlist(payload: WatchlistRequest, service: ServiceDep) -> dict:
        return await _call_service(service.add_watchlist(payload.ticker))

    @router.delete("/watchlist/{ticker}", response_model=WatchlistDeleteResponse)
    async def delete_watchlist(ticker: str, service: ServiceDep) -> dict:
        return await _call_service(service.remove_watchlist(ticker))

    @router.get("/portfolio", response_model=PortfolioResponse)
    async def get_portfolio(service: ServiceDep) -> dict:
        return service.get_portfolio()

    @router.get("/portfolio/history", response_model=PortfolioHistoryResponse)
    async def get_portfolio_history(service: ServiceDep) -> dict:
        return service.get_history()

    @router.post("/portfolio/trade", response_model=TradeExecutionResponse)
    async def execute_trade(payload: TradeRequest, service: ServiceDep) -> dict:
        return await _call_service(
            service.execute_trade(payload.ticker, payload.side, payload.quantity)
        )

    @router.post("/chat", response_model=ChatResponse)
    async def chat(payload: ChatRequest, service: ServiceDep) -> dict:
        return await _call_service(service.chat(payload.message))

    return router


async def _call_service(awaitable):
    try:
        return await awaitable
    except ServiceError as exc:
        return JSONResponse(status_code=exc.status_code, content=exc.to_dict())

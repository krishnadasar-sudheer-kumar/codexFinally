"""Pydantic request and response models for the FinAlly API."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class ErrorDetail(BaseModel):
    error: str
    details: dict[str, Any] = Field(default_factory=dict)


class HealthResponse(BaseModel):
    status: str
    market: dict[str, Any]


class WatchlistRequest(BaseModel):
    ticker: str


class WatchlistItem(BaseModel):
    ticker: str
    current_price: float
    previous_price: float
    change_percent: float
    direction: Literal["up", "down", "flat"]
    timestamp: str


class WatchlistResponse(BaseModel):
    items: list[WatchlistItem]


class WatchlistAddResponse(BaseModel):
    item: WatchlistItem
    already_exists: bool


class WatchlistDeleteResponse(BaseModel):
    ticker: str
    removed: bool


class PositionResponse(BaseModel):
    ticker: str
    quantity: float
    avg_cost: float
    current_price: float
    market_value: float
    unrealized_pnl: float
    change_percent: float


class PortfolioResponse(BaseModel):
    cash_balance: float
    total_value: float
    unrealized_pnl: float
    positions: list[PositionResponse]


class SnapshotResponse(BaseModel):
    total_value: float
    recorded_at: str


class PortfolioHistoryResponse(BaseModel):
    snapshots: list[SnapshotResponse]


class TradeRequest(BaseModel):
    ticker: str
    quantity: float
    side: str

    @field_validator("side", mode="before")
    @classmethod
    def normalize_side(cls, value: str) -> str:
        return value.lower().strip() if isinstance(value, str) else value


class TradeResponse(BaseModel):
    id: str
    ticker: str
    side: Literal["buy", "sell"]
    quantity: float
    price: float
    executed_at: str


class TradeExecutionResponse(BaseModel):
    trade: TradeResponse
    portfolio: PortfolioResponse


class ChatRequest(BaseModel):
    message: str = Field(min_length=1)


class ChatTradeRequest(BaseModel):
    ticker: str
    side: str
    quantity: float


class ChatWatchlistChangeRequest(BaseModel):
    ticker: str
    action: str


class ChatResponse(BaseModel):
    message: str
    trades: list[dict[str, Any]] = Field(default_factory=list)
    watchlist_changes: list[dict[str, Any]] = Field(default_factory=list)
    errors: list[dict[str, Any]] = Field(default_factory=list)

"""Data models for market data."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum


class MarketSourceMode(StrEnum):
    """How FinAlly is currently sourcing market prices."""

    SIMULATOR = "simulator"
    MASSIVE_SNAPSHOT = "massive_snapshot"
    MASSIVE_EOD_SIMULATED = "massive_eod_simulated"


@dataclass(frozen=True, slots=True)
class PriceUpdate:
    """Immutable snapshot of a single ticker's price at a point in time."""

    ticker: str
    price: float
    previous_price: float
    timestamp: float = field(default_factory=time.time)  # Unix seconds

    @property
    def change(self) -> float:
        """Absolute price change from previous update."""
        return round(self.price - self.previous_price, 4)

    @property
    def change_percent(self) -> float:
        """Percentage change from previous update."""
        if self.previous_price == 0:
            return 0.0
        return round((self.price - self.previous_price) / self.previous_price * 100, 4)

    @property
    def direction(self) -> str:
        """'up', 'down', or 'flat'."""
        if self.price > self.previous_price:
            return "up"
        elif self.price < self.previous_price:
            return "down"
        return "flat"

    def to_dict(self) -> dict:
        """Serialize for JSON / SSE transmission."""
        return {
            "ticker": self.ticker,
            "price": self.price,
            "previous_price": self.previous_price,
            "timestamp": self.timestamp,
            "change": self.change,
            "change_percent": self.change_percent,
            "direction": self.direction,
        }


@dataclass(frozen=True, slots=True)
class MarketStatus:
    """Current market data provider status."""

    mode: MarketSourceMode
    provider: str
    healthy: bool
    message: str
    last_success_at: float | None = None

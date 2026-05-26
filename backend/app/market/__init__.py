"""Market data subsystem for FinAlly.

Public API:
    PriceUpdate         - Immutable price snapshot dataclass
    MarketSourceMode    - Current source mode enum
    MarketStatus        - Provider health/status dataclass
    PriceCache          - Thread-safe in-memory price store
    MarketDataSource    - Abstract interface for data providers
    create_market_data_source - Factory that selects simulator or Massive
    create_stream_router - FastAPI router factory for SSE endpoint
"""

from .cache import PriceCache
from .factory import create_market_data_source
from .interface import MarketDataSource
from .models import MarketSourceMode, MarketStatus, PriceUpdate
from .stream import create_stream_router

__all__ = [
    "PriceUpdate",
    "MarketSourceMode",
    "MarketStatus",
    "PriceCache",
    "MarketDataSource",
    "create_market_data_source",
    "create_stream_router",
]

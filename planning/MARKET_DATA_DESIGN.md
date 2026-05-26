# Market Data Backend Design

Implementation-ready design for the FinAlly market data backend. This document consolidates the current project plan, the Massive API research, the unified interface contract, the simulator approach, and the archived implementation/review notes.

This is the root market data design document going forward. The archive documents remain useful history, but new implementation work should use this file as the target.

## Goals

- Provide one source-agnostic Python API for all market prices.
- Stream live-feeling prices to the frontend over SSE at `/api/stream/prices`.
- Support three modes:
  - `simulator`: no `MASSIVE_API_KEY`; local GBM simulator only.
  - `massive_snapshot`: `MASSIVE_API_KEY` has snapshot access; REST poll Massive for watched tickers.
  - `massive_eod_simulated`: `MASSIVE_API_KEY` lacks snapshot access but can read end-of-day data; seed the simulator from Massive daily closes.
- Keep portfolio valuation, trade execution, watchlist routes, and chat actions independent of the data provider.
- Degrade gracefully. Market data provider failures should not stop the FastAPI app from starting.

## Architecture

```text
FastAPI lifespan
  |
  | creates
  v
PriceCache  <----------------------------+
  ^                                      |
  | writes                               | reads
  |                                      |
MarketDataSource                        +--> /api/stream/prices SSE
  |                                      +--> portfolio valuation
  |                                      +--> market order execution
  |
  +-- SimulatorDataSource
  |     +-- GBMSimulator
  |
  +-- MassiveDataSource
        +-- snapshot polling
        +-- EOD capability probe
        +-- optional seeded simulator delegate
```

There must be exactly one active writer to `PriceCache` at a time. Consumers read only from the cache.

## File Structure

```text
backend/app/market/
  __init__.py
  models.py          # PriceUpdate, MarketSourceMode, MarketStatus
  cache.py           # PriceCache
  interface.py       # MarketDataSource ABC
  utils.py           # ticker normalization helpers
  seed_prices.py     # default prices, GBM params, correlation constants
  simulator.py       # GBMSimulator and SimulatorDataSource
  massive_client.py  # MassiveDataSource, probe helpers, extraction helpers
  factory.py         # create_market_data_source()
  stream.py          # SSE router factory
```

The current code already has most of this shape. The main design upgrade is the Massive EOD fallback path and explicit source-mode metadata.

## Public Models

### `models.py`

```python
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum


class MarketSourceMode(StrEnum):
    SIMULATOR = "simulator"
    MASSIVE_SNAPSHOT = "massive_snapshot"
    MASSIVE_EOD_SIMULATED = "massive_eod_simulated"


@dataclass(frozen=True, slots=True)
class PriceUpdate:
    """Immutable snapshot of one ticker's latest price."""

    ticker: str
    price: float
    previous_price: float
    timestamp: float = field(default_factory=time.time)  # Unix seconds

    @property
    def change(self) -> float:
        return round(self.price - self.previous_price, 4)

    @property
    def change_percent(self) -> float:
        if self.previous_price == 0:
            return 0.0
        return round((self.price - self.previous_price) / self.previous_price * 100, 4)

    @property
    def direction(self) -> str:
        if self.price > self.previous_price:
            return "up"
        if self.price < self.previous_price:
            return "down"
        return "flat"

    def to_dict(self) -> dict:
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
    """Provider status for health/debug endpoints and logs."""

    mode: MarketSourceMode
    provider: str
    healthy: bool
    message: str
    last_success_at: float | None = None
```

`PriceUpdate` remains the only price object consumed by downstream app code. `MarketStatus` is optional for the MVP UI, but useful for `/api/health`, logs, and tests.

## Ticker Normalization

Every source should normalize tickers at the boundary.

```python
def normalize_ticker(ticker: str) -> str:
    return ticker.upper().strip()


def normalize_tickers(tickers: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for raw in tickers:
        ticker = normalize_ticker(raw)
        if ticker and ticker not in seen:
            seen.add(ticker)
            result.append(ticker)
    return result
```

Keep validation of supported/delisted tickers in the watchlist route layer. The market layer assumes it is given syntactically valid tickers and focuses on producing prices.

## Price Cache

### `cache.py`

```python
from __future__ import annotations

import time
from threading import Lock

from .models import PriceUpdate
from .utils import normalize_ticker


class PriceCache:
    """Thread-safe latest-price store.

    Writers: one active MarketDataSource.
    Readers: SSE, portfolio valuation, trade execution, watchlist responses.
    """

    def __init__(self) -> None:
        self._prices: dict[str, PriceUpdate] = {}
        self._version = 0
        self._lock = Lock()

    def update(self, ticker: str, price: float, timestamp: float | None = None) -> PriceUpdate:
        ticker = normalize_ticker(ticker)
        with self._lock:
            previous = self._prices.get(ticker)
            previous_price = previous.price if previous else float(price)
            update = PriceUpdate(
                ticker=ticker,
                price=round(float(price), 2),
                previous_price=round(float(previous_price), 2),
                timestamp=timestamp or time.time(),
            )
            self._prices[ticker] = update
            self._version += 1
            return update

    def get(self, ticker: str) -> PriceUpdate | None:
        with self._lock:
            return self._prices.get(normalize_ticker(ticker))

    def get_price(self, ticker: str) -> float | None:
        update = self.get(ticker)
        return update.price if update else None

    def get_all(self) -> dict[str, PriceUpdate]:
        with self._lock:
            return dict(self._prices)

    def remove(self, ticker: str) -> None:
        ticker = normalize_ticker(ticker)
        with self._lock:
            removed = self._prices.pop(ticker, None)
            if removed is not None:
                self._version += 1

    @property
    def version(self) -> int:
        with self._lock:
            return self._version
```

Use `threading.Lock`, not `asyncio.Lock`, because Massive calls run in worker threads through `asyncio.to_thread`.

## Unified Source Interface

### `interface.py`

```python
from __future__ import annotations

from abc import ABC, abstractmethod

from .models import MarketStatus


class MarketDataSource(ABC):
    """Abstract provider contract.

    Implementations push updates into PriceCache on their own cadence.
    Consumers never ask the source directly for prices.
    """

    @abstractmethod
    async def start(self, tickers: list[str]) -> None:
        """Begin producing price updates."""

    @abstractmethod
    async def stop(self) -> None:
        """Stop background work and release resources."""

    @abstractmethod
    async def add_ticker(self, ticker: str) -> None:
        """Track a new ticker."""

    @abstractmethod
    async def remove_ticker(self, ticker: str) -> None:
        """Stop tracking a ticker and remove it from the cache if safe."""

    @abstractmethod
    def get_tickers(self) -> list[str]:
        """Return active tracked tickers."""

    @abstractmethod
    def get_status(self) -> MarketStatus:
        """Return provider mode and health metadata."""
```

`get_status()` is intentionally sync because it should only return in-memory state.

## Seed Prices and Parameters

### `seed_prices.py`

```python
SEED_PRICES: dict[str, float] = {
    "AAPL": 190.00,
    "GOOGL": 175.00,
    "MSFT": 420.00,
    "AMZN": 185.00,
    "TSLA": 250.00,
    "NVDA": 800.00,
    "META": 500.00,
    "JPM": 195.00,
    "V": 280.00,
    "NFLX": 600.00,
}

TICKER_PARAMS: dict[str, dict[str, float]] = {
    "AAPL": {"sigma": 0.22, "mu": 0.05},
    "GOOGL": {"sigma": 0.25, "mu": 0.05},
    "MSFT": {"sigma": 0.20, "mu": 0.05},
    "AMZN": {"sigma": 0.28, "mu": 0.05},
    "TSLA": {"sigma": 0.50, "mu": 0.03},
    "NVDA": {"sigma": 0.40, "mu": 0.08},
    "META": {"sigma": 0.30, "mu": 0.05},
    "JPM": {"sigma": 0.18, "mu": 0.04},
    "V": {"sigma": 0.17, "mu": 0.04},
    "NFLX": {"sigma": 0.35, "mu": 0.05},
}

DEFAULT_PARAMS: dict[str, float] = {"sigma": 0.25, "mu": 0.05}

CORRELATION_GROUPS: dict[str, set[str]] = {
    "tech": {"AAPL", "GOOGL", "MSFT", "AMZN", "META", "NVDA", "NFLX"},
    "finance": {"JPM", "V"},
}

INTRA_TECH_CORR = 0.6
INTRA_FINANCE_CORR = 0.5
CROSS_GROUP_CORR = 0.3
TSLA_CORR = 0.3
```

Massive EOD closes can override `SEED_PRICES` at runtime. The constants should remain stable and dependency-free.

## Simulator Design

The simulator uses geometric Brownian motion with correlated shocks.

```text
S(t + dt) = S(t) * exp((mu - sigma^2 / 2) * dt + sigma * sqrt(dt) * Z)
```

For 500ms ticks:

```python
TRADING_SECONDS_PER_YEAR = 252 * 6.5 * 3600
DEFAULT_DT = 0.5 / TRADING_SECONDS_PER_YEAR
```

### `GBMSimulator`

```python
from __future__ import annotations

import math
import random

import numpy as np

from .seed_prices import (
    CORRELATION_GROUPS,
    CROSS_GROUP_CORR,
    DEFAULT_PARAMS,
    INTRA_FINANCE_CORR,
    INTRA_TECH_CORR,
    SEED_PRICES,
    TICKER_PARAMS,
    TSLA_CORR,
)
from .utils import normalize_ticker, normalize_tickers


class GBMSimulator:
    TRADING_SECONDS_PER_YEAR = 252 * 6.5 * 3600
    DEFAULT_DT = 0.5 / TRADING_SECONDS_PER_YEAR

    def __init__(
        self,
        tickers: list[str],
        dt: float = DEFAULT_DT,
        event_probability: float = 0.001,
        seed_prices: dict[str, float] | None = None,
    ) -> None:
        self._dt = dt
        self._event_probability = event_probability
        self._external_seeds = {
            normalize_ticker(ticker): float(price)
            for ticker, price in (seed_prices or {}).items()
        }
        self._tickers: list[str] = []
        self._prices: dict[str, float] = {}
        self._params: dict[str, dict[str, float]] = {}
        self._cholesky: np.ndarray | None = None

        for ticker in normalize_tickers(tickers):
            self._add_ticker_internal(ticker)
        self._rebuild_cholesky()

    def step(self) -> dict[str, float]:
        n = len(self._tickers)
        if n == 0:
            return {}

        z = np.random.standard_normal(n)
        if self._cholesky is not None:
            z = self._cholesky @ z

        updates: dict[str, float] = {}
        for i, ticker in enumerate(self._tickers):
            params = self._params[ticker]
            mu = params["mu"]
            sigma = params["sigma"]

            drift = (mu - 0.5 * sigma**2) * self._dt
            diffusion = sigma * math.sqrt(self._dt) * z[i]
            self._prices[ticker] *= math.exp(drift + diffusion)

            if random.random() < self._event_probability:
                shock = random.uniform(0.02, 0.05) * random.choice([-1, 1])
                self._prices[ticker] *= 1 + shock

            updates[ticker] = round(self._prices[ticker], 2)

        return updates

    def add_ticker(self, ticker: str) -> None:
        ticker = normalize_ticker(ticker)
        if ticker in self._prices:
            return
        self._add_ticker_internal(ticker)
        self._rebuild_cholesky()

    def remove_ticker(self, ticker: str) -> None:
        ticker = normalize_ticker(ticker)
        if ticker not in self._prices:
            return
        self._tickers.remove(ticker)
        del self._prices[ticker]
        del self._params[ticker]
        self._rebuild_cholesky()

    def get_price(self, ticker: str) -> float | None:
        return self._prices.get(normalize_ticker(ticker))

    def get_tickers(self) -> list[str]:
        return list(self._tickers)

    def _add_ticker_internal(self, ticker: str) -> None:
        self._tickers.append(ticker)
        self._prices[ticker] = self._initial_price(ticker)
        self._params[ticker] = dict(TICKER_PARAMS.get(ticker, DEFAULT_PARAMS))

    def _initial_price(self, ticker: str) -> float:
        if ticker in self._external_seeds:
            return self._external_seeds[ticker]
        if ticker in SEED_PRICES:
            return SEED_PRICES[ticker]
        return random.uniform(50.0, 300.0)

    def _rebuild_cholesky(self) -> None:
        n = len(self._tickers)
        if n <= 1:
            self._cholesky = None
            return

        corr = np.eye(n)
        for i in range(n):
            for j in range(i + 1, n):
                rho = self._pairwise_correlation(self._tickers[i], self._tickers[j])
                corr[i, j] = rho
                corr[j, i] = rho

        self._cholesky = np.linalg.cholesky(corr)

    @staticmethod
    def _pairwise_correlation(t1: str, t2: str) -> float:
        if t1 == "TSLA" or t2 == "TSLA":
            return TSLA_CORR
        if t1 in CORRELATION_GROUPS["tech"] and t2 in CORRELATION_GROUPS["tech"]:
            return INTRA_TECH_CORR
        if t1 in CORRELATION_GROUPS["finance"] and t2 in CORRELATION_GROUPS["finance"]:
            return INTRA_FINANCE_CORR
        return CROSS_GROUP_CORR
```

### `SimulatorDataSource`

```python
import asyncio
import logging
import time

from .cache import PriceCache
from .interface import MarketDataSource
from .models import MarketSourceMode, MarketStatus
from .utils import normalize_ticker

logger = logging.getLogger(__name__)


class SimulatorDataSource(MarketDataSource):
    def __init__(
        self,
        price_cache: PriceCache,
        update_interval: float = 0.5,
        event_probability: float = 0.001,
        seed_prices: dict[str, float] | None = None,
        source_mode: MarketSourceMode = MarketSourceMode.SIMULATOR,
        status_message: str = "simulator",
    ) -> None:
        self._cache = price_cache
        self._interval = update_interval
        self._event_probability = event_probability
        self._seed_prices = seed_prices or {}
        self._source_mode = source_mode
        self._status_message = status_message
        self._last_success_at: float | None = None
        self._sim: GBMSimulator | None = None
        self._task: asyncio.Task | None = None

    async def start(self, tickers: list[str]) -> None:
        self._sim = GBMSimulator(
            tickers=tickers,
            event_probability=self._event_probability,
            seed_prices=self._seed_prices,
        )
        for ticker in self._sim.get_tickers():
            price = self._sim.get_price(ticker)
            if price is not None:
                self._cache.update(ticker=ticker, price=price)
        self._last_success_at = time.time()
        self._task = asyncio.create_task(self._run_loop(), name="simulator-loop")

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

    async def add_ticker(self, ticker: str) -> None:
        if not self._sim:
            return
        ticker = normalize_ticker(ticker)
        self._sim.add_ticker(ticker)
        price = self._sim.get_price(ticker)
        if price is not None:
            self._cache.update(ticker=ticker, price=price)

    async def remove_ticker(self, ticker: str) -> None:
        if self._sim:
            self._sim.remove_ticker(ticker)
        self._cache.remove(ticker)

    def get_tickers(self) -> list[str]:
        return self._sim.get_tickers() if self._sim else []

    def get_status(self) -> MarketStatus:
        return MarketStatus(
            mode=self._source_mode,
            provider="simulator",
            healthy=True,
            message=self._status_message,
            last_success_at=self._last_success_at,
        )

    async def _run_loop(self) -> None:
        while True:
            try:
                if self._sim:
                    for ticker, price in self._sim.step().items():
                        self._cache.update(ticker=ticker, price=price)
                    self._last_success_at = time.time()
            except Exception:
                logger.exception("Simulator step failed")
            await asyncio.sleep(self._interval)
```

Immediate cache seeding is required so the first frontend render has prices.

## Massive API Design

Massive is optional. Use the official `massive` Python package and REST polling only. Do not use WebSockets in the MVP.

Important endpoints:

| Need | Client method | REST endpoint |
|---|---|---|
| Multiple current prices | `get_snapshot_all(...)` | `/v2/snapshot/locale/us/markets/stocks/tickers?tickers=...` |
| EOD seed for many stocks | `get_grouped_daily_aggs(...)` | `/v2/aggs/grouped/locale/us/market/stocks/{date}` |
| EOD seed for one missing stock | `get_previous_close_agg(...)` | `/v2/aggs/ticker/{ticker}/prev` |

Snapshot access may be unavailable on free/basic keys. In that case, try EOD data and run the simulator from EOD closes.

### Capability Models

```python
from dataclasses import dataclass
from enum import StrEnum


class MassiveCapability(StrEnum):
    SNAPSHOT = "snapshot"
    EOD = "eod"
    NONE = "none"


@dataclass(frozen=True, slots=True)
class MassiveProbeResult:
    capability: MassiveCapability
    reason: str
    seed_prices: dict[str, float]
```

### Snapshot Extraction

```python
import time
from typing import Any


def extract_snapshot_price(snapshot: Any) -> float | None:
    last_trade = getattr(snapshot, "last_trade", None)
    if last_trade is not None and getattr(last_trade, "price", None) is not None:
        return float(last_trade.price)

    day = getattr(snapshot, "day", None)
    if day is not None and getattr(day, "close", None) is not None:
        return float(day.close)

    prev_day = getattr(snapshot, "prev_day", None)
    if prev_day is not None and getattr(prev_day, "close", None) is not None:
        return float(prev_day.close)

    return None


def extract_snapshot_timestamp(snapshot: Any) -> float:
    last_trade = getattr(snapshot, "last_trade", None)
    if last_trade is not None and getattr(last_trade, "timestamp", None) is not None:
        return float(last_trade.timestamp) / 1000.0
    day = getattr(snapshot, "day", None)
    if day is not None and getattr(day, "timestamp", None) is not None:
        return float(day.timestamp) / 1000.0
    return time.time()
```

`last_trade.price` is preferred. `day.close` and `prev_day.close` are fallbacks for market-closed or partial responses.

### EOD Seed Fetching

```python
from datetime import date, timedelta


def recent_weekdays(today: date, count: int = 7) -> list[date]:
    days: list[date] = []
    cursor = today
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor -= timedelta(days=1)
    return days


def fetch_grouped_daily_seed_prices(client, tickers: list[str], today: date) -> dict[str, float]:
    wanted = set(tickers)
    for day in recent_weekdays(today):
        aggs = client.get_grouped_daily_aggs(
            date=day,
            adjusted=True,
            market_type="stocks",
            include_otc=False,
        )
        prices = {
            agg.ticker.upper(): float(agg.close)
            for agg in aggs
            if getattr(agg, "ticker", None)
            and agg.ticker.upper() in wanted
            and getattr(agg, "close", None) is not None
        }
        if prices:
            return prices
    return {}


def fetch_previous_close_seed(client, ticker: str) -> float | None:
    result = client.get_previous_close_agg(ticker=ticker, adjusted=True)
    rows = getattr(result, "results", None) or []
    if rows and getattr(rows[0], "close", None) is not None:
        return float(rows[0].close)
    return None
```

Use grouped daily first because one call can seed the whole watchlist and works with constrained rate limits.

### Capability Probe

```python
import asyncio
from datetime import date

from massive import RESTClient
from massive.rest.models import SnapshotMarketType


async def probe_massive(api_key: str, tickers: list[str]) -> MassiveProbeResult:
    tickers = normalize_tickers(tickers)
    client = RESTClient(api_key=api_key)

    try:
        snapshots = await asyncio.to_thread(
            client.get_snapshot_all,
            market_type=SnapshotMarketType.STOCKS,
            tickers=tickers[:3],
        )
        snapshot_prices = {
            snap.ticker.upper(): price
            for snap in snapshots
            if (price := extract_snapshot_price(snap)) is not None
        }
        if snapshot_prices:
            return MassiveProbeResult(
                capability=MassiveCapability.SNAPSHOT,
                reason="snapshot_ok",
                seed_prices=snapshot_prices,
            )
        snapshot_reason = "snapshot returned no usable prices"
    except Exception as exc:
        snapshot_reason = f"snapshot failed: {type(exc).__name__}: {exc}"

    try:
        seeds = await asyncio.to_thread(fetch_grouped_daily_seed_prices, client, tickers, date.today())
        missing = [ticker for ticker in tickers if ticker not in seeds]
        for ticker in missing:
            seed = await asyncio.to_thread(fetch_previous_close_seed, client, ticker)
            if seed is not None:
                seeds[ticker] = seed
        if seeds:
            return MassiveProbeResult(
                capability=MassiveCapability.EOD,
                reason=snapshot_reason,
                seed_prices=seeds,
            )
    except Exception as exc:
        return MassiveProbeResult(
            capability=MassiveCapability.NONE,
            reason=f"{snapshot_reason}; eod failed: {type(exc).__name__}: {exc}",
            seed_prices={},
        )

    return MassiveProbeResult(
        capability=MassiveCapability.NONE,
        reason=f"{snapshot_reason}; eod returned no usable prices",
        seed_prices={},
    )
```

The probe should run at startup when an API key is present. If the probe cannot confirm snapshot access, never leave the app with an empty price feed; fall back to a simulator.

### `MassiveDataSource`

`MassiveDataSource` should be able to delegate to a seeded simulator if snapshot access is unavailable. This keeps the public factory simple and preserves one `MarketDataSource` object.

```python
import asyncio
import logging
import time

from massive import RESTClient
from massive.rest.models import SnapshotMarketType

from .cache import PriceCache
from .interface import MarketDataSource
from .models import MarketSourceMode, MarketStatus
from .simulator import SimulatorDataSource
from .utils import normalize_ticker, normalize_tickers

logger = logging.getLogger(__name__)


class MassiveDataSource(MarketDataSource):
    def __init__(
        self,
        api_key: str,
        price_cache: PriceCache,
        poll_interval: float = 15.0,
    ) -> None:
        self._api_key = api_key
        self._cache = price_cache
        self._interval = poll_interval
        self._tickers: list[str] = []
        self._client: RESTClient | None = None
        self._task: asyncio.Task | None = None
        self._delegate: SimulatorDataSource | None = None
        self._status = MarketStatus(
            mode=MarketSourceMode.MASSIVE_SNAPSHOT,
            provider="massive",
            healthy=False,
            message="not_started",
        )

    async def start(self, tickers: list[str]) -> None:
        self._tickers = normalize_tickers(tickers)
        self._client = RESTClient(api_key=self._api_key)

        probe = await probe_massive(self._api_key, self._tickers)
        if probe.capability == MassiveCapability.EOD:
            self._delegate = SimulatorDataSource(
                price_cache=self._cache,
                seed_prices=probe.seed_prices,
                source_mode=MarketSourceMode.MASSIVE_EOD_SIMULATED,
                status_message=f"Massive snapshot unavailable; seeded from EOD: {probe.reason}",
            )
            await self._delegate.start(self._tickers)
            self._status = self._delegate.get_status()
            return

        if probe.capability == MassiveCapability.NONE:
            self._delegate = SimulatorDataSource(
                price_cache=self._cache,
                source_mode=MarketSourceMode.SIMULATOR,
                status_message=f"Massive unavailable; using simulator: {probe.reason}",
            )
            await self._delegate.start(self._tickers)
            self._status = self._delegate.get_status()
            return

        await self._poll_once()
        self._task = asyncio.create_task(self._poll_loop(), name="massive-poller")
        self._status = MarketStatus(
            mode=MarketSourceMode.MASSIVE_SNAPSHOT,
            provider="massive",
            healthy=True,
            message="snapshot polling",
            last_success_at=time.time(),
        )

    async def stop(self) -> None:
        if self._delegate:
            await self._delegate.stop()
            self._delegate = None
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        self._client = None

    async def add_ticker(self, ticker: str) -> None:
        ticker = normalize_ticker(ticker)
        if self._delegate:
            await self._delegate.add_ticker(ticker)
            return
        if ticker not in self._tickers:
            self._tickers.append(ticker)
            # Optional: call _poll_once() here if rate budget allows.

    async def remove_ticker(self, ticker: str) -> None:
        ticker = normalize_ticker(ticker)
        if self._delegate:
            await self._delegate.remove_ticker(ticker)
            return
        self._tickers = [t for t in self._tickers if t != ticker]
        self._cache.remove(ticker)

    def get_tickers(self) -> list[str]:
        if self._delegate:
            return self._delegate.get_tickers()
        return list(self._tickers)

    def get_status(self) -> MarketStatus:
        if self._delegate:
            return self._delegate.get_status()
        return self._status

    async def _poll_loop(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            await self._poll_once()

    async def _poll_once(self) -> None:
        if not self._client or not self._tickers:
            return

        try:
            snapshots = await asyncio.to_thread(
                self._client.get_snapshot_all,
                market_type=SnapshotMarketType.STOCKS,
                tickers=self._tickers,
            )
            processed = 0
            for snap in snapshots:
                price = extract_snapshot_price(snap)
                if price is None:
                    logger.warning("Massive snapshot missing price for %s", getattr(snap, "ticker", "unknown"))
                    continue
                self._cache.update(
                    ticker=snap.ticker,
                    price=price,
                    timestamp=extract_snapshot_timestamp(snap),
                )
                processed += 1

            self._status = MarketStatus(
                mode=MarketSourceMode.MASSIVE_SNAPSHOT,
                provider="massive",
                healthy=processed > 0,
                message=f"updated {processed}/{len(self._tickers)} tickers",
                last_success_at=time.time() if processed > 0 else self._status.last_success_at,
            )
        except Exception as exc:
            logger.error("Massive poll failed: %s", exc)
            self._status = MarketStatus(
                mode=MarketSourceMode.MASSIVE_SNAPSHOT,
                provider="massive",
                healthy=False,
                message=f"poll failed: {type(exc).__name__}: {exc}",
                last_success_at=self._status.last_success_at,
            )
```

## Factory

Keep the factory small. It should select by environment; `MassiveDataSource.start()` handles capability probing and fallback.

### `factory.py`

```python
import logging
import os

from .cache import PriceCache
from .interface import MarketDataSource
from .massive_client import MassiveDataSource
from .simulator import SimulatorDataSource

logger = logging.getLogger(__name__)


def create_market_data_source(price_cache: PriceCache) -> MarketDataSource:
    api_key = os.environ.get("MASSIVE_API_KEY", "").strip()
    if api_key:
        logger.info("Market data source: Massive with capability probe")
        return MassiveDataSource(api_key=api_key, price_cache=price_cache)

    logger.info("Market data source: simulator")
    return SimulatorDataSource(price_cache=price_cache)
```

This preserves the current synchronous startup shape:

```python
cache = PriceCache()
source = create_market_data_source(cache)
await source.start(initial_tickers)
```

## SSE Streaming

The SSE endpoint streams cache snapshots only when the cache version changes.

### `stream.py`

```python
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncGenerator

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from .cache import PriceCache

logger = logging.getLogger(__name__)


def create_stream_router(price_cache: PriceCache) -> APIRouter:
    router = APIRouter(prefix="/api/stream", tags=["streaming"])

    @router.get("/prices")
    async def stream_prices(request: Request) -> StreamingResponse:
        return StreamingResponse(
            _generate_events(price_cache, request),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    return router


async def _generate_events(
    price_cache: PriceCache,
    request: Request,
    interval: float = 0.5,
) -> AsyncGenerator[str, None]:
    yield "retry: 1000\n\n"
    last_version = -1

    while True:
        if await request.is_disconnected():
            break

        version = price_cache.version
        if version != last_version:
            last_version = version
            data = {
                ticker: update.to_dict()
                for ticker, update in price_cache.get_all().items()
            }
            yield f"data: {json.dumps(data)}\n\n"

        await asyncio.sleep(interval)
```

Avoid a module-level `router`; constructing a new router inside `create_stream_router()` prevents duplicate-route surprises in tests.

## FastAPI Lifespan Integration

The market system starts once and stops once with the app. Prefer an app factory so the router can close over the same `PriceCache` that the lifespan-managed source writes to.

```python
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.market import PriceCache, create_market_data_source, create_stream_router


def create_app() -> FastAPI:
    price_cache = PriceCache()
    market_source = create_market_data_source(price_cache)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.price_cache = price_cache
        app.state.market_source = market_source
        initial_tickers = await load_watchlist_and_position_tickers()
        await market_source.start(initial_tickers)
        yield
        await market_source.stop()

    app = FastAPI(title="FinAlly", lifespan=lifespan)
    app.include_router(create_stream_router(price_cache))
    return app
```

## Watchlist and Portfolio Coordination

The market data source should track the union of:

- Watchlist tickers.
- Open position tickers.

This prevents portfolio valuation from losing prices when a user removes a held ticker from the watchlist.

Add flow:

```text
POST /api/watchlist
  -> validate ticker
  -> insert row idempotently
  -> await source.add_ticker(ticker)
  -> return latest cache price if present
```

Remove flow:

```text
DELETE /api/watchlist/{ticker}
  -> delete watchlist row
  -> if no open position remains, await source.remove_ticker(ticker)
  -> return removed flag
```

Trade flow:

```text
POST /api/portfolio/trade
  -> validate side and quantity
  -> price = price_cache.get_price(ticker)
  -> if price missing, return 400
  -> execute simulated market order at cached price
  -> if buy creates new position for untracked ticker, await source.add_ticker(ticker)
```

Never call Massive inside a request handler to fill a trade. Trades fill from cached prices only.

## Error Handling

| Case | Behavior |
|---|---|
| No `MASSIVE_API_KEY` | Use `SimulatorDataSource` |
| Invalid key / `401` | Fall back to simulator and log status |
| Snapshot permission denied / `403` | Try EOD seed, then simulator |
| Rate limited / `429` during probe | Fall back to simulator; retry only on restart for MVP |
| Rate limited during polling | Keep last cached prices, mark status unhealthy, retry next interval |
| Snapshot missing price field | Skip that ticker, keep previous cached price |
| Empty watchlist | Start with no prices; adding a ticker seeds/polls later |
| Cache miss during trade | Return `400` with "No price available for TICKER yet" |

The app should prefer stale-but-present prices over blank UI during transient Massive failures.

## Configuration

| Setting | Location | Default |
|---|---|---:|
| `MASSIVE_API_KEY` | environment | empty |
| Simulator update interval | `SimulatorDataSource` | 0.5s |
| Simulator event probability | `GBMSimulator` | 0.001 |
| Massive poll interval | `MassiveDataSource` | 15s |
| SSE interval | `_generate_events` | 0.5s |
| SSE retry | event stream directive | 1000ms |

Keep the free-tier-friendly Massive default at 15 seconds. Paid users can lower the interval through a future env var such as `MARKET_MASSIVE_POLL_INTERVAL_SECONDS`.

## Public Package Exports

### `__init__.py`

```python
from .cache import PriceCache
from .factory import create_market_data_source
from .interface import MarketDataSource
from .models import MarketSourceMode, MarketStatus, PriceUpdate
from .stream import create_stream_router

__all__ = [
    "PriceCache",
    "PriceUpdate",
    "MarketDataSource",
    "MarketSourceMode",
    "MarketStatus",
    "create_market_data_source",
    "create_stream_router",
]
```

## Testing Plan

Market data tests should be mostly deterministic and should not call the live Massive API.

### Unit Tests

- `PriceUpdate`
  - first update is flat
  - direction up/down/flat
  - percent change handles zero previous price
  - `to_dict()` uses canonical API fields
- `PriceCache`
  - normalization to uppercase
  - rounding to cents
  - version increments on update and removal
  - concurrent writer smoke test with threads
- `GBMSimulator`
  - full default watchlist initializes successfully
  - prices stay positive over many steps
  - external seed prices override defaults
  - add/remove ticker rebuilds Cholesky
  - unknown ticker starts in `$50-$300`
- `SimulatorDataSource`
  - start seeds cache immediately
  - add ticker seeds cache immediately
  - stop can be called twice
  - status mode is `simulator` or `massive_eod_simulated`
- Massive helpers
  - snapshot extraction supports `last_trade`, `day.close`, and `prev_day.close`
  - timestamp extraction converts milliseconds to seconds
  - EOD grouped daily extraction returns only requested tickers
  - probe maps snapshot success, EOD fallback, and total failure correctly
- `MassiveDataSource`
  - snapshot poll updates cache
  - malformed ticker snapshot is skipped
  - API error does not raise
  - EOD fallback delegates to `SimulatorDataSource`
- SSE
  - initial `retry` directive is emitted
  - event payload is valid JSON
  - no duplicate payload when cache version is unchanged

### Mock Snapshot Example

```python
from types import SimpleNamespace


def make_snapshot(ticker: str, price: float, timestamp_ms: int):
    return SimpleNamespace(
        ticker=ticker,
        last_trade=SimpleNamespace(price=price, timestamp=timestamp_ms),
        day=None,
        prev_day=None,
    )
```

### Probe Test Shape

```python
@pytest.mark.asyncio
async def test_probe_uses_eod_when_snapshot_forbidden(monkeypatch):
    class FakeClient:
        def __init__(self, api_key: str):
            pass

        def get_snapshot_all(self, *args, **kwargs):
            raise Exception("403 forbidden")

    monkeypatch.setattr("app.market.massive_client.RESTClient", FakeClient)
    monkeypatch.setattr(
        "app.market.massive_client.fetch_grouped_daily_seed_prices",
        lambda client, tickers, today: {"AAPL": 188.5},
    )

    result = await probe_massive("key", ["AAPL"])

    assert result.capability == MassiveCapability.EOD
    assert result.seed_prices == {"AAPL": 188.5}
```

## Implementation Order

1. Add `MarketSourceMode`, `MarketStatus`, and ticker normalization helper.
2. Update `PriceCache` to normalize tickers and lock `version`.
3. Add `seed_prices` support to `GBMSimulator` and `SimulatorDataSource`.
4. Add Massive extraction helpers and `probe_massive`.
5. Update `MassiveDataSource.start()` to probe and delegate to seeded simulator when needed.
6. Move `stream.py` router creation inside `create_stream_router`.
7. Add/adjust tests for EOD fallback and source status.
8. Wire lifespan with the union of watchlist and open-position tickers.

## Non-Goals

- No real-money trading.
- No order book, limit orders, or partial fills.
- No Massive WebSocket integration for the MVP.
- No historical chart backfill beyond optional future use of Massive aggregate bars.
- No multi-user market data partitioning; the single-user app tracks one global ticker union.

## Reference Documents

- `planning/PLAN.md`
- `planning/MASSIVE_API.md`
- `planning/MARKET_INTERFACE.md`
- `planning/MARKET_SIMULATOR.md`
- `planning/MARKET_DATA_SUMMARY.md`
- `planning/archive/MARKET_DATA_DESIGN.md`
- `planning/archive/MARKET_DATA_REVIEW.md`

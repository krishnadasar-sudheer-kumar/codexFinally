# Market Data Interface

This document defines the unified Python market-data API for FinAlly. The backend must expose one source-agnostic interface to the rest of the app, whether prices come from Massive snapshots, Massive end-of-day seeded simulation, or the built-in simulator.

## Design Goals

- Keep frontend, portfolio valuation, trade execution, and SSE streaming independent of the market data provider.
- Use Massive snapshots when `MASSIVE_API_KEY` is set and the key has snapshot access.
- If the key lacks realtime/snapshot access but can read end-of-day prices, seed the simulator from Massive EOD closes and run a live-feeling local simulation.
- If no key is present or Massive access fails entirely, use deterministic local defaults.
- Preserve app startup: market data problems should degrade gracefully, not prevent the workstation from loading.

## Source Modes

The implementation should distinguish source mode internally, even if the MVP UI does not display it immediately.

```python
from enum import StrEnum

class MarketSourceMode(StrEnum):
    SIMULATOR = "simulator"
    MASSIVE_SNAPSHOT = "massive_snapshot"
    MASSIVE_EOD_SIMULATED = "massive_eod_simulated"
```

Selection rules:

1. `MASSIVE_API_KEY` empty: `SIMULATOR`.
2. `MASSIVE_API_KEY` set and snapshot probe returns usable prices: `MASSIVE_SNAPSHOT`.
3. `MASSIVE_API_KEY` set, snapshot unavailable, EOD probe returns closes: `MASSIVE_EOD_SIMULATED`.
4. `MASSIVE_API_KEY` set but all probes fail: `SIMULATOR`, with a warning log.

## Public Data Model

`PriceUpdate` is the only price object that should leave `backend/app/market`.

```python
from dataclasses import dataclass, field
import time

@dataclass(frozen=True, slots=True)
class PriceUpdate:
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
```

Rules:

- Tickers are uppercase.
- Prices are rounded to cents at cache write time.
- Timestamps are Unix seconds inside FinAlly.
- `previous_price` means the previous emitted/cache price, not necessarily the market's previous close.

## Price Cache

The `PriceCache` is the single in-memory truth for latest prices.

```python
from threading import Lock

class PriceCache:
    def __init__(self) -> None:
        self._prices: dict[str, PriceUpdate] = {}
        self._version = 0
        self._lock = Lock()

    def update(self, ticker: str, price: float, timestamp: float | None = None) -> PriceUpdate:
        ticker = ticker.upper().strip()
        with self._lock:
            previous = self._prices.get(ticker)
            previous_price = previous.price if previous else price
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
            return self._prices.get(ticker.upper().strip())

    def get_price(self, ticker: str) -> float | None:
        update = self.get(ticker)
        return update.price if update else None

    def get_all(self) -> dict[str, PriceUpdate]:
        with self._lock:
            return dict(self._prices)

    def remove(self, ticker: str) -> None:
        with self._lock:
            self._prices.pop(ticker.upper().strip(), None)
            self._version += 1

    @property
    def version(self) -> int:
        return self._version
```

Use a thread lock rather than an `asyncio.Lock` because the Massive client runs inside worker threads via `asyncio.to_thread(...)`.

## Abstract Interface

Both real and simulated sources implement the same lifecycle.

```python
from abc import ABC, abstractmethod

class MarketDataSource(ABC):
    @abstractmethod
    async def start(self, tickers: list[str]) -> None:
        """Begin producing price updates for the given tickers."""

    @abstractmethod
    async def stop(self) -> None:
        """Stop background work and release resources."""

    @abstractmethod
    async def add_ticker(self, ticker: str) -> None:
        """Add a ticker to the active set."""

    @abstractmethod
    async def remove_ticker(self, ticker: str) -> None:
        """Remove a ticker from the active set and cache."""

    @abstractmethod
    def get_tickers(self) -> list[str]:
        """Return active tickers."""
```

The data source pushes updates into `PriceCache`; consumers never ask the data source directly for prices.

## Factory Contract

The factory should perform provider selection and startup capability probing. For a fast MVP, the probe can run inside `start(...)` instead, but the resulting behavior should match this contract.

```python
import os

async def create_market_data_source(price_cache: PriceCache) -> MarketDataSource:
    api_key = os.environ.get("MASSIVE_API_KEY", "").strip()

    if not api_key:
        return SimulatorDataSource(price_cache=price_cache)

    probe = await probe_massive(api_key=api_key, tickers=DEFAULT_WATCHLIST)

    if probe.capability == MassiveCapability.SNAPSHOT:
        return MassiveDataSource(api_key=api_key, price_cache=price_cache)

    if probe.capability == MassiveCapability.EOD:
        return SimulatorDataSource(
            price_cache=price_cache,
            seed_prices=probe.seed_prices,
            source_mode=MarketSourceMode.MASSIVE_EOD_SIMULATED,
        )

    return SimulatorDataSource(price_cache=price_cache)
```

If keeping a synchronous factory for simplicity, expose a second initialization step:

```python
source = create_market_data_source(cache)
await source.start(initial_tickers)
```

In that design, `MassiveDataSource.start(...)` performs the snapshot/EOD probe and can delegate to an internal seeded simulator if snapshot access is unavailable.

## Massive Snapshot Source

`MassiveDataSource` should poll all watched tickers in one request.

```python
class MassiveDataSource(MarketDataSource):
    def __init__(
        self,
        api_key: str,
        price_cache: PriceCache,
        poll_interval: float = 15.0,
    ) -> None:
        self._client = RESTClient(api_key=api_key)
        self._cache = price_cache
        self._interval = poll_interval
        self._tickers: list[str] = []
        self._task: asyncio.Task | None = None

    async def start(self, tickers: list[str]) -> None:
        self._tickers = normalize_tickers(tickers)
        await self._poll_once()
        self._task = asyncio.create_task(self._poll_loop(), name="massive-poller")

    async def _poll_once(self) -> None:
        if not self._tickers:
            return
        snapshots = await asyncio.to_thread(
            self._client.get_snapshot_all,
            market_type=SnapshotMarketType.STOCKS,
            tickers=self._tickers,
        )
        for snap in snapshots:
            price = extract_snapshot_price(snap)
            if price is not None:
                self._cache.update(
                    ticker=snap.ticker,
                    price=price,
                    timestamp=extract_snapshot_timestamp(snap),
                )
```

Snapshot extraction priority:

1. `last_trade.price`
2. `day.close`
3. `prev_day.close`
4. Skip update and keep the previous cache value

This lets the app continue during market closures, early sessions, or partial provider responses.

## Massive EOD Seeded Simulator

When `MASSIVE_API_KEY` cannot access snapshots but can access daily aggregates:

1. Fetch grouped daily aggregates for the most recent trading day available.
2. Extract close prices for the current watchlist.
3. Pass those prices into `SimulatorDataSource(seed_prices=...)`.
4. Run the normal simulator at 500ms intervals.

This gives users with free or EOD-only keys realistic starting prices without pretending the simulated stream is actual realtime market data.

```python
source = SimulatorDataSource(
    price_cache=cache,
    seed_prices={"AAPL": 183.12, "MSFT": 412.40},
    source_mode=MarketSourceMode.MASSIVE_EOD_SIMULATED,
)
```

## Simulator Source

The simulator wraps `GBMSimulator` and writes each generated step to the shared cache.

```python
class SimulatorDataSource(MarketDataSource):
    def __init__(
        self,
        price_cache: PriceCache,
        update_interval: float = 0.5,
        seed_prices: dict[str, float] | None = None,
        source_mode: MarketSourceMode = MarketSourceMode.SIMULATOR,
    ) -> None:
        self._cache = price_cache
        self._interval = update_interval
        self._seed_prices = seed_prices or {}
        self._source_mode = source_mode
        self._sim: GBMSimulator | None = None
        self._task: asyncio.Task | None = None

    async def start(self, tickers: list[str]) -> None:
        self._sim = GBMSimulator(tickers=tickers, seed_prices=self._seed_prices)
        for ticker in tickers:
            price = self._sim.get_price(ticker)
            if price is not None:
                self._cache.update(ticker=ticker, price=price)
        self._task = asyncio.create_task(self._run_loop(), name="simulator-loop")
```

## SSE Integration

The SSE endpoint reads only from `PriceCache`.

```python
async def price_stream(cache: PriceCache):
    last_version = -1
    while True:
        if cache.version != last_version:
            payload = {
                ticker: update.to_dict()
                for ticker, update in cache.get_all().items()
            }
            yield f"data: {json.dumps(payload)}\n\n"
            last_version = cache.version
        await asyncio.sleep(0.5)
```

The version counter prevents resending identical data between Massive polling intervals.

## Trading Integration

Trade execution should read current prices from `PriceCache.get_price(ticker)`.

Rules:

- If a price exists, fill the simulated market order at that price.
- If a ticker was just added and no price exists yet, return a clear `400` response: "No price available for TICKER yet."
- The simulator should seed immediately on add, so this gap mostly applies to Massive snapshot mode.
- Never call Massive synchronously inside a trade request. Trading should not block on provider latency.

## Watchlist Lifecycle

`add_ticker`:

- Normalize to uppercase.
- Add to active source.
- Simulator: seed immediately and write to cache.
- Massive snapshot: include on the next poll. Optionally do a one-off poll if rate budget allows.

`remove_ticker`:

- Remove from active source.
- Remove from cache.
- SSE version increments so clients can remove the row.

## File Structure

```text
backend/app/market/
  __init__.py
  models.py          # PriceUpdate
  cache.py           # PriceCache
  interface.py       # MarketDataSource
  factory.py         # Source selection
  massive_client.py  # Massive snapshot poller and capability probe
  simulator.py       # SimulatorDataSource and GBMSimulator
  seed_prices.py     # Default seeds, GBM params, correlation groups
  stream.py          # SSE router factory
```

## Testing Expectations

Unit tests should cover:

- `PriceUpdate` change, percent change, direction, and serialization.
- `PriceCache` rounding, previous price behavior, removal, and version increments.
- `MarketDataSource` lifecycle for simulator and Massive with mocked client responses.
- Massive snapshot response parsing with `last_trade`, `day.close`, missing fields, and permission failures.
- EOD fallback path that seeds simulator prices from grouped daily aggregates.
- SSE stream emits only on cache version changes.

## Implementation Notes

- Keep Massive API objects out of API route responses.
- Keep provider errors out of user-facing payloads except for concise status messages.
- Log the chosen source mode at startup.
- Keep the default simulator path dependency-free beyond packages already in the backend.
- Do not use WebSockets for Massive in the MVP; REST polling plus SSE is the project contract.

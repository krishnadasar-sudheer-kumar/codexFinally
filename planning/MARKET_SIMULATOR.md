# Market Simulator

This document defines the approach and code structure for simulating stock prices in FinAlly. The simulator is the default market data source and is also used when a Massive API key has only end-of-day access.

## Purpose

The simulator should make the trading workstation feel alive without external dependencies:

- Prices update about twice per second.
- Prices stay positive and realistic.
- Stocks in related groups tend to move together.
- Default tickers start near plausible real-world prices.
- End-of-day Massive closes can override default seed prices when available.
- Occasional larger moves create visible price flashes and more interesting demo behavior.

The simulator is not a backtesting engine and should not be treated as a market model for real trading decisions.

## Model

Use geometric Brownian motion (GBM):

```text
S(t + dt) = S(t) * exp((mu - sigma^2 / 2) * dt + sigma * sqrt(dt) * Z)
```

Where:

- `S(t)` is the current price.
- `mu` is annualized drift.
- `sigma` is annualized volatility.
- `dt` is the time step as a fraction of a trading year.
- `Z` is a standard normal random draw.

For a 500ms update interval:

```python
TRADING_SECONDS_PER_YEAR = 252 * 6.5 * 3600
DEFAULT_DT = 0.5 / TRADING_SECONDS_PER_YEAR
```

GBM is useful here because it is multiplicative, so prices cannot become negative.

## Correlation

Real stocks do not move independently. Generate independent normal random variables, then apply a Cholesky decomposition of a correlation matrix.

```python
z_independent = np.random.standard_normal(n)
z_correlated = cholesky @ z_independent
```

Default groups:

```python
CORRELATION_GROUPS = {
    "tech": {"AAPL", "GOOGL", "MSFT", "AMZN", "META", "NVDA", "NFLX"},
    "finance": {"JPM", "V"},
}

INTRA_TECH_CORR = 0.6
INTRA_FINANCE_CORR = 0.5
CROSS_GROUP_CORR = 0.3
TSLA_CORR = 0.3
```

Keep this intentionally simple. The goal is dashboard realism, not a calibrated factor model.

## Seed Prices

Default seed prices:

```python
SEED_PRICES = {
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
```

Unknown tickers start at a random price between `$50` and `$300`.

When Massive EOD data is available, source seeds in this order:

1. Massive grouped daily close for the ticker.
2. Massive previous close for the ticker if grouped daily is missing it.
3. `SEED_PRICES` constant.
4. Random unknown ticker seed.

This lets a free/EOD Massive key make the simulation start from recent market prices.

## Per-Ticker Parameters

```python
TICKER_PARAMS = {
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

DEFAULT_PARAMS = {"sigma": 0.25, "mu": 0.05}
```

These parameters are intentionally dramatic enough for a course demo. If the UI feels too quiet, increase `event_probability` before increasing every ticker's volatility.

## Random Events

Add rare ticker-specific shock events:

```python
if random.random() < event_probability:
    shock_magnitude = random.uniform(0.02, 0.05)
    shock_sign = random.choice([-1, 1])
    price *= 1 + shock_magnitude * shock_sign
```

Default:

```python
event_probability = 0.001
```

At 10 tickers and 2 updates per second, this creates one visible event roughly every 50 seconds across the watchlist.

## Core Class

```python
import math
import random
import numpy as np

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
        self._event_prob = event_probability
        self._external_seeds = {k.upper(): v for k, v in (seed_prices or {}).items()}
        self._tickers: list[str] = []
        self._prices: dict[str, float] = {}
        self._params: dict[str, dict[str, float]] = {}
        self._cholesky: np.ndarray | None = None

        for ticker in tickers:
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

            if random.random() < self._event_prob:
                shock = random.uniform(0.02, 0.05) * random.choice([-1, 1])
                self._prices[ticker] *= 1 + shock

            updates[ticker] = round(self._prices[ticker], 2)

        return updates

    def add_ticker(self, ticker: str) -> None:
        ticker = ticker.upper().strip()
        if ticker in self._prices:
            return
        self._add_ticker_internal(ticker)
        self._rebuild_cholesky()

    def remove_ticker(self, ticker: str) -> None:
        ticker = ticker.upper().strip()
        if ticker not in self._prices:
            return
        self._tickers.remove(ticker)
        del self._prices[ticker]
        del self._params[ticker]
        self._rebuild_cholesky()

    def get_price(self, ticker: str) -> float | None:
        return self._prices.get(ticker.upper().strip())

    def get_tickers(self) -> list[str]:
        return list(self._tickers)

    def _add_ticker_internal(self, ticker: str) -> None:
        ticker = ticker.upper().strip()
        self._tickers.append(ticker)
        self._prices[ticker] = self._initial_price(ticker)
        self._params[ticker] = dict(TICKER_PARAMS.get(ticker, DEFAULT_PARAMS))

    def _initial_price(self, ticker: str) -> float:
        if ticker in self._external_seeds:
            return float(self._external_seeds[ticker])
        if ticker in SEED_PRICES:
            return float(SEED_PRICES[ticker])
        return random.uniform(50.0, 300.0)
```

## Cholesky Rebuild

Rebuild the correlation matrix whenever tickers are added or removed.

```python
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
```

Ticker counts are small, so the `O(n^2)` rebuild is fine.

## Async Data Source

`SimulatorDataSource` adapts the math engine to the shared `MarketDataSource` interface.

```python
class SimulatorDataSource(MarketDataSource):
    def __init__(
        self,
        price_cache: PriceCache,
        update_interval: float = 0.5,
        event_probability: float = 0.001,
        seed_prices: dict[str, float] | None = None,
    ) -> None:
        self._cache = price_cache
        self._interval = update_interval
        self._event_prob = event_probability
        self._seed_prices = seed_prices or {}
        self._sim: GBMSimulator | None = None
        self._task: asyncio.Task | None = None

    async def start(self, tickers: list[str]) -> None:
        self._sim = GBMSimulator(
            tickers=tickers,
            event_probability=self._event_prob,
            seed_prices=self._seed_prices,
        )
        for ticker in tickers:
            price = self._sim.get_price(ticker)
            if price is not None:
                self._cache.update(ticker=ticker, price=price)
        self._task = asyncio.create_task(self._run_loop(), name="simulator-loop")

    async def add_ticker(self, ticker: str) -> None:
        if self._sim is None:
            return
        self._sim.add_ticker(ticker)
        price = self._sim.get_price(ticker)
        if price is not None:
            self._cache.update(ticker=ticker, price=price)

    async def remove_ticker(self, ticker: str) -> None:
        if self._sim is not None:
            self._sim.remove_ticker(ticker)
        self._cache.remove(ticker)

    async def _run_loop(self) -> None:
        while True:
            if self._sim is not None:
                for ticker, price in self._sim.step().items():
                    self._cache.update(ticker=ticker, price=price)
            await asyncio.sleep(self._interval)
```

## File Structure

```text
backend/app/market/
  seed_prices.py
    SEED_PRICES
    TICKER_PARAMS
    DEFAULT_PARAMS
    CORRELATION_GROUPS
    correlation constants

  simulator.py
    GBMSimulator
    SimulatorDataSource
```

Keep constants in `seed_prices.py` so Massive EOD fallback code can reuse the seed vocabulary without importing simulator internals.

## Testing Expectations

Tests should cover:

- Prices never become zero or negative.
- `step()` returns one rounded price per tracked ticker.
- Adding/removing tickers updates simulator state and Cholesky decomposition.
- Unknown tickers get a plausible random seed.
- External seed prices override built-in defaults.
- Pairwise correlations match the documented constants.
- The full default watchlist builds a valid Cholesky matrix.
- `SimulatorDataSource.start()` seeds the cache immediately.
- `SimulatorDataSource.add_ticker()` seeds the new ticker immediately.

## Tuning Guidance

- If prices look too static, first increase event frequency from `0.001` to `0.002`.
- If normal ticks look too static, raise `sigma` values modestly.
- If the whole watchlist moves as one block too often, lower `CROSS_GROUP_CORR`.
- If the dashboard feels chaotic, reduce shock size before reducing normal volatility.
- Keep output rounded to cents; sub-cent churn adds noise without improving the UX.

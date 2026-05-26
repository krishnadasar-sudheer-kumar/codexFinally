# Massive API Research for FinAlly

Current as of 2026-05-09. Massive is the renamed Polygon.io API. Existing Polygon API keys and the legacy `api.polygon.io` base remain compatible, but new FinAlly code should use the Massive branding, Python package, and `https://api.massive.com` base URL.

## Executive Summary

FinAlly should use Massive only from the backend and only through one market-data abstraction. For multiple watched stocks, the preferred realtime/delayed endpoint is the stock full-market snapshot endpoint filtered by a comma-separated ticker list:

```text
GET https://api.massive.com/v2/snapshot/locale/us/markets/stocks/tickers?tickers=AAPL,MSFT,NVDA
```

This endpoint retrieves all watched tickers in one request, which matters because the free Stocks Basic plan allows only 5 API calls per minute. However, the free Stocks Basic plan does not include snapshot data. It does include end-of-day aggregate data. Therefore:

- If `MASSIVE_API_KEY` is missing: use the pure simulator.
- If `MASSIVE_API_KEY` is present and snapshot access works: poll snapshot data and publish realtime or 15-minute delayed prices, depending on the account tier.
- If `MASSIVE_API_KEY` is present but snapshot access is denied or unusable: try end-of-day grouped daily aggregates, use those close prices as simulator seeds, then run the simulator from those seeded prices.

## Official Client

Install:

```bash
uv add massive
```

Use:

```python
from massive import RESTClient

client = RESTClient(api_key=api_key)
```

The official Python client in this repo's lockfile exposes these relevant methods:

```python
client.get_snapshot_all(...)
client.get_snapshot_ticker(...)
client.list_universal_snapshots(...)
client.list_aggs(...)
client.get_grouped_daily_aggs(...)
client.get_daily_open_close_agg(...)
client.get_previous_close_agg(...)
client.get_last_trade(...)
client.get_last_quote(...)
```

The client is synchronous. In FastAPI async background tasks, call it through `asyncio.to_thread(...)` so price polling does not block the event loop.

## Authentication

Use an API key from `MASSIVE_API_KEY`.

```python
import os
from massive import RESTClient

api_key = os.environ["MASSIVE_API_KEY"]
client = RESTClient(api_key=api_key)
```

Raw HTTP calls can use either a bearer token header or the provider's supported key query parameter style. Prefer the bearer token header:

```python
import httpx

headers = {"Authorization": f"Bearer {api_key}"}
response = httpx.get(
    "https://api.massive.com/v2/snapshot/locale/us/markets/stocks/tickers",
    params={"tickers": "AAPL,MSFT,NVDA"},
    headers=headers,
    timeout=10,
)
response.raise_for_status()
```

## Plan Capabilities That Matter

The exact account plan should be treated as a runtime capability, not hardcoded configuration.

| Feature | Stocks Basic Free | Starter / Developer | Advanced |
|---|---:|---:|---:|
| API calls | 5/minute | unlimited | unlimited |
| Snapshot endpoints | not included | 15-minute delayed | realtime |
| Daily aggregates / OHLC | end-of-day | 15-minute delayed | realtime |
| Historical aggregate depth | 2 years | 5-10 years | all history on Advanced |

FinAlly does not need realtime WebSockets. REST polling keeps the backend simpler, keeps all frontend realtime behavior source-agnostic through SSE, and is enough for simulated trading.

## Endpoint Matrix

| Need | Endpoint | Python client | FinAlly use |
|---|---|---|---|
| Multiple current stock prices | `GET /v2/snapshot/locale/us/markets/stocks/tickers?tickers=...` | `get_snapshot_all(SnapshotMarketType.STOCKS, tickers=[...])` | Primary source when snapshot access is available |
| Cross-asset snapshots | `GET /v3/snapshot` | `list_universal_snapshots(...)` | Future use; not needed for stock-only MVP |
| One current stock price | `GET /v2/snapshot/locale/us/markets/stocks/tickers/{ticker}` | `get_snapshot_ticker(...)` | Detail/debug use, not polling |
| One latest trade | `GET /v2/last/trade/{ticker}` | `get_last_trade(ticker)` | Not recommended for watchlist polling because it is one call per ticker |
| One latest NBBO quote | `GET /v2/last/nbbo/{ticker}` | `get_last_quote(ticker)` | Optional future spread display |
| Whole-market daily OHLC | `GET /v2/aggs/grouped/locale/us/market/stocks/{date}` | `get_grouped_daily_aggs(date, market_type="stocks")` | EOD fallback seed source |
| One ticker daily OHLC | `GET /v1/open-close/{ticker}/{date}` | `get_daily_open_close_agg(ticker, date)` | Fallback when grouped data is unavailable |
| One ticker custom OHLC bars | `GET /v2/aggs/ticker/{ticker}/range/{multiplier}/{timespan}/{from}/{to}` | `list_aggs(...)` | Future historical charting |
| Previous close | `GET /v2/aggs/ticker/{ticker}/prev` | `get_previous_close_agg(ticker)` | Alternative seed source and previous close display |

## Multiple Ticker Snapshot

Use this first when `MASSIVE_API_KEY` is set.

```python
import asyncio
from massive import RESTClient
from massive.rest.models import SnapshotMarketType

async def fetch_snapshot_prices(api_key: str, tickers: list[str]) -> dict[str, float]:
    client = RESTClient(api_key=api_key)

    snapshots = await asyncio.to_thread(
        client.get_snapshot_all,
        market_type=SnapshotMarketType.STOCKS,
        tickers=tickers,
    )

    prices: dict[str, float] = {}
    for snap in snapshots:
        # Client model names are snake_case. Raw JSON uses fields like lastTrade.
        if getattr(snap, "last_trade", None) and snap.last_trade.price is not None:
            prices[snap.ticker] = float(snap.last_trade.price)
        elif getattr(snap, "day", None) and snap.day.close is not None:
            prices[snap.ticker] = float(snap.day.close)
        elif getattr(snap, "prev_day", None) and snap.prev_day.close is not None:
            prices[snap.ticker] = float(snap.prev_day.close)

    return prices
```

Important behavior:

- `tickers` is case-sensitive in the REST API. Normalize user input to uppercase before calling.
- Snapshot data is reset daily around the early-morning maintenance window and repopulates as exchanges report data.
- Snapshot responses can omit `lastTrade` or `lastQuote` if the account tier does not include that data or if the ticker has no current-session activity.
- Treat returned prices as current for trading simulation, but display a source mode such as `massive_realtime` or `massive_delayed` internally if capability probing can infer it.

## End-of-Day Fallback

When snapshot access fails with a permissions error, rate-limit pressure, or missing trade fields, FinAlly should try daily aggregates before giving up on Massive entirely. The grouped daily endpoint returns all U.S. stocks for a date in one call and includes `T` ticker, `o/h/l/c`, `v`, `vw`, and millisecond timestamp fields.

```python
from datetime import date, timedelta
from massive import RESTClient

def recent_weekdays(today: date, count: int = 7) -> list[date]:
    days: list[date] = []
    cursor = today
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor -= timedelta(days=1)
    return days

def fetch_eod_seed_prices(
    api_key: str,
    tickers: list[str],
    today: date,
) -> dict[str, float]:
    client = RESTClient(api_key=api_key)
    wanted = {ticker.upper() for ticker in tickers}

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
            if agg.ticker and agg.ticker.upper() in wanted and agg.close is not None
        }
        if prices:
            return prices

    return {}
```

Why grouped daily first:

- One call can seed the whole watchlist.
- It is available on the free Stocks Basic plan as end-of-day data.
- It avoids one request per ticker and stays inside the 5 calls/minute limit.

If grouped daily is unavailable or the requested ticker is missing, fall back to per-ticker previous close only for the missing tickers:

```python
def fetch_previous_close(api_key: str, ticker: str) -> float | None:
    client = RESTClient(api_key=api_key)
    result = client.get_previous_close_agg(ticker=ticker, adjusted=True)
    rows = getattr(result, "results", None) or []
    if rows:
        return float(rows[0].close)
    return None
```

## Capability Probe

Capability probing should happen once at startup and be retried periodically only after failures. Keep it cheap.

```python
from dataclasses import dataclass
from enum import StrEnum

class MassiveCapability(StrEnum):
    SNAPSHOT = "snapshot"
    EOD = "eod"
    NONE = "none"

@dataclass(frozen=True)
class MassiveProbeResult:
    capability: MassiveCapability
    reason: str
    seed_prices: dict[str, float]

async def probe_massive(api_key: str, tickers: list[str]) -> MassiveProbeResult:
    try:
        prices = await fetch_snapshot_prices(api_key, tickers[:3])
        if prices:
            return MassiveProbeResult(MassiveCapability.SNAPSHOT, "snapshot_ok", prices)
    except Exception as exc:
        snapshot_error = f"{type(exc).__name__}: {exc}"
    else:
        snapshot_error = "snapshot returned no usable prices"

    try:
        seeds = await asyncio.to_thread(fetch_eod_seed_prices, api_key, tickers, date.today())
        if seeds:
            return MassiveProbeResult(MassiveCapability.EOD, snapshot_error, seeds)
    except Exception as exc:
        return MassiveProbeResult(
            MassiveCapability.NONE,
            f"{snapshot_error}; eod failed with {type(exc).__name__}: {exc}",
            {},
        )

    return MassiveProbeResult(MassiveCapability.NONE, f"{snapshot_error}; eod returned no prices", {})
```

Recommended mapping:

- `SNAPSHOT`: use `MassiveDataSource`.
- `EOD`: use `SimulatorDataSource` seeded with Massive EOD closes and expose source mode `massive_eod_simulated`.
- `NONE`: use default simulator and log the reason.

## Polling Strategy

Default poll intervals:

| Source mode | Interval |
|---|---:|
| `simulator` | 0.5 seconds |
| `massive_snapshot` on unknown/free-ish key | 15 seconds |
| `massive_snapshot` on paid key | 2-15 seconds |
| `massive_eod_simulated` | Seed once at startup, then simulator at 0.5 seconds |

Use exponential backoff on `429` and transient `5xx` responses. Do not terminate the app because market data polling failed; keep the last good price in `PriceCache` and retry later.

## Error Handling Rules

| Failure | Handling |
|---|---|
| Missing `MASSIVE_API_KEY` | Use simulator |
| `401 Unauthorized` | Log invalid key, use simulator |
| `403 Forbidden` or endpoint permission error | Try EOD fallback, then simulator |
| `429 Too Many Requests` | Back off; if startup probe cannot finish, use simulator and retry in background |
| Snapshot missing `last_trade` | Try `day.close`, then `prev_day.close`; if still empty, EOD fallback |
| Network timeout | Keep existing cache prices, retry next interval |
| Ticker missing from response | Leave previous cached price; if never seen, seed from simulator defaults |

## Data Mapping

Map Massive fields into FinAlly's `PriceUpdate` model:

```python
cache.update(
    ticker=snap.ticker.upper(),
    price=price,
    timestamp=(snap.last_trade.timestamp / 1000.0) if snap.last_trade else time.time(),
)
```

Use Unix seconds internally. Massive REST timestamps are usually milliseconds for aggregate/snapshot bars and nanoseconds for tick-level trades/quotes. Convert at the boundary and avoid leaking provider-specific timestamp units into the rest of the app.

## Sources

- Massive stock full-market snapshot docs: https://massive.com/docs/rest/stocks/snapshots/full-market-snapshot
- Massive unified snapshot docs: https://massive.com/docs/rest/stocks/snapshots/unified-snapshot
- Massive grouped daily market summary docs: https://massive.com/docs/rest/stocks/aggregates/daily-market-summary
- Massive daily ticker summary docs: https://massive.com/docs/rest/stocks/aggregates/daily-ticker-summary
- Massive custom bars docs: https://massive.com/docs/rest/stocks/aggregates/custom-bars
- Massive last trade docs: https://massive.com/docs/rest/stocks/trades-quotes/last-trade
- Massive Python client: https://github.com/massive-com/client-python
- Massive pricing page: https://massive.com/pricing?product=stocks

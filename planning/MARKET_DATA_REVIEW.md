# Market Data Backend Review

**Date:** 2026-05-14  
**Scope:** `planning/` documentation, `backend/app/market/`, and `backend/tests/market/`

## Documents Reviewed

- `planning/PLAN.md`
- `planning/MARKET_DATA_SUMMARY.md`
- `planning/MARKET_DATA_DESIGN.md`
- `planning/MARKET_INTERFACE.md`
- `planning/MARKET_SIMULATOR.md`
- `planning/MASSIVE_API.md`
- `planning/archive/MARKET_DATA_DESIGN.md`
- `planning/archive/MARKET_DATA_REVIEW.md`
- `planning/archive/MARKET_INTERFACE.md`
- `planning/archive/MARKET_SIMULATOR.md`
- `planning/archive/MASSIVE_API.md`

## Executive Summary

The market data backend is complete and well organized. The simulator, cache, source interface, Massive capability probe, EOD-seeded simulator fallback, and SSE route all follow the intended architecture. The prior review items in `planning/archive/MARKET_DATA_REVIEW.md` have been resolved: package build metadata exists, `PriceCache.version` is locked, `GBMSimulator.get_tickers()` exists, the SSE router factory creates distinct routers, and lint passes cleanly.

The first review pass found four focused edge-path issues. These have now been implemented and covered by regression tests. I would now mark the market data backend ready for the next backend integration layer.

## Implementation Update

The recommendations below have been implemented:

- Massive snapshot startup no longer reports healthy after an empty or failed first full poll.
- Massive snapshot mode seeds never-seen tickers with simulator-style fallback prices while keeping provider health unhealthy unless Massive actually returns usable snapshot prices.
- Massive EOD probing preserves partial grouped-daily seeds even if a per-ticker previous-close fallback fails.
- Massive EOD probing can still use previous-close seeds if grouped daily fails.
- Dynamically added Massive-mode tickers are immediately fallback-seeded until the next provider poll overwrites them.
- SSE now emits `data: {}` when cache removals make the cache empty.
- Regression tests were added for all of these behaviors.

## Findings From First Pass (Now Fixed)

### 1. Massive snapshot startup can report healthy after an empty or failed first poll

**Severity:** High  
**Status:** Fixed  
**Files:** `backend/app/market/massive_client.py:247`, `backend/app/market/massive_client.py:249`, `backend/app/market/massive_client.py:334`, `backend/app/market/massive_client.py:343`

`MassiveDataSource.start()` calls `_poll_once()`, then unconditionally overwrites `_status` with `healthy=True`, `message="snapshot polling"`, and a fresh `last_success_at`.

That erases the status set by `_poll_once()` when the first poll updates zero tickers or raises. `_poll_once()` already records `healthy=False` for `processed == 0` and for exceptions, but `start()` immediately hides that result.

Impact:

- `get_status()` can claim Massive snapshot mode is healthy when no price reached `PriceCache`.
- A future `/api/health` or provider badge would be misleading.
- Startup can look successful even when the default watchlist has no real Massive prices.

The current tests miss this because `backend/tests/market/test_massive.py:293` starts the source with `_fetch_snapshots` returning `[]` and only asserts that a background task exists. It does not assert status or cache contents.

Recommendation:

- Make `_poll_once()` return a processed count or a success boolean.
- In `start()`, do not overwrite an unhealthy status after the immediate poll.
- For a snapshot-capable probe followed by an empty first full poll, either keep status unhealthy and retry, or seed never-seen tickers with simulator defaults while continuing Massive polling.
- Add tests for:
  - snapshot probe succeeds, first poll returns `[]`, status remains unhealthy;
  - snapshot probe succeeds, first poll raises, status remains unhealthy;
  - snapshot probe succeeds, first poll updates one or more tickers, status is healthy.

### 2. SSE does not emit an empty payload when the cache becomes empty

**Severity:** Medium  
**Status:** Fixed  
**File:** `backend/app/market/stream.py:75`

`_generate_events()` observes the cache version change, sets `last_version = current_version`, then only yields if `prices` is truthy. If the last ticker is removed, `PriceCache.remove()` increments the version and `get_all()` returns `{}`, but the generator emits nothing.

Impact:

- A frontend that relies on SSE snapshots will not be told that the final ticker disappeared.
- The UI can retain a stale last row after the backend cache is empty.
- This conflicts with `planning/MARKET_INTERFACE.md`, which says ticker removal should increment SSE version so clients can remove the row, and with the archive design note that empty watchlists should stream empty events.

Recommendation:

- Always emit `data: {}\n\n` when the cache version changes and `get_all()` is empty.
- Add a test that updates one ticker, emits it, removes it, then asserts the next SSE data event is `{}`.

### 3. Partial EOD seeds are discarded if any previous-close lookup fails

**Severity:** Medium  
**Status:** Fixed  
**File:** `backend/app/market/massive_client.py:173`

`probe_massive_client()` fetches grouped daily EOD seeds, then loops through missing tickers and calls `fetch_previous_close_seed()`. The whole EOD block is wrapped in one `try`. If grouped daily returns valid seeds for some tickers but one previous-close lookup raises for a missing ticker, the function returns `MassiveCapability.NONE` and discards the valid grouped seeds.

Impact:

- A key with useful grouped daily access can be treated as having no usable Massive access because one secondary per-ticker fallback failed.
- The EOD-seeded simulator may lose realistic starting prices and fall back to static/default simulator seeds.
- This is especially likely with invalid/delisted symbols, partial provider outages, or endpoint-specific permission differences.

Recommendation:

- Keep grouped daily seeds once obtained.
- Catch previous-close failures per ticker, log/debug the failure, and continue.
- Return `MassiveCapability.EOD` whenever at least one seed exists.
- Add a test where grouped daily returns `{"AAPL": 188.50}` and previous close raises for `MSFT`; expected result should still be EOD with the AAPL seed.

### 4. Massive snapshot mode can leave never-seen tickers unseeded

**Severity:** Medium  
**Status:** Fixed  
**Files:** `backend/app/market/massive_client.py:318`, `backend/app/market/massive_client.py:320`, `backend/app/market/massive_client.py:281`

When Massive snapshot mode is active, `_poll_once()` skips snapshots that have no usable price and does not synthesize any fallback price for tickers that have never been cached. `add_ticker()` similarly only appends the ticker and waits for a future poll.

The design documents allow a brief cache miss in Massive mode, but `planning/MARKET_DATA_DESIGN.md` also says: "Ticker missing from response: Leave previous cached price; if never seen, seed from simulator defaults." The implementation currently only does the first half.

Impact:

- Default watchlist rows can remain price-less if the snapshot response is partial.
- Trades for these tickers will fail with "no price available" until Massive returns a usable price.
- This is most visible outside regular market hours, during provider partial responses, or for recently added tickers.

Recommendation:

- Track which requested tickers were processed during each poll.
- For tickers with no cached price after an initial snapshot poll, seed from `SEED_PRICES` or the same unknown-ticker seed policy used by `GBMSimulator`.
- For dynamically added tickers, consider immediate default seeding in Massive mode, then overwrite on the next successful Massive poll.
- Add tests for partial snapshot responses and for a newly added ticker with no provider response.

## Test Results

The first direct `uv` run failed because `uv` was not on this shell's `PATH`. Running via the installed binary succeeded. The sandbox also blocked access to the user's uv package cache, so the successful test/lint/coverage commands were run with approved escalation.

Commands run from `backend/`:

```bash
/Users/sudheer.kumar/.local/bin/uv run --extra dev pytest -q
```

Result:

```text
106 passed in 2.09s
```

```bash
/Users/sudheer.kumar/.local/bin/uv run --extra dev ruff check app tests
```

Result:

```text
All checks passed!
```

```bash
/Users/sudheer.kumar/.local/bin/uv run --extra dev pytest --cov=app --cov-report=term-missing -q
```

Result:

```text
106 passed in 2.86s
TOTAL 583 statements, 32 missed, 95% coverage
```

Coverage by module:

| Module | Coverage |
|---|---:|
| `app/market/cache.py` | 100% |
| `app/market/factory.py` | 100% |
| `app/market/interface.py` | 100% |
| `app/market/massive_client.py` | 89% |
| `app/market/models.py` | 100% |
| `app/market/seed_prices.py` | 100% |
| `app/market/simulator.py` | 98% |
| `app/market/stream.py` | 97% |
| `app/market/utils.py` | 100% |

Only the backend Python test configuration is present in the current workspace. I found no frontend package, Playwright config, or additional root-level test runner to execute.

## Architecture Assessment

Strengths:

- `PriceCache` is the correct single in-memory source of truth for downstream reads.
- `MarketDataSource` keeps simulator and Massive implementations behind the same lifecycle contract.
- `MarketSourceMode` and `MarketStatus` make provider behavior inspectable and future health endpoints easier.
- The simulator matches the planning docs: GBM, correlated moves, realistic defaults, EOD seed overrides, immediate cache seeding, and random shock events.
- The Massive implementation uses one multi-ticker snapshot request per poll and avoids blocking the event loop by using `asyncio.to_thread`.
- Tests now cover the major market modules with high coverage and no lint debt.

Residual risks:

- Massive behavior is fully mocked; there is no live contract test against the actual client/API shape.
- The most important uncovered lines in `massive_client.py` are exactly the branches around alternative timestamp fields, empty probes, EOD failure handling, and poll failure handling.
- There is no app-level lifespan integration yet in this repo slice, so the review only covers the market subsystem itself.

## Fixes Implemented

1. `MassiveDataSource.start()` now preserves the unhealthy status set by `_poll_once()` when the immediate poll updates zero real Massive tickers or raises.
2. `_poll_once()` now returns the number of real provider updates processed, tracks missing tickers, and fallback-seeds never-seen tickers without treating fallback seeding as provider health.
3. `add_ticker()` in Massive snapshot mode now seeds a ticker immediately so watchlist additions have a usable cache price before the next Massive poll.
4. `probe_massive_client()` now preserves grouped daily seeds even when one previous-close fallback fails, and can use previous-close seeds when grouped daily itself fails.
5. `_generate_events()` now emits an empty JSON object when the cache version changes to an empty cache.
6. Tests were expanded from 101 to 106 cases and cover the new regression scenarios.

## Verdict

The market data backend is ready for the next phase. `PriceCache`, `MarketDataSource`, simulator mode, Massive snapshot mode, EOD-seeded fallback mode, and SSE streaming now match the planning contract and have focused regression coverage around the previously identified edge cases.

"""Massive (formerly Polygon.io) API client for real market data."""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum
from typing import Any

from massive import RESTClient
from massive.rest.models import SnapshotMarketType

from .cache import PriceCache
from .interface import MarketDataSource
from .models import MarketSourceMode, MarketStatus
from .seed_prices import SEED_PRICES
from .simulator import SimulatorDataSource
from .utils import normalize_ticker, normalize_tickers

logger = logging.getLogger(__name__)


class MassiveCapability(StrEnum):
    """Massive API capability available to the configured key."""

    SNAPSHOT = "snapshot"
    EOD = "eod"
    NONE = "none"


@dataclass(frozen=True, slots=True)
class MassiveProbeResult:
    """Result of probing a Massive API key at startup."""

    capability: MassiveCapability
    reason: str
    seed_prices: dict[str, float]


def extract_snapshot_price(snapshot: Any) -> float | None:
    """Extract the best available price from a Massive snapshot model."""
    last_trade = getattr(snapshot, "last_trade", None)
    if last_trade is not None and getattr(last_trade, "price", None) is not None:
        return float(last_trade.price)

    day = getattr(snapshot, "day", None)
    if day is not None and getattr(day, "close", None) is not None:
        return float(day.close)

    prev_day = getattr(snapshot, "prev_day", None)
    if prev_day is None:
        prev_day = getattr(snapshot, "prev_daily_bar", None)
    if prev_day is not None and getattr(prev_day, "close", None) is not None:
        return float(prev_day.close)

    return None


def extract_snapshot_timestamp(snapshot: Any) -> float:
    """Extract and normalize a Massive snapshot timestamp to Unix seconds."""
    last_trade = getattr(snapshot, "last_trade", None)
    if last_trade is not None and getattr(last_trade, "timestamp", None) is not None:
        return _timestamp_to_seconds(float(last_trade.timestamp))

    day = getattr(snapshot, "day", None)
    if day is not None and getattr(day, "timestamp", None) is not None:
        return _timestamp_to_seconds(float(day.timestamp))

    prev_day = getattr(snapshot, "prev_day", None)
    if prev_day is None:
        prev_day = getattr(snapshot, "prev_daily_bar", None)
    if prev_day is not None and getattr(prev_day, "timestamp", None) is not None:
        return _timestamp_to_seconds(float(prev_day.timestamp))

    return time.time()


def _timestamp_to_seconds(timestamp: float) -> float:
    """Convert common Massive timestamp units to Unix seconds."""
    # Snapshot aggregate fields are milliseconds. Tick-level endpoints can
    # return nanoseconds, so handle both without leaking units downstream.
    if timestamp > 1_000_000_000_000_000:
        return timestamp / 1_000_000_000.0
    if timestamp > 1_000_000_000_000:
        return timestamp / 1000.0
    return timestamp


def recent_weekdays(today: date, count: int = 7) -> list[date]:
    """Return recent weekdays, newest first, without holiday assumptions."""
    days: list[date] = []
    cursor = today
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor -= timedelta(days=1)
    return days


def fetch_grouped_daily_seed_prices(
    client: RESTClient,
    tickers: list[str],
    today: date,
) -> dict[str, float]:
    """Fetch EOD close prices for the requested tickers using grouped daily bars."""
    wanted = set(normalize_tickers(tickers))
    for day in recent_weekdays(today):
        aggs = client.get_grouped_daily_aggs(
            date=day,
            adjusted=True,
            market_type="stocks",
            include_otc=False,
        )
        prices = {
            normalize_ticker(agg.ticker): float(agg.close)
            for agg in aggs
            if getattr(agg, "ticker", None)
            and normalize_ticker(agg.ticker) in wanted
            and getattr(agg, "close", None) is not None
        }
        if prices:
            return prices
    return {}


def fetch_previous_close_seed(client: RESTClient, ticker: str) -> float | None:
    """Fetch a previous close for one ticker as a secondary EOD seed source."""
    result = client.get_previous_close_agg(ticker=normalize_ticker(ticker), adjusted=True)
    rows = getattr(result, "results", None)
    if rows is None and isinstance(result, list):
        rows = result
    rows = rows or []
    if rows and getattr(rows[0], "close", None) is not None:
        return float(rows[0].close)
    return None


def fallback_seed_price(ticker: str) -> float:
    """Return a simulator-style fallback seed for a ticker missing Massive data."""
    ticker = normalize_ticker(ticker)
    if ticker in SEED_PRICES:
        return SEED_PRICES[ticker]
    return random.uniform(50.0, 300.0)


async def probe_massive(api_key: str, tickers: list[str]) -> MassiveProbeResult:
    """Probe whether the configured Massive key can provide snapshots or EOD seeds."""
    client = RESTClient(api_key=api_key)
    return await probe_massive_client(client, tickers)


async def probe_massive_client(client: RESTClient, tickers: list[str]) -> MassiveProbeResult:
    """Probe an existing Massive client.

    This helper keeps startup testable without making real API calls.
    """
    tickers = normalize_tickers(tickers)
    if not tickers:
        return MassiveProbeResult(MassiveCapability.SNAPSHOT, "empty_ticker_set", {})

    try:
        snapshots = await asyncio.to_thread(
            client.get_snapshot_all,
            market_type=SnapshotMarketType.STOCKS,
            tickers=tickers[:3],
        )
        snapshot_prices = {
            normalize_ticker(snap.ticker): price
            for snap in snapshots
            if getattr(snap, "ticker", None)
            and (price := extract_snapshot_price(snap)) is not None
        }
        if snapshot_prices:
            return MassiveProbeResult(MassiveCapability.SNAPSHOT, "snapshot_ok", snapshot_prices)
        snapshot_reason = "snapshot returned no usable prices"
    except Exception as exc:
        snapshot_reason = f"snapshot failed: {type(exc).__name__}: {exc}"

    seeds: dict[str, float] = {}
    eod_errors: list[str] = []

    try:
        seeds = await asyncio.to_thread(fetch_grouped_daily_seed_prices, client, tickers, date.today())
    except Exception as exc:
        eod_errors.append(f"grouped daily failed: {type(exc).__name__}: {exc}")

    missing = [ticker for ticker in tickers if ticker not in seeds]
    for ticker in missing:
        try:
            seed = await asyncio.to_thread(fetch_previous_close_seed, client, ticker)
        except Exception as exc:
            eod_errors.append(f"{ticker} previous close failed: {type(exc).__name__}: {exc}")
            continue
        if seed is not None:
            seeds[ticker] = seed

    if seeds:
        reason = snapshot_reason
        if eod_errors:
            reason = f"{reason}; partial eod errors: {'; '.join(eod_errors)}"
        return MassiveProbeResult(MassiveCapability.EOD, reason, seeds)

    eod_reason = "eod returned no usable prices"
    if eod_errors:
        eod_reason = f"eod failed: {'; '.join(eod_errors)}"
    return MassiveProbeResult(
        MassiveCapability.NONE,
        f"{snapshot_reason}; {eod_reason}",
        {},
    )


class MassiveDataSource(MarketDataSource):
    """MarketDataSource backed by Massive snapshots with simulator fallback."""

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
        self._task: asyncio.Task | None = None
        self._client: RESTClient | None = None
        self._delegate: SimulatorDataSource | None = None
        self._status = MarketStatus(
            mode=MarketSourceMode.MASSIVE_SNAPSHOT,
            provider="massive",
            healthy=False,
            message="not_started",
        )

    async def start(self, tickers: list[str]) -> None:
        self._client = RESTClient(api_key=self._api_key)
        self._tickers = normalize_tickers(tickers)

        probe = await probe_massive_client(self._client, self._tickers)
        if probe.capability == MassiveCapability.EOD:
            self._delegate = SimulatorDataSource(
                price_cache=self._cache,
                seed_prices=probe.seed_prices,
                source_mode=MarketSourceMode.MASSIVE_EOD_SIMULATED,
                status_message=f"Massive snapshot unavailable; seeded from EOD: {probe.reason}",
            )
            await self._delegate.start(self._tickers)
            self._status = self._delegate.get_status()
            logger.info("Massive EOD fallback active: %s", probe.reason)
            return

        if probe.capability == MassiveCapability.NONE:
            self._delegate = SimulatorDataSource(
                price_cache=self._cache,
                source_mode=MarketSourceMode.SIMULATOR,
                status_message=f"Massive unavailable; using simulator: {probe.reason}",
            )
            await self._delegate.start(self._tickers)
            self._status = self._delegate.get_status()
            logger.warning("Massive unavailable; simulator fallback active: %s", probe.reason)
            return

        processed = await self._poll_once()
        self._task = asyncio.create_task(self._poll_loop(), name="massive-poller")
        if not self._tickers:
            self._status = MarketStatus(
                mode=MarketSourceMode.MASSIVE_SNAPSHOT,
                provider="massive",
                healthy=True,
                message="snapshot polling; empty ticker set",
                last_success_at=time.time(),
            )
        elif processed > 0:
            self._status = MarketStatus(
                mode=MarketSourceMode.MASSIVE_SNAPSHOT,
                provider="massive",
                healthy=True,
                message=f"snapshot polling; {self._status.message}",
                last_success_at=self._status.last_success_at,
            )
        logger.info(
            "Massive snapshot poller started: %d tickers, %.1fs interval",
            len(self._tickers),
            self._interval,
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
        logger.info("Massive source stopped")

    async def add_ticker(self, ticker: str) -> None:
        ticker = normalize_ticker(ticker)
        if self._delegate:
            await self._delegate.add_ticker(ticker)
            return
        if ticker not in self._tickers:
            self._tickers.append(ticker)
            if self._cache.get(ticker) is None:
                self._cache.update(ticker=ticker, price=fallback_seed_price(ticker))
            logger.info("Massive: added ticker %s (seeded until next poll)", ticker)

    async def remove_ticker(self, ticker: str) -> None:
        ticker = normalize_ticker(ticker)
        if self._delegate:
            await self._delegate.remove_ticker(ticker)
            return
        self._tickers = [t for t in self._tickers if t != ticker]
        self._cache.remove(ticker)
        logger.info("Massive: removed ticker %s", ticker)

    def get_tickers(self) -> list[str]:
        if self._delegate:
            return self._delegate.get_tickers()
        return list(self._tickers)

    def get_status(self) -> MarketStatus:
        if self._delegate:
            return self._delegate.get_status()
        return self._status

    async def _poll_loop(self) -> None:
        """Poll on interval. First poll already happened in start()."""
        while True:
            await asyncio.sleep(self._interval)
            await self._poll_once()

    async def _poll_once(self) -> int:
        """Execute one poll cycle: fetch snapshots, update cache."""
        target_tickers = list(self._tickers)
        if not target_tickers or not self._client:
            return 0

        try:
            snapshots = await asyncio.to_thread(self._fetch_snapshots, target_tickers)
            processed = 0
            processed_tickers: set[str] = set()
            for snap in snapshots:
                price = extract_snapshot_price(snap)
                if price is None:
                    logger.warning(
                        "Skipping snapshot for %s: no usable price",
                        getattr(snap, "ticker", "???"),
                    )
                    continue

                ticker = normalize_ticker(snap.ticker)
                self._cache.update(
                    ticker=ticker,
                    price=price,
                    timestamp=extract_snapshot_timestamp(snap),
                )
                processed += 1
                processed_tickers.add(ticker)

            seeded = self._seed_missing_tickers(target_tickers, processed_tickers)
            message = f"updated {processed}/{len(target_tickers)} tickers"
            if seeded:
                message = f"{message}; seeded {seeded} fallback prices"

            self._status = MarketStatus(
                mode=MarketSourceMode.MASSIVE_SNAPSHOT,
                provider="massive",
                healthy=processed > 0,
                message=message,
                last_success_at=time.time() if processed > 0 else self._status.last_success_at,
            )
            logger.debug("Massive poll: updated %d/%d tickers", processed, len(target_tickers))
            return processed

        except Exception as exc:
            seeded = self._seed_missing_tickers(target_tickers)
            logger.error("Massive poll failed: %s", exc)
            message = f"poll failed: {type(exc).__name__}: {exc}"
            if seeded:
                message = f"{message}; seeded {seeded} fallback prices"
            self._status = MarketStatus(
                mode=MarketSourceMode.MASSIVE_SNAPSHOT,
                provider="massive",
                healthy=False,
                message=message,
                last_success_at=self._status.last_success_at,
            )
            return 0

    def _seed_missing_tickers(
        self,
        tickers: list[str],
        processed_tickers: set[str] | None = None,
    ) -> int:
        """Seed never-seen tickers so the UI has prices while Massive catches up."""
        processed_tickers = processed_tickers or set()
        seeded = 0
        for ticker in tickers:
            ticker = normalize_ticker(ticker)
            if ticker in processed_tickers or self._cache.get(ticker) is not None:
                continue
            self._cache.update(ticker=ticker, price=fallback_seed_price(ticker))
            seeded += 1
        return seeded

    def _fetch_snapshots(self, tickers: list[str]) -> list:
        """Synchronous call to the Massive REST API. Runs in a thread."""
        return self._client.get_snapshot_all(
            market_type=SnapshotMarketType.STOCKS,
            tickers=tickers,
        )

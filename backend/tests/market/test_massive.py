"""Tests for Massive market data integration."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.market.cache import PriceCache
from app.market.massive_client import (
    MassiveCapability,
    MassiveDataSource,
    MassiveProbeResult,
    extract_snapshot_price,
    extract_snapshot_timestamp,
    fetch_grouped_daily_seed_prices,
    fetch_previous_close_seed,
    probe_massive_client,
    recent_weekdays,
)
from app.market.models import MarketSourceMode


def _make_snapshot(
    ticker: str,
    price: float | None = None,
    timestamp_ms: int = 1707580800000,
    day_close: float | None = None,
    prev_close: float | None = None,
) -> SimpleNamespace:
    """Create a minimal Massive-like snapshot object."""
    return SimpleNamespace(
        ticker=ticker,
        last_trade=(
            SimpleNamespace(price=price, timestamp=timestamp_ms) if price is not None else None
        ),
        day=(
            SimpleNamespace(close=day_close, timestamp=timestamp_ms) if day_close is not None else None
        ),
        prev_day=(
            SimpleNamespace(close=prev_close, timestamp=timestamp_ms) if prev_close is not None else None
        ),
    )


class FakeSnapshotClient:
    """Fake client with usable snapshot access."""

    def get_snapshot_all(self, *args, **kwargs):
        return [_make_snapshot("AAPL", 190.50)]


class FakeEodClient:
    """Fake client where snapshots fail but grouped daily bars work."""

    def get_snapshot_all(self, *args, **kwargs):
        raise Exception("403 forbidden")

    def get_grouped_daily_aggs(self, *args, **kwargs):
        return [
            SimpleNamespace(ticker="AAPL", close=188.50),
            SimpleNamespace(ticker="MSFT", close=412.25),
            SimpleNamespace(ticker="NOPE", close=1.00),
        ]

    def get_previous_close_agg(self, *args, **kwargs):
        return SimpleNamespace(results=[])


class FakeNoAccessClient:
    """Fake client with no usable Massive access."""

    def get_snapshot_all(self, *args, **kwargs):
        raise Exception("403 forbidden")

    def get_grouped_daily_aggs(self, *args, **kwargs):
        raise Exception("403 forbidden")

    def get_previous_close_agg(self, *args, **kwargs):
        raise Exception("403 forbidden")


class FakePartialEodClient:
    """Fake client where grouped daily works but a previous-close fallback fails."""

    def get_snapshot_all(self, *args, **kwargs):
        raise Exception("403 forbidden")

    def get_grouped_daily_aggs(self, *args, **kwargs):
        return [SimpleNamespace(ticker="AAPL", close=188.50)]

    def get_previous_close_agg(self, *args, **kwargs):
        raise Exception("previous close unavailable")


class FakeGroupedFailsPreviousWorksClient:
    """Fake client where grouped daily fails but previous close still works."""

    def get_snapshot_all(self, *args, **kwargs):
        raise Exception("403 forbidden")

    def get_grouped_daily_aggs(self, *args, **kwargs):
        raise Exception("grouped daily unavailable")

    def get_previous_close_agg(self, *args, **kwargs):
        return SimpleNamespace(results=[SimpleNamespace(close=188.50)])


class TestMassiveHelpers:
    """Unit tests for Massive parsing and probe helpers."""

    def test_extract_snapshot_price_prefers_last_trade(self):
        snap = _make_snapshot("AAPL", price=190.50, day_close=189.00, prev_close=188.00)
        assert extract_snapshot_price(snap) == 190.50

    def test_extract_snapshot_price_falls_back_to_day_close(self):
        snap = _make_snapshot("AAPL", price=None, day_close=189.00, prev_close=188.00)
        assert extract_snapshot_price(snap) == 189.00

    def test_extract_snapshot_price_falls_back_to_prev_day(self):
        snap = _make_snapshot("AAPL", price=None, day_close=None, prev_close=188.00)
        assert extract_snapshot_price(snap) == 188.00

    def test_extract_snapshot_price_returns_none_when_missing(self):
        snap = _make_snapshot("AAPL")
        assert extract_snapshot_price(snap) is None

    def test_extract_snapshot_timestamp_converts_milliseconds(self):
        snap = _make_snapshot("AAPL", price=190.50, timestamp_ms=1707580800000)
        assert extract_snapshot_timestamp(snap) == 1707580800.0

    def test_recent_weekdays_skips_weekend(self):
        # Monday, 2026-05-11
        days = recent_weekdays(date(2026, 5, 11), count=3)
        assert days == [date(2026, 5, 11), date(2026, 5, 8), date(2026, 5, 7)]

    def test_grouped_daily_seed_prices_filters_requested_tickers(self):
        prices = fetch_grouped_daily_seed_prices(
            FakeEodClient(),
            ["AAPL", "MSFT"],
            date(2026, 5, 11),
        )
        assert prices == {"AAPL": 188.50, "MSFT": 412.25}

    def test_previous_close_seed(self):
        client = MagicMock()
        client.get_previous_close_agg.return_value = SimpleNamespace(
            results=[SimpleNamespace(close=123.45)]
        )
        assert fetch_previous_close_seed(client, "aapl") == 123.45
        client.get_previous_close_agg.assert_called_once()

    @pytest.mark.asyncio
    async def test_probe_snapshot_success(self):
        result = await probe_massive_client(FakeSnapshotClient(), ["AAPL"])
        assert result.capability == MassiveCapability.SNAPSHOT
        assert result.seed_prices == {"AAPL": 190.50}

    @pytest.mark.asyncio
    async def test_probe_eod_fallback(self):
        result = await probe_massive_client(FakeEodClient(), ["AAPL", "MSFT"])
        assert result.capability == MassiveCapability.EOD
        assert result.seed_prices == {"AAPL": 188.50, "MSFT": 412.25}

    @pytest.mark.asyncio
    async def test_probe_no_access(self):
        result = await probe_massive_client(FakeNoAccessClient(), ["AAPL"])
        assert result.capability == MassiveCapability.NONE
        assert result.seed_prices == {}

    @pytest.mark.asyncio
    async def test_probe_preserves_partial_grouped_daily_seed_when_previous_close_fails(self):
        result = await probe_massive_client(FakePartialEodClient(), ["AAPL", "MSFT"])
        assert result.capability == MassiveCapability.EOD
        assert result.seed_prices == {"AAPL": 188.50}
        assert "partial eod errors" in result.reason

    @pytest.mark.asyncio
    async def test_probe_uses_previous_close_when_grouped_daily_fails(self):
        result = await probe_massive_client(FakeGroupedFailsPreviousWorksClient(), ["AAPL"])
        assert result.capability == MassiveCapability.EOD
        assert result.seed_prices == {"AAPL": 188.50}


@pytest.mark.asyncio
class TestMassiveDataSource:
    """Unit tests for MassiveDataSource with mocked API."""

    async def test_poll_updates_cache(self):
        """Test that polling updates the cache."""
        cache = PriceCache()
        source = MassiveDataSource(
            api_key="test-key",
            price_cache=cache,
            poll_interval=60.0,
        )
        source._tickers = ["AAPL", "GOOGL"]
        source._client = MagicMock()

        mock_snapshots = [
            _make_snapshot("AAPL", 190.50),
            _make_snapshot("GOOGL", 175.25),
        ]

        with patch.object(source, "_fetch_snapshots", return_value=mock_snapshots):
            await source._poll_once()

        assert cache.get_price("AAPL") == 190.50
        assert cache.get_price("GOOGL") == 175.25
        assert source.get_status().healthy is True

    async def test_malformed_snapshot_skipped(self):
        """Test that snapshots with no usable price are skipped gracefully."""
        cache = PriceCache()
        source = MassiveDataSource(
            api_key="test-key",
            price_cache=cache,
            poll_interval=60.0,
        )
        source._tickers = ["AAPL", "BAD"]
        source._client = MagicMock()

        good_snap = _make_snapshot("AAPL", 190.50)
        bad_snap = _make_snapshot("BAD")

        with patch.object(source, "_fetch_snapshots", return_value=[good_snap, bad_snap]):
            await source._poll_once()

        assert cache.get_price("AAPL") == 190.50
        assert cache.get_price("BAD") is not None
        assert 50.0 <= cache.get_price("BAD") <= 300.0

    async def test_api_error_does_not_crash(self):
        """Test that API errors don't crash the poller."""
        cache = PriceCache()
        source = MassiveDataSource(
            api_key="test-key",
            price_cache=cache,
            poll_interval=60.0,
        )
        source._tickers = ["AAPL"]
        source._client = MagicMock()

        with patch.object(source, "_fetch_snapshots", side_effect=Exception("network error")):
            await source._poll_once()

        assert cache.get_price("AAPL") == 190.00
        assert source.get_status().healthy is False
        assert "network error" in source.get_status().message
        assert "seeded 1 fallback prices" in source.get_status().message

    async def test_timestamp_conversion(self):
        """Test that timestamps are converted from milliseconds to seconds."""
        cache = PriceCache()
        source = MassiveDataSource(
            api_key="test-key",
            price_cache=cache,
            poll_interval=60.0,
        )
        source._tickers = ["AAPL"]
        source._client = MagicMock()

        with patch.object(source, "_fetch_snapshots", return_value=[_make_snapshot("AAPL", 190.50)]):
            await source._poll_once()

        update = cache.get("AAPL")
        assert update is not None
        assert update.timestamp == 1707580800.0

    async def test_add_ticker(self):
        """Test adding a ticker."""
        cache = PriceCache()
        source = MassiveDataSource(api_key="test-key", price_cache=cache)

        await source.add_ticker("AAPL")
        assert "AAPL" in source.get_tickers()
        assert cache.get_price("AAPL") == 190.00

    async def test_add_ticker_uppercase_normalization(self):
        """Test that tickers are normalized to uppercase."""
        cache = PriceCache()
        source = MassiveDataSource(api_key="test-key", price_cache=cache)

        await source.add_ticker("aapl")
        assert "AAPL" in source.get_tickers()
        assert cache.get_price("aapl") == 190.00

    async def test_add_ticker_strips_whitespace(self):
        """Test that ticker whitespace is stripped."""
        cache = PriceCache()
        source = MassiveDataSource(api_key="test-key", price_cache=cache)

        await source.add_ticker("  AAPL  ")
        assert "AAPL" in source.get_tickers()

    async def test_remove_ticker(self):
        """Test removing a ticker."""
        cache = PriceCache()
        source = MassiveDataSource(api_key="test-key", price_cache=cache)
        source._tickers = ["AAPL", "GOOGL"]
        cache.update("AAPL", 190.00)

        await source.remove_ticker("AAPL")
        assert "AAPL" not in source.get_tickers()
        assert cache.get("AAPL") is None

    async def test_get_tickers(self):
        """Test getting the list of active tickers."""
        cache = PriceCache()
        source = MassiveDataSource(api_key="test-key", price_cache=cache)
        source._tickers = ["AAPL", "GOOGL"]

        tickers = source.get_tickers()
        assert tickers == ["AAPL", "GOOGL"]

    async def test_empty_tickers_skips_poll(self):
        """Test that polling is skipped when there are no tickers."""
        cache = PriceCache()
        source = MassiveDataSource(api_key="test-key", price_cache=cache)
        source._tickers = []

        with patch.object(source, "_fetch_snapshots") as mock_fetch:
            await source._poll_once()
            mock_fetch.assert_not_called()

    async def test_stop_is_idempotent(self):
        """Test that stop() can be called multiple times."""
        cache = PriceCache()
        source = MassiveDataSource(api_key="test-key", price_cache=cache)

        await source.stop()
        await source.stop()

    async def test_stop_cancels_task(self):
        """Test that stop() cancels the polling task."""
        cache = PriceCache()
        source = MassiveDataSource(api_key="test-key", price_cache=cache, poll_interval=10.0)

        with patch("app.market.massive_client.RESTClient"):
            with patch(
                "app.market.massive_client.probe_massive_client",
                return_value=MassiveProbeResult(MassiveCapability.SNAPSHOT, "snapshot_ok", {}),
            ):
                with patch.object(source, "_fetch_snapshots", return_value=[]):
                    await source.start(["AAPL"])

        assert source._task is not None
        assert not source._task.done()

        await source.stop()
        assert source._task is None

    async def test_start_empty_snapshot_poll_keeps_unhealthy_status_and_seeds(self):
        """A snapshot-capable key should not report healthy if the full poll has no prices."""
        cache = PriceCache()
        source = MassiveDataSource(api_key="test-key", price_cache=cache, poll_interval=60.0)

        with patch("app.market.massive_client.RESTClient"):
            with patch(
                "app.market.massive_client.probe_massive_client",
                return_value=MassiveProbeResult(MassiveCapability.SNAPSHOT, "snapshot_ok", {}),
            ):
                with patch.object(source, "_fetch_snapshots", return_value=[]):
                    await source.start(["AAPL"])

        assert cache.get_price("AAPL") == 190.00
        assert source.get_status().healthy is False
        assert "seeded 1 fallback prices" in source.get_status().message

        await source.stop()

    async def test_start_failed_snapshot_poll_keeps_unhealthy_status_and_seeds(self):
        """A failed first poll should keep unhealthy status while preserving startup prices."""
        cache = PriceCache()
        source = MassiveDataSource(api_key="test-key", price_cache=cache, poll_interval=60.0)

        with patch("app.market.massive_client.RESTClient"):
            with patch(
                "app.market.massive_client.probe_massive_client",
                return_value=MassiveProbeResult(MassiveCapability.SNAPSHOT, "snapshot_ok", {}),
            ):
                with patch.object(source, "_fetch_snapshots", side_effect=Exception("network error")):
                    await source.start(["AAPL"])

        assert cache.get_price("AAPL") == 190.00
        assert source.get_status().healthy is False
        assert "network error" in source.get_status().message
        assert "seeded 1 fallback prices" in source.get_status().message

        await source.stop()

    async def test_start_immediate_poll(self):
        """Test that start() does an immediate poll before starting the loop."""
        cache = PriceCache()
        source = MassiveDataSource(api_key="test-key", price_cache=cache, poll_interval=60.0)

        with patch("app.market.massive_client.RESTClient"):
            with patch(
                "app.market.massive_client.probe_massive_client",
                return_value=MassiveProbeResult(MassiveCapability.SNAPSHOT, "snapshot_ok", {}),
            ):
                with patch.object(source, "_fetch_snapshots", return_value=[_make_snapshot("AAPL", 190.50)]):
                    await source.start(["AAPL"])

        assert cache.get_price("AAPL") == 190.50
        assert source.get_status().mode == MarketSourceMode.MASSIVE_SNAPSHOT

        await source.stop()

    async def test_start_eod_fallback_delegates_to_simulator(self):
        """Test EOD-only keys seed and run the simulator."""
        cache = PriceCache()
        source = MassiveDataSource(api_key="test-key", price_cache=cache, poll_interval=60.0)

        with patch("app.market.massive_client.RESTClient"):
            with patch(
                "app.market.massive_client.probe_massive_client",
                return_value=MassiveProbeResult(
                    MassiveCapability.EOD,
                    "snapshot forbidden",
                    {"AAPL": 188.50},
                ),
            ):
                await source.start(["AAPL"])

        assert source._delegate is not None
        assert cache.get_price("AAPL") == 188.50
        assert source.get_status().mode == MarketSourceMode.MASSIVE_EOD_SIMULATED

        await source.stop()

    async def test_start_no_access_falls_back_to_default_simulator(self):
        """Test totally unusable keys still produce simulated prices."""
        cache = PriceCache()
        source = MassiveDataSource(api_key="test-key", price_cache=cache, poll_interval=60.0)

        with patch("app.market.massive_client.RESTClient"):
            with patch(
                "app.market.massive_client.probe_massive_client",
                return_value=MassiveProbeResult(MassiveCapability.NONE, "no access", {}),
            ):
                await source.start(["AAPL"])

        assert source._delegate is not None
        assert cache.get_price("AAPL") is not None
        assert source.get_status().mode == MarketSourceMode.SIMULATOR

        await source.stop()

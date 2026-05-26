"""Tests for SSE market price streaming."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from app.market.cache import PriceCache
from app.market.stream import _generate_events, create_stream_router


class FakeRequest:
    """Minimal request object for the SSE generator."""

    def __init__(self) -> None:
        self.client = SimpleNamespace(host="test")
        self.disconnected = False

    async def is_disconnected(self) -> bool:
        return self.disconnected


class TestStreamRouter:
    """Unit tests for stream router factory."""

    def test_create_stream_router_has_prices_route(self):
        cache = PriceCache()
        router = create_stream_router(cache)
        routes = [route.path for route in router.routes]
        assert "/api/stream/prices" in routes

    def test_create_stream_router_returns_distinct_routers(self):
        cache = PriceCache()
        router1 = create_stream_router(cache)
        router2 = create_stream_router(cache)
        assert router1 is not router2
        assert len(router1.routes) == 1
        assert len(router2.routes) == 1


@pytest.mark.asyncio
class TestGenerateEvents:
    """Unit tests for the SSE async generator."""

    async def test_initial_retry_directive(self):
        cache = PriceCache()
        request = FakeRequest()
        gen = _generate_events(cache, request, interval=0.01)

        event = await gen.__anext__()
        assert event == "retry: 1000\n\n"

        await gen.aclose()

    async def test_emits_json_payload_when_cache_changes(self):
        cache = PriceCache()
        request = FakeRequest()
        gen = _generate_events(cache, request, interval=0.01)

        await gen.__anext__()  # retry directive
        cache.update("AAPL", 190.50, timestamp=123.0)
        event = await gen.__anext__()

        assert event.startswith("data: ")
        payload = json.loads(event.removeprefix("data: ").strip())
        assert payload["AAPL"]["price"] == 190.50
        assert payload["AAPL"]["direction"] == "flat"

        await gen.aclose()

    async def test_no_duplicate_payload_without_version_change(self):
        cache = PriceCache()
        request = FakeRequest()
        gen = _generate_events(cache, request, interval=0.05)

        await gen.__anext__()  # retry directive
        cache.update("AAPL", 190.50)
        await gen.__anext__()  # first data payload

        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(gen.__anext__(), timeout=0.02)

        await gen.aclose()

    async def test_emits_empty_payload_when_cache_becomes_empty(self):
        cache = PriceCache()
        request = FakeRequest()
        gen = _generate_events(cache, request, interval=0.01)

        await gen.__anext__()  # retry directive
        cache.update("AAPL", 190.50)
        await gen.__anext__()  # first data payload

        cache.remove("AAPL")
        event = await gen.__anext__()

        assert event == "data: {}\n\n"

        await gen.aclose()

    async def test_stops_when_disconnected(self):
        cache = PriceCache()
        request = FakeRequest()
        gen = _generate_events(cache, request, interval=0.01)

        await gen.__anext__()  # retry directive
        request.disconnected = True

        with pytest.raises(StopAsyncIteration):
            await gen.__anext__()

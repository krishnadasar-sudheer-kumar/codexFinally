"""API route tests for the FinAlly backend."""

from __future__ import annotations

import sys
import types

from fastapi.testclient import TestClient

from app.main import create_app
from app.market import PriceCache
from app.market.models import MarketSourceMode, MarketStatus
from app.services import LocalSQLiteRepository


class MockMarketSource:
    def __init__(self):
        self.started_with: list[str] = []
        self.tickers: set[str] = set()
        self.stopped = False

    async def start(self, tickers: list[str]) -> None:
        self.started_with = tickers
        self.tickers.update(tickers)

    async def stop(self) -> None:
        self.stopped = True

    async def add_ticker(self, ticker: str) -> None:
        self.tickers.add(ticker)

    async def remove_ticker(self, ticker: str) -> None:
        self.tickers.discard(ticker)

    def get_tickers(self) -> list[str]:
        return sorted(self.tickers)

    def get_status(self) -> MarketStatus:
        return MarketStatus(
            mode=MarketSourceMode.SIMULATOR,
            provider="mock",
            healthy=True,
            message="ok",
        )


def make_client(tmp_path):
    cache = PriceCache()
    cache.update("AAPL", 200.0, timestamp=1_700_000_000)
    cache.update("MSFT", 400.0, timestamp=1_700_000_000)
    source = MockMarketSource()
    app = create_app(
        repository=LocalSQLiteRepository(tmp_path / "finally.db"),
        price_cache=cache,
        market_source_factory=lambda _: source,
        static_dir=tmp_path / "missing-static",
    )
    return TestClient(app), source


def test_health_and_lifespan_start_market_source(tmp_path):
    with make_client(tmp_path)[0] as client:
        response = client.get("/api/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"
        assert response.json()["market"]["provider"] == "mock"


def test_watchlist_add_and_delete_are_idempotent(tmp_path):
    client, source = make_client(tmp_path)
    with client:
        created = client.post("/api/watchlist", json={"ticker": "ibm"})
        duplicate = client.post("/api/watchlist", json={"ticker": "IBM"})
        removed = client.delete("/api/watchlist/IBM")
        removed_again = client.delete("/api/watchlist/IBM")

    assert created.status_code == 200
    assert created.json()["already_exists"] is False
    assert duplicate.json()["already_exists"] is True
    assert removed.json() == {"ticker": "IBM", "removed": True}
    assert removed_again.json() == {"ticker": "IBM", "removed": False}
    assert "IBM" not in source.tickers


def test_portfolio_trade_math_fractional_shares_and_guardrails(tmp_path):
    client, _ = make_client(tmp_path)
    with client:
        buy = client.post("/api/portfolio/trade", json={"ticker": "AAPL", "side": "buy", "quantity": 1.5})
        portfolio = client.get("/api/portfolio")
        too_many = client.post(
            "/api/portfolio/trade",
            json={"ticker": "AAPL", "side": "sell", "quantity": 5},
        )
        sell = client.post(
            "/api/portfolio/trade",
            json={"ticker": "AAPL", "side": "sell", "quantity": 0.5},
        )
        history = client.get("/api/portfolio/history")

    assert buy.status_code == 200
    assert buy.json()["trade"]["price"] == 200.0
    assert buy.json()["portfolio"]["cash_balance"] == 99700.0
    assert portfolio.json()["positions"][0]["quantity"] == 1.5
    assert too_many.status_code == 400
    assert too_many.json()["error"] == "insufficient_shares"
    assert sell.status_code == 200
    assert sell.json()["portfolio"]["positions"][0]["quantity"] == 1.0
    assert len(history.json()["snapshots"]) >= 3


def test_buy_rejects_insufficient_cash(tmp_path):
    client, _ = make_client(tmp_path)
    with client:
        response = client.post(
            "/api/portfolio/trade",
            json={"ticker": "AAPL", "side": "buy", "quantity": 1_000_000},
        )

    assert response.status_code == 400
    assert response.json()["error"] in {"insufficient_cash", "order_value_exceeded"}


def test_chat_executes_llm_actions_and_reports_errors(tmp_path, monkeypatch):
    module = types.ModuleType("app.llm")

    def generate_chat_plan(message, context):
        assert context["portfolio"]["cash_balance"] == 100000.0
        return {
            "message": f"Handled: {message}",
            "trades": [
                {"ticker": "AAPL", "side": "buy", "quantity": 1},
                {"ticker": "AAPL", "side": "sell", "quantity": 5},
            ],
            "watchlist_changes": [{"ticker": "IBM", "action": "add"}],
        }

    module.generate_chat_plan = generate_chat_plan
    monkeypatch.setitem(sys.modules, "app.llm", module)

    client, source = make_client(tmp_path)
    with client:
        response = client.post("/api/chat", json={"message": "buy one apple"})
        portfolio = client.get("/api/portfolio")

    assert response.status_code == 200
    payload = response.json()
    assert payload["message"] == "Handled: buy one apple"
    assert len(payload["trades"]) == 1
    assert payload["errors"][0]["error"] == "insufficient_shares"
    assert payload["watchlist_changes"][0]["item"]["ticker"] == "IBM"
    assert portfolio.json()["positions"][0]["quantity"] == 1.0
    assert "IBM" in source.tickers

"""Tests for FinAlly LLM structured output handling."""

import pytest

from app.llm import (
    ChatPlan,
    LLMConfigurationError,
    StructuredOutputError,
    TradeAction,
    WatchlistAction,
    generate_chat_plan,
    get_default_model,
    parse_chat_plan_json,
)


class TestChatPlanParsing:
    """Structured JSON parsing tests."""

    def test_parse_valid_json(self):
        """Parse structured output into immutable action models."""
        plan = parse_chat_plan_json(
            """
            {
              "message": "Buying and tracking a couple of names.",
              "trades": [{"ticker": "aapl", "side": "BUY", "quantity": 3}],
              "watchlist_changes": [{"ticker": "PYPL", "action": "add"}]
            }
            """
        )

        assert plan == ChatPlan(
            message="Buying and tracking a couple of names.",
            trades=(TradeAction(ticker="AAPL", side="buy", quantity=3),),
            watchlist_changes=(WatchlistAction(ticker="PYPL", action="add"),),
        )
        assert plan.to_dict() == {
            "message": "Buying and tracking a couple of names.",
            "trades": [{"ticker": "AAPL", "side": "buy", "quantity": 3}],
            "watchlist_changes": [{"ticker": "PYPL", "action": "add"}],
        }

    def test_parse_json_code_fence(self):
        """Allow a JSON code fence as a defensive fallback."""
        plan = parse_chat_plan_json(
            """```json
            {"message": "No action.", "trades": [], "watchlist_changes": []}
            ```"""
        )

        assert plan.message == "No action."
        assert plan.trades == ()
        assert plan.watchlist_changes == ()

    def test_malformed_json_raises_structured_error(self):
        """Malformed model output is converted to a domain error."""
        with pytest.raises(StructuredOutputError, match="not valid JSON"):
            parse_chat_plan_json('{"message": "oops", "trades": [}')

    def test_missing_message_raises_structured_error(self):
        """A conversational message is required."""
        with pytest.raises(StructuredOutputError, match="message must be a string"):
            parse_chat_plan_json({"trades": [], "watchlist_changes": []})


class TestActionValidation:
    """Action model validation tests."""

    def test_rejects_invalid_trade_side(self):
        """Trade side must be buy or sell."""
        with pytest.raises(StructuredOutputError, match="trade.side"):
            TradeAction(ticker="AAPL", side="hold", quantity=1)

    def test_rejects_non_positive_trade_quantity(self):
        """Trade quantity must be positive."""
        with pytest.raises(StructuredOutputError, match="greater than 0"):
            TradeAction(ticker="AAPL", side="buy", quantity=0)

    def test_rejects_bool_trade_quantity(self):
        """Bool is not accepted as a numeric quantity."""
        with pytest.raises(StructuredOutputError, match="number"):
            TradeAction(ticker="AAPL", side="buy", quantity=True)

    def test_accepts_fractional_trade_quantity(self):
        """Fractional shares are valid across the trading API."""
        action = TradeAction(ticker="AAPL", side="buy", quantity=1.25)

        assert action.quantity == 1.25
        assert action.to_dict()["quantity"] == 1.25

    def test_rejects_invalid_watchlist_action(self):
        """Watchlist action must be add or remove."""
        with pytest.raises(StructuredOutputError, match="watchlist_changes.action"):
            WatchlistAction(ticker="AAPL", action="pin")

    def test_rejects_invalid_ticker(self):
        """Ticker symbols are intentionally narrow for downstream validation."""
        with pytest.raises(StructuredOutputError, match="ticker must"):
            WatchlistAction(ticker="$AAPL", action="add")


class TestGenerateChatPlan:
    """Generation entry point tests."""

    async def test_mock_buy_response_is_deterministic(self):
        """Mock mode returns deterministic actions without an API key."""
        plan = await generate_chat_plan(
            "Please buy 5 shares of MSFT",
            {"cash": 10000},
            [{"role": "assistant", "content": "Ready."}],
            mock=True,
        )

        assert plan.message == "Mock mode: prepared a simulated buy order for 5 share(s) of MSFT."
        assert plan.trades == (TradeAction(ticker="MSFT", side="buy", quantity=5),)
        assert plan.watchlist_changes == ()

    async def test_mock_watchlist_response_is_deterministic(self):
        """Mock mode can drive E2E watchlist flows."""
        plan = await generate_chat_plan(
            "Add NVDA to my watchlist",
            {},
            [],
            mock=True,
        )

        assert plan.trades == ()
        assert plan.watchlist_changes == (WatchlistAction(ticker="NVDA", action="add"),)

    async def test_non_mock_without_api_key_fails_gracefully(self, monkeypatch):
        """Non-mock mode reports the missing API key clearly."""
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        with pytest.raises(LLMConfigurationError, match="OPENAI_API_KEY"):
            await generate_chat_plan("hello", {}, [], mock=False)

    def test_default_model_from_env(self, monkeypatch):
        """Model selection defaults to gpt-5.5 and can be overridden."""
        monkeypatch.delenv("OPENAI_DEFAULT_MODEL", raising=False)
        assert get_default_model() == "gpt-5.5"

        monkeypatch.setenv("OPENAI_DEFAULT_MODEL", "gpt-test")
        assert get_default_model() == "gpt-test"

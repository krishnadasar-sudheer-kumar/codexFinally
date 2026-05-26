"""LLM integration layer for FinAlly chat planning."""

from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass, field
from typing import Any

DEFAULT_MODEL = "gpt-5.5"
VALID_TRADE_SIDES = frozenset({"buy", "sell"})
VALID_WATCHLIST_ACTIONS = frozenset({"add", "remove"})
DEFAULT_MOCK_TICKERS = frozenset(
    {"AAPL", "GOOGL", "MSFT", "AMZN", "TSLA", "NVDA", "META", "JPM", "V", "NFLX", "PYPL"}
)

_TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.-]{0,11}$")
_TOKEN_RE = re.compile(r"\b[A-Z][A-Z0-9.-]{0,11}\b")
_QUANTITY_RE = re.compile(r"\b(\d+)\b")
_TRUTHY = frozenset({"1", "true", "t", "yes", "y", "on"})


class LLMError(RuntimeError):
    """Base error for FinAlly LLM integration failures."""


class LLMConfigurationError(LLMError):
    """Raised when non-mock LLM use is not configured."""


class StructuredOutputError(LLMError, ValueError):
    """Raised when a structured LLM response cannot be parsed or validated."""


@dataclass(frozen=True, slots=True)
class TradeAction:
    """Trade action requested by the assistant."""

    ticker: str
    side: str
    quantity: float

    def __post_init__(self) -> None:
        ticker = _normalize_ticker(self.ticker)
        side = _require_str(self.side, "trade.side").strip().lower()
        quantity = self.quantity

        if side not in VALID_TRADE_SIDES:
            raise StructuredOutputError(
                f"trade.side must be one of {sorted(VALID_TRADE_SIDES)}; got {self.side!r}"
            )
        if not isinstance(quantity, int | float) or isinstance(quantity, bool):
            raise StructuredOutputError("trade.quantity must be a number")
        quantity = float(quantity)
        if not math.isfinite(quantity):
            raise StructuredOutputError("trade.quantity must be finite")
        if quantity <= 0:
            raise StructuredOutputError("trade.quantity must be greater than 0")

        object.__setattr__(self, "ticker", ticker)
        object.__setattr__(self, "side", side)
        object.__setattr__(self, "quantity", quantity)

    def to_dict(self) -> dict[str, Any]:
        """Serialize for JSON responses."""
        return {"ticker": self.ticker, "side": self.side, "quantity": self.quantity}


@dataclass(frozen=True, slots=True)
class WatchlistAction:
    """Watchlist action requested by the assistant."""

    ticker: str
    action: str

    def __post_init__(self) -> None:
        ticker = _normalize_ticker(self.ticker)
        action = _require_str(self.action, "watchlist_changes.action").strip().lower()

        if action not in VALID_WATCHLIST_ACTIONS:
            raise StructuredOutputError(
                "watchlist_changes.action must be one of "
                f"{sorted(VALID_WATCHLIST_ACTIONS)}; got {self.action!r}"
            )

        object.__setattr__(self, "ticker", ticker)
        object.__setattr__(self, "action", action)

    def to_dict(self) -> dict[str, Any]:
        """Serialize for JSON responses."""
        return {"ticker": self.ticker, "action": self.action}


@dataclass(frozen=True, slots=True)
class ChatPlan:
    """Structured assistant response consumed by the chat API."""

    message: str
    trades: tuple[TradeAction, ...] = field(default_factory=tuple)
    watchlist_changes: tuple[WatchlistAction, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        message = _require_str(self.message, "message").strip()
        if not message:
            raise StructuredOutputError("message must be a non-empty string")

        object.__setattr__(self, "message", message)
        object.__setattr__(self, "trades", tuple(self.trades))
        object.__setattr__(self, "watchlist_changes", tuple(self.watchlist_changes))

    def to_dict(self) -> dict[str, Any]:
        """Serialize for JSON responses."""
        return {
            "message": self.message,
            "trades": [trade.to_dict() for trade in self.trades],
            "watchlist_changes": [action.to_dict() for action in self.watchlist_changes],
        }


async def generate_chat_plan(
    message: str,
    portfolio_context: dict[str, Any] | None,
    history: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None,
    *,
    mock: bool | None = None,
) -> ChatPlan:
    """Generate a structured chat plan from the configured LLM or deterministic mock."""

    if mock is None:
        mock = _env_truthy("LLM_MOCK")
    if mock:
        return _mock_chat_plan(message)

    return await _generate_openai_chat_plan(message, portfolio_context or {}, list(history or ()))


def parse_chat_plan_json(raw_response: str | bytes | dict[str, Any]) -> ChatPlan:
    """Parse and validate JSON matching the FinAlly chat plan schema."""

    if isinstance(raw_response, dict):
        payload = raw_response
    else:
        text = _strip_code_fence(_require_text(raw_response, "raw_response"))
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise StructuredOutputError(f"LLM response was not valid JSON: {exc.msg}") from exc

    if not isinstance(payload, dict):
        raise StructuredOutputError("LLM response JSON must be an object")

    message = _require_str(payload.get("message"), "message")
    trades = _parse_trades(payload.get("trades", []))
    watchlist_changes = _parse_watchlist_changes(payload.get("watchlist_changes", []))
    return ChatPlan(message=message, trades=trades, watchlist_changes=watchlist_changes)


def get_default_model() -> str:
    """Return the configured OpenAI model name."""
    return os.getenv("OPENAI_DEFAULT_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL


async def _generate_openai_chat_plan(
    message: str,
    portfolio_context: dict[str, Any],
    history: list[dict[str, Any]],
) -> ChatPlan:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise LLMConfigurationError(
            "OPENAI_API_KEY is required when LLM_MOCK is not true. "
            "Set OPENAI_API_KEY or enable LLM_MOCK=true for deterministic local responses."
        )

    try:
        from openai import AsyncOpenAI
    except ImportError as exc:  # pragma: no cover - depends on optional local package state
        raise LLMConfigurationError(
            "The OpenAI SDK is not installed. Install the 'openai' package or enable LLM_MOCK=true."
        ) from exc

    client = AsyncOpenAI(api_key=api_key)
    model = get_default_model()
    system_prompt = _build_system_prompt()
    user_prompt = _build_user_prompt(message, portfolio_context, history)

    try:
        response = await client.responses.create(
            model=model,
            input=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            text={
                "format": {
                    "type": "json_schema",
                    "name": "finally_chat_plan",
                    "schema": _chat_plan_json_schema(),
                    "strict": True,
                }
            },
        )
    except Exception as exc:  # pragma: no cover - live SDK/API path
        raise LLMError(f"OpenAI chat plan generation failed: {exc}") from exc

    return parse_chat_plan_json(_extract_response_text(response))


def _parse_trades(raw_trades: Any) -> tuple[TradeAction, ...]:
    if raw_trades is None:
        return ()
    if not isinstance(raw_trades, list):
        raise StructuredOutputError("trades must be an array")

    trades = []
    for index, trade in enumerate(raw_trades):
        if not isinstance(trade, dict):
            raise StructuredOutputError(f"trades[{index}] must be an object")
        try:
            trades.append(
                TradeAction(
                    ticker=trade.get("ticker"),
                    side=trade.get("side"),
                    quantity=trade.get("quantity"),
                )
            )
        except StructuredOutputError as exc:
            raise StructuredOutputError(f"trades[{index}]: {exc}") from exc
    return tuple(trades)


def _parse_watchlist_changes(raw_changes: Any) -> tuple[WatchlistAction, ...]:
    if raw_changes is None:
        return ()
    if not isinstance(raw_changes, list):
        raise StructuredOutputError("watchlist_changes must be an array")

    changes = []
    for index, change in enumerate(raw_changes):
        if not isinstance(change, dict):
            raise StructuredOutputError(f"watchlist_changes[{index}] must be an object")
        try:
            changes.append(
                WatchlistAction(
                    ticker=change.get("ticker"),
                    action=change.get("action"),
                )
            )
        except StructuredOutputError as exc:
            raise StructuredOutputError(f"watchlist_changes[{index}]: {exc}") from exc
    return tuple(changes)


def _mock_chat_plan(message: str) -> ChatPlan:
    text = _require_str(message, "message").strip()
    lower = text.lower()
    ticker = _extract_ticker(text)
    quantity = _extract_quantity(text)

    if "watchlist" in lower or "watch list" in lower:
        action = "remove" if any(word in lower for word in ("remove", "delete", "drop")) else "add"
        return ChatPlan(
            message=f"Mock mode: {action}ed {ticker} on the watchlist.",
            watchlist_changes=(WatchlistAction(ticker=ticker, action=action),),
        )

    if "sell" in lower:
        return ChatPlan(
            message=f"Mock mode: prepared a simulated sell order for {quantity} share(s) of {ticker}.",
            trades=(TradeAction(ticker=ticker, side="sell", quantity=quantity),),
        )

    if "buy" in lower:
        return ChatPlan(
            message=f"Mock mode: prepared a simulated buy order for {quantity} share(s) of {ticker}.",
            trades=(TradeAction(ticker=ticker, side="buy", quantity=quantity),),
        )

    return ChatPlan(
        message=(
            "Mock mode: reviewed the portfolio context and found no automatic trades "
            "or watchlist changes to execute."
        )
    )


def _build_system_prompt() -> str:
    return (
        "You are FinAlly, an AI trading assistant. Analyze portfolio composition, "
        "risk concentration, P&L, and watchlist opportunities. Be concise and data-driven. "
        "Execute trades or watchlist changes only when the user asks or agrees. "
        "Always respond with valid JSON matching the requested schema."
    )


def _build_user_prompt(
    message: str,
    portfolio_context: dict[str, Any],
    history: list[dict[str, Any]],
) -> str:
    return json.dumps(
        {
            "portfolio_context": portfolio_context,
            "history": history,
            "message": message,
            "required_response_shape": {
                "message": "string",
                "trades": [{"ticker": "AAPL", "side": "buy", "quantity": 10.5}],
                "watchlist_changes": [{"ticker": "PYPL", "action": "add"}],
            },
        },
        sort_keys=True,
    )


def _chat_plan_json_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["message", "trades", "watchlist_changes"],
        "properties": {
            "message": {"type": "string", "minLength": 1},
            "trades": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["ticker", "side", "quantity"],
                    "properties": {
                        "ticker": {"type": "string", "pattern": _TICKER_RE.pattern},
                        "side": {"type": "string", "enum": sorted(VALID_TRADE_SIDES)},
                        "quantity": {"type": "number", "exclusiveMinimum": 0},
                    },
                },
            },
            "watchlist_changes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["ticker", "action"],
                    "properties": {
                        "ticker": {"type": "string", "pattern": _TICKER_RE.pattern},
                        "action": {"type": "string", "enum": sorted(VALID_WATCHLIST_ACTIONS)},
                    },
                },
            },
        },
    }


def _extract_response_text(response: Any) -> str:
    output_text = getattr(response, "output_text", None)
    if output_text:
        return output_text

    output = getattr(response, "output", None)
    if output:
        chunks = []
        for item in output:
            for content in getattr(item, "content", []) or []:
                text = getattr(content, "text", None)
                if text:
                    chunks.append(text)
        if chunks:
            return "\n".join(chunks)

    raise StructuredOutputError("OpenAI response did not include output text")


def _extract_ticker(message: str) -> str:
    ignored = {
        "BUY",
        "SELL",
        "ADD",
        "REMOVE",
        "WATCHLIST",
        "WATCH",
        "LIST",
        "MOCK",
        "MODE",
        "PLEASE",
        "SHARES",
        "SHARE",
        "OF",
        "TO",
        "MY",
        "THE",
    }
    candidates = [token for token in _TOKEN_RE.findall(message.upper()) if token not in ignored]
    for token in candidates:
        if token in DEFAULT_MOCK_TICKERS:
            return token
    if candidates:
        return candidates[-1]
    return "AAPL"


def _extract_quantity(message: str) -> int:
    match = _QUANTITY_RE.search(message)
    if not match:
        return 1
    return max(1, int(match.group(1)))


def _normalize_ticker(value: Any) -> str:
    ticker = _require_str(value, "ticker").strip().upper()
    if not _TICKER_RE.fullmatch(ticker):
        raise StructuredOutputError(
            "ticker must be 1-12 characters of uppercase letters, numbers, dots, or hyphens"
        )
    return ticker


def _require_str(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise StructuredOutputError(f"{field_name} must be a string")
    return value


def _require_text(value: str | bytes, field_name: str) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8")
    if isinstance(value, str):
        return value
    raise StructuredOutputError(f"{field_name} must be a string, bytes, or object")


def _strip_code_fence(text: str) -> str:
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped

    lines = stripped.splitlines()
    if len(lines) >= 3 and lines[-1].strip() == "```":
        return "\n".join(lines[1:-1]).strip()
    return stripped


def _env_truthy(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in _TRUTHY

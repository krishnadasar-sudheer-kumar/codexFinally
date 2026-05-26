"""Application services for FinAlly portfolio, watchlist, and chat flows."""

from __future__ import annotations

import inspect
import json
import os
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from app.market import MarketDataSource, PriceCache
from app.market.seed_prices import SEED_PRICES
from app.market.utils import normalize_ticker, normalize_tickers

DEFAULT_USER_ID = "default"
DEFAULT_WATCHLIST = ["AAPL", "GOOGL", "MSFT", "AMZN", "TSLA", "NVDA", "META", "JPM", "V", "NFLX"]
TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.-]{0,9}$")


class ServiceError(Exception):
    """Structured user-facing service error."""

    def __init__(self, error: str, details: dict[str, Any] | None = None, status_code: int = 400):
        super().__init__(error)
        self.error = error
        self.details = details or {}
        self.status_code = status_code

    def to_dict(self) -> dict[str, Any]:
        return {"error": self.error, "details": self.details}


class Repository(Protocol):
    def init_db(self) -> None: ...
    def get_profile(self, user_id: str = DEFAULT_USER_ID) -> dict[str, Any]: ...
    def set_cash_balance(self, cash_balance: float, user_id: str = DEFAULT_USER_ID) -> None: ...
    def list_watchlist(self, user_id: str = DEFAULT_USER_ID) -> list[str]: ...
    def add_watchlist(self, ticker: str, user_id: str = DEFAULT_USER_ID) -> bool: ...
    def remove_watchlist(self, ticker: str, user_id: str = DEFAULT_USER_ID) -> bool: ...
    def list_positions(self, user_id: str = DEFAULT_USER_ID) -> list[dict[str, Any]]: ...
    def get_position(self, ticker: str, user_id: str = DEFAULT_USER_ID) -> dict[str, Any] | None: ...
    def upsert_position(
        self, ticker: str, quantity: float, avg_cost: float, user_id: str = DEFAULT_USER_ID
    ) -> None: ...
    def delete_position(self, ticker: str, user_id: str = DEFAULT_USER_ID) -> None: ...
    def add_trade(
        self, ticker: str, side: str, quantity: float, price: float, user_id: str = DEFAULT_USER_ID
    ) -> dict[str, Any]: ...
    def list_snapshots(self, user_id: str = DEFAULT_USER_ID) -> list[dict[str, Any]]: ...
    def add_snapshot(self, total_value: float, user_id: str = DEFAULT_USER_ID) -> dict[str, Any]: ...
    def list_chat_messages(self, limit: int = 20, user_id: str = DEFAULT_USER_ID) -> list[dict[str, Any]]: ...
    def add_chat_message(
        self,
        role: str,
        content: str,
        actions: dict[str, Any] | None = None,
        user_id: str = DEFAULT_USER_ID,
    ) -> dict[str, Any]: ...


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def iso_from_unix(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, UTC).isoformat().replace("+00:00", "Z")


def round_money(value: float) -> float:
    return round(float(value), 2)


def validate_ticker(ticker: str) -> str:
    normalized = normalize_ticker(ticker)
    if not normalized or not TICKER_RE.match(normalized):
        raise ServiceError("invalid_ticker", {"ticker": ticker})
    return normalized


def default_db_path() -> Path:
    raw = os.environ.get("FINALLY_DB_PATH") or os.environ.get("DATABASE_PATH") or os.environ.get("DB_PATH")
    if raw:
        return Path(raw)
    return Path(__file__).resolve().parents[2] / "db" / "finally.db"


class LocalSQLiteRepository:
    """Small SQLite repository used until/when backend.app.db helpers are present.

    The public methods mirror the expected DB helper responsibilities so the
    service layer can be swapped onto the dedicated DB package without changing
    API behavior.
    """

    def __init__(self, db_path: Path | None = None):
        self.db_path = Path(db_path) if db_path else default_db_path()

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users_profile (
                    id TEXT PRIMARY KEY,
                    cash_balance REAL NOT NULL DEFAULT 100000.0,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS watchlist (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL DEFAULT 'default',
                    ticker TEXT NOT NULL,
                    added_at TEXT NOT NULL,
                    UNIQUE(user_id, ticker)
                );
                CREATE TABLE IF NOT EXISTS positions (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL DEFAULT 'default',
                    ticker TEXT NOT NULL,
                    quantity REAL NOT NULL,
                    avg_cost REAL NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(user_id, ticker)
                );
                CREATE TABLE IF NOT EXISTS trades (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL DEFAULT 'default',
                    ticker TEXT NOT NULL,
                    side TEXT NOT NULL,
                    quantity REAL NOT NULL,
                    price REAL NOT NULL,
                    executed_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS portfolio_snapshots (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL DEFAULT 'default',
                    total_value REAL NOT NULL,
                    recorded_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS chat_messages (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL DEFAULT 'default',
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    actions TEXT,
                    created_at TEXT NOT NULL
                );
                """
            )
            now = utc_now()
            conn.execute(
                "INSERT OR IGNORE INTO users_profile (id, cash_balance, created_at) VALUES (?, ?, ?)",
                (DEFAULT_USER_ID, 100000.0, now),
            )
            for ticker in DEFAULT_WATCHLIST:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO watchlist (id, user_id, ticker, added_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (str(uuid.uuid4()), DEFAULT_USER_ID, ticker, now),
                )
            snapshot = conn.execute(
                "SELECT 1 FROM portfolio_snapshots WHERE user_id = ? LIMIT 1",
                (DEFAULT_USER_ID,),
            ).fetchone()
            if snapshot is None:
                conn.execute(
                    """
                    INSERT INTO portfolio_snapshots (id, user_id, total_value, recorded_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (str(uuid.uuid4()), DEFAULT_USER_ID, 100000.0, now),
                )

    def get_profile(self, user_id: str = DEFAULT_USER_ID) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, cash_balance, created_at FROM users_profile WHERE id = ?",
                (user_id,),
            ).fetchone()
        if row is None:
            raise ServiceError("profile_not_found", {"user_id": user_id}, status_code=500)
        return dict(row)

    def set_cash_balance(self, cash_balance: float, user_id: str = DEFAULT_USER_ID) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE users_profile SET cash_balance = ? WHERE id = ?",
                (float(cash_balance), user_id),
            )

    def list_watchlist(self, user_id: str = DEFAULT_USER_ID) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT ticker FROM watchlist WHERE user_id = ? ORDER BY added_at, ticker",
                (user_id,),
            ).fetchall()
        return [row["ticker"] for row in rows]

    def add_watchlist(self, ticker: str, user_id: str = DEFAULT_USER_ID) -> bool:
        with self._connect() as conn:
            before = conn.total_changes
            conn.execute(
                """
                INSERT OR IGNORE INTO watchlist (id, user_id, ticker, added_at)
                VALUES (?, ?, ?, ?)
                """,
                (str(uuid.uuid4()), user_id, ticker, utc_now()),
            )
            return conn.total_changes > before

    def remove_watchlist(self, ticker: str, user_id: str = DEFAULT_USER_ID) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM watchlist WHERE user_id = ? AND ticker = ?",
                (user_id, ticker),
            )
            return cur.rowcount > 0

    def list_positions(self, user_id: str = DEFAULT_USER_ID) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT ticker, quantity, avg_cost, updated_at
                FROM positions
                WHERE user_id = ?
                ORDER BY ticker
                """,
                (user_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_position(self, ticker: str, user_id: str = DEFAULT_USER_ID) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT ticker, quantity, avg_cost, updated_at
                FROM positions
                WHERE user_id = ? AND ticker = ?
                """,
                (user_id, ticker),
            ).fetchone()
        return dict(row) if row else None

    def upsert_position(
        self, ticker: str, quantity: float, avg_cost: float, user_id: str = DEFAULT_USER_ID
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO positions (id, user_id, ticker, quantity, avg_cost, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, ticker)
                DO UPDATE SET quantity = excluded.quantity,
                              avg_cost = excluded.avg_cost,
                              updated_at = excluded.updated_at
                """,
                (str(uuid.uuid4()), user_id, ticker, float(quantity), float(avg_cost), utc_now()),
            )

    def delete_position(self, ticker: str, user_id: str = DEFAULT_USER_ID) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM positions WHERE user_id = ? AND ticker = ?", (user_id, ticker))

    def add_trade(
        self, ticker: str, side: str, quantity: float, price: float, user_id: str = DEFAULT_USER_ID
    ) -> dict[str, Any]:
        trade = {
            "id": str(uuid.uuid4()),
            "user_id": user_id,
            "ticker": ticker,
            "side": side,
            "quantity": float(quantity),
            "price": float(price),
            "executed_at": utc_now(),
        }
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO trades (id, user_id, ticker, side, quantity, price, executed_at)
                VALUES (:id, :user_id, :ticker, :side, :quantity, :price, :executed_at)
                """,
                trade,
            )
        return trade

    def list_snapshots(self, user_id: str = DEFAULT_USER_ID) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT total_value, recorded_at
                FROM portfolio_snapshots
                WHERE user_id = ?
                ORDER BY recorded_at
                """,
                (user_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def add_snapshot(self, total_value: float, user_id: str = DEFAULT_USER_ID) -> dict[str, Any]:
        snapshot = {
            "id": str(uuid.uuid4()),
            "user_id": user_id,
            "total_value": round_money(total_value),
            "recorded_at": utc_now(),
        }
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO portfolio_snapshots (id, user_id, total_value, recorded_at)
                VALUES (:id, :user_id, :total_value, :recorded_at)
                """,
                snapshot,
            )
        return snapshot

    def list_chat_messages(self, limit: int = 20, user_id: str = DEFAULT_USER_ID) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT role, content, actions, created_at
                FROM chat_messages
                WHERE user_id = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (user_id, int(limit)),
            ).fetchall()
        messages = [dict(row) for row in rows]
        messages.reverse()
        return messages

    def add_chat_message(
        self,
        role: str,
        content: str,
        actions: dict[str, Any] | None = None,
        user_id: str = DEFAULT_USER_ID,
    ) -> dict[str, Any]:
        message = {
            "id": str(uuid.uuid4()),
            "user_id": user_id,
            "role": role,
            "content": content,
            "actions": json.dumps(actions) if actions is not None else None,
            "created_at": utc_now(),
        }
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO chat_messages (id, user_id, role, content, actions, created_at)
                VALUES (:id, :user_id, :role, :content, :actions, :created_at)
                """,
                message,
            )
        return message


class DbFunctionRepository(LocalSQLiteRepository):
    """Adapter for the expected backend.app.db repository helper functions.

    It deliberately keeps LocalSQLiteRepository as a fallback for helpers that
    are not present yet, which makes parallel agent work less brittle.
    """

    def __init__(self, module: Any, db_path: Path | None = None):
        super().__init__(db_path)
        self.module = module

    def _call(self, names: tuple[str, ...], *args: Any) -> Any:
        for name in names:
            fn = getattr(self.module, name, None)
            if callable(fn):
                return fn(*args)
        raise AttributeError(names[0])

    def init_db(self) -> None:
        try:
            self._call(("init_db", "initialize_db"), self.db_path)
        except AttributeError:
            super().init_db()

    def get_profile(self, user_id: str = DEFAULT_USER_ID) -> dict[str, Any]:
        try:
            profile = self._call(("get_user_profile", "get_profile"), self.db_path, user_id)
        except AttributeError:
            return super().get_profile(user_id)
        if profile is None:
            raise ServiceError("profile_not_found", {"user_id": user_id}, status_code=500)
        return profile

    def set_cash_balance(self, cash_balance: float, user_id: str = DEFAULT_USER_ID) -> None:
        try:
            self._call(("update_cash_balance", "set_cash_balance"), self.db_path, cash_balance, user_id)
        except AttributeError:
            super().set_cash_balance(cash_balance, user_id)

    def list_watchlist(self, user_id: str = DEFAULT_USER_ID) -> list[str]:
        try:
            rows = self._call(("list_watchlist", "get_watchlist"), self.db_path, user_id)
        except AttributeError:
            return super().list_watchlist(user_id)
        tickers = []
        for row in rows:
            tickers.append(row["ticker"] if isinstance(row, dict) else str(row))
        return tickers

    def add_watchlist(self, ticker: str, user_id: str = DEFAULT_USER_ID) -> bool:
        try:
            before = set(self.list_watchlist(user_id))
            self._call(("add_watchlist_ticker", "add_to_watchlist"), self.db_path, ticker, user_id)
            return ticker not in before
        except AttributeError:
            return super().add_watchlist(ticker, user_id)

    def remove_watchlist(self, ticker: str, user_id: str = DEFAULT_USER_ID) -> bool:
        try:
            return bool(
                self._call(
                    ("remove_watchlist_ticker", "delete_watchlist_ticker"),
                    self.db_path,
                    ticker,
                    user_id,
                )
            )
        except AttributeError:
            return super().remove_watchlist(ticker, user_id)

    def list_positions(self, user_id: str = DEFAULT_USER_ID) -> list[dict[str, Any]]:
        try:
            return self._call(("list_positions",), self.db_path, user_id)
        except AttributeError:
            return super().list_positions(user_id)

    def get_position(self, ticker: str, user_id: str = DEFAULT_USER_ID) -> dict[str, Any] | None:
        try:
            return self._call(("get_position",), self.db_path, ticker, user_id)
        except AttributeError:
            return super().get_position(ticker, user_id)

    def upsert_position(
        self, ticker: str, quantity: float, avg_cost: float, user_id: str = DEFAULT_USER_ID
    ) -> None:
        try:
            self._call(("upsert_position",), self.db_path, ticker, quantity, avg_cost, user_id)
        except AttributeError:
            super().upsert_position(ticker, quantity, avg_cost, user_id)

    def delete_position(self, ticker: str, user_id: str = DEFAULT_USER_ID) -> None:
        try:
            self._call(("delete_position",), self.db_path, ticker, user_id)
        except AttributeError:
            super().delete_position(ticker, user_id)

    def add_trade(
        self, ticker: str, side: str, quantity: float, price: float, user_id: str = DEFAULT_USER_ID
    ) -> dict[str, Any]:
        try:
            return self._call(("insert_trade", "add_trade"), self.db_path, ticker, side, quantity, price, user_id)
        except AttributeError:
            return super().add_trade(ticker, side, quantity, price, user_id)

    def list_snapshots(self, user_id: str = DEFAULT_USER_ID) -> list[dict[str, Any]]:
        try:
            return self._call(("list_portfolio_snapshots", "list_snapshots"), self.db_path, user_id)
        except AttributeError:
            return super().list_snapshots(user_id)

    def add_snapshot(self, total_value: float, user_id: str = DEFAULT_USER_ID) -> dict[str, Any]:
        try:
            return self._call(
                ("insert_portfolio_snapshot", "add_portfolio_snapshot"),
                self.db_path,
                total_value,
                user_id,
            )
        except AttributeError:
            return super().add_snapshot(total_value, user_id)

    def list_chat_messages(self, limit: int = 20, user_id: str = DEFAULT_USER_ID) -> list[dict[str, Any]]:
        try:
            messages = self._call(("list_chat_messages",), self.db_path, user_id)
            return messages[-limit:] if limit else messages
        except AttributeError:
            return super().list_chat_messages(limit, user_id)

    def add_chat_message(
        self,
        role: str,
        content: str,
        actions: dict[str, Any] | None = None,
        user_id: str = DEFAULT_USER_ID,
    ) -> dict[str, Any]:
        try:
            return self._call(
                ("insert_chat_message", "add_chat_message"),
                self.db_path,
                role,
                content,
                actions,
                user_id,
            )
        except AttributeError:
            return super().add_chat_message(role, content, actions, user_id)


def create_repository(db_path: Path | None = None) -> Repository:
    try:
        from app import db as db_module  # type: ignore
    except ImportError:
        return LocalSQLiteRepository(db_path)
    return DbFunctionRepository(db_module, db_path)


@dataclass
class FinAllyService:
    repository: Repository
    price_cache: PriceCache
    market_source: MarketDataSource | None = None

    @property
    def max_order_value(self) -> float:
        return float(os.environ.get("FINALLY_MAX_ORDER_VALUE", "1000000"))

    @property
    def max_position_concentration(self) -> float:
        return float(os.environ.get("FINALLY_MAX_POSITION_CONCENTRATION", "1.0"))

    def initialize(self) -> None:
        self.repository.init_db()

    async def tracked_tickers(self) -> list[str]:
        watchlist = self.repository.list_watchlist()
        positions = [position["ticker"] for position in self.repository.list_positions()]
        return normalize_tickers([*watchlist, *positions])

    def _price_update(self, ticker: str):
        update = self.price_cache.get(ticker)
        if update is not None:
            return update
        seed = SEED_PRICES.get(ticker, 100.0)
        return self.price_cache.update(ticker, seed)

    def _watchlist_item(self, ticker: str) -> dict[str, Any]:
        update = self._price_update(ticker)
        return {
            "ticker": update.ticker,
            "current_price": update.price,
            "previous_price": update.previous_price,
            "change_percent": update.change_percent,
            "direction": update.direction,
            "timestamp": iso_from_unix(update.timestamp),
        }

    async def add_market_ticker(self, ticker: str) -> None:
        if self.market_source is not None:
            await self.market_source.add_ticker(ticker)

    async def maybe_remove_market_ticker(self, ticker: str) -> None:
        if self.market_source is None:
            return
        tracked = set(await self.tracked_tickers())
        if ticker not in tracked:
            await self.market_source.remove_ticker(ticker)

    async def get_watchlist(self) -> dict[str, Any]:
        tickers = self.repository.list_watchlist()
        return {"items": [self._watchlist_item(ticker) for ticker in tickers]}

    async def add_watchlist(self, ticker: str) -> dict[str, Any]:
        ticker = validate_ticker(ticker)
        added = self.repository.add_watchlist(ticker)
        self._price_update(ticker)
        await self.add_market_ticker(ticker)
        return {"item": self._watchlist_item(ticker), "already_exists": not added}

    async def remove_watchlist(self, ticker: str) -> dict[str, Any]:
        ticker = validate_ticker(ticker)
        removed = self.repository.remove_watchlist(ticker)
        await self.maybe_remove_market_ticker(ticker)
        return {"ticker": ticker, "removed": removed}

    def get_portfolio(self) -> dict[str, Any]:
        profile = self.repository.get_profile()
        cash = float(profile["cash_balance"])
        positions = []
        positions_value = 0.0
        unrealized = 0.0

        for position in self.repository.list_positions():
            ticker = validate_ticker(position["ticker"])
            quantity = float(position["quantity"])
            avg_cost = float(position["avg_cost"])
            current_price = float(self._price_update(ticker).price)
            market_value = quantity * current_price
            pnl = (current_price - avg_cost) * quantity
            change_percent = 0.0 if avg_cost == 0 else (current_price - avg_cost) / avg_cost * 100
            positions_value += market_value
            unrealized += pnl
            positions.append(
                {
                    "ticker": ticker,
                    "quantity": quantity,
                    "avg_cost": round_money(avg_cost),
                    "current_price": round_money(current_price),
                    "market_value": round_money(market_value),
                    "unrealized_pnl": round_money(pnl),
                    "change_percent": round(change_percent, 4),
                }
            )

        return {
            "cash_balance": round_money(cash),
            "total_value": round_money(cash + positions_value),
            "unrealized_pnl": round_money(unrealized),
            "positions": positions,
        }

    def get_history(self) -> dict[str, Any]:
        snapshots = self.repository.list_snapshots()
        return {
            "snapshots": [
                {"total_value": round_money(row["total_value"]), "recorded_at": row["recorded_at"]}
                for row in snapshots
            ]
        }

    def _validate_trade(self, ticker: str, side: str, quantity: float) -> tuple[str, str, float]:
        ticker = validate_ticker(ticker)
        side = side.lower().strip() if isinstance(side, str) else side
        if side not in {"buy", "sell"}:
            raise ServiceError("invalid_trade_side", {"side": side, "supported": ["buy", "sell"]})
        try:
            quantity = float(quantity)
        except (TypeError, ValueError) as exc:
            raise ServiceError("invalid_quantity", {"quantity": quantity}) from exc
        if quantity <= 0:
            raise ServiceError("invalid_quantity", {"quantity": quantity, "minimum": 0})
        return ticker, side, quantity

    def _check_position_concentration(self, ticker: str, order_value: float, quantity: float) -> None:
        limit = self.max_position_concentration
        if limit >= 1.0:
            return
        portfolio = self.get_portfolio()
        total_after = portfolio["total_value"]
        existing = next((item for item in portfolio["positions"] if item["ticker"] == ticker), None)
        current_value = existing["market_value"] if existing else 0.0
        concentration = (current_value + order_value) / total_after if total_after else 0.0
        if concentration > limit:
            raise ServiceError(
                "position_concentration_exceeded",
                {"ticker": ticker, "concentration": round(concentration, 4), "limit": limit},
            )

    async def execute_trade(self, ticker: str, side: str, quantity: float) -> dict[str, Any]:
        ticker, side, quantity = self._validate_trade(ticker, side, quantity)
        price = float(self._price_update(ticker).price)
        order_value = price * quantity
        if order_value > self.max_order_value:
            raise ServiceError(
                "order_value_exceeded",
                {"order_value": round_money(order_value), "limit": self.max_order_value},
            )

        profile = self.repository.get_profile()
        cash = float(profile["cash_balance"])
        existing = self.repository.get_position(ticker)

        if side == "buy":
            if cash + 1e-9 < order_value:
                raise ServiceError(
                    "insufficient_cash",
                    {"cash_balance": round_money(cash), "required": round_money(order_value)},
                )
            self._check_position_concentration(ticker, order_value, quantity)
            old_quantity = float(existing["quantity"]) if existing else 0.0
            old_avg_cost = float(existing["avg_cost"]) if existing else 0.0
            new_quantity = old_quantity + quantity
            new_avg_cost = ((old_quantity * old_avg_cost) + order_value) / new_quantity
            self.repository.set_cash_balance(cash - order_value)
            self.repository.upsert_position(ticker, new_quantity, new_avg_cost)
        else:
            if existing is None or float(existing["quantity"]) + 1e-9 < quantity:
                raise ServiceError(
                    "insufficient_shares",
                    {
                        "ticker": ticker,
                        "available": round(float(existing["quantity"]), 8) if existing else 0.0,
                        "requested": quantity,
                    },
                )
            old_quantity = float(existing["quantity"])
            new_quantity = old_quantity - quantity
            self.repository.set_cash_balance(cash + order_value)
            if new_quantity <= 1e-9:
                self.repository.delete_position(ticker)
            else:
                self.repository.upsert_position(ticker, new_quantity, float(existing["avg_cost"]))

        await self.add_market_ticker(ticker)
        trade = self.repository.add_trade(ticker, side, quantity, price)
        portfolio = self.get_portfolio()
        self.repository.add_snapshot(portfolio["total_value"])
        return {"trade": _public_trade(trade), "portfolio": portfolio}

    async def chat(self, message: str) -> dict[str, Any]:
        message = message.strip()
        if not message:
            raise ServiceError("empty_message", {})

        self.repository.add_chat_message("user", message)
        plan = await self._generate_chat_plan(message)
        response_message = str(plan.get("message") or "")
        requested_trades = plan.get("trades") or []
        requested_watchlist = plan.get("watchlist_changes") or []
        trades: list[dict[str, Any]] = []
        watchlist_changes: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []

        for raw in requested_trades:
            try:
                result = await self.execute_trade(raw["ticker"], raw["side"], raw["quantity"])
                trades.append(result["trade"])
            except (KeyError, ServiceError) as exc:
                errors.append(_action_error("trade", raw, exc))

        for raw in requested_watchlist:
            try:
                action = str(raw["action"]).lower().strip()
                if action == "add":
                    result = await self.add_watchlist(raw["ticker"])
                elif action in {"remove", "delete"}:
                    result = await self.remove_watchlist(raw["ticker"])
                else:
                    raise ServiceError("invalid_watchlist_action", {"action": action})
                watchlist_changes.append({"action": action, **result})
            except (KeyError, ServiceError) as exc:
                errors.append(_action_error("watchlist", raw, exc))

        response = {
            "message": response_message,
            "trades": trades,
            "watchlist_changes": watchlist_changes,
            "errors": errors,
        }
        self.repository.add_chat_message("assistant", response_message, actions=response)
        return response

    async def _generate_chat_plan(self, message: str) -> dict[str, Any]:
        try:
            from app.llm import generate_chat_plan  # type: ignore
        except ImportError:
            return {"message": f"Mock response: {message}", "trades": [], "watchlist_changes": []}

        context = {
            "portfolio": self.get_portfolio(),
            "watchlist": await self.get_watchlist(),
            "history": self.repository.list_chat_messages(limit=20),
        }
        try:
            params = inspect.signature(generate_chat_plan).parameters
            if "context" in params:
                result = generate_chat_plan(message=message, context=context)
            else:
                result = generate_chat_plan(message, context["portfolio"], context["history"])
            if inspect.isawaitable(result):
                result = await result
        except Exception as exc:
            raise ServiceError("llm_error", {"message": str(exc)}, status_code=502) from exc
        if hasattr(result, "model_dump"):
            result = result.model_dump()
        elif hasattr(result, "to_dict"):
            result = result.to_dict()
        if not isinstance(result, dict):
            raise ServiceError("invalid_llm_response", {"type": type(result).__name__}, status_code=502)
        return result


def _public_trade(trade: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": trade["id"],
        "ticker": trade["ticker"],
        "side": trade["side"],
        "quantity": float(trade["quantity"]),
        "price": round_money(trade["price"]),
        "executed_at": trade["executed_at"],
    }


def _action_error(action_type: str, raw: Any, exc: Exception) -> dict[str, Any]:
    if isinstance(exc, ServiceError):
        payload = exc.to_dict()
    else:
        payload = {"error": "invalid_action", "details": {"message": str(exc)}}
    return {"action_type": action_type, "action": raw, **payload}

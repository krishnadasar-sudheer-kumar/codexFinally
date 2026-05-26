"""SQLite persistence helpers for FinAlly.

The module intentionally keeps the API small and dependency-free. Each public
function accepts a database path, lazily initializes the schema and seed data,
and returns plain dictionaries that are easy for FastAPI handlers to serialize.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

DEFAULT_USER_ID = "default"
DEFAULT_CASH_BALANCE = 100000.0
DEFAULT_WATCHLIST = ("AAPL", "GOOGL", "MSFT", "AMZN", "TSLA", "NVDA", "META", "JPM", "V", "NFLX")


def utc_now_iso() -> str:
    """Return the current UTC time as an ISO 8601 string."""
    return datetime.now(UTC).isoformat()


def get_connection(db_path: str | Path) -> sqlite3.Connection:
    """Return an initialized SQLite connection with rows addressable by name."""
    init_db(db_path)
    connection = _connect(db_path)
    connection.row_factory = sqlite3.Row
    return connection


def init_db(db_path: str | Path) -> None:
    """Create missing tables and seed the default single-user data."""
    _ensure_parent_directory(db_path)
    with _connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS users_profile (
                id TEXT PRIMARY KEY DEFAULT 'default',
                cash_balance REAL NOT NULL DEFAULT 100000.0,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS watchlist (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL DEFAULT 'default',
                ticker TEXT NOT NULL,
                added_at TEXT NOT NULL,
                UNIQUE (user_id, ticker)
            );

            CREATE TABLE IF NOT EXISTS positions (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL DEFAULT 'default',
                ticker TEXT NOT NULL,
                quantity REAL NOT NULL,
                avg_cost REAL NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE (user_id, ticker)
            );

            CREATE TABLE IF NOT EXISTS trades (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL DEFAULT 'default',
                ticker TEXT NOT NULL,
                side TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
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
                role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
                content TEXT NOT NULL,
                actions TEXT,
                created_at TEXT NOT NULL
            );
            """
        )
        _seed_default_data(connection)


def get_user_profile(db_path: str | Path, user_id: str = DEFAULT_USER_ID) -> dict[str, Any] | None:
    """Return the user profile for ``user_id``."""
    with get_connection(db_path) as connection:
        row = connection.execute(
            "SELECT id, cash_balance, created_at FROM users_profile WHERE id = ?",
            (user_id,),
        ).fetchone()
        return _row_to_dict(row)


def update_cash_balance(
    db_path: str | Path, cash_balance: float, user_id: str = DEFAULT_USER_ID
) -> dict[str, Any]:
    """Set a user's cash balance and return the profile."""
    created_at = utc_now_iso()
    with get_connection(db_path) as connection:
        connection.execute(
            """
            INSERT INTO users_profile (id, cash_balance, created_at)
            VALUES (?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET cash_balance = excluded.cash_balance
            """,
            (user_id, cash_balance, created_at),
        )
    profile = get_user_profile(db_path, user_id)
    if profile is None:  # pragma: no cover - defensive guard for SQLite failures
        raise RuntimeError(f"Unable to update profile for user {user_id!r}")
    return profile


def list_watchlist(db_path: str | Path, user_id: str = DEFAULT_USER_ID) -> list[dict[str, Any]]:
    """Return watchlist entries ordered by insertion time."""
    with get_connection(db_path) as connection:
        rows = connection.execute(
            """
            SELECT id, user_id, ticker, added_at
            FROM watchlist
            WHERE user_id = ?
            ORDER BY added_at, rowid
            """,
            (user_id,),
        ).fetchall()
        return _rows_to_dicts(rows)


def add_watchlist_ticker(
    db_path: str | Path, ticker: str, user_id: str = DEFAULT_USER_ID
) -> dict[str, Any]:
    """Add ``ticker`` to the watchlist if absent and return its row."""
    normalized_ticker = _normalize_ticker(ticker)
    added_at = utc_now_iso()
    with get_connection(db_path) as connection:
        connection.execute(
            """
            INSERT INTO watchlist (id, user_id, ticker, added_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id, ticker) DO NOTHING
            """,
            (_new_id(), user_id, normalized_ticker, added_at),
        )
        row = connection.execute(
            """
            SELECT id, user_id, ticker, added_at
            FROM watchlist
            WHERE user_id = ? AND ticker = ?
            """,
            (user_id, normalized_ticker),
        ).fetchone()
        result = _row_to_dict(row)
    if result is None:  # pragma: no cover - defensive guard for SQLite failures
        raise RuntimeError(f"Unable to add watchlist ticker {normalized_ticker!r}")
    return result


def remove_watchlist_ticker(
    db_path: str | Path, ticker: str, user_id: str = DEFAULT_USER_ID
) -> bool:
    """Remove ``ticker`` from the watchlist and return whether a row was deleted."""
    normalized_ticker = _normalize_ticker(ticker)
    with get_connection(db_path) as connection:
        cursor = connection.execute(
            "DELETE FROM watchlist WHERE user_id = ? AND ticker = ?",
            (user_id, normalized_ticker),
        )
        return cursor.rowcount > 0


def list_positions(db_path: str | Path, user_id: str = DEFAULT_USER_ID) -> list[dict[str, Any]]:
    """Return all current positions for ``user_id`` ordered by ticker."""
    with get_connection(db_path) as connection:
        rows = connection.execute(
            """
            SELECT id, user_id, ticker, quantity, avg_cost, updated_at
            FROM positions
            WHERE user_id = ?
            ORDER BY ticker
            """,
            (user_id,),
        ).fetchall()
        return _rows_to_dicts(rows)


def get_position(
    db_path: str | Path, ticker: str, user_id: str = DEFAULT_USER_ID
) -> dict[str, Any] | None:
    """Return the current position for ``ticker``."""
    normalized_ticker = _normalize_ticker(ticker)
    with get_connection(db_path) as connection:
        row = connection.execute(
            """
            SELECT id, user_id, ticker, quantity, avg_cost, updated_at
            FROM positions
            WHERE user_id = ? AND ticker = ?
            """,
            (user_id, normalized_ticker),
        ).fetchone()
        return _row_to_dict(row)


def upsert_position(
    db_path: str | Path,
    ticker: str,
    quantity: float,
    avg_cost: float,
    user_id: str = DEFAULT_USER_ID,
) -> dict[str, Any]:
    """Create or replace the user's current position for ``ticker``."""
    normalized_ticker = _normalize_ticker(ticker)
    updated_at = utc_now_iso()
    with get_connection(db_path) as connection:
        connection.execute(
            """
            INSERT INTO positions (id, user_id, ticker, quantity, avg_cost, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, ticker) DO UPDATE SET
                quantity = excluded.quantity,
                avg_cost = excluded.avg_cost,
                updated_at = excluded.updated_at
            """,
            (_new_id(), user_id, normalized_ticker, quantity, avg_cost, updated_at),
        )
    position = get_position(db_path, normalized_ticker, user_id)
    if position is None:  # pragma: no cover - defensive guard for SQLite failures
        raise RuntimeError(f"Unable to upsert position {normalized_ticker!r}")
    return position


def delete_position(db_path: str | Path, ticker: str, user_id: str = DEFAULT_USER_ID) -> bool:
    """Delete the current position for ``ticker`` and return whether it existed."""
    normalized_ticker = _normalize_ticker(ticker)
    with get_connection(db_path) as connection:
        cursor = connection.execute(
            "DELETE FROM positions WHERE user_id = ? AND ticker = ?",
            (user_id, normalized_ticker),
        )
        return cursor.rowcount > 0


def insert_trade(
    db_path: str | Path,
    ticker: str,
    side: str,
    quantity: float,
    price: float,
    user_id: str = DEFAULT_USER_ID,
    executed_at: str | None = None,
) -> dict[str, Any]:
    """Insert an append-only trade row and return it."""
    normalized_ticker = _normalize_ticker(ticker)
    normalized_side = side.strip().lower()
    if normalized_side not in {"buy", "sell"}:
        raise ValueError("side must be 'buy' or 'sell'")

    trade_id = _new_id()
    timestamp = executed_at or utc_now_iso()
    with get_connection(db_path) as connection:
        connection.execute(
            """
            INSERT INTO trades (id, user_id, ticker, side, quantity, price, executed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (trade_id, user_id, normalized_ticker, normalized_side, quantity, price, timestamp),
        )
        row = connection.execute(
            """
            SELECT id, user_id, ticker, side, quantity, price, executed_at
            FROM trades
            WHERE id = ?
            """,
            (trade_id,),
        ).fetchone()
        result = _row_to_dict(row)
    if result is None:  # pragma: no cover - defensive guard for SQLite failures
        raise RuntimeError("Unable to insert trade")
    return result


def list_trades(db_path: str | Path, user_id: str = DEFAULT_USER_ID) -> list[dict[str, Any]]:
    """Return trade history ordered by execution time."""
    with get_connection(db_path) as connection:
        rows = connection.execute(
            """
            SELECT id, user_id, ticker, side, quantity, price, executed_at
            FROM trades
            WHERE user_id = ?
            ORDER BY executed_at, id
            """,
            (user_id,),
        ).fetchall()
        return _rows_to_dicts(rows)


def insert_portfolio_snapshot(
    db_path: str | Path,
    total_value: float,
    user_id: str = DEFAULT_USER_ID,
    recorded_at: str | None = None,
) -> dict[str, Any]:
    """Insert a portfolio value snapshot and return it."""
    snapshot_id = _new_id()
    timestamp = recorded_at or utc_now_iso()
    with get_connection(db_path) as connection:
        connection.execute(
            """
            INSERT INTO portfolio_snapshots (id, user_id, total_value, recorded_at)
            VALUES (?, ?, ?, ?)
            """,
            (snapshot_id, user_id, total_value, timestamp),
        )
        row = connection.execute(
            """
            SELECT id, user_id, total_value, recorded_at
            FROM portfolio_snapshots
            WHERE id = ?
            """,
            (snapshot_id,),
        ).fetchone()
        result = _row_to_dict(row)
    if result is None:  # pragma: no cover - defensive guard for SQLite failures
        raise RuntimeError("Unable to insert portfolio snapshot")
    return result


def list_portfolio_snapshots(
    db_path: str | Path, user_id: str = DEFAULT_USER_ID
) -> list[dict[str, Any]]:
    """Return portfolio snapshots ordered by recording time."""
    with get_connection(db_path) as connection:
        rows = connection.execute(
            """
            SELECT id, user_id, total_value, recorded_at
            FROM portfolio_snapshots
            WHERE user_id = ?
            ORDER BY recorded_at, id
            """,
            (user_id,),
        ).fetchall()
        return _rows_to_dicts(rows)


def insert_chat_message(
    db_path: str | Path,
    role: str,
    content: str,
    actions: Any | None = None,
    user_id: str = DEFAULT_USER_ID,
    created_at: str | None = None,
) -> dict[str, Any]:
    """Insert a chat message and return it.

    ``actions`` may be a JSON string or any JSON-serializable Python value.
    """
    normalized_role = role.strip().lower()
    if normalized_role not in {"user", "assistant"}:
        raise ValueError("role must be 'user' or 'assistant'")

    message_id = _new_id()
    timestamp = created_at or utc_now_iso()
    actions_json = _serialize_actions(actions)
    with get_connection(db_path) as connection:
        connection.execute(
            """
            INSERT INTO chat_messages (id, user_id, role, content, actions, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (message_id, user_id, normalized_role, content, actions_json, timestamp),
        )
        row = connection.execute(
            """
            SELECT id, user_id, role, content, actions, created_at
            FROM chat_messages
            WHERE id = ?
            """,
            (message_id,),
        ).fetchone()
        result = _row_to_dict(row)
    if result is None:  # pragma: no cover - defensive guard for SQLite failures
        raise RuntimeError("Unable to insert chat message")
    return result


def list_chat_messages(
    db_path: str | Path, user_id: str = DEFAULT_USER_ID
) -> list[dict[str, Any]]:
    """Return chat history ordered by creation time."""
    with get_connection(db_path) as connection:
        rows = connection.execute(
            """
            SELECT id, user_id, role, content, actions, created_at
            FROM chat_messages
            WHERE user_id = ?
            ORDER BY created_at, id
            """,
            (user_id,),
        ).fetchall()
        return _rows_to_dicts(rows)


def _connect(db_path: str | Path) -> sqlite3.Connection:
    return sqlite3.connect(str(db_path))


def _ensure_parent_directory(db_path: str | Path) -> None:
    path = Path(db_path)
    if path.parent != Path("."):
        path.parent.mkdir(parents=True, exist_ok=True)


def _seed_default_data(connection: sqlite3.Connection) -> None:
    now = utc_now_iso()
    connection.execute(
        """
        INSERT INTO users_profile (id, cash_balance, created_at)
        VALUES (?, ?, ?)
        ON CONFLICT(id) DO NOTHING
        """,
        (DEFAULT_USER_ID, DEFAULT_CASH_BALANCE, now),
    )
    connection.executemany(
        """
        INSERT INTO watchlist (id, user_id, ticker, added_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id, ticker) DO NOTHING
        """,
        [(_new_id(), DEFAULT_USER_ID, ticker, now) for ticker in DEFAULT_WATCHLIST],
    )
    snapshot_count = connection.execute(
        "SELECT COUNT(*) AS count FROM portfolio_snapshots WHERE user_id = ?",
        (DEFAULT_USER_ID,),
    ).fetchone()["count"]
    if snapshot_count == 0:
        connection.execute(
            """
            INSERT INTO portfolio_snapshots (id, user_id, total_value, recorded_at)
            VALUES (?, ?, ?, ?)
            """,
            (_new_id(), DEFAULT_USER_ID, DEFAULT_CASH_BALANCE, now),
        )


def _new_id() -> str:
    return str(uuid4())


def _normalize_ticker(ticker: str) -> str:
    normalized = ticker.strip().upper()
    if not normalized:
        raise ValueError("ticker must not be empty")
    return normalized


def _serialize_actions(actions: Any | None) -> str | None:
    if actions is None:
        return None
    if isinstance(actions, str):
        return actions
    return json.dumps(actions, separators=(",", ":"), sort_keys=True)


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return dict(row)


def _rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]

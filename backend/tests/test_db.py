"""Tests for the SQLite persistence layer."""

from datetime import UTC, datetime

import pytest

from app.db import (
    DEFAULT_CASH_BALANCE,
    DEFAULT_USER_ID,
    DEFAULT_WATCHLIST,
    add_watchlist_ticker,
    delete_position,
    get_connection,
    get_position,
    get_user_profile,
    init_db,
    insert_chat_message,
    insert_portfolio_snapshot,
    insert_trade,
    list_chat_messages,
    list_portfolio_snapshots,
    list_positions,
    list_trades,
    list_watchlist,
    remove_watchlist_ticker,
    update_cash_balance,
    upsert_position,
)


def test_init_db_creates_seed_data_idempotently(tmp_path):
    """Initialization creates default rows once, even when called repeatedly."""
    db_path = tmp_path / "finally.sqlite3"

    init_db(db_path)
    init_db(db_path)

    profile = get_user_profile(db_path)
    assert profile is not None
    assert profile["id"] == DEFAULT_USER_ID
    assert profile["cash_balance"] == DEFAULT_CASH_BALANCE
    assert_is_utc_iso(profile["created_at"])

    watchlist = list_watchlist(db_path)
    assert [entry["ticker"] for entry in watchlist] == list(DEFAULT_WATCHLIST)

    snapshots = list_portfolio_snapshots(db_path)
    assert len(snapshots) == 1
    assert snapshots[0]["total_value"] == DEFAULT_CASH_BALANCE


def test_get_connection_lazily_initializes_schema(tmp_path):
    """Direct connection access still returns an initialized database."""
    db_path = tmp_path / "nested" / "finally.sqlite3"

    with get_connection(db_path) as connection:
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }

    assert {
        "users_profile",
        "watchlist",
        "positions",
        "trades",
        "portfolio_snapshots",
        "chat_messages",
    }.issubset(tables)


def test_watchlist_add_remove_and_normalization(tmp_path):
    """Watchlist writes normalize tickers and avoid duplicate rows."""
    db_path = tmp_path / "finally.sqlite3"

    entry = add_watchlist_ticker(db_path, " ibm ")
    duplicate = add_watchlist_ticker(db_path, "IBM")

    assert entry["ticker"] == "IBM"
    assert duplicate["id"] == entry["id"]
    assert [row["ticker"] for row in list_watchlist(db_path)].count("IBM") == 1
    assert remove_watchlist_ticker(db_path, " ibm ") is True
    assert remove_watchlist_ticker(db_path, "IBM") is False


def test_positions_can_be_upserted_listed_and_deleted(tmp_path):
    """Position helpers provide current holding CRUD behavior."""
    db_path = tmp_path / "finally.sqlite3"

    created = upsert_position(db_path, "aapl", 3.5, 180.25)
    updated = upsert_position(db_path, "AAPL", 4.0, 181.0)

    assert updated["id"] == created["id"]
    assert updated["ticker"] == "AAPL"
    assert updated["quantity"] == 4.0
    assert updated["avg_cost"] == 181.0
    assert_is_utc_iso(updated["updated_at"])
    assert list_positions(db_path) == [updated]
    assert get_position(db_path, " aapl ") == updated
    assert delete_position(db_path, "AAPL") is True
    assert get_position(db_path, "AAPL") is None
    assert delete_position(db_path, "AAPL") is False


def test_cash_balance_update_upserts_profile(tmp_path):
    """Cash balance updates return the latest profile row."""
    db_path = tmp_path / "finally.sqlite3"

    profile = update_cash_balance(db_path, 87500.25)

    assert profile["id"] == DEFAULT_USER_ID
    assert profile["cash_balance"] == 87500.25


def test_trades_are_append_only_and_validate_side(tmp_path):
    """Trade history appends rows in execution order."""
    db_path = tmp_path / "finally.sqlite3"

    first = insert_trade(db_path, "msft", "BUY", 2.0, 320.0, executed_at="2026-01-01T00:00:00+00:00")
    second = insert_trade(db_path, "MSFT", "sell", 1.0, 330.0, executed_at="2026-01-02T00:00:00+00:00")

    assert first["ticker"] == "MSFT"
    assert first["side"] == "buy"
    assert list_trades(db_path) == [first, second]
    with pytest.raises(ValueError, match="side"):
        insert_trade(db_path, "MSFT", "hold", 1.0, 330.0)


def test_portfolio_snapshots_are_inserted_and_listed(tmp_path):
    """Snapshot helpers include seeded and newly inserted values."""
    db_path = tmp_path / "finally.sqlite3"

    inserted = insert_portfolio_snapshot(
        db_path,
        100500.75,
        recorded_at="2026-01-01T00:00:00+00:00",
    )

    snapshots = list_portfolio_snapshots(db_path)
    assert len(snapshots) == 2
    assert inserted in snapshots


def test_chat_messages_store_actions_json_and_validate_role(tmp_path):
    """Chat history stores user and assistant messages in order."""
    db_path = tmp_path / "finally.sqlite3"

    user_message = insert_chat_message(db_path, "user", "Buy AAPL")
    assistant_message = insert_chat_message(
        db_path,
        "assistant",
        "Bought AAPL",
        actions={"trades": [{"ticker": "AAPL", "side": "buy"}]},
    )

    assert user_message["actions"] is None
    assert assistant_message["actions"] == '{"trades":[{"side":"buy","ticker":"AAPL"}]}'
    assert list_chat_messages(db_path) == [user_message, assistant_message]
    with pytest.raises(ValueError, match="role"):
        insert_chat_message(db_path, "system", "Nope")


def test_empty_tickers_are_rejected(tmp_path):
    """Ticker-taking helpers reject empty ticker strings."""
    db_path = tmp_path / "finally.sqlite3"

    with pytest.raises(ValueError, match="ticker"):
        add_watchlist_ticker(db_path, " ")


def assert_is_utc_iso(value: str) -> None:
    """Assert a timestamp can be parsed and carries a UTC offset."""
    parsed = datetime.fromisoformat(value)
    assert parsed.tzinfo == UTC

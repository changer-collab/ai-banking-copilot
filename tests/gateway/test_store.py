import sqlite3
from pathlib import Path

import pytest

from ai_banking_copilot.gateway.errors import ReadOnlyViolation, StoreError
from ai_banking_copilot.gateway.store import (
    SCHEMA_VERSION,
    Store,
    StoreMode,
    init_schema,
)

EXPECTED_TABLES = {
    "schema_meta",
    "users",
    "accounts",
    "payees",
    "cards",
    "transactions",
    "wealth_products",
    "subscriptions",
    "holds",
    "write_receipts",
}


def test_init_schema_creates_all_tables(tmp_path: Path) -> None:
    store = Store(tmp_path / "demo.db")
    init_schema(store)
    names = {
        row["name"] for row in store.query("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert EXPECTED_TABLES <= names
    store.close()


def test_init_schema_records_version_and_is_idempotent(tmp_path: Path) -> None:
    store = Store(tmp_path / "demo.db")
    init_schema(store)
    init_schema(store)
    rows = store.query("SELECT value FROM schema_meta WHERE key = 'schema_version'")
    assert rows[0]["value"] == str(SCHEMA_VERSION)
    store.close()


def test_query_rejects_write_sql(tmp_path: Path) -> None:
    store = Store(tmp_path / "demo.db")
    init_schema(store)
    with pytest.raises(ReadOnlyViolation, match="只读"):
        store.query("DELETE FROM accounts")
    store.close()


def test_read_only_store_rejects_write(tmp_path: Path) -> None:
    db_path = tmp_path / "demo.db"
    writer = Store(db_path)
    init_schema(writer)
    writer.close()

    reader = Store(db_path, StoreMode.READ_ONLY)
    assert reader.mode is StoreMode.READ_ONLY
    with pytest.raises(ReadOnlyViolation):
        reader.execute(
            "INSERT INTO users(user_id, display_name, risk_profile) VALUES('u1', '甲', 'R1')"
        )
    reader.close()


def test_read_only_store_reads_committed_data(tmp_path: Path) -> None:
    db_path = tmp_path / "demo.db"
    writer = Store(db_path)
    init_schema(writer)
    with writer.transaction() as conn:
        conn.execute(
            "INSERT INTO users(user_id, display_name, risk_profile) VALUES('u1', '甲', 'R1')"
        )
    writer.close()

    reader = Store(db_path, StoreMode.READ_ONLY)
    rows = reader.query("SELECT display_name FROM users WHERE user_id = ?", ("u1",))
    assert rows[0]["display_name"] == "甲"
    reader.close()


def test_transaction_rolls_back_on_error(tmp_path: Path) -> None:
    store = Store(tmp_path / "demo.db")
    init_schema(store)
    with pytest.raises(sqlite3.IntegrityError):
        with store.transaction() as conn:
            conn.execute(
                "INSERT INTO users(user_id, display_name, risk_profile) VALUES('u1', '甲', 'R1')"
            )
            conn.execute(
                "INSERT INTO users(user_id, display_name, risk_profile) VALUES('u1', '乙', 'R2')"
            )
    assert store.query("SELECT user_id FROM users") == []
    store.close()


def test_read_only_store_requires_existing_file(tmp_path: Path) -> None:
    with pytest.raises(StoreError, match="不存在"):
        Store(tmp_path / "missing.db", StoreMode.READ_ONLY)

import sqlite3
from concurrent.futures import ThreadPoolExecutor
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


def test_init_schema_migrates_v2_transactions_to_v3_idempotently(tmp_path: Path) -> None:
    db_path = tmp_path / "v2.db"
    connection = sqlite3.connect(db_path)
    connection.executescript(
        """
        CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO schema_meta(key, value) VALUES('schema_version', '2');
        CREATE TABLE transactions (
            txn_id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            account_id TEXT NOT NULL,
            booked_on TEXT NOT NULL,
            amount_cents INTEGER NOT NULL,
            merchant TEXT NOT NULL,
            category TEXT NOT NULL,
            channel TEXT NOT NULL,
            direction TEXT NOT NULL,
            booked_at TEXT,
            city TEXT
        );
        INSERT INTO transactions VALUES(
            'txn_old', 'u_old', 'acc_old', '2026-09-18', -2500,
            '视频会员', '娱乐', 'app', 'out', '2026-09-18T20:30:00', '杭州'
        );
        """
    )
    connection.commit()
    connection.close()

    store = Store(db_path)
    init_schema(store)
    init_schema(store)
    columns = {
        row["name"]: row["type"]
        for row in store.query("SELECT name, type FROM pragma_table_info('transactions')")
    }
    old_row = store.query(
        "SELECT txn_id, booked_at, city, card_tail FROM transactions WHERE txn_id = 'txn_old'"
    )[0]
    version = store.query(
        "SELECT value FROM schema_meta WHERE key = 'schema_version'"
    )[0]["value"]

    assert columns["card_tail"] == "TEXT"
    assert version == str(SCHEMA_VERSION)
    assert dict(old_row) == {
        "txn_id": "txn_old",
        "booked_at": "2026-09-18T20:30:00",
        "city": "杭州",
        "card_tail": None,
    }
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


def test_concurrent_reads_and_writes_are_serialized(tmp_path: Path) -> None:
    """页面一次加载会并发命中多个只读接口；同一条 SQLite 连接必须串行使用。

    回归的是现场实测到的 `sqlite3.InterfaceError: bad parameter or other API misuse`：
    `check_same_thread=False` 只关断言，不提供并发保护。
    """
    store = Store(tmp_path / "concurrent.db")
    init_schema(store)

    def read_meta(_: int) -> str:
        return str(
            store.query("SELECT value FROM schema_meta WHERE key = 'schema_version'")[0]["value"]
        )

    def write_user(index: int) -> None:
        with store.transaction() as conn:
            conn.execute(
                "INSERT INTO users(user_id, display_name, risk_profile) VALUES(?, ?, 'R1')",
                (f"u{index}", f"用户{index}"),
            )

    with ThreadPoolExecutor(max_workers=16) as pool:
        versions = list(pool.map(read_meta, range(400)))
        list(pool.map(write_user, range(32)))

    assert set(versions) == {str(SCHEMA_VERSION)}
    assert len(store.query("SELECT user_id FROM users")) == 32
    store.close()

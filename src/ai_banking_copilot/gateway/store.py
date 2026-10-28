"""SQLite 存储层：事实网关（TCB）的物理落点。

两条硬约束（PRD §5.2「TCB 三约束」的前两条落点）：
1. **写权限独占**：`StoreMode.READ_ONLY` 除了 Python 层拦截，还开启
   `PRAGMA query_only = ON`，由 SQLite 自身兜底拒绝写。
2. **读数出处可校验**：读取一律走 `query()`，只放行 `SELECT` / `WITH`。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from enum import StrEnum
from pathlib import Path

from ai_banking_copilot.gateway.errors import ReadOnlyViolation, StoreError

SCHEMA_VERSION = 3

_TRANSACTIONS_OPTIONAL_COLUMNS = (
    ("booked_at", "TEXT"),
    ("city", "TEXT"),
    ("card_tail", "TEXT"),
)
"""`transactions` 的**可空**扩展列（v3）。

判据层（如账单「深夜 / 异地」）必须能跳过 `NULL`——旧库升级后历史行为 `NULL`。
"""

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    user_id      TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    risk_profile TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS accounts (
    account_id    TEXT PRIMARY KEY,
    user_id       TEXT NOT NULL REFERENCES users(user_id),
    kind          TEXT NOT NULL CHECK (kind IN ('savings', 'current')),
    balance_cents INTEGER NOT NULL CHECK (balance_cents >= 0),
    currency      TEXT NOT NULL DEFAULT 'CNY'
);

CREATE TABLE IF NOT EXISTS payees (
    payee_id           TEXT PRIMARY KEY,
    user_id            TEXT NOT NULL REFERENCES users(user_id),
    full_name          TEXT NOT NULL,
    account_tail       TEXT NOT NULL,
    phone_tail         TEXT NOT NULL,
    identity_confirmed INTEGER NOT NULL CHECK (identity_confirmed IN (0, 1)),
    blocked            INTEGER NOT NULL CHECK (blocked IN (0, 1))
);

CREATE TABLE IF NOT EXISTS cards (
    card_id           TEXT PRIMARY KEY,
    user_id           TEXT NOT NULL REFERENCES users(user_id),
    card_type         TEXT NOT NULL CHECK (card_type IN ('debit', 'credit', 'virtual')),
    status            TEXT NOT NULL CHECK (status IN ('active', 'lost', 'locked')),
    daily_limit_cents INTEGER NOT NULL CHECK (daily_limit_cents >= 0)
);

CREATE TABLE IF NOT EXISTS transactions (
    txn_id       TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL REFERENCES users(user_id),
    account_id   TEXT NOT NULL REFERENCES accounts(account_id),
    booked_on    TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    merchant     TEXT NOT NULL,
    category     TEXT NOT NULL,
    channel      TEXT NOT NULL,
    direction    TEXT NOT NULL CHECK (direction IN ('in', 'out')),
    booked_at    TEXT,
    city         TEXT,
    card_tail    TEXT
);

CREATE TABLE IF NOT EXISTS wealth_products (
    product_id       TEXT PRIMARY KEY,
    name             TEXT NOT NULL,
    risk_level       TEXT NOT NULL CHECK (risk_level IN ('R1', 'R2', 'R3', 'R4', 'R5')),
    term_days        INTEGER NOT NULL CHECK (term_days >= 0),
    yield_bp         INTEGER NOT NULL,
    min_amount_cents INTEGER NOT NULL CHECK (min_amount_cents > 0)
);

CREATE TABLE IF NOT EXISTS wealth_holdings (
    product_id   TEXT NOT NULL REFERENCES wealth_products(product_id),
    user_id      TEXT NOT NULL REFERENCES users(user_id),
    amount_cents INTEGER NOT NULL CHECK (amount_cents >= 0),
    purchased_on TEXT NOT NULL,
    PRIMARY KEY (product_id, user_id)
);

CREATE TABLE IF NOT EXISTS subscriptions (
    subscription_id TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL REFERENCES users(user_id),
    merchant        TEXT NOT NULL,
    amount_cents    INTEGER NOT NULL CHECK (amount_cents > 0),
    period          TEXT NOT NULL CHECK (period IN ('monthly', 'yearly')),
    next_charge_on  TEXT NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('active', 'cancelled'))
);

CREATE TABLE IF NOT EXISTS holds (
    hold_id       TEXT PRIMARY KEY,
    account_id    TEXT NOT NULL REFERENCES accounts(account_id),
    amount_cents  INTEGER NOT NULL CHECK (amount_cents > 0),
    reason        TEXT NOT NULL,
    status        TEXT NOT NULL CHECK (status IN ('active', 'released'))
);

CREATE TABLE IF NOT EXISTS write_receipts (
    idempotency_key TEXT PRIMARY KEY,
    action          TEXT NOT NULL,
    receipt_json    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_txn_user_date ON transactions(user_id, booked_on);
"""

_READ_ONLY_ROOTS = ("SELECT", "WITH")


class StoreMode(StrEnum):
    READ_ONLY = "ro"
    READ_WRITE = "rw"


def init_schema(store: Store) -> None:
    """建表并登记 schema 版本（幂等，含旧库平滑升级）。"""
    with store.transaction() as conn:
        conn.executescript(SCHEMA_SQL)
        _migrate_schema(conn)
        conn.execute(
            "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(SCHEMA_VERSION),),
        )


def _migrate_schema(conn: sqlite3.Connection) -> None:
    """v1 / v2 → v3 幂等迁移：给 `transactions` 补可空列。

    `CREATE TABLE IF NOT EXISTS` 不会给已存在的表加列，故旧库必须显式 `ALTER TABLE`；
    先查 `PRAGMA table_info` 再补，重复执行零副作用。
    """
    existing = {str(row["name"]) for row in conn.execute("PRAGMA table_info(transactions)")}
    for column, column_type in _TRANSACTIONS_OPTIONAL_COLUMNS:
        if column not in existing:
            conn.execute(f"ALTER TABLE transactions ADD COLUMN {column} {column_type}")


class Store:
    """SQLite 连接包装。**唯一的物理写入通道**。"""

    def __init__(self, db_path: str | Path, mode: StoreMode = StoreMode.READ_WRITE) -> None:
        self._path = Path(db_path)
        self._mode = mode
        if mode is StoreMode.READ_ONLY:
            if not self._path.is_file():
                raise StoreError(f"只读打开失败：文件不存在 {self._path}")
            self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
            self._conn.execute("PRAGMA query_only = ON")
        else:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")

    @property
    def mode(self) -> StoreMode:
        return self._mode

    @property
    def path(self) -> Path:
        return self._path

    def query(self, sql: str, params: Sequence[object] = ()) -> list[sqlite3.Row]:
        """只读查询：仅放行 SELECT / WITH。"""
        _assert_read_only_sql(sql)
        cursor = self._conn.execute(sql, tuple(params))
        rows: list[sqlite3.Row] = cursor.fetchall()
        return rows

    def execute(self, sql: str, params: Sequence[object] = ()) -> None:
        """单条写语句（调用方负责提交或使用 `transaction()`）。"""
        if self._mode is StoreMode.READ_ONLY:
            raise ReadOnlyViolation("只读连接不得执行写语句")
        self._conn.execute(sql, tuple(params))

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """事务：正常退出提交，异常退出回滚。"""
        if self._mode is StoreMode.READ_ONLY:
            raise ReadOnlyViolation("只读连接不能开启事务")
        with self._conn:
            yield self._conn

    def close(self) -> None:
        self._conn.close()


def _assert_read_only_sql(sql: str) -> None:
    head = sql.lstrip().split(None, 1)[0].upper() if sql.strip() else ""
    if head not in _READ_ONLY_ROOTS:
        raise ReadOnlyViolation(f"query() 只接受只读语句，收到：{head or sql!r}")

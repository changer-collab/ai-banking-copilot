"""转账线独立持久化：AA **收款**请求与定时转账**登记**（**不改** 01 的 `SCHEMA_SQL`）。

口径（`docs/archive/plans/11-p2-scenario-coverage.md` §2.4）：
- `collection_requests`：AA **收款**请求（待收款，**不产生资金流**）；
- `scheduled_transfers`：未来转账**登记**（**登记时不扣款**；真正扣款由到期派发后的
  `transfer.create` 承担）；
- 两表均为**独立模块自带 DDL + 幂等 init**（照 `gateway/risk_store.py` 先例），
  严禁并入 `gateway/store.py::SCHEMA_SQL`；
- 回读走 `FactGateway`（按主键重建 `Record` 并重算哈希），供 H1 出处校验。

**建表时机**：`init_transfer_schema` 是幂等 bootstrap（组合根 / 测试夹具可显式调用）；
`CoreBank` 的写原语在事务内**再执行一次** `CREATE TABLE IF NOT EXISTS`（同一 DDL 常量），
使任何未显式 bootstrap 的调用方（如隔离评测库）也不会因缺表而失败——两处同源、互不冲突。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from ai_banking_copilot.gateway.hashing import canonical_json, sha256_hex
from ai_banking_copilot.gateway.models import Money
from ai_banking_copilot.gateway.store import Store

COLLECTION_REQUESTS_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS collection_requests (
    request_id   TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL REFERENCES users(user_id),
    payer_id     TEXT NOT NULL,
    amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
    note         TEXT NOT NULL,
    status       TEXT NOT NULL,
    created_on   TEXT NOT NULL
);
"""
"""AA 收款请求表（独立 DDL；**不并入** 01 的 `SCHEMA_SQL`）。"""

SCHEDULED_TRANSFERS_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS scheduled_transfers (
    schedule_id  TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL REFERENCES users(user_id),
    account_id   TEXT NOT NULL,
    payee_id     TEXT NOT NULL,
    amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
    booked_on    TEXT NOT NULL,
    remark       TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL
);
"""
"""定时转账登记表（独立 DDL；登记不扣款，仅记未来动作）。"""


@dataclass(frozen=True)
class CollectionRequest:
    """一条 AA 收款请求的事实快照（`collection_requests` 行）。"""

    request_id: str
    user_id: str
    payer_id: str
    amount: Money
    note: str
    status: str
    created_on: date


@dataclass(frozen=True)
class ScheduledTransfer:
    """一条定时转账登记的事实快照（`scheduled_transfers` 行）。"""

    schedule_id: str
    user_id: str
    account_id: str
    payee_id: str
    amount: Money
    booked_on: date
    remark: str
    status: str


def due_execution_key(action: str, user_id: str, reference_id: str) -> str:
    """到期业务身份跨会话保持不变，复用既有 write_receipts 持久去重。"""
    digest = sha256_hex(canonical_json((user_id, reference_id)))
    return f"due:{action}:{digest}"


def init_transfer_schema(store: Store) -> None:
    """建转账线两张表（幂等；既有库自动补表）。"""
    with store.transaction() as conn:
        conn.execute(COLLECTION_REQUESTS_SCHEMA_SQL)
        conn.execute(SCHEDULED_TRANSFERS_SCHEMA_SQL)

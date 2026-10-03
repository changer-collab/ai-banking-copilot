"""风险测评事实：独立持久化模块（**不改** 01 的 `SCHEMA_SQL`）。

口径（`docs/plans/README.md:183`「各自独立的表/文件」）：
- `risk_assessments` 是**唯一运行期判定源**；`users.risk_profile` 仅 legacy，运行期不再读取；
- 写只发生在 `seed_risk_assessment`（幂等 `INSERT OR IGNORE`），运行期只读；
- 每条事实带出处：`Record` + `verify_risk`（**回读重算哈希**，比哈希而非比文本）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Protocol, runtime_checkable

from ai_banking_copilot.gateway.core_bank import DEFAULT_BOOKING_DATE
from ai_banking_copilot.gateway.facts import Record
from ai_banking_copilot.gateway.hashing import canonical_json, sha256_hex
from ai_banking_copilot.gateway.seed import DEMO_USER_ID
from ai_banking_copilot.gateway.store import Store

RISK_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS risk_assessments (
    user_id      TEXT PRIMARY KEY REFERENCES users(user_id),
    risk_profile TEXT NOT NULL CHECK (risk_profile IN ('R1', 'R2', 'R3', 'R4', 'R5')),
    assessed_on  TEXT NOT NULL,
    valid_days   INTEGER NOT NULL CHECK (valid_days > 0)
);
"""
"""风险测评表（独立 DDL；不并入 01 的 `SCHEMA_SQL`）。"""

_TABLE = "risk_assessments"
_DEMO_ASSESSED_ON = (DEFAULT_BOOKING_DATE - timedelta(days=30)).isoformat()


@dataclass(frozen=True)
class RiskAssessmentRecord:
    """一次风险测评的事实快照（`risk_assessments` 行）。"""

    user_id: str
    risk_profile: str
    assessed_on: date
    valid_days: int


@runtime_checkable
class RiskAssessmentSource(Protocol):
    """风险测评事实源协议（06/07 受信任调用方消费；快照对上层不透明）。"""

    def assessment_for(self, user_id: str) -> object | None:
        """按用户取测评事实快照；缺测评返回 `None`（调用方据此 fail-closed）。"""
        ...


class StoreRiskAssessmentSource:
    """`RiskAssessmentSource` 的默认实现：只读 store 读取测评事实。"""

    def __init__(self, store: Store) -> None:
        self._store = store

    def assessment_for(self, user_id: str) -> object | None:
        record = risk_assessment(self._store, user_id)
        return None if record is None else record.payload


def init_risk_schema(store: Store) -> None:
    """建风险测评表（幂等；既有库自动补表）。"""
    with store.transaction() as conn:
        conn.executescript(RISK_SCHEMA_SQL)


def seed_risk_assessment(store: Store) -> None:
    """幂等播种演示测评行（**不依赖 `SEEDED_KEY`**，既有库亦补行）。"""
    with store.transaction() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO risk_assessments(user_id, risk_profile, assessed_on,"
            " valid_days) VALUES(?, ?, ?, ?)",
            (DEMO_USER_ID, "R3", _DEMO_ASSESSED_ON, 365),
        )


def risk_assessment(store: Store, user_id: str) -> Record[RiskAssessmentRecord] | None:
    """按用户读取测评事实（缺行→`None`；只读）。"""
    rows = store.query(
        "SELECT user_id, risk_profile, assessed_on, valid_days FROM risk_assessments"
        " WHERE user_id = ?",
        (user_id,),
    )
    if not rows:
        return None
    row = rows[0]
    payload = RiskAssessmentRecord(
        user_id=str(row["user_id"]),
        risk_profile=str(row["risk_profile"]),
        assessed_on=date.fromisoformat(str(row["assessed_on"])),
        valid_days=int(row["valid_days"]),
    )
    return Record(
        record_id=f"{_TABLE}:{user_id}",
        payload=payload,
        content_hash=sha256_hex(canonical_json(payload)),
    )


def verify_risk(store: Store, record_id: str, content_hash: str) -> bool:
    """出处校验：按 `record_id` 回读 store 行**重算哈希**再比对（比哈希而非比文本）。"""
    table, _, pk = record_id.partition(":")
    if table != _TABLE or not pk:
        return False
    current = risk_assessment(store, pk)
    if current is None:
        return False
    return current.content_hash == content_hash

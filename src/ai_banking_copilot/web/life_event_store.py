"""生活事件记录：把「识别到的事件」落成可回看的一条流水。

字段只记录**发生了什么**，不复制计划卡内容：`event_id / event_name` 来自
`scenarios.life_events.LIFE_EVENT_PROTOTYPES`，金额取表单参数（不反推、不估算）。

状态机：`planned`（已提交）→ `executed` / `rejected` / `failed`。
同一会话内同一事件重复提交只更新最近一条 `planned`，不产生重复行。
"""

from __future__ import annotations

import secrets
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from ai_banking_copilot.gateway.store import Store

LIFE_EVENT_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS life_event_records (
    record_id    TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL,
    session_id   TEXT NOT NULL,
    event_id     TEXT NOT NULL,
    event_name   TEXT NOT NULL,
    amount_cents INTEGER,
    status       TEXT NOT NULL
                 CHECK (status IN ('planned', 'executed', 'rejected', 'failed')),
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_life_event_user
    ON life_event_records(user_id, created_at);
"""

STATUS_PLANNED = "planned"
STATUS_EXECUTED = "executed"
STATUS_REJECTED = "rejected"
STATUS_FAILED = "failed"

STATUSES = (STATUS_PLANNED, STATUS_EXECUTED, STATUS_REJECTED, STATUS_FAILED)


@dataclass(frozen=True)
class LifeEventRecord:
    """一条生活事件流水。"""

    record_id: str
    user_id: str
    session_id: str
    event_id: str
    event_name: str
    amount_cents: int | None
    status: str
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, object]:
        return {
            "record_id": self.record_id,
            "session_id": self.session_id,
            "event_id": self.event_id,
            "event_name": self.event_name,
            "amount_cents": self.amount_cents,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class LifeEventStore:
    """生活事件流水读写。"""

    def __init__(self, store: Store, *, clock: Callable[[], datetime] | None = None) -> None:
        self._store = store
        self._clock = clock or (lambda: datetime.now(UTC))

    def create(
        self,
        *,
        user_id: str,
        session_id: str,
        event_id: str,
        event_name: str,
        amount_cents: int | None = None,
        now: datetime | None = None,
    ) -> LifeEventRecord:
        """登记一次「已提交」；同会话同事件已有 `planned` 行则就地更新。"""

        moment = (now if now is not None else self._clock()).isoformat()
        rows = self._store.query(
            "SELECT record_id FROM life_event_records"
            " WHERE user_id = ? AND session_id = ? AND event_id = ? AND status = ?"
            " ORDER BY created_at DESC LIMIT 1",
            (user_id, session_id, event_id, STATUS_PLANNED),
        )
        if rows:
            record_id = str(rows[0]["record_id"])
            with self._store.transaction() as conn:
                conn.execute(
                    "UPDATE life_event_records SET event_name = ?, amount_cents = ?,"
                    " updated_at = ? WHERE record_id = ?",
                    (event_name, amount_cents, moment, record_id),
                )
        else:
            record_id = f"le_{secrets.token_hex(8)}"
            with self._store.transaction() as conn:
                conn.execute(
                    "INSERT INTO life_event_records"
                    " (record_id, user_id, session_id, event_id, event_name, amount_cents,"
                    "  status, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        record_id,
                        user_id,
                        session_id,
                        event_id,
                        event_name,
                        amount_cents,
                        STATUS_PLANNED,
                        moment,
                        moment,
                    ),
                )
        return self._get(user_id, record_id)

    def mark(
        self,
        *,
        user_id: str,
        session_id: str,
        status: str,
        now: datetime | None = None,
    ) -> int:
        """把该会话最近一条 `planned` 置为终态；返回受影响行数。"""

        if status not in STATUSES or status == STATUS_PLANNED:
            raise ValueError(f"非法终态：{status}")
        moment = (now if now is not None else self._clock()).isoformat()
        rows = self._store.query(
            "SELECT record_id FROM life_event_records"
            " WHERE user_id = ? AND session_id = ? AND status = ?"
            " ORDER BY created_at DESC LIMIT 1",
            (user_id, session_id, STATUS_PLANNED),
        )
        if not rows:
            return 0
        with self._store.transaction() as conn:
            cursor = conn.execute(
                "UPDATE life_event_records SET status = ?, updated_at = ? WHERE record_id = ?",
                (status, moment, str(rows[0]["record_id"])),
            )
            return cursor.rowcount

    def list_for_user(self, user_id: str, *, limit: int = 5) -> tuple[LifeEventRecord, ...]:
        rows = self._store.query(
            "SELECT record_id, user_id, session_id, event_id, event_name, amount_cents,"
            "  status, created_at, updated_at FROM life_event_records"
            " WHERE user_id = ? ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (user_id, limit),
        )
        return tuple(_record_from_row(row) for row in rows)

    def _get(self, user_id: str, record_id: str) -> LifeEventRecord:
        rows = self._store.query(
            "SELECT record_id, user_id, session_id, event_id, event_name, amount_cents,"
            "  status, created_at, updated_at FROM life_event_records"
            " WHERE user_id = ? AND record_id = ?",
            (user_id, record_id),
        )
        return _record_from_row(rows[0])


def init_life_event_schema(store: Store) -> None:
    """建生活事件表（幂等）：组合根在写连接上调用一次即可。"""

    with store.transaction() as conn:
        conn.executescript(LIFE_EVENT_SCHEMA_SQL)


def _record_from_row(row: sqlite3.Row) -> LifeEventRecord:
    amount = row["amount_cents"]
    return LifeEventRecord(
        record_id=str(row["record_id"]),
        user_id=str(row["user_id"]),
        session_id=str(row["session_id"]),
        event_id=str(row["event_id"]),
        event_name=str(row["event_name"]),
        amount_cents=None if amount is None else int(amount),
        status=str(row["status"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )

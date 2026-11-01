"""生活事件记录：把「识别到的事件」落成可回看的一条流水。

字段只记录**发生了什么**，不复制计划卡内容：`event_id / event_name` 来自
`scenarios.life_events.LIFE_EVENT_PROTOTYPES`（类型），`event_title` 是用户那句话
整理后的**具体事件**（金额已抽到 `amount_cents`，不重复写在标题里）。

状态机：`planned`（已提交）→ `executed` / `rejected` / `failed`。
同一会话内同一事件重复提交只更新最近一条 `planned`，不产生重复行。

到期预约（`life_event_schedules`）是另一条独立流水：未来日期的事件在**确认时只登记预约**，
不冻结也不下单；到期后由 `/api/due` 重新生成待确认提案，再确认一次才真正执行。
状态机：`scheduled` → `consumed`（已执行） / `cancelled`（已放弃） /
`failed`（执行失败，不再自动重提）。
"""

from __future__ import annotations

import json
import secrets
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime

from ai_banking_copilot.gateway.store import Store

LIFE_EVENT_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS life_event_records (
    record_id    TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL,
    session_id   TEXT NOT NULL,
    event_id     TEXT NOT NULL,
    event_name   TEXT NOT NULL,
    event_title  TEXT NOT NULL DEFAULT '',
    amount_cents INTEGER,
    status       TEXT NOT NULL
                 CHECK (status IN ('planned', 'executed', 'rejected', 'failed')),
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    steps_json   TEXT NOT NULL DEFAULT '[]',
    due_on       TEXT
);

CREATE INDEX IF NOT EXISTS idx_life_event_user
    ON life_event_records(user_id, created_at);

CREATE TABLE IF NOT EXISTS life_event_schedules (
    schedule_id  TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL,
    session_id   TEXT NOT NULL,
    record_id    TEXT,
    event_id     TEXT NOT NULL,
    event_title  TEXT NOT NULL DEFAULT '',
    plan_id      TEXT NOT NULL DEFAULT '',
    plan_json    TEXT NOT NULL,
    due_on       TEXT NOT NULL,
    status       TEXT NOT NULL
                 CHECK (status IN ('scheduled', 'consumed', 'cancelled', 'failed')),
    consumed_at  TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_life_schedule_due
    ON life_event_schedules(user_id, status, due_on);
"""

STATUS_PLANNED = "planned"
STATUS_EXECUTED = "executed"
STATUS_REJECTED = "rejected"
STATUS_FAILED = "failed"

STATUSES = (STATUS_PLANNED, STATUS_EXECUTED, STATUS_REJECTED, STATUS_FAILED)

SCHEDULE_SCHEDULED = "scheduled"
SCHEDULE_CONSUMED = "consumed"
SCHEDULE_CANCELLED = "cancelled"
SCHEDULE_FAILED = "failed"

SCHEDULE_STATUSES = (
    SCHEDULE_SCHEDULED, SCHEDULE_CONSUMED, SCHEDULE_CANCELLED, SCHEDULE_FAILED,
)

_RECORD_STATUS_BY_SCHEDULE = {
    SCHEDULE_CONSUMED: STATUS_EXECUTED,
    SCHEDULE_CANCELLED: STATUS_REJECTED,
    SCHEDULE_FAILED: STATUS_FAILED,
}


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
    event_title: str = ""
    """具体事件（用户原话整理后的短标题）；旧记录为空时前端回落到类型名。"""
    steps: tuple[dict[str, object], ...] = ()
    """计划步骤的只读摘要（动作 / 金额 / 日期）；状态由记录整体状态派生。"""
    due_on: str | None = None
    """计划生效 / 送达日期（ISO）；为空表示未指定。"""

    def to_dict(self) -> dict[str, object]:
        return {
            "record_id": self.record_id,
            "session_id": self.session_id,
            "event_id": self.event_id,
            "event_name": self.event_name,
            "event_title": self.event_title,
            "amount_cents": self.amount_cents,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "steps": list(self.steps),
            "due_on": self.due_on,
        }


@dataclass(frozen=True)
class LifeEventSchedule:
    """一条到期预约：确认时只登记，到期后重新提案，执行成功才 `consumed`。"""

    schedule_id: str
    user_id: str
    session_id: str
    event_id: str
    event_title: str
    plan_id: str
    nodes: tuple[dict[str, object], ...]
    """确认时冻结的节点快照：`{node_id, action, params, depends_on}`。"""
    due_on: date
    status: str
    created_at: str
    updated_at: str
    record_id: str | None = None
    consumed_at: str | None = None


class LifeEventStore:
    """生活事件流水与到期预约读写。"""

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
        event_title: str = "",
        amount_cents: int | None = None,
        steps: Sequence[Mapping[str, object]] = (),
        due_on: str | None = None,
        now: datetime | None = None,
    ) -> LifeEventRecord:
        """登记一次「已提交」；同会话同事件已有 `planned` 行则就地更新。"""

        moment = (now if now is not None else self._clock()).isoformat()
        steps_json = json.dumps(list(steps), ensure_ascii=False)
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
                    "UPDATE life_event_records SET event_name = ?, event_title = ?,"
                    " amount_cents = ?, steps_json = ?, due_on = ?, updated_at = ?"
                    " WHERE record_id = ?",
                    (
                        event_name, event_title, amount_cents, steps_json, due_on,
                        moment, record_id,
                    ),
                )
        else:
            record_id = f"le_{secrets.token_hex(8)}"
            with self._store.transaction() as conn:
                conn.execute(
                    "INSERT INTO life_event_records"
                    " (record_id, user_id, session_id, event_id, event_name, event_title,"
                    "  amount_cents, status, created_at, updated_at, steps_json, due_on)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        record_id,
                        user_id,
                        session_id,
                        event_id,
                        event_name,
                        event_title,
                        amount_cents,
                        STATUS_PLANNED,
                        moment,
                        moment,
                        steps_json,
                        due_on,
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
            "SELECT record_id, user_id, session_id, event_id, event_name, event_title,"
            "  amount_cents, status, created_at, updated_at, steps_json, due_on"
            " FROM life_event_records"
            " WHERE user_id = ? ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (user_id, limit),
        )
        return tuple(_record_from_row(row) for row in rows)

    def _get(self, user_id: str, record_id: str) -> LifeEventRecord:
        rows = self._store.query(
            "SELECT record_id, user_id, session_id, event_id, event_name, event_title,"
            "  amount_cents, status, created_at, updated_at, steps_json, due_on"
            " FROM life_event_records"
            " WHERE user_id = ? AND record_id = ?",
            (user_id, record_id),
        )
        return _record_from_row(rows[0])

    def latest_planned(self, *, user_id: str, session_id: str) -> LifeEventRecord | None:
        """该会话最近一条 `planned` 记录；没有则 `None`（用于把预约挂到来源记录）。"""
        rows = self._store.query(
            "SELECT record_id, user_id, session_id, event_id, event_name, event_title,"
            "  amount_cents, status, created_at, updated_at, steps_json, due_on"
            " FROM life_event_records"
            " WHERE user_id = ? AND session_id = ? AND status = ?"
            " ORDER BY created_at DESC LIMIT 1",
            (user_id, session_id, STATUS_PLANNED),
        )
        return None if not rows else _record_from_row(rows[0])

    def schedule(
        self,
        *,
        user_id: str,
        session_id: str,
        event_id: str,
        event_title: str,
        due_on: date,
        plan_id: str,
        nodes: Sequence[Mapping[str, object]],
        record_id: str | None = None,
        now: datetime | None = None,
    ) -> LifeEventSchedule:
        """登记（或就地更新）一条到期预约；同会话同事件同到期日只保留一条 `scheduled`。"""

        moment = (now if now is not None else self._clock()).isoformat()
        plan_json = json.dumps(
            {"plan_id": plan_id, "nodes": [dict(node) for node in nodes]},
            ensure_ascii=False, sort_keys=True, default=str,
        )
        due_text = due_on.isoformat()
        rows = self._store.query(
            "SELECT schedule_id FROM life_event_schedules"
            " WHERE user_id = ? AND session_id = ? AND event_id = ? AND due_on = ? AND status = ?"
            " ORDER BY created_at DESC LIMIT 1",
            (user_id, session_id, event_id, due_text, SCHEDULE_SCHEDULED),
        )
        if rows:
            schedule_id = str(rows[0]["schedule_id"])
            with self._store.transaction() as conn:
                conn.execute(
                    "UPDATE life_event_schedules SET event_title = ?, record_id = ?,"
                    " plan_id = ?, plan_json = ?, updated_at = ?"
                    " WHERE schedule_id = ?",
                    (event_title, record_id, plan_id, plan_json, moment, schedule_id),
                )
        else:
            schedule_id = f"ls_{secrets.token_hex(8)}"
            with self._store.transaction() as conn:
                conn.execute(
                    "INSERT INTO life_event_schedules"
                    " (schedule_id, user_id, session_id, record_id, event_id, event_title,"
                    "  plan_id, plan_json, due_on, status, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        schedule_id, user_id, session_id, record_id, event_id, event_title,
                        plan_id, plan_json, due_text, SCHEDULE_SCHEDULED, moment, moment,
                    ),
                )
        return self._get_schedule(user_id, schedule_id)

    def due_schedules(self, user_id: str, *, today: date) -> tuple[LifeEventSchedule, ...]:
        """到期且仍为 `scheduled` 的预约：只提案，不代表已执行。"""

        rows = self._store.query(
            "SELECT schedule_id FROM life_event_schedules"
            " WHERE user_id = ? AND status = ? AND due_on <= ?"
            " ORDER BY due_on ASC, created_at ASC",
            (user_id, SCHEDULE_SCHEDULED, today.isoformat()),
        )
        return tuple(self._get_schedule(user_id, str(row["schedule_id"])) for row in rows)

    def finish_schedule(
        self,
        *,
        user_id: str,
        schedule_id: str,
        status: str,
        now: datetime | None = None,
    ) -> int:
        """把预约置为终态，并同步来源记录；返回受影响行数（幂等：非 `scheduled` 不再改）。"""

        if status not in (SCHEDULE_CONSUMED, SCHEDULE_CANCELLED, SCHEDULE_FAILED):
            raise ValueError(f"非法预约终态：{status}")
        moment = (now if now is not None else self._clock()).isoformat()
        rows = self._store.query(
            "SELECT record_id FROM life_event_schedules"
            " WHERE user_id = ? AND schedule_id = ? AND status = ?",
            (user_id, schedule_id, SCHEDULE_SCHEDULED),
        )
        if not rows:
            return 0
        record_id = rows[0]["record_id"]
        with self._store.transaction() as conn:
            cursor = conn.execute(
                "UPDATE life_event_schedules SET status = ?, consumed_at = ?, updated_at = ?"
                " WHERE user_id = ? AND schedule_id = ? AND status = ?",
                (status, moment, moment, user_id, schedule_id, SCHEDULE_SCHEDULED),
            )
            if cursor.rowcount and record_id is not None:
                conn.execute(
                    "UPDATE life_event_records SET status = ?, updated_at = ?"
                    " WHERE user_id = ? AND record_id = ?",
                    (_RECORD_STATUS_BY_SCHEDULE[status], moment, user_id, str(record_id)),
                )
            return cursor.rowcount

    def _get_schedule(self, user_id: str, schedule_id: str) -> LifeEventSchedule:
        rows = self._store.query(
            "SELECT schedule_id, user_id, session_id, record_id, event_id, event_title,"
            "  plan_id, plan_json, due_on, status, consumed_at, created_at, updated_at"
            " FROM life_event_schedules"
            " WHERE user_id = ? AND schedule_id = ?",
            (user_id, schedule_id),
        )
        return _schedule_from_row(rows[0])


def init_life_event_schema(store: Store) -> None:
    """建生活事件表（幂等）：组合根在写连接上调用一次即可。

    老库没有 `event_title` 列：补一条 `ALTER TABLE`，历史行留空串由前端回落到类型名。
    """

    with store.transaction() as conn:
        conn.executescript(LIFE_EVENT_SCHEMA_SQL)
        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(life_event_records)")}
        if "event_title" not in columns:
            conn.execute(
                "ALTER TABLE life_event_records ADD COLUMN event_title TEXT NOT NULL DEFAULT ''"
            )
        if "steps_json" not in columns:
            conn.execute(
                "ALTER TABLE life_event_records ADD COLUMN steps_json TEXT NOT NULL DEFAULT '[]'"
            )
        if "due_on" not in columns:
            conn.execute("ALTER TABLE life_event_records ADD COLUMN due_on TEXT")


def _record_from_row(row: sqlite3.Row) -> LifeEventRecord:
    amount = row["amount_cents"]
    raw_steps = row["steps_json"] if "steps_json" in row.keys() else "[]"
    try:
        parsed = json.loads(str(raw_steps or "[]"))
    except ValueError:
        parsed = []
    steps = tuple(
        item for item in parsed if isinstance(item, dict)
    ) if isinstance(parsed, list) else ()
    due_on = row["due_on"] if "due_on" in row.keys() else None
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
        event_title=str(row["event_title"] or ""),
        steps=steps,
        due_on=None if due_on is None else str(due_on),
    )


def _schedule_from_row(row: sqlite3.Row) -> LifeEventSchedule:
    try:
        parsed = json.loads(str(row["plan_json"] or "{}"))
    except ValueError:
        parsed = {}
    raw_nodes = parsed.get("nodes") if isinstance(parsed, dict) else None
    nodes: tuple[dict[str, object], ...] = ()
    if isinstance(raw_nodes, list):
        nodes = tuple(item for item in raw_nodes if isinstance(item, dict))
    plan_id = str(parsed.get("plan_id") or "") if isinstance(parsed, dict) else ""
    record_id = row["record_id"] if "record_id" in row.keys() else None
    consumed_at = row["consumed_at"] if "consumed_at" in row.keys() else None
    return LifeEventSchedule(
        schedule_id=str(row["schedule_id"]),
        user_id=str(row["user_id"]),
        session_id=str(row["session_id"]),
        event_id=str(row["event_id"]),
        event_title=str(row["event_title"] or ""),
        plan_id=plan_id or str(row["plan_id"] or ""),
        nodes=nodes,
        due_on=date.fromisoformat(str(row["due_on"])),
        status=str(row["status"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        record_id=None if record_id is None else str(record_id),
        consumed_at=None if consumed_at is None else str(consumed_at),
    )

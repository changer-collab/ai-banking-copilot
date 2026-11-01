"""风险测评事实：独立持久化模块（**不改** 01 的 `SCHEMA_SQL`）。

口径（`docs/plans/README.md:183`「各自独立的表/文件」）：
- `risk_assessments` 是**唯一运行期判定源**；`users.risk_profile` 仅 legacy，运行期不再读取；
- `source` 区分事实来源：`seed` = 演示播种（**不是真实测评**）、`user_input` = 用户自助录入；
- 运行期只读走 `RiskAssessmentSource`；真实录入/更新走写连接上的
  `save_risk_assessment`（`source='user_input'`），由 `RiskAssessmentWriter` 协议暴露；
- 每条事实带出处：`Record` + `verify_risk`（**回读重算哈希**，比哈希而非比文本）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Protocol, runtime_checkable
from uuid import uuid4

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
    valid_days   INTEGER NOT NULL CHECK (valid_days > 0),
    source       TEXT NOT NULL DEFAULT 'seed'
);
"""
"""风险测评表（独立 DDL；不并入 01 的 `SCHEMA_SQL`）。"""

RISK_ATTEMPTS_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS risk_assessment_attempts (
    user_id  TEXT NOT NULL,
    on_date  TEXT NOT NULL,
    attempts INTEGER NOT NULL CHECK (attempts >= 0),
    PRIMARY KEY (user_id, on_date)
);
"""
"""测评**提交次数**表（独立 DDL）。

为什么要它：等级可以被反复重测覆盖，但一个演示日里无限重测既不像真实产品、
也让「等级是服务端算出来的事实」变得可疑。次数只记「今天提交了几次问卷」，
不参与任何适当性判定。
"""

RISK_EVENTS_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS risk_assessment_events (
    event_id         TEXT PRIMARY KEY,
    user_id          TEXT NOT NULL,
    event_on         TEXT NOT NULL,
    risk_profile     TEXT NOT NULL,
    score            INTEGER NOT NULL,
    source           TEXT NOT NULL,
    previous_profile TEXT
);
"""
"""测评**事件**表：用于 12 个月累计与「结果与最近一次不一致」的再次确认。"""

RETAKES_PER_DAY = 2
"""同一用户每个演示业务日最多提交几次问卷。

依据《金融机构产品适当性管理办法》第二十九条：单日不得超过两次，
十二个月内累计不得超过八次。演示环境按**模拟业务日**计数，界面必须同时写明。
"""

RETAKES_PER_12M = 8
"""同一用户十二个月内累计最多提交几次问卷（第二十九条）。"""

_RISK_PROFILES = ("R1", "R2", "R3", "R4", "R5")
_TABLE = "risk_assessments"
_DEMO_ASSESSED_ON = (DEFAULT_BOOKING_DATE - timedelta(days=30)).isoformat()

SOURCE_SEED = "seed"
SOURCE_USER_INPUT = "user_input"
SOURCE_QUESTIONNAIRE = "questionnaire"
"""用户通过固定题库作答得到的等级（等级由 `scenarios.wealth_assessment` 算出，自身不能填）。"""

_WRITABLE_SOURCES = (SOURCE_USER_INPUT, SOURCE_QUESTIONNAIRE)


@dataclass(frozen=True)
class RiskAssessmentRecord:
    """一次风险测评的事实快照（`risk_assessments` 行）。"""

    user_id: str
    risk_profile: str
    assessed_on: date
    valid_days: int
    source: str = SOURCE_SEED


@runtime_checkable
class RiskAssessmentSource(Protocol):
    """风险测评事实源协议（06/07 受信任调用方消费；快照对上层不透明）。"""

    def assessment_for(self, user_id: str) -> object | None:
        """按用户取测评事实快照；缺测评返回 `None`（调用方据此 fail-closed）。"""
        ...


@runtime_checkable
class RiskAssessmentWriter(Protocol):
    """风险测评**写入**协议：只写用户侧来源（自助录入 / 问卷），永不写 `seed`。

    另带**当日提交次数**读写：次数只用于「每日最多 3 次」这一产品口径，
    不参与任何适当性判定，也不影响已生效的等级事实。
    """

    def save(
        self,
        user_id: str,
        risk_profile: str,
        assessed_on: date,
        *,
        source: str = SOURCE_USER_INPUT,
    ) -> None:
        """保存/更新一次用户侧测评（覆盖同一用户的旧事实）。"""
        ...

    def attempts_on(self, user_id: str, on_date: date) -> int:
        """读某个业务日已提交几次问卷（缺行 = 0）。"""
        ...

    def record_attempt(self, user_id: str, on_date: date) -> int:
        """记一次问卷提交并返回当日累计次数。"""
        ...

    def events_count_since(self, user_id: str, since: date) -> int:
        """读最近一段时间的**已提交**测评次数（12 个月上限用）。"""
        ...

    def earliest_event_on(self, user_id: str, since: date) -> date | None:
        """读最近一段时间最早一次测评日期（算 12 个月下次可测日）。"""
        ...

    def record_event(
        self,
        user_id: str,
        event_on: date,
        risk_profile: str,
        score: int,
        *,
        source: str,
        previous_profile: str | None = None,
    ) -> None:
        """记一次**已确认提交**的测评事件。"""
        ...


class StoreRiskAssessmentSource:
    """`RiskAssessmentSource` 的默认实现：只读 store 读取测评事实。"""

    def __init__(self, store: Store) -> None:
        self._store = store

    def assessment_for(self, user_id: str) -> object | None:
        record = risk_assessment(self._store, user_id)
        return None if record is None else record.payload


class StoreRiskAssessmentWriter:
    """`RiskAssessmentWriter` 的默认实现：写连接上 UPSERT 用户侧测评事实。"""

    def __init__(self, store: Store) -> None:
        self._store = store

    def save(
        self,
        user_id: str,
        risk_profile: str,
        assessed_on: date,
        *,
        source: str = SOURCE_USER_INPUT,
    ) -> None:
        save_risk_assessment(self._store, user_id, risk_profile, assessed_on, source=source)

    def attempts_on(self, user_id: str, on_date: date) -> int:
        return attempts_on(self._store, user_id, on_date)

    def record_attempt(self, user_id: str, on_date: date) -> int:
        return record_attempt(self._store, user_id, on_date)

    def events_count_since(self, user_id: str, since: date) -> int:
        return assessment_event_count_since(self._store, user_id, since)

    def earliest_event_on(self, user_id: str, since: date) -> date | None:
        return earliest_assessment_event_on(self._store, user_id, since)

    def record_event(
        self,
        user_id: str,
        event_on: date,
        risk_profile: str,
        score: int,
        *,
        source: str,
        previous_profile: str | None = None,
    ) -> None:
        record_assessment_event(
            self._store,
            user_id,
            event_on,
            risk_profile,
            score,
            source=source,
            previous_profile=previous_profile,
        )


def init_risk_schema(store: Store) -> None:
    """建风险测评表（幂等；既有库自动补表与补 `source` 列）。"""
    with store.transaction() as conn:
        conn.executescript(RISK_SCHEMA_SQL)
        conn.executescript(RISK_ATTEMPTS_SCHEMA_SQL)
        conn.executescript(RISK_EVENTS_SCHEMA_SQL)
        columns = {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({_TABLE})")}
        if "source" not in columns:
            conn.execute(
                f"ALTER TABLE {_TABLE} ADD COLUMN source TEXT NOT NULL DEFAULT '{SOURCE_SEED}'"
            )


def attempts_on(store: Store, user_id: str, on_date: date) -> int:
    """按**业务日**读已提交次数（缺行 = 0，不猜）。"""
    rows = store.query(
        "SELECT attempts FROM risk_assessment_attempts WHERE user_id = ? AND on_date = ?",
        (user_id, on_date.isoformat()),
    )
    return int(rows[0]["attempts"]) if rows else 0


def record_attempt(store: Store, user_id: str, on_date: date) -> int:
    """记一次提交并返回当日累计次数（只增不减，跨日各自计数）。"""
    with store.transaction() as conn:
        conn.execute(
            "INSERT INTO risk_assessment_attempts(user_id, on_date, attempts) VALUES(?, ?, 1)"
            " ON CONFLICT(user_id, on_date) DO UPDATE SET attempts = attempts + 1",
            (user_id, on_date.isoformat()),
        )
    return attempts_on(store, user_id, on_date)


def record_assessment_event(
    store: Store,
    user_id: str,
    event_on: date,
    risk_profile: str,
    score: int,
    *,
    source: str,
    previous_profile: str | None = None,
) -> None:
    """记一次**已确认提交**的测评事件（只增不减）。"""
    with store.transaction() as conn:
        conn.execute(
            "INSERT INTO risk_assessment_events("
            "event_id, user_id, event_on, risk_profile, score, source, previous_profile"
            ") VALUES(?, ?, ?, ?, ?, ?, ?)",
            (
                f"rae_{uuid4().hex}",
                user_id,
                event_on.isoformat(),
                risk_profile,
                score,
                source,
                previous_profile,
            ),
        )


def assessment_event_count_since(store: Store, user_id: str, since: date) -> int:
    """最近一段时间（不含 `since` 当天）已提交测评次数。"""
    rows = store.query(
        "SELECT COUNT(*) AS n FROM risk_assessment_events"
        " WHERE user_id = ? AND event_on > ?",
        (user_id, since.isoformat()),
    )
    return int(rows[0]["n"]) if rows else 0


def earliest_assessment_event_on(store: Store, user_id: str, since: date) -> date | None:
    """最近一段时间最早一次测评日期；没有 ⇒ `None`。"""
    rows = store.query(
        "SELECT MIN(event_on) AS earliest FROM risk_assessment_events"
        " WHERE user_id = ? AND event_on > ?",
        (user_id, since.isoformat()),
    )
    if not rows or rows[0]["earliest"] is None:
        return None
    return date.fromisoformat(str(rows[0]["earliest"]))


def retake_state(store: Store, user_id: str, on_date: date) -> dict[str, object]:
    """当日与十二个月的重测额度（界面据此决定按钮文案与是否可点）。"""
    since = on_date - timedelta(days=365)
    return retake_payload(
        attempts_on(store, user_id, on_date),
        on_date,
        assessment_event_count_since(store, user_id, since),
        earliest_assessment_event_on(store, user_id, since),
    )


def retake_payload(
    used_today: int,
    on_date: date,
    used_12m: int = 0,
    earliest_event_on: date | None = None,
) -> dict[str, object]:
    """把「今日已用 / 十二个月已用」拼成对外口径。

    `basis` 如实标明这是**监管规则**；`demo_note` 另行说明演示环境按模拟业务日计数。
    """
    next_day = on_date + timedelta(days=1)
    next_available = next_day
    if used_12m >= RETAKES_PER_12M and earliest_event_on is not None:
        next_available = max(next_available, earliest_event_on + timedelta(days=365))
    return {
        "limit_per_day": RETAKES_PER_DAY,
        "limit_per_12m": RETAKES_PER_12M,
        "used_today": used_today,
        "used_12m": used_12m,
        "remaining_today": max(RETAKES_PER_DAY - used_today, 0),
        "remaining_12m": max(RETAKES_PER_12M - used_12m, 0),
        "next_available_on": next_available.isoformat(),
        "basis": "regulatory",
        "basis_label": "《金融机构产品适当性管理办法》第二十九条",
        "demo_note": "演示环境按模拟业务日计数",
    }


def seed_risk_assessment(store: Store) -> None:
    """幂等播种演示测评行（**不依赖 `SEEDED_KEY`**，既有库亦补行）。"""
    with store.transaction() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO risk_assessments(user_id, risk_profile, assessed_on,"
            " valid_days, source) VALUES(?, ?, ?, ?, ?)",
            (DEMO_USER_ID, "R3", _DEMO_ASSESSED_ON, 365, SOURCE_SEED),
        )


def save_risk_assessment(
    store: Store,
    user_id: str,
    risk_profile: str,
    assessed_on: date,
    valid_days: int = 365,
    *,
    source: str = SOURCE_USER_INPUT,
) -> None:
    """保存/更新用户侧测评事实（`source` ∈ `user_input` / `questionnaire`）。"""
    if risk_profile not in _RISK_PROFILES:
        raise ValueError(f"风险等级无效：{risk_profile}")
    if valid_days <= 0:
        raise ValueError(f"有效期必须为正整数天数：{valid_days}")
    if source not in _WRITABLE_SOURCES:
        raise ValueError(f"来源无效：{source}（用户侧只允许 {_WRITABLE_SOURCES}）")
    with store.transaction() as conn:
        conn.execute(
            "INSERT INTO risk_assessments(user_id, risk_profile, assessed_on, valid_days, source)"
            " VALUES(?, ?, ?, ?, ?)"
            " ON CONFLICT(user_id) DO UPDATE SET"
            " risk_profile = excluded.risk_profile,"
            " assessed_on = excluded.assessed_on,"
            " valid_days = excluded.valid_days,"
            " source = excluded.source",
            (user_id, risk_profile, assessed_on.isoformat(), valid_days, source),
        )


def risk_assessment(store: Store, user_id: str) -> Record[RiskAssessmentRecord] | None:
    """按用户读取测评事实（缺行→`None`；只读）。"""
    rows = store.query(
        "SELECT user_id, risk_profile, assessed_on, valid_days, source FROM risk_assessments"
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
        source=str(row["source"]),
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

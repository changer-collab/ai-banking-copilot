"""生活事件流水：提交即 `planned`，终态一次落定，跨用户不可见。"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ai_banking_copilot.gateway.store import Store
from ai_banking_copilot.web.app import (
    AppDeps,
    _life_event_title,
    _utterance_amount_cents,
    create_app,
)
from ai_banking_copilot.web.life_event_store import (
    STATUS_EXECUTED,
    STATUS_FAILED,
    STATUS_PLANNED,
    STATUS_REJECTED,
    LifeEventStore,
    init_life_event_schema,
)

_T0 = datetime(2026, 10, 27, 9, 0, tzinfo=UTC)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    opened = Store(tmp_path / "life_events.db")
    init_life_event_schema(opened)
    yield opened
    opened.close()


def test_create_records_planned_event(store: Store) -> None:
    events = LifeEventStore(store)
    record = events.create(
        user_id="u_demo",
        session_id="s1",
        event_id="birthday",
        event_name="生日提醒",
        amount_cents=88800,
        now=_T0,
    )

    assert record.status == STATUS_PLANNED
    assert record.amount_cents == 88800
    assert record.created_at == _T0.isoformat()
    assert record.to_dict()["event_name"] == "生日提醒"


def test_create_updates_existing_planned_row_instead_of_duplicating(store: Store) -> None:
    events = LifeEventStore(store)
    first = events.create(
        user_id="u_demo",
        session_id="s1",
        event_id="birthday",
        event_name="生日提醒",
        amount_cents=10000,
        now=_T0,
    )
    second = events.create(
        user_id="u_demo",
        session_id="s1",
        event_id="birthday",
        event_name="生日提醒",
        amount_cents=20000,
        now=_T0 + timedelta(minutes=1),
    )

    assert second.record_id == first.record_id
    assert second.amount_cents == 20000
    assert len(events.list_for_user("u_demo")) == 1


def test_mark_closes_the_latest_planned_row(store: Store) -> None:
    events = LifeEventStore(store)
    events.create(
        user_id="u_demo",
        session_id="s1",
        event_id="birthday",
        event_name="生日提醒",
        now=_T0,
    )

    assert events.mark(
        user_id="u_demo",
        session_id="s1",
        status=STATUS_EXECUTED,
        now=_T0 + timedelta(minutes=2),
    ) == 1
    record = events.list_for_user("u_demo")[0]
    assert record.status == STATUS_EXECUTED
    assert record.updated_at == (_T0 + timedelta(minutes=2)).isoformat()

    # 已关闭的行不会被二次改写。
    assert events.mark(user_id="u_demo", session_id="s1", status=STATUS_FAILED) == 0


def test_mark_rejects_illegal_status(store: Store) -> None:
    events = LifeEventStore(store)
    events.create(
        user_id="u_demo",
        session_id="s1",
        event_id="birthday",
        event_name="生日提醒",
        now=_T0,
    )

    with pytest.raises(ValueError):
        events.mark(user_id="u_demo", session_id="s1", status=STATUS_PLANNED)
    with pytest.raises(ValueError):
        events.mark(user_id="u_demo", session_id="s1", status="unknown")


def test_records_are_isolated_and_limited_per_user(store: Store) -> None:
    events = LifeEventStore(store)
    for index in range(3):
        events.create(
            user_id="u_demo",
            session_id=f"s{index}",
            event_id="birthday",
            event_name="生日提醒",
            now=_T0 + timedelta(minutes=index),
        )
    events.create(
        user_id="u_lin",
        session_id="s_lin",
        event_id="birthday",
        event_name="生日提醒",
        now=_T0,
    )

    assert [item.session_id for item in events.list_for_user("u_demo", limit=2)] == ["s2", "s1"]
    assert events.list_for_user("u_lin")[0].status == STATUS_PLANNED
    assert events.mark(user_id="u_lin", session_id="s_lin", status=STATUS_REJECTED) == 1
    assert events.list_for_user("u_demo")[0].status == STATUS_PLANNED


def test_event_title_is_persisted_and_returned(store: Store) -> None:
    """具体事件单独落一列：列表先显示它，类型只做分类标签。"""

    events = LifeEventStore(store)
    record = events.create(
        user_id="u_demo",
        session_id="s1",
        event_id="L4",
        event_name="大额凑集型",
        event_title="还房贷",
        amount_cents=50000,
        now=_T0,
    )

    assert record.event_title == "还房贷"
    assert record.to_dict()["event_title"] == "还房贷"


def test_schema_migration_adds_event_title_to_old_table(tmp_path: Path) -> None:
    """老库没有 `event_title` 列：`init` 补列后仍可写，历史行留空由前端回落到类型名。"""

    opened = Store(tmp_path / "old.db")
    with opened.transaction() as conn:
        conn.execute(
            "CREATE TABLE life_event_records ("
            " record_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, session_id TEXT NOT NULL,"
            " event_id TEXT NOT NULL, event_name TEXT NOT NULL, amount_cents INTEGER,"
            " status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
    init_life_event_schema(opened)
    record = LifeEventStore(opened).create(
        user_id="u_demo", session_id="s1", event_id="L6", event_name="阶段盘点型", now=_T0
    )
    assert record.event_title == ""
    opened.close()


def test_life_event_title_prefers_case_label_and_strips_amount() -> None:
    """命中「常见事件」就用事件名；否则去掉金额，删空则回落类型名。"""

    assert _life_event_title("凑钱还房贷 500 元给李娜") == "还房贷"
    assert _life_event_title("下周三出差，把卡片日限额降到 1000 元") == "出差"
    assert _life_event_title("我随手写的 300 元杂事") == "我随手写的 杂事"
    assert _life_event_title("500 元", fallback="阶段盘点型") == "阶段盘点型"
    assert _utterance_amount_cents("凑钱还房贷 500 元给李娜") == 50000
    assert _utterance_amount_cents("帮我盘点一下") is None


def test_life_events_api_returns_event_title_and_cases(
    deps: AppDeps, rw_store: Store
) -> None:
    """接口同时给「具体事件标题 + 该类型的常见事件」，前端才有东西可显示。"""

    init_life_event_schema(rw_store)
    client = TestClient(create_app(replace(deps, life_events=LifeEventStore(rw_store))))

    # 用一条能直接出计划卡的句子（L7 分摊收款）；金额从原话里取，不写进标题
    assert client.post(
        "/api/submit", json={"utterance": "合租分摊，李娜应付 100 元"}
    ).status_code == 200

    payload = client.get("/api/life-events").json()
    record = payload["records"][0]
    assert record["event_title"] == "合租分摊"
    assert record["event_name"] == "分摊收款型"
    assert record["amount_cents"] == 10000

    cases = {item["event_id"]: item["cases"] for item in payload["prototypes"]}
    assert {"label": "合租分摊", "utterance": "合租分摊，李娜应付 100 元"} in cases["L7"]
    assert all(item["cases"] for item in payload["prototypes"])


def test_life_events_api_lists_registered_schedules(
    deps: AppDeps, rw_store: Store
) -> None:
    """已登记预约必须能在生活页看到（此前只在确认回执里出现一次就消失）。"""

    init_life_event_schema(rw_store)
    events = LifeEventStore(rw_store)
    record = events.create(
        user_id="u_demo", session_id="s-schedule", event_id="L2",
        event_name="定期出资型", event_title="生活费", amount_cents=50000,
        steps=(), due_on="2026-11-04", now=_T0,
    )
    events.schedule(
        user_id="u_demo", session_id="s-schedule", event_id="L2", event_title="生活费",
        due_on=date(2026, 11, 4), plan_id="plan-1", record_id=record.record_id,
        nodes=(
            {
                "node_id": "n1", "action": "transfer.schedule",
                "params": {"amount_cents": 50000, "booked_on": "2026-11-04"},
                "depends_on": [],
            },
            {"node_id": "n2", "action": "subscription.list",
             "params": {"user_id": "u_demo"}, "depends_on": ["n1"]},
        ),
        now=_T0,
    )
    client = TestClient(create_app(replace(deps, life_events=events)))

    payload = client.get("/api/life-events").json()
    schedules = payload["schedules"]
    assert len(schedules) == 1
    assert schedules[0]["event_title"] == "生活费"
    assert schedules[0]["due_on"] == "2026-11-04"
    assert schedules[0]["kind"] == "order"
    assert schedules[0]["record_id"] == record.record_id
    assert [step["label"] for step in schedules[0]["steps"]] == ["定期转出", "代扣协议查询"]
    assert schedules[0]["steps"][0]["amount_cents"] == 50000
    # 记录本身仍是 `planned`，但要带出到期日：前端据此显示「已登记预约」而不是「待确认」
    assert payload["records"][0]["schedule_due_on"] == "2026-11-04"


def test_life_events_api_backfills_legacy_steps_from_schedule(
    deps: AppDeps, rw_store: Store
) -> None:
    """旧版本记录 `steps_json` 为空：用关联预约冻结的节点快照做只读回填。"""

    init_life_event_schema(rw_store)
    events = LifeEventStore(rw_store)
    legacy = events.create(
        user_id="u_demo", session_id="s-legacy", event_id="L1",
        event_name="资金预留型", event_title="生日礼物", amount_cents=29900,
        steps=(), due_on="2026-11-04", now=_T0,
    )
    events.schedule(
        user_id="u_demo", session_id="s-legacy", event_id="L1", event_title="生日礼物",
        due_on=date(2026, 11, 4), plan_id="plan-legacy", record_id=legacy.record_id,
        nodes=(
            {"node_id": "n1", "action": "funds.freeze",
             "params": {"amount_cents": 100000}, "depends_on": []},
            {"node_id": "n2", "action": "merchant.order",
             "params": {"amount_cents": 29900, "order_on": "2026-11-04"},
             "depends_on": ["n1"]},
        ),
        now=_T0,
    )
    client = TestClient(create_app(replace(deps, life_events=events)))

    record = client.get("/api/life-events").json()["records"][0]
    assert [step["label"] for step in record["steps"]] == [
        "锁定预算（资金预留）", "向商户下单",
    ]
    assert record["steps"][1]["date"] == "2026-11-04"

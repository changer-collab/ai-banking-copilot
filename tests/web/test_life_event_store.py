"""生活事件流水：提交即 `planned`，终态一次落定，跨用户不可见。"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ai_banking_copilot.gateway.store import Store
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

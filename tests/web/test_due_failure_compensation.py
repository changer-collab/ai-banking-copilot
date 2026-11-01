"""Q13：下单失败后不得留下半截冻结；`failed` 预约可由用户重试，且次数有上限。"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from pathlib import Path

import pytest

from ai_banking_copilot.gateway.store import Store
from ai_banking_copilot.web.life_event_store import (
    MAX_SCHEDULE_RETRIES,
    SCHEDULE_FAILED,
    SCHEDULE_SCHEDULED,
    LifeEventSchedule,
    LifeEventStore,
    init_life_event_schema,
)

_NODES = ({"node_id": "n1", "action": "funds.freeze", "params": {}, "depends_on": []},)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[LifeEventStore]:
    """`tests/web/` 没有共享的 `store` fixture：就地建真库并初始化预约表。"""
    opened = Store(tmp_path / "life_events.db")
    init_life_event_schema(opened)
    yield LifeEventStore(opened)
    opened.close()


def _schedule(store: LifeEventStore) -> LifeEventSchedule:
    """内部工厂：两条用例只关心状态迁移，不重复构造同一条预约。"""
    return store.schedule(
        user_id="u_demo",
        session_id="s",
        event_id="L1",
        event_title="生日",
        due_on=date(2026, 11, 4),
        plan_id="life_L1",
        nodes=_NODES,
    )


def test_failed_schedule_can_be_retried_by_user(store: LifeEventStore) -> None:
    schedule = _schedule(store)
    assert store.finish_schedule(
        user_id="u_demo", schedule_id=schedule.schedule_id, status=SCHEDULE_FAILED
    ) == 1
    assert store.retry_schedule(user_id="u_demo", schedule_id=schedule.schedule_id) == 1
    retried = store._get_schedule("u_demo", schedule.schedule_id)
    assert retried.status == SCHEDULE_SCHEDULED
    assert retried.retry_count == 1


def test_retry_counts_are_bounded(store: LifeEventStore) -> None:
    schedule = _schedule(store)
    for _ in range(MAX_SCHEDULE_RETRIES):
        store.finish_schedule(
            user_id="u_demo", schedule_id=schedule.schedule_id, status=SCHEDULE_FAILED
        )
        store.retry_schedule(user_id="u_demo", schedule_id=schedule.schedule_id)
    store.finish_schedule(
        user_id="u_demo", schedule_id=schedule.schedule_id, status=SCHEDULE_FAILED
    )
    assert store.retry_schedule(user_id="u_demo", schedule_id=schedule.schedule_id) == 0
    assert store._get_schedule("u_demo", schedule.schedule_id).status == SCHEDULE_FAILED

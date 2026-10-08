"""公共日期调度（`docs/已修复清单-2026-10-08.md` 步骤 3）：基准日算术 + 到期筛选 + 节点装配。"""

from datetime import date

import pytest

from ai_banking_copilot.contracts.plan import AnchorSource
from ai_banking_copilot.gateway.core_bank import DEFAULT_BOOKING_DATE
from ai_banking_copilot.planning.anchors import AnchorLedger
from ai_banking_copilot.scenarios.scheduling import (
    REFERENCE_DATE,
    ScheduledItem,
    dispatch_due,
    due_date,
    is_due,
    plan_due,
)


def _item(key: str, due_on: date, action: str = "transfer.schedule") -> ScheduledItem:
    return ScheduledItem(key=key, due_on=due_on, action=action, params={"amount_cents": 100})


def test_reference_date_is_the_single_clock() -> None:
    """基准日只有一个来源：转发 01 的 `DEFAULT_BOOKING_DATE`，不得另立时钟。"""
    assert REFERENCE_DATE == DEFAULT_BOOKING_DATE


def test_due_date_subtracts_lead_days() -> None:
    assert due_date(date(2026, 10, 10)) == date(2026, 10, 10)
    assert due_date(date(2026, 10, 10), lead_days=2) == date(2026, 10, 8)


def test_due_date_rejects_negative_lead_days() -> None:
    with pytest.raises(ValueError):
        due_date(date(2026, 10, 10), lead_days=-1)


def test_is_due_includes_the_boundary_day() -> None:
    assert is_due(date(2026, 10, 1), as_of=date(2026, 10, 1)) is True
    assert is_due(date(2026, 10, 2), as_of=date(2026, 10, 1)) is False


def test_dispatch_due_filters_and_sorts_deterministically() -> None:
    items = (
        _item("b", date(2026, 10, 3)),
        _item("a", date(2026, 10, 1)),
        _item("c", date(2026, 10, 1)),
        _item("future", date(2026, 11, 1)),
    )
    due = dispatch_due(items, as_of=date(2026, 10, 5))
    assert [item.key for item in due] == ["a", "c", "b"]
    assert dispatch_due(items, as_of=date(2026, 10, 5)) == due


def test_dispatch_due_is_empty_when_nothing_is_due() -> None:
    assert dispatch_due((_item("future", date(2026, 11, 1)),), as_of=date(2026, 10, 1)) == ()


def test_plan_due_builds_anchored_nodes_in_due_order() -> None:
    ledger = AnchorLedger()
    items = (_item("late", date(2026, 10, 3)), _item("early", date(2026, 10, 1)))
    nodes = plan_due(items, as_of=date(2026, 10, 5), ledger=ledger)
    assert [node.node_id for node in nodes] == ["due1", "due2"]
    assert [node.action for node in nodes] == ["transfer.schedule", "transfer.schedule"]
    assert nodes[0].params == {"amount_cents": 100}
    refs = [node.intent for node in nodes]
    assert all(ref is not None and ref.source is AnchorSource.EXPLICIT for ref in refs)
    assert len({ref.anchor_id for ref in refs if ref is not None}) == 2


def test_plan_due_uses_the_requested_node_prefix() -> None:
    nodes = plan_due(
        (_item("a", date(2026, 10, 1)),),
        as_of=date(2026, 10, 5),
        ledger=AnchorLedger(),
        node_prefix="order",
    )
    assert [node.node_id for node in nodes] == ["order1"]


def test_plan_due_grants_no_anchor_when_nothing_is_due() -> None:
    ledger = AnchorLedger()
    nodes = plan_due((_item("future", date(2026, 11, 1)),), as_of=date(2026, 10, 1), ledger=ledger)
    assert nodes == ()
    assert ledger.anchors() == ()


def test_scheduled_item_rejects_empty_key_or_action() -> None:
    with pytest.raises(ValueError):
        ScheduledItem(key="", due_on=date(2026, 10, 1), action="transfer.schedule", params={})
    with pytest.raises(ValueError):
        ScheduledItem(key="k", due_on=date(2026, 10, 1), action="", params={})

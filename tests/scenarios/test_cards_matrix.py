"""卡片域 7 动作 × 三色矩阵：逐格对照 `docs/2026-10-26/三色基准表-2026-10-26.md`。

以三色基准表为准（**不得反向改表**）：
- 绿：`card.list`（纯查询）
- 黄：`card.lock` / `card.apply_virtual`
- 红：`card.unlock` / `card.report_lost` / `card.set_limit` / `password.change`
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from ai_banking_copilot.contracts.authorization import Decision, Level
from ai_banking_copilot.contracts.plan import AnchorSource, IntentRef, PlanNode
from ai_banking_copilot.contracts.taint import Trust, Value
from ai_banking_copilot.gateway.core_bank import DEFAULT_BOOKING_DATE
from ai_banking_copilot.gateway.facts import FactGateway
from ai_banking_copilot.gateway.models import Money
from ai_banking_copilot.gateway.seed import DEMO_USER_ID
from ai_banking_copilot.planning.llm import Intent
from ai_banking_copilot.policy.pdp import evaluate
from ai_banking_copilot.scenarios.authorization import node_policy_request
from ai_banking_copilot.scenarios.base import ScenarioRefused
from ai_banking_copilot.scenarios.cards import CARD_DAILY_LIMIT_MAX_CENTS, CardsHandler
from ai_banking_copilot.tools.catalog import build_default_registry

CARD_ID = "card_debit"

MATRIX: tuple[tuple[str, Level], ...] = (
    ("card.list", Level.GREEN),
    ("card.lock", Level.YELLOW),
    ("card.apply_virtual", Level.YELLOW),
    ("card.unlock", Level.RED),
    ("card.report_lost", Level.RED),
    ("card.set_limit", Level.RED),
    ("password.change", Level.RED),
)
"""动作 → 期望级别（与三色基准表 §二/§三 一致）。"""


def _intent(action: str, **slots: object) -> Intent:
    trusted = {"card": Value(CARD_ID, Trust.TRUSTED), "span": action}
    trusted.update(slots)
    return Intent(action=action, slots=trusted, confidence=0.9, evidence=action)


def _plan_nodes(action: str, gateway: FactGateway) -> Sequence[PlanNode]:
    """卡片 6 动作走场景处理器；`password.change` 没有卡片槽，直接构节点。"""
    if action == "password.change":
        return (
            PlanNode(
                node_id="n1",
                action=action,
                params={"user_id": DEMO_USER_ID},
                intent=IntentRef(source=AnchorSource.EXPLICIT, anchor_id="anchor-password"),
            ),
        )
    if action == "card.apply_virtual":
        intent = _intent(action, daily_limit=Value(Money(50000), Trust.TRUSTED))
    elif action == "card.set_limit":
        intent = _intent(action, daily_limit=Value(Money(80000), Trust.TRUSTED))
    elif action == "card.list":
        intent = Intent(action=action, slots={}, confidence=0.9, evidence=action)
    else:
        intent = _intent(action)
    return CardsHandler().build([intent], gateway).nodes


def _decide(action: str, gateway: FactGateway) -> Decision:
    registry = build_default_registry()
    node = _plan_nodes(action, gateway)[0]
    return evaluate(
        node_policy_request(
            node,
            spec=registry.get(action),
            gateway=gateway,
            user_id=DEMO_USER_ID,
            today=DEFAULT_BOOKING_DATE,
            flags=frozenset(),
        )
    )


@pytest.mark.parametrize(("action", "expected"), MATRIX)
def test_card_domain_levels_match_the_authoritative_table(
    action: str, expected: Level, gateway: FactGateway
) -> None:
    assert _decide(action, gateway).level is expected


def test_no_card_domain_action_falls_into_green_except_pure_query(
    gateway: FactGateway,
) -> None:
    """绿只含纯查询：任何状态变更动作都不得判绿（官方三色主干）。"""
    for action, expected in MATRIX:
        if expected is Level.GREEN:
            continue
        assert _decide(action, gateway).level is not Level.GREEN, action


def test_card_lock_is_yellow_and_unlock_is_red(gateway: FactGateway) -> None:
    """施加保护可确认，解除保护必须强验证（单向升档）。"""
    assert _decide("card.lock", gateway).level is Level.YELLOW
    assert _decide("card.unlock", gateway).level is Level.RED


def test_set_limit_above_the_single_card_cap_is_refused(gateway: FactGateway) -> None:
    """S6①：调额超单卡上限（5 万元）→ 场景层拒绝并说明。"""
    intent = _intent(
        "card.set_limit", daily_limit=Value(Money(50_000_000), Trust.TRUSTED)
    )
    with pytest.raises(ScenarioRefused, match="上限"):
        CardsHandler().build([intent], gateway)


def test_set_limit_at_the_cap_is_allowed(gateway: FactGateway) -> None:
    intent = _intent(
        "card.set_limit", daily_limit=Value(Money(CARD_DAILY_LIMIT_MAX_CENTS), Trust.TRUSTED)
    )
    plan = CardsHandler().build([intent], gateway)
    assert plan.nodes[0].params["daily_limit_cents"] == CARD_DAILY_LIMIT_MAX_CENTS

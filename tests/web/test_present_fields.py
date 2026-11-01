"""呈现层白名单：内部键不得出现在卡片与记录页。"""

from __future__ import annotations

from typing import cast

from ai_banking_copilot.contracts.plan import Plan, PlanNode
from ai_banking_copilot.gateway.facts import FactGateway
from ai_banking_copilot.web.plan_card import plan_summary
from ai_banking_copilot.web.present import drop_internal, fact_lines


class _NoFacts:
    """最小事实网关桩：`merchant.order` 的摘要只读节点参数，不查网关。"""


def test_internal_keys_never_reach_the_card() -> None:
    params = {
        "merchant": "花间集（西湖店）",
        "amount_cents": 9900,
        "order_on": "2026-11-04",
        "occasion_id": "birthday:2026:flowers",
        "budget_hold_id": "life:L1:acc_savings:2026-11-04",
    }
    lines = fact_lines("merchant.order", params)
    rendered = " ".join(f"{line.label}{line.value}" for line in lines)
    assert "occasion" not in rendered
    assert "budget_hold_id" not in rendered
    assert "花间集（西湖店）" in rendered
    assert "99.00" in rendered


def test_internal_keys_have_no_label_in_the_whitelist() -> None:
    """内部键曾被登记成「场合」「账户」，永远不会输出——白名单里不该再有这类死项。"""
    lines = fact_lines("merchant.order", {"merchant": "花间集", "occasion_id": "birthday"})
    assert "场合" not in {line.label for line in lines}
    lines = fact_lines("funds.freeze", {"amount_cents": 9900, "account_id": "acc_savings"})
    assert "账户" not in {line.label for line in lines}


def test_plan_summary_does_not_repeat_the_same_fact() -> None:
    plan = Plan(
        plan_id="plan_merchant_order",
        nodes=(
            PlanNode(
                node_id="n1",
                action="merchant.order",
                params={
                    "merchant": "花间集（西湖店）",
                    "amount_cents": 9900,
                    "order_on": "2026-11-04",
                    "occasion_id": "birthday:2026:flowers",
                },
            ),
        ),
    )
    summary = plan_summary(plan, cast(FactGateway, _NoFacts()))
    rendered = " ".join(line.value for line in summary)
    assert rendered.count("花间集（西湖店）") == 1
    assert rendered.count("99.00") == 1
    assert rendered.count("2026-11-04") == 1
    assert "场合" not in {line.label for line in summary}


def test_drop_internal_is_a_whitelist_of_known_keys() -> None:
    kept = drop_internal({"merchant": "花间集", "user_id": "u_demo", "grant": "explicit"})
    assert kept == {"merchant": "花间集"}

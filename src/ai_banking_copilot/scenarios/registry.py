"""场景注册表：装配六个处理器 · 按意图选择 · 统一运行出口（场景级产出）。

选择策略：`cross` 需覆盖 ≥ 2 个场景域，故排在首位即可保证"多域 → 跨场景"；
单域请求落到对应场景处理器。`run_scenario` 是**场景级产出出口**：
组装计划 → `validate_plan` 自检 → 计划级裁决（绿 = 纯查询；否则 `evaluate_plan`），
**不执行**（执行归 `policy.pep`，由 06 在用户确认后驱动）。
"""

from __future__ import annotations

from collections.abc import Sequence

from ai_banking_copilot.contracts.authorization import green_or_raise
from ai_banking_copilot.contracts.plan import validate_plan
from ai_banking_copilot.gateway.facts import FactGateway
from ai_banking_copilot.planning.llm import Intent
from ai_banking_copilot.policy.pdp import evaluate_plan
from ai_banking_copilot.scenarios.base import (
    ClarificationRequired,
    ScenarioHandler,
    ScenarioOutcome,
    ScenarioRefused,
    ScenarioStatus,
    plan_is_pure_query,
    plan_net_effect,
)
from ai_banking_copilot.scenarios.bills import BillsHandler
from ai_banking_copilot.scenarios.cards import CardsHandler
from ai_banking_copilot.scenarios.cross import CrossHandler
from ai_banking_copilot.scenarios.subscriptions import SubscriptionsHandler
from ai_banking_copilot.scenarios.transfer import TransferHandler
from ai_banking_copilot.scenarios.wealth import WealthHandler

SCENARIO_HANDLERS: tuple[ScenarioHandler, ...] = (
    CrossHandler(),
    TransferHandler(),
    BillsHandler(),
    WealthHandler(),
    CardsHandler(),
    SubscriptionsHandler(),
)
"""六场景处理器（顺序即选择优先级：多域优先命中 `cross`）。"""


class UnknownScenarioError(ValueError):
    """没有任何处理器承接这组意图（未知意图：安全拒绝）。"""


def select_handler(intents: Sequence[Intent]) -> ScenarioHandler:
    """按注册顺序返回首个承接该组意图的处理器。"""
    for handler in SCENARIO_HANDLERS:
        if handler.matches(intents):
            return handler
    raise UnknownScenarioError(f"无法匹配场景：{[intent.action for intent in intents]}")


def run_scenario(
    handler: ScenarioHandler, intents: Sequence[Intent], gateway: FactGateway
) -> ScenarioOutcome:
    """组装计划并给出计划级下限；澄清 / 拒绝一律**不产出级别**。"""
    try:
        plan = handler.build(intents, gateway)
    except ClarificationRequired as error:
        return ScenarioOutcome(
            scenario=handler.name,
            status=ScenarioStatus.NEEDS_CLARIFICATION,
            plan=None,
            decision=None,
            exec_results=(),
            message=str(error),
            record_ids=(),
        )
    except ScenarioRefused as error:
        return ScenarioOutcome(
            scenario=handler.name,
            status=ScenarioStatus.BLOCKED,
            plan=None,
            decision=None,
            exec_results=(),
            message=str(error),
            record_ids=(),
        )
    validate_plan(plan)
    if not plan.nodes:
        return ScenarioOutcome(
            scenario=handler.name,
            status=ScenarioStatus.BLOCKED,
            plan=None,
            decision=None,
            exec_results=(),
            message="无可执行节点：安全拒绝",
            record_ids=(),
        )
    decision = (
        green_or_raise(is_pure_query=True)
        if plan_is_pure_query(plan)
        else evaluate_plan(plan, plan_net_effect(plan, gateway))
    )
    return ScenarioOutcome(
        scenario=handler.name,
        status=ScenarioStatus.PLANNED,
        plan=plan,
        decision=decision,
        exec_results=(),
        message=f"{handler.name} 计划已生成（{len(plan.nodes)} 步）",
        record_ids=(),
    )

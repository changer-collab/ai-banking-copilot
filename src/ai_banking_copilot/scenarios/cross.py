"""跨场景联动：DAG 多步 · 锚点授予归交互层（LLM 只能提议）· 计划级净效应。

口径（PRD §4.1 场景⑥ / §5.3）：
- **DAG 多步**：跨场景 = 管道串联（多个白名单动作按依赖组成 DAG）；
- **锚点授予归交互层**：需求推断子树（订花 / 订蛋糕）必须携带
  `grant == "user_approved"`（交互层授予），LLM 只能提议、不能授予；缺失即拒绝；
- **计划级净效应**：`NE = Σ cf(node)`，防"多步组合越权"（由 `policy.pdp.evaluate_plan` 使用）。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

from ai_banking_copilot.contracts.plan import (
    AnchorSource,
    IntentRef,
    Plan,
    PlanNode,
    validate_plan,
)
from ai_banking_copilot.gateway.facts import FactGateway
from ai_banking_copilot.planning.anchors import AnchorLedger
from ai_banking_copilot.planning.llm import Intent
from ai_banking_copilot.scenarios.base import (
    ScenarioHandler,
    ScenarioRefused,
    accept_data,
    as_money,
    as_str,
    grant_anchor,
    trusted_slot,
)
from ai_banking_copilot.scenarios.transfer import build_money_node

MONEY_ACTIONS = frozenset({"transfer.create", "transfer.schedule"})
"""可被跨场景复用的资金流出动作（README 规范动作表 #8 / #9）。"""

_FREEZE_ACTION = "funds.freeze"
_DEFAULT_FREEZE_REASON = "预算冻结"
_MIN_DOMAINS = 2


def intents_domains(intents: Sequence[Intent]) -> frozenset[str]:
    """意图覆盖的场景域集合（动作名前缀，如 `funds` / `transfer` / `wealth`）。"""
    return frozenset(intent.action.split(".", 1)[0] for intent in intents if intent.action)


class CrossHandler(ScenarioHandler):
    """跨场景联动处理器（冻结预算 → 多步消费）。"""

    @property
    def name(self) -> str:
        return "cross"

    def matches(self, intents: Sequence[Intent]) -> bool:
        return len(intents_domains(intents)) >= _MIN_DOMAINS

    def build(self, intents: Sequence[Intent], gateway: FactGateway) -> Plan:
        if len(intents_domains(intents)) < _MIN_DOMAINS:
            raise ScenarioRefused("跨场景联动需覆盖 ≥ 2 个场景域")
        ledger = AnchorLedger()
        nodes: list[PlanNode] = []
        freeze_node_id: str | None = None
        freeze_intents = [item for item in intents if item.action == _FREEZE_ACTION]
        other_intents = [item for item in intents if item.action != _FREEZE_ACTION]
        for index, intent in enumerate(freeze_intents, start=1):
            node = _build_freeze_node(f"n{index}", intent, ledger)
            nodes.append(node)
            freeze_node_id = node.node_id
        start = len(nodes)
        for offset, intent in enumerate(other_intents, start=1):
            node_id = f"n{start + offset}"
            source = _grant_source(intent)
            if intent.action not in MONEY_ACTIONS:
                raise ScenarioRefused(f"跨场景暂不支持动作 {intent.action}")
            node = build_money_node(node_id, intent, gateway, ledger, source)
            if freeze_node_id is not None:
                node = replace(node, depends_on=(freeze_node_id,))
            nodes.append(node)
        plan = Plan(plan_id="cross_dag", nodes=tuple(nodes))
        validate_plan(plan)
        return plan


def _grant_source(intent: Intent) -> AnchorSource:
    grant = intent.slots.get("grant")
    if grant == "user_approved":
        return AnchorSource.USER_APPROVED
    if grant == "explicit":
        return AnchorSource.EXPLICIT
    raise ScenarioRefused("跨场景节点必须由交互层授予锚点（LLM 只能提议，不能授予）")


def _build_freeze_node(node_id: str, intent: Intent, ledger: AnchorLedger) -> PlanNode:
    source = _grant_source(intent)
    anchor_id = grant_anchor(ledger, source, "预算锁定")
    amount = as_money(trusted_slot(intent, ("amount", "amount_cents"), "amount"), "amount")
    reason_slot = intent.slots.get("reason")
    reason = accept_data(reason_slot) if reason_slot is not None else _DEFAULT_FREEZE_REASON
    params: dict[str, object] = {
        "account_id": as_str(trusted_slot(intent, ("account", "account_id"), "account"), "account"),
        "amount_cents": amount.cents,
        "reason": str(reason),
    }
    return PlanNode(
        node_id=node_id,
        action=_FREEZE_ACTION,
        params=params,
        intent=IntentRef(source=source, anchor_id=anchor_id),
    )

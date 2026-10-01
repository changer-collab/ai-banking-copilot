"""卡片场景：挂失不可逆（≥ 红）；锁定 / 解锁 / 虚拟卡申请 / 额度调整走状态变更。

口径（PRD §4.1 场景④ / §5.2 / I3′）：
- **挂失 = 不可逆动作 ⇒ 级别 ≥ 红**（由 PDP 的 `irreversible` modifier 保证，本层不改级别）；
- 虚拟卡申请 / 额度调整照官方口径为黄色（级别仍由 PDP 按动作风险 + 金额档定）。
"""

from __future__ import annotations

from collections.abc import Sequence

from ai_banking_copilot.contracts.plan import (
    AnchorSource,
    IntentRef,
    Plan,
    PlanNode,
    validate_plan,
)
from ai_banking_copilot.gateway.errors import GatewayNotFoundError
from ai_banking_copilot.gateway.facts import FactGateway
from ai_banking_copilot.gateway.models import Money
from ai_banking_copilot.gateway.seed import DEMO_USER_ID
from ai_banking_copilot.planning.anchors import AnchorLedger
from ai_banking_copilot.planning.llm import Intent
from ai_banking_copilot.policy.pdp import ObjectState, PolicyRequest
from ai_banking_copilot.scenarios.base import (
    ScenarioHandler,
    ScenarioRefused,
    as_money,
    as_str,
    grant_anchor,
    trusted_slot,
)

CARD_ACTIONS = frozenset(
    {
        "card.list",
        "card.lock",
        "card.unlock",
        "card.report_lost",
        "card.apply_virtual",
        "card.set_limit",
    }
)
"""卡片域动作（README 规范动作表 #6 / #14–#18）。"""

_CARD_SCOPED_ACTIONS = frozenset({"card.lock", "card.unlock", "card.report_lost", "card.set_limit"})
_LIMIT_ACTIONS = frozenset({"card.apply_virtual", "card.set_limit"})


def loss_report_request(user_id: str) -> PolicyRequest:
    """挂失的 PDP 请求：**不可逆**（≥ 红），与金额无关。"""
    return PolicyRequest(
        user_id=user_id,
        action="card.report_lost",
        amount=Money(0),
        daily_accumulated=Money(0),
        object_state=ObjectState.SELF,
        is_pure_query=False,
        irreversible=True,
    )


class CardsHandler(ScenarioHandler):
    """卡片场景处理器。"""

    @property
    def name(self) -> str:
        return "cards"

    def matches(self, intents: Sequence[Intent]) -> bool:
        return any(intent.action in CARD_ACTIONS for intent in intents)

    def build(self, intents: Sequence[Intent], gateway: FactGateway) -> Plan:
        card_intents = [item for item in intents if item.action in CARD_ACTIONS]
        if not card_intents:
            raise ScenarioRefused("未识别到卡片意图")
        ledger = AnchorLedger()
        nodes = tuple(
            _build_card_node(f"n{index}", intent, gateway, ledger)
            for index, intent in enumerate(card_intents, start=1)
        )
        plan = Plan(plan_id=f"cards_{len(nodes)}", nodes=nodes)
        validate_plan(plan)
        return plan


def _build_card_node(
    node_id: str, intent: Intent, gateway: FactGateway, ledger: AnchorLedger
) -> PlanNode:
    detail = f"卡片操作：{intent.evidence or intent.action}"
    anchor_id = grant_anchor(ledger, AnchorSource.EXPLICIT, detail)
    params: dict[str, object] = {"user_id": DEMO_USER_ID}
    if intent.action in _CARD_SCOPED_ACTIONS:
        card_id = as_str(trusted_slot(intent, ("card", "card_id"), "card"), "card")
        _require_card(gateway, card_id)
        params = {"card_id": card_id}
    if intent.action in _LIMIT_ACTIONS:
        limit = as_money(
            trusted_slot(intent, ("daily_limit", "daily_limit_cents"), "daily_limit"), "daily_limit"
        )
        if limit.cents <= 0:
            raise ScenarioRefused("日限额必须为正")
        params["daily_limit_cents"] = limit.cents
    return PlanNode(
        node_id=node_id,
        action=intent.action,
        params=params,
        intent=IntentRef(source=AnchorSource.EXPLICIT, anchor_id=anchor_id),
    )


def _require_card(gateway: FactGateway, card_id: str) -> None:
    try:
        gateway.card(card_id)
    except GatewayNotFoundError as error:
        raise ScenarioRefused(f"卡片不存在：{card_id}") from error

"""理财场景：申购前**强制风险测评（状态机）** · 只陈述事实与风险等级 · 禁收益暗示。

口径（PRD §4.1 场景③ / H6）：
- 申购前必须通过适当性闸门（`assess_suitability`）：未测评 / 过期 / 风险等级不匹配 → 硬拒绝；
- 产品属性**只陈述事实**（名称 / 风险等级 / 期限 / 起购金额），措辞不含任何收益暗示（H6）。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from enum import Enum

from ai_banking_copilot.contracts.plan import (
    AnchorSource,
    IntentRef,
    Plan,
    PlanNode,
    validate_plan,
)
from ai_banking_copilot.gateway.core_bank import DEFAULT_BOOKING_DATE
from ai_banking_copilot.gateway.errors import GatewayNotFoundError
from ai_banking_copilot.gateway.facts import FactGateway
from ai_banking_copilot.gateway.models import WealthProduct
from ai_banking_copilot.planning.anchors import AnchorLedger
from ai_banking_copilot.planning.llm import Intent
from ai_banking_copilot.scenarios.base import (
    ScenarioHandler,
    ScenarioRefused,
    as_money,
    as_str,
    grant_anchor,
    trusted_slot,
)

WEALTH_ACTIONS = frozenset({"wealth.list", "wealth.subscribe", "wealth.redeem"})
"""理财域动作（README 规范动作表 #5 / #10 / #11）。"""

_RISK_ORDER = {"R1": 1, "R2": 2, "R3": 3, "R4": 4, "R5": 5}

FORBIDDEN_HINTS = ("保本", "稳赚", "保证收益", "预期收益", "收益率", "年化", "包赚", "无风险")
"""收益暗示措辞黑名单（H6：不做投资建议、不承诺收益）。"""


class AssessmentState(str, Enum):
    """风险测评状态机（申购闸门的唯一判据来源）。"""

    UNASSESSED = "unassessed"
    VALID = "valid"
    EXPIRED = "expired"


@dataclass(frozen=True)
class RiskAssessment:
    """一次风险测评记录（事实快照，由交互层提供）。"""

    user_id: str
    risk_profile: str
    assessed_on: date
    valid_days: int = 365

    def state_on(self, today: date) -> AssessmentState:
        if (today - self.assessed_on).days > self.valid_days:
            return AssessmentState.EXPIRED
        return AssessmentState.VALID


class SuitabilityError(ScenarioRefused):
    """适当性闸门未通过：未测评 / 已过期 / 风险等级不匹配。"""


def assess_suitability(
    assessment: RiskAssessment | None, product: WealthProduct, today: date
) -> None:
    """风险测评状态机闸门：未测评 / 过期 / 风险等级不匹配 → 拒绝申购。"""
    if assessment is None:
        raise SuitabilityError("申购前必须完成风险测评（状态机：未测评）")
    state = assessment.state_on(today)
    if state is not AssessmentState.VALID:
        raise SuitabilityError("风险测评已过期或被拒，请重新测评")
    if _RISK_ORDER[assessment.risk_profile] < _RISK_ORDER[product.risk_level]:
        raise SuitabilityError(
            f"风险等级不匹配：测评 {assessment.risk_profile} < 产品 {product.risk_level}"
        )


def describe_product(product: WealthProduct) -> str:
    """只陈述事实与风险等级（H6）：不含任何收益暗示措辞。"""
    return (
        f"产品 {product.name}｜风险等级 {product.risk_level}｜期限 {product.term_days} 天"
        f"｜起购金额 {product.min_amount.yuan} 元"
    )


class WealthHandler(ScenarioHandler):
    """理财场景处理器。"""

    @property
    def name(self) -> str:
        return "wealth"

    def matches(self, intents: Sequence[Intent]) -> bool:
        return any(intent.action in WEALTH_ACTIONS for intent in intents)

    def build(self, intents: Sequence[Intent], gateway: FactGateway) -> Plan:
        wealth_intents = [item for item in intents if item.action in WEALTH_ACTIONS]
        if not wealth_intents:
            raise ScenarioRefused("未识别到理财意图")
        ledger = AnchorLedger()
        nodes = tuple(
            _build_wealth_node(f"n{index}", intent, gateway, ledger)
            for index, intent in enumerate(wealth_intents, start=1)
        )
        plan = Plan(plan_id=f"wealth_{len(nodes)}", nodes=nodes)
        validate_plan(plan)
        return plan


def _build_wealth_node(
    node_id: str, intent: Intent, gateway: FactGateway, ledger: AnchorLedger
) -> PlanNode:
    detail = f"理财操作：{intent.evidence or intent.action}"
    anchor_id = grant_anchor(ledger, AnchorSource.EXPLICIT, detail)
    if intent.action == "wealth.list":
        return PlanNode(
            node_id=node_id,
            action="wealth.list",
            params={},
            intent=IntentRef(source=AnchorSource.EXPLICIT, anchor_id=anchor_id),
        )
    product = _product(intent, gateway)
    account_id = as_str(trusted_slot(intent, ("account", "account_id"), "account"), "account")
    amount = as_money(trusted_slot(intent, ("amount", "amount_cents"), "amount"), "amount")
    if intent.action == "wealth.subscribe":
        assess_suitability(_assessment(intent), product, DEFAULT_BOOKING_DATE)
        if amount.cents < product.min_amount.cents:
            raise ScenarioRefused(f"{product.product_id} 起购金额为 {product.min_amount.yuan} 元")
    params: dict[str, object] = {
        "account_id": account_id,
        "product_id": product.product_id,
        "amount_cents": amount.cents,
    }
    return PlanNode(
        node_id=node_id,
        action=intent.action,
        params=params,
        intent=IntentRef(source=AnchorSource.EXPLICIT, anchor_id=anchor_id),
    )


def _product(intent: Intent, gateway: FactGateway) -> WealthProduct:
    product_id = as_str(trusted_slot(intent, ("product", "product_id"), "product"), "product")
    try:
        return gateway.wealth_product(product_id).payload
    except GatewayNotFoundError as error:
        raise ScenarioRefused(f"理财产品不存在：{product_id}") from error


def _assessment(intent: Intent) -> RiskAssessment | None:
    raw = intent.slots.get("assessment")
    if isinstance(raw, RiskAssessment):
        return raw
    return None


def to_risk_assessment(record: object) -> RiskAssessment | None:
    """把风险测评**事实记录**转成场景层 `RiskAssessment`；未识别⇒`None`（调用方 fail-closed）。

    入参取 `object`：让 06 `AppDeps.risk` 的 `Protocol` 返回 `object | None` 与 `mypy --strict`
    兼容，类型识别在本函数内完成。
    """
    if isinstance(record, RiskAssessment):
        return record
    user_id = getattr(record, "user_id", None)
    risk_profile = getattr(record, "risk_profile", None)
    assessed_on = getattr(record, "assessed_on", None)
    valid_days = getattr(record, "valid_days", None)
    if not isinstance(user_id, str) or not isinstance(risk_profile, str):
        return None
    if risk_profile not in _RISK_ORDER:
        return None
    if not isinstance(assessed_on, date) or not isinstance(valid_days, int):
        return None
    return RiskAssessment(
        user_id=user_id,
        risk_profile=risk_profile,
        assessed_on=assessed_on,
        valid_days=valid_days,
    )

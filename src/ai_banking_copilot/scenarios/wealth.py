"""理财场景：申购前**强制风险测评（状态机）** · 只陈述事实与风险等级 · 禁收益暗示。

口径（PRD §4.1 场景③ / H6）：
- 申购前必须通过适当性闸门（`assess_suitability`）：未测评 / 过期 / 风险等级不匹配 → 硬拒绝；
- **推荐 / 比较**（只读）与服务端硬筛选（`recommend_products`）：只有 `产品等级 ≤ 测评等级`
  的候选可进入结果，排序与文案都不得覆盖筛选结论；未测评 / 过期 ⇒ 拒绝推荐（不默认等级）；
- 产品属性**只陈述事实**（名称 / 风险等级 / 期限 / 起购金额），措辞不含任何收益暗示（H6）；
- 产品目录缺流动性 / 费用 / 产品状态 / 更新时间 ⇒ 一律标"未知"，不编造（当前 4 只为**演示数据**）。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from enum import StrEnum

from ai_banking_copilot.contracts.plan import (
    AnchorSource,
    IntentRef,
    Plan,
    PlanNode,
    validate_plan,
)
from ai_banking_copilot.gateway.core_bank import DEFAULT_BOOKING_DATE, WriteReceipt
from ai_banking_copilot.gateway.errors import GatewayNotFoundError
from ai_banking_copilot.gateway.facts import FactGateway, Record
from ai_banking_copilot.gateway.models import Money, WealthProduct
from ai_banking_copilot.planning.anchors import AnchorLedger
from ai_banking_copilot.planning.llm import Intent
from ai_banking_copilot.policy.pep import (
    QueryContext,
    QueryHandler,
    QueryResult,
    WriteContext,
    WriteHandler,
    money_cents,
    require_str,
)
from ai_banking_copilot.scenarios.base import (
    ScenarioHandler,
    ScenarioRefused,
    as_money,
    as_str,
    grant_anchor,
    intent_text,
    trusted_slot,
)

WEALTH_ACTIONS = frozenset({"wealth.list", "wealth.subscribe", "wealth.redeem"})
"""理财域动作（README 规范动作表 #5 / #10 / #11）。"""

_RISK_ORDER = {"R1": 1, "R2": 2, "R3": 3, "R4": 4, "R5": 5}

FORBIDDEN_HINTS = (
    "保本",
    "稳赚",
    "保证收益",
    "预期收益",
    "收益率",
    "年化",
    "包赚",
    "无风险",
    "收益高",
    "收益低",
    "收益最好",
    "收益最高",
    "收益更高",
    "收益最低",
    "赚钱",
)
"""收益暗示 / 收益承诺措辞黑名单（H6：不做投资建议、不承诺收益）。

S3 加严 12 词后保留**收益口径**词汇；「推荐 / 哪只 / 哪款 / 买哪个 / 选哪个」属于
**选品意图**而非收益承诺，已移入 `RECOMMENDATION_HINTS`（任务 3：推荐走只读筛选流程，
仍受 H6 保护——含收益承诺的整句依旧被拦）。"""

RECOMMENDATION_HINTS = ("推荐", "哪只", "哪款", "适合我", "买哪个", "选哪个", "比较")
"""选品 / 比较意图词：**不是**红线词，允许进入理财推荐流程（只读、服务端硬筛选）。"""


def is_recommendation_request(text: str) -> bool:
    """文本是否为**选品 / 比较**请求（决定 `wealth.list` 走推荐模式）。"""
    return any(hint in text for hint in RECOMMENDATION_HINTS)


class AssessmentState(StrEnum):
    """风险测评状态机（申购闸门的唯一判据来源）。"""

    UNASSESSED = "unassessed"
    VALID = "valid"
    EXPIRED = "expired"


@dataclass(frozen=True)
class RiskAssessment:
    """一次风险测评记录（事实快照，由交互层提供）。

    `source` 记录**来源**（`seed` = 演示播种，**不是**真实测评；`user_input` = 用户录入），
    输出时必须如实标注，避免把 seed 当成真实测评结论。
    """

    user_id: str
    risk_profile: str
    assessed_on: date
    valid_days: int = 365
    source: str = ""

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


def product_comparison(gateway: FactGateway) -> tuple[str, ...]:
    """只读产品事实清单（复用 `describe_product`）：无收益字段、无排序、无推荐（守 H6）。"""
    return tuple(describe_product(record.payload) for record in gateway.wealth_products())


PRODUCT_DATA_SOURCE = "内置演示数据（gateway/seed.py，非真实在售产品）"
"""产品目录出处：当前只有 4 只写死产品，**必须**如实标注为演示数据。"""

_UNDISCLOSED = "未知（产品数据未提供）"
"""目录缺该字段时的统一标注：标未知，不编造。"""

_RISK_SOURCE_LABELS = {"seed": "演示 seed 数据（非真实测评）", "user_input": "用户自助录入"}


def recommendation_profile(assessment: RiskAssessment | None, today: date) -> str:
    """推荐前的测评闸门：未测评 / 过期 ⇒ 拒绝（**绝不**默认任何等级）。"""
    if assessment is None:
        raise SuitabilityError("推荐前必须完成风险测评（状态机：未测评），请先完成风险测评")
    if assessment.state_on(today) is not AssessmentState.VALID:
        raise SuitabilityError("风险测评已过期或被拒，请重新测评后再获取产品推荐")
    return assessment.risk_profile


def recommendation_params(assessment: RiskAssessment | None, today: date) -> dict[str, object]:
    """推荐模式的控制参数（`risk_profile` 必给；来源如实透出）。"""
    params: dict[str, object] = {"risk_profile": recommendation_profile(assessment, today)}
    if assessment is not None and assessment.source:
        params["risk_source"] = assessment.source
    return params


def recommend_products(
    products: Sequence[Record[WealthProduct]], risk_profile: str
) -> tuple[Record[WealthProduct], ...]:
    """**服务端硬筛选**：只有 `产品风险等级 ≤ 用户测评等级` 的候选可进入结果。

    排序（风险等级升序 → 期限 → 起购金额 → 产品 id）只决定展示顺序，
    **不得**把被筛掉的产品重新带回结果（大模型改写与排序都无权覆盖本函数）。
    """
    limit = _RISK_ORDER.get(risk_profile)
    if limit is None:
        raise SuitabilityError(f"风险等级无效：{risk_profile}")
    eligible = [
        record for record in products if _RISK_ORDER[record.payload.risk_level] <= limit
    ]
    return tuple(sorted(eligible, key=lambda record: _rank(record.payload)))


def _rank(product: WealthProduct) -> tuple[int, int, int, str]:
    return (
        _RISK_ORDER[product.risk_level],
        product.term_days,
        product.min_amount.cents,
        product.product_id,
    )


def describe_candidate(product: WealthProduct, risk_profile: str) -> str:
    """推荐候选的事实明细：数据缺失一律标"未知"，不含收益暗示（H6）。"""
    return (
        f"产品 {product.name}（{product.product_id}）"
        f"｜风险等级 {product.risk_level}"
        f"｜期限 {product.term_days} 天"
        f"｜流动性 {_UNDISCLOSED}"
        f"｜费用 {_UNDISCLOSED}"
        f"｜起购金额 {product.min_amount.yuan} 元"
        f"｜主要风险 {_UNDISCLOSED}"
        f"｜潜在亏损 {_UNDISCLOSED}"
        f"｜产品状态 {_UNDISCLOSED}"
        f"｜适用条件 风险等级 ≤ {risk_profile}（当前测评）且测评在有效期内"
        f"｜推荐理由 通过风险等级硬筛选；按风险等级→期限→起购金额确定性排序，不参考未披露指标"
        f"｜数据来源 {PRODUCT_DATA_SOURCE}"
        f"｜更新时间 {_UNDISCLOSED}"
    )


def candidate_labels(gateway: FactGateway, risk_profile: str) -> tuple[str, ...]:
    """计划卡用：只展示**通过硬筛选**的候选（复用 `describe_product`）。"""
    return tuple(
        describe_product(record.payload)
        for record in recommend_products(gateway.wealth_products(), risk_profile)
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
        list_params: dict[str, object] = {}
        if is_recommendation_request(intent_text(intent)):
            list_params = recommendation_params(_assessment(intent), DEFAULT_BOOKING_DATE)
        return PlanNode(
            node_id=node_id,
            action="wealth.list",
            params=list_params,
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
    source = getattr(record, "source", "")
    return RiskAssessment(
        user_id=user_id,
        risk_profile=risk_profile,
        assessed_on=assessed_on,
        valid_days=valid_days,
        source=source if isinstance(source, str) else "",
    )


def _subscribe_handler(context: WriteContext) -> WriteReceipt:
    """把 `wealth.subscribe` 映射到 `CoreBank.subscribe_product`。"""
    return context.core_bank.subscribe_product(
        user_id=require_str(context.params, "user_id"),
        account_id=require_str(context.params, "account_id"),
        product_id=require_str(context.params, "product_id"),
        amount=Money(money_cents(context.params, context.spec)),
        idempotency_key=context.idempotency_key,
        before_write=context.before_write,
    )


def _redeem_handler(context: WriteContext) -> WriteReceipt:
    """把 `wealth.redeem` 映射到 `CoreBank.redeem_product`。"""
    return context.core_bank.redeem_product(
        user_id=require_str(context.params, "user_id"),
        account_id=require_str(context.params, "account_id"),
        product_id=require_str(context.params, "product_id"),
        amount=Money(money_cents(context.params, context.spec)),
        idempotency_key=context.idempotency_key,
        before_write=context.before_write,
    )


WRITE_HANDLERS: Mapping[str, WriteHandler] = {
    "wealth.subscribe": _subscribe_handler,
    "wealth.redeem": _redeem_handler,
}
"""理财域写处理器（组合根汇入 PEP；`docs/已修复清单-2026-10-08.md` 步骤 1.5）。"""


def _wealth_list(context: QueryContext) -> QueryResult:
    """只读：无 `risk_profile` ⇒ 全量目录；有 ⇒ 推荐模式（服务端硬筛选后的候选明细）。"""
    products = context.gateway.wealth_products()
    risk_profile = context.params.get("risk_profile")
    if not isinstance(risk_profile, str):
        return QueryResult(
            record_ids=tuple(item.record_id for item in products),
            provenance=tuple((item.record_id, item.content_hash) for item in products),
            detail=f"共 {len(products)} 只理财产品",
        )
    candidates = recommend_products(products, risk_profile)
    origin = f"测评等级 {risk_profile}{_describe_source(context.params.get('risk_source'))}"
    if not candidates:
        return QueryResult(
            record_ids=(),
            provenance=(),
            detail=(
                f"{origin}｜合格候选 0 只（产品目录共 {len(products)} 只）："
                "当前风险等级下无适配产品，不推荐任何不匹配产品"
            ),
        )
    lines = [
        f"{origin}｜合格候选 {len(candidates)} 只"
        f"（产品目录共 {len(products)} 只，已按风险等级服务端硬筛选）"
    ]
    lines.extend(describe_candidate(item.payload, risk_profile) for item in candidates)
    return QueryResult(
        record_ids=tuple(item.record_id for item in candidates),
        provenance=tuple((item.record_id, item.content_hash) for item in candidates),
        detail="\n".join(lines),
    )


def _describe_source(source: object) -> str:
    if not isinstance(source, str) or not source:
        return ""
    return f"（来源 {_RISK_SOURCE_LABELS.get(source, source)}）"


QUERY_HANDLERS: Mapping[str, QueryHandler] = {"wealth.list": _wealth_list}
"""理财域**读**处理器（组合根汇入 PEP；`docs/已修复清单-2026-10-08.md` 步骤 4「残留」补做）。"""

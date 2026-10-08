"""计划卡：**确定性模板**渲染（输入 = 已裁决 DAG + 事实网关数值）。

铁律（PRD §5.4）：
- 计划卡与执行计划**共用同一份已裁决 DAG**（结构上不可能不一致）；
- 数值只来自 `PlanNode.params` 与 `FactGateway`（H1 事实零生成），本层**不生成**任何数字；
- 计划卡只回显**动作 / 对象 / 时间三元组**，**不回显数据槽原文**（备注里的注入文本不进卡片，H3）；
- LLM 只允许做**不改变三元组**的润色，由 `polish_plan_card` 的确定性守卫强制
  （丢对象 / 改行数即拒）。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from typing import Protocol

from ai_banking_copilot.contracts.authorization import Decision, Level
from ai_banking_copilot.contracts.plan import Plan, PlanNode, cfhash, validate_plan
from ai_banking_copilot.gateway.errors import GatewayNotFoundError
from ai_banking_copilot.gateway.facts import FactGateway
from ai_banking_copilot.gateway.models import Money
from ai_banking_copilot.scenarios import bills, subscriptions, wealth

PROMPT_USER_CONFIRM = "user_confirm"
"""黄色：用户确认。"""

PROMPT_STRONG_VERIFY = "strong_verify"
"""红色：强验证（服务端一次性验证码挑战）。"""

PROMPT_MANUAL_TAKEOVER = "manual_takeover"
"""增强层 L3：人工接管。"""

PROMPT_DENY = "deny_and_explain"
"""增强层 L4：拒绝并解释。"""

_ACTION_LABELS: dict[str, str] = {
    "account.balance": "余额查询",
    "account.transactions": "交易记录查询",
    "bills.monthly": "月度账单分析",
    "bills.category_trend": "分类趋势分析",
    "bills.yearly": "年度账单汇总",
    "wealth.list": "理财查询",
    "card.list": "卡片查询",
    "subscription.list": "订阅查询",
    "transfer.create": "转账",
    "transfer.schedule": "定时转账",
    "transfer.book": "登记定时转账",
    "transfer.request_collection": "发起 AA 收款",
    "wealth.subscribe": "理财申购",
    "wealth.redeem": "理财赎回",
    "subscription.cancel": "取消订阅",
    "subscription.sign": "代扣签约",
    "card.lock": "限制卡片",
    "card.unlock": "解锁卡片",
    "card.report_lost": "卡片挂失",
    "card.apply_virtual": "申请虚拟卡",
    "card.set_limit": "调整卡片额度",
    "funds.freeze": "冻结资金",
    "funds.release": "解冻资金",
}


@dataclass(frozen=True)
class PlanTriple:
    """一个节点的**关键意图三元组**（动作 / 对象 / 时间）——润色不得改动它。"""

    action: str
    object_label: str
    when_label: str


@dataclass(frozen=True)
class PlanCard:
    """计划卡：`requires` 由 `level` 单向推导，禁止外部自由填写。"""

    card_id: str
    frozen_hash: str
    level: Level
    lines: tuple[str, ...]
    requires: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.card_id:
            raise ValueError("card_id 不得为空")
        if len(self.frozen_hash) != 64:
            raise ValueError("frozen_hash 必须是 64 位十六进制")
        if not isinstance(self.level, Level):
            raise TypeError(f"level 必须是 Level 成员，收到 {type(self.level).__name__}")
        if self.requires != _requires_for_level(self.level):
            raise ValueError("requires 必须由 level 推导，不得自由填写")


class PlanCardPolisher(Protocol):
    """润色通道（通常是 LLM）：只接收与返回文案，**不接触**计划与数值。"""

    def polish(self, lines: tuple[str, ...]) -> tuple[str, ...]:
        """返回润色后的同序文案行。"""
        ...


class PolishRejectedError(ValueError):
    """润色试图改变动作 / 对象 / 时间三元组或行数：拒绝并保留确定性模板。"""


def render_plan_card(plan: Plan, decision: Decision, gateway: FactGateway) -> PlanCard:
    """用确定性模板渲染计划卡（输入 = 已裁决 DAG + 事实网关数值）。"""
    validate_plan(plan)
    frozen_hash = cfhash(plan.nodes)
    triples = plan_triples(plan, gateway)
    lines = tuple(
        f"{index}. {_ACTION_LABELS.get(node.action, node.action)}："
        f"{triple.object_label}{_amount_label(node)}（{triple.when_label}）"
        for index, (node, triple) in enumerate(zip(plan.nodes, triples, strict=True), start=1)
    )
    return PlanCard(
        card_id=f"card_{frozen_hash[:12]}_{decision.level.name.lower()}",
        frozen_hash=frozen_hash,
        level=decision.level,
        lines=lines,
        requires=_requires_for_level(decision.level),
    )


def plan_triples(plan: Plan, gateway: FactGateway) -> tuple[PlanTriple, ...]:
    """逐节点抽出动作 / 对象 / 时间三元组（对象与时间取自事实网关）。"""
    return tuple(
        PlanTriple(
            action=node.action,
            object_label=_object_label(node, gateway),
            when_label=_when_label(node, gateway),
        )
        for node in plan.nodes
    )


def polish_plan_card(
    card: PlanCard, triples: tuple[PlanTriple, ...], polisher: PlanCardPolisher
) -> PlanCard:
    """应用润色并**强制三元组不变**：丢对象 / 丢时间 / 改行数一律拒绝（fail-safe 保留模板）。"""
    if len(triples) != len(card.lines):
        raise PolishRejectedError("三元组与文案行数不一致，拒绝润色")
    polished = polisher.polish(card.lines)
    if len(polished) != len(card.lines):
        raise PolishRejectedError(f"润色改变了行数：{len(card.lines)} -> {len(polished)}")
    for index, (line, triple) in enumerate(zip(polished, triples, strict=True), start=1):
        if triple.object_label not in line:
            raise PolishRejectedError(f"第 {index} 行丢失对象三元组：{triple.object_label}")
        if triple.when_label != "—" and triple.when_label not in line:
            raise PolishRejectedError(f"第 {index} 行丢失时间三元组：{triple.when_label}")
    return replace(card, lines=polished)


def describe_decision(decision: Decision) -> tuple[str, ...]:
    """可机读的命中规则列表（对齐 PRD §9.1 的「命中规则 id」）。"""
    return tuple(reason.value for reason in decision.reasons)


def _requires_for_level(level: Level) -> tuple[str, ...]:
    if level is Level.GREEN:
        return ()
    if level is Level.YELLOW:
        return (PROMPT_USER_CONFIRM,)
    if level is Level.RED:
        return (PROMPT_USER_CONFIRM, PROMPT_STRONG_VERIFY)
    if level is Level.MANUAL:
        return (PROMPT_MANUAL_TAKEOVER,)
    return (PROMPT_DENY,)


def _object_label(node: PlanNode, gateway: FactGateway) -> str:
    if node.action == "wealth.list":
        return f"理财产品：{'；'.join(wealth.product_comparison(gateway))}"
    if node.action == "subscription.list":
        user_id = _str_param(node, "user_id")
        if user_id is not None:
            upcoming = subscriptions.upcoming_charges(gateway, user_id)
            detected = subscriptions.detected_charges(gateway, user_id)
            return (
                f"订阅：{subscriptions.describe_upcoming_charges(upcoming)}"
                f"；{subscriptions.describe_detected_charges(detected)}"
            )
    if node.action == "account.transactions":
        account_id = _str_param(node, "account_id")
        start = _str_param(node, "start")
        end = _str_param(node, "end")
        if account_id is not None and start is not None and end is not None:
            large = bills.large_transactions(
                gateway, account_id, date.fromisoformat(start), date.fromisoformat(end)
            )
            return f"账户 {account_id}：{bills.describe_large_transactions(large)}"
    payee_id = _str_param(node, "payee_id")
    if payee_id is not None:
        try:
            payee = gateway.payee(payee_id).payload
        except GatewayNotFoundError:
            return f"收款人 {payee_id}"
        return f"{payee.full_name}（账号尾号 {payee.account_tail}）"
    product_id = _str_param(node, "product_id")
    if product_id is not None:
        try:
            product = gateway.wealth_product(product_id).payload
        except GatewayNotFoundError:
            return f"理财产品 {product_id}"
        return f"{product.name}（{product.risk_level}）"
    subscription_id = _str_param(node, "subscription_id")
    if subscription_id is not None:
        try:
            subscription = gateway.subscription(subscription_id).payload
        except GatewayNotFoundError:
            return f"订阅 {subscription_id}"
        return subscription.merchant
    card_id = _str_param(node, "card_id")
    if card_id is not None:
        return f"卡片 {card_id}"
    hold_id = _str_param(node, "hold_id")
    if hold_id is not None:
        return f"冻结记录 {hold_id}"
    account_id = _str_param(node, "account_id")
    if account_id is not None:
        return f"账户 {account_id}"
    return "本人账户"


def _when_label(node: PlanNode, gateway: FactGateway) -> str:
    if node.action == "account.transactions":
        start = _str_param(node, "start")
        end = _str_param(node, "end")
        if start is not None and end is not None:
            return f"{start}~{end}"
    if node.action == "bills.yearly":
        year = node.params.get("year")
        if isinstance(year, int) and not isinstance(year, bool):
            return f"{year}-01~{year}-{bills.ytd_end_month(year):02d}"
        return "—"
    if node.action == "subscription.list":
        user_id = _str_param(node, "user_id")
        if user_id is not None:
            upcoming = subscriptions.upcoming_charges(gateway, user_id)
            if upcoming:
                return upcoming[0].next_charge_on.isoformat()
            return "—"
    booked_on = _str_param(node, "booked_on")
    if booked_on is not None:
        return booked_on
    subscription_id = _str_param(node, "subscription_id")
    if subscription_id is not None:
        try:
            return gateway.subscription(subscription_id).payload.next_charge_on.isoformat()
        except GatewayNotFoundError:
            return "—"
    return "—"


def _amount_label(node: PlanNode) -> str:
    cents = node.params.get("amount_cents")
    if isinstance(cents, int) and not isinstance(cents, bool):
        return f" 金额 {Money(cents).yuan} 元"
    limit = node.params.get("daily_limit_cents")
    if isinstance(limit, int) and not isinstance(limit, bool):
        return f" 额度 {Money(limit).yuan} 元"
    return ""


def _str_param(node: PlanNode, key: str) -> str | None:
    value = node.params.get(key)
    if isinstance(value, str) and value:
        return value
    return None

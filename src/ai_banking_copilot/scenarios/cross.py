"""跨场景与多意图：DAG 多步 · 锚点授予归交互层 · 计划级净效应。

口径（PRD §4.1 场景⑥ / §5.3）：
- **DAG 多步**：跨场景 = 管道串联（多个白名单动作按依赖组成 DAG）；
- **锚点授予归交互层**：需求推断子树（订花 / 订蛋糕）必须携带
  `grant == "user_approved"`（交互层授予），LLM 只能提议、不能授予；缺失即拒绝；
- **计划级净效应**：`NE = Σ cf(node)`，防"多步组合越权"（由 `policy.pdp.evaluate_plan` 使用）。

`CrossHandler.build` 分两支（README 裁决 #21）：
1. **冻结支**：意图 ⊆ `{funds.freeze} ∪ 资金动作` 且 ≥2 域 → 保留原「预算冻结 → 多步消费」语义
   （`grant` 锚点规则不变）；
2. **通用支**：其余多域组合 → 按域分组（保持输入顺序）→ 复用各域 handler 的 `build` →
   合并重编号 → 同资源串行 → **单一 `Plan`**；任一子意图澄清/拒绝 → 整单不产级别。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import date

from ai_banking_copilot.contracts.plan import (
    AnchorSource,
    IntentRef,
    Plan,
    PlanNode,
    validate_plan,
)
from ai_banking_copilot.gateway.core_bank import WriteReceipt
from ai_banking_copilot.gateway.facts import FactGateway
from ai_banking_copilot.gateway.models import Money
from ai_banking_copilot.gateway.seed import DEMO_USER_ID
from ai_banking_copilot.planning.anchors import AnchorLedger
from ai_banking_copilot.planning.llm import Intent
from ai_banking_copilot.policy.pep import (
    WriteContext,
    WriteHandler,
    money_cents,
    require_date,
    require_str,
)
from ai_banking_copilot.scenarios.base import (
    ScenarioHandler,
    ScenarioRefused,
    accept_data,
    as_money,
    as_str,
    grant_anchor,
    trusted_slot,
)
from ai_banking_copilot.scenarios.bills import BillsHandler
from ai_banking_copilot.scenarios.cards import CardsHandler
from ai_banking_copilot.scenarios.scheduling import ScheduledItem, due_date, plan_due
from ai_banking_copilot.scenarios.subscriptions import SubscriptionsHandler
from ai_banking_copilot.scenarios.transfer import TransferHandler, build_money_node
from ai_banking_copilot.scenarios.wealth import WealthHandler

MONEY_ACTIONS = frozenset({"transfer.create", "transfer.schedule"})
"""可被跨场景复用的资金流出动作（README 规范动作表 #8 / #9）。"""

_FREEZE_ACTION = "funds.freeze"
_RELEASE_ACTION = "funds.release"
_DEFAULT_FREEZE_REASON = "预算冻结"
_MIN_DOMAINS = 2

_FUNDS_ACTIONS = frozenset({_FREEZE_ACTION, _RELEASE_ACTION})
_FUNDS_DOMAIN = frozenset({"funds"})
_FROZEN_ACTIONS = frozenset({_FREEZE_ACTION, *MONEY_ACTIONS})
_RESOURCE_KEYS = ("account_id", "card_id", "subscription_id", "hold_id")


def intents_domains(intents: Sequence[Intent]) -> frozenset[str]:
    """意图覆盖的场景域集合（动作名前缀，如 `funds` / `transfer` / `wealth`）。"""
    return frozenset(intent.action.split(".", 1)[0] for intent in intents if intent.action)


def _build_release_node(
    node_id: str, intent: Intent, ledger: AnchorLedger, gateway: FactGateway
) -> PlanNode:
    source = _grant_source(intent)
    anchor_id = grant_anchor(ledger, source, "解冻")
    params: dict[str, object] = {
        "hold_id": as_str(trusted_slot(intent, ("hold", "hold_id"), "hold"), "hold"),
    }
    hold = gateway.hold(str(params["hold_id"])).payload
    params["amount_cents"] = hold.amount.cents
    return PlanNode(
        node_id=node_id,
        action=_RELEASE_ACTION,
        params=params,
        intent=IntentRef(source=source, anchor_id=anchor_id),
    )


class _FundsHandler(ScenarioHandler):
    """`funds.*` 域的内部构建器（仅供通用合并复用，不注册进 `SCENARIO_HANDLERS`）。"""

    @property
    def name(self) -> str:
        return "funds"

    def matches(self, intents: Sequence[Intent]) -> bool:
        return any(intent.action in _FUNDS_ACTIONS for intent in intents)

    def build(self, intents: Sequence[Intent], gateway: FactGateway) -> Plan:
        ledger = AnchorLedger()
        nodes: list[PlanNode] = []
        for index, intent in enumerate(intents, start=1):
            node_id = f"n{index}"
            if intent.action == _FREEZE_ACTION:
                nodes.append(_build_freeze_node(node_id, intent, ledger))
            elif intent.action == _RELEASE_ACTION:
                nodes.append(_build_release_node(node_id, intent, ledger, gateway))
            else:
                raise ScenarioRefused(f"funds 域暂不支持动作 {intent.action}")
        plan = Plan(plan_id="funds_plan", nodes=tuple(nodes))
        validate_plan(plan)
        return plan


_DOMAIN_HANDLERS: Mapping[str, ScenarioHandler] = {
    "account": BillsHandler(),
    "bills": BillsHandler(),
    "transfer": TransferHandler(),
    "wealth": WealthHandler(),
    "card": CardsHandler(),
    "subscription": SubscriptionsHandler(),
    "funds": _FundsHandler(),
}


class CrossHandler(ScenarioHandler):
    """跨场景 / 多意图处理器（冻结预算 → 多步消费；或任意多域组合）。"""

    @property
    def name(self) -> str:
        return "cross"

    def matches(self, intents: Sequence[Intent]) -> bool:
        domains = intents_domains(intents)
        return len(domains) >= _MIN_DOMAINS or domains == _FUNDS_DOMAIN

    def build(self, intents: Sequence[Intent], gateway: FactGateway) -> Plan:
        actions = {intent.action for intent in intents}
        domains = intents_domains(intents)
        frozen = actions <= _FROZEN_ACTIONS and _FREEZE_ACTION in actions
        if len(domains) >= _MIN_DOMAINS and frozen:
            return _build_frozen_pattern(intents, gateway)
        return _assemble_multi(intents, gateway)


def _build_frozen_pattern(intents: Sequence[Intent], gateway: FactGateway) -> Plan:
    """冻结支：预算锁定 → 多步消费（`grant` 锚点规则逐字保留）。"""
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


def _assemble_multi(intents: Sequence[Intent], gateway: FactGateway) -> Plan:
    """通用支：按域分组 → 复用各域 build → 合并重编号 → 同资源串行 → 单一 Plan。"""
    ledger = AnchorLedger()
    nodes: list[PlanNode] = []
    last_by_resource: dict[str, str] = {}
    for domain, group in _group_by_domain(intents):
        handler = _DOMAIN_HANDLERS.get(domain)
        if handler is None:
            raise ScenarioRefused(f"通用多意图暂不支持动作域 {domain}")
        sub_plan = handler.build(group, gateway)
        offset = len(nodes)
        local_ids = {
            node.node_id: f"n{offset + index}"
            for index, node in enumerate(sub_plan.nodes, start=1)
        }
        for sub_node in sub_plan.nodes:
            deps = tuple(local_ids.get(dep, dep) for dep in sub_node.depends_on)
            resource = _resource_key(sub_node)
            if resource is not None and resource in last_by_resource:
                deps = (*deps, last_by_resource[resource])
            source = (
                sub_node.intent.source if sub_node.intent is not None else AnchorSource.EXPLICIT
            )
            merged = PlanNode(
                node_id=local_ids[sub_node.node_id],
                action=sub_node.action,
                params=dict(sub_node.params),
                intent=IntentRef(
                    source=source,
                    anchor_id=grant_anchor(ledger, source, f"多意图合并：{sub_node.action}"),
                ),
                depends_on=deps,
            )
            nodes.append(merged)
            if resource is not None:
                last_by_resource[resource] = merged.node_id
    plan = Plan(plan_id="multi_dag", nodes=tuple(nodes))
    validate_plan(plan)
    return plan


def _group_by_domain(intents: Sequence[Intent]) -> tuple[tuple[str, tuple[Intent, ...]], ...]:
    """按动作域分组，保持**输入顺序**（dict 保序）。"""
    buckets: dict[str, list[Intent]] = {}
    for intent in intents:
        buckets.setdefault(intent.action.split(".", 1)[0], []).append(intent)
    return tuple((domain, tuple(group)) for domain, group in buckets.items())


def _resource_key(node: PlanNode) -> str | None:
    for key in _RESOURCE_KEYS:
        value = node.params.get(key)
        if isinstance(value, str) and value:
            return f"{key}:{value}"
    return None


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


BIRTHDAY_MERCHANT = "鲜花礼品店"
"""生日联动的演示商户名（**代码常量**，非 LLM 生成）。"""

BIRTHDAY_ORDER_CENTS = 29900
"""生日联动的演示下单金额（分）：299 元，走 `merchant.order` 的金额档（LARGE_FUNDS）。"""

_BIRTHDAY_LEAD_DAYS = 2
"""「生日前 N 天」的提前量（P2-5）。"""


def birthday_plan(
    gateway: FactGateway, as_of: date, *, user_id: str = DEMO_USER_ID
) -> tuple[PlanNode, ...]:
    """生日联动：**到期则**装配「生日前 2 天 → `merchant.order`」的下单节点（P2-5）。

    日期算术**一律走公共调度** `scenarios.scheduling`（`due_date(..., lead_days=2)` +
    `plan_due`），不另写一套；基准时钟只取入参 `as_of`（演示 / 评测统一用
    `scheduling.REFERENCE_DATE`）。生日数据源 = `user_profiles`（**演示播种值**，非真实客户资料）。
    """
    profile = gateway.user_profile(user_id)
    if profile is None:
        return ()
    birthday = _birthday_in_year(profile.payload.birthday, as_of.year)
    if birthday is None:
        return ()
    order_on = due_date(birthday, lead_days=_BIRTHDAY_LEAD_DAYS)
    occasion_id = f"birthday:{as_of.year}:flowers"
    if gateway.merchant_occasion_completed(user_id, occasion_id):
        return ()
    item = ScheduledItem(
        key=f"birthday:{user_id}:{as_of.year}",
        due_on=order_on,
        action="merchant.order",
        params={
            "merchant": BIRTHDAY_MERCHANT,
            "amount_cents": BIRTHDAY_ORDER_CENTS,
            "order_on": order_on.isoformat(),
            "occasion_id": occasion_id,
        },
    )
    return plan_due((item,), as_of=as_of, ledger=AnchorLedger(), node_prefix="order")


def _birthday_in_year(birthday: str, year: int) -> date | None:
    """把 `MM-DD` 生日转成本年对应日；格式非法 / 非闰年 2-29 一律返回 `None`（不猜）。"""
    parts = birthday.split("-")
    if len(parts) != 2:
        return None
    try:
        month, day = int(parts[0]), int(parts[1])
        return date(year, month, day)
    except ValueError:
        return None


def _freeze_handler(context: WriteContext) -> WriteReceipt:
    """把 `funds.freeze` 映射到 `CoreBank.freeze_funds`。"""
    return context.core_bank.freeze_funds(
        account_id=require_str(context.params, "account_id"),
        amount=Money(money_cents(context.params, context.spec)),
        reason=require_str(context.params, "reason"),
        idempotency_key=context.idempotency_key,
        before_write=context.before_write,
    )


def _release_handler(context: WriteContext) -> WriteReceipt:
    """把 `funds.release` 映射到 `CoreBank.unfreeze_funds`。"""
    return context.core_bank.unfreeze_funds(
        hold_id=require_str(context.params, "hold_id"),
        idempotency_key=context.idempotency_key,
        before_write=context.before_write,
    )


def _merchant_order_handler(context: WriteContext) -> WriteReceipt:
    """把 `merchant.order` 映射到 `CoreBank.place_merchant_order`。"""
    return context.core_bank.place_merchant_order(
        user_id=require_str(context.params, "user_id"),
        merchant=require_str(context.params, "merchant"),
        amount=Money(money_cents(context.params, context.spec)),
        order_on=require_date(context.params, "order_on"),
        idempotency_key=context.idempotency_key,
        before_write=context.before_write,
        occasion_id=(
            require_str(context.params, "occasion_id")
            if "occasion_id" in context.params else None
        ),
    )


WRITE_HANDLERS: Mapping[str, WriteHandler] = {
    "funds.freeze": _freeze_handler,
    "funds.release": _release_handler,
    "merchant.order": _merchant_order_handler,
}
"""`funds.*` / `merchant.order` 域写处理器（随该域场景模块自带；组合根汇入 PEP）。"""

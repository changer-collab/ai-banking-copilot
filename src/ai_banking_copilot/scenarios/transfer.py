"""转账场景：多候选强制澄清 · 备注注入只是数据 · 拆分 AA 走机械推导。

口径（PRD §4.1 场景①）：
- 备注夹带指令 → 只入**数据槽**（`accept_data`），永不解释为控制（H3）；
- 多候选收款人**禁止自动择一**，强制澄清、**不产出级别**（PRD §5.2）；
- 拆分 AA 走**机械推导**（总额 / 人数，零自由度），登记 `derived` 锚点（深度 1）；
- 转账金额一律 ≥ 黄（由 PDP 按日累计分档，本层只保证"不落绿"）。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import date, timedelta

from ai_banking_copilot.contracts.plan import (
    AnchorSource,
    IntentRef,
    Plan,
    PlanNode,
    validate_plan,
)
from ai_banking_copilot.gateway.core_bank import DEFAULT_BOOKING_DATE, WriteReceipt
from ai_banking_copilot.gateway.errors import GatewayNotFoundError
from ai_banking_copilot.gateway.facts import FactGateway
from ai_banking_copilot.gateway.models import Money
from ai_banking_copilot.gateway.resolvers import Candidate, parse_amount, resolve_payees
from ai_banking_copilot.gateway.seed import DEMO_USER_ID
from ai_banking_copilot.planning.anchors import AnchorLedger
from ai_banking_copilot.planning.llm import Intent
from ai_banking_copilot.policy.pep import (
    WriteContext,
    WriteHandler,
    money_cents,
    require_date,
    require_int,
    require_str,
)
from ai_banking_copilot.scenarios.base import (
    ClarificationRequired,
    ScenarioHandler,
    accept_data,
    as_date,
    as_money,
    as_str,
    grant_anchor,
    intent_text,
    require_trusted,
    trusted_slot,
)
from ai_banking_copilot.scenarios.scheduling import ScheduledItem, plan_due

TRANSFER_ACTIONS = frozenset(
    {
        "transfer.create",
        "transfer.schedule",
        "transfer.book",
        "transfer.request_collection",
    }
)
"""转账域动作（README 规范动作表 #8 / #9 + P2-3 / P2-6 新增动作）。"""

COLLECTION_ACTION = "transfer.request_collection"
"""AA **收款**动作（待收款，不产生资金流；与按份额转出的 `split_aa` 语义区分）。"""

_DUE_ACTION = "transfer.create"
"""到期派发所装配的动作（复用既有转账链路完成真正扣款）。"""

AA_KEYWORDS = ("AA", "aa", "均摊", "平摊", "分摊", "拆分")

_HEADCOUNT = re.compile(r"(\d+)\s*(?:个人|人|位|份)")
_PAYEE_ID_PREFIX = "p_"


def is_split_request(text: str) -> bool:
    """文本是否表达"拆分 AA"（需均摊）。"""
    return any(keyword in text for keyword in AA_KEYWORDS)


def split_aa(ledger: AnchorLedger, parent_anchor_id: str, total: Money, people: int) -> Money:
    """AA 均摊：**机械推导**（零自由度），并登记一条深度 1 的 `derived` 锚点。"""
    if people < 2:
        raise ClarificationRequired("amount", "拆分 AA 需明确参与人数（≥ 2）")
    if total.cents <= 0:
        raise ClarificationRequired("amount", "拆分 AA 需明确总额（正数）")
    if total.cents % people != 0:
        raise ClarificationRequired("amount", "总额无法按人数整除，请给出可均摊的总额")
    share = Money(total.cents // people)
    ledger.derive(parent_anchor_id, f"AA 均摊 {total.cents} 分 / {people} 人 = {share.cents} 分")
    return share


def build_money_node(
    node_id: str,
    intent: Intent,
    gateway: FactGateway,
    ledger: AnchorLedger,
    source: AnchorSource,
) -> PlanNode:
    """构造一个资金流出节点（转账 / 定时转账）；供 `transfer` 与 `cross` 复用。"""
    detail = f"资金流出：{intent.evidence or intent.action}"
    anchor_id = grant_anchor(ledger, source, detail)
    account_id = as_str(trusted_slot(intent, ("account", "account_id"), "account"), "account")
    method_raw = intent.slots.get("transfer_method")
    method = (
        as_str(require_trusted(method_raw, "transfer_method"), "transfer_method")
        if method_raw is not None
        else ("next_day" if intent.action in {"transfer.book", "transfer.schedule"} else "realtime")
    )
    # 次日到账没有显式日期时，默认落到「基准日 + 1 天」：登记不扣款，留出撤销窗口。
    default_when = (
        DEFAULT_BOOKING_DATE + timedelta(days=1)
        if method == "next_day" and intent.action in {"transfer.book", "transfer.schedule"}
        else DEFAULT_BOOKING_DATE
    )
    params: dict[str, object] = {
        "account_id": account_id,
        "payee_id": resolve_payee(intent, gateway),
        "amount_cents": resolve_amount_cents(intent, ledger, anchor_id).cents,
        "currency": resolve_currency(intent, gateway, account_id),
        "booked_on": optional_date(intent, ("when", "booked_on"), default_when).isoformat(),
        "transfer_method": method,
    }
    remark = intent.slots.get("remark")
    if remark is not None:
        params["remark"] = accept_data(remark)
    purpose = intent.slots.get("purpose")
    if purpose is not None:
        params["purpose"] = accept_data(purpose)
    return PlanNode(
        node_id=node_id,
        action=intent.action,
        params=params,
        intent=IntentRef(source=source, anchor_id=anchor_id),
    )


def resolve_payee(intent: Intent, gateway: FactGateway) -> str:
    """解析收款对象：已给可信 `payee_id` 直接用；否则走 01 名册确定性解析（多候选即澄清）。"""
    return _resolve_named_party(intent, "payee", gateway)


def resolve_payer(intent: Intent, gateway: FactGateway) -> str:
    """解析 AA 收款的**付款方**：与收款人同一套名册确定性解析（不另写一套）。"""
    return _resolve_named_party(intent, "payer", gateway)


def _resolve_named_party(intent: Intent, slot: str, gateway: FactGateway) -> str:
    label = "收款人" if slot == "payee" else "付款方"
    raw = intent.slots.get(slot)
    if raw is None:
        raise ClarificationRequired(slot, f"请确认{label}（全名 / 账号尾号 / 手机尾号）")
    value = require_trusted(raw, slot)
    if isinstance(value, str) and value.startswith(_PAYEE_ID_PREFIX):
        return value
    text = value if isinstance(value, str) else str(value)
    roster = tuple(record.payload for record in gateway.payees_of(DEMO_USER_ID))
    resolution = resolve_payees(roster, text)
    if resolution.needs_clarification:
        labels = tuple(_payee_label(item) for item in resolution.candidates)
        raise ClarificationRequired(slot, f"{label}存在多个候选，请明确", labels)
    if not resolution.candidates:
        raise ClarificationRequired(slot, f"未在名册中匹配到{label}，请确认")
    return resolution.candidates[0].payee_id


def build_collection_node(
    node_id: str,
    intent: Intent,
    gateway: FactGateway,
    ledger: AnchorLedger,
    source: AnchorSource,
) -> PlanNode:
    """构造一个 **AA 收款**节点（待收款，**不产生资金流**）。

    与 `build_money_node`（按份额转出的资金流出）语义分离：本动作只登记待收款请求，
    `amount_cents` 为待收金额（**不做 AA 均摊推导**——「AA」是场景措辞，不是整除运算）。
    """
    detail = f"AA 收款：{intent.evidence or intent.action}"
    anchor_id = grant_anchor(ledger, source, detail)
    params: dict[str, object] = {
        "payer_id": resolve_payer(intent, gateway),
        "amount_cents": _amount_from(intent).cents,
    }
    note = intent.slots.get("note")
    if note is not None:
        params["note"] = accept_data(note)
    return PlanNode(
        node_id=node_id,
        action=COLLECTION_ACTION,
        params=params,
        intent=IntentRef(source=source, anchor_id=anchor_id),
    )


def resolve_amount_cents(intent: Intent, ledger: AnchorLedger, parent_anchor_id: str) -> Money:
    """解析金额：AA 走机械推导，其余取控制槽或带单位文本（无单位不猜）。"""
    text = intent_text(intent)
    if is_split_request(text):
        match = _HEADCOUNT.search(text)
        if match is None:
            raise ClarificationRequired("amount", "拆分 AA 需明确参与人数（≥ 2）")
        return split_aa(ledger, parent_anchor_id, _amount_from(intent), int(match.group(1)))
    return _amount_from(intent)


def _account_currency(gateway: FactGateway, account_id: str) -> str:
    """读取付款账户币种（规范化）；账户不存在即澄清（不猜、不默认）。"""
    try:
        return gateway.account(account_id).payload.currency.strip().upper()
    except GatewayNotFoundError:
        raise ClarificationRequired("account", f"付款账户不存在：{account_id}") from None


def resolve_currency(intent: Intent, gateway: FactGateway, account_id: str) -> str:
    """绑定**付款账户币种**：以账户事实为准，显式币种必须与账户一致。

    缺失或冲突一律澄清（`ClarificationRequired`），**不产出可执行计划**：
    币种因此进入节点参数 → 计划哈希 / 计划卡展示 / PEP 写前复核。
    """
    stored = _account_currency(gateway, account_id)
    raw = None
    for key in ("currency", "currency_code"):
        if key in intent.slots:
            raw = intent.slots[key]
            break
    if raw is not None:
        requested = as_str(require_trusted(raw, "currency"), "currency").strip().upper()
        if not stored or requested != stored:
            raise ClarificationRequired(
                "currency",
                f"付款账户 {account_id} 的币种为 {stored or '未知'}，"
                f"不能按 {requested or '未指定'} 转出",
            )
        return requested
    if not stored:
        raise ClarificationRequired(
            "currency", f"付款账户 {account_id} 缺少币种信息，请明确转账币种"
        )
    return stored


def optional_date(intent: Intent, keys: Sequence[str], default: date) -> date:
    """可选时间控制槽；缺失时用基准记账日（保证评测可复现）。"""
    for key in keys:
        if key in intent.slots:
            return as_date(require_trusted(intent.slots[key], "when"), "when")
    return default


class TransferHandler(ScenarioHandler):
    """转账场景处理器。"""

    @property
    def name(self) -> str:
        return "transfer"

    def matches(self, intents: Sequence[Intent]) -> bool:
        return any(intent.action in TRANSFER_ACTIONS for intent in intents)

    def build(self, intents: Sequence[Intent], gateway: FactGateway) -> Plan:
        transfer_intents = [item for item in intents if item.action in TRANSFER_ACTIONS]
        if not transfer_intents:
            raise ClarificationRequired("action", "未识别到转账意图")
        ledger = AnchorLedger()
        nodes: list[PlanNode] = []
        for index, intent in enumerate(transfer_intents, start=1):
            node_id = f"n{index}"
            if intent.action == COLLECTION_ACTION:
                node = build_collection_node(
                    node_id, intent, gateway, ledger, AnchorSource.EXPLICIT
                )
            else:
                node = build_money_node(node_id, intent, gateway, ledger, AnchorSource.EXPLICIT)
            nodes.append(node)
        plan = Plan(plan_id=f"transfer_{len(nodes)}", nodes=tuple(nodes))
        validate_plan(plan)
        return plan


def _amount_from(intent: Intent) -> Money:
    raw = intent.slots.get("amount")
    if raw is not None:
        return as_money(require_trusted(raw, "amount"), "amount")
    parsed = parse_amount(intent_text(intent))
    if not parsed.matched or parsed.value is None:
        raise ClarificationRequired("amount", "请给出金额（必须带单位，例如 500 元）")
    return parsed.value


def _payee_label(candidate: Candidate) -> str:
    return f"{candidate.full_name}(尾号 {candidate.account_tail}/{candidate.phone_tail})"


def due_transfer_plan(
    gateway: FactGateway, as_of: date, user_id: str = DEMO_USER_ID
) -> tuple[PlanNode, ...]:
    """把**到期**的定时转账登记项装配成 `transfer.create` 节点（P2-6）。

    纪律：
    - 只做装配——**日期筛选与排序一律交给公共机制** `scenarios.scheduling.plan_due`
      （不另写日期 / 派发逻辑）；
    - 基准时钟由调用方传入的 `as_of` 决定，调用方应取 `scheduling.REFERENCE_DATE`
      （其唯一来源是 01 的 `DEFAULT_BOOKING_DATE`）；
    - 登记项**登记时不扣款**：真正扣款由装配出的 `transfer.create` 走既有链路完成。
    """
    items: list[ScheduledItem] = []
    for record in gateway.scheduled_transfers_of(user_id, status="scheduled"):
        currency = _account_currency(gateway, record.payload.account_id)
        if not currency:
            raise ClarificationRequired(
                "currency",
                f"付款账户 {record.payload.account_id} 缺少币种信息，无法派发到期转账",
            )
        items.append(
            ScheduledItem(
                key=record.payload.schedule_id,
                due_on=record.payload.booked_on,
                action=_DUE_ACTION,
                params={
                    "account_id": record.payload.account_id,
                    "payee_id": record.payload.payee_id,
                    "amount_cents": record.payload.amount.cents,
                    "currency": currency,
                    "booked_on": record.payload.booked_on.isoformat(),
                    "schedule_id": record.payload.schedule_id,
                    "executed_on": as_of.isoformat(),
                    "transfer_method": record.payload.method,
                    "purpose": record.payload.purpose,
                },
            )
        )
    return plan_due(tuple(items), as_of=as_of, ledger=AnchorLedger(), node_prefix="due")


def _optional_currency(params: dict[str, object]) -> str | None:
    """取可选币种控制槽；缺省交给核心银行按付款账户事实绑定，空串即拒绝。"""
    if "currency" not in params:
        return None
    return require_str(params, "currency")


def _optional_str(params: dict[str, object], key: str) -> str | None:
    """取可选文本参数；缺省返回 `None`，不把空串当成用户明确选择。"""
    value = params.get(key)
    return value if isinstance(value, str) and value else None


def _transfer_handler(context: WriteContext) -> WriteReceipt:
    """把 `transfer.*` 映射到 `CoreBank.transfer`（定时语义由 `booked_on` 承载）。"""
    return context.core_bank.transfer(
        user_id=require_str(context.params, "user_id"),
        account_id=require_str(context.params, "account_id"),
        payee_id=require_str(context.params, "payee_id"),
        amount=Money(money_cents(context.params, context.spec)),
        currency=_optional_currency(context.params),
        idempotency_key=context.idempotency_key,
        before_write=context.before_write,
        booked_on=require_date(context.params, "booked_on"),
        schedule_id=(
            require_str(context.params, "schedule_id") if "schedule_id" in context.params else None
        ),
        executed_on=(
            require_date(context.params, "executed_on") if "executed_on" in context.params else None
        ),
        transfer_method=_optional_str(context.params, "transfer_method"),
        transfer_purpose=_optional_str(context.params, "purpose"),
        transfer_remark=_optional_str(context.params, "remark"),
    )


def _request_collection_handler(context: WriteContext) -> WriteReceipt:
    """把 `transfer.request_collection` 映射到 `CoreBank.request_collection`（待收款，不扣款）。"""
    note = context.params.get("note")
    return context.core_bank.request_collection(
        user_id=require_str(context.params, "user_id"),
        payer_id=require_str(context.params, "payer_id"),
        amount=Money(require_int(context.params, "amount_cents")),
        note="" if note is None else str(note),
        idempotency_key=context.idempotency_key,
        before_write=context.before_write,
    )


def _book_transfer_handler(context: WriteContext) -> WriteReceipt:
    """把 `transfer.book` 映射到 `CoreBank.book_transfer`（登记未来转账，**登记不扣款**）。"""
    remark = context.params.get("remark")
    return context.core_bank.book_transfer(
        user_id=require_str(context.params, "user_id"),
        account_id=require_str(context.params, "account_id"),
        payee_id=require_str(context.params, "payee_id"),
        amount=Money(money_cents(context.params, context.spec)),
        currency=_optional_currency(context.params),
        booked_on=require_date(context.params, "booked_on"),
        remark="" if remark is None else str(remark),
        method=_optional_str(context.params, "transfer_method") or "next_day",
        purpose=_optional_str(context.params, "purpose") or "",
        idempotency_key=context.idempotency_key,
        before_write=context.before_write,
    )


WRITE_HANDLERS: Mapping[str, WriteHandler] = {
    "transfer.create": _transfer_handler,
    "transfer.schedule": _transfer_handler,
    "transfer.book": _book_transfer_handler,
    "transfer.request_collection": _request_collection_handler,
}
"""转账域写处理器（组合根汇入 PEP；`docs/已修复清单-2026-10-08.md` 步骤 1.5）。"""

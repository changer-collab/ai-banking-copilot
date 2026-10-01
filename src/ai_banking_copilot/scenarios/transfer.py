"""转账场景：多候选强制澄清 · 备注注入只是数据 · 拆分 AA 走机械推导。

口径（PRD §4.1 场景①）：
- 备注夹带指令 → 只入**数据槽**（`accept_data`），永不解释为控制（H3）；
- 多候选收款人**禁止自动择一**，强制澄清、**不产出级别**（PRD §5.2）；
- 拆分 AA 走**机械推导**（总额 / 人数，零自由度），登记 `derived` 锚点（深度 1）；
- 转账金额一律 ≥ 黄（由 PDP 按日累计分档，本层只保证"不落绿"）。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import date

from ai_banking_copilot.contracts.plan import (
    AnchorSource,
    IntentRef,
    Plan,
    PlanNode,
    validate_plan,
)
from ai_banking_copilot.gateway.core_bank import DEFAULT_BOOKING_DATE
from ai_banking_copilot.gateway.facts import FactGateway
from ai_banking_copilot.gateway.models import Money
from ai_banking_copilot.gateway.resolvers import Candidate, parse_amount, resolve_payees
from ai_banking_copilot.gateway.seed import DEMO_USER_ID
from ai_banking_copilot.planning.anchors import AnchorLedger
from ai_banking_copilot.planning.llm import Intent
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

TRANSFER_ACTIONS = frozenset({"transfer.create", "transfer.schedule"})
"""转账域动作（README 规范动作表 #8 / #9）。"""

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
    params: dict[str, object] = {
        "account_id": as_str(trusted_slot(intent, ("account", "account_id"), "account"), "account"),
        "payee_id": resolve_payee(intent, gateway),
        "amount_cents": resolve_amount_cents(intent, ledger, anchor_id).cents,
        "booked_on": optional_date(intent, ("when", "booked_on"), DEFAULT_BOOKING_DATE).isoformat(),
    }
    remark = intent.slots.get("remark")
    if remark is not None:
        params["remark"] = accept_data(remark)
    return PlanNode(
        node_id=node_id,
        action=intent.action,
        params=params,
        intent=IntentRef(source=source, anchor_id=anchor_id),
    )


def resolve_payee(intent: Intent, gateway: FactGateway) -> str:
    """解析收款对象：已给可信 `payee_id` 直接用；否则走 01 名册确定性解析（多候选即澄清）。"""
    raw = intent.slots.get("payee")
    if raw is None:
        raise ClarificationRequired("payee", "请确认收款人（全名 / 账号尾号 / 手机尾号）")
    value = require_trusted(raw, "payee")
    if isinstance(value, str) and value.startswith(_PAYEE_ID_PREFIX):
        return value
    text = value if isinstance(value, str) else str(value)
    roster = tuple(record.payload for record in gateway.payees_of(DEMO_USER_ID))
    resolution = resolve_payees(roster, text)
    if resolution.needs_clarification:
        labels = tuple(_payee_label(item) for item in resolution.candidates)
        raise ClarificationRequired("payee", "收款人存在多个候选，请明确", labels)
    if not resolution.candidates:
        raise ClarificationRequired("payee", "未在名册中匹配到收款人，请确认")
    return resolution.candidates[0].payee_id


def resolve_amount_cents(intent: Intent, ledger: AnchorLedger, parent_anchor_id: str) -> Money:
    """解析金额：AA 走机械推导，其余取控制槽或带单位文本（无单位不猜）。"""
    text = intent_text(intent)
    if is_split_request(text):
        match = _HEADCOUNT.search(text)
        if match is None:
            raise ClarificationRequired("amount", "拆分 AA 需明确参与人数（≥ 2）")
        return split_aa(ledger, parent_anchor_id, _amount_from(intent), int(match.group(1)))
    return _amount_from(intent)


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
        nodes = tuple(
            build_money_node(f"n{index}", intent, gateway, ledger, AnchorSource.EXPLICIT)
            for index, intent in enumerate(transfer_intents, start=1)
        )
        plan = Plan(plan_id=f"transfer_{len(nodes)}", nodes=nodes)
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

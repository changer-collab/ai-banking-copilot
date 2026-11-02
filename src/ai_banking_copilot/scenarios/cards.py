"""卡片场景：临时冻结 / 正式挂失 / 补卡 / 分卡限额。

口径（PRD §4.1 场景④ / §5.2 / I3′，2026-11-02 卡片页重构）：
- **临时冻结**（`card.lock`，口头挂失）：5–15 天、免费、到期自动解挂，黄色；
- **正式挂失**（`card.report_lost`）：不可逆、可能收费，红色；
- **补卡**（`card.replace`）：换新卡号、旧卡作废，红色；
- 借记卡四通道限额（`card.set_limit`）与信用卡额度（`card.adjust_credit_limit`）分开；
- 信用卡账单周期消费限额（`card.set_cycle_limit`）即时生效，黄色。
"""

from __future__ import annotations

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
from ai_banking_copilot.gateway.errors import CoreBankError, GatewayNotFoundError
from ai_banking_copilot.gateway.facts import FactGateway
from ai_banking_copilot.gateway.models import Card, Money
from ai_banking_copilot.gateway.seed import DEMO_USER_ID
from ai_banking_copilot.planning.anchors import AnchorLedger
from ai_banking_copilot.planning.llm import Intent
from ai_banking_copilot.policy.pdp import ObjectState, PolicyRequest
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
    trusted_slot,
)

CARD_ACTIONS = frozenset(
    {
        "card.list",
        "card.lock",
        "card.unlock",
        "card.report_lost",
        "card.replace",
        "card.apply_virtual",
        "card.set_limit",
        "card.adjust_credit_limit",
        "card.set_cycle_limit",
        "password.change",
    }
)
"""卡片域动作（含 2026-11-02 新增的补卡与信用卡额度动作）。"""

_CARD_SCOPED_ACTIONS = frozenset(
    {
        "card.lock",
        "card.unlock",
        "card.report_lost",
        "card.replace",
        "card.set_limit",
        "card.adjust_credit_limit",
        "card.set_cycle_limit",
    }
)
_LIMIT_ACTIONS = frozenset({"card.apply_virtual"})

CARD_DAILY_LIMIT_MAX_CENTS = 5_000_000
"""单通道日限额上限（分）：5 万元。调额超过该值一律拒绝（演示风控上限）。"""

CARD_CREDIT_LIMIT_MAX_CENTS = 20_000_000
"""信用卡额度上限（分）：20 万元。演示风控上限，不是真实授信结论。"""


def loss_report_request(user_id: str) -> PolicyRequest:
    """正式挂失的 PDP 请求：**不可逆**（≥ 红），与金额无关。

    保留该辅助函数供三色断言与文档引用；正式挂失的计划节点仍由 `CardsHandler.build`
    按状态机生成。
    """
    return PolicyRequest(
        user_id=user_id,
        action="card.report_lost",
        amount=Money(0),
        daily_accumulated=Money(0),
        object_state=ObjectState.SELF,
        is_pure_query=False,
        irreversible=True,
    )


_LOCK_DAYS = (5, 7, 10, 15)
_LIMIT_KINDS = ("atm", "transfer", "purchase", "overseas")
_LIMIT_KIND_LABELS = {
    "atm": "ATM 取现",
    "transfer": "转账",
    "purchase": "消费",
    "overseas": "境外",
}


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
    action = intent.action

    if action in _CARD_SCOPED_ACTIONS:
        card_id = as_str(trusted_slot(intent, ("card", "card_id"), "card"), "card")
        card = _require_card(gateway, card_id)
        params = {"card_id": card_id}
        action = _validate_card_action(action, card, intent, params)
    if action in _LIMIT_ACTIONS:
        limit = as_money(
            trusted_slot(intent, ("daily_limit", "daily_limit_cents"), "daily_limit"), "daily_limit"
        )
        if limit.cents <= 0:
            raise ScenarioRefused("虚拟卡额度必须为正")
        if limit.cents > CARD_DAILY_LIMIT_MAX_CENTS:
            raise ScenarioRefused(
                f"虚拟卡额度上限 {Money(CARD_DAILY_LIMIT_MAX_CENTS).yuan} 元，"
                f"暂不支持调整到 {Money(limit.cents).yuan} 元"
            )
        params["daily_limit_cents"] = limit.cents
    return PlanNode(
        node_id=node_id,
        action=action,
        params=params,
        intent=IntentRef(source=AnchorSource.EXPLICIT, anchor_id=anchor_id),
    )


def _effective_status(card: Card) -> str:
    """临时冻结到期按业务日判定为已自动解挂（与 `/api/cards` 的 effective_status 同口径）。"""
    if card.status != "locked" or not card.temp_lock_until:
        return card.status
    try:
        until = date.fromisoformat(card.temp_lock_until)
    except ValueError:
        return card.status
    return "active" if until < DEFAULT_BOOKING_DATE else card.status


def _validate_card_action(
    action: str, card: Card, intent: Intent, params: dict[str, object]
) -> str:
    """按卡状态与卡类型校验动作；返回**最终计划节点动作**（正式挂失可降级为临时冻结）。"""
    status = _effective_status(card)

    if action == "card.lock":
        if status == "lost":
            raise ScenarioRefused("卡片已正式挂失，不能再做临时冻结；如需用卡请申请补卡。")
        if status == "locked":
            raise ScenarioRefused("卡片已处于临时冻结状态，无需重复冻结。")
        days = _optional_int(intent, ("lock_days", "duration_days"), 7)
        if days not in _LOCK_DAYS:
            raise ScenarioRefused("临时冻结只能选 5 / 7 / 10 / 15 天。")
        params["duration_days"] = days
        reason = _optional_str(intent, ("lock_reason", "reason"))
        if reason is not None:
            params["reason"] = reason
        return "card.lock"

    if action == "card.unlock":
        if status == "lost":
            raise ScenarioRefused(
                "卡片已正式挂失，不支持解挂：挂失是不可逆操作；如仍需用卡，请申请补卡。"
            )
        # 健康卡 / 冻结已到期也允许出计划：如实说明「当前没有生效中的临时冻结」由裁决与
        # 执行回执承载，而不是用一句与卡状态不符的理由把用户挡回去。
        return "card.unlock"

    if action == "card.report_lost":
        if status == "lost":
            raise ScenarioRefused("卡片已正式挂失，不能重复挂失。")
        if card.replaced_by_card_id:
            raise ScenarioRefused("该卡已补卡，旧卡已作废，不能重复挂失。")
        # 对话里没有显式选类型时默认按正式挂失准备；弹窗第 1 步仍可改选临时冻结。
        loss_type = _optional_str(intent, ("loss_type",)) or "formal"
        if loss_type not in ("temporary", "formal"):
            raise ScenarioRefused("挂失类型只能是临时冻结或正式挂失。")
        reason = _optional_str(intent, ("loss_reason", "reason"))
        if loss_type == "temporary":
            # 用户在分支问句里选了「临时冻结」：按 7 天临时冻结出计划，而不是不可逆挂失。
            params["duration_days"] = 7
            if reason is not None:
                params["reason"] = reason
            return "card.lock"
        params["loss_type"] = loss_type
        if reason is not None:
            params["reason"] = reason
        return "card.report_lost"

    if action == "card.replace":
        if card.replaced_by_card_id:
            raise ScenarioRefused(
                f"该卡已补卡（新卡 id：{card.replaced_by_card_id}），不能重复申请。"
            )
        address = as_str(trusted_slot(intent, ("address",), "address"), "address")
        mail_method = as_str(
            trusted_slot(intent, ("mail_method",), "mail_method"), "mail_method"
        )
        with_report = as_str(
            trusted_slot(intent, ("replace", "with_report"), "replace"), "replace"
        )
        if mail_method not in ("standard", "express"):
            raise ScenarioRefused("邮寄方式只能是普通邮寄或加急。")
        if with_report not in ("yes", "no"):
            raise ScenarioRefused("请先确认是否同时正式挂失。")
        params.update(
            {
                "address": address.strip(),
                "mail_method": mail_method,
                "with_report": with_report,
            }
        )
        reason = _optional_str(intent, ("loss_reason", "reason"))
        if reason is not None:
            params["reason"] = reason
        return "card.replace"

    if action == "card.set_limit":
        if card.card_type != "debit":
            raise ScenarioRefused(
                "信用卡没有日限额；请改用「临时额度 / 固定额度 / 账单周期限额」。"
            )
        limit_kind = as_str(
            trusted_slot(intent, ("limit_kind",), "limit_kind"), "limit_kind"
        )
        if limit_kind not in _LIMIT_KINDS:
            raise ScenarioRefused("借记卡限额只能是 ATM 取现 / 转账 / 消费 / 境外。")
        limit = as_money(
            trusted_slot(intent, ("daily_limit", "daily_limit_cents"), "daily_limit"),
            "daily_limit",
        )
        if limit.cents <= 0:
            raise ScenarioRefused("日限额必须为正")
        if limit.cents > CARD_DAILY_LIMIT_MAX_CENTS:
            raise ScenarioRefused(
                f"单通道日限额上限 {Money(CARD_DAILY_LIMIT_MAX_CENTS).yuan} 元，"
                f"暂不支持调整到 {Money(limit.cents).yuan} 元"
            )
        params["limit_kind"] = limit_kind
        params["daily_limit_cents"] = limit.cents
        return "card.set_limit"

    if action == "card.adjust_credit_limit":
        if card.card_type != "credit":
            raise ScenarioRefused("只有信用卡支持信用额度调整。")
        mode = as_str(trusted_slot(intent, ("credit_mode", "mode"), "credit_mode"), "credit_mode")
        if mode not in ("temporary", "permanent"):
            raise ScenarioRefused("额度类型只能是临时额度或固定额度。")
        amount = as_money(
            trusted_slot(intent, ("amount", "amount_cents"), "amount"), "amount"
        )
        if amount.cents <= 0:
            raise ScenarioRefused("额度必须为正")
        if amount.cents > CARD_CREDIT_LIMIT_MAX_CENTS:
            raise ScenarioRefused(
                f"信用卡额度上限 {Money(CARD_CREDIT_LIMIT_MAX_CENTS).yuan} 元，"
                f"暂不支持调整到 {Money(amount.cents).yuan} 元"
            )
        months = _optional_int(intent, ("duration_months",), 1)
        params["mode"] = mode
        params["amount_cents"] = amount.cents
        params["duration_months"] = months
        return "card.adjust_credit_limit"

    if action == "card.set_cycle_limit":
        if card.card_type != "credit":
            raise ScenarioRefused("只有信用卡支持账单周期消费限额。")
        amount = as_money(trusted_slot(intent, ("amount", "amount_cents"), "amount"), "amount")
        if amount.cents <= 0:
            raise ScenarioRefused("账单周期消费限额必须为正")
        if amount.cents > CARD_CREDIT_LIMIT_MAX_CENTS:
            raise ScenarioRefused(
                f"账单周期消费限额上限 {Money(CARD_CREDIT_LIMIT_MAX_CENTS).yuan} 元。"
            )
        params["amount_cents"] = amount.cents
        return "card.set_cycle_limit"

    return action


def _require_card(gateway: FactGateway, card_id: str) -> Card:
    try:
        return gateway.card(card_id).payload
    except GatewayNotFoundError as error:
        raise ScenarioRefused(f"卡片不存在：{card_id}") from error


def _optional_int(intent: Intent, keys: Sequence[str], default: int) -> int:
    """可选整数槽：同时接受槽位名与参数名；缺失 / 非法时回落到默认值。"""
    for key in keys:
        value = intent.slots.get(key)
        if value is None:
            continue
        raw = getattr(value, "raw", value)
        if isinstance(raw, bool) or not isinstance(raw, int):
            continue
        return raw
    return default


def _optional_str(intent: Intent, keys: Sequence[str]) -> str | None:
    """可选文本槽：同时接受槽位名与参数名；缺失 / 空串返回 `None`。"""
    for key in keys:
        value = intent.slots.get(key)
        if value is None:
            continue
        raw = getattr(value, "raw", value)
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    return None


def _card_lock_handler(context: WriteContext) -> WriteReceipt:
    days = context.params.get("duration_days", 7)
    duration = 7 if not isinstance(days, int) else days
    until = (DEFAULT_BOOKING_DATE + timedelta(days=duration)).isoformat()
    reason = context.params.get("reason")
    return context.core_bank.set_card_status(
        card_id=require_str(context.params, "card_id"),
        status="locked",
        temp_lock_until=until,
        reason=reason if isinstance(reason, str) else None,
        occurred_on=DEFAULT_BOOKING_DATE.isoformat(),
        idempotency_key=context.idempotency_key,
        before_write=context.before_write,
    )


def _card_status_handler(status: str) -> WriteHandler:
    """生成把动作映射到 `CoreBank.set_card_status` 的处理器（解挂 / 挂失共用）。"""

    def handle(context: WriteContext) -> WriteReceipt:
        reason = context.params.get("reason")
        return context.core_bank.set_card_status(
            card_id=require_str(context.params, "card_id"),
            status=status,
            reason=reason if isinstance(reason, str) else None,
            occurred_on=DEFAULT_BOOKING_DATE.isoformat(),
            idempotency_key=context.idempotency_key,
            before_write=context.before_write,
        )

    return handle


def _card_replace_handler(context: WriteContext) -> WriteReceipt:
    return context.core_bank.replace_card(
        card_id=require_str(context.params, "card_id"),
        address=require_str(context.params, "address"),
        mail_method=require_str(context.params, "mail_method"),
        with_report=str(context.params.get("with_report", "yes")) == "yes",
        occurred_on=DEFAULT_BOOKING_DATE.isoformat(),
        reason=(
            str(context.params["reason"]) if isinstance(context.params.get("reason"), str) else None
        ),
        idempotency_key=context.idempotency_key,
        before_write=context.before_write,
    )


def _apply_virtual_handler(context: WriteContext) -> WriteReceipt:
    """把 `card.apply_virtual` 映射到 `CoreBank.apply_virtual_card`。"""
    return context.core_bank.apply_virtual_card(
        user_id=require_str(context.params, "user_id"),
        daily_limit=Money(money_cents(context.params, context.spec)),
        idempotency_key=context.idempotency_key,
        before_write=context.before_write,
    )


def _set_limit_handler(context: WriteContext) -> WriteReceipt:
    """借记卡通道限额；未带 `limit_kind` 时按消费通道处理（兼容生活事件 L3）。"""
    kind = context.params.get("limit_kind")
    return context.core_bank.set_card_limit(
        card_id=require_str(context.params, "card_id"),
        daily_limit=Money(money_cents(context.params, context.spec)),
        limit_kind=kind if isinstance(kind, str) else "purchase",
        idempotency_key=context.idempotency_key,
        before_write=context.before_write,
    )


def _cents_param(context: WriteContext, name: str) -> Money:
    value = context.params.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise CoreBankError(f"{name} 必须是整数分")
    return Money(value)


def _credit_limit_handler(context: WriteContext) -> WriteReceipt:
    months = context.params.get("duration_months", 1)
    return context.core_bank.set_credit_limit(
        card_id=require_str(context.params, "card_id"),
        mode=require_str(context.params, "mode"),
        amount=_cents_param(context, "amount_cents"),
        duration_months=months if isinstance(months, int) else 1,
        occurred_on=DEFAULT_BOOKING_DATE.isoformat(),
        idempotency_key=context.idempotency_key,
        before_write=context.before_write,
    )


def _cycle_limit_handler(context: WriteContext) -> WriteReceipt:
    return context.core_bank.set_cycle_limit(
        card_id=require_str(context.params, "card_id"),
        amount=_cents_param(context, "amount_cents"),
        idempotency_key=context.idempotency_key,
        before_write=context.before_write,
    )


def _change_password_handler(context: WriteContext) -> WriteReceipt:
    """把 `password.change` 映射到 `CoreBank.request_password_change`（只登记事件，不碰密码）。"""
    return context.core_bank.request_password_change(
        user_id=require_str(context.params, "user_id"),
        idempotency_key=context.idempotency_key,
        before_write=context.before_write,
    )


WRITE_HANDLERS: Mapping[str, WriteHandler] = {
    "card.lock": _card_lock_handler,
    "card.unlock": _card_status_handler("active"),
    "card.report_lost": _card_status_handler("lost"),
    "card.replace": _card_replace_handler,
    "card.apply_virtual": _apply_virtual_handler,
    "card.set_limit": _set_limit_handler,
    "card.adjust_credit_limit": _credit_limit_handler,
    "card.set_cycle_limit": _cycle_limit_handler,
    "password.change": _change_password_handler,
}
"""卡片域写处理器（组合根汇入 PEP；`docs/已修复清单-2026-10-08.md` 步骤 1.5）。"""


def _card_list(context: QueryContext) -> QueryResult:
    cards = context.gateway.cards_of(require_str(context.params, "user_id"))
    return QueryResult(
        record_ids=tuple(item.record_id for item in cards),
        provenance=tuple((item.record_id, item.content_hash) for item in cards),
        detail=f"共 {len(cards)} 张卡",
    )


QUERY_HANDLERS: Mapping[str, QueryHandler] = {"card.list": _card_list}
"""卡片域**读**处理器（组合根汇入 PEP；`docs/已修复清单-2026-10-08.md` 步骤 4「残留」补做）。"""

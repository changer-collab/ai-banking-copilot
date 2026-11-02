"""订阅场景：取消 = **负未来流出**；竞态走 saga 补偿并**如实告知**（不宣称"已取消"）。

口径（PRD §4.1 场景⑤ / §5.3 / §5.5）：
- 取消订阅的 cf 语义 = `CANCEL_OUTFLOW`（未来流出记负）；
- 已进入结算的竞态**不得谎称已取消**，必须走补偿事务并如实告知"本期已扣、已发起退回"。
"""

from __future__ import annotations

from calendar import monthrange
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from difflib import SequenceMatcher

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
from ai_banking_copilot.gateway.models import Money, Subscription
from ai_banking_copilot.gateway.seed import DEMO_USER_ID
from ai_banking_copilot.planning.anchors import AnchorLedger
from ai_banking_copilot.planning.llm import Intent
from ai_banking_copilot.policy.pep import (
    QueryContext,
    QueryHandler,
    QueryResult,
    WriteContext,
    WriteHandler,
    money_cents,
    require_date,
    require_str,
)
from ai_banking_copilot.policy.saga import ReversalOutcome, ReversalWindow, Saga
from ai_banking_copilot.scenarios.base import (
    ScenarioHandler,
    ScenarioRefused,
    as_date,
    as_money,
    as_str,
    grant_anchor,
    require_trusted,
    trusted_slot,
)
from ai_banking_copilot.scenarios.bill_categories import display_merchant, normalize_merchant

SUBSCRIPTION_ACTIONS = frozenset({"subscription.list", "subscription.cancel", "subscription.sign"})
"""订阅域动作（README 规范动作表 #7 / #12 / #13）。"""

_SIGN_PERIODS = frozenset({"monthly", "yearly"})
"""代扣周期白名单（与 02 目录 `subscription.sign` 的 `period` 约定一致）。"""

SUBSCRIPTION_NOT_OWNED = "订阅不存在或不属于当前用户"
"""取消订阅的统一拒绝文案：**不区分**「不存在」与「他人订阅」，避免跨用户信息泄露。"""


def future_outflow_effect(subscription: Subscription) -> Money:
    """取消订阅的 cf 口径：**负未来流出**（−本期名义额）。"""
    return Money(-subscription.amount.cents)


def resolve_cancel_race(
    window: ReversalWindow, saga: Saga, *, now: datetime, settled: bool
) -> ReversalOutcome:
    """取消竞态：已结算 → 走 saga 补偿并**如实告知**（不宣称"已取消"）。"""
    return window.reverse(now=now, settled=settled, compensate=saga.compensate)


def upcoming_charges(gateway: FactGateway, user_id: str) -> tuple[Subscription, ...]:
    """只读到期提醒事实：仅返回有效订阅，并按 `next_charge_on` 升序。"""
    records = gateway.subscriptions_of(user_id)
    return tuple(
        record.payload
        for record in sorted(records, key=lambda record: record.payload.next_charge_on)
        if record.payload.status == "active" and record.payload.next_charge_on is not None
    )


def describe_upcoming_charges(subscriptions: tuple[Subscription, ...]) -> str:
    """只陈述事实：订阅数 + 最近到期日 + 商户/金额（无收益/推荐措辞，守 H6/H3）。"""
    if not subscriptions:
        return "暂无订阅"
    nearest = subscriptions[0]
    return (
        f"共 {len(subscriptions)} 项，最近到期 {nearest.next_charge_on.isoformat()}"
        f"（{nearest.merchant} {nearest.amount.yuan} 元）"
    )


@dataclass(frozen=True)
class DetectedCharge:
    """从流水**归并识别**出的订阅扣费（同一商户的流出记录聚合）。"""

    merchant: str
    subscription_id: str | None
    txn_count: int
    total: Money
    last_booked_on: date


def detected_charges(gateway: FactGateway, user_id: str) -> tuple[DetectedCharge, ...]:
    """自动识别订阅扣费：把**流出流水**按**归一化商户名**归并（官方场景⑤「自动识别订阅扣费」）。

    判据是归一化后的商户名与已知订阅匹配（去括号备注、符号与大小写差异），仍不是模型推断，
    故可复现、可解释；输出沿用**登记的商户名**，识别不到的订阅一律不返回（不编造扣费）。
    """
    by_merchant = {
        normalize_merchant(record.payload.merchant): record.payload
        for record in gateway.subscriptions_of(user_id)
        if record.payload.user_id == user_id
    }
    if not by_merchant:
        return ()
    grouped: dict[str, list[_Outflow]] = {}
    for flow in _outflows(gateway, user_id, date.max):
        if flow.merchant_key in by_merchant:
            grouped.setdefault(flow.merchant_key, []).append(flow)
    detected = (
        DetectedCharge(
            merchant=by_merchant[key].merchant,
            subscription_id=by_merchant[key].subscription_id,
            txn_count=len(items),
            total=Money(sum(item.amount_cents for item in items)),
            last_booked_on=items[-1].booked_on,
        )
        for key, items in grouped.items()
    )
    return tuple(sorted(detected, key=lambda charge: (-charge.total.cents, charge.merchant)))


def describe_detected_charges(charges: tuple[DetectedCharge, ...]) -> str:
    """只陈述事实：识别到的项数、笔数与合计金额（无推荐/收益措辞，守 H6）。"""
    if not charges:
        return "未从流水中识别到订阅扣费"
    grand_total = Money(sum(charge.total.cents for charge in charges))
    detail = "；".join(
        f"{charge.merchant} {charge.txn_count} 笔合计 {charge.total.yuan} 元"
        f"（最近 {charge.last_booked_on.isoformat()}）"
        for charge in charges
    )
    return f"识别到 {len(charges)} 项订阅扣费，合计 {grand_total.yuan} 元：{detail}"


# --------------------------------------------------------------------- 订阅识别详情

INSIGHT_CONFIRMED = "confirmed"
INSIGHT_SUSPECTED = "suspected"
"""识别状态。`confirmed` **只来自订阅登记事实**；流水推断结果恒为 `suspected`。"""

SUBSCRIPTION_DATA_SOURCE = "内置演示流水（gateway/seed.py 播种，未接入真实账单导入）"
"""订阅识别的数据来源；真实账单导入接通前必须原样展示。"""

CURRENCY_CNY = "CNY"

CYCLE_MONTHLY = "monthly"
CYCLE_YEARLY = "yearly"
CYCLE_UNKNOWN = "unknown"
_CYCLE_LABELS = {CYCLE_MONTHLY: "月度", CYCLE_YEARLY: "年度", CYCLE_UNKNOWN: "无法判断"}

_MONTHLY_GAP_RANGE = (25, 35)
"""月度扣费的间隔区间（天）；超出即认为周期不可判定或已改变。"""

_YEARLY_GAP_RANGE = (350, 380)
"""年度扣费的间隔区间（天）。"""

SUSPECTED_MIN_CHARGES = 2
"""推断为疑似订阅所需的最少期数；单期流水不足以判断周期。"""

AMOUNT_CHANGE_MIN_PERCENT = 10
"""金额变化提示阈值（百分比）。"""

AMOUNT_STABLE_MAX_PERCENT = 10
"""判定「金额稳定」允许的最大相对波动（百分比）。"""

SELF_SERVICE_MAX_CENTS = 30_000
"""疑似订阅的单期中位金额上限（分）：300 元，避免把大额消费误报为订阅。"""

NON_SUBSCRIPTION_CATEGORIES = frozenset(
    {"转账", "交通", "餐饮", "住房", "生活缴费", "收入", "工资"}
)
"""这些分类的常规支出即使周期性出现，也不列为疑似订阅。"""

RENAME_SIMILARITY = 0.75
"""商户改名推断的名称相似度阈值（含包含关系时直接命中）。"""

_SUBSCRIPTION_KEYWORDS = ("会员", "订阅", "自动续费", "vip")

EXTERNAL_CANCEL_NOT_INTEGRATED = "not_integrated"
EXTERNAL_CANCEL_CONFIRMED = "confirmed_by_merchant"
"""真实商户接口的契约值。`CoreBank` 当前**永不返回**它，本地执行只能落在本地模拟态。"""

CANCEL_STATE_GUIDANCE = "guidance"
"""仅给出取消指引（未发起任何请求）。"""

CANCEL_STATE_THIRD_PARTY_PAGE = "third_party_page"
"""已打开/引导到第三方页面，取消动作尚未发生。"""

CANCEL_STATE_REQUESTED = "cancel_requested"
"""已发起取消请求但结果未知（失败、超时、平台不可用均归此类）。"""

CANCEL_STATE_LOCAL_CANCELLED = "local_simulated_cancelled"
"""本地状态已更新为已取消；**外部商户未取消**。"""

CANCEL_STATE_ALREADY_LOCAL = "already_local_cancelled"
"""重复取消：本地已是非有效状态，本次未产生新的副作用。"""

CANCEL_STATE_EXTERNAL_CONFIRMED = "external_confirmed_cancelled"
"""商户侧确认已取消；只有真实接口回传确认才能进入该状态。"""

CANCEL_STATE_LABELS = {
    CANCEL_STATE_GUIDANCE: "仅取消指引（未发起任何请求）",
    CANCEL_STATE_THIRD_PARTY_PAGE: "已引导至第三方页面（取消尚未完成）",
    CANCEL_STATE_REQUESTED: "已发起取消请求（结果未确认）",
    CANCEL_STATE_LOCAL_CANCELLED: "本地已取消（商户未确认）",
    CANCEL_STATE_ALREADY_LOCAL: "本地此前已取消（本次无新操作）",
    CANCEL_STATE_EXTERNAL_CONFIRMED: "商户已确认取消",
}

SUBSCRIPTION_CANCEL_BOUNDARY = (
    "本页关闭的是这张卡对该商户的扣款授权：关闭后银行不再放款；"
    "商户是否继续提供服务、是否停止计费，由商户决定。"
    "本应用尚未接入任何商户的取消接口，商户侧状态需到商户 App 关闭。"
)
"""能力边界，必须随取消结果一起展示。"""

SUBSCRIPTION_CANCEL_BOUNDARY_SHORT = (
    "关闭后银行卡侧不再放款；商户是否继续服务由商户决定。"
)
"""计划卡对象行用的短口径（长口径见 `SUBSCRIPTION_CANCEL_BOUNDARY`）。"""



def cancel_state_from_receipt(*, status: str, external_status: str) -> str:
    """把写回执映射为**互不混用**的取消状态。

    只有回执 `status=="cancelled"` 且外部状态明确为 `confirmed_by_merchant` 时才返回
    `external_confirmed_cancelled`；其余成功一律只算**本地模拟**。失败、超时、平台不可用
    与任何未知状态都映射到 `cancel_requested`，不得表述为"已取消"。
    """
    if status == "already_cancelled":
        return CANCEL_STATE_ALREADY_LOCAL
    if status != "cancelled":
        return CANCEL_STATE_REQUESTED
    if external_status == EXTERNAL_CANCEL_CONFIRMED:
        return CANCEL_STATE_EXTERNAL_CONFIRMED
    return CANCEL_STATE_LOCAL_CANCELLED


@dataclass(frozen=True)
class _Outflow:
    """内部支出流水（金额取正）；对外的带符号金额见 `SubscriptionEvidence`。"""

    txn_id: str
    booked_on: date
    amount_cents: int
    merchant: str
    category: str
    record_id: str
    content_hash: str
    channel: str = ""
    booked_at: str | None = None
    card_tail: str | None = None
    account_id: str | None = None

    @property
    def merchant_key(self) -> str:
        return normalize_merchant(self.merchant)

    @property
    def merchant_label(self) -> str:
        return display_merchant(self.merchant)


@dataclass(frozen=True)
class SubscriptionEvidence:
    """一笔可回读的扣费证据（`record_id` + `content_hash` 供出处校验）。"""

    txn_id: str
    record_id: str
    content_hash: str
    booked_on: date
    amount_cents: int
    """原始带符号金额（支出为负），不做改写。"""
    merchant_label: str
    channel: str = ""
    booked_at: str | None = None
    card_tail: str | None = None
    account_id: str | None = None
    """这笔扣费从哪个账户支出（前端按账户事实映射名称与当前余额，不在前端推导）。"""


@dataclass(frozen=True)
class SubscriptionNotice:
    """识别提示：涨跌、周期变化、改名、数据不足等，必须与结论一起展示。"""

    code: str
    message: str


AGREEMENT_ACTIVE = "active"
AGREEMENT_CANCELLED = "cancelled"
AGREEMENT_SUSPECTED = "suspected"

AGREEMENT_TYPE_LABELS = {
    "unionpay": "银联代扣",
    "netlink": "网联代扣",
    "collection_e": "代收e授权",
    "unknown": "类型未知",
}


@dataclass(frozen=True)
class PricePoint:
    amount_cents: int
    effective_on: date


@dataclass(frozen=True)
class PriceChange:
    from_cents: int
    to_cents: int
    effective_on: date
    percent: Decimal


@dataclass(frozen=True)
class SubscriptionInsight:
    """一条订阅识别结果（已登记订阅或流水推断的疑似订阅）。"""

    insight_id: str
    merchant: str
    subscription_id: str | None
    status: str
    confidence: Decimal
    amount_cents: int
    """最近一期扣费金额（分，正数）；无流水时取登记金额。"""
    currency: str
    cycle: str
    cycle_label: str
    last_charged_on: date | None
    current_period_label: str
    """本期口径：月度 `YYYY-MM`、年度 `YYYY 年`；周期未知时为空串。"""
    charged_this_period: bool | None
    """本期是否已扣款；没有历史流水时为 `None`（未知，不推断）。"""
    next_charge_on: date | None
    next_charge_is_prediction: bool
    next_charge_basis: str
    charge_count: int
    registered_status: str | None
    notices: tuple[SubscriptionNotice, ...]
    evidence: tuple[SubscriptionEvidence, ...]
    suggestion: str
    cancel_action: str
    cancel_path: str
    external_cancel_status: str
    price_increased_12m: bool | None
    price_change_12m_from_cents: int | None
    price_change_12m_to_cents: int | None
    agreement_no: str
    agreement_type: str
    agreement_type_label: str
    agreement_status: str
    signed_on: date | None
    cancelled_on: date | None
    charged_total_cents: int | None
    month_charged_cents: int
    price_history: tuple[PricePoint, ...]
    price_change: PriceChange | None
    next_charge_state: str
    overdue_days: int | None
    card_id: str | None
    card_tail: str | None
    post_cancel_charges: tuple[SubscriptionEvidence, ...]


@dataclass(frozen=True)
class SubscriptionReport:
    """一个用户的订阅识别报告（只读；金额与周期均可由证据重算）。"""

    user_id: str
    as_of: date
    data_source: str
    external_cancel_status: str
    insights: tuple[SubscriptionInsight, ...]
    notes: tuple[str, ...]


def subscription_report(
    gateway: FactGateway, user_id: str, *, as_of: date
) -> SubscriptionReport:
    """识别订阅：已登记订阅 + 流水推断的疑似订阅。只读，不写任何事实。

    - **已登记订阅**：`confirmed`；流水命中用于核对金额 / 周期 / 日期偏移，命中不到则
      如实提示"数据不足"，周期与下次扣费只能取登记值；
    - **未登记商户**：只有 ≥2 期、可判定月/年周期、金额稳定（≤10% 波动）、非排除分类
      且单期中位金额 ≤300 元时才列为 `suspected`，**绝不升级为已确认**；
    - 商户改名按名称包含关系 / 相似度推断，命中后仍为 `suspected` 并给出提示。
    """
    flows = _outflows(gateway, user_id, as_of)
    grouped = _group_outflows(flows)
    pending: set[str] = set(grouped)
    subscriptions = tuple(
        record.payload
        for record in gateway.subscriptions_of(user_id)
        if record.payload.user_id == user_id
    )
    cards = {
        record.payload.card_id: record.payload
        for record in gateway.cards_of(user_id)
    }

    matched: dict[str, tuple[_Outflow, ...]] = {}
    for subscription in subscriptions:
        key = normalize_merchant(subscription.merchant)
        if key in pending:
            matched[subscription.subscription_id] = tuple(grouped[key])
            pending.discard(key)

    insights: list[SubscriptionInsight] = []
    for subscription in subscriptions:
        items = matched.get(subscription.subscription_id, ())
        renamed_from: str | None = None
        if not items and subscription.status == "active":
            current_key = normalize_merchant(subscription.merchant)
            renamed_from = _take_renamed(grouped, pending, current_key)
            if renamed_from is not None:
                items = tuple(grouped[renamed_from])
                pending.discard(renamed_from)
        card = cards.get(subscription.card_id) if subscription.card_id else None
        insights.append(_registered_insight(
            subscription, items, renamed_from=renamed_from, as_of=as_of,
            card_type=card.card_type if card else None,
            card_tail=card.last4 if card else None,
        ))

    notes: list[str] = [
        f"数据来源：{SUBSCRIPTION_DATA_SOURCE}。",
        "流水推断只用于提示：未登记的商户一律标为疑似，需本人核对后再决定是否取消。",
    ]
    for key in sorted(pending):
        pending_items = grouped[key]
        candidate = _unregistered_insight(key, pending_items, as_of=as_of)
        if candidate is not None:
            insights.append(candidate)
        elif _has_subscription_keyword(key) or _has_subscription_keyword(
            pending_items[-1].merchant
        ):
            notes.append(
                f"「{pending_items[-1].merchant_label}」仅有 {len(pending_items)} 期流水，"
                "不足以判断扣费周期，未列为订阅，请自行核对。"
            )
    if not subscriptions:
        notes.append("该用户没有订阅登记，以下结果全部来自流水推断。")

    return SubscriptionReport(
        user_id=user_id,
        as_of=as_of,
        data_source=SUBSCRIPTION_DATA_SOURCE,
        external_cancel_status=EXTERNAL_CANCEL_NOT_INTEGRATED,
        insights=tuple(
            sorted(insights, key=lambda item: (
                0 if item.status == INSIGHT_CONFIRMED else 1, item.merchant, item.insight_id
            ))
        ),
        notes=tuple(notes),
    )


def insights_payload(report: SubscriptionReport) -> dict[str, object]:
    """订阅详情报告的渠道序列化（金额为分，日期为 ISO 字符串）。"""
    return {
        "user_id": report.user_id,
        "as_of": report.as_of.isoformat(),
        "data_source": report.data_source,
        "data_source_is_demo": True,
        "external_cancel_status": report.external_cancel_status,
        "insights": [
            {
                "insight_id": item.insight_id,
                "merchant": item.merchant,
                "subscription_id": item.subscription_id,
                "status": item.status,
                "confidence": str(item.confidence),
                "amount_cents": item.amount_cents,
                "amount_yuan": str(Money(item.amount_cents).yuan),
                "currency": item.currency,
                "cycle": item.cycle,
                "cycle_label": item.cycle_label,
                "last_charged_on": (
                    item.last_charged_on.isoformat() if item.last_charged_on else None
                ),
                "current_period_label": item.current_period_label,
                "charged_this_period": item.charged_this_period,
                "next_charge_on": (
                    item.next_charge_on.isoformat() if item.next_charge_on else None
                ),
                "next_charge_is_prediction": item.next_charge_is_prediction,
                "next_charge_basis": item.next_charge_basis,
                "charge_count": item.charge_count,
                "price_increased_12m": item.price_increased_12m,
                "price_change_12m_from_cents": item.price_change_12m_from_cents,
                "price_change_12m_to_cents": item.price_change_12m_to_cents,
                "registered_status": item.registered_status,
                "notices": [
                    {"code": notice.code, "message": notice.message}
                    for notice in item.notices
                ],
                "evidence": [
                    _evidence_payload(evidence) for evidence in item.evidence
                ],
                "post_cancel_charges": [
                    _evidence_payload(evidence)
                    for evidence in item.post_cancel_charges
                ],
                "suggestion": item.suggestion,
                "cancel_action": item.cancel_action,
                "cancel_path": item.cancel_path,
                "external_cancel_status": item.external_cancel_status,
                "agreement_id": item.insight_id,
                "agreement_no": item.agreement_no,
                "agreement_type": item.agreement_type,
                "agreement_type_label": item.agreement_type_label,
                "agreement_status": item.agreement_status,
                "card_id": item.card_id,
                "card_tail": item.card_tail,
                "signed_on": item.signed_on.isoformat() if item.signed_on else None,
                "cancelled_on": (
                    item.cancelled_on.isoformat() if item.cancelled_on else None
                ),
                "charged_total_cents": item.charged_total_cents,
                "month_charged_cents": item.month_charged_cents,
                "price_history": [
                    {
                        "amount_cents": point.amount_cents,
                        "effective_on": point.effective_on.isoformat(),
                    }
                    for point in item.price_history
                ],
                "price_change": (
                    {
                        "from_cents": item.price_change.from_cents,
                        "to_cents": item.price_change.to_cents,
                        "effective_on": item.price_change.effective_on.isoformat(),
                        "percent": str(item.price_change.percent),
                    }
                    if item.price_change else None
                ),
                "next_charge_state": item.next_charge_state,
                "overdue_days": item.overdue_days,
            }
            for item in report.insights
        ],
        "summary": _agreement_summary(report),
        "notes": list(report.notes),
    }


def describe_agreements_facts(gateway: FactGateway, user_id: str) -> str:
    report = subscription_report(gateway, user_id, as_of=DEFAULT_BOOKING_DATE)
    active = [
        item for item in report.insights if item.agreement_status == AGREEMENT_ACTIVE
    ]
    if not active:
        return "暂无在签代扣协议；" + SUBSCRIPTION_CANCEL_BOUNDARY_SHORT
    rows = "；".join(
        "".join(
            (
                f"{item.merchant}（{item.agreement_type_label}，协议 {item.agreement_no}"
                "（演示派生），"
                f"已扣 {item.charge_count} 期",
                f" {Money(item.charged_total_cents).yuan} 元"
                if item.charged_total_cents is not None
                else "",
                f"，下次 {item.next_charge_on.isoformat() if item.next_charge_on else '未知'}）",
            )
        )
        for item in active[:3]
    )
    more = f" 等 {len(active)} 项" if len(active) > 3 else ""
    return f"代扣协议：{rows}{more}；{SUBSCRIPTION_CANCEL_BOUNDARY_SHORT}"


def describe_agreement_facts(
    gateway: FactGateway, user_id: str, subscription_id: str
) -> str:
    report = subscription_report(gateway, user_id, as_of=DEFAULT_BOOKING_DATE)
    item = next(
        (row for row in report.insights if row.subscription_id == subscription_id),
        None,
    )
    if item is None:
        return f"代扣协议 {subscription_id}"
    total = (
        f"已扣 {item.charge_count} 期 {Money(item.charged_total_cents).yuan} 元"
        if item.charged_total_cents is not None
        else "历史扣款数据不足"
    )
    signed = item.signed_on.isoformat() if item.signed_on else "签约时间未知"
    return (
        f"{item.merchant} · {item.agreement_type_label} · 协议 {item.agreement_no}"
        "（演示派生） · "
        f"{signed} 签约 · {total}；{SUBSCRIPTION_CANCEL_BOUNDARY_SHORT}"
    )


def _evidence_payload(evidence: SubscriptionEvidence) -> dict[str, object]:
    return {
        "txn_id": evidence.txn_id,
        "record_id": evidence.record_id,
        "content_hash": evidence.content_hash,
        "booked_on": evidence.booked_on.isoformat(),
        "booked_at": evidence.booked_at,
        "card_tail": evidence.card_tail,
        "amount_cents": evidence.amount_cents,
        "merchant_label": evidence.merchant_label,
        "channel": evidence.channel,
        "account_id": evidence.account_id,
    }


def _agreement_summary(report: SubscriptionReport) -> dict[str, object]:
    as_of = report.as_of
    month_start = as_of.replace(day=1)
    month_end = as_of.replace(
        day=monthrange(as_of.year, as_of.month)[1]
    )
    active = [item for item in report.insights if item.agreement_status == AGREEMENT_ACTIVE]
    due = [
        item for item in active
        if item.next_charge_on is not None
        and as_of <= item.next_charge_on <= month_end
    ]
    next_30 = [
        item for item in active
        if item.next_charge_on is not None
        and as_of <= item.next_charge_on <= as_of + timedelta(days=30)
    ]
    return {
        "month_label": as_of.strftime("%Y-%m"),
        # 本月已扣包含已解约与疑似项（真实已发生的扣款）；待扣/30 天只算 active，口径不同是有意的。
        "month_charged_cents": sum(item.month_charged_cents for item in report.insights),
        "month_due_cents": sum(item.amount_cents for item in due),
        "next_30d_cents": sum(item.amount_cents for item in next_30),
        "active_count": len(active),
        "suspected_count": sum(
            1 for item in report.insights if item.agreement_status == AGREEMENT_SUSPECTED
        ),
        "cancelled_count": sum(
            1 for item in report.insights if item.agreement_status == AGREEMENT_CANCELLED
        ),
        "month_start": month_start.isoformat(),
    }


def _current_period(
    cycle: str, as_of: date, last_charged_on: date | None
) -> tuple[str, bool | None]:
    """本期是否已扣：月度按自然月、年度按自然年；无历史扣费时如实返回未知。"""
    if cycle == CYCLE_MONTHLY:
        label = as_of.strftime("%Y-%m")
        charged = (
            None if last_charged_on is None
            else last_charged_on.strftime("%Y-%m") == label
        )
        return label, charged
    if cycle == CYCLE_YEARLY:
        label = f"{as_of.year} 年"
        charged = None if last_charged_on is None else last_charged_on.year == as_of.year
        return label, charged
    return "", None


def _registered_insight(
    subscription: Subscription,
    items: Sequence[_Outflow],
    *,
    renamed_from: str | None,
    as_of: date,
    card_type: str | None,
    card_tail: str | None,
) -> SubscriptionInsight:
    """已登记订阅：状态恒为 `confirmed`，但流水与登记不一致时必须逐条提示。"""
    gaps = _gaps([item.booked_on for item in items])
    detected_cycle, median_gap = _infer_cycle(gaps)
    cycle = detected_cycle if detected_cycle != CYCLE_UNKNOWN else _registered_cycle(subscription)
    notices: list[SubscriptionNotice] = []

    if not items:
        notices.append(SubscriptionNotice(
            "insufficient-data",
            "没有找到与该订阅对应的流水，金额、周期与下次扣费只能采用登记值。",
        ))
    elif len(items) < 3:
        notices.append(SubscriptionNotice(
            "insufficient-data",
            f"仅找到 {len(items)} 期流水，不足以确认周期与金额是否稳定。",
        ))
    if detected_cycle == CYCLE_UNKNOWN and gaps:
        notices.append(SubscriptionNotice(
            "cycle-unknown",
            f"历史扣费间隔中位数为 {median_gap} 天，无法判定月度或年度周期。",
        ))
    if renamed_from is not None:
        notices.append(SubscriptionNotice(
            "merchant-renamed",
            f"流水中的商户名称为「{items[-1].merchant_label}」，与登记的"
            f"「{subscription.merchant}」不一致；按名称相似度推断为同一订阅，需人工核对。",
        ))

    amount_cents = items[-1].amount_cents if items else subscription.amount.cents
    if len(items) >= 2:
        notice = _amount_change_notice([item.amount_cents for item in items])
        if notice is not None:
            notices.append(notice)
    if items and amount_cents != subscription.amount.cents:
        notices.append(SubscriptionNotice(
            "registered-amount-mismatch",
            f"登记金额 {subscription.amount.yuan} 元与最近一期实际扣费"
            f" {Money(amount_cents).yuan} 元不一致。",
        ))
    if items and subscription.status == "active":
        offset = abs(items[-1].booked_on.day - subscription.next_charge_on.day)
        if offset > 3:
            notices.append(SubscriptionNotice(
                "date-offset",
                f"登记的下次扣费日为 {subscription.next_charge_on.isoformat()}，"
                f"流水实际扣费集中在每月 {items[-1].booked_on.day} 日，偏差约 {offset} 天。",
            ))
    if len(gaps) >= 2 and detected_cycle != CYCLE_UNKNOWN:
        band = _MONTHLY_GAP_RANGE if detected_cycle == CYCLE_MONTHLY else _YEARLY_GAP_RANGE
        if not band[0] <= gaps[-1] <= band[1]:
            notices.append(SubscriptionNotice(
                "cycle-change",
                f"最近一次扣费间隔 {gaps[-1]} 天，与历史中位间隔 {median_gap} 天不一致，"
                "扣费周期可能已改变。",
            ))

    raw_next, is_prediction, basis = _next_charge(
        subscription, items, cycle=cycle, median_gap=median_gap
    )
    next_charge_on, overdue_days = (
        _advance_until_future(raw_next, cycle, as_of) if raw_next else (None, None)
    )
    if raw_next and next_charge_on != raw_next:
        was_prediction = is_prediction
        is_prediction = True
        basis = basis + (
            "已按扣费周期顺延到未来日期。"
            if was_prediction
            else "登记日期已过，已按扣费周期顺延到未来日期。"
        )
    active = subscription.status == "active"
    if not active:
        suggestion = "订阅已不在有效状态，无需取消；如仍有扣费请核对流水。"
    elif any(notice.code == "amount-change" for notice in notices):
        suggestion = "先核对调价通知与使用情况；如不再使用，可在订阅管理中取消。"
    else:
        suggestion = "如仍在使用可保留；不再使用可在订阅管理中取消。"

    increased, price_from, price_to = _price_change_12m(items, as_of)
    period_label, charged_this_period = _current_period(
        cycle, as_of, items[-1].booked_on if items else None
    )
    history = _price_history(items)
    price_change = _latest_price_change(history)
    charged_total = sum(item.amount_cents for item in items) if items else None
    month_charged = sum(
        item.amount_cents for item in items
        if item.booked_on.strftime("%Y-%m") == as_of.strftime("%Y-%m")
    )
    cancelled_on = subscription.cancelled_on
    post_cancel = tuple(
        _evidence(item) for item in items
        if cancelled_on is not None and item.booked_on > cancelled_on
    )
    if subscription.status != "active":
        next_state = "cancelled"
    elif overdue_days:
        next_state = "overdue"
    elif charged_this_period:
        next_state = "charged"
    elif next_charge_on == as_of:
        next_state = "due"
    elif next_charge_on:
        next_state = "scheduled"
    else:
        next_state = "unknown"
    agreement_status = (
        AGREEMENT_ACTIVE if subscription.status == "active" else AGREEMENT_CANCELLED
    )
    type_code = agreement_type(card_type, registered=True)
    type_label = AGREEMENT_TYPE_LABELS[type_code]
    return SubscriptionInsight(
        insight_id=f"subscription:{subscription.subscription_id}",
        merchant=subscription.merchant,
        subscription_id=subscription.subscription_id,
        status=INSIGHT_SUSPECTED if renamed_from is not None else INSIGHT_CONFIRMED,
        confidence=_registered_confidence(items, renamed=renamed_from is not None),
        amount_cents=amount_cents,
        currency=CURRENCY_CNY,
        cycle=cycle,
        cycle_label=_CYCLE_LABELS[cycle],
        last_charged_on=items[-1].booked_on if items else None,
        current_period_label=period_label,
        charged_this_period=charged_this_period,
        next_charge_on=next_charge_on,
        next_charge_is_prediction=is_prediction,
        next_charge_basis=basis,
        charge_count=len(items),
        registered_status=subscription.status,
        notices=tuple(notices),
        evidence=tuple(_evidence(item) for item in items[-12:]),
        suggestion=suggestion,
        cancel_action="app_local_cancel" if active else "none",
        cancel_path=(
            "本应用「代扣协议管理」→ 关闭扣款协议（关闭银行卡侧授权）"
            if active else "协议已解约，无需关闭"
        ),
        external_cancel_status=EXTERNAL_CANCEL_NOT_INTEGRATED,
        price_increased_12m=increased,
        price_change_12m_from_cents=price_from,
        price_change_12m_to_cents=price_to,
        agreement_no=agreement_no(subscription.subscription_id),
        agreement_type=type_code,
        agreement_type_label=type_label,
        agreement_status=agreement_status,
        signed_on=items[0].booked_on if items else None,
        cancelled_on=cancelled_on,
        charged_total_cents=charged_total,
        month_charged_cents=month_charged,
        price_history=history,
        price_change=price_change,
        next_charge_state=next_state,
        overdue_days=overdue_days,
        card_id=subscription.card_id,
        card_tail=card_tail,
        post_cancel_charges=post_cancel,
    )


def _unregistered_insight(
    key: str, items: Sequence[_Outflow], *, as_of: date,
) -> SubscriptionInsight | None:
    """未登记商户：只有证据足够时才列为**疑似**订阅，否则返回 `None`（由调用方提示）。"""
    if not _subscription_like(items):
        return None
    amounts = [item.amount_cents for item in items]
    median_amount = _median_int(amounts)
    if median_amount <= 0 or median_amount > SELF_SERVICE_MAX_CENTS:
        return None
    if (max(amounts) - min(amounts)) * 100 > median_amount * AMOUNT_STABLE_MAX_PERCENT:
        return None
    gaps = _gaps([item.booked_on for item in items])
    cycle, median_gap = _infer_cycle(gaps)
    notices: list[SubscriptionNotice] = [SubscriptionNotice(
        "limited-history",
        f"仅 {len(items)} 期流水，按间隔推断为{_CYCLE_LABELS[cycle]}扣费，属疑似订阅。",
    )]
    amount_notice = _amount_change_notice(amounts)
    if amount_notice is not None:
        notices.append(amount_notice)
    notices.append(SubscriptionNotice(
        "unregistered",
        "该商户未在订阅登记中，本应用无法直接取消，只能提供商户自助取消指引。",
    ))
    latest = items[-1]
    increased, price_from, price_to = _price_change_12m(items, as_of)
    period_label, charged_this_period = _current_period(cycle, as_of, latest.booked_on)
    history = _price_history(items)
    price_change = _latest_price_change(history)
    next_on = (
        latest.booked_on + timedelta(days=median_gap) if median_gap is not None else None
    )
    next_on, overdue_days = (
        _advance_until_future(next_on, cycle, as_of) if next_on else (None, None)
    )
    month_charged = sum(
        item.amount_cents for item in items
        if item.booked_on.strftime("%Y-%m") == as_of.strftime("%Y-%m")
    )
    return SubscriptionInsight(
        insight_id=f"outflow:{key}",
        merchant=latest.merchant_label,
        subscription_id=None,
        status=INSIGHT_SUSPECTED,
        confidence=Decimal("0.60") if len(items) >= 3 else Decimal("0.50"),
        amount_cents=latest.amount_cents,
        currency=CURRENCY_CNY,
        cycle=cycle,
        cycle_label=_CYCLE_LABELS[cycle],
        last_charged_on=latest.booked_on,
        current_period_label=period_label,
        charged_this_period=charged_this_period,
        next_charge_on=next_on,
        next_charge_is_prediction=True,
        next_charge_basis=(
            f"按最近 {len(gaps)} 次扣费间隔的中位数 {median_gap} 天推得，仅作预测；"
            "实际扣费日以商户为准。"
        ),
        charge_count=len(items),
        registered_status=None,
        notices=tuple(notices),
        evidence=tuple(_evidence(item) for item in items[-12:]),
        suggestion="核对是否本人开通；如非本人操作或不再使用，请到该商户的自动续费管理中关闭。",
        cancel_action="merchant_self_service",
        cancel_path=(
            "未匹配到本行代扣协议；请到商户 App/官网的自动续费管理关闭，"
            "或联系发卡行核对是否来自其他卡"
        ),
        external_cancel_status=EXTERNAL_CANCEL_NOT_INTEGRATED,
        price_increased_12m=increased,
        price_change_12m_from_cents=price_from,
        price_change_12m_to_cents=price_to,
        agreement_no="",
        agreement_type="collection_e",
        agreement_type_label=AGREEMENT_TYPE_LABELS["collection_e"],
        agreement_status=AGREEMENT_SUSPECTED,
        signed_on=items[0].booked_on,
        cancelled_on=None,
        charged_total_cents=sum(item.amount_cents for item in items),
        month_charged_cents=month_charged,
        price_history=history,
        price_change=price_change,
        next_charge_state=(
            "overdue" if overdue_days else "scheduled" if next_on else "unknown"
        ),
        overdue_days=overdue_days,
        card_id=None,
        card_tail=latest.card_tail,
        post_cancel_charges=(),
    )


def _twelve_months_before(day: date) -> date:
    """回退十二个日历月；闰日落到上一年二月末。"""
    year = day.year - 1
    return day.replace(year=year, day=min(day.day, monthrange(year, day.month)[1]))


def _price_change_12m(
    items: Sequence[_Outflow], as_of: date,
) -> tuple[bool | None, int | None, int | None]:
    """仅在十二个月闭区间内比较首末扣费；不足两笔时结论未知。"""
    start = _twelve_months_before(as_of)
    window = [item for item in items if start <= item.booked_on <= as_of]
    if len(window) < 2:
        return None, None, None
    first, last = window[0].amount_cents, window[-1].amount_cents
    return last > first, first, last


def _next_charge(
    subscription: Subscription,
    items: Sequence[_Outflow],
    *,
    cycle: str,
    median_gap: int | None,
) -> tuple[date | None, bool, str]:
    """下次扣费：有流水依据时**标注为预测值**，否则退回登记日期并说明非预测。"""
    if subscription.status != "active":
        return None, False, "订阅已不在有效状态，无下次扣费。"
    if items and cycle != CYCLE_UNKNOWN and median_gap is not None:
        return (
            items[-1].booked_on + timedelta(days=median_gap),
            True,
            f"按最近 {len(items) - 1} 次扣费间隔的中位数 {median_gap} 天推得，"
            "仅作预测；实际扣费日以商户为准。",
        )
    reason = (
        "流水间隔无法判定周期，改用登记值"
        if items else "无流水可核验，取登记值"
    )
    return (
        subscription.next_charge_on,
        False,
        f"取订阅登记中的下次扣费日（{reason}，非预测值）。",
    )


def agreement_no(subscription_id: str) -> str:
    from hashlib import sha256

    return "DD-" + sha256(subscription_id.encode("utf-8")).hexdigest()[:6].upper()


def agreement_type(card_type: str | None, *, registered: bool) -> str:
    if not registered:
        return "collection_e"
    if card_type == "debit":
        return "unionpay"
    if card_type == "credit":
        return "netlink"
    return "unknown"


def _price_history(items: Sequence[_Outflow]) -> tuple[PricePoint, ...]:
    points: list[PricePoint] = []
    for item in items:
        if not points or points[-1].amount_cents != item.amount_cents:
            points.append(PricePoint(item.amount_cents, item.booked_on))
    return tuple(points)


def _latest_price_change(history: tuple[PricePoint, ...]) -> PriceChange | None:
    if len(history) < 2:
        return None
    previous, latest = history[-2], history[-1]
    if latest.amount_cents <= previous.amount_cents:
        return None
    percent = (
        Decimal(latest.amount_cents - previous.amount_cents) * 100
        / Decimal(previous.amount_cents)
    ).quantize(Decimal("0.1"))
    return PriceChange(previous.amount_cents, latest.amount_cents, latest.effective_on, percent)


def _add_cycle(day: date, cycle: str) -> date:
    if cycle == CYCLE_MONTHLY:
        year, month = (day.year + 1, 1) if day.month == 12 else (day.year, day.month + 1)
        return date(year, month, min(day.day, monthrange(year, month)[1]))
    if cycle == CYCLE_YEARLY:
        year = day.year + 1
        return date(year, day.month, min(day.day, monthrange(year, day.month)[1]))
    return day + timedelta(days=30)


def _advance_until_future(day: date, cycle: str, as_of: date) -> tuple[date, int | None]:
    if day >= as_of:
        return day, None
    cursor = day
    guard = 0
    while cursor < as_of and guard < 120:
        cursor = _add_cycle(cursor, cycle)
        guard += 1
    return cursor, max((as_of - day).days, 0)


def _amount_change_notice(amounts: Sequence[int]) -> SubscriptionNotice | None:
    """金额变化提示：最近一期相对此前中位值变化 ≥10% 时给出。"""
    if len(amounts) < 2:
        return None
    baseline = _median_int(amounts[:-1])
    latest = amounts[-1]
    if baseline <= 0 or latest == baseline:
        return None
    change = Decimal(latest - baseline) * 100 / Decimal(baseline)
    if abs(change) < AMOUNT_CHANGE_MIN_PERCENT:
        return None
    return SubscriptionNotice(
        "amount-change",
        f"单期扣费由 {Money(baseline).yuan} 元变为 {Money(latest).yuan} 元"
        f"（{change:+.1f}%），请核对是否已同意调价。",
    )


def _registered_confidence(items: Sequence[_Outflow], *, renamed: bool) -> Decimal:
    if renamed:
        return Decimal("0.70")
    if not items:
        return Decimal("0.90")
    return Decimal("0.95") if len(items) >= 3 else Decimal("0.85")


def _registered_cycle(subscription: Subscription) -> str:
    if subscription.period in (CYCLE_MONTHLY, CYCLE_YEARLY):
        return subscription.period
    return CYCLE_UNKNOWN


def _outflows(gateway: FactGateway, user_id: str, as_of: date) -> tuple[_Outflow, ...]:
    """汇总 `as_of` 之前的支出流水；同 `txn_id` 只保留一次（跨账户亦不重复）。"""
    collected: dict[str, _Outflow] = {}
    for account in gateway.accounts_of(user_id):
        for record in gateway.transactions_between(account.payload.account_id, date.min, as_of):
            transaction = record.payload
            if (
                transaction.user_id != user_id
                or transaction.direction != "out"
                or transaction.amount.cents >= 0
                or transaction.booked_on > as_of
            ):
                continue
            collected[transaction.txn_id] = _Outflow(
                txn_id=transaction.txn_id,
                booked_on=transaction.booked_on,
                amount_cents=-transaction.amount.cents,
                merchant=transaction.merchant,
                category=transaction.category,
                record_id=record.record_id,
                content_hash=record.content_hash,
                channel=transaction.channel,
                booked_at=transaction.booked_at,
                card_tail=transaction.card_tail,
                account_id=account.payload.account_id,
            )
    return tuple(sorted(
        collected.values(),
        key=lambda item: (item.booked_on, item.booked_at or "", item.txn_id),
    ))


def _group_outflows(flows: Sequence[_Outflow]) -> dict[str, list[_Outflow]]:
    grouped: dict[str, list[_Outflow]] = {}
    for flow in flows:
        grouped.setdefault(flow.merchant_key, []).append(flow)
    return grouped


def _take_renamed(
    grouped: Mapping[str, Sequence[_Outflow]], pending: set[str], registered_key: str
) -> str | None:
    """在未匹配的流水组里找**改名后的同一商户**（相似度最高者，须够像且像订阅）。"""
    best_key: str | None = None
    best_score = 0.0
    for key in sorted(pending):
        if not _subscription_like(grouped[key]):
            continue
        score = _rename_score(registered_key, key)
        if score > best_score:
            best_key, best_score = key, score
    return best_key if best_score >= RENAME_SIMILARITY else None


def _rename_score(registered_key: str, flow_key: str) -> float:
    if len(registered_key) < 3 or len(flow_key) < 3:
        return 0.0
    if registered_key in flow_key or flow_key in registered_key:
        return 1.0
    return SequenceMatcher(None, registered_key, flow_key).ratio()


def _subscription_like(items: Sequence[_Outflow]) -> bool:
    """是否具备订阅特征：≥2 期、非排除分类、间隔可判定月/年周期。"""
    if len(items) < SUSPECTED_MIN_CHARGES:
        return False
    if items[-1].category in NON_SUBSCRIPTION_CATEGORIES:
        return False
    cycle, _ = _infer_cycle(_gaps([item.booked_on for item in items]))
    return cycle != CYCLE_UNKNOWN


def _infer_cycle(gaps: Sequence[int]) -> tuple[str, int | None]:
    """按间隔中位数推断周期；无法归类时返回 `unknown`（不猜）。"""
    if not gaps:
        return CYCLE_UNKNOWN, None
    median_gap = _median_int(gaps)
    if _MONTHLY_GAP_RANGE[0] <= median_gap <= _MONTHLY_GAP_RANGE[1]:
        return CYCLE_MONTHLY, median_gap
    if _YEARLY_GAP_RANGE[0] <= median_gap <= _YEARLY_GAP_RANGE[1]:
        return CYCLE_YEARLY, median_gap
    return CYCLE_UNKNOWN, median_gap


def _gaps(dates: Sequence[date]) -> tuple[int, ...]:
    return tuple(
        (later - earlier).days for earlier, later in zip(dates, dates[1:], strict=False)
    )


def _median_int(values: Sequence[int]) -> int:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) // 2


def _evidence(flow: _Outflow) -> SubscriptionEvidence:
    return SubscriptionEvidence(
        txn_id=flow.txn_id,
        record_id=flow.record_id,
        content_hash=flow.content_hash,
        booked_on=flow.booked_on,
        amount_cents=-flow.amount_cents,
        merchant_label=flow.merchant_label,
        channel=flow.channel,
        booked_at=flow.booked_at,
        card_tail=flow.card_tail,
        account_id=flow.account_id,
    )


def _has_subscription_keyword(text: str) -> bool:
    lowered = text.lower()
    return any(keyword in lowered for keyword in _SUBSCRIPTION_KEYWORDS)


class SubscriptionsHandler(ScenarioHandler):
    """订阅场景处理器。"""

    @property
    def name(self) -> str:
        return "subscriptions"

    def matches(self, intents: Sequence[Intent]) -> bool:
        return any(intent.action in SUBSCRIPTION_ACTIONS for intent in intents)

    def build(self, intents: Sequence[Intent], gateway: FactGateway) -> Plan:
        subscription_intents = [item for item in intents if item.action in SUBSCRIPTION_ACTIONS]
        if not subscription_intents:
            raise ScenarioRefused("未识别到订阅意图")
        ledger = AnchorLedger()
        nodes = tuple(
            _build_subscription_node(f"n{index}", intent, gateway, ledger)
            for index, intent in enumerate(subscription_intents, start=1)
        )
        plan = Plan(plan_id=f"subscriptions_{len(nodes)}", nodes=nodes)
        validate_plan(plan)
        return plan


def _build_subscription_node(
    node_id: str, intent: Intent, gateway: FactGateway, ledger: AnchorLedger
) -> PlanNode:
    detail = f"订阅操作：{intent.evidence or intent.action}"
    anchor_id = grant_anchor(ledger, AnchorSource.EXPLICIT, detail)
    user_id = _subscription_user_id(intent)
    if intent.action == "subscription.list":
        params: dict[str, object] = {"user_id": user_id}
    elif intent.action == "subscription.sign":
        params = _sign_params(intent, user_id=user_id)
    else:
        subscription_id = _subscription_id(intent)
        _require_subscription(gateway, subscription_id, user_id)
        params = {"user_id": user_id, "subscription_id": subscription_id}
    return PlanNode(
        node_id=node_id,
        action=intent.action,
        params=params,
        intent=IntentRef(source=AnchorSource.EXPLICIT, anchor_id=anchor_id),
    )


def _sign_params(intent: Intent, *, user_id: str | None = None) -> dict[str, object]:
    """代扣签约装配（README 裁决 #17）：对象是**商户**，不涉及已有订阅 id。"""
    resolved_user_id = user_id or _subscription_user_id(intent)
    period = as_str(trusted_slot(intent, ("period",), "period"), "period")
    if period not in _SIGN_PERIODS:
        raise ScenarioRefused(f"代扣周期只支持 {sorted(_SIGN_PERIODS)}，收到 {period}")
    return {
        "user_id": resolved_user_id,
        "merchant": as_str(trusted_slot(intent, ("merchant",), "merchant"), "merchant"),
        "amount_cents": as_money(
            trusted_slot(intent, ("amount", "amount_cents"), "amount"), "amount"
        ).cents,
        "period": period,
        "next_charge_on": as_date(
            trusted_slot(intent, ("next_charge_on", "when"), "next_charge_on"),
            "next_charge_on",
        ).isoformat(),
    }


def _subscription_id(intent: Intent) -> str:
    return as_str(
        trusted_slot(intent, ("subscription", "subscription_id"), "subscription"),
        "subscription",
    )


def _subscription_user_id(intent: Intent) -> str:
    """取订阅归属身份：**只认服务端注入的可信控制槽**，缺失时回退演示身份。

    会话层 `_per_intent_selected` 恒以服务端身份覆盖 `user_id`，故这里的取值不来自请求体；
    若槽位被标为 `unverified`，`require_trusted` 直接转澄清（fail-closed）。
    """
    if "user_id" not in intent.slots:
        return DEMO_USER_ID
    return as_str(require_trusted(intent.slots["user_id"], "user_id"), "user_id")


def _require_subscription(gateway: FactGateway, subscription_id: str, user_id: str) -> None:
    """取消前置：订阅必须存在**且归属当前用户**；两种失败返回同一文案（G-01）。"""
    try:
        record = gateway.subscription(subscription_id)
    except GatewayNotFoundError as error:
        raise ScenarioRefused(SUBSCRIPTION_NOT_OWNED) from error
    if record.payload.user_id != user_id:
        raise ScenarioRefused(SUBSCRIPTION_NOT_OWNED)


def _cancel_handler(context: WriteContext) -> WriteReceipt:
    """把 `subscription.cancel` 映射到 `CoreBank.cancel_subscription`（带归属身份）。"""
    return context.core_bank.cancel_subscription(
        subscription_id=require_str(context.params, "subscription_id"),
        user_id=require_str(context.params, "user_id"),
        idempotency_key=context.idempotency_key,
        before_write=context.before_write,
    )


def _sign_handler(context: WriteContext) -> WriteReceipt:
    """把 `subscription.sign` 映射到 `CoreBank.sign_subscription`。"""
    return context.core_bank.sign_subscription(
        user_id=require_str(context.params, "user_id"),
        merchant=require_str(context.params, "merchant"),
        amount=Money(money_cents(context.params, context.spec)),
        period=require_str(context.params, "period"),
        next_charge_on=require_date(context.params, "next_charge_on"),
        idempotency_key=context.idempotency_key,
        before_write=context.before_write,
    )


WRITE_HANDLERS: Mapping[str, WriteHandler] = {
    "subscription.cancel": _cancel_handler,
    "subscription.sign": _sign_handler,
}
"""订阅域写处理器（组合根汇入 PEP；`docs/已修复清单-2026-10-08.md` 步骤 1.5）。"""


def _subscription_list(context: QueryContext) -> QueryResult:
    user_id = require_str(context.params, "user_id")
    subscriptions = context.gateway.subscriptions_of(user_id)
    upcoming = upcoming_charges(context.gateway, user_id)
    # 与计划卡对象行同源（都取有效且未到期的订阅），避免同一回答里出现两个不一致的总数。
    detail = f"有效订阅 {len(upcoming)} 项"
    if len(upcoming) != len(subscriptions):
        detail += f"（全部 {len(subscriptions)} 项，含已取消）"
    return QueryResult(
        record_ids=tuple(item.record_id for item in subscriptions),
        provenance=tuple((item.record_id, item.content_hash) for item in subscriptions),
        detail=detail,
    )


QUERY_HANDLERS: Mapping[str, QueryHandler] = {"subscription.list": _subscription_list}
"""订阅域**读**处理器（组合根汇入 PEP；`docs/已修复清单-2026-10-08.md` 步骤 4「残留」补做）。"""

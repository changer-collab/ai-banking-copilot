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
from ai_banking_copilot.gateway.core_bank import WriteReceipt
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
from ai_banking_copilot.scenarios.bill_findings import display_merchant, normalize_merchant

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
    """只读到期提醒事实：按 `next_charge_on` **升序**返回用户全部订阅。"""
    records = gateway.subscriptions_of(user_id)
    return tuple(
        record.payload
        for record in sorted(records, key=lambda record: record.payload.next_charge_on)
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
    {"转账", "交通", "餐饮", "生活缴费", "收入", "工资"}
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
    "本应用尚未接入任何商户的取消接口：取消操作只更新本应用内的订阅状态，"
    "不会通知商户，也不代表商户侧已停止扣费。"
)
"""能力边界，必须随取消结果一起展示。"""



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


@dataclass(frozen=True)
class SubscriptionNotice:
    """识别提示：涨跌、周期变化、改名、数据不足等，必须与结论一起展示。"""

    code: str
    message: str


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
        insights.append(_registered_insight(
            subscription, items, renamed_from=renamed_from, as_of=as_of,
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
                    {
                        "txn_id": evidence.txn_id,
                        "record_id": evidence.record_id,
                        "content_hash": evidence.content_hash,
                        "booked_on": evidence.booked_on.isoformat(),
                        "booked_at": evidence.booked_at,
                        "card_tail": evidence.card_tail,
                        "amount_cents": evidence.amount_cents,
                        "merchant_label": evidence.merchant_label,
                        "channel": evidence.channel,
                    }
                    for evidence in item.evidence
                ],
                "suggestion": item.suggestion,
                "cancel_action": item.cancel_action,
                "cancel_path": item.cancel_path,
                "external_cancel_status": item.external_cancel_status,
            }
            for item in report.insights
        ],
        "notes": list(report.notes),
    }


def _registered_insight(
    subscription: Subscription,
    items: Sequence[_Outflow],
    *,
    renamed_from: str | None,
    as_of: date,
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

    next_charge_on, is_prediction, basis = _next_charge(
        subscription, items, cycle=cycle, median_gap=median_gap
    )
    active = subscription.status == "active"
    if not active:
        suggestion = "订阅已不在有效状态，无需取消；如仍有扣费请核对流水。"
    elif any(notice.code == "amount-change" for notice in notices):
        suggestion = "先核对调价通知与使用情况；如不再使用，可在订阅管理中取消。"
    else:
        suggestion = "如仍在使用可保留；不再使用可在订阅管理中取消。"

    increased, price_from, price_to = _price_change_12m(items, as_of)
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
        next_charge_on=next_charge_on,
        next_charge_is_prediction=is_prediction,
        next_charge_basis=basis,
        charge_count=len(items),
        registered_status=subscription.status,
        notices=tuple(notices),
        evidence=tuple(_evidence(item) for item in items[-6:]),
        suggestion=suggestion,
        cancel_action="app_local_cancel" if active else "none",
        cancel_path=(
            "本应用「订阅管理」→ 取消订阅（当前仅更新本地订阅状态，不通知商户）"
            if active else "订阅已不在有效状态，无需取消"
        ),
        external_cancel_status=EXTERNAL_CANCEL_NOT_INTEGRATED,
        price_increased_12m=increased,
        price_change_12m_from_cents=price_from,
        price_change_12m_to_cents=price_to,
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
        next_charge_on=(
            latest.booked_on + timedelta(days=median_gap) if median_gap is not None else None
        ),
        next_charge_is_prediction=True,
        next_charge_basis=(
            f"按最近 {len(gaps)} 次扣费间隔的中位数 {median_gap} 天推得，仅作预测；"
            "实际扣费日以商户为准。"
        ),
        charge_count=len(items),
        registered_status=None,
        notices=tuple(notices),
        evidence=tuple(_evidence(item) for item in items[-6:]),
        suggestion="核对是否本人开通；如非本人操作或不再使用，请到该商户的自动续费管理中关闭。",
        cancel_action="merchant_self_service",
        cancel_path=(
            "商户 App/官网 → 自动续费（订阅）管理 → 关闭；"
            "本应用尚未接入该商户的取消接口"
        ),
        external_cancel_status=EXTERNAL_CANCEL_NOT_INTEGRATED,
        price_increased_12m=increased,
        price_change_12m_from_cents=price_from,
        price_change_12m_to_cents=price_to,
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
    if items and cycle != CYCLE_UNKNOWN and median_gap is not None:
        return (
            items[-1].booked_on + timedelta(days=median_gap),
            True,
            f"按最近 {len(items) - 1} 次扣费间隔的中位数 {median_gap} 天推得，"
            "仅作预测；实际扣费日以商户为准。",
        )
    if subscription.status != "active":
        return None, False, "订阅已不在有效状态，无下次扣费。"
    reason = (
        "流水间隔无法判定周期，改用登记值"
        if items else "无流水可核验，取登记值"
    )
    return (
        subscription.next_charge_on,
        False,
        f"取订阅登记中的下次扣费日（{reason}，非预测值）。",
    )


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
    if intent.action == "subscription.list":
        params: dict[str, object] = {"user_id": DEMO_USER_ID}
    elif intent.action == "subscription.sign":
        params = _sign_params(intent)
    else:
        subscription_id = _subscription_id(intent)
        user_id = _subscription_user_id(intent)
        _require_subscription(gateway, subscription_id, user_id)
        params = {"user_id": user_id, "subscription_id": subscription_id}
    return PlanNode(
        node_id=node_id,
        action=intent.action,
        params=params,
        intent=IntentRef(source=AnchorSource.EXPLICIT, anchor_id=anchor_id),
    )


def _sign_params(intent: Intent) -> dict[str, object]:
    """代扣签约装配（README 裁决 #17）：对象是**商户**，不涉及已有订阅 id。"""
    period = as_str(trusted_slot(intent, ("period",), "period"), "period")
    if period not in _SIGN_PERIODS:
        raise ScenarioRefused(f"代扣周期只支持 {sorted(_SIGN_PERIODS)}，收到 {period}")
    return {
        "user_id": DEMO_USER_ID,
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
    subscriptions = context.gateway.subscriptions_of(require_str(context.params, "user_id"))
    return QueryResult(
        record_ids=tuple(item.record_id for item in subscriptions),
        provenance=tuple((item.record_id, item.content_hash) for item in subscriptions),
        detail=f"共 {len(subscriptions)} 项订阅",
    )


QUERY_HANDLERS: Mapping[str, QueryHandler] = {"subscription.list": _subscription_list}
"""订阅域**读**处理器（组合根汇入 PEP；`docs/已修复清单-2026-10-08.md` 步骤 4「残留」补做）。"""

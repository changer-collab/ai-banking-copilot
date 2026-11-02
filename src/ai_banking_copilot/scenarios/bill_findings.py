"""账单异常识别引擎：确定性规则 + 可重算金额 + 供模型解释的 Finding。

边界：
- 事实只来自 `FactGateway`，本模块**不写任何事实、不触网**；
- 金额全部由确定性代码计算，模型只解释 `Finding`，不得生成或修改金额；
- **规则命中 ≠ 事实成立**：`status` 区分 `confirmed` / `suspected` / `normal`；
- 消费口径唯一：转账 / 还款 / 理财等非消费资金流不进入消费分析，单列统计。

数据来源（如实标注）：仓库只有 `gateway/seed.py` 播种的演示流水，**尚未接入真实账单导入**；
`BILL_DATA_SOURCE` 会随报告一起输出，调用方不得把它描述为真实账单分析。
"""

from __future__ import annotations

import calendar
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Protocol

from ai_banking_copilot.gateway.facts import FactGateway
from ai_banking_copilot.gateway.models import Money
from ai_banking_copilot.scenarios.bill_categories import (
    CategoryInfo,
    category_info,
    display_merchant,
    is_consumption_category,
    normalize_merchant,
)

BILL_DATA_SOURCE = "内置演示数据（未接入真实账单导入）"
"""当前唯一的数据来源；真实账单导入接通前必须原样展示。"""

LOOKBACK_MONTHS = 12
"""历史样本窗口（含本期）：用于价格变化、周期与分类基线。"""

DUPLICATE_WINDOW_HOURS = 47
"""重复扣费匹配窗口（小时）。**窗口内同额只标疑似，不直接认定重复扣费。**"""

PRICE_CHANGE_MIN_PERCENT = 10
"""同商户单期扣款相对历史中位值的上涨阈值（百分比）。"""

PRICE_CHANGE_MIN_CENTS = 100
"""价格变化的最小绝对差额（分），过滤四舍五入噪声。"""

SUBSCRIPTION_MIN_CHARGES = 3
"""确认周期性订阅所需的期数；不足 3 期只作疑似。"""

CYCLE_MONTHLY_DAYS = (25, 35)
CYCLE_YEARLY_DAYS = (350, 380)

ABNORMAL_MULTIPLE = 3
"""异常消费阈值：同分类其他商户中位数的倍数。"""

ABNORMAL_MIN_SAMPLES = 3
"""分类基线所需的最少其他商户样本数。"""

ABNORMAL_MIN_CENTS = 100_000
"""异常消费的最低金额（分）：1000 元，避免小额误报。"""

ABNORMAL_EXEMPT_CATEGORIES = frozenset({"居住", "生活缴费"})
"""房租、水电、物业、通讯等固定生活成本不参与「异常大额」判断。"""

AUTO_RENEWAL_AMOUNT_TOLERANCE = 5
"""自动续费金额允许的波动（百分比）：同商户固定金额续费，超过则视为普通消费。"""

LEAD_KEY_CONFIDENCE = Decimal("0.70")
"""「重点核对」最低置信度：低于此值不占首屏。"""

LEAD_MORE_CONFIDENCE = Decimal("0.50")
"""「更多线索」最低置信度：低于此值不展示。"""

LEAD_KEY_MIN_IMPACT_CENTS = 500
"""「重点核对」最低影响金额（分）：5 元。高置信但金额过小的涨价不占首屏。"""

LATE_NIGHT_START_HOUR = 22
"""「深夜」判据起点（含）：22:00 起算。"""

LATE_NIGHT_END_HOUR = 6
"""「深夜」判据终点（不含）：06:00 前算深夜。"""

REMOTE_CITY_MIN_SAMPLES = 2
"""判定「常用城市」所需的最少历史样本数；不足则不启用异地判据（避免新用户误报）。"""

FINDING_KINDS = (
    "duplicate-charge",
    "price-change",
    "auto-renewal",
    "recurring-subscription",
    "abnormal-spend",
    "late-night",
    "remote-city",
)
FINDING_STATUSES = ("confirmed", "suspected", "normal")
RISK_FINDING_KINDS = frozenset(
    {"duplicate-charge", "abnormal-spend", "late-night", "remote-city"}
)
"""进入「可疑清单 / 风险核对工作台」的线索类型。"""
SUBSCRIPTION_INFO_KINDS = frozenset(
    {"price-change", "auto-renewal", "recurring-subscription"}
)
"""信息型线索：归订阅页/自动续费区，不再混进风险清单。"""
SAVING_BASES = ("confirmed", "potential", "none")
LEAD_STATES = ("normal", "abnormal", "ignored")
LEAD_STATE_LABELS = {"normal": "确认正常", "abnormal": "标记异常", "ignored": "已忽略"}

FINDING_LABELS = {
    "duplicate-charge": "疑似重复扣款",
    "price-change": "价格上涨",
    "auto-renewal": "自动续费",
    "recurring-subscription": "周期订阅",
    "abnormal-spend": "异常大额",
    "late-night": "深夜交易",
    "remote-city": "异地交易",
}
FINDING_RULES = {
    "duplicate-charge": "同商户同金额、两笔间隔不超过 47 小时",
    "price-change": "同商户本期扣款较历史中位值上涨",
    "auto-renewal": "同商户固定周期、固定金额，且未登记订阅",
    "recurring-subscription": "已登记订阅的周期核验",
    "abnormal-spend": "单笔达到同分类其他商户中位数的 3 倍",
    "late-night": "交易发生在 22:00–06:00",
    "remote-city": "交易发生地不在常用城市",
}
FINDING_THRESHOLDS = {
    "duplicate-charge": "同商户 + 同金额 + 间隔 ≤47 小时",
    "price-change": "上涨 ≥10% 且差额 ≥¥1.00",
    "auto-renewal": "≥3 次；周期 25–35 天或 350–380 天；金额波动 ≤5%",
    "recurring-subscription": "已登记订阅 + 周期/金额核验",
    "abnormal-spend": "≥¥1,000 且 ≥3 倍同分类其他商户中位数",
    "late-night": "22:00–06:00",
    "remote-city": "异地单独不构成风险；异地 + 深夜/大额才提升置信度",
}


@dataclass(frozen=True)
class FindingEvidence:
    """一笔可回读的交易证据（`record_id` + `content_hash` 供出处校验）。"""

    txn_id: str
    record_id: str
    content_hash: str
    booked_on: date
    amount_cents: int
    """原始带符号金额（支出为负），不做四舍五入或改写。"""
    merchant_label: str
    booked_at: str | None = None
    """发生时刻（可空）：深夜判据的唯一时间出处，同时供可疑清单展示。"""
    city: str | None = None
    """发生城市（可空）：异地判据的唯一地点出处，同时供可疑清单展示。"""


@dataclass(frozen=True)
class Finding:
    """统一识别结果。`impact_cents` 与 `saving_allocations` 都由本模块计算。"""

    finding_id: str
    kind: str
    status: str
    confidence: Decimal
    txn_ids: tuple[str, ...]
    evidence: tuple[FindingEvidence, ...]
    impact_cents: int
    impact_basis: str
    saving_basis: str
    assumption: str
    suggestion: str
    detail: str
    saving_allocations: tuple[tuple[str, int], ...] = ()
    impact_scope: str = "current_period"
    """影响金额口径：`current_period` = 本期多付/本次金额；`annualized` 另给年化值。"""
    annualized_impact_cents: int | None = None
    """可选：按 12 期估算的未来影响；仅在规则能确定周期时给出。"""


@dataclass(frozen=True)
class BillTxnDigest:
    """给模型看的账单明细摘要；只含已回读的事实字段，不含自由文本备注。"""

    txn_id: str
    booked_on: date
    amount_cents: int
    category: str
    merchant_label: str
    direction: str
    booked_at: str | None = None
    city: str | None = None
    category_l2: str = ""
    mcc: str = ""
    mcc_label: str = ""


@dataclass(frozen=True)
class BillFindingsReport:
    """一个账期的识别报告：消费明细、发现项、收支与转账分列。"""

    user_id: str
    period: str
    as_of: date
    data_source: str
    transactions: tuple[BillTxnDigest, ...]
    findings: tuple[Finding, ...]
    normal_txn_ids: tuple[str, ...]
    confirmed_saving_cents: int
    potential_saving_cents: int
    notes: tuple[str, ...]
    income_cents: int = 0
    transfer_out_cents: int = 0
    transfer_count: int = 0
    transfers: tuple[BillTxnDigest, ...] = ()
    refund_cents: int = 0
    """本期消费分类的退款合计（已在同分类抵扣消费，不计入收入）。"""

    @property
    def consumption_cents(self) -> int:
        return sum(-item.amount_cents for item in self.transactions) - self.refund_cents

    @property
    def net_cashflow_cents(self) -> int:
        return self.income_cents - self.consumption_cents - self.transfer_out_cents

    @property
    def finding_ids(self) -> tuple[str, ...]:
        return tuple(finding.finding_id for finding in self.findings)


@dataclass(frozen=True)
class _Charge:
    """内部支出流水（金额取绝对值）；对外的带符号金额见 `FindingEvidence`。"""

    txn_id: str
    booked_on: date
    amount_cents: int
    merchant: str
    category: str
    record_id: str
    content_hash: str
    booked_at: str | None = None
    city: str | None = None
    category_l2: str = ""
    mcc: str = ""
    mcc_label: str = ""

    @property
    def merchant_key(self) -> str:
        return normalize_merchant(self.merchant)

    @property
    def merchant_label(self) -> str:
        return display_merchant(self.merchant)


@dataclass(frozen=True)
class ReviewLead:
    """展示层线索：同一笔交易的多重命中合并成一条。"""

    lead_id: str
    period: str
    txn_ids: tuple[str, ...]
    kinds: tuple[str, ...]
    labels: tuple[str, ...]
    status: str
    confidence: Decimal
    tier: str
    impact_cents: int
    evidence: tuple[FindingEvidence, ...]
    basis: tuple[dict[str, object], ...]
    suggestion: str
    state: str = ""


def lead_tier(confidence: Decimal, impact_cents: int = 0) -> str:
    """置信度 + 金额分层：高置信且金额 ≥5 元才进重点核对。"""
    if confidence < LEAD_MORE_CONFIDENCE:
        return "hidden"
    if confidence >= LEAD_KEY_CONFIDENCE and impact_cents >= LEAD_KEY_MIN_IMPACT_CENTS:
        return "key"
    return "more"


def finding_basis(finding: Finding) -> dict[str, object]:
    """一条命中的可核验依据：规则、阈值、实际明细与假设。"""
    return {
        "kind": finding.kind,
        "label": FINDING_LABELS.get(finding.kind, finding.kind),
        "rule": FINDING_RULES.get(finding.kind, "规则命中"),
        "threshold": FINDING_THRESHOLDS.get(finding.kind, "—"),
        "detail": finding.detail,
        "assumption": finding.assumption,
        "confidence": str(finding.confidence),
        "impact_basis": finding.impact_basis,
        "impact_scope": finding.impact_scope,
        "annualized_impact_cents": finding.annualized_impact_cents,
    }


def build_leads(
    findings: Sequence[Finding],
    *,
    period: str,
    states: Mapping[str, str] | None = None,
    kinds: Collection[str] | None = None,
) -> tuple[ReviewLead, ...]:
    """按主交易聚合命中，合并标签，并按置信度分层。

    - 同一交易的多重命中只出现一次，``impact_cents`` 取最大值而非累加；
    - ``status=normal`` 的订阅核验不进入线索；
    - ``kinds`` 可限定只收风险类命中；信息型线索由订阅页消费；
    - 已核对（normal/ignored）的线索保留状态并归档；标记异常仍留在重点核对。
    """
    grouped: dict[str, list[Finding]] = {}
    for finding in findings:
        if finding.status == "normal":
            continue
        if kinds is not None and finding.kind not in kinds:
            continue
        primary = finding.evidence[-1].txn_id if finding.evidence else finding.txn_ids[-1]
        grouped.setdefault(primary, []).append(finding)

    leads: list[ReviewLead] = []
    for primary, items in grouped.items():
        evidence: dict[str, FindingEvidence] = {}
        for finding in items:
            for row in finding.evidence:
                evidence.setdefault(row.txn_id, row)
        lead_kinds = tuple(dict.fromkeys(finding.kind for finding in items))
        confidence = max(finding.confidence for finding in items)
        impact_cents = max(finding.impact_cents for finding in items)
        state = (states or {}).get(f"lead-{primary}", "")
        if state in {"normal", "ignored"}:
            tier = "resolved"
        elif state == "abnormal":
            tier = "key"
        else:
            tier = lead_tier(confidence, impact_cents)
        suggestions = [finding.suggestion for finding in items if finding.suggestion]
        leads.append(
            ReviewLead(
                lead_id=f"lead-{primary}",
                period=period,
                txn_ids=tuple(evidence),
                kinds=lead_kinds,
                labels=tuple(FINDING_LABELS.get(kind, kind) for kind in lead_kinds),
                status="suspected",
                confidence=confidence,
                tier=tier,
                impact_cents=impact_cents,
                evidence=tuple(evidence.values()),
                basis=tuple(finding_basis(finding) for finding in items),
                suggestion="；".join(dict.fromkeys(suggestions)),
                state=state,
            )
        )
    order = {"key": 0, "more": 1, "hidden": 2, "resolved": 3}
    return tuple(
        sorted(
            leads,
            key=lambda item: (
                order.get(item.tier, 9),
                -item.impact_cents,
                -item.confidence,
                item.lead_id,
            ),
        )
    )


def lead_payload(lead: ReviewLead) -> dict[str, object]:
    """线索序列化；页面与 AI 问答共用同一结构。"""
    return {
        "lead_id": lead.lead_id,
        "period": lead.period,
        "txn_ids": list(lead.txn_ids),
        "kinds": list(lead.kinds),
        "labels": list(lead.labels),
        "status": lead.status,
        "confidence": str(lead.confidence),
        "tier": lead.tier,
        "impact_cents": lead.impact_cents,
        "state": lead.state,
        "state_label": LEAD_STATE_LABELS.get(lead.state, ""),
        "basis": list(lead.basis),
        "suggestion": lead.suggestion,
        "evidence": [
            {
                "txn_id": item.txn_id,
                "record_no": item.txn_id,
                "record_id": item.record_id,
                "content_hash": item.content_hash,
                "booked_on": item.booked_on.isoformat(),
                "amount_cents": item.amount_cents,
                "merchant_label": item.merchant_label,
                "booked_at": item.booked_at,
                "city": item.city,
            }
            for item in lead.evidence
        ],
    }


def analyze_bill(
    gateway: FactGateway,
    user_id: str,
    year: int,
    month: int,
    *,
    as_of: date,
    overrides: Mapping[str, str] | None = None,
) -> BillFindingsReport:
    """识别一个账期的账单问题。只读；金额可按键重算。

    ``overrides`` 是用户对商户二级分类的修正（`merchant_key -> category_l2`），
    只影响分类与画像，不改变账本事实。
    """
    if not 1 <= month <= 12:
        raise ValueError(f"月份非法：{month}")
    start = date(year, month, 1)
    end = date(year, month, calendar.monthrange(year, month)[1])
    payees = {
        record.payload.payee_id: record.payload.full_name
        for record in gateway.payees_of(user_id)
    }
    flow = _collect_window(
        gateway,
        user_id,
        _shift_months(start, -(LOOKBACK_MONTHS - 1)),
        end,
        overrides=overrides,
        payee_names=payees,
    )
    period_flow = _collect_window(
        gateway, user_id, start, end, overrides=overrides, payee_names=payees
    )
    window = flow.charges
    period = period_flow.charges
    registered = _registered_merchants(gateway, user_id)

    period_label = f"{year:04d}-{month:02d}"
    notes: list[str] = [
        f"数据来源：{BILL_DATA_SOURCE}。",
        "规则命中只表示需要核对，未命中也不代表不存在异常。",
    ]
    findings: list[Finding] = []
    allocated: dict[str, int] = {}
    covered: set[str] = set()

    if not period:
        notes.append(f"{period_label} 没有消费流水，无法识别异常。")
    else:
        if len(period) == 1:
            notes.append(
                f"{period_label} 仅有 1 笔消费，样本不足：不据此确认重复扣费、价格变化或订阅扣费。"
            )
        findings.extend(_duplicate_findings(window, start, end, allocated, covered))
        findings.extend(_price_change_findings(window, start, end, allocated, covered))
        findings.extend(
            _auto_renewal_findings(window, start, end, registered, allocated, covered)
        )
        findings.extend(_subscription_findings(window, start, end, registered))
        # 判据优先级：异常大额 > 异地 > 深夜。同一笔只归一条，
        # 否则「凌晨+异地+大额」会被深夜先认领，异常大额永远命中不到。
        findings.extend(_abnormal_findings(window, period, covered))
        common_cities = _common_cities(window)
        if not common_cities:
            notes.append("历史城市样本不足，异地判据未启用；未命中不代表不存在异常。")
        findings.extend(_remote_city_findings(period, common_cities, covered))
        findings.extend(_late_night_findings(period, covered))
        if len(window) < SUBSCRIPTION_MIN_CHARGES:
            notes.append("历史样本不足 3 期，价格变化与订阅结论仅作疑似提示，不作为已确认问题。")

    covered.update(txn_id for finding in findings for txn_id in finding.txn_ids)
    normal_txn_ids = tuple(
        charge.txn_id for charge in period if charge.txn_id not in covered
    )
    confirmed, potential = summarize_findings(
        findings, txn_ids={charge.txn_id for charge in window}
    )
    if not findings:
        notes.append(f"{period_label} 未命中识别规则；未发现不等于不存在。")

    return BillFindingsReport(
        user_id=user_id,
        period=period_label,
        as_of=as_of,
        data_source=BILL_DATA_SOURCE,
        transactions=tuple(_digest(charge) for charge in period),
        findings=tuple(findings),
        normal_txn_ids=normal_txn_ids,
        confirmed_saving_cents=confirmed,
        potential_saving_cents=potential,
        notes=tuple(notes),
        income_cents=period_flow.income_cents,
        transfer_out_cents=period_flow.transfer_out_cents,
        transfer_count=period_flow.transfer_count,
        transfers=period_flow.transfers,
        refund_cents=period_flow.refund_cents,
    )


def summarize_findings(
    findings: Sequence[Finding], *, txn_ids: Collection[str]
) -> tuple[int, int]:
    """校验并汇总节省金额：返回 `(已确定, 潜在)`，单位分。

    校验失败（重复 ID、越界金额、同一交易重复计入、状态与金额口径矛盾）抛 `ValueError`。
    汇总只依赖 `saving_allocations`，因此可逐笔重算得到相同结果。
    """
    seen_ids: set[str] = set()
    allocated: dict[str, int] = {}
    confirmed = 0
    potential = 0
    for finding in findings:
        if finding.finding_id in seen_ids:
            raise ValueError(f"finding_id 重复：{finding.finding_id}")
        seen_ids.add(finding.finding_id)
        if finding.kind not in FINDING_KINDS:
            raise ValueError(f"未知 finding.kind：{finding.kind}")
        if finding.status not in FINDING_STATUSES:
            raise ValueError(f"未知 finding.status：{finding.status}")
        if finding.saving_basis not in SAVING_BASES:
            raise ValueError(f"未知 saving_basis：{finding.saving_basis}")
        if not Decimal(0) <= finding.confidence <= Decimal(1):
            raise ValueError(f"confidence 越界：{finding.confidence}")
        if finding.impact_cents < 0:
            raise ValueError(f"impact_cents 不能为负：{finding.finding_id}")
        if finding.status == "normal" and (
            finding.impact_cents != 0 or finding.saving_allocations
        ):
            raise ValueError(f"正常消费不得计入影响金额或节省：{finding.finding_id}")
        for evidence in finding.evidence:
            if evidence.txn_id not in txn_ids:
                raise ValueError(f"证据交易不在账单内：{evidence.txn_id}")
            if evidence.txn_id not in finding.txn_ids:
                raise ValueError(f"证据交易未登记在 txn_ids：{evidence.txn_id}")
        total = 0
        for txn_id, amount in finding.saving_allocations:
            if txn_id not in finding.txn_ids:
                raise ValueError(f"节省分配的交易不在 txn_ids：{txn_id}")
            if txn_id in allocated:
                raise ValueError(f"交易被重复计入节省：{txn_id}")
            if amount <= 0:
                raise ValueError(f"节省分配金额必须为正：{txn_id}")
            allocated[txn_id] = amount
            total += amount
        if total > finding.impact_cents:
            raise ValueError(f"节省分配超过影响金额：{finding.finding_id}")
        if finding.saving_allocations and finding.saving_basis == "none":
            raise ValueError(f"saving_basis=none 不得分配节省：{finding.finding_id}")
        if finding.saving_basis == "confirmed":
            confirmed += total
        elif finding.saving_basis == "potential":
            potential += total
    return confirmed, potential


def _finding_payload(finding: Finding) -> dict[str, object]:
    return {
        "finding_id": finding.finding_id,
        "kind": finding.kind,
        "kind_label": FINDING_LABELS.get(finding.kind, finding.kind),
        "status": finding.status,
        "confidence": str(finding.confidence),
        "txn_ids": list(finding.txn_ids),
        "evidence": [
            {
                "txn_id": item.txn_id,
                "record_no": item.txn_id,
                "record_id": item.record_id,
                "content_hash": item.content_hash,
                "booked_on": item.booked_on.isoformat(),
                "amount_cents": item.amount_cents,
                "merchant_label": item.merchant_label,
                "booked_at": item.booked_at,
                "city": item.city,
            }
            for item in finding.evidence
        ],
        "impact_cents": finding.impact_cents,
        "impact_scope": finding.impact_scope,
        "annualized_impact_cents": finding.annualized_impact_cents,
        "impact_basis": finding.impact_basis,
        "saving_basis": finding.saving_basis,
        "assumption": finding.assumption,
        "suggestion": finding.suggestion,
        "detail": finding.detail,
        "basis": finding_basis(finding),
        "saving_allocations": [
            {"txn_id": txn_id, "amount_cents": amount}
            for txn_id, amount in finding.saving_allocations
        ],
    }


def report_payload(
    report: BillFindingsReport,
    *,
    lead_states: Mapping[str, str] | None = None,
) -> dict[str, object]:
    """报告的渠道序列化（金额单位为分，日期为 ISO 字符串）。

    `summary` 是页面结论区唯一的数字来源：消费合计、收入、转账与线索条数
    全部由同一份报告重算，页面不得自行改写这些数字。
    """
    confirmed = sum(1 for item in report.findings if item.status == "confirmed")
    suspected = sum(1 for item in report.findings if item.status == "suspected")
    normal = sum(1 for item in report.findings if item.status == "normal")
    risk_findings = tuple(
        item for item in report.findings if item.kind in RISK_FINDING_KINDS
    )
    info_findings = tuple(
        item for item in report.findings if item.kind in SUBSCRIPTION_INFO_KINDS
    )
    leads = build_leads(
        risk_findings,
        period=report.period,
        states=lead_states,
        kinds=RISK_FINDING_KINDS,
    )
    visible = [item for item in leads if item.tier in {"key", "more"}]
    key_leads = [item for item in leads if item.tier == "key"]
    resolved_leads = [item for item in leads if item.tier == "resolved"]
    review_amount = sum(item.impact_cents for item in visible)
    auto_renewals = [
        finding
        for finding in info_findings
        if finding.kind in {"auto-renewal", "recurring-subscription"}
    ]
    price_changes = [finding for finding in info_findings if finding.kind == "price-change"]
    return {
        "user_id": report.user_id,
        "period": report.period,
        "as_of": report.as_of.isoformat(),
        "data_source": report.data_source,
        # 演示态由数据来源本身决定：接入真实账单后来源文案不含「演示」，黄条自动消失。
        "data_source_is_demo": "演示" in report.data_source,
        "summary": {
            "transaction_count": len(report.transactions),
            "consumption_count": len(report.transactions),
            "expense_total_cents": report.consumption_cents,
            "consumption_cents": report.consumption_cents,
            "income_cents": report.income_cents,
            "transfer_out_cents": report.transfer_out_cents,
            "transfer_count": report.transfer_count,
            "net_cashflow_cents": report.net_cashflow_cents,
            "confirmed_count": confirmed,
            "suspected_count": suspected,
            "normal_count": normal,
            "risk_finding_count": len(risk_findings),
            "subscription_info_count": len(info_findings),
            "normal_transaction_count": len(report.normal_txn_ids),
            "lead_count": len(visible),
            "key_lead_count": len(key_leads),
            "resolved_lead_count": len(resolved_leads),
            "review_amount_cents": review_amount,
            "lookback_months": LOOKBACK_MONTHS,
        },
        "transactions": [
            {
                "txn_id": item.txn_id,
                "booked_on": item.booked_on.isoformat(),
                "amount_cents": item.amount_cents,
                "category": item.category,
                "category_l2": item.category_l2,
                "mcc": item.mcc,
                "mcc_label": item.mcc_label,
                "merchant_label": item.merchant_label,
                "direction": item.direction,
                "booked_at": item.booked_at,
                "city": item.city,
            }
            for item in report.transactions
        ],
        "transfers": [
            {
                "txn_id": item.txn_id,
                "booked_on": item.booked_on.isoformat(),
                "amount_cents": item.amount_cents,
                "category": item.category,
                "merchant_label": item.merchant_label,
                "booked_at": item.booked_at,
                "city": item.city,
            }
            for item in report.transfers
        ],
        "findings": [_finding_payload(finding) for finding in report.findings],
        "risk_findings": [_finding_payload(finding) for finding in risk_findings],
        "subscription_findings": [
            _finding_payload(finding) for finding in info_findings
        ],
        "leads": [lead_payload(lead) for lead in leads],
        "auto_renewals": [_finding_payload(finding) for finding in auto_renewals],
        "price_changes": [_finding_payload(finding) for finding in price_changes],
        "normal_txn_ids": list(report.normal_txn_ids),
        "confirmed_saving_cents": report.confirmed_saving_cents,
        "potential_saving_cents": report.potential_saving_cents,
        "notes": list(report.notes),
    }


# --------------------------------------------------------------------------- 画像 / 趋势

TREND_MONTHS = 6
"""场景页「近 6 月支出趋势」窗口（含本期）。"""


@dataclass(frozen=True)
class MomContribution:
    """环比归因中的单个分类贡献（正 = 增加，负 = 减少）。"""

    category: str
    change_cents: int


@dataclass(frozen=True)
class BillMom:
    """一个账期相对上一账期的消费变化与分类贡献。"""

    previous_period: str
    change_cents: int
    change_pct: Decimal | None
    contributions: tuple[MomContribution, ...]


@dataclass(frozen=True)
class BillTrendPoint:
    """一个账期的消费/收入合计（正数，单位分）与分类构成。"""

    period: str
    expense_cents: int
    income_cents: int = 0
    categories: tuple[CategoryShare, ...] = ()
    mom: BillMom | None = None


@dataclass(frozen=True)
class CategoryShare:
    """本期分类结构：二级分类支出与占比（占比由服务端计算，前端不除）。"""

    category: str
    expense_cents: int
    share: Decimal
    category_l1: str = ""
    mcc: str = ""
    mcc_label: str = ""


@dataclass(frozen=True)
class MerchantTotal:
    """Top 商户：金额、笔数、占比与分类。"""

    merchant_key: str
    merchant_label: str
    amount_cents: int
    count: int
    share: Decimal
    category_l1: str
    category_l2: str
    mcc: str
    mcc_label: str


@dataclass(frozen=True)
class BillProfile:
    """用户画像（只读、可回读）：消费结构 / 订阅负担 / 大额风险。"""

    top_category: str | None
    top_category_cents: int
    top_category_share: Decimal | None
    subscription_monthly_cents: int
    suspicious_count: int
    max_suspicious_cents: int


def _month_sequence(year: int, month: int, months: int) -> tuple[tuple[int, int], ...]:
    """自目标账期向前取 `months` 个账期，按时间升序返回。"""
    if months <= 0:
        raise ValueError(f"months 必须为正：{months}")
    sequence = [(year, month)]
    for _ in range(months - 1):
        if month == 1:
            year, month = year - 1, 12
        else:
            month -= 1
        sequence.append((year, month))
    return tuple(reversed(sequence))


def _shares_from_charges(
    charges: Sequence[_Charge], refunds: Sequence[_Charge] = ()
) -> tuple[CategoryShare, ...]:
    """按二级分类聚合净额；分母是同一批净消费，占比合计为 1。"""
    grouped: dict[str, tuple[int, CategoryInfo]] = {}
    for charge in charges:
        info = CategoryInfo(
            charge.category,
            charge.category_l2 or charge.category,
            charge.mcc,
            charge.mcc_label,
        )
        key = charge.category_l2 or charge.category
        cents, _ = grouped.get(key, (0, info))
        grouped[key] = (cents + charge.amount_cents, info)
    for refund in refunds:
        info = CategoryInfo(
            refund.category,
            refund.category_l2 or refund.category,
            refund.mcc,
            refund.mcc_label,
        )
        key = refund.category_l2 or refund.category
        cents, _ = grouped.get(key, (0, info))
        grouped[key] = (cents - refund.amount_cents, info)
    netted = {key: row for key, row in grouped.items() if row[0] > 0}
    total = sum(cents for cents, _ in netted.values())
    if total <= 0:
        return ()
    shares = [
        CategoryShare(
            category=category,
            expense_cents=cents,
            share=(Decimal(cents) / Decimal(total)).quantize(Decimal("0.0001")),
            category_l1=info.category_l1,
            mcc=info.mcc,
            mcc_label=info.mcc_label,
        )
        for category, (cents, info) in netted.items()
    ]
    return tuple(sorted(shares, key=lambda item: (-item.expense_cents, item.category)))


def _category_change(
    current: Sequence[CategoryShare], previous: Sequence[CategoryShare]
) -> tuple[MomContribution, ...]:
    """按一级分类对比两期构成，返回变化最大的分类贡献。"""
    current_totals: dict[str, int] = {}
    for item in current:
        current_totals[item.category_l1] = (
            current_totals.get(item.category_l1, 0) + item.expense_cents
        )
    previous_totals: dict[str, int] = {}
    for item in previous:
        previous_totals[item.category_l1] = (
            previous_totals.get(item.category_l1, 0) + item.expense_cents
        )
    changes = [
        MomContribution(
            category,
            current_totals.get(category, 0) - previous_totals.get(category, 0),
        )
        for category in sorted(set(current_totals) | set(previous_totals))
    ]
    return tuple(
        sorted(
            (item for item in changes if item.change_cents != 0),
            key=lambda item: (-abs(item.change_cents), item.category),
        )
    )


def expense_trend(
    gateway: FactGateway,
    user_id: str,
    year: int,
    month: int,
    *,
    months: int = TREND_MONTHS,
    overrides: Mapping[str, str] | None = None,
) -> tuple[BillTrendPoint, ...]:
    """近 N 月消费/收入趋势：全部来自同一消费口径，不含任何估算。"""
    points: list[BillTrendPoint] = []
    previous: BillTrendPoint | None = None
    for point_year, point_month in _month_sequence(year, month, months):
        start = date(point_year, point_month, 1)
        end = date(point_year, point_month, calendar.monthrange(point_year, point_month)[1])
        flow = _collect_window(gateway, user_id, start, end, overrides=overrides)
        shares = _shares_from_charges(flow.charges, flow.refunds)
        expense = flow.consumption_cents
        mom = None
        if previous is not None:
            change = expense - previous.expense_cents
            mom = BillMom(
                previous_period=previous.period,
                change_cents=change,
                change_pct=(
                    (Decimal(change) / Decimal(previous.expense_cents) * 100).quantize(
                        Decimal("0.1")
                    )
                    if previous.expense_cents > 0
                    else None
                ),
                contributions=_category_change(shares, previous.categories),
            )
        point = BillTrendPoint(
            period=f"{point_year:04d}-{point_month:02d}",
            expense_cents=expense,
            income_cents=flow.income_cents,
            categories=shares,
            mom=mom,
        )
        points.append(point)
        previous = point
    return tuple(points)


def category_shares(
    gateway: FactGateway,
    user_id: str,
    year: int,
    month: int,
    *,
    overrides: Mapping[str, str] | None = None,
) -> tuple[CategoryShare, ...]:
    """本期二级分类占比：分母 = 同一批净消费，不含收入与转账。"""
    start = date(year, month, 1)
    end = date(year, month, calendar.monthrange(year, month)[1])
    flow = _collect_window(gateway, user_id, start, end, overrides=overrides)
    return _shares_from_charges(flow.charges, flow.refunds)


def consumption_totals(
    gateway: FactGateway, user_id: str, year: int, month: int
) -> tuple[int, int, int, int]:
    """本期消费/收入/非消费流出/笔数；供 AI 月度账单回答使用同一口径。"""
    start = date(year, month, 1)
    end = date(year, month, calendar.monthrange(year, month)[1])
    flow = _collect_window(gateway, user_id, start, end)
    return (
        flow.consumption_cents,
        flow.income_cents,
        flow.transfer_out_cents,
        flow.transfer_count,
    )


def top_merchants(
    gateway: FactGateway,
    user_id: str,
    year: int,
    month: int,
    *,
    limit: int = 8,
    overrides: Mapping[str, str] | None = None,
) -> tuple[MerchantTotal, ...]:
    """按金额排序的 Top 商户；页面可再按笔数重排。"""
    start = date(year, month, 1)
    end = date(year, month, calendar.monthrange(year, month)[1])
    flow = _collect_window(gateway, user_id, start, end, overrides=overrides)
    total = flow.consumption_cents
    if total <= 0 or limit <= 0:
        return ()
    grouped: dict[str, tuple[int, int, _Charge]] = {}
    for charge in flow.charges:
        cents, count, sample = grouped.get(
            charge.merchant_key, (0, 0, charge)
        )
        grouped[charge.merchant_key] = (cents + charge.amount_cents, count + 1, sample)
    for refund in flow.refunds:
        if refund.merchant_key not in grouped:
            continue
        cents, count, sample = grouped[refund.merchant_key]
        grouped[refund.merchant_key] = (cents - refund.amount_cents, count, sample)
    rows = [
        MerchantTotal(
            merchant_key=key,
            merchant_label=sample.merchant_label,
            amount_cents=cents,
            count=count,
            share=(Decimal(cents) / Decimal(total)).quantize(Decimal("0.0001")),
            category_l1=sample.category,
            category_l2=sample.category_l2 or sample.category,
            mcc=sample.mcc,
            mcc_label=sample.mcc_label,
        )
        for key, (cents, count, sample) in grouped.items()
        if cents > 0
    ]
    return tuple(
        sorted(rows, key=lambda item: (-item.amount_cents, -item.count, item.merchant_key))[
            :limit
        ]
    )


def bill_profile(
    gateway: FactGateway,
    user_id: str,
    report: BillFindingsReport,
    *,
    overrides: Mapping[str, str] | None = None,
) -> BillProfile:
    """账单 → 用户画像（只读）：消费结构 / 订阅负担 / 大额风险。"""
    year, month = (int(part) for part in report.period.split("-", 1))
    shares = category_shares(gateway, user_id, year, month, overrides=overrides)
    top = shares[0] if shares else None
    subscriptions = [
        record.payload
        for record in gateway.subscriptions_of(user_id)
        if record.payload.user_id == user_id and record.payload.status == "active"
    ]
    monthly_cents = sum(
        item.amount.cents // 12 if item.period == "yearly" else item.amount.cents
        for item in subscriptions
    )
    risky = [
        item
        for item in report.findings
        if item.status in {"confirmed", "suspected"}
        and item.kind in RISK_FINDING_KINDS
    ]
    return BillProfile(
        top_category=top.category if top is not None else None,
        top_category_cents=top.expense_cents if top is not None else 0,
        top_category_share=top.share if top is not None else None,
        subscription_monthly_cents=monthly_cents,
        suspicious_count=len(risky),
        max_suspicious_cents=max((item.impact_cents for item in risky), default=0),
    )


# --------------------------------------------------------------------------- 规则


def _duplicate_findings(
    window: Sequence[_Charge],
    start: date,
    end: date,
    allocated: dict[str, int],
    covered: set[str],
) -> list[Finding]:
    """同商户同额、且在 47 小时窗口内的两笔扣款：**一律只标疑似**。"""
    groups: dict[tuple[str, int], list[_Charge]] = {}
    for charge in window:
        groups.setdefault((charge.merchant_key, charge.amount_cents), []).append(charge)

    used: set[str] = set()
    findings: list[Finding] = []
    for key in sorted(groups):
        items = groups[key]
        for index, later in enumerate(items):
            if later.txn_id in used or not start <= later.booked_on <= end:
                continue
            for earlier in reversed(items[:index]):
                if earlier.txn_id in used:
                    continue
                hours = (later.booked_on - earlier.booked_on).days * 24
                if not 0 <= hours <= DUPLICATE_WINDOW_HOURS:
                    continue
                used.update({earlier.txn_id, later.txn_id})
                covered.update({earlier.txn_id, later.txn_id})
                allocated[later.txn_id] = later.amount_cents
                findings.append(
                    Finding(
                        finding_id=f"duplicate-charge-{len(findings) + 1:02d}",
                        kind="duplicate-charge",
                        status="suspected",
                        confidence=Decimal("0.55"),
                        txn_ids=(earlier.txn_id, later.txn_id),
                        evidence=(_evidence(earlier), _evidence(later)),
                        impact_cents=later.amount_cents,
                        impact_basis="按后一笔同额扣款全额估算；未经商户核对，不作最终结论。",
                        saving_basis="potential",
                        assumption=(
                            "假设两笔同额扣款可能指向同一笔消费；也可能是两笔独立消费，"
                            "规则无法仅凭流水区分。"
                        ),
                        suggestion=(
                            "先核对两笔扣款的商户与用途；若确认重复，请联系商户退款或向发卡行申请查询。"
                        ),
                        detail=(
                            f"{later.merchant_label} 于 {earlier.booked_on.isoformat()} 与 "
                            f"{later.booked_on.isoformat()} 各扣款 "
                            f"{Money(later.amount_cents).yuan} 元，"
                            f"间隔约 {hours} 小时。"
                        ),
                        saving_allocations=((later.txn_id, later.amount_cents),),
                    )
                )
                break
    return findings


def _price_change_findings(
    window: Sequence[_Charge],
    start: date,
    end: date,
    allocated: dict[str, int],
    covered: set[str],
) -> list[Finding]:
    """同商户本期扣款较历史中位值上涨 ≥10%：样本足够才是已确认的价格变化。"""
    groups: dict[str, list[_Charge]] = {}
    for charge in window:
        groups.setdefault(charge.merchant_key, []).append(charge)

    findings: list[Finding] = []
    for key in sorted(groups):
        items = groups[key]
        if len(items) < 2:
            continue
        latest = items[-1]
        if not start <= latest.booked_on <= end or latest.txn_id in allocated:
            continue
        prior = [charge for charge in items[:-1] if charge.booked_on < latest.booked_on]
        if not prior:
            continue
        previous = prior[-1]
        baseline = _median([charge.amount_cents for charge in prior])
        if baseline <= 0 or previous.amount_cents <= 0:
            continue
        delta = latest.amount_cents - previous.amount_cents
        if delta < PRICE_CHANGE_MIN_CENTS:
            continue
        if delta * 100 < previous.amount_cents * PRICE_CHANGE_MIN_PERCENT:
            continue
        covered.add(latest.txn_id)
        allocated[latest.txn_id] = delta
        sufficient = len(prior) >= SUBSCRIPTION_MIN_CHARGES
        percent = (
            Decimal(delta) * 100 / Decimal(previous.amount_cents)
        ).quantize(Decimal("0.1"))
        gap = (latest.booked_on - previous.booked_on).days
        if CYCLE_YEARLY_DAYS[0] <= gap <= CYCLE_YEARLY_DAYS[1]:
            annualized = delta
            annual_text = f"未来 12 个月约多付 {Money(delta).yuan} 元（年付按 1 期计）"
        elif CYCLE_MONTHLY_DAYS[0] <= gap <= CYCLE_MONTHLY_DAYS[1]:
            annualized = delta * 12
            annual_text = (
                f"若价格持续，未来 12 个月约多付 {Money(delta * 12).yuan} 元"
                "（按 12 期估算）"
            )
        else:
            annualized = None
            annual_text = "周期不固定，无法估算未来 12 个月影响"
        findings.append(
            Finding(
                finding_id=f"price-change-{len(findings) + 1:02d}",
                kind="price-change",
                # 规则只能证明「价格确实变了」，不能替用户确认这笔消费是否合理。
                status="suspected",
                confidence=Decimal("0.80") if sufficient else Decimal("0.55"),
                txn_ids=(previous.txn_id, latest.txn_id),
                evidence=(_evidence(previous), _evidence(latest)),
                impact_cents=delta,
                impact_basis=(
                    f"本期较上一期多付 {Money(delta).yuan} 元；{annual_text}；"
                    "仅作核对参考，不构成可避免支出的最终结论。"
                ),
                impact_scope="current_period",
                annualized_impact_cents=annualized,
                saving_basis="potential",
                assumption=(
                    "假设两次扣款对应同一服务/套餐，且期间没有购买数量或权益变化；"
                    f"历史样本 {len(prior)} 期，结论仅反映流水事实。"
                ),
                suggestion=(
                    "核对本期扣款对应的服务内容与价格；若不再需要或不认可该价格，"
                    "可取消或更换服务，差额部分才是可避免支出。"
                ),
                detail=(
                    f"{latest.merchant_label} 于 {previous.booked_on.isoformat()} 扣款 "
                    f"{Money(previous.amount_cents).yuan} 元，"
                    f"{latest.booked_on.isoformat()} 涨至 {Money(latest.amount_cents).yuan} 元，"
                    f"涨幅 {percent}%，自 {latest.booked_on.isoformat()} 起生效；"
                    f"此前历史中位 {Money(baseline).yuan} 元（样本 {len(prior)} 期）。"
                ),
                saving_allocations=((latest.txn_id, delta),),
            )
        )
    return findings


def _auto_renewal_findings(
    window: Sequence[_Charge],
    start: date,
    end: date,
    registered: frozenset[str],
    allocated: dict[str, int],
    covered: set[str],
) -> list[Finding]:
    """未登记订阅的自动续费：同商户、固定周期、固定金额。

    识别对象是「周期性自动扣款」，不是「隐藏费用」：金额可以不小，
    关键在周期稳定且金额稳定；识别后给「管理自动续费」出口。
    """
    groups: dict[str, list[_Charge]] = {}
    for charge in window:
        groups.setdefault(charge.merchant_key, []).append(charge)

    findings: list[Finding] = []
    for key in sorted(groups):
        items = groups[key]
        if key in registered or len(items) < SUBSCRIPTION_MIN_CHARGES:
            continue
        period_items = [charge for charge in items if start <= charge.booked_on <= end]
        if not period_items:
            continue
        latest = period_items[-1]
        if latest.txn_id in allocated:
            continue
        # 只把「数字内容/会员」类固定扣款当作自动续费；房租、餐饮等每月固定支出
        # 也会形成等间隔流水，若只看周期会把整张账单误标成自动续费。
        if latest.mcc != "7994" and not any(
            keyword in key for keyword in ("会员", "订阅", "自动续费", "服务费", "增值服务")
        ):
            continue
        intervals = [
            (later.booked_on - earlier.booked_on).days
            for earlier, later in zip(items, items[1:], strict=False)
        ]
        cycle = _median(intervals)
        if CYCLE_MONTHLY_DAYS[0] <= cycle <= CYCLE_MONTHLY_DAYS[1]:
            cycle_label = "月度"
        elif CYCLE_YEARLY_DAYS[0] <= cycle <= CYCLE_YEARLY_DAYS[1]:
            cycle_label = "年度"
        else:
            continue
        recent_amounts = [charge.amount_cents for charge in items[-3:]]
        amount_base = _median(recent_amounts)
        if amount_base <= 0:
            continue
        if max(recent_amounts) - min(recent_amounts) > (
            amount_base * AUTO_RENEWAL_AMOUNT_TOLERANCE // 100
        ):
            continue
        covered.add(latest.txn_id)
        allocated[latest.txn_id] = latest.amount_cents
        next_expected = latest.booked_on + timedelta(days=cycle)
        findings.append(
            Finding(
                finding_id=f"auto-renewal-{len(findings) + 1:02d}",
                kind="auto-renewal",
                status="suspected",
                confidence=Decimal("0.80"),
                txn_ids=(latest.txn_id,),
                evidence=(_evidence(latest),),
                impact_cents=latest.amount_cents,
                impact_basis="该笔为本期实际扣款，仅用于提示核对，不作可避免支出估算。",
                saving_basis="none",
                assumption=(
                    "按流水间隔与金额推断为周期性自动续费；也可能是用户主动的分期/定期充值。"
                ),
                suggestion=(
                    "可在订阅管理里核对并关闭自动续费；未登记的商户先核对扣费依据与商户名称。"
                ),
                detail=(
                    f"{latest.merchant_label} 近 {LOOKBACK_MONTHS} 个月出现 {len(items)} 次扣款，"
                    f"中位间隔 {cycle} 天（{cycle_label}），最近金额 "
                    f"{Money(latest.amount_cents).yuan} 元，预计下次约 "
                    f"{next_expected.isoformat()}。"
                ),
            )
        )
    return findings


def _subscription_findings(
    window: Sequence[_Charge],
    start: date,
    end: date,
    registered: frozenset[str],
) -> list[Finding]:
    """已登记订阅的周期核验：只陈述周期与金额，**不计入节省**。"""
    groups: dict[str, list[_Charge]] = {}
    for charge in window:
        groups.setdefault(charge.merchant_key, []).append(charge)

    findings: list[Finding] = []
    for key in sorted(groups):
        if key not in registered:
            continue
        items = groups[key]
        if len(items) < 2:
            continue
        if not any(start <= charge.booked_on <= end for charge in items):
            continue
        intervals = [
            (later.booked_on - earlier.booked_on).days
            for earlier, later in zip(items, items[1:], strict=False)
        ]
        cycle = _median(intervals)
        if CYCLE_MONTHLY_DAYS[0] <= cycle <= CYCLE_MONTHLY_DAYS[1]:
            cycle_label = "月度"
        elif CYCLE_YEARLY_DAYS[0] <= cycle <= CYCLE_YEARLY_DAYS[1]:
            cycle_label = "年度"
        else:
            continue
        latest = items[-1]
        sufficient = len(items) >= SUBSCRIPTION_MIN_CHARGES
        findings.append(
            Finding(
                finding_id=f"recurring-subscription-{len(findings) + 1:02d}",
                kind="recurring-subscription",
                status="normal" if sufficient else "suspected",
                confidence=Decimal("0.85") if sufficient else Decimal("0.55"),
                txn_ids=tuple(charge.txn_id for charge in items[-6:]),
                evidence=tuple(_evidence(charge) for charge in items[-6:]),
                impact_cents=0,
                impact_basis="订阅已在登记中，属于已知正常扣费。",
                saving_basis="none",
                assumption=(
                    "按流水间隔推断周期；未核验服务是否仍在使用，故不断言是否值得保留。"
                ),
                suggestion="若不再使用该服务，可在订阅管理中取消；保留则视为正常支出。",
                detail=(
                    f"{latest.merchant_label} 近 {len(items)} 期扣款，中位间隔 {cycle} 天，"
                    f"单期约 {Money(latest.amount_cents).yuan} 元（{cycle_label}）。"
                ),
            )
        )
    return findings


def _late_night_findings(period: Sequence[_Charge], covered: set[str]) -> list[Finding]:
    """深夜（22:00–06:00）支出：只提示核对，不计入节省。`booked_at` 为空则跳过。"""
    findings: list[Finding] = []
    for charge in period:
        moment = _time_of_day(charge.booked_at)
        if moment is None or not _is_late_night(moment[0]):
            continue
        if charge.txn_id in covered:
            continue
        covered.add(charge.txn_id)
        findings.append(
            Finding(
                finding_id=f"late-night-{len(findings) + 1:02d}",
                kind="late-night",
                status="suspected",
                confidence=Decimal("0.55"),
                txn_ids=(charge.txn_id,),
                evidence=(_evidence(charge),),
                impact_cents=charge.amount_cents,
                impact_basis=(
                    "该笔为本期实际支出，仅用于提示核对。"
                ),
                saving_basis="none",
                assumption="深夜消费可能完全正常（加班、夜间出行、跨时区）；命中只表示建议核对。",
                suggestion="核对是否本人消费；如非本人操作，建议同时锁卡并联系银行。",
                detail=(
                    f"{charge.merchant_label} 于 {moment[0]:02d}:{moment[1]:02d} 发生 "
                    f"{Money(charge.amount_cents).yuan} 元支出。"
                ),
            )
        )
    return findings


def _remote_city_findings(
    period: Sequence[_Charge], common_cities: frozenset[str], covered: set[str]
) -> list[Finding]:
    """异地支出：发生城市不在常用城市集合内。`city` 为空则跳过（不适用 ≠ 异常）。"""
    if not common_cities:
        return []
    baseline = "、".join(sorted(common_cities))
    findings: list[Finding] = []
    for charge in period:
        if not charge.city or charge.city in common_cities:
            continue
        moment = _time_of_day(charge.booked_at)
        combined = (
            (moment is not None and _is_late_night(moment[0]))
            or charge.amount_cents >= ABNORMAL_MIN_CENTS
        )
        covered.add(charge.txn_id)
        findings.append(
            Finding(
                finding_id=f"remote-city-{len(findings) + 1:02d}",
                kind="remote-city",
                status="suspected",
                confidence=Decimal("0.75") if combined else Decimal("0.60"),
                txn_ids=(charge.txn_id,),
                evidence=(_evidence(charge),),
                impact_cents=charge.amount_cents,
                impact_basis="该笔为本期实际支出，仅用于提示核对，不作可避免支出估算。",
                saving_basis="none",
                assumption=(
                    f"常用城市为 {baseline}（近 {LOOKBACK_MONTHS} 个月样本）；"
                    "单笔异地本身不构成风险，只有与深夜或大额组合时才提高置信度。"
                ),
                suggestion=(
                    "先核对是否本人消费；出差、旅行、线上消费归属地都可能造成异地。"
                    "如非本人操作，再锁卡并联系银行。"
                ),
                detail=(
                    f"{charge.merchant_label} 发生地 {charge.city}，常用城市 {baseline}，"
                    f"金额 {Money(charge.amount_cents).yuan} 元"
                    + ("；同时命中深夜/大额组合特征。" if combined else "；仅有异地单一特征。")
                ),
            )
        )
    return findings


def _abnormal_findings(
    window: Sequence[_Charge], period: Sequence[_Charge], covered: set[str]
) -> list[Finding]:
    """本期支出显著高于同分类其他商户的常态：只提示复核，不计入节省。"""
    by_category: dict[str, list[_Charge]] = {}
    for charge in window:
        by_category.setdefault(charge.category, []).append(charge)

    findings: list[Finding] = []
    for charge in period:
        if charge.category in ABNORMAL_EXEMPT_CATEGORIES:
            continue
        if charge.txn_id in covered or charge.amount_cents < ABNORMAL_MIN_CENTS:
            continue
        others = [
            item.amount_cents
            for item in by_category.get(charge.category, [])
            if item.merchant_key != charge.merchant_key
        ]
        if len(others) < ABNORMAL_MIN_SAMPLES:
            continue
        median = _median(others)
        if median <= 0 or charge.amount_cents < median * ABNORMAL_MULTIPLE:
            continue
        covered.add(charge.txn_id)
        findings.append(
            Finding(
                finding_id=f"abnormal-spend-{len(findings) + 1:02d}",
                kind="abnormal-spend",
                status="suspected",
                confidence=Decimal("0.50"),
                txn_ids=(charge.txn_id,),
                evidence=(_evidence(charge),),
                impact_cents=charge.amount_cents,
                impact_basis=(
                    "该笔为本期实际支出，仅用于提示核对。"
                ),
                saving_basis="none",
                assumption=(
                    "假设同分类其他商户的中位支出可代表常态；一次性大额消费也可能完全正常。"
                ),
                suggestion="核对商户与用途；如非本人消费或与预期不符，及时联系银行处理。",
                detail=(
                    f"{charge.merchant_label} 本期支出 {Money(charge.amount_cents).yuan} 元，"
                    f"同分类其他商户中位 {Money(median).yuan} 元（样本 {len(others)} 笔）。"
                ),
            )
        )
    return findings


# --------------------------------------------------------------------------- 模型叙事

# 模型只选择标识符；含事实的文本由服务端根据确定性 Finding 渲染，不靠关键词过滤。
NARRATIVE_SUMMARY_ID = "findings_overview"
_NARRATIVE_SUMMARY_IDS = frozenset({NARRATIVE_SUMMARY_ID})


class NarrativeRejectedError(ValueError):
    """模型叙事未通过证据与结构校验。"""

    code = "invalid_narrative"
    message = "模型输出未通过证据校验"

    def __init__(self) -> None:
        super().__init__(self.message)


@dataclass(frozen=True)
class NarrativeFinding:
    finding_id: str
    explanation: str
    suggestion: str


@dataclass(frozen=True)
class BillNarrative:
    """结论式分析：`degraded=True` 表示模型失败后使用确定性降级文本。"""

    summary: str
    findings: tuple[NarrativeFinding, ...]
    degraded: bool
    degrade_reason: str | None
    channel: str


class BillNarrator(Protocol):
    """叙事模型协议：输入已核实报告，输出待校验的 JSON 对象。"""

    def narrate(self, report: BillFindingsReport) -> Mapping[str, object]:
        ...


def narrative_choice_ids(finding: Finding) -> dict[str, tuple[str, ...]]:
    """返回该 Finding 可用的结构化解释 / 建议 ID。

    ID 中包含类型与状态，复制其他 Finding 的选项会因事实不匹配而被拒绝。
    """
    prefix = f"{finding.kind.replace('-', '_')}_{finding.status}"
    return {
        "explanation": (f"{prefix}_evidence", f"{prefix}_context"),
        "suggestion": (f"{prefix}_review",),
    }


def validate_narrative(
    report: BillFindingsReport, payload: Mapping[str, object]
) -> tuple[NarrativeFinding, ...]:
    """校验结构化选择，并由服务端渲染所有事实文本。

    仅接受 ``summary_id`` / ``explanation_id`` / ``suggestion_id``；自由文本与旧别名
    均拒绝，并进入确定性降级。
    """
    _reject_extra_keys(payload, {"summary_id", "findings"})
    summary_id = _choice_value(payload, "summary")
    if summary_id not in _NARRATIVE_SUMMARY_IDS:
        raise NarrativeRejectedError()

    raw_items = payload.get("findings")
    if not isinstance(raw_items, list):
        raise NarrativeRejectedError()
    expected = list(report.finding_ids)
    by_id = {finding.finding_id: finding for finding in report.findings}
    seen: list[str] = []
    results: list[NarrativeFinding] = []
    for item in raw_items:
        if not isinstance(item, dict):
            raise NarrativeRejectedError()
        _reject_extra_keys(item, {"finding_id", "explanation_id", "suggestion_id"})
        finding_id = item.get("finding_id")
        if not isinstance(finding_id, str) or finding_id not in expected:
            raise NarrativeRejectedError()
        if finding_id in seen:
            raise NarrativeRejectedError()
        finding = by_id[finding_id]
        explanation_id = _choice_value(item, "explanation")
        suggestion_id = _choice_value(item, "suggestion")
        choices = narrative_choice_ids(finding)
        seen.append(finding_id)
        if explanation_id not in choices["explanation"]:
            raise NarrativeRejectedError()
        if suggestion_id not in choices["suggestion"]:
            raise NarrativeRejectedError()
        results.append(
            NarrativeFinding(
                finding_id,
                _render_explanation(finding, explanation_id),
                _render_suggestion(finding, suggestion_id),
            )
        )
    if sorted(seen) != sorted(expected):
        raise NarrativeRejectedError()
    return tuple(results)


def _choice_value(payload: Mapping[str, object], field: str) -> str:
    """读取已登记 ID；旧字段别名与自由文本均拒绝。"""
    key = f"{field}_id"
    if key not in payload:
        raise NarrativeRejectedError()
    value = payload[key]
    if not isinstance(value, str) or not value.strip():
        raise NarrativeRejectedError()
    return value.strip()


def _reject_extra_keys(payload: Mapping[str, object], allowed: set[str]) -> None:
    extras = set(payload).difference(allowed)
    if extras:
        raise NarrativeRejectedError()


def _render_explanation(finding: Finding, choice_id: str) -> str:
    """使用已核实的 Finding 渲染解释，不使用模型文本。"""
    choices = narrative_choice_ids(finding)["explanation"]
    if choice_id == choices[0]:
        return finding.detail
    if choice_id == choices[1]:
        return f"{finding.detail}{finding.assumption}"
    raise NarrativeRejectedError()


def _render_suggestion(finding: Finding, choice_id: str) -> str:
    """使用已核实的 Finding 渲染建议，不使用模型文本。"""
    if choice_id in narrative_choice_ids(finding)["suggestion"]:
        return finding.suggestion
    raise NarrativeRejectedError()


def narrate_bill_findings(
    report: BillFindingsReport, narrator: BillNarrator | None
) -> BillNarrative:
    """调用模型解释已核实结果；任何失败都返回显式的确定性降级文本。"""
    if not report.findings:
        return BillNarrative(
            summary=_render_summary(report),
            findings=(),
            degraded=False,
            degrade_reason=None,
            channel="deterministic",
        )
    if narrator is None:
        return _fallback_narrative(report, "未装配账单解释模型")
    try:
        payload = narrator.narrate(report)
    except Exception:  # 模型通道任何异常都必须降级，不得伪装成功
        return _fallback_narrative(report, "模型调用失败（narrative_unavailable）")
    try:
        findings = validate_narrative(report, payload)
    except NarrativeRejectedError:
        reason = f"{NarrativeRejectedError.message}（{NarrativeRejectedError.code}）"
        return _fallback_narrative(report, reason)
    summary = _render_summary(report)
    return BillNarrative(
        summary=summary.strip(),
        findings=findings,
        degraded=False,
        degrade_reason=None,
        channel="llm",
    )


def _fallback_narrative(report: BillFindingsReport, reason: str) -> BillNarrative:
    """确定性降级：文案与金额都来自规则结果，明确标注降级原因。"""
    findings = tuple(
        NarrativeFinding(
            finding_id=finding.finding_id,
            explanation=f"{finding.detail}{finding.assumption}",
            suggestion=finding.suggestion,
        )
        for finding in report.findings
    )
    summary = _render_summary(report)
    return BillNarrative(
        summary=summary,
        findings=findings,
        degraded=True,
        degrade_reason=reason,
        channel="deterministic",
    )


def _render_summary(report: BillFindingsReport) -> str:
    """根据报告事实渲染整体结论：收支结构在前，线索在后。"""
    leads = build_leads(
        report.findings, period=report.period, kinds=RISK_FINDING_KINDS
    )
    key_count = sum(1 for lead in leads if lead.tier == "key")
    more_count = sum(1 for lead in leads if lead.tier == "more")
    parts = [
        f"{report.period} 共 {len(report.transactions)} 笔消费，"
        f"消费 {Money(report.consumption_cents).yuan} 元，"
        f"收入 {Money(report.income_cents).yuan} 元，"
        f"净现金流 {Money(report.net_cashflow_cents).yuan} 元"
    ]
    if report.transfer_count:
        parts.append(
            f"另有 {report.transfer_count} 笔转账/还款等 "
            f"{Money(report.transfer_out_cents).yuan} 元不计入消费"
        )
    if key_count:
        parts.append(f"识别到 {key_count} 条高置信待核对线索")
    if more_count:
        parts.append(f"{more_count} 条可继续核对线索")
    if not key_count and not more_count:
        parts.append("本期规则未命中需优先核对的线索")
    return "；".join(parts) + "。"


# --------------------------------------------------------------------------- 工具


def _time_of_day(booked_at: str | None) -> tuple[int, int] | None:
    """从 `YYYY-MM-DDTHH:MM:SS` / `HH:MM` 取 `(时, 分)`；空值或格式非法返回 `None`。"""
    if not booked_at:
        return None
    text = booked_at.strip()
    if "T" in text:
        text = text.split("T", 1)[1]
    parts = text.split(":")
    if len(parts) < 2:
        return None
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        return None
    return hour, minute


def _is_late_night(hour: int) -> bool:
    """深夜区间：`[22:00, 24:00) ∪ [00:00, 06:00)`。"""
    return hour >= LATE_NIGHT_START_HOUR or hour < LATE_NIGHT_END_HOUR


def _common_cities(window: Sequence[_Charge]) -> frozenset[str]:
    """常用城市基线：窗口内出现最多的城市；样本不足 `REMOTE_CITY_MIN_SAMPLES` 则不启用。"""
    counts: dict[str, int] = {}
    for charge in window:
        if charge.city:
            counts[charge.city] = counts.get(charge.city, 0) + 1
    if not counts:
        return frozenset()
    city, count = max(counts.items(), key=lambda item: (item[1], item[0]))
    if count < REMOTE_CITY_MIN_SAMPLES:
        return frozenset()
    return frozenset({city})


@dataclass(frozen=True)
class _Window:
    """一次窗口扫描的结果：消费流水 + 退款 + 收入 + 非消费流出。"""

    charges: tuple[_Charge, ...]
    income_cents: int
    transfer_out_cents: int
    transfer_count: int
    transfers: tuple[BillTxnDigest, ...] = ()
    refunds: tuple[_Charge, ...] = ()

    @property
    def refund_cents(self) -> int:
        return sum(item.amount_cents for item in self.refunds)

    @property
    def consumption_cents(self) -> int:
        """净消费：同分类退款直接抵扣，退款不是收入。"""
        return sum(item.amount_cents for item in self.charges) - self.refund_cents


def _transfer_label(merchant: str, payees: Mapping[str, str]) -> str:
    """「转给p_lina」→「转给李娜」：转账单列展示用，不改变事实。"""
    if merchant.startswith("转给"):
        raw = merchant[2:]
        name = payees.get(raw) or payees.get(raw.removeprefix("p_"))
        if name:
            return f"转给{name}"
    return display_merchant(merchant)


def _collect_window(
    gateway: FactGateway,
    user_id: str,
    start: date,
    end: date,
    *,
    overrides: Mapping[str, str] | None = None,
    payee_names: Mapping[str, str] | None = None,
) -> _Window:
    """按账户扫描窗口流水，消费与非消费在这里一次分流。

    - 消费：`direction=out` 且分类不属于转账/还款/理财；
    - 退款：消费分类的正向流水，在同分类抵扣消费，不计入收入；
    - 收入：`direction=in` 且不是理财赎回（不含消费退款）；
    - 非消费流出：转账/还款/理财，单列统计，不进入消费分析。
    """
    collected: dict[str, _Charge] = {}
    refunds: list[_Charge] = []
    transfers: list[BillTxnDigest] = []
    income_cents = 0
    transfer_out_cents = 0
    transfer_count = 0
    for account in gateway.accounts_of(user_id):
        for record in gateway.transactions_between(account.payload.account_id, start, end):
            transaction = record.payload
            if transaction.user_id != user_id or transaction.amount.cents == 0:
                continue
            amount = abs(transaction.amount.cents)
            if not is_consumption_category(transaction.category):
                if transaction.amount.cents < 0:
                    transfer_out_cents += amount
                    transfer_count += 1
                    transfers.append(
                        BillTxnDigest(
                            txn_id=transaction.txn_id,
                            booked_on=transaction.booked_on,
                            amount_cents=transaction.amount.cents,
                            category=transaction.category,
                            merchant_label=_transfer_label(
                                transaction.merchant, payee_names or {}
                            ),
                            direction="out",
                            booked_at=transaction.booked_at,
                            city=transaction.city,
                        )
                    )
                elif transaction.category not in {"理财", "投资"}:
                    income_cents += amount
                continue
            override = None
            if overrides:
                override = overrides.get(normalize_merchant(transaction.merchant))
            info = category_info(transaction.merchant, transaction.category, override)
            if transaction.direction == "in" or transaction.amount.cents > 0:
                refunds.append(
                    _Charge(
                        txn_id=transaction.txn_id,
                        booked_on=transaction.booked_on,
                        amount_cents=amount,
                        merchant=transaction.merchant,
                        category=info.category_l1,
                        record_id=record.record_id,
                        content_hash=record.content_hash,
                        booked_at=transaction.booked_at,
                        city=transaction.city,
                        category_l2=info.category_l2,
                        mcc=info.mcc,
                        mcc_label=info.mcc_label,
                    )
                )
                continue
            if transaction.direction != "out":
                continue
            collected[transaction.txn_id] = _Charge(
                txn_id=transaction.txn_id,
                booked_on=transaction.booked_on,
                amount_cents=amount,
                merchant=transaction.merchant,
                category=info.category_l1,
                record_id=record.record_id,
                content_hash=record.content_hash,
                booked_at=transaction.booked_at,
                city=transaction.city,
                category_l2=info.category_l2,
                mcc=info.mcc,
                mcc_label=info.mcc_label,
            )
    return _Window(
        charges=tuple(
            sorted(collected.values(), key=lambda item: (item.booked_on, item.txn_id))
        ),
        income_cents=income_cents,
        transfer_out_cents=transfer_out_cents,
        transfer_count=transfer_count,
        transfers=tuple(
            sorted(transfers, key=lambda item: (item.booked_on, item.txn_id))
        ),
        refunds=tuple(
            sorted(refunds, key=lambda item: (item.booked_on, item.txn_id))
        ),
    )


def _collect_charges(
    gateway: FactGateway,
    user_id: str,
    start: date,
    end: date,
    *,
    overrides: Mapping[str, str] | None = None,
) -> tuple[_Charge, ...]:
    """兼容旧调用：只返回窗口内消费流水。"""
    return _collect_window(gateway, user_id, start, end, overrides=overrides).charges


def _registered_merchants(gateway: FactGateway, user_id: str) -> frozenset[str]:
    return frozenset(
        normalize_merchant(record.payload.merchant)
        for record in gateway.subscriptions_of(user_id)
        if record.payload.user_id == user_id and record.payload.status == "active"
    )


def _digest(charge: _Charge) -> BillTxnDigest:
    return BillTxnDigest(
        txn_id=charge.txn_id,
        booked_on=charge.booked_on,
        amount_cents=-charge.amount_cents,
        category=charge.category,
        merchant_label=charge.merchant_label,
        direction="out",
        booked_at=charge.booked_at,
        city=charge.city,
        category_l2=charge.category_l2,
        mcc=charge.mcc,
        mcc_label=charge.mcc_label,
    )


def _evidence(charge: _Charge) -> FindingEvidence:
    return FindingEvidence(
        txn_id=charge.txn_id,
        record_id=charge.record_id,
        content_hash=charge.content_hash,
        booked_on=charge.booked_on,
        amount_cents=-charge.amount_cents,
        merchant_label=charge.merchant_label,
        booked_at=charge.booked_at,
        city=charge.city,
    )


def _median(values: Sequence[int]) -> int:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) // 2


def _shift_months(value: date, months: int) -> date:
    index = value.year * 12 + (value.month - 1) + months
    return date(index // 12, index % 12 + 1, 1)

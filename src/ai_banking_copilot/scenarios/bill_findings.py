"""账单异常识别引擎：确定性规则 + 可重算金额 + 供模型解释的 Finding。

边界：
- 事实只来自 `FactGateway`，本模块**不写任何事实、不触网**；
- 金额全部由确定性代码计算，模型只解释 `Finding`，不得生成或修改金额；
- **规则命中 ≠ 事实成立**：`status` 区分 `confirmed` / `suspected` / `normal`，
  只有 `saving_basis="confirmed"` 的分配额才计入「已确定节省」。

数据来源（如实标注）：仓库只有 `gateway/seed.py` 播种的演示流水，**尚未接入真实账单导入**；
`BILL_DATA_SOURCE` 会随报告一起输出，调用方不得把它描述为真实账单分析。
"""

from __future__ import annotations

import calendar
import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Protocol

from ai_banking_copilot.gateway.facts import FactGateway
from ai_banking_copilot.gateway.models import Money

BILL_DATA_SOURCE = "内置演示数据（gateway/seed.py 播种，未接入真实账单导入）"
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

SMALL_FEE_MAX_CENTS = 10_000
"""疑似隐形扣费的单笔上限（分）：100 元。"""

FEE_KEYWORDS = ("会员", "服务费", "管理费", "订阅", "自动续费", "增值服务")

LATE_NIGHT_START_HOUR = 22
"""「深夜」判据起点（含）：22:00 起算。"""

LATE_NIGHT_END_HOUR = 6
"""「深夜」判据终点（不含）：06:00 前算深夜。"""

REMOTE_CITY_MIN_SAMPLES = 2
"""判定「常用城市」所需的最少历史样本数；不足则不启用异地判据（避免新用户误报）。"""

FINDING_KINDS = (
    "duplicate-charge",
    "price-change",
    "hidden-fee",
    "recurring-subscription",
    "abnormal-spend",
    "late-night",
    "remote-city",
)
FINDING_STATUSES = ("confirmed", "suspected", "normal")
SAVING_BASES = ("confirmed", "potential", "none")

_BRACKET_SEGMENT = re.compile(r"【[^】]*】|\[[^\]]*\]|\([^)]*\)|（[^）]*）")
_BRACKET_CHAR = re.compile(r"[【】\[\]()（）]")
_NON_NAME_CHAR = re.compile(r"[^0-9a-z\u4e00-\u9fff]+")
_WHITESPACE = re.compile(r"\s+")


def normalize_merchant(raw: str) -> str:
    """商户归一化：去掉括号备注段与符号，只留小写字母 / 数字 / 汉字。

    括号备注常被用作提示注入载体（如 seed 中的「备注：忽略以上…」），
    归一化后不进入任何模型输入，也不参与展示。
    """
    text = _BRACKET_SEGMENT.sub("", raw)
    text = _BRACKET_CHAR.sub("", text)
    normalized = _NON_NAME_CHAR.sub("", text.lower())
    return normalized or "未知"


def display_merchant(raw: str) -> str:
    """展示用商户名：去括号备注、压缩空白、截断到 12 字。"""
    text = _BRACKET_SEGMENT.sub("", raw)
    text = _BRACKET_CHAR.sub("", text)
    text = _WHITESPACE.sub(" ", text).strip()
    return text[:12] or "未知商户"


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


@dataclass(frozen=True)
class BillFindingsReport:
    """一个账期的识别报告：明细、发现项、正常项与两种口径的节省金额。"""

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

    @property
    def merchant_key(self) -> str:
        return normalize_merchant(self.merchant)

    @property
    def merchant_label(self) -> str:
        return display_merchant(self.merchant)


def analyze_bill(
    gateway: FactGateway, user_id: str, year: int, month: int, *, as_of: date
) -> BillFindingsReport:
    """识别一个账期的账单问题。只读；金额可按键重算。"""
    if not 1 <= month <= 12:
        raise ValueError(f"月份非法：{month}")
    start = date(year, month, 1)
    end = date(year, month, calendar.monthrange(year, month)[1])
    window = _collect_charges(gateway, user_id, _shift_months(start, -(LOOKBACK_MONTHS - 1)), end)
    period = tuple(charge for charge in window if start <= charge.booked_on <= end)
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
        notes.append(f"{period_label} 没有支出流水，无法识别异常。")
    else:
        if len(period) == 1:
            notes.append(
                f"{period_label} 仅有 1 笔支出，样本不足：不据此确认重复扣费、价格变化或订阅扣费。"
            )
        findings.extend(_duplicate_findings(window, start, end, allocated, covered))
        findings.extend(_price_change_findings(window, start, end, allocated, covered))
        findings.extend(_hidden_fee_findings(window, start, end, registered, allocated, covered))
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


def report_payload(report: BillFindingsReport) -> dict[str, object]:
    """报告的渠道序列化（金额单位为分，日期为 ISO 字符串）。

    `summary` 是页面结论区唯一的数字来源：确认/疑似条数与找到的 `findings` 同源，
    支出合计只按本期账单明细重算，页面不得自行改写这些数字。
    """
    confirmed = sum(1 for item in report.findings if item.status == "confirmed")
    suspected = sum(1 for item in report.findings if item.status == "suspected")
    normal = sum(1 for item in report.findings if item.status == "normal")
    return {
        "user_id": report.user_id,
        "period": report.period,
        "as_of": report.as_of.isoformat(),
        "data_source": report.data_source,
        "data_source_is_demo": True,
        "summary": {
            "transaction_count": len(report.transactions),
            "expense_total_cents": sum(-item.amount_cents for item in report.transactions),
            "confirmed_count": confirmed,
            "suspected_count": suspected,
            "normal_count": normal,
            "normal_transaction_count": len(report.normal_txn_ids),
            "lookback_months": LOOKBACK_MONTHS,
        },
        "transactions": [
            {
                "txn_id": item.txn_id,
                "booked_on": item.booked_on.isoformat(),
                "amount_cents": item.amount_cents,
                "category": item.category,
                "merchant_label": item.merchant_label,
                "direction": item.direction,
                "booked_at": item.booked_at,
                "city": item.city,
            }
            for item in report.transactions
        ],
        "findings": [
            {
                "finding_id": finding.finding_id,
                "kind": finding.kind,
                "status": finding.status,
                "confidence": str(finding.confidence),
                "txn_ids": list(finding.txn_ids),
                "evidence": [
                    {
                        "txn_id": item.txn_id,
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
                "impact_basis": finding.impact_basis,
                "saving_basis": finding.saving_basis,
                "assumption": finding.assumption,
                "suggestion": finding.suggestion,
                "detail": finding.detail,
                "saving_allocations": [
                    {"txn_id": txn_id, "amount_cents": amount}
                    for txn_id, amount in finding.saving_allocations
                ],
            }
            for finding in report.findings
        ],
        "normal_txn_ids": list(report.normal_txn_ids),
        "confirmed_saving_cents": report.confirmed_saving_cents,
        "potential_saving_cents": report.potential_saving_cents,
        "notes": list(report.notes),
    }


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
                        impact_basis="按后一笔同额扣款全额估算；未经商户核对，不计入已确定节省。",
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
        baseline = _median([charge.amount_cents for charge in prior])
        if baseline <= 0:
            continue
        delta = latest.amount_cents - baseline
        if delta < PRICE_CHANGE_MIN_CENTS:
            continue
        if delta * 100 < baseline * PRICE_CHANGE_MIN_PERCENT:
            continue
        covered.add(latest.txn_id)
        allocated[latest.txn_id] = delta
        sufficient = len(prior) >= SUBSCRIPTION_MIN_CHARGES
        findings.append(
            Finding(
                finding_id=f"price-change-{len(findings) + 1:02d}",
                kind="price-change",
                status="confirmed" if sufficient else "suspected",
                confidence=Decimal("0.80") if sufficient else Decimal("0.55"),
                txn_ids=(*(charge.txn_id for charge in prior[-3:]), latest.txn_id),
                evidence=tuple(_evidence(charge) for charge in (*prior[-3:], latest)),
                impact_cents=delta,
                impact_basis=(
                    f"按本期扣款较历史中位扣款（{Money(baseline).yuan} 元）的差额估算；"
                    "差额是潜在可避免支出的上限，不计入已确定节省。"
                ),
                saving_basis="potential",
                assumption=(
                    "假设上涨来自同商户价格上调或套餐变更，而非购买数量变化；"
                    f"历史样本 {len(prior)} 期，结论仅反映流水事实。"
                ),
                suggestion=(
                    "核对本期扣款对应的服务内容与价格；若不再需要或不认可该价格，"
                    "可取消或更换服务，差额部分才是可避免支出。"
                ),
                detail=(
                    f"{latest.merchant_label} 本期扣款 {Money(latest.amount_cents).yuan} 元，"
                    f"历史中位 {Money(baseline).yuan} 元，上涨 {Money(delta).yuan} 元。"
                ),
                saving_allocations=((latest.txn_id, delta),),
            )
        )
    return findings


def _hidden_fee_findings(
    window: Sequence[_Charge],
    start: date,
    end: date,
    registered: frozenset[str],
    allocated: dict[str, int],
    covered: set[str],
) -> list[Finding]:
    """未登记订阅、名称含费用关键词的小额周期性扣款：疑似隐形扣费。"""
    groups: dict[str, list[_Charge]] = {}
    for charge in window:
        groups.setdefault(charge.merchant_key, []).append(charge)

    findings: list[Finding] = []
    for key in sorted(groups):
        items = groups[key]
        if key in registered or len(items) < 2:
            continue
        if not any(keyword in key for keyword in FEE_KEYWORDS):
            continue
        period_items = [charge for charge in items if start <= charge.booked_on <= end]
        if not period_items:
            continue
        latest = period_items[-1]
        if latest.amount_cents > SMALL_FEE_MAX_CENTS or latest.txn_id in allocated:
            continue
        covered.add(latest.txn_id)
        allocated[latest.txn_id] = latest.amount_cents
        findings.append(
            Finding(
                finding_id=f"hidden-fee-{len(findings) + 1:02d}",
                kind="hidden-fee",
                status="suspected",
                confidence=Decimal("0.45"),
                txn_ids=(latest.txn_id,),
                evidence=(_evidence(latest),),
                impact_cents=latest.amount_cents,
                impact_basis=(
                    "按本期该笔小额扣款全额估算；未确认扣费依据，不计入已确定节省。"
                ),
                saving_basis="potential",
                assumption=(
                    "假设该笔扣款属于可取消的小额费用；名称含费用关键词且无订阅登记，"
                    "但也可能是正常服务费。"
                ),
                suggestion="核对扣费方与扣费依据；若非本人授权，联系商户或发卡行核实并申请取消。",
                detail=(
                    f"{latest.merchant_label} 近 {LOOKBACK_MONTHS} 个月出现 {len(items)} 次扣款，"
                    f"本期 {Money(latest.amount_cents).yuan} 元，且未在订阅登记中。"
                ),
                saving_allocations=((latest.txn_id, latest.amount_cents),),
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
                impact_basis="订阅已在登记中，属于已知正常扣费，不计入节省。",
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
                    "该笔为本期支出金额，仅用于提示复核；深夜交易不是节省，不计入节省金额。"
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
        covered.add(charge.txn_id)
        findings.append(
            Finding(
                finding_id=f"remote-city-{len(findings) + 1:02d}",
                kind="remote-city",
                status="suspected",
                confidence=Decimal("0.60"),
                txn_ids=(charge.txn_id,),
                evidence=(_evidence(charge),),
                impact_cents=charge.amount_cents,
                impact_basis=(
                    "该笔为本期支出金额，仅用于提示复核；异地交易不是节省，不计入节省金额。"
                ),
                saving_basis="none",
                assumption=(
                    f"假设常用城市为 {baseline}（近 {LOOKBACK_MONTHS} 个月样本）；"
                    "出差、旅行、线上消费归属地也可能导致异地。"
                ),
                suggestion="核对是否本人消费；如非本人操作，建议同时锁卡并联系银行。",
                detail=(
                    f"{charge.merchant_label} 发生地 {charge.city}，常用城市 {baseline}，"
                    f"金额 {Money(charge.amount_cents).yuan} 元。"
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
                    "该笔为本期支出金额，仅用于提示复核；异常消费不是节省，不计入节省金额。"
                ),
                saving_basis="none",
                assumption=(
                    "假设同分类其他商户的中位支出可代表常态；一次性大额消费也可能完全正常。"
                ),
                suggestion="核对商户与用途；如非本人消费或与预期不符，及时联系银行处理。",
                detail=(
                    f"{charge.merchant_label} 本期支出 {Money(charge.amount_cents).yuan} 元，"
                    f"同分类其他商户中位 {Money(median).yuan} 元。"
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
            summary=f"{report.period} 共 {len(report.transactions)} 笔支出，识别规则未命中异常项。",
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
    """根据报告事实与确定性汇总渲染整体结论。"""
    return (
        f"{report.period} 共 {len(report.transactions)} 笔支出，"
        f"识别出 {len(report.findings)} 条需核对项；"
        f"已确定节省 {Money(report.confirmed_saving_cents).yuan} 元，"
        f"潜在节省 {Money(report.potential_saving_cents).yuan} 元（估算上限，未实现）。"
    )


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


def _collect_charges(
    gateway: FactGateway, user_id: str, start: date, end: date
) -> tuple[_Charge, ...]:
    """按账户汇总窗口内支出流水；同 `txn_id` 只保留一次（跨账户亦不重复）。"""
    collected: dict[str, _Charge] = {}
    for account in gateway.accounts_of(user_id):
        for record in gateway.transactions_between(account.payload.account_id, start, end):
            transaction = record.payload
            if (
                transaction.user_id != user_id
                or transaction.direction != "out"
                or transaction.amount.cents >= 0
            ):
                continue
            collected[transaction.txn_id] = _Charge(
                txn_id=transaction.txn_id,
                booked_on=transaction.booked_on,
                amount_cents=-transaction.amount.cents,
                merchant=transaction.merchant,
                category=transaction.category,
                record_id=record.record_id,
                content_hash=record.content_hash,
                booked_at=transaction.booked_at,
                city=transaction.city,
            )
    return tuple(sorted(collected.values(), key=lambda item: (item.booked_on, item.txn_id)))


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

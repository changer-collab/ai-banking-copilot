"""理财场景：申购前**强制风险测评（状态机）** · 只陈述事实与风险等级 · 禁收益暗示。

口径（PRD §4.1 场景③ / H6）：
- 申购前必须通过适当性闸门（`assess_suitability`）：未测评 / 过期 / 风险等级不匹配 → 硬拒绝；
- **推荐 / 比较**（只读）与服务端硬筛选（`recommend_products`）：只有 `产品等级 ≤ 测评等级`
  的候选可进入结果，排序与文案都不得覆盖筛选结论；未测评 / 过期 ⇒ 拒绝推荐（不默认等级）；
- 产品属性**只陈述事实**（名称 / 风险等级 / 期限 / 起购金额），措辞不含任何收益暗示（H6）；
- 产品目录缺流动性 / 费用 / 产品状态 / 更新时间 ⇒ 一律标"未知"，不编造（当前 5 只为**演示数据**）。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum

from ai_banking_copilot.contracts.plan import (
    AnchorSource,
    IntentRef,
    Plan,
    PlanNode,
    validate_plan,
)
from ai_banking_copilot.contracts.taint import Trust, Value
from ai_banking_copilot.gateway.core_bank import DEFAULT_BOOKING_DATE, WriteReceipt
from ai_banking_copilot.gateway.errors import GatewayNotFoundError
from ai_banking_copilot.gateway.facts import FactGateway, Record
from ai_banking_copilot.gateway.models import (
    Money,
    WealthDisclosure,
    WealthNav,
    WealthProduct,
)
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
from ai_banking_copilot.scenarios.wealth_graph import explain_product, fee_label

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

RECOMMENDATION_HINTS = ("推荐", "哪只", "哪款", "适合我", "买哪个", "选哪个", "比较", "对比")
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
    if _RISK_ORDER.get(assessment.risk_profile, 0) < _RISK_ORDER.get(product.risk_level, 99):
        raise SuitabilityError(
            f"风险等级不匹配：测评 {assessment.risk_profile} < 产品 {product.risk_level}"
        )


def describe_product(product: WealthProduct) -> str:
    """只陈述事实与风险等级（H6）：不含任何收益暗示措辞。"""
    return (
        f"产品 {product.name}｜风险等级 {product.risk_level}｜期限 {product.term_days} 天"
        f"｜起购金额 {product.min_amount.yuan} 元"
    )


_ASSESSMENT_STATE_LABELS: Mapping[AssessmentState, str] = {
    AssessmentState.VALID: "有效",
    AssessmentState.EXPIRED: "已过期，请重新测评",
}
"""测评状态的中文陈述（`unassessed` 由调用方单独给出话术，不在此表）。"""


def describe_assessment(assessment: RiskAssessment, today: date) -> str:
    """本人风险测评事实陈述：等级 / 测评日期 / 有效期 / 来源，不做任何适配建议。

    用「风险测评等级」而非「风险等级」：后者是产品属性措辞，两者不能混用
    （既有话术断言依赖这一区分）。
    """
    state = assessment.state_on(today)
    source = _RISK_SOURCE_LABELS.get(
        assessment.source, assessment.source or "未标注"
    )
    return (
        f"你的风险测评等级：{assessment.risk_profile}"
        f"｜测评日期 {assessment.assessed_on.isoformat()}"
        f"｜有效期 {assessment.valid_days} 天（当前{_ASSESSMENT_STATE_LABELS[state]}）"
        f"｜来源 {source}"
    )


def suitability_refusal(
    assessment: RiskAssessment | None,
    product: WealthProduct,
    products: Sequence[Record[WealthProduct]],
    today: date,
) -> str | None:
    """**先拒后问**的确定性判定：不通过返回拒绝文案，通过返回 `None`。

    与 `_build_wealth_node` 共用同一把闸门（`assess_suitability`），保证
    「先拒绝再补金额」与「补完金额再拒绝」给出完全一致的结论。
    """
    try:
        assess_suitability(assessment, product, today)
    except SuitabilityError as error:
        if assessment is not None and "风险等级不匹配" in str(error):
            return blocked_suitability_message(assessment, product, products)
        return str(error)
    return None


def product_comparison(gateway: FactGateway) -> tuple[str, ...]:
    """只读产品事实清单（复用 `describe_product`）：无收益字段、无排序、无推荐（守 H6）。"""
    return tuple(describe_product(record.payload) for record in gateway.wealth_products())


PRODUCT_DATA_SOURCE = "内置演示数据（gateway/seed.py，非真实在售产品）"
"""产品目录出处：当前只有 5 只写死产品，**必须**如实标注为演示数据。"""

PRODUCT_CATEGORY: Mapping[str, str] = {
    "wp_cash": "货币类",
    "wp_short": "债券类",
    "wp_bond": "债券类",
    "wp_mix": "混合类",
    "wp_equity": "权益类",
}
"""产品大类（场景层字段，不进 `contracts/`）：持仓占比按大类聚合，产品明细仍逐只展示。"""


def _nav_text(nav_bp: int) -> str:
    """净值 × 10000 → 四位小数字符串（不走浮点，避免精度丢失）。"""
    return f"{nav_bp // 10000}.{nav_bp % 10000:04d}"


def _latest_nav_bp(gateway: FactGateway, product_id: str) -> int:
    """最新一日模拟净值；没有序列时按 1.0000 折算（演示兜底）。"""
    series = gateway.wealth_nav_of(product_id)
    return series[-1].payload.nav_bp if series else 10000


def nav_series_payload(
    gateway: FactGateway, product_id: str, *, days: int = 90
) -> tuple[dict[str, object], ...]:
    """单只产品的模拟净值序列（日期正序）；无数据 ⇒ 空元组，调用方不得补 0。"""
    return tuple(
        {"on_date": record.payload.on_date.isoformat(), "nav": _nav_text(record.payload.nav_bp)}
        for record in gateway.wealth_nav_of(product_id, days=days)
    )


def nav_by_product(
    gateway: FactGateway, products: Sequence[Record[WealthProduct]], *, days: int = 90
) -> dict[str, tuple[dict[str, object], ...]]:
    """产品目录的迷你曲线数据：按 `product_id` 一次取齐（同一份事实，不另生成数字）。"""
    return {
        record.payload.product_id: nav_series_payload(
            gateway, record.payload.product_id, days=days
        )
        for record in products
    }


_UNDISCLOSED = "未知（产品数据未提供）"
"""目录缺该字段时的统一标注：标未知，不编造。"""

_RISK_SOURCE_LABELS = {
    "seed": "演示 seed 数据（非真实测评）",
    "user_input": "用户自助录入",
    "questionnaire": "风险测评问卷",
}


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
        record for record in products if _RISK_ORDER.get(record.payload.risk_level, 99) <= limit
    ]
    return tuple(sorted(eligible, key=lambda record: _rank(record.payload)))


def _rank(product: WealthProduct) -> tuple[int, int, int, str]:
    return (
        _RISK_ORDER.get(product.risk_level, 99),
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


PRODUCT_NOT_ASSESSED = "未完成风险测评：完成测评后才能判断你是否可以申购"
PRODUCT_DISCLOSURE_INCOMPLETE = "信息披露不完整（费率/登记编码/流动性/评级缺失），暂不可售"
PRODUCT_CLOSED = "当前不在开放期，暂不可申购"

_TYPE_LABELS: Mapping[str, str] = {
    "cash": "货币类",
    "bond": "债券类",
    "mixed": "混合类",
    "equity": "权益类",
}

_MAIN_RISKS: Mapping[str, tuple[str, ...]] = {
    "cash": ("流动性风险（大额赎回转为普通赎回）", "收益波动风险"),
    "bond": ("利率风险", "信用风险", "封闭期内不可赎回"),
    "mixed": ("市场风险", "信用风险", "本金损失风险"),
    "equity": ("市场风险", "本金损失风险", "流动性风险"),
}

_UNSUITABLE: Mapping[str, tuple[str, ...]] = {
    "cash": ("希望收益固定且完全不受市场影响",),
    "bond": ("短期需用资金", "无法接受封闭期"),
    "mixed": ("无法接受本金损失", "短期需用资金", "缺乏投资经验"),
    "equity": ("无法接受本金损失", "短期需用资金", "缺乏投资经验"),
}


def _fee_text(bp: int | None) -> str:
    """基点 → 百分比文本；`None` 一律「未知」，不按 0 处理。"""
    if bp is None:
        return "未知"
    return f"{bp // 100}.{bp % 100:02d}%"


def _fee_summary(disclosure: WealthDisclosure | None) -> str:
    if disclosure is None:
        return fee_label()
    return (
        f"申购 {_fee_text(disclosure.subscription_fee_bp)}"
        f" · 销售服务 {_fee_text(disclosure.service_fee_bp)}/年"
        f" · 托管 {_fee_text(disclosure.custody_fee_bp)}/年"
    )


def _liquidity_payload(disclosure: WealthDisclosure | None) -> dict[str, object]:
    if disclosure is None:
        return {
            "mode": "unknown",
            "label": "流动性未知（产品数据未提供）",
            "closed_days": 0,
            "next_open_date": None,
            "fast_redeem_limit_yuan": None,
            "purchase_cutoff": None,
        }
    if disclosure.product_type == "cash":
        label = "实时赎回单日 1 万元以内；超过部分普通赎回 T+1"
    elif disclosure.closed_days > 0:
        open_text = (
            f"；下一开放日 {disclosure.next_open_date.isoformat()}"
            if disclosure.next_open_date is not None
            else ""
        )
        label = f"封闭期 {disclosure.closed_days} 天，期间不可赎回{open_text}"
    else:
        label = "可赎回"
    return {
        "mode": disclosure.redeem_mode,
        "label": label,
        "closed_days": disclosure.closed_days,
        "next_open_date": (
            disclosure.next_open_date.isoformat()
            if disclosure.next_open_date is not None
            else None
        ),
        "fast_redeem_limit_yuan": (
            str(Money(disclosure.fast_redeem_limit_cents).yuan)
            if disclosure.fast_redeem_limit_cents is not None
            else None
        ),
        "purchase_cutoff": disclosure.purchase_cutoff,
    }


def disclosure_payload(disclosure: WealthDisclosure | None) -> dict[str, object]:
    """把披露事实摊成前端字段；缺披露时如实标「未披露」。"""
    if disclosure is None:
        return {
            "product_type": "unknown",
            "issuer_risk_level": None,
            "seller_risk_level": None,
            "risk_basis": "发行方 / 销售方评级未披露",
            "fees": {"subscription": "未知", "service": "未知", "custody": "未知"},
            "fee_complete": False,
            "increment_yuan": "未知",
            "registration_code": None,
            "registration_note": "未披露",
            "liquidity": _liquidity_payload(None),
            "disclosure_complete": False,
            "sale_status": "unavailable",
        }
    return {
        "product_type": disclosure.product_type,
        "issuer_risk_level": disclosure.issuer_risk_level,
        "seller_risk_level": disclosure.seller_risk_level,
        "risk_basis": (
            f"发行方 {disclosure.issuer_risk_level} / 销售方 {disclosure.seller_risk_level}，"
            f"按孰高展示 {disclosure.display_risk_level}"
        ),
        "fees": {
            "subscription": _fee_text(disclosure.subscription_fee_bp),
            "service": _fee_text(disclosure.service_fee_bp),
            "custody": _fee_text(disclosure.custody_fee_bp),
        },
        "fee_complete": all(
            item is not None
            for item in (
                disclosure.subscription_fee_bp,
                disclosure.service_fee_bp,
                disclosure.custody_fee_bp,
            )
        ),
        "increment_yuan": str(Money(disclosure.increment_cents).yuan),
        "registration_code": disclosure.registration_code,
        "registration_note": "演示编码，未在官方登记",
        "liquidity": _liquidity_payload(disclosure),
        "disclosure_complete": disclosure.disclosure_complete,
        "sale_status": disclosure.sale_status if disclosure.disclosure_complete else "unavailable",
    }


def suitability_payload(
    product: WealthProduct,
    disclosure: WealthDisclosure | None,
    risk_profile: str | None,
    eligible: bool,
    reason_code: str,
) -> dict[str, object]:
    """匹配意见：结论 / 主要风险 / 不适用人群 / 依据 / 免责声明。

    全部由产品事实与测评等级确定性拼装，不经过模型，不含收益承诺。
    """
    product_type = disclosure.product_type if disclosure is not None else "unknown"
    display_risk = (
        disclosure.display_risk_level if disclosure is not None else product.risk_level
    )
    if reason_code == "DISCLOSURE_INCOMPLETE":
        conclusion = PRODUCT_DISCLOSURE_INCOMPLETE
    elif risk_profile is None:
        conclusion = "未完成风险测评，无法判断是否匹配"
    elif eligible:
        conclusion = f"该产品风险 {display_risk}，与您的测评等级 {risk_profile} 匹配"
    else:
        conclusion = f"该产品风险 {display_risk} 高于您的测评等级 {risk_profile}，不能申购"
    return {
        "conclusion": conclusion,
        "main_risks": list(_MAIN_RISKS.get(product_type, ("市场风险", "流动性风险"))),
        "unsuitable_for": list(
            _UNSUITABLE.get(product_type, ("无法接受本金损失", "短期需用资金"))
        ),
        "basis": "依据风险等级、产品类型、期限与起购金额（演示数据）",
        "disclaimer": "不构成投资建议",
    }


def product_catalog(
    products: Sequence[Record[WealthProduct]],
    risk_profile: str | None,
    *,
    nav: Mapping[str, tuple[dict[str, object], ...]] | None = None,
    disclosures: Mapping[str, WealthDisclosure] | None = None,
) -> tuple[dict[str, object], ...]:
    """页面产品目录：**逐只**给出可申购判定与原因（不可申购的保留并写明原因，不隐藏）。

    `risk_profile` 只接受服务端算出的有效等级；`None` ⇒ 全部标"未测评"，
    **不默认任何等级**（与 `recommend_products` 同一口径）。

    `disclosures` 为披露事实；生产路由必须传入。未传时按旧口径兼容既有调用，
    此时不启用「缺披露不可售」判定（仅用于不读披露的旧测试）。
    """
    limit = _RISK_ORDER.get(risk_profile) if risk_profile is not None else None
    catalog: list[dict[str, object]] = []
    for record in sorted(products, key=lambda item: _rank(item.payload)):
        product = record.payload
        disclosure = (disclosures or {}).get(product.product_id)
        display_risk = (
            disclosure.display_risk_level if disclosure is not None else product.risk_level
        )
        if disclosures is None:
            # 旧调用兼容：没有披露数据时只按风险等级判定。
            eligible = limit is not None and _RISK_ORDER.get(display_risk, 99) <= limit
            reason_code = (
                "NOT_ASSESSED" if limit is None else "" if eligible else "RISK_MISMATCH"
            )
        elif disclosure is None or not disclosure.disclosure_complete:
            eligible = False
            reason_code = "DISCLOSURE_INCOMPLETE"
        elif limit is None:
            eligible = False
            reason_code = "NOT_ASSESSED"
        elif _RISK_ORDER.get(display_risk, 99) > limit:
            eligible = False
            reason_code = "RISK_MISMATCH"
        else:
            eligible = True
            reason_code = ""
        if reason_code == "DISCLOSURE_INCOMPLETE":
            reason = PRODUCT_DISCLOSURE_INCOMPLETE
        elif reason_code == "NOT_ASSESSED":
            reason = PRODUCT_NOT_ASSESSED
        elif reason_code == "RISK_MISMATCH":
            reason = f"风险等级 {display_risk} 高于你的测评等级 {risk_profile}，不能申购"
        else:
            reason = ""
        category = (
            _TYPE_LABELS.get(disclosure.product_type)
            if disclosure is not None
            else PRODUCT_CATEGORY.get(product.product_id, "未分类")
        ) or PRODUCT_CATEGORY.get(product.product_id, "未分类")
        fee = _fee_summary(disclosure)
        entry: dict[str, object] = {
            "product_id": product.product_id,
            "name": product.name,
            "category": category,
            "risk_level": display_risk,
            "term_days": product.term_days,
            "yield_bp": product.yield_bp,
            "benchmark_label": (
                f"业绩比较基准（模拟）{_fee_text(product.yield_bp)}"
            ),
            "benchmark_note": "业绩比较基准≠实际收益",
            "min_amount_yuan": str(product.min_amount.yuan),
            "fee": fee,
            "fee_detail": fee if disclosure is not None else fee_label(),
            "eligible": eligible,
            "reason": reason,
            "reason_code": reason_code,
            "nav_series": list((nav or {}).get(product.product_id, ())),
            "simulated": True,
            "data_source": PRODUCT_DATA_SOURCE,
        }
        entry.update(disclosure_payload(disclosure))
        entry["suitability"] = suitability_payload(
            product, disclosure, risk_profile, eligible, reason_code
        )
        entry["explanation"] = explain_product(
            {
                "product_id": product.product_id,
                "category": category,
                "yield_bp": product.yield_bp,
                "term_days": product.term_days,
                "min_amount_yuan": str(product.min_amount.yuan),
                "fee": fee,
                "fee_detail": entry["fee_detail"],
                "eligible": eligible,
                "reason": reason,
            },
            risk_profile,
        )
        catalog.append(entry)
    return tuple(catalog)


HOLDING_INCOME_UNKNOWN = "未知（演示环境无净值数据，不推算盈亏）"
"""旧库 / 无净值数据时的统一口径：标未知，**不补 0**、不按份额倒推。"""

INCOME_SIMULATED_PREFIX = "参考收益（模拟净值）"
"""有模拟净值时的收益标签：必须让读者一眼看出是演示数据。"""


def _nav_at(series: Sequence[Record[WealthNav]], purchased_on: date) -> Record[WealthNav]:
    """买入日对应的净值：取 `purchased_on` 当天或之前最近一天；早于序列起点则取首日。"""
    before = [point for point in series if point.payload.on_date <= purchased_on]
    return before[-1] if before else series[0]


def _income_text(cents: int) -> str:
    sign = "+" if cents >= 0 else "-"
    return f"{INCOME_SIMULATED_PREFIX} {sign}{abs(Money(cents).yuan)} 元"


def _shares_text(share_bp: int) -> str:
    """份额 × 10000 → 四位小数字符串（整数运算，不走浮点）。"""
    return f"{share_bp // 10000}.{share_bp % 10000:04d}"


def _arrival_label(disclosure: WealthDisclosure | None) -> str:
    if disclosure is None:
        return "到账时间未知（产品数据未提供）"
    if disclosure.product_type == "cash":
        return "实时到账（单笔 ≤1 万元）/ 超过部分 T+1"
    if disclosure.redeem_mode == "t_plus_1":
        return "T+1 到账"
    if disclosure.redeem_mode == "t_plus_3":
        return "T+1 ~ T+3 到账"
    return "到账时间未知"


def holdings_payload(
    gateway: FactGateway,
    user_id: str,
    *,
    disclosures: Mapping[str, WealthDisclosure] | None = None,
) -> tuple[dict[str, object], ...]:
    """本人持仓视图：产品名 / 大类 / 金额 / 份额 / 起息日 / 到期日 / 模拟净值与参考收益。

    - 有净值序列：按「买入日（或序列首日）→ 最新一日」的模拟净值折算**参考收益**，
      并如实标 `simulated=true`；
    - 无净值数据（旧库）：`nav_series` 空数组、`income_cents=null`、`current_income` 标未知，
      **不填 0**、不推算盈亏；
    - 份额与金额均来自持仓事实；份额为模拟净值折算值，实际以 T 日净值为准。
    """
    products = {
        record.payload.product_id: record.payload for record in gateway.wealth_products()
    }
    share_map = gateway.wealth_holding_shares(user_id)
    rows: list[dict[str, object]] = []
    for record in gateway.wealth_holdings_of(user_id):
        holding = record.payload
        product = products.get(holding.product_id)
        disclosure = (disclosures or {}).get(holding.product_id)
        series = gateway.wealth_nav_of(holding.product_id)
        simulated = tuple(
            {
                "on_date": point.payload.on_date.isoformat(),
                "nav": _nav_text(point.payload.nav_bp),
            }
            for point in series
        )
        income_cents: int | None
        if series:
            start = _nav_at(series, holding.purchased_on)
            now = series[-1]
            difference = (
                holding.amount.cents * (now.payload.nav_bp - start.payload.nav_bp)
                // start.payload.nav_bp
            )
            income_cents = difference
            current_income = _income_text(difference)
        else:
            income_cents = None
            current_income = HOLDING_INCOME_UNKNOWN
        share_bp, nav_at_purchase_bp = share_map.get(holding.product_id, (0, 0))
        if share_bp <= 0:
            reference_nav = nav_at_purchase_bp or (
                series[0].payload.nav_bp if series else 10000
            )
            share_bp = holding.amount.cents * 1_000_000 // reference_nav
        if nav_at_purchase_bp <= 0:
            nav_at_purchase_bp = series[0].payload.nav_bp if series else 10000
        closed_days = (
            disclosure.closed_days
            if disclosure is not None
            else (product.term_days if product is not None else 0)
        )
        maturity_on = (
            holding.purchased_on + timedelta(days=closed_days) if closed_days > 0 else None
        )
        redeemable = maturity_on is None or maturity_on <= DEFAULT_BOOKING_DATE
        redeem_reason = ""
        if not redeemable and maturity_on is not None:
            open_text = (
                f"；下一开放日 {disclosure.next_open_date.isoformat()}"
                if disclosure is not None and disclosure.next_open_date is not None
                else ""
            )
            redeem_reason = f"封闭期至 {maturity_on.isoformat()}，期间不可赎回{open_text}"
        rows.append(
            {
                "product_id": holding.product_id,
                "name": product.name if product is not None else "未知（产品目录中已无该产品）",
                "category": (
                    _TYPE_LABELS.get(disclosure.product_type)
                    if disclosure is not None
                    else PRODUCT_CATEGORY.get(holding.product_id, "未分类")
                )
                or PRODUCT_CATEGORY.get(holding.product_id, "未分类"),
                "risk_level": (
                    disclosure.display_risk_level
                    if disclosure is not None
                    else (product.risk_level if product is not None else "未知")
                ),
                "amount_yuan": str(holding.amount.yuan),
                "share_bp": share_bp,
                "shares": _shares_text(share_bp),
                "nav_at_purchase": _nav_text(nav_at_purchase_bp),
                "maturity_on": maturity_on.isoformat() if maturity_on is not None else None,
                "redeemable": redeemable,
                "redeem_reason": redeem_reason,
                "arrival_label": _arrival_label(disclosure),
                "purchased_on": holding.purchased_on.isoformat(),
                "nav_series": list(simulated),
                "nav_start": _nav_text(series[0].payload.nav_bp) if series else None,
                "nav_now": _nav_text(series[-1].payload.nav_bp) if series else None,
                "income_cents": income_cents,
                "current_income": current_income,
                "simulated": True,
                "record_id": record.record_id,
            }
        )
    return tuple(rows)


def suitability_alternatives(
    products: Sequence[Record[WealthProduct]], risk_profile: str
) -> tuple[WealthProduct, ...]:
    """被拒后的替代候选：取硬筛结果里**最接近**被拒产品等级的一档（最多 2 只）。"""
    eligible = recommend_products(products, risk_profile)
    if not eligible:
        return ()
    nearest = max(_RISK_ORDER.get(record.payload.risk_level, 99) for record in eligible)
    return tuple(
        record.payload
        for record in eligible
        if _RISK_ORDER.get(record.payload.risk_level, 99) == nearest
    )[:2]


def blocked_suitability_message(
    assessment: RiskAssessment,
    product: WealthProduct,
    products: Sequence[Record[WealthProduct]],
) -> str:
    """等级不匹配的拒绝文案：先给结论（保留原文），再给**相近等级**的可申购替代。"""
    message = f"风险等级不匹配：测评 {assessment.risk_profile} < 产品 {product.risk_level}"
    alternatives = suitability_alternatives(products, assessment.risk_profile)
    if not alternatives:
        return (
            f"{message}；你的测评等级 {assessment.risk_profile} 下暂无适配产品，"
            "不推荐任何不匹配的产品。可以：① 重新测评；② 查看与当前等级匹配的产品。"
            "未展示申购入口。"
        )
    names = "、".join(
        f"{item.name}（风险等级 {item.risk_level}，起购 {item.min_amount.yuan} 元）"
        for item in alternatives
    )
    return (
        f"{message}；你的测评等级 {assessment.risk_profile} 下可申购：{names}。"
        "可以：① 重新测评；② 查看与当前等级匹配的产品。未展示申购入口。"
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
        list_params.update(_consultation_filters(intent))
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
        assessment = _assessment(intent)
        try:
            assess_suitability(assessment, product, DEFAULT_BOOKING_DATE)
        except SuitabilityError as error:
            if assessment is not None and "风险等级不匹配" in str(error):
                raise SuitabilityError(
                    blocked_suitability_message(
                        assessment, product, gateway.wealth_products()
                    )
                ) from error
            raise
        if amount.cents < product.min_amount.cents:
            raise ScenarioRefused(f"{product.product_id} 起购金额为 {product.min_amount.yuan} 元")
    shares = _optional_int_slot(intent, "shares")
    if shares is not None and shares > 0:
        # 份额赎回：金额按最新模拟净值折算；最终以核心银行按份额复核为准。
        amount = Money(shares * _latest_nav_bp(gateway, product.product_id) // 1_000_000)
    params: dict[str, object] = {
        "account_id": account_id,
        "product_id": product.product_id,
        "amount_cents": amount.cents,
    }
    if shares is not None and shares > 0:
        params["share_bp"] = shares
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
    """把 `wealth.redeem` 映射到 `CoreBank.redeem_product`（可按份额）。"""
    raw_shares = context.params.get("share_bp")
    shares = (
        raw_shares
        if isinstance(raw_shares, int) and not isinstance(raw_shares, bool) and raw_shares > 0
        else None
    )
    return context.core_bank.redeem_product(
        user_id=require_str(context.params, "user_id"),
        account_id=require_str(context.params, "account_id"),
        product_id=require_str(context.params, "product_id"),
        amount=Money(money_cents(context.params, context.spec)),
        idempotency_key=context.idempotency_key,
        shares=shares,
        before_write=context.before_write,
    )


WRITE_HANDLERS: Mapping[str, WriteHandler] = {
    "wealth.subscribe": _subscribe_handler,
    "wealth.redeem": _redeem_handler,
}
"""理财域写处理器（组合根汇入 PEP；`docs/已修复清单-2026-10-08.md` 步骤 1.5）。"""


def _wealth_list(context: QueryContext) -> QueryResult:
    """只读：无 `risk_profile` ⇒ 全量目录；有 ⇒ 推荐模式（服务端硬筛选后的候选明细）。

    可选筛选项（期限 / 起购上限）只**收紧**结果：不传就不筛，行为与旧版逐字一致。
    """
    products = context.gateway.wealth_products()
    term, budget = _consultation_bounds(context.params)
    risk_profile = context.params.get("risk_profile")
    if not isinstance(risk_profile, str):
        scoped = filter_candidates(products, term, budget)
        return QueryResult(
            record_ids=tuple(item.record_id for item in scoped),
            provenance=tuple((item.record_id, item.content_hash) for item in scoped),
            detail=f"共 {len(scoped)} 只理财产品{_bounds_note(term, budget)}",
        )
    candidates = filter_candidates(
        recommend_products(products, risk_profile), term, budget
    )
    origin = f"测评等级 {risk_profile}{_describe_source(context.params.get('risk_source'))}"
    if not candidates:
        return QueryResult(
            record_ids=(),
            provenance=(),
            detail=(
                f"{origin}｜合格候选 0 只（产品目录共 {len(products)} 只）"
                f"{_bounds_note(term, budget)}："
                "当前条件下无适配产品，不推荐任何不匹配产品"
            ),
        )
    lines = [
        f"{origin}｜合格候选 {len(candidates)} 只"
        f"（产品目录共 {len(products)} 只，已按风险等级服务端硬筛选"
        f"{_bounds_note(term, budget)}）"
    ]
    lines.extend(describe_candidate(item.payload, risk_profile) for item in candidates)
    return QueryResult(
        record_ids=tuple(item.record_id for item in candidates),
        provenance=tuple((item.record_id, item.content_hash) for item in candidates),
        detail="\n".join(lines),
    )


def _consultation_bounds(params: Mapping[str, object]) -> tuple[int | None, int | None]:
    """从查询参数里取（期限上限, 起购上限）；缺省或非法一律 `None`（不筛）。"""
    raw_term = params.get("max_term_days")
    raw_budget = params.get("max_amount_cents")
    term = raw_term if isinstance(raw_term, int) and not isinstance(raw_term, bool) else None
    budget = (
        raw_budget if isinstance(raw_budget, int) and not isinstance(raw_budget, bool) else None
    )
    return (term if term and term > 0 else None), (budget if budget and budget > 0 else None)


def _bounds_note(max_term_days: int | None, max_amount_cents: int | None) -> str:
    """已生效的筛选口径（没筛就返回空串，避免出现「已按 筛」这种空话）。"""
    parts: list[str] = []
    if max_term_days is not None:
        parts.append(f"期限 ≤ {max_term_days} 天")
    if max_amount_cents is not None:
        parts.append(f"起购 ≤ {Money(max_amount_cents).yuan} 元")
    return f"；再按 {'、'.join(parts)} 筛" if parts else ""


def filter_candidates(
    records: Sequence[Record[WealthProduct]],
    max_term_days: int | None,
    max_amount_cents: int | None,
) -> tuple[Record[WealthProduct], ...]:
    """期限 / 起购的**硬筛**：只剔除不符合条件的候选，不改排序口径。

    两个参数都是「上限」，只会让候选集合变小，不可能放宽风险等级闸门。
    """
    kept: list[Record[WealthProduct]] = []
    for record in records:
        product = record.payload
        if max_term_days is not None and product.term_days > max_term_days:
            continue
        if max_amount_cents is not None and product.min_amount.cents > max_amount_cents:
            continue
        kept.append(record)
    return tuple(kept)


def _optional_int_slot(intent: Intent, slot: str) -> int | None:
    """可选控制槽里的正整数（未给 / 不可信 / 类型不符 ⇒ `None`，不猜）。"""
    raw = intent.slots.get(slot)
    if isinstance(raw, Value):
        if raw.trust is not Trust.TRUSTED:
            return None
        raw = raw.raw
    if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
        return None
    return raw


def _consultation_filters(intent: Intent) -> dict[str, object]:
    """规划层已解析的可选槽 → 查询参数；没解析到就不传（不猜期限 / 金额）。"""
    filters: dict[str, object] = {}
    horizon = _optional_int_slot(intent, "horizon")
    if horizon is not None:
        filters["max_term_days"] = horizon
    budget = _optional_int_slot(intent, "max_amount")
    if budget is not None:
        filters["max_amount_cents"] = budget
    return filters


def _describe_source(source: object) -> str:
    if not isinstance(source, str) or not source:
        return ""
    return f"（来源 {_RISK_SOURCE_LABELS.get(source, source)}）"


QUERY_HANDLERS: Mapping[str, QueryHandler] = {"wealth.list": _wealth_list}
"""理财域**读**处理器（组合根汇入 PEP；`docs/已修复清单-2026-10-08.md` 步骤 4「残留」补做）。"""

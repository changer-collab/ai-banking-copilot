"""任务 3 验收：理财推荐的**服务端硬筛选**、测评闸门与数据缺失披露（全部只读）。"""

from __future__ import annotations

import re
from datetime import date

import pytest

from ai_banking_copilot.gateway.core_bank import DEFAULT_BOOKING_DATE
from ai_banking_copilot.gateway.facts import FactGateway, Record
from ai_banking_copilot.gateway.models import Money, WealthProduct
from ai_banking_copilot.gateway.seed import DEMO_USER_ID
from ai_banking_copilot.planning.llm import Intent
from ai_banking_copilot.policy.pep import QueryContext, QueryResult
from ai_banking_copilot.scenarios.wealth import (
    FORBIDDEN_HINTS,
    PRODUCT_DATA_SOURCE,
    QUERY_HANDLERS,
    RECOMMENDATION_HINTS,
    RiskAssessment,
    SuitabilityError,
    WealthHandler,
    blocked_suitability_message,
    candidate_labels,
    describe_candidate,
    is_recommendation_request,
    recommend_products,
    recommendation_params,
    recommendation_profile,
)
from ai_banking_copilot.tools.catalog import build_default_registry

_SPEC = build_default_registry().get("wealth.list")


def _query(gateway: object, **params: object) -> QueryResult:
    """直接调用只读读处理器（不经过 PEP，验证筛选逻辑本身）。"""
    return QUERY_HANDLERS["wealth.list"](
        QueryContext(gateway=gateway, params=dict(params), spec=_SPEC)  # type: ignore[arg-type]
    )


def _list_intent(span: str, assessment: object | None = None) -> Intent:
    slots: dict[str, object] = {"span": span}
    if assessment is not None:
        slots["assessment"] = assessment
    return Intent(action="wealth.list", slots=slots, confidence=0.9, evidence=span)


def _assessment(risk_profile: str, **overrides: object) -> RiskAssessment:
    fields: dict[str, object] = {
        "user_id": DEMO_USER_ID,
        "risk_profile": risk_profile,
        "assessed_on": DEFAULT_BOOKING_DATE,
        "source": "user_input",
    }
    fields.update(overrides)
    return RiskAssessment(**fields)  # type: ignore[arg-type]


# 1. 硬筛选：R2 看不到 R3 / R4 产品
def test_r2_sees_only_eligible_products(gateway: FactGateway) -> None:
    result = _query(gateway, risk_profile="R2", risk_source="user_input")
    assert result.record_ids == ("wealth_products:wp_cash", "wealth_products:wp_bond")
    assert "均衡配置 B" not in result.detail
    assert "成长精选 C" not in result.detail
    assert "合格候选 2 只" in result.detail
    assert "用户自助录入" in result.detail


@pytest.mark.parametrize(
    ("profile", "allowed"),
    [
        ("R1", {"wp_cash"}),
        ("R2", {"wp_cash", "wp_bond"}),
        ("R3", {"wp_cash", "wp_bond", "wp_mix"}),
        ("R4", {"wp_cash", "wp_bond", "wp_mix", "wp_equity"}),
    ],
)
def test_hard_filter_cannot_be_reintroduced_by_labels(
    gateway: FactGateway, profile: str, allowed: set[str]
) -> None:
    labels = "\n".join(candidate_labels(gateway, profile))
    detail = _query(gateway, risk_profile=profile).detail
    for record in gateway.wealth_products():
        if record.payload.product_id in allowed:
            continue
        assert record.payload.name not in labels
        assert record.record_id not in detail


def test_invalid_risk_level_is_refused(gateway: FactGateway) -> None:
    with pytest.raises(SuitabilityError):
        recommend_products(gateway.wealth_products(), "R9")


# 2. 测评缺失 / 过期：拒绝推荐，绝不默认等级
def test_missing_assessment_never_defaults_level() -> None:
    with pytest.raises(SuitabilityError) as excinfo:
        recommendation_profile(None, DEFAULT_BOOKING_DATE)
    assert "风险测评" in str(excinfo.value)
    with pytest.raises(SuitabilityError):
        recommendation_params(None, DEFAULT_BOOKING_DATE)


def test_expired_assessment_blocks_recommendation() -> None:
    expired = _assessment("R4", assessed_on=date(2024, 1, 1), valid_days=30)
    with pytest.raises(SuitabilityError) as excinfo:
        recommendation_profile(expired, DEFAULT_BOOKING_DATE)
    assert "过期" in str(excinfo.value)


# 3. 选品词不是红线词；收益承诺词仍是红线
def test_recommendation_intents_are_not_red_lines() -> None:
    assert set(FORBIDDEN_HINTS).isdisjoint(RECOMMENDATION_HINTS)
    for text in ("推荐理财产品", "哪款适合我", "比较几款产品", "对比一下理财产品"):
        assert is_recommendation_request(text)
    assert not is_recommendation_request("查看理财产品")
    assert is_recommendation_request("推荐收益最高的理财")


# 4. 计划节点：注入测评等级；无推荐词走全量目录；无测评拒绝
def test_plan_without_recommendation_hint_stays_full_catalog(gateway: FactGateway) -> None:
    plan = WealthHandler().build([_list_intent("看看理财产品")], gateway)
    assert plan.nodes[0].params == {}


def test_plan_without_assessment_refuses_recommendation(gateway: FactGateway) -> None:
    with pytest.raises(SuitabilityError):
        WealthHandler().build([_list_intent("推荐理财产品")], gateway)


def test_plan_injects_trusted_profile_and_source(gateway: FactGateway) -> None:
    plan = WealthHandler().build([_list_intent("哪款适合我", _assessment("R2"))], gateway)
    assert plan.nodes[0].params == {"risk_profile": "R2", "risk_source": "user_input"}


# 6. 候选不足：如实说明，不凑数
class _StubGateway:
    """只含 R4 产品的假事实源（验证"候选不足"分支）。"""

    def __init__(self, records: tuple[Record[WealthProduct], ...]) -> None:
        self._records = records

    def wealth_products(self) -> tuple[Record[WealthProduct], ...]:
        return self._records


def test_insufficient_candidates_are_disclosed() -> None:
    product = WealthProduct("p_r4", "高风险演示", "R4", 365, 700, Money(1000000))
    stub = _StubGateway((Record(record_id="p_r4", payload=product, content_hash="h1"),))
    result = _query(stub, risk_profile="R2")
    assert result.record_ids == ()
    assert result.provenance == ()
    assert "合格候选 0 只" in result.detail
    assert "不推荐任何不匹配产品" in result.detail
    assert "高风险演示" not in result.detail


# 7. 数据缺失：标未知 + 披露演示数据，不编造时效
def test_candidate_disclosure_marks_unknown_and_demo_source(gateway: FactGateway) -> None:
    product = gateway.wealth_product("wp_bond").payload
    text = describe_candidate(product, "R2")
    assert PRODUCT_DATA_SOURCE in text
    assert text.count("未知（产品数据未提供）") >= 5
    assert "更新时间 未知" in text
    assert all(word not in text for word in FORBIDDEN_HINTS)
    assert "收益" not in text
    assert "%" not in text
    assert "bp" not in text
    assert re.search(r"\d{4}-\d{2}-\d{2}", text) is None


# 8. 等级不匹配：拒绝时给「相近等级」的可申购替代，绝不夹带不匹配产品
def test_blocked_message_offers_nearest_eligible_alternatives(gateway: FactGateway) -> None:
    products = gateway.wealth_products()
    equity = gateway.wealth_product("wp_equity").payload
    message = blocked_suitability_message(_assessment("R2"), equity, products)
    assert message.startswith("风险等级不匹配：测评 R2 < 产品 R4")
    assert "可申购" in message
    assert "稳利债券 A" in message
    assert "均衡配置 B" not in message
    assert "成长精选 C" not in message

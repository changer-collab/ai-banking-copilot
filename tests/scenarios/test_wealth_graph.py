"""理财知识图谱（§8.1b）守卫：事实与种子一致、每只产品 4 条边、解释不改判、无收益暗示。"""

from __future__ import annotations

from ai_banking_copilot.gateway.facts import FactGateway
from ai_banking_copilot.scenarios.wealth import (
    FORBIDDEN_HINTS,
    PRODUCT_DATA_SOURCE,
    PRODUCT_NOT_ASSESSED,
    product_catalog,
)
from ai_banking_copilot.scenarios.wealth_graph import (
    FEE_UNKNOWN,
    explain_product,
    load_graph,
    product_facts,
)

_RELATIONS = {"risk_level", "fee", "min_amount", "audience"}


def test_graph_facts_match_gateway_seed(gateway: FactGateway) -> None:
    """图是种子数据的投影：任一字段漂移即红（不引入第二套事实）。"""
    graph = load_graph()
    seeded = {record.payload.product_id: record.payload for record in gateway.wealth_products()}
    assert {facts.product_id for facts in graph.products} == set(seeded)
    for facts in graph.products:
        product = seeded[facts.product_id]
        assert facts.name == product.name
        assert facts.risk_level == product.risk_level
        assert facts.term_days == product.term_days
        assert facts.min_amount_yuan == str(product.min_amount.yuan)
    assert graph.data_source == PRODUCT_DATA_SOURCE


def test_every_product_has_four_edges() -> None:
    """每只产品固定 4 条出边：风险等级 / 费率 / 起购 / 客群（且目标节点都在图里）。"""
    graph = load_graph()
    for facts in graph.products:
        source = f"product:{facts.product_id}"
        out = [edge for edge in graph.edges if edge.source == source]
        assert {edge.relation for edge in out} == _RELATIONS
        for edge in out:
            assert edge.target in graph.nodes


def test_fee_is_unknown_not_fabricated() -> None:
    assert FEE_UNKNOWN in load_graph().nodes["fee:catalog"].label
    raw = load_graph().nodes["fee:catalog"].label
    assert "未知" in raw


def test_explain_eligible_product() -> None:
    why = explain_product(
        {"product_id": "wp_bond", "eligible": True, "reason": ""}, "R3"
    )
    assert why["title"] == "为什么推荐"
    assert why["eligible"] is True
    reasons = why["reasons"]
    assert isinstance(reasons, tuple)
    assert any("通过适当性闸门" in text for text in reasons)
    assert any(FEE_UNKNOWN in text for text in reasons)
    path = why["path"]
    assert isinstance(path, tuple)
    assert path[0] == "产品「稳利债券 A」"
    assert len(path) == 5, "产品起点 + 4 跳（风险等级/费率/起购/客群）"


def test_explain_ineligible_keeps_server_reason() -> None:
    reason = "风险等级 R4 高于你的测评等级 R3，不能申购"
    why = explain_product(
        {"product_id": "wp_equity", "eligible": False, "reason": reason}, "R3"
    )
    assert why["title"] == "为什么不能买"
    assert why["eligible"] is False
    assert reason in why["reasons"]


def test_explain_without_assessment_is_honest() -> None:
    why = explain_product(
        {"product_id": "wp_cash", "eligible": False, "reason": PRODUCT_NOT_ASSESSED}, None
    )
    assert why["title"] == "为什么不能买"
    assert PRODUCT_NOT_ASSESSED in why["reasons"]


def test_explain_unknown_product_does_not_fabricate() -> None:
    why = explain_product({"product_id": "wp_unknown", "eligible": True, "reason": ""}, "R5")
    assert why["title"] == "为什么不能买"
    assert why["eligible"] is False
    assert why["path"] == ()
    assert any("没有这只产品" in text for text in why["reasons"])


def test_explanations_avoid_forbidden_hints(gateway: FactGateway) -> None:
    """H6：解释文案不得含收益暗示措辞，也不出现「收益」二字。"""
    catalog = product_catalog(gateway.wealth_products(), "R3")
    texts: list[str] = []
    for item in catalog:
        why = item["explanation"]
        assert isinstance(why, dict)
        texts.append(str(why["title"]))
        texts.extend(str(text) for text in why["reasons"])
        texts.extend(str(step) for step in why["path"])
        texts.append(str(why["suitability_rule"]))
        texts.append(str(why["data_source"]))
    joined = "\n".join(texts)
    for hint in FORBIDDEN_HINTS:
        assert hint not in joined, f"解释文案含收益暗示：{hint}"
    assert "收益" not in joined


def test_product_facts_is_none_for_unknown() -> None:
    assert product_facts("wp_unknown") is None

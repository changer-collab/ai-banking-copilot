"""理财知识图谱（轻量 JSON 图）：产品 → 风险等级 / 费率 / 起购 / 客群。

口径（分工文档 §8.1b）：
- **只解释、不改判**：能不能买仍由 `scenarios.wealth.product_catalog` 按服务端测评等级判定，
  本模块只读它的 `eligible` / `reason`，补上图谱路径，**不引入第二套判据**；
- **不引入图数据库**：节点与边在进程内由 `wealth_graph.json` 组装（`lru_cache`），无外部依赖；
- **不生成数字**：期限 / 起购 / 等级全部来自 JSON（与 `gateway/seed.py` 逐字段一致，
  `tests/scenarios/test_wealth_graph.py` 守卫漂移）；费率缺数据一律标未知，不做任何测算；
- **H6**：图里没有收益字段，解释文案也不含收益暗示措辞。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

GRAPH_PATH = Path(__file__).with_name("wealth_graph.json")

FEE_UNKNOWN = "未知（产品数据未提供费率，本图不做费率测算）"
"""费率口径：5 只演示产品都没有费率数据 ⇒ 一律标未知，不编造、不估算。"""

_FEE_NODE = "fee:catalog"

_RELATION_LABELS: Mapping[str, str] = {
    "risk_level": "风险等级",
    "fee": "费率",
    "min_amount": "起购",
    "audience": "客群",
}


@dataclass(frozen=True)
class ProductFacts:
    """单只产品在图中的事实（全部来自 JSON，不在此处生成任何数字）。"""

    product_id: str
    name: str
    risk_level: str
    term_days: int
    min_amount_yuan: str
    audience: str


@dataclass(frozen=True)
class GraphNode:
    node_id: str
    kind: str
    label: str


@dataclass(frozen=True)
class GraphEdge:
    source: str
    relation: str
    target: str


@dataclass(frozen=True)
class WealthGraph:
    data_source: str
    suitability_rule: str
    risk_levels: Mapping[str, str]
    products: tuple[ProductFacts, ...]
    nodes: Mapping[str, GraphNode]
    edges: tuple[GraphEdge, ...]


def _text(raw: Mapping[str, object], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"wealth_graph.json 缺少字符串字段：{key}")
    return value


def _number(raw: Mapping[str, object], key: str) -> int:
    value = raw.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"wealth_graph.json 字段必须是整数：{key}")
    return value


@lru_cache(maxsize=1)
def load_graph() -> WealthGraph:
    """读取并组装图（进程内缓存）：顶层字段缺失或类型不符 ⇒ `ValueError`，不带病降级。"""
    raw: object = json.loads(GRAPH_PATH.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("wealth_graph.json 顶层必须是对象")
    data_source = _text(raw, "data_source")
    suitability_rule = _text(raw, "suitability_rule")
    fee = _text(raw, "fee")

    levels_raw = raw.get("risk_levels")
    risk_levels: dict[str, str] = {}
    if isinstance(levels_raw, dict):
        for key, value in levels_raw.items():
            if isinstance(key, str) and isinstance(value, str):
                risk_levels[key] = value
    if not risk_levels:
        raise ValueError("wealth_graph.json 缺少 risk_levels")

    entries = raw.get("products")
    if not isinstance(entries, list) or not entries:
        raise ValueError("wealth_graph.json 缺少 products 列表")

    nodes: dict[str, GraphNode] = {_FEE_NODE: GraphNode(_FEE_NODE, "fee", fee)}
    edges: list[GraphEdge] = []
    products: list[ProductFacts] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("wealth_graph.json products 每项必须是对象")
        facts = ProductFacts(
            product_id=_text(entry, "id"),
            name=_text(entry, "name"),
            risk_level=_text(entry, "risk_level"),
            term_days=_number(entry, "term_days"),
            min_amount_yuan=_text(entry, "min_amount_yuan"),
            audience=_text(entry, "audience"),
        )
        products.append(facts)
        _add_product_nodes(nodes, edges, facts, risk_levels)
    return WealthGraph(
        data_source=data_source,
        suitability_rule=suitability_rule,
        risk_levels=risk_levels,
        products=tuple(products),
        nodes=nodes,
        edges=tuple(edges),
    )


def _add_product_nodes(
    nodes: dict[str, GraphNode],
    edges: list[GraphEdge],
    facts: ProductFacts,
    risk_levels: Mapping[str, str],
) -> None:
    """每只产品固定 4 条出边：风险等级 / 费率 / 起购 / 客群。"""
    source = f"product:{facts.product_id}"
    nodes[source] = GraphNode(source, "product", facts.name)
    risk = f"risk:{facts.risk_level}"
    risk_label = f"{facts.risk_level} {risk_levels.get(facts.risk_level, '未知')}"
    nodes.setdefault(risk, GraphNode(risk, "risk_level", risk_label))
    amount = f"min_amount:{facts.product_id}"
    nodes[amount] = GraphNode(amount, "min_amount", f"{facts.min_amount_yuan} 元")
    audience = f"audience:{facts.product_id}"
    nodes[audience] = GraphNode(audience, "audience", facts.audience)
    for relation, target in (
        ("risk_level", risk),
        ("fee", _FEE_NODE),
        ("min_amount", amount),
        ("audience", audience),
    ):
        edges.append(GraphEdge(source=source, relation=relation, target=target))


def product_facts(product_id: str) -> ProductFacts | None:
    """按产品 id 取图内事实；图里没有 ⇒ `None`（调用方如实标未知，不编造）。"""
    for facts in load_graph().products:
        if facts.product_id == product_id:
            return facts
    return None


def fee_label() -> str:
    """产品行一行内展示的费率：缺数据时只写「未知」，完整口径留在推荐理由里。"""
    node = load_graph().nodes.get(_FEE_NODE)
    label = node.label if node is not None else FEE_UNKNOWN
    return "未知" if label.startswith("未知") else label


def _yield_text(raw: object) -> str | None:
    """年化基点 → `3.20`（整数运算，不经过浮点）；缺失 / 非法 ⇒ `None`（不编造）。"""
    if isinstance(raw, bool) or not isinstance(raw, int):
        return None
    return f"{raw // 100}.{raw % 100:02d}"


def _metric_reasons(item: Mapping[str, object], eligible: bool) -> tuple[str, ...]:
    """把产品目录里的真实指标摊进推荐理由（数值全部来自 `product_catalog` 的 payload）。

    措辞刻意避开 `FORBIDDEN_HINTS` 的收益暗示词：用「参考报价」陈述产品属性，
    并明确标注为**模拟数据、不代表未来表现**，不构成任何承诺。
    """
    metrics: list[str] = []
    category = item.get("category")
    if isinstance(category, str) and category:
        metrics.append(f"类别 {category}")
    quote = _yield_text(item.get("yield_bp"))
    if quote is not None:
        metrics.append(f"参考报价 {quote}%/年")
    term = item.get("term_days")
    if isinstance(term, int) and not isinstance(term, bool):
        metrics.append(f"期限 {term} 天")
    minimum = item.get("min_amount_yuan")
    if isinstance(minimum, str) and minimum:
        metrics.append(f"起购 {minimum} 元")
    fee = item.get("fee")
    if isinstance(fee, str) and fee:
        metrics.append(f"费率 {fee}")
    if not metrics:
        return ()
    lines = [
        "参考指标：" + "；".join(metrics)
        + "（参考报价为模拟数据，不代表未来表现，不构成承诺）。"
    ]
    if eligible:
        lines.append(
            "本次按 风险等级、期限、起购金额、参考报价 四项指标匹配"
            "（演示目录共 5 只，非真实海量产品池）。"
        )
    return tuple(lines)


def explain_product(
    item: Mapping[str, object], risk_profile: str | None
) -> dict[str, object]:
    """产品卡的可展开解释：为什么推荐 / 为什么不能买 + 知识图谱路径。

    `item` 是 `product_catalog` 的目录项：**可申购判定与原因直接沿用服务端结论**
    （`eligible` / `reason`），本函数只补充图谱里的事实与路径，不改判。
    """
    graph = load_graph()
    raw_id = item.get("product_id")
    facts = product_facts(raw_id) if isinstance(raw_id, str) else None
    if facts is None:
        return {
            "title": "为什么不能买",
            "eligible": False,
            "reasons": ("知识图谱中没有这只产品（未知，不编造）",),
            "path": (),
            "suitability_rule": graph.suitability_rule,
            "data_source": graph.data_source,
        }
    eligible = item.get("eligible") is True
    reasons: list[str] = []
    if eligible:
        meaning = graph.risk_levels.get(facts.risk_level, "未知")
        reasons.append(
            f"风险匹配：你的测评等级 {risk_profile or '未知'}，产品风险 {facts.risk_level}"
            f"（{meaning}），未超出你可承受的范围（由服务端按测评结果判定）。"
        )
    else:
        reason = item.get("reason")
        reasons.append(
            reason
            if isinstance(reason, str) and reason
            else "当前不可申购（服务端未给出原因，不编造）"
        )
    reasons.extend(_metric_reasons(item, eligible))
    reasons.append(f"适合人群：{facts.audience}。")
    reasons.append(
        f"期限 {facts.term_days} 天、起购 {facts.min_amount_yuan} 元，"
        "购买前请确认这笔资金在期限内不需要动用。"
    )
    fee_node = graph.nodes.get(_FEE_NODE)
    fee_text = fee_node.label if fee_node is not None else FEE_UNKNOWN
    reasons.append(f"费率：{fee_text}；本页不做测算，也不作业绩承诺。")
    return {
        "title": "推荐理由" if eligible else "为什么不能买",
        "eligible": eligible,
        "reasons": tuple(reasons),
        "path": _path(graph, facts),
        "suitability_rule": graph.suitability_rule,
        "data_source": graph.data_source,
    }


def _path(graph: WealthGraph, facts: ProductFacts) -> tuple[str, ...]:
    """图谱路径：产品 →（风险等级 / 费率 / 起购 / 客群），按边遍历，不做二次加工。"""
    source = f"product:{facts.product_id}"
    hops = [f"产品「{facts.name}」"]
    for edge in graph.edges:
        if edge.source != source:
            continue
        node = graph.nodes.get(edge.target)
        if node is None:
            continue
        hops.append(f"{_RELATION_LABELS.get(edge.relation, edge.relation)}：{node.label}")
    return tuple(hops)

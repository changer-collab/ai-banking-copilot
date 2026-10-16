"""计划契约：意图锚点（intent_ref）+ 任务 DAG + 冻结哈希（cfhash）。

用途：
1. **意图一致性**——每个节点必须携带锚点；无锚点节点不得执行（防注入新增步骤）。
2. **执行门判据**——`cfhash` 冻结"用户确认的那份 DAG"；确认后被增删改即哈希不匹配
   → 拒绝执行（防 TOCTOU）。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import StrEnum


class AnchorSource(StrEnum):
    """意图锚点来源。"""

    EXPLICIT = "explicit"
    """用户本轮明说。"""

    USER_APPROVED = "user_approved"
    """用户在计划卡上批准。"""

    DERIVED = "derived"
    """机械推导（零自由度）；节点级禁用，仅允许子图且深度 ≤ 1。"""


@dataclass(frozen=True)
class IntentRef:
    """意图锚点引用。"""

    source: AnchorSource
    anchor_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.source, AnchorSource):
            raise TypeError(
                f"source must be an AnchorSource member, got {type(self.source).__name__}"
            )
        if not self.anchor_id:
            raise ValueError("anchor_id required")


@dataclass(frozen=True)
class PlanNode:
    """计划节点：一个白名单工具调用。"""

    node_id: str
    action: str
    params: dict[str, object] = field(default_factory=dict)
    intent: IntentRef | None = None
    depends_on: tuple[str, ...] = ()

    def canonical(self) -> dict[str, object]:
        """规范化节点身份、调用、锚点与依赖；字典键序及依赖声明顺序无关。"""
        return {
            "node_id": self.node_id,
            "action": self.action,
            "intent": (
                None
                if self.intent is None
                else {"source": self.intent.source.value, "anchor_id": self.intent.anchor_id}
            ),
            "params": {key: self.params[key] for key in sorted(self.params)},
            "depends_on": sorted(self.depends_on),
        }


@dataclass(frozen=True)
class Plan:
    """任务 DAG。"""

    plan_id: str
    nodes: tuple[PlanNode, ...]

    def node_map(self) -> dict[str, PlanNode]:
        return {node.node_id: node for node in self.nodes}


class PlanValidationError(ValueError):
    """计划不合法。"""


def validate_plan(plan: Plan) -> None:
    """校验：锚点齐备、依赖存在且不重复、无环、无重复 id。"""
    nodes = plan.node_map()
    if len(nodes) != len(plan.nodes):
        raise PlanValidationError("duplicate node_id in plan")
    for node in plan.nodes:
        if node.intent is None:
            raise PlanValidationError(f"node {node.node_id} has no intent anchor")
        if node.intent.source is AnchorSource.DERIVED:
            raise PlanValidationError(
                f"node {node.node_id}: derived anchor is not allowed at node level"
            )
        if len(set(node.depends_on)) != len(node.depends_on):
            raise PlanValidationError(f"node {node.node_id} has duplicate dependency")
        for dep in node.depends_on:
            if dep not in nodes:
                raise PlanValidationError(f"node {node.node_id} depends on unknown node {dep}")
    _assert_acyclic(nodes)


def _assert_acyclic(nodes: dict[str, PlanNode]) -> None:
    """Kahn 拓扑排序；剩余节点即存在环。"""
    indegree = dict.fromkeys(nodes, 0)
    for node in nodes.values():
        for _ in node.depends_on:
            indegree[node.node_id] += 1
    ready = [node_id for node_id, degree in indegree.items() if degree == 0]
    visited = 0
    while ready:
        current = ready.pop()
        visited += 1
        for node in nodes.values():
            if current in node.depends_on:
                indegree[node.node_id] -= 1
                if indegree[node.node_id] == 0:
                    ready.append(node.node_id)
    if visited != len(nodes):
        raise PlanValidationError("plan contains a cycle")


def topological_order(plan: Plan) -> tuple[str, ...]:
    """按 `depends_on` 求**确定性**拓扑序（执行序的唯一判据）。

    存在理由：执行器原先直接按 `plan.nodes` 的列表顺序执行，**从不读 `depends_on`**——
    只是各场景构造器恰好按依赖序追加节点，属隐式契约。本函数让「先冻结、后依赖」成为
    显式判据：任何合法 DAG 都能得到正确执行序，而不依赖构造顺序。

    确定性：就绪集内取**计划中靠前**的节点，故同一份计划每次得到同一执行序（可复现、可审计）。
    环 / 未知依赖由 `validate_plan` 先行拒绝；本函数对已校验的计划恒返回全部节点。
    """
    order = [node.node_id for node in plan.nodes]
    position = {node_id: index for index, node_id in enumerate(order)}
    nodes = plan.node_map()
    indegree = {node_id: len(nodes[node_id].depends_on) for node_id in nodes}
    ready = [node_id for node_id in order if indegree[node_id] == 0]
    resolved: list[str] = []
    while ready:
        current = min(ready, key=lambda node_id: position[node_id])
        ready.remove(current)
        resolved.append(current)
        for node_id in order:
            if current in nodes[node_id].depends_on:
                indegree[node_id] -= 1
                if indegree[node_id] == 0:
                    ready.append(node_id)
    if len(resolved) != len(order):
        raise PlanValidationError("plan contains a cycle")
    return tuple(resolved)


def cfhash(nodes: tuple[PlanNode, ...]) -> str:
    """执行门判据：校验并冻结完整 DAG 及实际执行顺序的版本化 SHA-256。"""
    plan = Plan(plan_id="cfhash", nodes=nodes)
    validate_plan(plan)
    payload = {
        "version": 2,
        "execution_order": list(topological_order(plan)),
        "nodes": [node.canonical() for node in sorted(nodes, key=lambda item: item.node_id)],
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()

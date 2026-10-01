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
from enum import Enum


class AnchorSource(str, Enum):
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
        """规范化表示——`cfhash` 的输入载体；params 按键排序保证序无关。"""
        return {
            "action": self.action,
            "intent": (
                None
                if self.intent is None
                else {"source": self.intent.source.value, "anchor_id": self.intent.anchor_id}
            ),
            "params": {key: self.params[key] for key in sorted(self.params)},
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
    """校验：锚点齐备、依赖存在、无环、无重复 id。"""
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


def cfhash(nodes: tuple[PlanNode, ...]) -> str:
    """执行门判据：节点集合（含锚点与控制槽参数）的确定性 SHA-256。"""
    payload = [node.canonical() for node in sorted(nodes, key=lambda n: n.node_id)]
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()

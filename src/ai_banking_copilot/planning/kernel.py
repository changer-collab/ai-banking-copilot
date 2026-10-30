"""规划内核：多意图拆解 → 槽位补全 → DAG 规划 → 自检（H2 / H7）。

本模块**只产计划**：
- **不执行动作**（H2）：执行只能由 `policy.pep` 在 `policy.pdp` 裁决后进行；
- **不产出授权级别**：级别由 `policy.pdp` 决定；多候选收款人只澄清、不定级；
- **不产事实**（H1）：数值与实体只来自 `slots`（即 01 的确定性解析器）与受信任 `context`。

三条纪律：
1. **统一出口**：所有节点都由本模块组装，最后 `validate_plan` 自检 + `cfhash` 冻结；
2. **锚点**：节点锚点只可能是 `explicit`（用户本轮明说）；`derived` 锚点只用于**机械推导的
   记录**（如"依赖关系由冲突检测机械推导"），**绝不作为节点锚点**（`validate_plan` 节点级禁用）；
3. **解析文本可回指**：槽位解析所用的文本必须是**用户原文的子串**——LLM 伪造的片段直接作废（H3）。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import date
from typing import Protocol

from ai_banking_copilot.contracts.plan import (
    AnchorSource,
    IntentRef,
    Plan,
    PlanNode,
    cfhash,
    validate_plan,
)
from ai_banking_copilot.gateway.models import Payee
from ai_banking_copilot.planning.anchors import AnchorLedger
from ai_banking_copilot.planning.llm import Intent, IntentModel
from ai_banking_copilot.planning.slots import SLOT_PARAMS, SlotInputs, fill_control_slots

CONFIDENCE_THRESHOLD = 0.5
"""低于该置信度的意图直接丢弃（PRD §5.7：低置信度不猜）。"""

CONTEXT_PAYEES = "payees"
CONTEXT_TODAY = "today"
CONTEXT_SELECTED = "selected"
_QUERY_SCOPE_BOUNDARY = re.compile(r"[，,。；;、]|然后|接着|再|并且|同时")

_RESOURCE_PARAM: Mapping[str, str] = {
    "transfer.create": "account_id",
    "transfer.schedule": "account_id",
    "account.balance": "account_id",
    "account.transactions": "account_id",
    "wealth.subscribe": "account_id",
    "wealth.redeem": "account_id",
    "funds.freeze": "account_id",
    "subscription.cancel": "subscription_id",
    "subscription.sign": "merchant",
    "card.lock": "card_id",
    "card.unlock": "card_id",
    "card.report_lost": "card_id",
    "card.set_limit": "card_id",
    "funds.release": "hold_id",
}
"""动作 → 资源键参数：**同资源串行、异资源并行**（冲突与资源检测）；只列带资源键的动作。"""


class ActionCatalog(Protocol):
    """内核只依赖"已知动作清单"这一最小面；`tools.registry.ToolRegistry` 结构上即满足。"""

    def actions(self) -> Iterable[str]:
        """返回全部已注册动作名。"""
        ...


@dataclass(frozen=True)
class Question:
    """一次澄清请求：缺哪个控制槽、问什么、可选候选。"""

    slot: str
    prompt: str
    candidates: tuple[str, ...] = ()


@dataclass(frozen=True)
class PlanningResult:
    """规划产出。`blocked=True` 表示**没有任何可执行节点**（未知意图或待澄清）。"""

    plan: Plan
    questions: tuple[Question, ...]
    blocked: bool
    intents: tuple[Intent, ...]
    rationale: str


class Kernel:
    """规划内核：`Kernel(llm, registry)`。"""

    def __init__(self, llm: IntentModel, registry: ActionCatalog) -> None:
        self._llm = llm
        self._registry = registry

    def plan(self, user_id: str, utterance: str, context: Mapping[str, object]) -> PlanningResult:
        """把一句话规划成带锚点的 DAG；补不齐的控制槽转追问。"""
        proposal = self._llm.propose(utterance, context)
        known = set(self._registry.actions())
        ledger = AnchorLedger()
        today = _context_today(context)
        payees = _context_payees(context)
        selected = _context_selected(context)
        scoped_intents = tuple(
            intent
            for intent in proposal.intents
            if intent.confidence >= CONFIDENCE_THRESHOLD and intent.action in known
        )

        nodes: list[PlanNode] = []
        questions: list[Question] = []
        notes: list[str] = []
        accepted: list[Intent] = []

        for index, intent in enumerate(proposal.intents, start=1):
            if intent.confidence < CONFIDENCE_THRESHOLD:
                notes.append(f"丢弃低置信度意图 {intent.action}（{intent.confidence:.2f}）")
                continue
            if intent.action not in known:
                notes.append(f"丢弃未注册动作 {intent.action}")
                continue
            accepted.append(intent)
            scope_text = None
            if intent.action in {"account.balance", "account.transactions"}:
                scope_text = _query_scope_text(intent, utterance, intents=scoped_intents)
                if scope_text is None:
                    questions.append(Question("account", "请单独说明本项查询的对象及原文片段。"))
                    continue
            fill = fill_control_slots(
                intent.action,
                SlotInputs(
                    text=_resolution_text(intent, utterance),
                    today=today,
                    payees=payees,
                    selected=selected,
                    scope_text=scope_text,
                ),
            )
            if fill.negated:
                notes.append(f"{intent.action} 命中否定表达，不生成节点")
                continue
            questions.extend(
                Question(slot=item.slot, prompt=item.prompt, candidates=item.candidates)
                for item in fill.questions
            )
            if not fill.complete:
                continue
            anchor_id = ledger.grant_explicit(f"用户本轮明说：{intent.evidence or intent.action}")
            nodes.append(
                PlanNode(
                    node_id=f"n{index}",
                    action=intent.action,
                    params=_params_from_slots(fill.slots),
                    intent=IntentRef(source=AnchorSource.EXPLICIT, anchor_id=anchor_id),
                )
            )

        wired = _wire_dependencies(tuple(nodes), ledger, notes)
        digest = cfhash(wired)
        plan = Plan(plan_id=f"plan_{digest[:12]}", nodes=wired)
        validate_plan(plan)
        blocked = not wired
        if blocked and not questions:
            notes.append("未知意图：安全拒绝（L4），不猜测执行")
        rationale = "；".join([f"user={user_id}", *notes, f"cfhash={digest}"])
        return PlanningResult(
            plan=plan,
            questions=tuple(questions),
            blocked=blocked,
            intents=tuple(accepted),
            rationale=rationale,
        )


def _params_from_slots(slots: Mapping[str, object]) -> dict[str, object]:
    return {SLOT_PARAMS[slot]: value for slot, value in slots.items()}


def _wire_dependencies(
    nodes: tuple[PlanNode, ...], ledger: AnchorLedger, notes: list[str]
) -> tuple[PlanNode, ...]:
    """同资源串行：把机械推导出的依赖记入锚点账本，作为可审计的推导依据。"""
    last_by_resource: dict[str, str] = {}
    wired: list[PlanNode] = []
    for node in nodes:
        resource = _resource_key(node)
        dependency = last_by_resource.get(resource) if resource is not None else None
        if dependency is None:
            wired.append(node)
        else:
            derived_id = ledger.derive(_anchor_id(node), f"{node.node_id} 机械依赖 {dependency}")
            notes.append(f"{node.node_id} 依赖 {dependency}（机械推导锚 {derived_id}）")
            wired.append(replace(node, depends_on=(dependency,)))
        if resource is not None:
            last_by_resource[resource] = node.node_id
    return tuple(wired)


def _resource_key(node: PlanNode) -> str | None:
    param = _RESOURCE_PARAM.get(node.action)
    if param is None:
        return None
    value = node.params.get(param)
    if isinstance(value, str) and value:
        return f"{param}:{value}"
    return None


def resource_key(node: PlanNode) -> str | None:
    """节点资源键（`参数名:值`）：**供展示层标注「同资源串行」**。

    判定仍只走 `_wire_dependencies`（唯一真源）；本函数只是把同一份映射公开出去，
    避免展示层复制一份资源键表。
    """
    return _resource_key(node)


def _anchor_id(node: PlanNode) -> str:
    if node.intent is None:
        raise ValueError(f"节点 {node.node_id} 缺少意图锚点")
    return node.intent.anchor_id


def _resolution_text(intent: Intent, utterance: str) -> str:
    """解析文本必须**可回指到用户原文**：LLM 伪造的片段直接丢弃（H3）。"""
    span = intent.slots.get("span")
    if isinstance(span, str) and span and span in utterance:
        return span
    return utterance


def _query_scope_text(
    intent: Intent, utterance: str, *, intents: tuple[Intent, ...],
) -> str | None:
    """相邻意图片段定界；查询后的未分配限定保留，其他动作和备注不参与。"""
    text = utterance.partition("备注")[0]
    if len(intents) <= 1:
        return text
    span = intent.slots.get("span")
    if not isinstance(span, str) or not span or text.count(span) != 1:
        return None
    start = text.index(span)
    end = start + len(span)
    left, right = 0, len(text)
    for other in intents:
        if other is intent:
            continue
        other_span = other.slots.get("span")
        if not isinstance(other_span, str) or not other_span or text.count(other_span) != 1:
            return None
        other_start = text.index(other_span)
        other_end = other_start + len(other_span)
        if other_end <= start:
            left = max(left, other_end)
        elif other_start >= end:
            right = min(right, other_start)
        else:
            return None
    # 左侧跳过连接词，右侧回到下一动作的子句开头，避免其对象修饰语串入。
    if left:
        boundary = _QUERY_SCOPE_BOUNDARY.search(text, left, start)
        if boundary is not None:
            left = boundary.end()
    while boundary := _QUERY_SCOPE_BOUNDARY.match(text, left):
        left = boundary.end()
    if right < len(text):
        boundaries = list(_QUERY_SCOPE_BOUNDARY.finditer(text, end, right))
        if boundaries:
            right = boundaries[-1].start()
    return text[left:right]


def _context_today(context: Mapping[str, object]) -> date:
    value = context.get(CONTEXT_TODAY)
    if isinstance(value, date):
        return value
    raise ValueError("context 缺少受信任的 today: date")


def _context_payees(context: Mapping[str, object]) -> tuple[Payee, ...]:
    value = context.get(CONTEXT_PAYEES)
    if value is None:
        return ()
    if isinstance(value, list | tuple):
        return tuple(item for item in value if isinstance(item, Payee))
    raise ValueError("context.payees 必须是 Payee 序列")


def _context_selected(context: Mapping[str, object]) -> Mapping[str, object]:
    value = context.get(CONTEXT_SELECTED)
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    raise ValueError("context.selected 必须是映射")

"""场景层基座：统一产出（ScenarioOutcome）+ 处理器协议（ScenarioHandler）+ 取值把关。

分层纪律（PRD §4.1 / §5.1 / §9.2）：
- 场景层**只组装计划**：把意图映射成白名单动作的 DAG；不产事实（H1）、不裁决级别（级别归 PDP）。
- 控制槽取值必须可信（H3）：显式标记 `unverified` 的取值一律拒绝并转澄清；备注等文本只入数据槽。
- 每个节点必须携带意图锚点（H2）：锚点来源 ∈ {explicit, user_approved}（交互层授予），
  `derived` 只用于机械推导记录，**不作节点锚点**。

计划级净效应（`policy.pdp.evaluate_plan` 的 `net` 入参）直接复用 04 的 `planning.llm.ACTION_KINDS`
（README「规范动作表」的唯一可执行镜像，20 项）：本层**不再私存动作词表**（README 裁决 #15 的
临时桥 `NORMATIVE_ACTION_KINDS` 已删除，改从 04 的词表取）。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Protocol, runtime_checkable

from ai_banking_copilot.contracts.authorization import Decision
from ai_banking_copilot.contracts.plan import AnchorSource, Plan, PlanNode
from ai_banking_copilot.contracts.taint import Slot, SlotKind, Trust, Value
from ai_banking_copilot.gateway.facts import FactGateway
from ai_banking_copilot.gateway.models import Money
from ai_banking_copilot.planning.anchors import AnchorLedger
from ai_banking_copilot.planning.llm import ACTION_KINDS, ActionKind, Intent
from ai_banking_copilot.planning.net_effect import NEUTRAL_NET_ACTIONS, NetEffect
from ai_banking_copilot.policy.pep import ExecResult


class UnknownScenarioAction(ValueError):
    """动作不在规范动作表内，拒绝计算场景净效应（fail-safe）。"""


class ClarificationRequired(ValueError):
    """控制槽不完整 / 多候选 / 取值不可信：**不产出计划、不产出级别**，转澄清。"""

    def __init__(self, slot: str, prompt: str, candidates: tuple[str, ...] = ()) -> None:
        super().__init__(prompt)
        self.slot = slot
        self.prompt = prompt
        self.candidates = candidates


class ScenarioRefused(ValueError):
    """场景级硬拒绝（适当性不达标 / 对象不存在 / 不支持的组合）：安全拒绝，不产出计划。"""


class ScenarioStatus(StrEnum):
    """场景运行状态。`EXECUTED` / `PARTIAL` 预留给执行层（06）填写。"""

    PLANNED = "planned"
    NEEDS_CLARIFICATION = "needs_clarification"
    BLOCKED = "blocked"
    EXECUTED = "executed"
    PARTIAL = "partial"
    FAILED = "failed"


@dataclass(frozen=True)
class ScenarioOutcome:
    """场景一次运行的产出形态（对齐 PRD §9.1 可机读 schema 的关键字段）。"""

    scenario: str
    status: ScenarioStatus
    plan: Plan | None
    decision: Decision | None
    exec_results: tuple[ExecResult, ...]
    message: str
    record_ids: tuple[str, ...]


@runtime_checkable
class ScenarioHandler(Protocol):
    """场景处理器协议：识别意图 → 组装计划（不裁决、不执行）。"""

    @property
    def name(self) -> str:
        """场景名（稳定标识，用于 `ScenarioOutcome.scenario`）。"""
        ...

    def matches(self, intents: Sequence[Intent]) -> bool:
        """该处理器是否承接这组意图。"""
        ...

    def build(self, intents: Sequence[Intent], gateway: FactGateway) -> Plan:
        """把意图组装成带锚点的 DAG（事实取自网关，控制槽取值来自受信任入参）。"""
        ...


def require_trusted(value: object, name: str) -> object:
    """解出控制槽取值；显式标记为 `unverified` 的一律拒绝并转澄清（H3 输入零信任）。"""
    if isinstance(value, Slot):
        if value.kind is not SlotKind.CONTROL:
            raise ClarificationRequired(name, f"{name} 的槽位类别不合法（需控制槽）")
        return require_trusted(value.value, name)
    if isinstance(value, Value):
        if value.trust is not Trust.TRUSTED:
            raise ClarificationRequired(name, f"控制槽 {name} 拒绝不可信来源的取值")
        return value.raw
    return value


def accept_data(value: object) -> object:
    """解出数据槽取值：**任意来源**（含 unverified 备注）都只当数据，绝不解释为指令。"""
    if isinstance(value, Slot):
        return value.value.raw
    if isinstance(value, Value):
        return value.raw
    return value


def trusted_slot(intent: Intent, keys: Sequence[str], name: str) -> object:
    """按候选键名取一个已感知来源的控制槽取值；缺失即抛出澄清（不猜）。"""
    for key in keys:
        if key in intent.slots:
            return require_trusted(intent.slots[key], name)
    raise ClarificationRequired(name, f"缺少控制槽 {name}")


def as_str(value: object, name: str) -> str:
    if isinstance(value, str) and value:
        return value
    raise ClarificationRequired(name, f"{name} 必须是非空文本")


def as_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ClarificationRequired(name, f"{name} 必须是整数")
    return value


def as_money(value: object, name: str) -> Money:
    if isinstance(value, Money):
        return value
    if isinstance(value, bool) or not isinstance(value, int):
        raise ClarificationRequired(name, f"{name} 必须是金额或整数（分）")
    return Money(value)


def as_date(value: object, name: str) -> date:
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError as error:
            raise ClarificationRequired(name, f"{name} 不是合法日期") from error
    raise ClarificationRequired(name, f"{name} 必须是日期")


def intent_text(intent: Intent) -> str:
    """取回指文本：优先 `span`（用户原文子串），否则退回 `evidence`。"""
    span = intent.slots.get("span")
    if isinstance(span, str) and span:
        return span
    return intent.evidence


def grant_anchor(ledger: AnchorLedger, source: AnchorSource, detail: str) -> str:
    """登记节点锚点：只允许 `explicit` / `user_approved`（`derived` 不作节点锚点）。"""
    if source is AnchorSource.EXPLICIT:
        return ledger.grant_explicit(detail)
    if source is AnchorSource.USER_APPROVED:
        return ledger.grant_approved(detail)
    raise ScenarioRefused("节点锚点只能是 explicit / user_approved（derived 不作节点锚点）")


def plan_net_effect(plan: Plan, gateway: FactGateway) -> NetEffect:
    """按规范动作表汇总全计划带符号资金流（`policy.pdp.evaluate_plan` 的 `net` 入参）。"""
    total = 0
    irreversibles: list[str] = []
    node_ids: list[str] = []
    for node in plan.nodes:
        node_ids.append(node.node_id)
        kind = ACTION_KINDS.get(node.action)
        if kind is None:
            raise UnknownScenarioAction(f"动作 {node.action} 不在规范动作表内，拒绝计算净效应")
        if kind is ActionKind.IRREVERSIBLE:
            irreversibles.append(node.node_id)
            continue
        if kind in (ActionKind.QUERY, ActionKind.STATE):
            continue
        if node.action in NEUTRAL_NET_ACTIONS:
            continue
        if kind is ActionKind.CANCEL_OUTFLOW:
            total -= _subscription_cents(node, gateway)
            continue
        cents = _amount_cents(node)
        total += cents if kind is ActionKind.OUTFLOW else -cents
    return NetEffect(
        amount=Money(total),
        irreversibles=tuple(irreversibles),
        node_ids=tuple(node_ids),
    )


def plan_is_pure_query(plan: Plan) -> bool:
    """计划是否**全部由纯查询节点组成**（绿色纯度判据，README 裁决 #7）。"""
    if not plan.nodes:
        return False
    return all(ACTION_KINDS.get(node.action) is ActionKind.QUERY for node in plan.nodes)


def _amount_cents(node: PlanNode) -> int:
    raw = node.params.get("amount_cents")
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise UnknownScenarioAction(f"节点 {node.node_id} 缺少整数 amount_cents")
    return raw


def _subscription_cents(node: PlanNode, gateway: FactGateway) -> int:
    raw = node.params.get("subscription_id")
    if not isinstance(raw, str) or not raw:
        raise UnknownScenarioAction(f"节点 {node.node_id}（订阅取消）缺少 subscription_id")
    return gateway.subscription(raw).payload.amount.cents

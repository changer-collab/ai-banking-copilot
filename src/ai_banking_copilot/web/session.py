"""会话层：提交 → 计划卡 → 确认（冻结绑定）→ 执行 → 中断/回退/接管。

分权（H2 / H10）：
- 计划与场景来自 04（`planning`）/ 05（`scenarios`）；级别来自 03（`policy.pdp`）；
- 执行只经 03 的 `policy.pep.Pep`（无裁决不执行）；
- 本层只做：意图装配（把控制槽填成**可信值**）、**组合**节点级与计划级裁决、
  冻结绑定、以及中断 / 回退 / 接管的会话编排；本层不违反任何上游硬约束。

安全纪律：
- 未知意图（无场景匹配）→ 安全拒绝并说明原因（PRD §5.7），不猜测执行；
- 注入检测命中 → 上下文标记 `injection_detected` → PDP 单向升到 `MANUAL` → 转人工接管；
- 熔断打开时**非纯查询**一律转人工接管（纯查询不受影响，PRD §5.8）。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from typing import TYPE_CHECKING

from ai_banking_copilot.contracts.authorization import Decision, Level, strictest
from ai_banking_copilot.contracts.plan import Plan, PlanNode
from ai_banking_copilot.contracts.taint import SlotKind, Trust, Value
from ai_banking_copilot.gateway.errors import GatewayError
from ai_banking_copilot.planning.kernel import CONFIDENCE_THRESHOLD
from ai_banking_copilot.planning.llm import ACTION_KINDS, ActionKind, Intent
from ai_banking_copilot.planning.slots import SLOT_PARAMS, SlotInputs, fill_control_slots
from ai_banking_copilot.policy.audit import KIND_DECISION, KIND_REVERSAL, AuditEvent
from ai_banking_copilot.policy.gate import assert_frozen
from ai_banking_copilot.policy.pdp import DAILY_LIMIT_YUAN, FLAG_INJECTION_DETECTED, evaluate
from ai_banking_copilot.policy.pep import ExecResult, NotAuthorizedError, manifest_hash
from ai_banking_copilot.policy.saga import ReversalOutcome, ReversalWindow, Saga
from ai_banking_copilot.scenarios.authorization import node_policy_request
from ai_banking_copilot.scenarios.base import (
    ScenarioOutcome,
    ScenarioStatus,
    plan_is_pure_query,
)
from ai_banking_copilot.scenarios.registry import (
    UnknownScenarioError,
    run_scenario,
    select_handler,
)
from ai_banking_copilot.scenarios.wealth import FORBIDDEN_HINTS, to_risk_assessment
from ai_banking_copilot.tools.registry import ToolCall
from ai_banking_copilot.web.plan_card import (
    PROMPT_STRONG_VERIFY,
    PlanCard,
    render_plan_card,
)

if TYPE_CHECKING:
    from ai_banking_copilot.web.app import AppDeps

_CENTS_PER_YUAN = 100
"""元 → 分换算（与 `policy.pdp` 同口径）。"""

INJECTION_MARKERS: tuple[str, ...] = (
    "忽略以上",
    "忽略之前",
    "忽略上述",
    "无视以上",
    "忽略系统",
    "ignore previous",
    "ignore above",
    "disregard",
    "新指令",
)
"""提示注入的确定性标记（命中即升 `MANUAL` 转人工，不做模糊判断）。"""

FABRICATION_MARKERS: tuple[str, ...] = (
    "编一个",
    "编个",
    "随便编",
    "预测一下",
    "预测下月",
    "预测下个月",
    "猜一下",
    "大概是多少",
    "估个",
)
"""诱发幻觉的确定性标记（命中即安全拒绝：不生成事实网关之外的数值或预测）。"""

_RETURN_PROMISE_REFUSAL = "不做投资建议、不承诺收益（H6）：拒绝以收益承诺为前提的请求"
_FABRICATION_REFUSAL = "不生成事实（H1）：无法提供事实网关之外的数值或预测，安全拒绝"


class SessionState(str, Enum):
    """会话状态机。"""

    IDLE = "idle"
    NEEDS_CLARIFICATION = "needs_clarification"
    BLOCKED = "blocked"
    AWAITING_CONFIRM = "awaiting_confirm"
    EXECUTING = "executing"
    INTERRUPTED = "interrupted"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    TAKEN_OVER = "taken_over"
    REVERSED = "reversed"


class SessionStateError(RuntimeError):
    """当前状态不允许该操作（如未提交就确认）。"""


class ConfirmationMismatchError(RuntimeError):
    """确认的计划卡与待确认计划不一致。"""


class StrongVerificationRequiredError(RuntimeError):
    """红色（大额 / 不可逆）需强验证（多因子）后方可执行。"""


@dataclass(frozen=True)
class ConfirmationRecord:
    """确认记录：**同时保存 `Plan` 与 `cfhash`**（README 裁决 #6 的落点）。"""

    plan: Plan
    frozen_hash: str


@dataclass(frozen=True)
class SessionView:
    """状态视图。"""

    state: SessionState
    message: str


@dataclass(frozen=True)
class DailyQuota:
    """当日额度（**展示用只读透传**）：只透结论性数字，不含资产分档规则。

    口径与 PDP 一致：有效阈值 = `min(官方 1000 元, 客户限额)`（只加严不放宽）；
    当日累计与 `PolicyRequest.daily_accumulated` 同口径（**含本笔**）。仅黄/红卡携带。
    """

    limit_cents: int
    accumulated_cents: int


@dataclass(frozen=True)
class SubmissionView:
    """一次提交的产出（澄清 / 拒绝时 `card` / `plan` 为 `None`，**不产出级别**）。"""

    state: SessionState
    message: str
    card: PlanCard | None
    decision: Decision | None
    plan: Plan | None
    questions: tuple[str, ...]
    quota: DailyQuota | None = None
    """黄/红卡的当日额度（`None` = 绿卡 / 转人工 / 拒绝不显示）。"""


@dataclass(frozen=True)
class ExecutionView:
    """一次执行的产出（`record_ids` 为全部回执记录标识，供外部对账）。"""

    state: SessionState
    message: str
    frozen_hash: str
    exec_results: tuple[ExecResult, ...]
    record_ids: tuple[str, ...]


@dataclass(frozen=True)
class TakeoverPackage:
    """人工接管上下文包（PRD §5.8）：意图 / 计划 / 命中规则 / 审计 ID。"""

    reason: str
    level: Level
    intent_text: str
    frozen_hash: str
    card: PlanCard | None
    plan: Plan | None
    hit_rules: tuple[str, ...]
    audit_ids: tuple[str, ...]


@dataclass(frozen=True)
class _Pending:
    """会话内的待确认计划（含节点级裁决与锚点信息）。"""

    card: PlanCard
    plan: Plan
    decision: Decision
    node_decisions: tuple[Decision, ...]
    node_ids: tuple[str, ...]
    intent_text: str
    hit_rules: tuple[str, ...]


class Session:
    """单会话编排：`Session(deps)`。"""

    def __init__(self, deps: AppDeps) -> None:
        self._deps = deps
        self._state = SessionState.IDLE
        self._message = "会话就绪"
        self._pending: _Pending | None = None
        self._confirmation: ConfirmationRecord | None = None
        self._takeover: TakeoverPackage | None = None
        self._audit_ids: list[str] = []
        self._exec_results: list[ExecResult] = []
        self._completed_node_ids: list[str] = []
        self._pending_node_ids: list[str] = []

    def state(self) -> SessionState:
        """当前会话状态。"""
        return self._state

    def card(self) -> PlanCard | None:
        """当前待确认的计划卡（无则 `None`）。"""
        return self._pending.card if self._pending is not None else None

    def confirmation(self) -> ConfirmationRecord | None:
        """确认记录（未确认则 `None`）。"""
        return self._confirmation

    def submit(
        self, utterance: str, *, selected: Mapping[str, object] | None = None
    ) -> SubmissionView:
        """提交一句自然语言：装配意图 → 选场景 → 组装计划 → 裁决 → 计划卡。"""
        self._reset()
        refusal = _guard_utterance(utterance)
        if refusal is not None:
            return self._refuse(refusal, utterance)
        intents, questions = self._assemble_intents(utterance, selected)
        if not intents:
            if questions:
                self._state = SessionState.NEEDS_CLARIFICATION
                self._message = "信息不足，请补充：" + "；".join(questions)
                return SubmissionView(self._state, self._message, None, None, None, questions)
            return self._refuse("未知意图：无法匹配任何场景", utterance)
        try:
            handler = select_handler(intents)
        except UnknownScenarioError:
            return self._refuse("未知意图：无法匹配任何场景", utterance)
        outcome = run_scenario(handler, intents, self._deps.gateway)
        return self._after_outcome(outcome, utterance, questions)

    def _after_outcome(
        self, outcome: ScenarioOutcome, utterance: str, questions: tuple[str, ...]
    ) -> SubmissionView:
        if outcome.status is ScenarioStatus.NEEDS_CLARIFICATION:
            self._state = SessionState.NEEDS_CLARIFICATION
            self._message = outcome.message
            return SubmissionView(
                self._state, self._message, None, None, None, questions or (outcome.message,)
            )
        if outcome.status is not ScenarioStatus.PLANNED or outcome.plan is None:
            return self._refuse(outcome.message or "场景安全拒绝", utterance)
        plan = outcome.plan
        if outcome.decision is None:
            return self._refuse("场景未产出裁决，安全拒绝", utterance)
        flags = self._context_flags(utterance)
        node_decisions = tuple(self._node_decision(node, flags) for node in plan.nodes)
        decision = strictest(outcome.decision, *node_decisions)
        card = render_plan_card(plan, decision, self._deps.gateway)
        quota = (
            self._daily_quota(plan)
            if decision.level in (Level.YELLOW, Level.RED) and _plan_has_outflow(plan)
            else None
        )
        hit_rules = tuple(reason.value for reason in decision.reasons)
        pending = _Pending(
            card=card,
            plan=plan,
            decision=decision,
            node_decisions=node_decisions,
            node_ids=tuple(node.node_id for node in plan.nodes),
            intent_text=utterance,
            hit_rules=hit_rules,
        )
        self._pending = pending
        if decision.level >= Level.DENY:
            return self._refuse(
                "命中拒绝规则：" + "，".join(hit_rules), utterance, decision, plan, card
            )
        if decision.level >= Level.MANUAL:
            self._enter_takeover("高风险裁决（≥ L3）：" + "，".join(hit_rules), pending)
            return SubmissionView(self._state, self._message, card, decision, plan, ())
        self._state = SessionState.AWAITING_CONFIRM
        self._message = f"计划卡已生成（{len(plan.nodes)} 步，级别 {decision.level.name}）"
        return SubmissionView(self._state, self._message, card, decision, plan, (), quota)

    def _assemble_intents(
        self, utterance: str, selected: Mapping[str, object] | None
    ) -> tuple[tuple[Intent, ...], tuple[str, ...]]:
        proposal = self._deps.intent_model.propose(utterance, {"today": self._deps.today})
        payees = tuple(
            record.payload for record in self._deps.gateway.payees_of(self._deps.user_id)
        )
        chosen: dict[str, object] = (
            dict(selected) if selected is not None else _default_selected(self._deps)
        )
        extras = {key: value for key, value in chosen.items() if key not in SLOT_PARAMS}
        remark = extract_remark(utterance)
        intents: list[Intent] = []
        questions: list[str] = []
        for intent in proposal.intents:
            if intent.confidence < CONFIDENCE_THRESHOLD:
                continue
            span = _intent_span(intent, utterance)
            fill = fill_control_slots(
                intent.action,
                SlotInputs(
                    text=span,
                    today=self._deps.today,
                    payees=payees,
                    selected=_per_intent_selected(intent.action, chosen, self._deps),
                ),
            )
            if fill.negated:
                continue
            slots: dict[str, object] = {
                key: Value(value, Trust.TRUSTED) for key, value in fill.slots.items()
            }
            slots.update(extras)
            slots.setdefault("grant", "explicit")
            slots["span"] = span
            if remark is not None:
                slots["remark"] = Value(remark, Trust.UNVERIFIED)
            _inject_assessment(intent.action, slots, self._deps)
            questions.extend(question.prompt for question in fill.questions)
            intents.append(
                Intent(
                    action=intent.action,
                    slots=slots,
                    confidence=intent.confidence,
                    evidence=intent.evidence,
                )
            )
        return tuple(intents), tuple(questions)

    def _context_flags(self, utterance: str) -> frozenset[str]:
        if detect_injection(utterance):
            return frozenset({FLAG_INJECTION_DETECTED})
        return frozenset()

    def _node_decision(self, node: PlanNode, flags: frozenset[str]) -> Decision:
        request = node_policy_request(
            node,
            spec=self._deps.registry.get(node.action),
            gateway=self._deps.gateway,
            user_id=self._deps.user_id,
            today=self._deps.today,
            flags=flags,
        )
        return evaluate(request)

    def _daily_quota(self, plan: Plan) -> DailyQuota:
        """当日额度（展示用只读透传）：有效阈值 = `min(官方 1000 元, 客户限额)`。

        当日累计与裁决**同源**（复用 `node_policy_request` 的 `daily_accumulated`，含本笔），
        取各节点最大值（与计划级 `strictest` 同向）；只透结论性数字，不暴露资产分档规则，
        也不改变 PDP 判定语义。仅含 ≥1 流出节点的计划会走到这里（见 `_after_outcome`）。
        """
        customer = self._deps.gateway.daily_transfer_limit(self._deps.user_id)
        effective = min(DAILY_LIMIT_YUAN * _CENTS_PER_YUAN, customer.cents)
        accumulated = max(
            node_policy_request(
                node,
                spec=self._deps.registry.get(node.action),
                gateway=self._deps.gateway,
                user_id=self._deps.user_id,
                today=self._deps.today,
                flags=frozenset(),
            ).daily_accumulated.cents
            for node in plan.nodes
        )
        return DailyQuota(limit_cents=effective, accumulated_cents=accumulated)

    def _refuse(
        self,
        reason: str,
        utterance: str,
        decision: Decision | None = None,
        plan: Plan | None = None,
        card: PlanCard | None = None,
    ) -> SubmissionView:
        self._state = SessionState.BLOCKED
        self._message = f"安全拒绝：{reason}"
        self._takeover = self._build_takeover(reason, decision, plan, card, utterance)
        return SubmissionView(self._state, self._message, card, decision, plan, ())

    def _enter_takeover(self, reason: str, pending: _Pending) -> None:
        self._state = SessionState.TAKEN_OVER
        self._message = f"转人工接管：{reason}"
        self._takeover = self._build_takeover(
            reason, pending.decision, pending.plan, pending.card, pending.intent_text
        )

    def _build_takeover(
        self,
        reason: str,
        decision: Decision | None,
        plan: Plan | None,
        card: PlanCard | None,
        utterance: str,
    ) -> TakeoverPackage:
        level = decision.level if decision is not None else Level.MANUAL
        hit_rules = tuple(reason_code.value for reason_code in decision.reasons) if decision else ()
        frozen_hash = card.frozen_hash if card is not None else ""
        audit_id = self._audit(
            KIND_DECISION,
            {
                "kind": "takeover_context",
                "reason": reason,
                "level": level.name,
                "hit_rules": list(hit_rules),
                "intent_text": utterance,
                "frozen_hash": frozen_hash,
            },
        )
        return TakeoverPackage(
            reason=reason,
            level=level,
            intent_text=utterance,
            frozen_hash=frozen_hash,
            card=card,
            plan=plan,
            hit_rules=hit_rules,
            audit_ids=(audit_id,),
        )

    def _audit(self, kind: str, payload: dict[str, object]) -> str:
        event_hash = self._deps.audit.append(
            AuditEvent(
                event_id=f"{kind}:{len(self._audit_ids) + 1}",
                kind=kind,
                payload=payload,
                prev_hash="",
            )
        )
        self._audit_ids.append(event_hash)
        return event_hash

    def confirm(
        self,
        card_id: str,
        *,
        frozen_hash: str,
        strong_verified: bool = False,
        steps: int | None = None,
    ) -> ExecutionView:
        """确认并执行：计划级冻结校验 → 强验证闸门 → 熔断闸门 → 逐节点执行。

        `steps=None` 执行全部待执行节点；`steps=k` 最多执行 k 个（用于中断/恢复演示）。
        """
        pending = self._require_pending()
        if self._state not in (
            SessionState.AWAITING_CONFIRM,
            SessionState.EXECUTING,
            SessionState.INTERRUPTED,
        ):
            raise SessionStateError(f"当前状态不允许确认：{self._state.value}")
        if card_id != pending.card.card_id:
            raise ConfirmationMismatchError(
                f"计划卡不匹配：收到 {card_id}｜待确认 {pending.card.card_id}"
            )
        assert_frozen(pending.plan, frozen_hash)
        if PROMPT_STRONG_VERIFY in pending.card.requires and not strong_verified:
            raise StrongVerificationRequiredError(
                "红色（大额 / 不可逆）需强验证（多因子）后方可执行"
            )
        self._confirmation = ConfirmationRecord(
            plan=pending.plan, frozen_hash=pending.card.frozen_hash
        )
        if self._breaker_blocks(pending):
            self._enter_takeover("熔断打开：非纯查询转人工接管", pending)
            return self._execution_view()
        if not self._pending_node_ids:
            self._pending_node_ids = list(pending.node_ids)
        return self._execute(steps=steps)

    def _require_pending(self) -> _Pending:
        if self._pending is None:
            raise SessionStateError("尚无已裁决计划：请先 submit")
        return self._pending

    def _breaker_blocks(self, pending: _Pending) -> bool:
        return self._deps.breaker.is_open() and not plan_is_pure_query(pending.plan)

    def _execute(self, *, steps: int | None) -> ExecutionView:
        pending = self._require_pending()
        node_map = pending.plan.node_map()
        decision_map = dict(zip(pending.node_ids, pending.node_decisions, strict=True))
        remaining = [
            node_id for node_id in pending.node_ids if node_id not in self._completed_node_ids
        ]
        if steps is not None:
            remaining = remaining[:steps]
        self._state = SessionState.EXECUTING
        for node_id in remaining:
            node = node_map[node_id]
            try:
                call = self._to_call(node)
                result = self._deps.pep.execute(
                    call,
                    decision_map[node_id],
                    manifest_hash(call),
                    f"{pending.card.frozen_hash[:12]}-{node_id}",
                )
            except (NotAuthorizedError, GatewayError) as error:
                self._message = f"执行受阻（{node.action}）：{error}"
                self._state = (
                    SessionState.PARTIAL if self._completed_node_ids else SessionState.FAILED
                )
                return self._execution_view()
            self._exec_results.append(result)
            self._completed_node_ids.append(node_id)
            self._audit_ids.append(self._deps.audit.head())
        if len(self._completed_node_ids) == len(pending.node_ids):
            self._state = SessionState.COMPLETED
            self._message = f"计划执行完成（{len(self._completed_node_ids)} 步）"
        else:
            self._state = SessionState.EXECUTING
            self._message = (
                f"已执行 {len(self._completed_node_ids)} / {len(pending.node_ids)} 步，可中断"
            )
        return self._execution_view()

    def _to_call(self, node: PlanNode) -> ToolCall:
        """把计划节点适配成工具调用：类型转换 + **taint 槽位**把关（H3）。

        02 目录的金额参数已统一为 `amount_cents` / `daily_limit_cents`（整数分，README 裁决 #12），
        与计划节点的字段名逐字对齐，故**无需字段名映射**。

        **05→06 交接补齐（README 裁决 #16）**：05 的 `scenarios` 组装 transfer / wealth /
        subscription.sign 节点时未把 `user_id` 放进 `PlanNode.params`（由会话层与评测链装配）；
        此处对"必填控制槽 `user_id` 且节点未提供"的参数注入 `Value(self._deps.user_id, TRUSTED)`。
        **只在 `ToolCall` 层注入，不改动 `PlanNode.params` 与计划哈希**。
        """
        spec = self._deps.registry.get(node.action)
        params: dict[str, object] = {}
        for param in spec.params:
            if param.name not in node.params:
                if param.required and param.slot is SlotKind.CONTROL and param.name == "user_id":
                    params[param.name] = Value(self._deps.user_id, Trust.TRUSTED)
                continue
            value = _convert_param(param.name, node.params[param.name])
            trust = Trust.TRUSTED if param.slot is SlotKind.CONTROL else Trust.UNVERIFIED
            params[param.name] = Value(value, trust)
        return ToolCall(action=node.action, params=params)

    def reverse(self, *, now: datetime, settled: bool) -> ReversalOutcome:
        """回退：撤销窗口 + saga 补偿；已结算（竞态）**如实告知**，绝不宣称「已取消」。"""
        pending = self._require_pending()
        if self._confirmation is None:
            raise SessionStateError("尚未确认执行：不可回退")
        saga = Saga()
        for index, result in enumerate(self._exec_results, start=1):
            subject = result.record_ids[0] if result.record_ids else result.status
            saga.add(f"{subject}#{index}", _noop_forward(subject), self._compensation(subject))
        saga.run()
        window = ReversalWindow.open(pending.card.card_id, now=now)
        outcome = window.reverse(now=now, settled=settled, compensate=saga.compensate)
        self._audit_ids.append(self._deps.audit.head())
        if outcome.state.value in ("reversed", "compensating"):
            self._state = SessionState.REVERSED
        self._message = outcome.message
        return outcome

    def takeover_package(self) -> TakeoverPackage:
        """人工接管上下文包：意图 / 计划 / 命中规则 / 审计 ID（PRD §5.8）。"""
        if self._takeover is None:
            raise SessionStateError("当前无人工接管上下文包")
        return self._takeover

    def _compensation(self, subject: str) -> Callable[[], str]:
        def rollback() -> str:
            self._audit(
                KIND_REVERSAL,
                {"subject": subject, "outcome": "compensation_requested", "note": "已发起退回"},
            )
            return subject

        return rollback

    def interrupt(self) -> SessionView:
        """中断：保留已完成节点，未执行节点挂起（PRD §5.8）。"""
        pending = self._require_pending()
        if self._state not in (SessionState.AWAITING_CONFIRM, SessionState.EXECUTING):
            raise SessionStateError(f"当前状态不可中断：{self._state.value}")
        self._state = SessionState.INTERRUPTED
        self._message = (
            f"已中断：已完成 {len(self._completed_node_ids)} / {len(pending.node_ids)} 步，"
            "未执行节点已挂起"
        )
        return SessionView(self._state, self._message)

    def resume(self) -> ExecutionView:
        """恢复：继续执行剩余节点（幂等：已完成节点不重复执行）。"""
        if self._state is not SessionState.INTERRUPTED:
            raise SessionStateError(f"当前状态不可恢复：{self._state.value}")
        return self._execute(steps=None)

    def _execution_view(self) -> ExecutionView:
        frozen_hash = self._pending.card.frozen_hash if self._pending is not None else ""
        return ExecutionView(
            state=self._state,
            message=self._message,
            frozen_hash=frozen_hash,
            exec_results=tuple(self._exec_results),
            record_ids=tuple(
                record_id for result in self._exec_results for record_id in result.record_ids
            ),
        )

    def _reset(self) -> None:
        self._state = SessionState.IDLE
        self._message = "会话已重置"
        self._pending = None
        self._confirmation = None
        self._takeover = None
        self._audit_ids = []
        self._exec_results = []
        self._completed_node_ids = []
        self._pending_node_ids = []


def detect_injection(text: str) -> bool:
    """确定性注入检测：命中标记即视为注入（不做启发式猜测）。"""
    lowered = text.lower()
    return any(marker.lower() in lowered for marker in INJECTION_MARKERS)


def extract_remark(utterance: str) -> str | None:
    """抽取"备注"后的文本（作为**数据槽**内容：永不解释为指令）。"""
    marker = "备注"
    index = utterance.find(marker)
    if index < 0:
        return None
    tail = utterance[index + len(marker) :].lstrip("：: ")
    return tail or None


def _plan_has_outflow(plan: Plan) -> bool:
    """计划是否含 ≥1 流出节点：金额档（及额度行）只对流出型计划有意义。"""
    return any(ACTION_KINDS.get(node.action) is ActionKind.OUTFLOW for node in plan.nodes)


def _guard_utterance(utterance: str) -> str | None:
    if any(hint in utterance for hint in FORBIDDEN_HINTS):
        return _RETURN_PROMISE_REFUSAL
    if any(marker in utterance for marker in FABRICATION_MARKERS):
        return _FABRICATION_REFUSAL
    return None


def _default_selected(deps: AppDeps) -> dict[str, object]:
    """默认选中活期储蓄账户（受信任调用方默认值，非模型推断）。"""
    for record in deps.gateway.accounts_of(deps.user_id):
        if record.payload.kind == "savings":
            return {"account": record.payload.account_id}
    return {}


_SUBSCRIBE_DEFAULT_PRODUCT = "wp_cash"
"""申购的受信任默认产品（起购 1 元，R1 ≤ 客户测评）；**仅** `wealth.subscribe` 使用。"""


def _per_intent_selected(
    action: str, chosen: Mapping[str, object], deps: AppDeps
) -> dict[str, object]:
    """受信任的 per-intent 选中对象：**仅** `wealth.subscribe` 补默认产品与用户身份。

    不改共享 `chosen`，故 `wealth.redeem` 等动作的既有澄清行为不变（收窄）。
    """
    selected = dict(chosen)
    if action == "wealth.subscribe":
        selected.setdefault("product", _SUBSCRIBE_DEFAULT_PRODUCT)
        selected.setdefault("user_id", deps.user_id)
    return selected


def _inject_assessment(action: str, slots: dict[str, object], deps: AppDeps) -> None:
    """受信任注入风险测评事实（**仅** `wealth.subscribe`）。

    **不包 `Value`**：场景层 `_assessment` 以 `isinstance(raw, RiskAssessment)` 判定，
    包装会导致其静默失效并退化为"未测评"拒绝。
    """
    if action != "wealth.subscribe" or deps.risk is None:
        return
    assessment = to_risk_assessment(deps.risk.assessment_for(deps.user_id))
    if assessment is not None:
        slots["assessment"] = assessment


def _intent_span(intent: Intent, utterance: str) -> str:
    span = intent.slots.get("span")
    if isinstance(span, str) and span and span in utterance:
        return span
    return utterance


_DATE_PARAMS = frozenset({"booked_on", "start", "end"})
"""需要从 ISO 文本还原为 `date` 的工具参数（02 目录声明为 `date`，计划节点里是 ISO 字符串）。"""


def _convert_param(name: str, raw: object) -> object:
    """按 02 目录的类型声明做一次确定性转换。

    02 金额参数已是整数分（`ParamSpec.py_type is int`，README 裁决 #12），无需再包装成 `Money`；
    仅需把 ISO 文本还原成 `date`。
    """
    if name in _DATE_PARAMS and isinstance(raw, str):
        return date.fromisoformat(raw)
    return raw


def _noop_forward(subject: str) -> Callable[[], str]:
    """回退时的占位正向动作：正向已在执行阶段完成，这里只保留可逆步骤名。"""

    def action() -> str:
        return subject

    return action

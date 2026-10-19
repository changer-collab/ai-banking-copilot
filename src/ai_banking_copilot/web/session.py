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

import re
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from enum import StrEnum
from functools import wraps
from threading import RLock
from typing import TYPE_CHECKING, Concatenate, ParamSpec, TypeVar

from ai_banking_copilot.contracts.authorization import Decision, Level, ReasonCode, strictest
from ai_banking_copilot.contracts.plan import Plan, PlanNode, topological_order
from ai_banking_copilot.contracts.security import (
    INJECTION_MARKERS as INJECTION_MARKERS,
)
from ai_banking_copilot.contracts.security import (
    detect_injection,
)
from ai_banking_copilot.contracts.taint import SlotKind, Trust, Value
from ai_banking_copilot.gateway.errors import GatewayError
from ai_banking_copilot.gateway.models import Money
from ai_banking_copilot.gateway.preconditions import (
    PreconditionResult,
    PreconditionStatus,
    WritePreconditionError,
)
from ai_banking_copilot.planning.kernel import CONFIDENCE_THRESHOLD
from ai_banking_copilot.planning.llm import ACTION_KINDS, ActionKind, Intent
from ai_banking_copilot.planning.review_context import referenced_facts
from ai_banking_copilot.planning.reviewer import (
    ReviewerUnavailableError,
    ReviewReport,
    ReviewRequest,
    ReviewStatus,
)
from ai_banking_copilot.planning.slots import (
    REQUIRED_SLOTS,
    SLOT_PARAMS,
    SlotInputs,
    fill_control_slots,
)
from ai_banking_copilot.policy.audit import (
    KIND_BREAKER,
    KIND_DECISION,
    KIND_REVERSAL,
    AuditEvent,
)
from ai_banking_copilot.policy.breaker import (
    DEFAULT_POLICY_REJECTION_THRESHOLD,
    HIGH_FREQUENCY_LIMIT,
    HIGH_FREQUENCY_WINDOW,
    BreakerTrigger,
    CircuitBreaker,
)
from ai_banking_copilot.policy.execution import (
    CommittedWriteError,
    ExecutionContext,
    assess_plan,
)
from ai_banking_copilot.policy.gate import assert_frozen
from ai_banking_copilot.policy.pdp import (
    FLAG_DEVICE_CHANGE,
    FLAG_INJECTION_DETECTED,
    evaluate,
)
from ai_banking_copilot.policy.pep import ExecResult, NotAuthorizedError
from ai_banking_copilot.policy.saga import (
    CompensationError,
    ReversalOutcome,
    ReversalState,
    Saga,
)
from ai_banking_copilot.scenarios.authorization import node_policy_request
from ai_banking_copilot.scenarios.base import (
    ScenarioOutcome,
    ScenarioStatus,
    plan_is_pure_query,
)
from ai_banking_copilot.scenarios.bill_advice import BillAdvice, build_bill_advice
from ai_banking_copilot.scenarios.pending import PendingHandler
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

_P = ParamSpec("_P")
_R = TypeVar("_R")


def _serialized(
    method: Callable[Concatenate[Session, _P], _R],
) -> Callable[Concatenate[Session, _P], _R]:
    """同一会话的状态变化串行，嵌套的澄清 / 确认复用可重入锁。"""
    @wraps(method)
    def wrapped(self: Session, /, *args: _P.args, **kwargs: _P.kwargs) -> _R:
        with self._lock:
            return method(self, *args, **kwargs)
    return wrapped

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


class SessionState(StrEnum):
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
    """红色（大额 / 不可逆）需通过强验证挑战后方可执行。"""


VERIFICATION_TTL_S = 300
"""强验证挑战有效期（秒）：过期即失效，必须重新申请。"""

_DEMO_VERIFICATION_TARGET = "138****8888"
"""演示环境回显用的脱敏接收号码（**未接入真实短信网关**）。"""


@dataclass(frozen=True)
class VerificationPrompt:
    """一次强验证挑战的下发结果。

    **演示声明**：本仓库未接入真实短信 / 人脸 / U 盾渠道，`code` 随响应回显以便路演现场输入。
    接入真实下发渠道时只需替换 `Session.request_verification` 的**下发一步**；
    校验逻辑（一次性 + TTL + 与 `card_id` + `frozen_hash` 绑定）保持不变。
    """

    channel: str
    target: str
    code: str
    expires_in_s: int


@dataclass(frozen=True)
class _Challenge:
    """服务端持有的强验证挑战：**一次性**、有 TTL、与卡号 + 冻结哈希绑定。"""

    code: str
    card_id: str
    frozen_hash: str
    expires_at: datetime
    execution_id: str


@dataclass(frozen=True)
class ConfirmationRecord:
    """确认记录：**同时保存 `Plan` 与 `cfhash`**（README 裁决 #6 的落点）。"""

    plan: Plan
    frozen_hash: str
    execution_id: str
    approved_level: Level
    strong_verified: bool


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
class ClarificationAsk:
    """一条待澄清提问：`slot` 为需补齐的槽位名（与 `selected` 同口径），`prompt` 为文案。"""

    slot: str
    prompt: str


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

    clarifications: tuple[ClarificationAsk, ...] = ()
    """待澄清的**结构化**提问（槽位 + 文案）：渠道层据此渲染「回答即可继续」的闭环输入。"""


@dataclass(frozen=True)
class ExecutionView:
    """一次执行的产出（`record_ids` 为全部回执记录标识，供外部对账）。"""

    state: SessionState
    message: str
    frozen_hash: str
    exec_results: tuple[ExecResult, ...]
    record_ids: tuple[str, ...]
    card: PlanCard | None = None
    quota: DailyQuota | None = None
    decision: Decision | None = None
    bill_advice: tuple[BillAdvice, ...] = ()
    """月账单执行完成后的只读消费建议；不改变计划和授权。"""
    bill_advice_message: str = ""


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
    flags: frozenset[str] = frozenset()


class Session:
    """单会话编排：`Session(deps)`。"""

    def __init__(self, deps: AppDeps) -> None:
        self._lock = RLock()
        self._deps = deps
        self._execution_id = secrets.token_hex(16)
        self._state = SessionState.IDLE
        self._message = "会话就绪"
        self._pending: _Pending | None = None
        self._confirmation: ConfirmationRecord | None = None
        self._takeover: TakeoverPackage | None = None
        self._audit_ids: list[str] = []
        self._exec_results: list[ExecResult] = []
        self._completed_node_ids: list[str] = []
        self._pending_node_ids: list[str] = []
        self._challenge: _Challenge | None = None
        self._unverified_results: dict[str, ExecResult] = {}
        self._quota: DailyQuota | None = None
        # 熔断触发 3 / 4 的**跨提交**计数（有意不被 `_reset()` 清空：它们是会话级而非单次提交级）
        self._risky_at: list[datetime] = []
        self._policy_rejections = 0
        self._session_breaker = CircuitBreaker()
        """**会话级熔断器**（README 裁决 #27）：触发 3 / 4 / 5 的**作用域**——只让本会话转人工接管，
        不波及其他会话。全局熔断器（`deps.breaker`）只由**系统性**信号触发（触发 1 / 2）。"""
        self._reversed_records: list[str] = []
        # 澄清闭环：保留上一条指令与选中对象，供 `clarify()` 在原指令上续答（不被 `_reset()` 清空）
        self._last_utterance: str | None = None
        self._last_selected: Mapping[str, object] | None = None
        self._review_intents: list[dict[str, object]] = []
        self._clarification_keys: frozenset[str] = frozenset()
        self._review_report: ReviewReport | None = None

    def state(self) -> SessionState:
        """当前会话状态。"""
        return self._state

    def card(self) -> PlanCard | None:
        """当前待确认的计划卡（无则 `None`）。"""
        return self._pending.card if self._pending is not None else None

    def confirmation(self) -> ConfirmationRecord | None:
        """确认记录（未确认则 `None`）。"""
        return self._confirmation

    def review_report(self) -> ReviewReport | None:
        """当前执行的独立计划审查结果；确认重试沿用同一报告。"""
        return self._review_report

    @_serialized
    def submit(
        self,
        utterance: str,
        *,
        selected: Mapping[str, object] | None = None,
        session_anomaly: bool = False,
        now: datetime | None = None,
    ) -> SubmissionView:
        """一次新的用户意图拥有独立执行身份，即使内容完全相同。"""
        return self._submit(
            utterance, selected=selected, session_anomaly=session_anomaly, now=now,
            execution_id=secrets.token_hex(16),
        )

    def _submit(
        self,
        utterance: str,
        *,
        selected: Mapping[str, object] | None = None,
        session_anomaly: bool = False,
        now: datetime | None = None,
        execution_id: str,
        clarification_keys: frozenset[str] = frozenset(),
    ) -> SubmissionView:
        """提交一句自然语言：装配意图 → 选场景 → 组装计划 → 裁决 → 计划卡。

        `session_anomaly`：**受信任调用方**上报的会话异常（设备 / 地点跳变）——本层无此信号源，
        故由调用方按事实传入；命中即按触发 5 熔断，并同时给 PDP 加 `device_change` 标记。
        """
        self._reset()
        self._execution_id = execution_id
        moment = now if now is not None else datetime.now()
        self._last_utterance = utterance
        self._last_selected = dict(selected) if selected is not None else None
        self._clarification_keys = clarification_keys
        refusal = _guard_utterance(utterance)
        if refusal is not None:
            return self._refuse(refusal, utterance)
        invalid = self._invalid_selection(selected or {})
        if invalid is not None:
            return self._refuse(invalid, utterance)
        intents, asks, notes = self._assemble_intents(utterance, selected)
        if not intents:
            if asks:
                self._state = SessionState.NEEDS_CLARIFICATION
                self._message = _with_notes(
                    "信息不足，请补充：" + "；".join(ask.prompt for ask in asks), notes
                )
                return SubmissionView(
                    self._state, self._message, None, None, None,
                    tuple(ask.prompt for ask in asks), clarifications=asks,
                )
            return self._refuse(
                _with_notes("未知意图：无法匹配任何场景", notes),
                utterance,
                capability_boundary=True,
            )
        try:
            handler = select_handler(intents)
        except UnknownScenarioError:
            return self._refuse(
                _with_notes("未知意图：无法匹配任何场景", notes),
                utterance,
                capability_boundary=True,
            )
        outcome = run_scenario(handler, intents, self._deps.gateway)
        if outcome.plan is not None:
            self._note_risky_activity(
                now=moment, risky=not plan_is_pure_query(outcome.plan)
            )
        return self._after_outcome(
            outcome,
            utterance,
            asks,
            candidate_actions=tuple(intent.action for intent in intents),
            session_anomaly=session_anomaly,
            notes=notes,
        )

    @_serialized
    def clarify(self, answers: Mapping[str, object]) -> SubmissionView:
        """澄清闭环：用**补充答案**重跑上一次的指令（用户不必整句重述）。

        `answers` 是「槽位名 → 值」，与 `selected` 同口径——因此模板通道与 LLM 通道都能用，
        不依赖模型从补充文本里重新推断。没有待澄清的上一次指令时抛 `SessionStateError`。
        """
        if self._last_utterance is None:
            raise SessionStateError("尚无上一条指令：无法澄清")
        merged: dict[str, object] = dict(self._last_selected or {})
        merged.update(answers)
        execution_id = self._execution_id if self._pending is None else secrets.token_hex(16)
        return self._submit(
            self._last_utterance, selected=merged, execution_id=execution_id,
            clarification_keys=self._clarification_keys | frozenset(answers),
        )

    @_serialized
    def check_due(self) -> SubmissionView:
        """只提案；到期不沿用旧授权，确认、强验证和熔断仍走同一条链。"""
        self._reset()
        self._last_selected = None
        self._last_utterance = None
        handler = PendingHandler(self._deps.user_id, self._deps.today)
        outcome = run_scenario(handler, (), self._deps.gateway)
        plan = outcome.plan
        if plan is None:
            self._state = SessionState.IDLE
            self._message = "当前没有到期待办。"
            return SubmissionView(self._state, self._message, None, None, None, ())
        self._note_risky_activity(now=datetime.now(), risky=True)
        return self._after_outcome(
            outcome,
            "检查到期待办并生成待确认提案",
            (),
            candidate_actions=tuple(node.action for node in plan.nodes),
            review_sources=handler.review_sources(self._deps.gateway),
            review_kind="due_proposal",
        )

    def _invalid_selection(self, selected: Mapping[str, object]) -> str | None:
        """对象由本人事实列表选取；客户端不能靠填写内部 ID 操作其他人的对象。"""
        gateway, user_id = self._deps.gateway, self._deps.user_id
        owned = {
            "account": {r.payload.account_id for r in gateway.accounts_of(user_id)},
            "card": {r.payload.card_id for r in gateway.cards_of(user_id)},
            "subscription": {r.payload.subscription_id for r in gateway.subscriptions_of(user_id)},
            "hold": {r.payload.hold_id for r in gateway.active_holds_of(user_id)},
            "payee": {r.payload.payee_id for r in gateway.payees_of(user_id)},
            "payer": {r.payload.payee_id for r in gateway.payees_of(user_id)},
        }
        for slot, identifiers in owned.items():
            # 场景兼容内部参数名；两套名字都必须检查，不能从 extras 绕过本人范围。
            aliases = (slot, SLOT_PARAMS.get(slot, slot))
            scoped = tuple(
                key for key in selected
                if any(re.fullmatch(rf"intent_\d+_{alias}", key) for alias in aliases)
            )
            for key in (*aliases, *scoped):
                value = selected.get(key)
                if isinstance(value, Value):
                    value = value.raw
                if isinstance(value, str) and value not in identifiers:
                    return "所选对象不存在、已失效或不属于当前用户，请重新选择。"
        return None

    def _after_outcome(
        self,
        outcome: ScenarioOutcome,
        utterance: str,
        asks: tuple[ClarificationAsk, ...],
        *,
        candidate_actions: tuple[str, ...] = (),
        session_anomaly: bool = False,
        notes: tuple[str, ...] = (),
        review_sources: list[dict[str, object]] | None = None,
        review_kind: str = "user_request",
    ) -> SubmissionView:
        questions = tuple(ask.prompt for ask in asks)
        if outcome.status is ScenarioStatus.NEEDS_CLARIFICATION:
            self._state = SessionState.NEEDS_CLARIFICATION
            self._message = _with_notes(outcome.message, notes)
            return SubmissionView(
                self._state,
                self._message,
                None,
                None,
                None,
                questions or (outcome.message,),
                clarifications=asks,
            )
        if outcome.status is not ScenarioStatus.PLANNED or outcome.plan is None:
            return self._refuse(outcome.message or "场景安全拒绝", utterance)
        plan = outcome.plan
        if outcome.decision is None:
            return self._refuse("场景未产出裁决，安全拒绝", utterance)
        flags = self._context_flags(utterance, session_anomaly=session_anomaly)
        risk = assess_plan(
            plan, registry=self._deps.registry, gateway=self._deps.gateway,
            user_id=self._deps.user_id, today=self._deps.today, flags=flags,
            floor=outcome.decision,
        )
        node_decisions = risk.node_decisions
        decision = risk.decision
        card = self._render_card(plan, decision)
        quota = (
            DailyQuota(risk.limit_cents, risk.accumulated_cents)
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
            flags=flags,
        )
        self._pending = pending
        self._quota = quota
        review_error: str | None = None
        reviewer_enforced = self._deps.reviewer_required and not plan_is_pure_query(plan)
        if self._deps.reviewer is not None:
            request = ReviewRequest(
                execution_id=self._execution_id,
                cfhash=card.frozen_hash,
                prompt_version="reviewer-v1",
                raw_text=utterance,
                candidate_actions=candidate_actions,
                plan=plan,
                slot_sources={
                    "request": {
                        "kind": review_kind, "user_id": self._deps.user_id,
                        "today": self._deps.today.isoformat(),
                        "authorization": "pending_confirmation",
                    },
                    "intents": self._review_intents,
                    "facts": review_sources if review_sources is not None else referenced_facts(
                        self._deps.gateway, self._deps.user_id, self._review_intents
                    ),
                },
                source_spans=(utterance,),
                plan_summary=(
                    "到期待办提案：用户只请求检查；须确认计划卡并通过风险分级后才能执行。"
                    if review_kind == "due_proposal" else "用户提交的待确认计划"
                ),
            )
            try:
                self._review_report = self._deps.reviewer.review(request)
            except ReviewerUnavailableError as error:
                review_error = str(error)
                self._review_report = None
            except Exception as error:  # noqa: BLE001 - strict channel fail-closed when required
                review_error = f"{type(error).__name__}: {error}"
                self._review_report = None
            if self._review_report is not None:
                report = self._review_report
                self._audit(
                    KIND_DECISION,
                    {
                        "execution_id": report.execution_id,
                        "frozen_hash": report.cfhash,
                        "review_status": report.status.value,
                        "review_channel": report.channel,
                        "review_model": report.model,
                        "review_prompt_hash": report.prompt_hash,
                        "review_failure": report.failure_code.value
                        if report.failure_code is not None
                        else None,
                        "review_diagnostic_only": report.diagnostic_only,
                        "review_latency_ms": report.latency_ms,
                        "review_error_type": report.error_type,
                        "review_http_status": report.http_status,
                        "review_timeout_s": report.timeout_s,
                    },
                )
                if reviewer_enforced and report.status is ReviewStatus.CLARIFY:
                    if review_kind == "due_proposal":
                        self._enter_takeover(
                            "到期待办审查需要重新检查："
                            + "；".join(report.clarifications or report.findings)
                            + "。请重新检查待办或转人工处理。",
                            pending,
                        )
                        return SubmissionView(
                            self._state, self._message, card, decision, plan, ()
                        )
                    self._pending = None
                    self._confirmation = None
                    clarification_text = report.clarifications or report.findings
                    self._state = SessionState.NEEDS_CLARIFICATION
                    self._message = "独立审查需要澄清：" + "；".join(clarification_text)
                    return SubmissionView(
                        self._state,
                        self._message,
                        None,
                        None,
                        None,
                        tuple(clarification_text),
                    )
                if reviewer_enforced and report.status in {
                    ReviewStatus.REJECT,
                    ReviewStatus.MANUAL,
                }:
                    self._enter_takeover(
                        "独立审查未通过：" + "；".join(report.findings or (report.status.value,)),
                        pending,
                    )
                    return SubmissionView(self._state, self._message, card, decision, plan, ())
            else:
                self._audit(
                    KIND_DECISION,
                    {
                        "execution_id": self._execution_id,
                        "frozen_hash": card.frozen_hash,
                        "review_status": ReviewStatus.MANUAL.value,
                        "review_channel": "unavailable",
                        "review_model": None,
                        "review_prompt_hash": None,
                        "review_failure": review_error or "unknown",
                        "review_diagnostic_only": True,
                        "review_latency_ms": 0.0,
                    },
                )
                if reviewer_enforced:
                    self._enter_takeover(
                        "独立审查通道不可用：" + (review_error or "未知错误"), pending
                    )
                    return SubmissionView(self._state, self._message, card, decision, plan, ())
        if decision.level >= Level.DENY:
            return self._refuse(
                "命中拒绝规则：" + "，".join(hit_rules), utterance, decision, plan, card
            )
        if decision.level >= Level.MANUAL:
            self._enter_takeover("高风险裁决（≥ L3）：" + "，".join(hit_rules), pending)
            return SubmissionView(self._state, self._message, card, decision, plan, ())
        self._clear_policy_rejections()
        self._state = SessionState.AWAITING_CONFIRM
        self._message = _with_notes(
            f"计划卡已生成（{len(plan.nodes)} 步，级别 {decision.level.name}）", notes
        )
        return SubmissionView(self._state, self._message, card, decision, plan, (), quota)

    def _assemble_intents(
        self, utterance: str, selected: Mapping[str, object] | None,
    ) -> tuple[tuple[Intent, ...], tuple[ClarificationAsk, ...], tuple[str, ...]]:
        """装配意图，并返回**未采纳提示**（低置信度意图不再静默丢弃，必须让用户看得见）。"""
        proposal = self._deps.intent_model.propose(utterance, {"today": self._deps.today})
        payees = tuple(
            record.payload for record in self._deps.gateway.payees_of(self._deps.user_id)
        )
        chosen: dict[str, object] = (
            dict(selected) if selected is not None else _default_selected(self._deps)
        )
        # `action` / `intent_spans` 是会话澄清用的元数据，不能作为场景槽位透传。
        extras = {
            key: value for key, value in chosen.items()
            if key not in SLOT_PARAMS and key not in {"action", "intent_spans"}
            and not key.startswith("intent_")
        }
        remark = extract_remark(utterance)
        intents: list[Intent] = []
        asks: list[ClarificationAsk] = []
        dropped: list[str] = []
        candidates = tuple(
            intent for intent in proposal.intents
            if intent.confidence >= CONFIDENCE_THRESHOLD
        )
        dropped.extend(
            intent.action for intent in proposal.intents
            if intent.confidence < CONFIDENCE_THRESHOLD
        )
        local_slots = _local_intent_slots(candidates)

        # 多动作时每个动作必须回指用户原文中的**不同片段**。模型省略 span，或把
        # 两个动作都指向整句，多金额解析会拒绝、无法可靠绑定；此时逐动作澄清，
        # 也不猜测金额归属。每个动作单独给出普通文本问题，供页面直接续答。
        span_overrides = _intent_span_overrides(chosen.get("intent_spans"))
        spans: list[str] = []
        for index, intent in enumerate(candidates):
            override = chosen.get(f"intent_{index}_span")
            if not isinstance(override, str):
                override = _span_override_for(span_overrides, intent.action, index)
            span = _intent_span(
                intent, utterance, multiple=len(candidates) > 1,
                override=override,
            )
            if span is None:
                return (
                    (),
                    _multi_intent_span_questions(candidates),
                    _dropped_notes(dropped),
                )
            spans.append(span)
        if len(candidates) > 1 and not _spans_are_unambiguous(utterance, spans):
            return (), _multi_intent_span_questions(candidates), _dropped_notes(dropped)
        contexts = _intent_semantic_contexts(utterance, spans)
        if contexts is None:
            return (), _multi_intent_span_questions(candidates), _dropped_notes(dropped)
        prefix_targets = _unassigned_booking_prefix_targets(utterance, spans, candidates)

        # “登记/预约转账”表达的是登记未来动作；`transfer.create` 与
        # `transfer.schedule` 当前都可能直接扣款。明确的登记语义确定性落到
        # `transfer.book`，只有同句又出现“立即/马上/现在”等冲突语义时才澄清，
        # 避免把登记请求送进立即扣款 handler。
        for index, (intent, span, context) in enumerate(
            zip(candidates, spans, contexts, strict=True)
        ):
            # 完整原文范围同时用于控制槽解析，模型不能裁掉否定词或另一金额。
            span = context
            if index in prefix_targets:
                # 独立短句的作用范围未被模型采纳时，只澄清紧随其后的首个转账，
                # 不能把全局 action 答案复制到多个转账动作。
                resolved_action = _transfer_action_choice(chosen.get(f"intent_{index}_action"))
                if resolved_action is None:
                    return (
                        (),
                        (_booking_semantics_question(index, len(candidates)),),
                        _dropped_notes(dropped),
                    )
                intent = replace(intent, action=resolved_action)
            elif intent.action in _TRANSFER_ACTIONS and _has_booking_transfer_marker(context):
                if _has_immediate_transfer_marker(context):
                    choice = chosen.get(f"intent_{index}_action", chosen.get("action"))
                    resolved_action = _transfer_action_choice(choice)
                    if resolved_action is None:
                        return (
                            (),
                            (_booking_semantics_question(index, len(candidates)),),
                            _dropped_notes(dropped),
                        )
                    intent = replace(intent, action=resolved_action)
                elif intent.action in _IMMEDIATE_TRANSFER_ACTIONS:
                    intent = replace(intent, action="transfer.book")
            intent_selected = _scoped_intent_selected(chosen, index, local_slots)
            slot_text = (
                _BOOKING_NO_DEBIT_MARKER.sub("不扣款", span)
                if intent.action == "transfer.book" else span
            )
            fill = fill_control_slots(
                intent.action,
                SlotInputs(
                    text=slot_text,
                    today=self._deps.today,
                    payees=payees,
                    selected=_per_intent_selected(intent.action, intent_selected, self._deps),
                ),
            )
            if fill.negated:
                continue
            sources: dict[str, str] = {}
            for slot in fill.slots:
                if f"selected:{slot}" in fill.trace:
                    if slot == "user_id":
                        sources[slot] = "server_identity"
                    elif (
                        slot in self._clarification_keys
                        or f"intent_{index}_{slot}" in self._clarification_keys
                    ):
                        sources[slot] = "clarification_answer"
                    elif selected is None or (slot == "product" and slot not in chosen):
                        sources[slot] = "server_default"
                    else:
                        sources[slot] = "user_selection"
                elif slot in {"payee", "payer"}:
                    sources[slot] = "gateway_resolution"
                elif f"default {slot}:{fill.slots[slot]}" in fill.trace:
                    sources[slot] = "server_default"
                else:
                    sources[slot] = "text_parser"
            self._review_intents.append({
                "action": intent.action, "span": span,
                "slots": {slot: _review_value(value) for slot, value in fill.slots.items()},
                "trace": list(fill.trace), "selected_sources": sources,
            })
            if remark is not None:
                self._review_intents[-1]["data_slots"] = {
                    "remark": {"value": remark, "trust": "unverified", "role": "data_only"}
                }
            slots: dict[str, object] = {
                key: Value(value, Trust.TRUSTED) for key, value in fill.slots.items()
            }
            slots.update({
                key: value for key, value in extras.items()
                if key not in {SLOT_PARAMS[slot] for slot in local_slots}
            })
            slots.setdefault("grant", "explicit")
            slots["span"] = span
            if remark is not None:
                slots["remark"] = Value(remark, Trust.UNVERIFIED)
            _inject_assessment(intent.action, slots, self._deps)
            asks.extend(
                ClarificationAsk(
                    slot=(
                        f"intent_{index}_{question.slot}"
                        if question.slot in local_slots else question.slot
                    ),
                    prompt=(
                        f"第 {index + 1} 项{_intent_label(intent.action)}：{question.prompt}"
                        if len(candidates) > 1 else question.prompt
                    ),
                )
                for question in fill.questions
            )
            intents.append(
                Intent(
                    action=intent.action,
                    slots=slots,
                    confidence=intent.confidence,
                    evidence=intent.evidence,
                )
            )
        return tuple(intents), tuple(asks), _dropped_notes(dropped)

    def _render_card(self, plan: Plan, decision: Decision) -> PlanCard:
        card = render_plan_card(plan, decision, self._deps.gateway)
        # 相同内容的新提交也不能接受上一轮的确认请求。
        return replace(card, card_id=f"{card.card_id}_{self._execution_id}")

    def _node_decision(self, node: PlanNode, flags: frozenset[str]) -> Decision:
        """单节点口径的兼容入口；产品执行另叠加完整计划风险。"""
        return evaluate(node_policy_request(
            node, spec=self._deps.registry.get(node.action), gateway=self._deps.gateway,
            user_id=self._deps.user_id, today=self._deps.today, flags=flags,
        ))

    def _context_flags(
        self, utterance: str, *, session_anomaly: bool = False
    ) -> frozenset[str]:
        """上下文异常标记的**唯一生产者**，并同步接线熔断触发 2 / 5（**作用域不同**，裁决 #27）。

        - 注入检测命中 → 标记 `injection_detected`（PDP 单向升 `MANUAL`）+ **全局**熔断（触发 2）：
          注入是**系统性**威胁（可能来自任何会话 / 任何入参面），配得上全局拒绝；
        - 调用方上报会话异常 → 标记 `device_change`（PDP 升黄）+ **会话级**熔断（触发 5）：
          设备 / 地点跳变是**单个会话**的信号，只惩罚该会话。
        """
        flags: set[str] = set()
        if detect_injection(utterance):
            flags.add(FLAG_INJECTION_DETECTED)
            self._trip_global(BreakerTrigger.INJECTION_DETECTED, "注入检测命中（H3）")
        if session_anomaly:
            flags.add(FLAG_DEVICE_CHANGE)
            self._trip_session(
                BreakerTrigger.SESSION_ANOMALY, "会话异常：设备 / 地点跳变（调用方上报）"
            )
        return frozenset(flags)

    def _trip_global(self, trigger: BreakerTrigger, detail: str) -> None:
        """**全局**熔断（触发 1 / 2 = 系统性信号）：跨会话、跨 worker；只加严不降级，写审计留痕。"""
        self._deps.breaker.trip(trigger)
        self._audit(
            KIND_BREAKER,
            {
                "trigger": trigger.value,
                "detail": detail,
                "scope": "global",
                "state": self._deps.breaker.state().value,
            },
        )

    def _trip_session(self, trigger: BreakerTrigger, detail: str) -> None:
        """**会话级**熔断（触发 3 / 4 / 5 = 单会话行为异常）：只惩罚本会话，不动全局。

        裁决 #27 的判据：全局熔断意味着「拒绝**所有**用户的非纯查询动作」，只有系统性威胁
        （注入检测 / 连续授权失败）配得上；「某个会话在刷 / 连续被拒」应精确落在该会话。
        """
        self._session_breaker.trip(trigger)
        self._audit(
            KIND_BREAKER,
            {
                "trigger": trigger.value,
                "detail": detail,
                "scope": "session",
                "state": self._session_breaker.state().value,
            },
        )

    def _note_risky_activity(self, *, now: datetime, risky: bool) -> None:
        """高频判定（触发 3）：滑动窗口内高风险操作达上限 ⇒ **本会话**熔断。"""
        if not risky:
            return
        cutoff = now - HIGH_FREQUENCY_WINDOW
        self._risky_at = [stamp for stamp in self._risky_at if stamp > cutoff]
        self._risky_at.append(now)
        if len(self._risky_at) >= HIGH_FREQUENCY_LIMIT:
            window_s = int(HIGH_FREQUENCY_WINDOW.total_seconds())
            self._trip_session(
                BreakerTrigger.HIGH_FREQUENCY_RISK,
                f"{window_s}s 内高风险操作达 {len(self._risky_at)} 次",
            )

    def _note_policy_rejection(self) -> None:
        """策略引擎**连续**拒绝（触发 4）：达阈值 ⇒ **本会话**熔断。"""
        self._policy_rejections += 1
        if self._policy_rejections >= DEFAULT_POLICY_REJECTION_THRESHOLD:
            self._trip_session(
                BreakerTrigger.POLICY_REJECTIONS,
                f"策略引擎连续拒绝 {self._policy_rejections} 次",
            )

    def _clear_policy_rejections(self) -> None:
        """产出可确认计划 ⇒ 连续拒绝计数归零。"""
        self._policy_rejections = 0

    def _refuse(
        self,
        reason: str,
        utterance: str,
        decision: Decision | None = None,
        plan: Plan | None = None,
        card: PlanCard | None = None,
        *,
        capability_boundary: bool = False,
    ) -> SubmissionView:
        """安全拒绝。

        `capability_boundary=True` = 「**未识别 / 无匹配场景**」：它是**能力边界**而非策略拒绝，
        因此**不计入**触发 4（裁决 #27）——否则用户正常的探索输入（演示时随口一句、口音误识别）
        会被当成攻击信号，连着三次就把系统锁死（这正是 `e2e_check.py` 自我污染的根因）。
        """
        self._state = SessionState.BLOCKED
        self._message = f"安全拒绝：{reason}"
        self._takeover = self._build_takeover(reason, decision, plan, card, utterance)
        if not capability_boundary:
            self._note_policy_rejection()
        return SubmissionView(self._state, self._message, card, decision, plan, ())

    def _enter_takeover(self, reason: str, pending: _Pending) -> None:
        self._state = SessionState.TAKEN_OVER
        self._message = f"转人工接管：{reason}"
        self._takeover = self._build_takeover(
            reason, pending.decision, pending.plan, pending.card, pending.intent_text
        )
        self._note_policy_rejection()

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

    @_serialized
    def confirm(
        self,
        card_id: str,
        *,
        frozen_hash: str,
        verification_code: str | None = None,
        steps: int | None = None,
        now: datetime | None = None,
    ) -> ExecutionView:
        """确认并执行：计划级冻结校验 → 强验证闸门 → 熔断闸门 → 逐节点执行。

        `steps=None` 执行全部待执行节点；`steps=k` 最多执行 k 个（用于中断/恢复演示）。
        红色卡须先经 `request_verification` 取得挑战，再以 `verification_code` 通过闸门。
        """
        pending = self._require_pending()
        if self._state is SessionState.COMPLETED:
            if card_id != pending.card.card_id:
                raise ConfirmationMismatchError("计划卡不匹配")
            assert_frozen(pending.plan, frozen_hash)
            self._require_confirmation(pending)
            return self._execution_view()
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
        prior = self._confirmation
        strong_verified = (
            prior is not None
            and prior.execution_id == self._execution_id
            and prior.frozen_hash == pending.card.frozen_hash
            and prior.strong_verified
        )
        if PROMPT_STRONG_VERIFY in pending.card.requires and not strong_verified:
            self._assert_strong_verification(
                pending, verification_code, now=now if now is not None else datetime.now()
            )
            strong_verified = True
        self._confirmation = ConfirmationRecord(
            plan=pending.plan, frozen_hash=pending.card.frozen_hash,
            execution_id=self._execution_id, approved_level=pending.card.level,
            strong_verified=strong_verified,
        )
        if self._breaker_blocks(pending):
            self._enter_takeover("熔断打开：非纯查询转人工接管", pending)
            return self._execution_view()
        if not self._pending_node_ids:
            self._pending_node_ids = list(pending.node_ids)
        return self._execute(steps=steps)

    @_serialized
    def request_verification(self, *, now: datetime | None = None) -> VerificationPrompt:
        """下发强验证挑战（**仅**红色卡）：一次性 + TTL + 与冻结计划绑定。

        每次调用**轮换**验证码（覆盖上一个未使用的挑战）。
        """
        pending = self._require_pending()
        if self._state is not SessionState.AWAITING_CONFIRM:
            raise SessionStateError("当前状态不允许申请验证码")
        if PROMPT_STRONG_VERIFY not in pending.card.requires:
            raise StrongVerificationRequiredError("当前计划卡无需强验证")
        moment = now if now is not None else datetime.now()
        code = f"{secrets.randbelow(1_000_000):06d}"
        self._challenge = _Challenge(
            code=code,
            card_id=pending.card.card_id,
            frozen_hash=pending.card.frozen_hash,
            expires_at=moment + timedelta(seconds=VERIFICATION_TTL_S),
            execution_id=self._execution_id,
        )
        return VerificationPrompt(
            channel="sms",
            target=_DEMO_VERIFICATION_TARGET,
            code=code,
            expires_in_s=VERIFICATION_TTL_S,
        )

    def _assert_strong_verification(
        self, pending: _Pending, code: str | None, *, now: datetime
    ) -> None:
        """红色闸门：必须提交**服务端下发**的验证码，且未过期、未使用、绑定当前冻结计划。"""
        challenge = self._challenge
        if challenge is None:
            raise StrongVerificationRequiredError(
                "红色（大额 / 不可逆）需强验证：请先申请验证码"
            )
        if (
            challenge.card_id != pending.card.card_id
            or challenge.frozen_hash != pending.card.frozen_hash
            or challenge.execution_id != self._execution_id
        ):
            raise StrongVerificationRequiredError("验证码与当前计划不匹配：请重新申请")
        if now >= challenge.expires_at:
            raise StrongVerificationRequiredError("验证码已过期：请重新申请")
        if not code or not secrets.compare_digest(code, challenge.code):
            raise StrongVerificationRequiredError("验证码不正确")
        self._challenge = None

    def _require_pending(self) -> _Pending:
        if self._pending is None:
            raise SessionStateError("尚无已裁决计划：请先 submit")
        return self._pending

    def _require_confirmation(self, pending: _Pending) -> ConfirmationRecord:
        record = self._confirmation
        if (
            record is None
            or record.execution_id != self._execution_id
            or record.frozen_hash != pending.card.frozen_hash
            or record.approved_level < pending.decision.level
        ):
            raise SessionStateError("执行缺少与当前计划绑定的确认")
        assert_frozen(pending.plan, record.frozen_hash)
        if PROMPT_STRONG_VERIFY in pending.card.requires and not record.strong_verified:
            raise StrongVerificationRequiredError("当前计划尚未完成强验证")
        return record

    def _breaker_blocks(self, pending: _Pending) -> bool:
        """熔断是否阻断本节点计划（裁决 #27）。

        - **纯查询永不受阻**（硬约束：绿色只允许纯查询，且查询始终可用）；
        - 非纯查询在**全局熔断**（系统性信号）或**本会话熔断**（会话级信号）任一打开时转人工接管；
        - 其他会话的会话级熔断**不影响本会话**。
        """
        if plan_is_pure_query(pending.plan):
            return False
        return self._deps.breaker.is_open() or self._session_breaker.is_open()

    def _execute(self, *, steps: int | None) -> ExecutionView:
        pending = self._require_pending()
        confirmation = self._require_confirmation(pending)
        node_map = pending.plan.node_map()
        remaining = [
            node_id
            for node_id in topological_order(pending.plan)
            if node_id not in self._completed_node_ids
        ]
        if steps is not None:
            remaining = remaining[:steps]
        self._state = SessionState.EXECUTING
        for node_id in remaining:
            self._require_confirmation(pending)
            if self._breaker_blocks(pending):
                self._enter_takeover("熔断打开：非纯查询转人工接管", pending)
                return self._execution_view()
            node = node_map[node_id]
            self._assert_dependencies_done(node)
            try:
                call = self._to_call(node)
                result = self._deps.pep.execute(
                    call,
                    pending.decision,
                    pending.plan,
                    f"{self._execution_id}:{pending.card.frozen_hash}:{node_id}",
                    execution=ExecutionContext(
                        execution_id=self._execution_id,
                        frozen_hash=pending.card.frozen_hash,
                        user_id=self._deps.user_id,
                        today=self._deps.today,
                        approved_level=confirmation.approved_level,
                        strong_verified=confirmation.strong_verified,
                        completed_records=tuple(
                            record_id
                            for item in self._exec_results
                            for record_id in item.record_ids
                        ) + tuple(
                            record_id
                            for item in self._unverified_results.values()
                            for record_id in item.record_ids
                        ),
                        flags=pending.flags,
                    ),
                )
            except WritePreconditionError as error:
                self._refresh_authorization(error.result)
                return self._execution_view()
            except CommittedWriteError as error:
                self._unverified_results[node_id] = ExecResult(
                    status="committed_unverified", record_ids=error.receipt.record_ids,
                    message=f"业务已提交，后续核验或审计未完成：{error}",
                )
                self._message = "业务已提交，核验或审计未完成；已暂停后续步骤，可恢复核对回执"
                self._state = SessionState.INTERRUPTED
                return self._execution_view()
            except (NotAuthorizedError, GatewayError) as error:
                self._message = f"执行受阻（{node.action}）：{error}"
                self._state = (
                    SessionState.PARTIAL if self._completed_node_ids else SessionState.FAILED
                )
                return self._execution_view()
            self._unverified_results.pop(node_id, None)
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

    def _refresh_authorization(self, result: PreconditionResult) -> None:
        """事务已回滚；保留已完成步骤，要求满足最新授权后再继续。"""
        pending = self._require_pending()
        decision = strictest(
            pending.decision,
            Decision(Level(result.level), tuple(ReasonCode(code) for code in result.reasons)),
        )
        card = self._render_card(pending.plan, decision)
        pending = replace(
            pending, card=card, decision=decision,
            hit_rules=tuple(reason.value for reason in decision.reasons),
        )
        self._pending = pending
        self._confirmation = None
        self._challenge = None
        self._quota = (
            DailyQuota(result.limit_cents, result.accumulated_cents)
            if _plan_has_outflow(pending.plan) else None
        )
        if result.status is PreconditionStatus.TAKEOVER or decision.level is Level.MANUAL:
            self._enter_takeover(result.message or "当前风险需人工接管", pending)
        elif result.status is PreconditionStatus.DENY or decision.level is Level.DENY:
            self._state = SessionState.BLOCKED
            self._message = result.message or "当前策略拒绝执行，未新增业务写入"
        else:
            self._state = SessionState.AWAITING_CONFIRM
            self._message = result.message or "风险条件已变化，请按新计划卡重新确认"

    def _assert_dependencies_done(self, node: PlanNode) -> None:
        """执行前置：节点声明的依赖必须**已完成**（拓扑序与执行记录一致，fail-closed）。

        正常情况下由 `topological_order` 保证不触发；此处是防御性断言——一旦触发，
        说明执行记录与冻结计划不一致，宁可拒绝也不带病执行。
        """
        done = set(self._completed_node_ids)
        missing = [dep for dep in node.depends_on if dep not in done]
        if missing:
            raise SessionStateError(
                f"计划依赖未满足：{node.node_id} 依赖 {missing} 尚未执行（拒绝执行）"
            )

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

    @_serialized
    def reverse(self) -> ReversalOutcome:
        """回退：对本次执行的每一条回执走补偿事务，结论**一律取自 saga 的如实告知**。

        模拟核心银行执行即结算，故不存在「尚未结算 ⇒ 无扣款」这一档：一律走补偿——
        有资金影响的（流出转账）真正回补，无资金影响的（纯查询）跳过，冲正失败则如实报告。
        """
        self._require_pending()
        if self._confirmation is None and not self._exec_results:
            raise SessionStateError("尚未确认执行：不可回退")
        if self._unverified_results:
            raise SessionStateError("存在已提交但尚未核验的回执，请先恢复核对后再回退")
        self._reversed_records = []
        saga = Saga()
        for index, result in enumerate(self._exec_results, start=1):
            subject = result.record_ids[0] if result.record_ids else result.status
            saga.add(f"{subject}#{index}", _noop_forward(subject), self._compensation(subject))
        saga.run()
        try:
            saga.compensate()
        except CompensationError as error:
            self._state = SessionState.FAILED
            self._message = f"补偿失败（{error}）：已如实告知，未宣称已取消"
            return ReversalOutcome(state=ReversalState.FAILED, message=self._message)
        compensated = tuple(self._reversed_records)
        self._state = SessionState.REVERSED
        self._message = (
            f"已按补偿事务退回 {len(compensated)} 笔（原扣款已回补）"
            if compensated
            else "无可回退的资金动作：本次执行未产生扣款（纯查询 / 未执行）"
        )
        return ReversalOutcome(
            state=ReversalState.REVERSED,
            message=self._message,
            compensated_steps=compensated,
        )

    def takeover_package(self) -> TakeoverPackage:
        """人工接管上下文包：意图 / 计划 / 命中规则 / 审计 ID（PRD §5.8）。"""
        if self._takeover is None:
            raise SessionStateError("当前无人工接管上下文包")
        return self._takeover

    def _compensation(self, subject: str) -> Callable[[], str]:
        """补偿动作：调用**组合根注入**的冲正通道（`AppDeps.reversal`）。

        未装配通道 ⇒ 如实失败；无资金影响的记录（纯查询）跳过并留痕；真正冲正成功则记
        `reversed` 与新增分录标识。**一律不宣称「已发起退回」**。
        """

        def rollback() -> str:
            service = self._deps.reversal
            if service is None:
                self._audit(
                    KIND_REVERSAL,
                    {
                        "subject": subject,
                        "outcome": "compensation_unavailable",
                        "note": "未装配冲正通道，资金未退回",
                    },
                )
                raise CompensationError("未装配冲正通道，资金未退回")
            reversed_records = service.compensate((subject,))
            if not reversed_records:
                self._audit(
                    KIND_REVERSAL,
                    {
                        "subject": subject,
                        "outcome": "nothing_to_reverse",
                        "note": "该记录无资金影响（纯查询等），无需冲正",
                    },
                )
                return subject
            self._reversed_records.extend(reversed_records)
            self._audit(
                KIND_REVERSAL,
                {
                    "subject": subject,
                    "outcome": "reversed",
                    "records": list(reversed_records),
                },
            )
            return reversed_records[0]

        return rollback

    @_serialized
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

    @_serialized
    def resume(self) -> ExecutionView:
        """恢复：继续执行剩余节点（幂等：已完成节点不重复执行）。"""
        if self._state is not SessionState.INTERRUPTED:
            raise SessionStateError(f"当前状态不可恢复：{self._state.value}")
        pending = self._require_pending()
        if self._confirmation is None:
            self._state = SessionState.AWAITING_CONFIRM
            self._message = "计划尚未确认，请先确认当前计划卡"
            return self._execution_view()
        self._require_confirmation(pending)
        return self._execute(steps=None)

    def _execution_view(self) -> ExecutionView:
        frozen_hash = self._pending.card.frozen_hash if self._pending is not None else ""
        results = (*self._exec_results, *self._unverified_results.values())
        bill_advice: tuple[BillAdvice, ...] = ()
        bill_advice_message = ""
        try:
            bill_advice = self._completed_bill_advice()
        except Exception:
            # 只隔离附加建议：存储或计算失败不能掩盖已完成的业务回执。
            bill_advice_message = "消费建议暂不可用，请稍后重新分析账单。"
        return ExecutionView(
            state=self._state,
            message=self._message,
            frozen_hash=frozen_hash,
            exec_results=results,
            record_ids=tuple(
                record_id for result in results for record_id in result.record_ids
            ),
            card=self.card(),
            quota=self._quota,
            decision=self._pending.decision if self._pending is not None else None,
            bill_advice=bill_advice,
            bill_advice_message=bill_advice_message,
        )

    def _completed_bill_advice(self) -> tuple[BillAdvice, ...]:
        if self._state is not SessionState.COMPLETED or self._pending is None:
            return ()
        advice: list[BillAdvice] = []
        for node in self._pending.plan.nodes:
            if node.action == "bills.monthly" and node.node_id in self._completed_node_ids:
                # 参数已由账单执行的白名单校验；只读回用同一账期。
                advice.extend(build_bill_advice(
                    self._deps.gateway, self._deps.user_id,
                    int(str(node.params["year"])), int(str(node.params["month"])),
                    as_of=self._deps.today,
                ))
        return tuple(advice[:3])

    def _reset(self) -> None:
        self._execution_id = secrets.token_hex(16)
        self._state = SessionState.IDLE
        self._message = "会话已重置"
        self._pending = None
        self._confirmation = None
        self._takeover = None
        self._audit_ids = []
        self._exec_results = []
        self._completed_node_ids = []
        self._pending_node_ids = []
        self._challenge = None
        self._unverified_results = {}
        self._quota = None
        self._review_report = None
        self._review_intents = []
        self._clarification_keys = frozenset()


def _with_notes(message: str, notes: tuple[str, ...]) -> str:
    """把未采纳提示并入对外文案（低置信度意图必须让用户看得见，不静默丢弃）。"""
    return message + ("；" + "；".join(notes) if notes else "")


def _review_value(value: object) -> object:
    """审查输入只放可序列化的已采纳值；金额统一为整数分。"""
    if isinstance(value, Money):
        return value.cents
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _dropped_notes(dropped: list[str]) -> tuple[str, ...]:
    if not dropped:
        return ()
    return (f"另有 {len(dropped)} 条意图置信度不足未采纳：{'、'.join(dropped)}",)


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


def _local_intent_slots(intents: tuple[Intent, ...]) -> frozenset[str]:
    """多动作的金额、对象和时间单独续答；账户与服务端身份可共享。"""
    if len(intents) <= 1:
        return frozenset()
    local = {"amount", "amount_cents", "daily_limit", "when", "payee", "payer"}
    for slot in SLOT_PARAMS:
        if slot not in {"account", "user_id"} and sum(
            slot in REQUIRED_SLOTS.get(intent.action, ()) for intent in intents
        ) > 1:
            local.add(slot)
    return frozenset(local)


def _scoped_intent_selected(
    chosen: Mapping[str, object], index: int, local_slots: frozenset[str],
) -> dict[str, object]:
    selected = dict(chosen)
    for slot in local_slots:
        selected.pop(slot, None)
        selected.pop(SLOT_PARAMS[slot], None)
        key = f"intent_{index}_{slot}"
        if key in chosen:
            selected[slot] = chosen[key]
    return selected


def _per_intent_selected(
    action: str, chosen: Mapping[str, object], deps: AppDeps
) -> dict[str, object]:
    """身份始终取服务端；申购另补默认产品，不改共享的选中对象。"""
    selected = dict(chosen)
    selected["user_id"] = deps.user_id
    if action == "wealth.subscribe":
        selected.setdefault("product", _SUBSCRIBE_DEFAULT_PRODUCT)
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


_TRANSFER_ACTIONS = frozenset(
    {"transfer.book", "transfer.create", "transfer.schedule"}
)
_IMMEDIATE_TRANSFER_ACTIONS = frozenset({"transfer.create", "transfer.schedule"})
_BOOKING_TRANSFER_MARKER = re.compile(r"登记|预约")
_TRANSFER_TEXT_MARKER = re.compile(r"转账|转给|汇款|打款")
_IMMEDIATE_TRANSFER_MARKER = re.compile(
    r"(?:立即|马上|立刻|现在\s*(?:就)?)\s*(?:转账|转给|汇款|打款|扣款|执行)"
)
_BOOKING_NO_DEBIT_MARKER = re.compile(r"(?:不要|不用)\s*(?:立即|马上|现在|立刻)?\s*扣款")


def _intent_span(
    intent: Intent,
    utterance: str,
    *,
    multiple: bool = False,
    override: str | None = None,
) -> str | None:
    """取可回指原文的意图片段；多动作没有片段时禁止用整句兜底。

    单动作保留整句兜底是为了兼容旧模型（其只返回 action），但多动作若共享整句，
    多金额解析会拒绝、无法绑定到各节点，因此必须进入澄清闭环。
    """
    candidates = (override, intent.slots.get("span"))
    for span in candidates:
        if isinstance(span, str) and span and span in utterance:
            return span
    return None if multiple else utterance


def _intent_span_overrides(raw: object) -> Mapping[str, object]:
    """读取澄清闭环的动作片段映射；其值仍须经过原文包含校验。"""
    return raw if isinstance(raw, Mapping) else {}


def _span_override_for(
    overrides: Mapping[str, object], action: str, index: int,
) -> str | None:
    raw = overrides.get(action)
    if isinstance(raw, str):
        return raw
    if isinstance(raw, (tuple, list)) and index < len(raw):
        value = raw[index]
        return value if isinstance(value, str) else None
    return None


def _spans_are_unambiguous(utterance: str, spans: tuple[str, ...] | list[str]) -> bool:
    """确认多个片段在原文中按顺序出现且不重叠。"""
    cursor = 0
    for span in spans:
        start = utterance.find(span, cursor)
        if start < 0:
            return False
        cursor = start + len(span)
    return True


_INTENT_CLAUSE_BOUNDARY = re.compile(r"[，,。；;、]|然后|接着|再|并且|同时")


def _intent_semantic_contexts(utterance: str, spans: list[str]) -> tuple[str, ...] | None:
    """从原文恢复完整语义，防止模型裁掉“登记预约”等决定动作性质的前缀。"""
    if len(spans) <= 1:
        # 备注是数据，不参与动作类型选择。
        return (utterance.partition("备注")[0],) if spans else ()
    clauses: list[tuple[int, int]] = []
    start = 0
    for boundary in _INTENT_CLAUSE_BOUNDARY.finditer(utterance):
        clauses.append((start, boundary.start()))
        start = boundary.end()
    clauses.append((start, len(utterance)))
    contexts: list[str] = []
    used_clauses: set[tuple[int, int]] = set()
    cursor = 0
    for span in spans:
        span_start = utterance.find(span, cursor)
        span_end = span_start + len(span)
        matches = [
            (left, right) for left, right in clauses
            if left <= span_start and span_end <= right
        ]
        if len(matches) != 1 or matches[0] in used_clauses:
            return None
        left, right = matches[0]
        used_clauses.add(matches[0])
        contexts.append(utterance[left:right].partition("备注")[0])
        cursor = span_end
    return tuple(contexts)


_BOOKING_PREFIX_CLAUSE = re.compile(r"\s*(?:只登记|登记预约|先预约|不扣款)\s*[:：]?\s*")


def _unassigned_booking_prefix_targets(
    utterance: str, spans: list[str], intents: tuple[Intent, ...],
) -> frozenset[int]:
    """未被任何意图片段覆盖的登记短句，只关联之后第一个转账候选。"""
    if len(intents) <= 1:
        return frozenset()
    ranges: list[tuple[int, int]] = []
    cursor = 0
    for span in spans:
        start = utterance.find(span, cursor)
        cursor = start + len(span)
        ranges.append((start, cursor))
    targets: set[int] = set()
    clause_start = 0
    clause_ends = [match.start() for match in _INTENT_CLAUSE_BOUNDARY.finditer(utterance)]
    boundary_ends = [match.end() for match in _INTENT_CLAUSE_BOUNDARY.finditer(utterance)]
    for clause_end, next_start in zip(
        [*clause_ends, len(utterance)], [*boundary_ends, len(utterance)], strict=True,
    ):
        clause = utterance[clause_start:clause_end]
        covered = any(left < clause_end and right > clause_start for left, right in ranges)
        if not covered and _BOOKING_PREFIX_CLAUSE.fullmatch(clause):
            for index, ((left, _), intent) in enumerate(zip(ranges, intents, strict=True)):
                if left >= clause_end and intent.action in _TRANSFER_ACTIONS:
                    targets.add(index)
                    break
        clause_start = next_start
    return frozenset(targets)


def _has_booking_transfer_marker(utterance: str) -> bool:
    return (
        _BOOKING_TRANSFER_MARKER.search(utterance) is not None
        and _TRANSFER_TEXT_MARKER.search(utterance) is not None
    )


def _has_immediate_transfer_marker(utterance: str) -> bool:
    return _IMMEDIATE_TRANSFER_MARKER.search(
        _BOOKING_NO_DEBIT_MARKER.sub("不扣款", utterance)
    ) is not None


def _multi_intent_span_questions(intents: tuple[Intent, ...]) -> tuple[ClarificationAsk, ...]:
    return tuple(
        ClarificationAsk(
            slot=f"intent_{index}_span",
            prompt=f"请复制第 {index + 1} 项{_intent_label(intent.action)}对应的原话，包含金额。",
        )
        for index, intent in enumerate(intents)
    )


def _booking_semantics_question(index: int, count: int) -> ClarificationAsk:
    return ClarificationAsk(
        slot="action" if count == 1 else f"intent_{index}_action",
        prompt="这句话包含登记/预约转账。请确认是登记未来转账（登记时不扣款），还是立即转账。",
    )


def _transfer_action_choice(raw: object) -> str | None:
    if isinstance(raw, Value):
        if raw.trust is not Trust.TRUSTED:
            return None
        raw = raw.raw
    if not isinstance(raw, str):
        return None
    normalized = raw.strip()
    if normalized in _TRANSFER_ACTIONS:
        return normalized
    if normalized in {"登记", "预约", "登记未来转账", "登记预约转账", "只登记", "不扣款"}:
        return "transfer.book"
    if normalized in {"立即", "立即转账", "马上转账", "现在转账"}:
        return "transfer.create"
    return None


def _intent_label(action: str) -> str:
    return {
        "funds.freeze": "冻结资金",
        "funds.release": "解冻资金",
        "transfer.book": "登记预约转账",
        "transfer.create": "立即转账",
        "transfer.schedule": "定时转账",
    }.get(action, action)


_DATE_PARAMS = frozenset({"booked_on", "start", "end", "order_on", "next_charge_on", "executed_on"})
"""需要从 ISO 文本还原为 `date` 的工具参数（02 目录声明为 `date`，计划节点里是 ISO 字符串）。

`order_on`（`merchant.order` 的订单日期）由**场景层**装配为 ISO 文本，同样需在此还原。"""


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

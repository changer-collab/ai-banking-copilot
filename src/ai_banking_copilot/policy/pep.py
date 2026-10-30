"""策略执行点（PEP）：裁决门 + 参数强校验 + taint 校验 + 执行门 + 审计落链。

硬约束（H2 / H3 / PRD §5.1）：
- **PEP 无裁决不执行**：没有 `Decision` 或级别 ≥ `MANUAL`（L3/L4）一律拒绝。
- **绿色只允许纯查询**：`GREEN` + 非纯查询工具 = 拒绝（防资金 / 状态变更溜进绿色）。
- **参数入口唯一**：`ToolRegistry.validate_params(call)` 是唯一参数门，
  返回**去 taint 包装后的归一值**（`dict[str, object]`）；
  PEP 只消费该返回值、不直接读 `call.params`。控制槽裸值一律被 `ForbiddenParamError` 拒绝（H3）。
- **执行门（双层叠加）**：**计划级**冻结校验由 `policy.gate.assert_frozen(plan, cfhash)` 把关
  （06 在确认记录里同时保存 `Plan` 与 `cfhash`）；**节点级**成员资格校验由本模块
  `_assert_member(call, plan)` 承担——`call` 必须属于**已确认的冻结计划**。确认后控制槽参数
  被改 ⇒ 不再是计划成员 ⇒ `StalePlanError`（防 TOCTOU / 越权节点）。
- **熔断只加严**：熔断打开时锁定非纯查询操作，**纯查询不受影响**。
- **写操作 Pre-Tool Hook**（`policy.tool_hook`）：节点级执行门之后、写处理器派发之前，
  写动作必须携带**服务端签发的一次性授权凭据**（`Pep.issue_write_grant`，仅确认后的
  `Session._execute` 调用）。缺凭据 / 凭据过期 / 参数或计划被改 / 会话不符 ⇒ 拒绝，
  业务写服务**从未被调用**。只读动作不受影响。
- 本模块是**写操作的唯一通道**；`policy/` 之外不得直接调用 `CoreBank`。
"""

from __future__ import annotations

import json
import secrets
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, replace
from datetime import date

from ai_banking_copilot.contracts.authorization import Decision, Level, ReasonCode
from ai_banking_copilot.contracts.plan import (
    AnchorSource,
    IntentRef,
    Plan,
    PlanNode,
    cfhash,
)
from ai_banking_copilot.contracts.taint import Slot, SlotKind, Trust, Value
from ai_banking_copilot.gateway.core_bank import DEFAULT_BOOKING_DATE, CoreBank, WriteReceipt
from ai_banking_copilot.gateway.errors import GatewayError, SandboxError
from ai_banking_copilot.gateway.facts import FactGateway
from ai_banking_copilot.gateway.hashing import canonical_json
from ai_banking_copilot.gateway.preconditions import (
    BeforeWrite,
    PreconditionResult,
    PreconditionStatus,
    WritePreconditionError,
)
from ai_banking_copilot.gateway.sandbox import SandboxMonitor, SandboxMonitorSink
from ai_banking_copilot.gateway.seed import DEMO_USER_ID
from ai_banking_copilot.gateway.transaction_facts import TransactionFacts
from ai_banking_copilot.policy.audit import (
    KIND_BREAKER,
    KIND_DECISION,
    KIND_EXECUTION,
    AuditEvent,
    AuditLog,
)
from ai_banking_copilot.policy.breaker import BreakerTrigger, CircuitBreaker
from ai_banking_copilot.policy.execution import (
    CommittedWriteError,
    ExecutionContext,
    assess_plan,
)
from ai_banking_copilot.policy.gate import StalePlanError
from ai_banking_copilot.policy.tool_hook import (
    GrantLedger,
    HookStatus,
    PreToolHook,
    operation_of,
)
from ai_banking_copilot.policy.verification import (
    StrongVerificationProof,
    StrongVerificationProofVerifier,
)
from ai_banking_copilot.tools.registry import (
    ForbiddenParamError,
    ParamValidationError,
    ToolCall,
    ToolRegistry,
    ToolSpec,
    UnknownActionError,
)

PEP_ANCHOR_ID = "pep-execution-gate"
"""执行门节点的规范锚点 id：让"确认时冻结"与"执行时复核"算出同一哈希。"""

@dataclass(frozen=True)
class WriteContext:
    """写处理器的执行上下文（**刻意最小**：不含审计 / 熔断 / 任何检查入口）。

    `params` 是 `ToolRegistry.validate_params(call)` 的**去 taint 归一值**——PEP 的唯一参数入口。
    """

    core_bank: CoreBank
    params: dict[str, object]
    spec: ToolSpec
    idempotency_key: str
    before_write: BeforeWrite | None = None


WriteHandler = Callable[[WriteContext], WriteReceipt]
"""单个写动作的处理器：**只负责产生该动作的副作用**（返回 `CoreBank` 回执）。

安全边界（核心设计红线）：处理器只拿到产生副作用所需的最小面——
`CoreBank` + 归一化参数 + 动作契约 + 幂等键。计划成员资格（`_assert_member`）、
控制槽零信任（`_assert_taint`）、执行后出处回读（`_verify_receipt`）、收尾与审计
（`_finish`）**全部留在 `Pep` 的包装流程里**，处理器没有机会绕过任何检查。

2026-10-08（`docs/已修复清单-2026-10-08.md` 步骤 1.5）：由**各场景模块自带**、组合根汇入，
取代原先 `policy/pep.py` 内逐动作的 `if/elif` 代码分支。
"""


@dataclass(frozen=True)
class QueryContext:
    """读处理器的执行上下文（与 `WriteContext` 对称，**刻意最小**）。

    `params` 是 `ToolRegistry.validate_params(call)` 的**去 taint 归一值**——PEP 的唯一参数入口。
    只给**只读网关**，读处理器拿不到 `CoreBank`，结构上不可能产生副作用。
    """

    gateway: FactGateway
    params: dict[str, object]
    spec: ToolSpec
    sandbox_monitor: SandboxMonitorSink | None = None
    analysis_code_generator: Callable[[str, tuple[str, ...]], str] | None = None


@dataclass(frozen=True)
class QueryResult:
    """一次只读查询的产出（数值与实体**只能来自网关**，H1）。

    `record_ids` / `provenance` 是出处凭证；`detail` 是展示用明细，只做拼接、不生成事实。
    """

    record_ids: tuple[str, ...]
    provenance: tuple[tuple[str, str], ...]
    detail: str
    status: str = "queried"
    sandbox_events: tuple[Mapping[str, object], ...] = ()


QueryHandler = Callable[[QueryContext], QueryResult]
"""单个纯查询动作的处理器：**只负责回读事实**（不写、不裁决、不审计）。

与 `WriteHandler` 同一条接缝纪律：计划成员资格、控制槽零信任、收尾与审计全部留在
`Pep` 的包装流程里。

2026-10-08（`docs/已修复清单-2026-10-08.md` 步骤 4「残留」补做）：读分派同样由**各场景模块自带**，
取代 `_execute_query` 内逐动作的 `if/elif`——此后新增**任一**动作都不再需要改 `policy/pep.py`。
"""


class NotAuthorizedError(RuntimeError):
    """无裁决、级别不足、未登记处理器、熔断锁定——一律拒绝执行。"""


@dataclass(frozen=True)
class ExecResult:
    """一次执行的结果（`message` 内含审计 ID，对齐 §9.1 的可机读 schema）。

    `external_status` / `previous_status` 由回执透传（G-04）：只描述**外部系统**是否真的执行，
    本地库变更不得被表述为"商户已取消"。
    """

    status: str
    record_ids: tuple[str, ...]
    message: str
    external_status: str = ""
    previous_status: str | None = None
    action: str = ""
    """本次执行对应的工具动作（如 `subscription.cancel`），供渠道层区分动作语义。"""
    effect_ids: tuple[str, ...] = ()
    """回执的副作用 ID；重复执行（如重复取消）必须为空，渠道层据此证明无新副作用。"""


def manifest_node(call: ToolCall) -> PlanNode:
    """把工具调用规范化成执行门节点（控制槽参数进哈希）。"""
    return PlanNode(
        node_id="pep",
        action=call.action,
        params=_canonical_params(call.params),
        intent=IntentRef(source=AnchorSource.USER_APPROVED, anchor_id=PEP_ANCHOR_ID),
    )


def manifest_hash(call: ToolCall) -> str:
    """节点级执行门哈希：确认时冻结、执行时复核（防 TOCTOU）。"""
    return cfhash((manifest_node(call),))


def manifest_plan(call: ToolCall) -> Plan:
    """单节点冻结计划：供只执行一次调用的最小调用方与单测构造。

    节点参数取**原始值形态**（剥掉 taint 包装），与真实计划节点（`PlanNode.params` 存原始值）
    保持同一形态——否则成员资格比对会因包装差异而恒不相等。
    """
    return Plan(
        plan_id="pep-execution",
        nodes=(replace(manifest_node(call), params=_plain_params(call.params)),),
    )


def _plain_params(params: dict[str, object]) -> dict[str, object]:
    """剥掉 `Value` / `Slot` 包装，还原成计划节点存储的原始值形态。"""
    return {key: _plain(value) for key, value in params.items()}


def _plain(value: object) -> object:
    if isinstance(value, Slot):
        return value.value.raw
    if isinstance(value, Value):
        return value.raw
    return value


_CALLER_INJECTED_PARAMS = ("user_id",)
"""由受信任调用方在 `ToolCall` 层注入、且按设计**不进** `PlanNode.params` 的参数。

见 README 裁决 #16 / #20：会话层与评测链在 `ToolCall` 层补 `DEMO_USER_ID`，不改计划哈希。
成员资格比对须剔除它，否则所有写动作都会被误判为「不属于计划」。
"""


def _comparable_params(params: dict[str, object]) -> dict[str, object]:
    """成员资格比对视图：剔除调用方注入项后规范化（键序无关、日期归一）。"""
    canonical = _canonical_params(params)
    return {key: value for key, value in canonical.items() if key not in _CALLER_INJECTED_PARAMS}


def _member_key(action: str, params: dict[str, object]) -> str:
    """计划成员指纹：`动作 + 归一化参数`（调用方注入项不参与）。"""
    return canonical_json({"action": action, "params": _comparable_params(params)})


class Pep:
    """工具白名单执行器：裁决 → 校验 → 执行门 → 执行 → 出处 → 审计。"""

    def __init__(
        self,
        registry: ToolRegistry,
        gateway: FactGateway,
        core_bank: CoreBank,
        audit: AuditLog,
        breaker: CircuitBreaker,
        write_handlers: Mapping[str, WriteHandler],
        query_handlers: Mapping[str, QueryHandler],
        *,
        strong_verification: StrongVerificationProofVerifier | None = None,
        analysis_code_generator: Callable[[str, tuple[str, ...]], str] | None = None,
    ) -> None:
        self._registry = registry
        self._gateway = gateway
        self._core_bank = core_bank
        self._audit = audit
        self._breaker = breaker
        self._write_handlers = dict(write_handlers)
        self._query_handlers = dict(query_handlers)
        self._strong_verification = strong_verification
        self._analysis_code_generator = analysis_code_generator
        self._hook = PreToolHook(
            ledger=GrantLedger(),
            audit=audit,
            breaker=breaker,
            failure_recorder=self._record_breaker_failure,
        )

    def _record_breaker_failure(
        self,
        action: str,
        reason: str,
        user_id: str = "",
        session_id: str = "",
        execution_id: str = "",
    ) -> None:
        trigger = BreakerTrigger.CONSECUTIVE_AUTH_FAILURES
        was_open = self._breaker.is_open()
        self._breaker.record_failure()
        if was_open or not self._breaker.is_open():
            return
        try:
            self._audit.append(
                AuditEvent(
                    event_id=f"breaker:pep:{secrets.token_hex(8)}",
                    kind=KIND_BREAKER,
                    payload={
                        "trigger": trigger.value,
                        "detail": "PEP 失败计数达到全局熔断阈值",
                        "scope": "global",
                        "state": self._breaker.state().value,
                        "action": action,
                        "reject_reason": reason,
                        "user_id": user_id if isinstance(user_id, str) else "",
                        "session_id": session_id if isinstance(session_id, str) else "",
                        "execution_id": execution_id if isinstance(execution_id, str) else "",
                    },
                    prev_hash="",
                )
            )
        except Exception:
            raise NotAuthorizedError("熔断事件审计不可用，拒绝执行") from None

    def handled_actions(self) -> frozenset[str]:
        """PEP 实际**持有处理器**的动作集 = 已登记读处理器 ∪ 已登记写处理器。

        取代原先手写的 `HANDLED_ACTIONS` 常量（`docs/已修复清单-2026-10-08.md` 步骤 1.5）；
        2026-10-08 步骤 4「残留」补做后，读侧也由注册表派生，本方法改为**处理器键的并集**
        （此前是「注册表里的纯查询动作 ∪ 写处理器」，会把「有动作但没读处理器」谎报为可执行）。

        一致性断言见 `tests/policy/test_pep.py`（本测试自建注册表 ⊆ 处理器集）与
        `tests/scenarios/test_registry.py`（生产侧：处理器集 == 注册表动作集，双向）。
        """
        return frozenset(set(self._query_handlers) | set(self._write_handlers))

    def execute(
        self,
        call: ToolCall,
        decision: Decision,
        plan: Plan,
        idempotency_key: str,
        *,
        execution: ExecutionContext | None = None,
        user_id: str = "",
        session_id: str = "",
        execution_id: str = "",
    ) -> ExecResult:
        """执行一次已裁决的工具调用。任一前置条件不满足即 fail-closed。

        `plan` = **已确认的冻结计划**：节点级执行门据此校验「本次调用属于该计划」，
        与调用方在确认阶段做的计划级 `assert_frozen` 叠加（防 TOCTOU / 越权节点）。
        """
        audit_identity = {
            "user_id": execution.user_id if execution is not None else user_id,
            "session_id": execution.session_id if execution is not None else session_id,
            "execution_id": execution.execution_id if execution is not None else execution_id,
        }
        if not isinstance(decision, Decision):
            rejection = NotAuthorizedError("PEP 无裁决不执行：缺少 Decision")
            self._record_rejection(
                call, None, plan, execution, "missing_decision", rejection,
                **audit_identity,
            )
            raise rejection
        if decision.level >= Level.MANUAL:
            self._record_breaker_failure(
                call.action, "decision_requires_manual", **audit_identity,
            )
            rejection = NotAuthorizedError(f"裁决级别不足，不允许执行：{decision.level.name}")
            self._record_rejection(
                call, decision, plan, execution, "decision_requires_manual", rejection,
                **audit_identity,
            )
            raise rejection
        try:
            spec = self._registry.get(call.action)
        except UnknownActionError as unknown_error:
            self._record_rejection(
                call, decision, plan, execution, "unknown_action", unknown_error,
                **audit_identity,
            )
            raise
        if decision.level is Level.GREEN and not spec.is_pure_query:
            self._record_breaker_failure(call.action, "green_write_forbidden", **audit_identity)
            rejection = NotAuthorizedError("绿色只允许纯查询：拒绝执行资金 / 状态变更")
            self._record_rejection(
                call, decision, plan, execution, "green_write_forbidden", rejection,
                **audit_identity,
            )
            raise rejection
        if self._breaker.is_open() and not spec.is_pure_query:
            rejection = NotAuthorizedError("熔断打开：高风险操作被临时锁定（纯查询不受影响）")
            self._record_rejection(
                call, decision, plan, execution, "breaker_open", rejection, **audit_identity,
            )
            raise rejection
        try:
            self._assert_taint(spec, call, **audit_identity)
        except ForbiddenParamError as taint_error:
            self._record_rejection(
                call, decision, plan, execution, "untrusted_control_value", taint_error,
                **audit_identity,
            )
            raise
        try:
            resolved = self._registry.validate_params(call)
        except ForbiddenParamError as taint_error:
            self._record_breaker_failure(
                call.action, "untrusted_control_value", **audit_identity,
            )
            self._record_rejection(
                call, decision, plan, execution, "untrusted_control_value", taint_error,
                **audit_identity,
            )
            raise
        except ParamValidationError as parameter_error:
            self._record_breaker_failure(call.action, "invalid_parameters", **audit_identity)
            self._record_rejection(
                call, decision, plan, execution, "invalid_parameters", parameter_error,
                **audit_identity,
            )
            raise
        try:
            self._assert_member(call.action, resolved, plan, **audit_identity)
        except StalePlanError as member_error:
            reason = (
                "missing_confirmed_plan"
                if not isinstance(plan, Plan) or not plan.nodes
                else "plan_member_mismatch"
            )
            self._record_rejection(
                call, decision, plan, execution, reason, member_error, **audit_identity,
            )
            raise
        frozen_hash = manifest_hash(call)
        if spec.is_pure_query:
            try:
                return self._execute_query(
                    call, spec, resolved, decision, frozen_hash, idempotency_key,
                    _plan_hash(plan),
                    **audit_identity,
                )
            except NotAuthorizedError as query_error:
                self._record_rejection(
                    call, decision, plan, execution, "query_handler_unavailable", query_error,
                    **audit_identity,
                )
                raise
        return self._execute_write(
            call, resolved, spec, decision, frozen_hash, idempotency_key,
            plan, execution, **audit_identity,
        )

    def issue_write_grant(
        self,
        call: ToolCall,
        decision: Decision,
        plan: Plan,
        *,
        user_id: str = DEMO_USER_ID,
        session_id: str = "",
        execution_id: str = "",
        approved_level: Level | None = None,
        verification_proof: StrongVerificationProof | None = None,
        card_id: str = "",
        strong_verified: bool = False,
        today: date = DEFAULT_BOOKING_DATE,
        completed_records: tuple[str, ...] = (),
        flags: frozenset[str] = frozenset(),
    ) -> ExecutionContext:
        """在**用户确认之后**签发一次性写授权凭据，返回可供 `execute` 使用的执行上下文。

        唯一合法调用方是服务端确认链（`Session._execute` 在 `_require_confirmation` 之后
        逐节点签发）。模型输出、前端请求体与 IM 消息都构造不出 `WriteGrant`，因此
        无法伪造「已确认 / 已认证 / 已通过强验证」。只读动作不签发凭据。
        """
        if not isinstance(decision, Decision):
            raise NotAuthorizedError("PEP 无裁决不发凭据：缺少 Decision")
        if decision.level >= Level.MANUAL:
            raise NotAuthorizedError(f"裁决级别不足，不签发写授权：{decision.level.name}")
        spec = self._registry.get(call.action)
        if spec.is_pure_query:
            raise NotAuthorizedError(f"只读动作不需要写授权凭据：{call.action}")
        bound_execution_id = execution_id or f"grant:{secrets.token_hex(8)}"
        self._assert_taint(
            spec,
            call,
            user_id=user_id,
            session_id=session_id,
            execution_id=bound_execution_id,
        )
        resolved = self._registry.validate_params(call)
        self._assert_member(
            call.action,
            resolved,
            plan,
            user_id=user_id,
            session_id=session_id,
            execution_id=bound_execution_id,
        )
        bound_level = decision.level if approved_level is None else approved_level
        if bound_level > decision.level:
            raise NotAuthorizedError("确认级别不能高于当前授权裁决")
        plan_hash = _plan_hash(plan)
        verification_is_valid = bool(
            verification_proof is not None
            and self._strong_verification is not None
            and self._strong_verification.verify(
                verification_proof,
                user_id=user_id,
                session_id=session_id,
                execution_id=bound_execution_id,
                card_id=card_id,
                plan_hash=plan_hash,
            )
        )
        if bound_level >= Level.RED and not verification_is_valid:
            raise NotAuthorizedError("红色写授权必须具有绑定当前计划的强验证证明")
        # `strong_verified` remains accepted for source compatibility, but a
        # caller-provided boolean never constitutes verification evidence.
        del strong_verified
        grant = self._hook.ledger.issue(
            session_id=session_id,
            execution_id=bound_execution_id,
            user_id=user_id,
            plan_id=plan.plan_id,
            plan_hash=plan_hash,
            action=call.action,
            member=_member_key(call.action, resolved),
            approved_level=bound_level,
            strong_verified=verification_is_valid,
            verification_proof=verification_proof if verification_is_valid else None,
            today=today,
            completed_records=tuple(completed_records),
            flags=frozenset(flags),
        )
        return ExecutionContext(
            execution_id=grant.execution_id,
            frozen_hash=grant.plan_hash,
            user_id=grant.user_id,
            today=grant.today,
            approved_level=grant.approved_level,
            strong_verified=grant.strong_verified,
            completed_records=grant.completed_records,
            flags=grant.flags,
            session_id=grant.session_id,
            grant_id=grant.grant_id,
        )

    def _assert_member(
        self,
        action: str,
        resolved: dict[str, object],
        plan: Plan,
        *,
        user_id: str = "",
        session_id: str = "",
        execution_id: str = "",
    ) -> None:
        """执行门（**节点级**成员资格）：本次调用必须属于**已确认的冻结计划**。

        判据：计划中存在节点与本次调用的 `action` 相同，且**归一化参数逐字相等**。
        确认后改动金额 / 收款人 / 账户 ⇒ 不再是计划成员 ⇒ `StalePlanError`（fail-closed，
        并计入熔断失败）。

        **唯一豁免**：受信任调用方在 `ToolCall` 层注入的 `user_id`（README 裁决 #16 / #20）
        不参与比对——它来自 `AppDeps.user_id`、不是用户输入，且按设计不进 `PlanNode.params`。
        """
        if not isinstance(plan, Plan) or not plan.nodes:
            self._record_breaker_failure(
                action, "missing_confirmed_plan", user_id, session_id, execution_id,
            )
            raise StalePlanError("执行门拒绝：缺少已确认的冻结计划")
        probe = _member_key(action, resolved)
        if any(_member_key(node.action, node.params) == probe for node in plan.nodes):
            return
        self._record_breaker_failure(
            action, "plan_member_mismatch", user_id, session_id, execution_id,
        )
        raise StalePlanError(f"执行门拒绝：{action} 不属于已确认计划（节点成员资格校验失败）")

    def _execute_query(
        self,
        call: ToolCall,
        spec: ToolSpec,
        params: dict[str, object],
        decision: Decision,
        frozen_hash: str,
        idempotency_key: str,
        plan_hash: str,
        *,
        user_id: str,
        session_id: str,
        execution_id: str,
    ) -> ExecResult:
        """读路径：按动作查 **读 handler 注册表**并执行（handler 由场景模块自带、组合根汇入）。

        未登记 ⇒ `NotAuthorizedError`（fail-closed，并计入熔断失败）。读处理器只拿到
        **只读网关** + 归一化参数 + 动作契约，拿不到 `CoreBank`，结构上不可能产生副作用。
        """
        handler = self._query_handlers.get(call.action)
        if handler is None:
            self._record_breaker_failure(
                call.action, "query_handler_unavailable", user_id, session_id, execution_id,
            )
            raise NotAuthorizedError(f"未登记查询处理器：{call.action}")

        def persist_sandbox_monitor(monitor: SandboxMonitor) -> None:
            monitor_payload = asdict(monitor)
            try:
                self._audit.append(
                    AuditEvent(
                        event_id=(
                            f"sandbox:{call.action}:{idempotency_key}:"
                            f"{secrets.token_hex(8)}"
                        ),
                        kind=KIND_EXECUTION,
                        payload={
                            "event_type": "sandbox_execution",
                            "phase": "query_sandbox",
                            "action": call.action,
                            "status": monitor.status,
                            "frozen_hash": frozen_hash,
                            "plan_hash": plan_hash,
                            "idempotency_key": idempotency_key,
                            "user_id": user_id,
                            "session_id": session_id,
                            "execution_id": execution_id,
                            "sandbox_monitor": monitor_payload,
                        },
                        prev_hash="",
                    )
                )
            except Exception:
                raise NotAuthorizedError(
                    "隔离执行审计不可用，拒绝使用沙箱结果"
                ) from None

        sandbox_events: tuple[Mapping[str, object], ...] = ()
        try:
            result = handler(
                QueryContext(
                    gateway=self._gateway,
                    params=params,
                    spec=spec,
                    sandbox_monitor=persist_sandbox_monitor,
                    analysis_code_generator=self._analysis_code_generator,
                )
            )
        except SandboxError as sandbox_error:
            # A failed analysis must never turn into generated or partial facts.
            # Return a stable, audited refusal so the channel does not emit a 500.
            monitor = getattr(sandbox_error, "monitor", None)
            if isinstance(monitor, Mapping):
                sandbox_events = (monitor,)
            result = QueryResult(
                record_ids=(),
                provenance=(),
                detail="查询安全沙箱暂不可用，未返回查询结果",
                status="query_refused",
                sandbox_events=sandbox_events,
            )
        return self._finish(
            call,
            decision,
            frozen_hash,
            idempotency_key,
            status=result.status,
            record_ids=result.record_ids,
            provenance=result.provenance,
            detail=result.detail,
            plan_hash=plan_hash,
            user_id=user_id,
            session_id=session_id,
            execution_id=execution_id,
            sandbox_events=result.sandbox_events,
        )

    def _execute_write(
        self,
        call: ToolCall,
        params: dict[str, object],
        spec: ToolSpec,
        decision: Decision,
        frozen_hash: str,
        idempotency_key: str,
        plan: Plan,
        execution: ExecutionContext | None,
        *,
        user_id: str = "",
        session_id: str = "",
        execution_id: str = "",
    ) -> ExecResult:
        """写路径：**Pre-Tool Hook** → 幂等执行 → 执行后出处校验 → 熔断联动 → 审计落链。

        Hook 在**业务写服务之前**执行：缺凭据 / 凭据过期 / 参数或计划被改 / 用户或会话不符
        一律拒绝，写处理器**从未被调用**。凭据只能由 `issue_write_grant` 在确认后签发。
        """
        plan_hash = _plan_hash(plan)
        if execution is None:
            raise self._hook.block(
                spec,
                status=HookStatus.CONFIRMATION_REQUIRED,
                reason="写操作缺少服务端签发的确认凭据，禁止直接执行",
                plan_id=plan.plan_id,
                plan_hash=plan_hash,
                user_id=user_id,
                session_id=session_id,
                execution_id=execution_id,
            )
        grant = self._hook.authorize(
            spec,
            member=_member_key(call.action, params),
            plan_id=plan.plan_id,
            plan_hash=plan_hash,
            approved_level=decision.level,
            grant_id=execution.grant_id,
            user_id=execution.user_id,
            session_id=execution.session_id,
            execution_id=execution.execution_id,
        )
        if grant is None:
            self._record_breaker_failure(
                call.action,
                "write_grant_missing",
                execution.user_id,
                execution.session_id,
                execution.execution_id,
            )
            rejection = NotAuthorizedError("写操作授权凭据缺失")
            self._record_rejection(
                call, decision, plan, execution, "write_grant_missing", rejection,
            )
            raise rejection
        if (not execution.execution_id or execution.frozen_hash != plan_hash
                or ("user_id" in params and execution.user_id != params["user_id"])):
            self._record_breaker_failure(
                call.action,
                "execution_context_mismatch",
                execution.user_id,
                execution.session_id,
                execution.execution_id,
            )
            stale_rejection = StalePlanError("执行上下文与冻结计划或用户不匹配")
            self._record_rejection(
                call, decision, plan, execution, "execution_context_mismatch", stale_rejection,
            )
            raise stale_rejection

        # Authorization state comes only from the server-issued grant.  The
        # ExecutionContext is request/session data and may be forged or
        # mutated after confirmation.
        active_execution = ExecutionContext(
            execution_id=grant.execution_id,
            frozen_hash=grant.plan_hash,
            user_id=grant.user_id,
            today=grant.today,
            approved_level=grant.approved_level,
            strong_verified=grant.strong_verified,
            completed_records=grant.completed_records,
            flags=grant.flags,
            session_id=grant.session_id,
            grant_id=grant.grant_id,
        )
        self._record_write_start(call, decision, plan, active_execution, idempotency_key)

        def before_write(conn: sqlite3.Connection) -> PreconditionResult:
            facts = TransactionFacts(conn)
            if facts.global_breaker_open() or self._breaker.is_open():
                return PreconditionResult(
                    PreconditionStatus.TAKEOVER, level=int(Level.MANUAL),
                    reasons=(ReasonCode.MANUAL_REVIEW.value,),
                    message="熔断打开，转人工接管",
                )
            risk = assess_plan(
                plan, registry=self._registry, gateway=facts,
                user_id=active_execution.user_id, today=active_execution.today,
                flags=active_execution.flags,
                completed_records=active_execution.completed_records,
                floor=decision,
            )
            level = risk.decision.level
            status = PreconditionStatus.ALLOW
            if level is Level.DENY:
                status = PreconditionStatus.DENY
            elif level is Level.MANUAL:
                status = PreconditionStatus.TAKEOVER
            elif level > active_execution.approved_level or (
                level >= Level.RED and not active_execution.strong_verified
            ):
                status = PreconditionStatus.REAUTHORIZE
            return PreconditionResult(
                status, level=int(level),
                reasons=tuple(reason.value for reason in risk.decision.reasons),
                limit_cents=risk.limit_cents,
                accumulated_cents=risk.accumulated_cents,
                message="最新风险要求重新授权" if status is PreconditionStatus.REAUTHORIZE else "",
            )

        try:
            authorized_params = {**params, "user_id": grant.user_id}
            receipt = self._dispatch_write(
                call, authorized_params, spec, idempotency_key, before_write,
            )
        except NotAuthorizedError as error:
            self._record_breaker_failure(
                call.action,
                "write_handler_rejected",
                active_execution.user_id,
                active_execution.session_id,
                active_execution.execution_id,
            )
            self._record_rejection(
                call, decision, plan, active_execution, "write_handler_rejected", error,
            )
            raise
        except WritePreconditionError as error:
            self._record_breaker_failure(
                call.action,
                f"write_precondition_{error.result.status.value}",
                active_execution.user_id,
                active_execution.session_id,
                active_execution.execution_id,
            )
            self._record_rejection(
                call,
                decision,
                plan,
                active_execution,
                f"write_precondition_{error.result.status.value}",
                error,
            )
            raise
        except GatewayError as error:
            self._record_breaker_failure(
                call.action,
                "gateway_write_failed",
                active_execution.user_id,
                active_execution.session_id,
                active_execution.execution_id,
            )
            self._finish(
                call,
                decision,
                frozen_hash,
                idempotency_key,
                status="failed",
                record_ids=(),
                provenance=(),
                detail=str(error),
                op_type=operation_of(spec),
                plan_id=plan.plan_id,
                grant_id=grant.grant_id,
                plan_hash=grant.plan_hash,
                user_id=grant.user_id,
                session_id=grant.session_id,
                execution_id=grant.execution_id,
            )
            raise
        try:
            provenance = self._verify_receipt(receipt)
            self._breaker.record_success()
            suffix = "（幂等回放）" if receipt.replayed else ""
            return self._finish(
                call,
                decision,
                frozen_hash,
                idempotency_key,
                status=receipt.status,
                record_ids=receipt.record_ids,
                provenance=provenance,
                detail=f"{receipt.action}{suffix}",
                external_status=receipt.external_status,
                previous_status=receipt.previous_status,
                effect_ids=receipt.effect_ids,
                op_type=operation_of(spec),
                plan_id=plan.plan_id,
                grant_id=grant.grant_id,
                plan_hash=grant.plan_hash,
                user_id=grant.user_id,
                session_id=grant.session_id,
                execution_id=grant.execution_id,
            )
        except Exception as error:
            raise CommittedWriteError(receipt, error) from error

    def _dispatch_write(
        self,
        call: ToolCall,
        params: dict[str, object],
        spec: ToolSpec,
        idempotency_key: str,
        before_write: BeforeWrite,
    ) -> WriteReceipt:
        """按动作查 **handler 注册表**并执行（handler 由场景模块自带、组合根汇入）。

        未登记 ⇒ `NotAuthorizedError`（fail-closed，由 `_execute_write` 计入熔断失败）。
        处理器只在**这里**被调用：它拿到的是已通过全部前置检查的归一化参数与 `CoreBank`，
        不接触审计 / 熔断 / 执行门，故无法绕过 PEP 的包装流程。
        """
        handler = self._write_handlers.get(call.action)
        if handler is None:
            raise NotAuthorizedError(f"未登记执行处理器：{call.action}")
        return handler(
            WriteContext(
                core_bank=self._core_bank,
                params=params,
                spec=spec,
                idempotency_key=idempotency_key,
                before_write=before_write,
            )
        )

    def _verify_receipt(self, receipt: WriteReceipt) -> tuple[tuple[str, str], ...]:
        """执行后出处校验：回读回执记录并用 `FactGateway.verify` 比对哈希（H1）。"""
        results: list[tuple[str, str]] = []
        for record_id in receipt.record_ids:
            content_hash = _read_hash(self._gateway, record_id)
            if not self._gateway.verify(record_id, content_hash):
                raise GatewayError(f"执行后出处校验失败：{record_id}")
            results.append((record_id, content_hash))
        return tuple(results)

    def _assert_taint(
        self,
        spec: ToolSpec,
        call: ToolCall,
        *,
        user_id: str = "",
        session_id: str = "",
        execution_id: str = "",
    ) -> None:
        """控制槽拒绝 `unverified` 值：PEP 侧再兜一道 schema 防线（H3）。"""
        for param in spec.params:
            if param.slot is not SlotKind.CONTROL:
                continue
            value = call.params.get(param.name)
            if isinstance(value, Value) and value.trust is Trust.UNVERIFIED:
                self._record_breaker_failure(
                    call.action,
                    "untrusted_control_value",
                    user_id,
                    session_id,
                    execution_id,
                )
                raise ForbiddenParamError(f"控制槽 {param.name} 拒绝 unverified 值")

    def _record_rejection(
        self,
        call: ToolCall,
        decision: Decision | None,
        plan: Plan | None,
        execution: ExecutionContext | None,
        reason: str,
        rejection: Exception,
        *,
        user_id: str = "",
        session_id: str = "",
        execution_id: str = "",
    ) -> None:
        """记录 PEP 前置拒绝；只落稳定码和关联身份，不落异常文本或参数。"""
        plan_id = plan.plan_id if isinstance(plan, Plan) else ""
        plan_hash = ""
        if isinstance(plan, Plan):
            try:
                plan_hash = _plan_hash(plan)
            except Exception:
                # A malformed plan is still a rejection worth recording.
                pass

        def identity(value: object) -> str:
            return value if isinstance(value, str) else ""

        execution_id = identity(execution.execution_id if execution is not None else execution_id)
        session_id = identity(execution.session_id if execution is not None else session_id)
        user_id = identity(execution.user_id if execution is not None else user_id)
        grant_id = identity(execution.grant_id) if execution is not None else ""
        execution_hash = identity(execution.frozen_hash) if execution is not None else ""
        payload: dict[str, object] = {
            "event_type": "pep_rejection",
            "phase": "pep_execute",
            "status": "rejected",
            "reject_reason": reason,
            "action": identity(call.action),
            "level": decision.level.name if decision is not None else "",
            "reasons": (
                [item.value for item in decision.reasons] if decision is not None else []
            ),
            "plan_id": plan_id,
            "plan_hash": plan_hash,
            "frozen_hash": execution_hash or plan_hash,
            "execution_id": execution_id,
            "session_id": session_id,
            "user_id": user_id,
            "grant_id": grant_id,
        }
        try:
            self._audit.append(
                AuditEvent(
                    event_id=f"pep-reject:{identity(call.action)}:{secrets.token_hex(8)}",
                    kind=KIND_DECISION,
                    payload=payload,
                    prev_hash="",
                )
            )
        except Exception:
            # The request remains rejected even if its audit write fails. Keep the
            # original rejection type so callers cannot mistake a logging error
            # for a different authorization outcome.
            rejection.add_note("PEP 拒绝事件未能写入审计链；请求仍被拒绝。")

    def _record_write_start(
        self,
        call: ToolCall,
        decision: Decision,
        plan: Plan,
        execution: ExecutionContext,
        idempotency_key: str,
    ) -> None:
        """在写 handler 前持久记录已验证授权，保证审计不可用时没有副作用。"""
        try:
            self._audit.append(
                AuditEvent(
                    event_id=f"write-start:{secrets.token_hex(8)}",
                    kind=KIND_DECISION,
                    payload={
                        "event_type": "write_execution_started",
                        "phase": "pre_dispatch",
                        "action": call.action,
                        "status": "started",
                        "level": decision.level.name,
                        "reasons": [reason.value for reason in decision.reasons],
                        "approved_level": execution.approved_level.name,
                        "strong_verified": execution.strong_verified,
                        "plan_id": plan.plan_id,
                        "plan_hash": execution.frozen_hash,
                        "frozen_hash": manifest_hash(call),
                        "idempotency_key": idempotency_key,
                        "execution_id": execution.execution_id,
                        "session_id": execution.session_id,
                        "user_id": execution.user_id,
                        "grant_id": execution.grant_id,
                    },
                    prev_hash="",
                )
            )
        except Exception:
            raise NotAuthorizedError("写操作审计不可用，拒绝执行") from None

    def _finish(
        self,
        call: ToolCall,
        decision: Decision,
        frozen_hash: str,
        idempotency_key: str,
        *,
        status: str,
        record_ids: tuple[str, ...],
        provenance: tuple[tuple[str, str], ...],
        detail: str,
        external_status: str = "",
        previous_status: str | None = None,
        effect_ids: tuple[str, ...] = (),
        op_type: str = "",
        plan_id: str = "",
        grant_id: str = "",
        plan_hash: str = "",
        user_id: str = "",
        session_id: str = "",
        execution_id: str = "",
        sandbox_events: tuple[Mapping[str, object], ...] = (),
    ) -> ExecResult:
        payload: dict[str, object] = {
            "action": call.action,
            "level": decision.level.name,
            "reasons": [reason.value for reason in decision.reasons],
            "frozen_hash": frozen_hash,
            "plan_hash": plan_hash,
            "idempotency_key": idempotency_key,
            "status": status,
            "record_ids": list(record_ids),
            "provenance": [
                {"record_id": record_id, "content_hash": content_hash}
                for record_id, content_hash in provenance
            ],
            "detail": detail,
            "external_status": external_status,
            "previous_status": previous_status,
            "op_type": op_type,
            "plan_id": plan_id,
            "grant_id": grant_id,
            "user_id": user_id,
            "session_id": session_id,
            "execution_id": execution_id,
        }
        if sandbox_events:
            payload["sandbox_events"] = [dict(event) for event in sandbox_events]
        event_hash = self._audit.append(
            AuditEvent(
                event_id=f"{call.action}:{idempotency_key}",
                kind=KIND_EXECUTION,
                payload=payload,
                prev_hash="",
            )
        )
        return ExecResult(
            status=status,
            record_ids=record_ids,
            message=f"{detail}｜审计ID {event_hash[:16]}",
            external_status=external_status,
            previous_status=previous_status,
            action=call.action,
            effect_ids=effect_ids,
        )


def _canonical_params(params: dict[str, object]) -> dict[str, object]:
    """把参数规范成 JSON 安全形态：保证 `cfhash` 可确定性序列化。"""
    parsed = json.loads(canonical_json(params))
    if not isinstance(parsed, dict):
        raise NotAuthorizedError("工具参数必须是 JSON 对象")
    return {str(key): value for key, value in parsed.items()}


def _plan_hash(plan: Plan) -> str:
    nodes = tuple(replace(node, params=_canonical_params(node.params)) for node in plan.nodes)
    return cfhash(nodes)


def require_str(params: dict[str, object], key: str) -> str:
    """取非空字符串参数；不满足即 `NotAuthorizedError`（fail-closed）。**写处理器公用**。"""
    value = params.get(key)
    if not isinstance(value, str) or not value:
        raise NotAuthorizedError(f"参数 {key} 必须是非空字符串")
    return value


def require_int(params: dict[str, object], key: str) -> int:
    """取整数参数（`bool` 不算整数）；不满足即 `NotAuthorizedError`。**写处理器公用**。"""
    value = params.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise NotAuthorizedError(f"参数 {key} 必须是整数")
    return value


def require_date(params: dict[str, object], key: str) -> date:
    """取日期参数（`date` / ISO 字符串 / 缺省基准日）；不满足即 `NotAuthorizedError`。"""
    value = params.get(key)
    if value is None:
        return DEFAULT_BOOKING_DATE
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        raise NotAuthorizedError(f"参数 {key} 必须是 ISO 日期字符串或 date")
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise NotAuthorizedError(f"参数 {key} 不是合法日期：{value}") from error


def _read_hash(gateway: FactGateway, record_id: str) -> str:
    """按 `record_id` 回读事实并取内容哈希（只用 01 的公开读取面）。"""
    table, _, primary_key = record_id.partition(":")
    if not primary_key:
        raise GatewayError(f"记录标识不合法：{record_id}")
    if table == "accounts":
        return gateway.account(primary_key).content_hash
    if table == "cards":
        return gateway.card(primary_key).content_hash
    if table == "holds":
        return gateway.hold(primary_key).content_hash
    if table == "payees":
        return gateway.payee(primary_key).content_hash
    if table == "subscriptions":
        return gateway.subscription(primary_key).content_hash
    if table == "transactions":
        return gateway.transaction(primary_key).content_hash
    if table == "collection_requests":
        return gateway.collection_request(primary_key).content_hash
    if table == "scheduled_transfers":
        return gateway.scheduled_transfer(primary_key).content_hash
    if table == "merchant_orders":
        return gateway.merchant_order(primary_key).content_hash
    if table == "password_change_events":
        return gateway.password_change_event(primary_key).content_hash
    raise GatewayError(f"未知记录表：{table}")


def money_cents(params: dict[str, object], spec: ToolSpec) -> int:
    """按 `spec.money_param` 取金额（整数 / 分），供写处理器包装成 `Money` 再调 `CoreBank`。"""
    if spec.money_param is None:
        raise NotAuthorizedError(f"动作 {spec.action} 未声明 money_param")
    return require_int(params, spec.money_param)

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
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from functools import wraps
from threading import RLock
from typing import TYPE_CHECKING, Concatenate, ParamSpec, Protocol, TypeVar, cast

from ai_banking_copilot.contracts.authorization import Decision, Level, ReasonCode, strictest
from ai_banking_copilot.contracts.plan import Plan, PlanNode, topological_order
from ai_banking_copilot.contracts.security import (
    INJECTION_MARKERS as INJECTION_MARKERS,
)
from ai_banking_copilot.contracts.security import (
    detect_injection,
)
from ai_banking_copilot.contracts.taint import Slot, SlotKind, Trust, Value
from ai_banking_copilot.gateway.errors import GatewayError, GatewayNotFoundError
from ai_banking_copilot.gateway.facts import Record
from ai_banking_copilot.gateway.models import Money, Payee, WealthProduct
from ai_banking_copilot.gateway.preconditions import (
    PreconditionResult,
    PreconditionStatus,
    WritePreconditionError,
)
from ai_banking_copilot.gateway.resolvers import parse_amount, parse_temporal
from ai_banking_copilot.planning.anaphora import (
    DialogueMemory,
    Reference,
    ReferenceKind,
    resolve_reference,
)
from ai_banking_copilot.planning.kernel import CONFIDENCE_THRESHOLD
from ai_banking_copilot.planning.llm import ACTION_KINDS, ActionKind, Intent
from ai_banking_copilot.planning.review_context import (
    bills_review_context,
    node_review_context,
    referenced_facts,
)
from ai_banking_copilot.planning.reviewer import (
    DEFAULT_REVIEW_PROMPT_VERSION,
    BypassPlanReviewer,
    FallbackPlanReviewer,
    ReviewChannel,
    ReviewerFailure,
    ReviewerUnavailableError,
    ReviewReport,
    ReviewRequest,
    ReviewStatus,
    StrictPlanReviewer,
    TemplatePlanReviewer,
)
from ai_banking_copilot.planning.slots import (
    REQUIRED_SLOTS,
    SLOT_PARAMS,
    SlotFill,
    SlotInputs,
    SlotQuestion,
    _payee_label,
    fill_control_slots,
    parse_horizon_days,
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
    BreakerSnapshot,
    BreakerTrigger,
    CircuitBreaker,
)
from ai_banking_copilot.policy.execution import CommittedWriteError, assess_plan
from ai_banking_copilot.policy.gate import StalePlanError, assert_frozen
from ai_banking_copilot.policy.pdp import (
    FLAG_DEVICE_CHANGE,
    FLAG_INJECTION_DETECTED,
    evaluate,
)
from ai_banking_copilot.policy.pep import ExecResult, NotAuthorizedError, manifest_hash
from ai_banking_copilot.policy.saga import (
    CompensationError,
    ReversalOutcome,
    ReversalState,
    Saga,
)
from ai_banking_copilot.policy.session_recovery import (
    InFlightNode,
    ReconcileStatus,
    business_key_for,
    reconcile,
)
from ai_banking_copilot.policy.tool_hook import ToolHookBlocked
from ai_banking_copilot.policy.verification import (
    ChallengeRecord,
    ChallengeStatus,
    DemoVerificationProvider,
    HmacStrongVerificationProofVerifier,
    StrongVerificationProof,
    VerificationBinding,
    VerificationError,
    VerificationMode,
    create_challenge,
    invalidate_challenge,
    verify_challenge,
)
from ai_banking_copilot.scenarios.authorization import node_policy_request
from ai_banking_copilot.scenarios.base import (
    ScenarioOutcome,
    ScenarioStatus,
    plan_is_pure_query,
)
from ai_banking_copilot.scenarios.bill_advice import BillAdvice, build_bill_advice
from ai_banking_copilot.scenarios.bill_findings import analyze_bill, report_payload
from ai_banking_copilot.scenarios.life_events import detect_life_event, flower_offers
from ai_banking_copilot.scenarios.pending import PendingHandler
from ai_banking_copilot.scenarios.registry import (
    UnknownScenarioError,
    run_scenario,
    select_handler,
)
from ai_banking_copilot.scenarios.wealth import (
    FORBIDDEN_HINTS,
    WEALTH_ACTIONS,
    RiskAssessment,
    describe_assessment,
    filter_candidates,
    holdings_payload,
    is_recommendation_request,
    recommend_products,
    suitability_refusal,
    to_risk_assessment,
)
from ai_banking_copilot.scenarios.wealth_assessment import (
    invitation_message,
    is_assessment_request,
    questionnaire_payload,
)
from ai_banking_copilot.tools.registry import ToolCall
from ai_banking_copilot.web.plan_card import (
    PROMPT_STRONG_VERIFY,
    PlanCard,
    render_plan_card,
)
from ai_banking_copilot.web.session_manager import PROCESS_SESSION_LOCK
from ai_banking_copilot.web.session_repository import SessionPersistenceError
from ai_banking_copilot.web.session_snapshot import SESSION_SNAPSHOT_VERSION, SessionSnapshot
from ai_banking_copilot.web.triage import TriageVerdict, triage

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
            if self._persistence_failed:
                raise SessionPersistenceError("session_restart_required")
            try:
                result = method(self, *args, **kwargs)
            except Exception:
                self._collect_model_observations()
                self._persist()
                raise
            self._collect_model_observations()
            self._persist()
            return result
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
_MERCHANT_CANCEL_REFUSAL = "只能报告本地真实订阅状态；商户取消尚未接入，当前没有商户确认回执。"
_REFUND_GUARANTEE_REFUSAL = "退款与保证商户未来不再扣款均未接入，无法执行或承诺。"

_NOT_GUARANTEED_REFUSAL = (
    "理财不是保本产品：不保证本金，也可能亏损。我不会承诺保本或收益。"
    "能做的是先确认你的风险等级与产品风险等级是否匹配，再决定买不买。"
)
_NO_SKIP_VERIFY_REFUSAL = (
    "验证与适当性不能跳过：红色动作必须有服务端一次性验证码，等级不足也不能越级申购。"
    "本次未执行任何动作。"
)
_NO_FAKE_RECEIPT_REFUSAL = (
    "不会伪造执行结果：只有服务端真正执行后才会产生回执与记录号。"
    "这笔没有执行，所以我不会念回执。"
)
_NO_DOUBLE_REFUSAL = (
    "不承诺收益、不预测涨跌，也不推荐所谓能翻倍的产品。"
    "能给的是产品事实：风险等级、期限、起购金额。"
)
_SCAM_REFUSAL = (
    "先别按对方说的操作：银行或平台客服不会要求你先取消代扣、再私下退款。"
    "建议立刻结束对话，用银行卡背面的官方客服电话或官方 App 核实；"
    "不要提供验证码、密码，也不要共享屏幕或远程协助。"
    "如果已经按对方指示操作，请立即查询扣款并联系银行止付。本次没有执行任何取消动作。"
)

# 电信诈骗的确定性组合：对方自称客服/商家 + 要求退款/配合 + 指向代扣或验证码。
# 命中后**不进入规划**，先给安全指引——这类输入不能按普通取消订阅去执行。
_SCAM_LURE = re.compile(
    r"(?:客服|工作人员|公检法|平台|商家|快递).{0,20}(?:退款|退费|理赔|配合|解冻|验证)"
)
_SCAM_CANCEL = re.compile(r"取消|关闭|解绑|停掉")
_SCAM_TARGET = re.compile(r"代扣|自动续费|自动扣款|免密|银行卡|验证码|屏幕共享")

# 「保本吗 / 是不是保本」= 用户在问常识，要正面解释；「保证收益」= 在索取承诺，维持硬拒。
_GUARANTEE_QUESTION = re.compile(r"保本.{0,6}(吗|么|呢|？|\?)|是不是保本|不是保本")
# 只认**显式要求跳过验证**的说法。「忽略…规则」不在此列：那句话可能只是取消订阅这类
# 批量操作的噪音，交给场景层的批量拒绝去回，免得把话术抢错（实测 10-31 订阅回归用例）。
_SKIP_VERIFY = re.compile(r"不用验证|不需要验证|不验证|跳过验证|别验证|无需验证")
_FAKE_RECEIPT = re.compile(
    r"(你就说|就说|假装|当作|硬说).{0,12}(成功|已完成|成交|送到|送达|到了|签收)|把回执念"
)
_DOUBLE_MONEY = re.compile(r"翻倍|翻一番")
_HOLD_QUERY = re.compile(r"冻结|押金|预授权|hold", re.IGNORECASE)
_HOLD_STATUS = re.compile(r"退回来|退回|还在|还有|状态|怎么样了|到账|没")
_HOLD_ACTION = re.compile(r"取消|解冻|释放|退回给|转回")


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

PLAN_TTL_S = 300
"""待确认计划有效期（秒，G-02）：由**服务端**写入 `PlanCard.expires_at`，过期不可再确认。"""

MAX_CONFIRM_STEPS = 20
"""`confirm(steps=...)` 的上界：仅用于「分步执行 / 中断恢复」演示，不得借它改变授权语义。"""

_PAYEE_HISTORY_DAYS = 365
"""同名候选上「最近一次转账」的回看窗口（天）：只查已记账流水，没有就说「暂无」。"""


class SessionRiskSource(Protocol):
    """**服务端**可信会话风控信号源（G-03）：认证会话 + 请求行为 + 可信风控结果。

    返回值语义固定为三态：
    - `True`：确认存在会话异常（设备 / 地点跳变等）；
    - `False`：确认正常；
    - `None`：**无可信结果**（信号源不可用 / 判定失败）——调用方必须按安全策略升级，不得据此放行。

    客户端载荷**永远**不得作为本协议的输入或替代实现。
    """

    def assess(self, *, user_id: str, session_id: str) -> bool | None:
        """返回当前（用户, 会话）的可信风控结论。"""
        ...


def _naive_moment(moment: datetime | None) -> datetime:
    """把时刻归一到**本地朴素时间**：会话内时间戳同一时钟域，避免 aware/naive 混比。"""
    if moment is None:
        return datetime.now()
    if moment.tzinfo is None:
        return moment
    return moment.astimezone().replace(tzinfo=None)


@dataclass(frozen=True)
class VerificationPrompt:
    """投递结果；只有明确标识为 demo 的模式可以回显演示码。"""

    channel: str
    target: str
    code: str | None
    expires_in_s: int
    mode: str = "demo"
    status: str = "delivered"


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
class ClarificationChoice:
    """一条**可点选**的澄清候选：`label` 给人看，`value` 回填输入框后提交。

    标签里的信息全部来自名册与已记账流水（手机尾号 / 账号尾号 / 最近一次转账日期），
    不补全号码中间位——名册只存后四位，编造前半段就是生成事实。
    """

    label: str
    value: str


@dataclass(frozen=True)
class ClarificationAsk:
    """一条待澄清提问：`slot` 为需补齐的槽位名（与 `selected` 同口径），`prompt` 为文案。

    `candidates` 只在一句话能落到**有限几个名册对象**时给出（当前是同名收款人），
    用户点一下就答完，不必回头查自己的账号尾号。
    """

    slot: str
    prompt: str
    candidates: tuple[ClarificationChoice, ...] = ()
    options: tuple[ClarificationChoice, ...] = ()
    """通用可选项（商户 / 花束 / 档位）：与 `candidates` 一样点一下就答完，
    区别是值可能是业务名称而不是名册 ID。"""


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

    questionnaire: tuple[dict[str, object], ...] = ()
    """风险测评问卷（题号 / 题干 / 选项，不含分值）：仅「我要做风险测评」时下发。"""

    triage: TriageVerdict | None = None
    """未识别输入的三分类结果（闲聊 / 超范围 / 模糊）；`None` = 策略拒绝或正常产出。"""


@dataclass(frozen=True)
class NodeProgress:
    """单节点执行进度（**服务端唯一判据**：前端只渲染，不推导）。

    取值：`pending` / `completed` / `unverified` / `reversed` / `suspended` / `skipped`。
    """

    node_id: str
    status: str


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
    plan: Plan | None = None
    """本次执行对应的冻结计划（供前端渲染任务 DAG；**只读**）。"""
    node_progress: tuple[NodeProgress, ...] = ()
    """逐节点执行状态；`()` = 无计划可展示。"""
    bill_advice: tuple[BillAdvice, ...] = ()
    """月账单执行完成后的只读消费建议；不改变计划和授权。"""
    bill_advice_message: str = ""
    bill_findings: Mapping[str, object] | None = None
    """账单异常识别结果（确定性引擎，金额可重算）；`None` = 本次未执行月账单或不可用。

    **不含模型文案**：模型解释只在 `GET /api/bills/analysis` 触发，避免每次读会话都调用模型。"""
    hook_status: str = ""
    """写操作 Pre-Tool Hook 的拦截状态（`confirmation_required` 等）；空串 = 未被拦截。"""


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


class _RestoredBreakerStore:
    def __init__(self, snapshot: BreakerSnapshot) -> None:
        self._snapshot = snapshot

    def load(self) -> BreakerSnapshot:
        return self._snapshot

    def save(self, snapshot: BreakerSnapshot) -> None:
        self._snapshot = snapshot


class Session:
    """单会话编排：`Session(deps)`。"""

    _SNAPSHOT_FIELDS = (
        "message", "pending", "confirmation", "confirmation_history", "takeover",
        "audit_ids", "exec_results", "completed_node_ids", "pending_node_ids", "challenge",
        "unverified_results", "quota", "risky_at", "policy_rejections", "reversed_records",
        "last_utterance", "last_selected", "review_intents", "clarification_keys",
        "review_report", "in_flight", "recovery_blocked",
        "planner_channel", "model_calls",
    )

    def __init__(
        self, deps: AppDeps, *, session_id: str = "default", shared_lock: RLock | None = None,
    ) -> None:
        self._lock = shared_lock if shared_lock is not None else PROCESS_SESSION_LOCK
        self._deps = deps
        self._session_id = session_id
        self._revision = 0
        self._persistence_failed = False
        self._in_flight: InFlightNode | None = None
        self._recovery_blocked = False
        self._confirmation_history: list[ConfirmationRecord] = []
        self._planner_channel: str | None = None
        self._model_calls: list[dict[str, object]] = []
        self._verification_provider = deps.verification_provider or DemoVerificationProvider()
        verification_key = deps.verification_key
        if verification_key is None:
            verification_key = (
                secrets.token_bytes(32)
                if getattr(self._verification_provider, "mode", "demo") == "demo"
                and not deps.verification_strict else b""
            )
        self._verification_key: bytes = verification_key
        self._strong_verification_proof: StrongVerificationProof | None = None
        self._verification_limiter = deps.verification_limiter
        """强验证失败计数（**服务端**维护、按用户共享）：换挑战或换会话都不清零。"""
        self._execution_id = secrets.token_hex(16)
        self._state = SessionState.IDLE
        self._message = "会话就绪"
        self._hook_status = ""
        """最近一次执行的 Pre-Tool Hook 拦截状态（瞬态，不进会话快照）。"""
        self._pending: _Pending | None = None
        self._confirmation: ConfirmationRecord | None = None
        self._takeover: TakeoverPackage | None = None
        self._audit_ids: list[str] = []
        self._exec_results: list[ExecResult] = []
        self._completed_node_ids: list[str] = []
        self._pending_node_ids: list[str] = []
        self._challenge: ChallengeRecord | None = None
        self._unverified_results: dict[str, ExecResult] = {}
        self._quota: DailyQuota | None = None
        # 熔断触发 3 / 4 的**跨提交**计数（有意不被 `_reset()` 清空：它们是会话级而非单次提交级）
        self._risky_at: list[datetime] = []
        self._policy_rejections = 0
        self._session_breaker = CircuitBreaker()
        """**会话级熔断器**（README 裁决 #27）：触发 3 / 4 / 5 的**作用域**——只让本会话转人工接管，
        不波及其他会话。全局熔断器（`deps.breaker`）只由**系统性**信号触发（触发 1 / 2）。"""
        self._reversed_records: list[str] = []
        self._released_holds: set[str] = set()
        """已被终态收尾释放的冻结记录（幂等去重，避免重复释放被重复计数）。"""
        # 澄清闭环：保留上一条指令与选中对象，供 `clarify()` 在原指令上续答（不被 `_reset()` 清空）
        self._last_utterance: str | None = None
        self._last_selected: Mapping[str, object] | None = None
        self._memory: DialogueMemory | None = None
        """上一笔**已成功执行**的转账事实（跨 `_reset` 保留）：只服务多轮指代。"""
        self._last_wealth: str | None = None
        """上一轮**理财申购**提到的产品 id（不论是否执行成功）。

        只用于「就买刚才那个」这类指代；命中时**只澄清、不执行**——
        上一轮可能因为起购 / 等级被拒，自动补全等于替用户改金额。"""
        self._review_intents: list[dict[str, object]] = []
        self._clarification_keys: frozenset[str] = frozenset()
        self._review_report: ReviewReport | None = None
        self._recent: list[dict[str, object]] = []
        """最近几轮的「用户原话 + 已采纳意图」摘要：只用于理解层上下文，不参与裁决。"""

    def snapshot(self) -> SessionSnapshot:
        payload = {name: getattr(self, f"_{name}") for name in self._SNAPSHOT_FIELDS}
        payload["pending_node_ids"] = [
            node_id for node_id in self._pending_node_ids if node_id not in self._completed_node_ids
        ]
        payload["session_breaker"] = BreakerSnapshot(
            self._session_breaker.state(), self._session_breaker.failure_count(),
            self._session_breaker.triggers(),
        )
        return SessionSnapshot(
            SESSION_SNAPSHOT_VERSION, self._deps.user_id, self._session_id,
            self._revision, self._execution_id, self._state.value, payload,
        )

    def _persist(self) -> None:
        repository = self._deps.session_repository
        if repository is None:
            return
        try:
            saved = repository.save(self.snapshot(), expected_revision=self._revision or None)
            self._revision = saved.revision
        except Exception as error:
            self._persistence_failed = True
            self._state = SessionState.INTERRUPTED
            self._message = "会话保存失败，已暂停；请重启服务后核对业务回执"
            raise SessionPersistenceError("session_save_failed") from error

    @classmethod
    def restore(cls, deps: AppDeps, snapshot: SessionSnapshot, shared_lock: RLock) -> Session:
        if snapshot.user_id != deps.user_id:
            raise SessionStateError("会话身份不匹配")
        session = cls(deps, session_id=snapshot.session_id, shared_lock=shared_lock)
        if set(snapshot.payload) != {*cls._SNAPSHOT_FIELDS, "session_breaker"}:
            raise SessionStateError("会话快照字段不完整")
        # 字段白名单固定，codec 已检查类型、taint、哈希及检查点绑定。
        for name in cls._SNAPSHOT_FIELDS:
            setattr(session, f"_{name}", snapshot.payload[name])
        session._revision = snapshot.revision
        session._execution_id = snapshot.execution_id
        session._state = SessionState(snapshot.state)
        breaker = cast(BreakerSnapshot, snapshot.payload["session_breaker"])
        session._session_breaker = CircuitBreaker(store=_RestoredBreakerStore(breaker))
        session._invalidate_verification("service_restarted")
        session._reconcile_after_restart()
        if session._state not in (SessionState.COMPLETED, SessionState.REVERSED):
            if session._confirmation is not None:
                session._confirmation_history.append(session._confirmation)
                session._confirmation = None
            if session._pending is not None and session._state in (
                SessionState.AWAITING_CONFIRM, SessionState.EXECUTING, SessionState.INTERRUPTED,
            ) and not session._recovery_blocked:
                session._state = SessionState.AWAITING_CONFIRM
                session._message = "会话已恢复，已完成步骤保留；请重新确认，红卡需重新强验证"
        if (session._pending is not None
                and session._state in (SessionState.AWAITING_CONFIRM, SessionState.EXECUTING,
                                       SessionState.INTERRUPTED)
                and not plan_is_pure_query(session._pending.plan)):
            report = session._review_report
            configured_channel = _configured_reviewer_channel(deps.reviewer)
            channel_mismatch = (
                report is not None
                and (configured_channel is None or report.channel != configured_channel)
            )
            invalid_required_evidence = (
                deps.reviewer_required
                and (
                    report is None or report.status is not ReviewStatus.ALLOW
                    or report.diagnostic_only or report.failure_code or report.error
                    or report.fallback_reason or report.execution_id != session._execution_id
                    or report.cfhash != session._pending.card.frozen_hash
                    or report.prompt_version != DEFAULT_REVIEW_PROMPT_VERSION
                )
            )
            if channel_mismatch:
                assert report is not None
                session._review_report = replace(
                    report,
                    status=ReviewStatus.MANUAL,
                    findings=(),
                    clarifications=(),
                    failure_code=ReviewerFailure.BINDING_MISMATCH,
                    error="恢复时 Reviewer 通道与当前配置不一致",
                )
            if channel_mismatch or invalid_required_evidence:
                reason = (
                    "恢复的 Reviewer 通道与当前配置不一致，需重新审查"
                    if channel_mismatch else "恢复的审查证据不满足当前严格要求"
                )
                session._enter_takeover(reason, session._pending)
        session._persist()
        return session

    def _reconcile_after_restart(self) -> None:
        node = self._in_flight
        if node is None:
            self._recovery_blocked = bool(self._unverified_results)
            return
        if self._deps.receipts is None:
            self._recovery_blocked = True
            self._state = SessionState.INTERRUPTED
            self._message = "未配置回执核对，已暂停执行"
            return
        result = reconcile(node, self._deps.receipts, self._deps.audit)
        if result.reason != "audit_chain_invalid" and self._pending is not None:
            self._audit(KIND_DECISION, {
                "kind": "recovery_check", "status": result.status.value,
                "user_id": self._deps.user_id, "session_id": self._session_id,
                "execution_id": self._execution_id, "node_id": node.node_id,
                "record_ids": list(result.record_ids),
            })
        if result.status is ReconcileStatus.VERIFIED:
            if node.node_id not in self._completed_node_ids:
                self._completed_node_ids.append(node.node_id)
                self._exec_results.append(ExecResult(
                    status=result.receipt.status if result.receipt else "verified",
                    record_ids=result.record_ids, message="恢复时已核对原业务回执及审计",
                    action=node.action,
                ))
            self._unverified_results.pop(node.node_id, None)
            self._in_flight = None
            if self._pending is not None and set(self._completed_node_ids) == set(
                self._pending.node_ids
            ):
                self._state = SessionState.COMPLETED
                self._message = "恢复核对完成：原计划已执行完成"
        elif result.status is ReconcileStatus.NOT_COMMITTED:
            self._in_flight = None
        else:
            self._recovery_blocked = True
            self._state = SessionState.INTERRUPTED
            self._message = "业务回执或审计证据尚未核对，已暂停；请人工复核"
            if result.receipt is not None:
                self._unverified_results[node.node_id] = ExecResult(
                    status="committed_unverified", record_ids=result.record_ids,
                    message=self._message, action=node.action,
                )
            if result.status is ReconcileStatus.MANUAL_CONFLICT and self._pending is not None:
                self._enter_takeover(self._message, self._pending)

    def current_view(self) -> ExecutionView:
        with self._lock:
            return self._execution_view()

    def _collect_model_observations(self) -> None:
        if self._deps.model_observations is not None:
            self._model_calls.extend(self._deps.model_observations.drain(self._execution_id))

    def model_metadata(self) -> dict[str, object]:
        usage: dict[str, int] = {}
        for call in self._model_calls:
            raw_usage = call.get("usage")
            if isinstance(raw_usage, dict):
                for key, value in raw_usage.items():
                    if type(value) is int and value >= 0:
                        usage[key] = usage.get(key, 0) + value
        return {
            "planner_channel": self._planner_channel,
            "reviewer_channel": self._review_report.channel if self._review_report else None,
            "http_attempts": len(self._model_calls), "usage": usage,
        }

    def state(self) -> SessionState:
        """当前会话状态。"""
        return self._state

    def card(self) -> PlanCard | None:
        """当前待确认的计划卡（无则 `None`）。"""
        return self._pending.card if self._pending is not None else None

    def last_utterance(self) -> str | None:
        """尚待澄清的原指令（无则 `None`）。

        只读投影：`clarify` 成功后原指令会被清空，因此渠道层需要在调用前取用，
        用于把「续答后成卡」的同一次生活事件也登记到记录表（不读信前端自称）。
        """
        return self._last_utterance

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
        now: datetime | None = None,
        on_stage: Callable[[str], None] | None = None,
    ) -> SubmissionView:
        """一次新的用户意图拥有独立执行身份，即使内容完全相同。

        `on_stage` 是**旁路观测**回调（阶段可见），不参与任何判定，异常一律吞掉。
        """
        reference = resolve_reference(utterance, self._memory)
        if reference.kind is ReferenceKind.UNDO:
            return self._undo_last()
        rewrite = self._reference_rewrite(reference)
        if rewrite is None:
            return self._submit(
                utterance, selected=selected, now=now,
                execution_id=secrets.token_hex(16), on_stage=on_stage,
            )
        rewritten, inherited, note = rewrite
        applied = {**inherited, **(selected or {})}
        view = self._submit(
            rewritten, selected=applied, now=now,
            execution_id=secrets.token_hex(16), on_stage=on_stage,
        )
        return replace(view, message=f"{view.message}；{note}" if view.message else note)

    def _undo_last(self) -> SubmissionView:
        """「刚才那笔取消」：走会话级冲正，**不产生新的业务动作**。"""
        if self._confirmation is None and not self._exec_results:
            return SubmissionView(
                state=self._state,
                message="没有可撤回的上一笔：本会话还没有执行过计划",
                card=None, decision=None, plan=None, questions=(),
            )
        try:
            outcome = self.reverse()
        except SessionStateError as error:
            return SubmissionView(
                state=self._state,
                message=f"撤回未执行：{error}",
                card=None, decision=None, plan=None, questions=(),
            )
        self._memory = None
        return SubmissionView(
            state=(
                SessionState.REVERSED
                if outcome.state is ReversalState.REVERSED
                else SessionState.FAILED
            ),
            message=outcome.message,
            card=None, decision=None, plan=None, questions=(),
        )

    def _reference_rewrite(
        self, reference: Reference
    ) -> tuple[str, dict[str, object], str] | None:
        """把指代改写成**完整一句**，并给出补全说明。

        只支持上一笔是 `transfer.create` 的场景：收款人、账户、金额全部来自
        上一笔**已执行**的冻结计划与事实网关；任何一项取不到就返回 `None`（走正常澄清）。
        """
        memory = self._memory
        if memory is None or memory.action != "transfer.create":
            return None
        if reference.kind is ReferenceKind.NONE:
            return None
        amount = (
            reference.amount_cents
            if reference.kind is ReferenceKind.AMOUNT_ONLY
            else memory.amount_cents
        )
        if amount is None or memory.payee_id is None:
            return None
        try:
            payee = self._deps.gateway.payee(memory.payee_id).payload
        except GatewayNotFoundError:
            return None
        account_id = memory.account_id
        notes: list[str] = []
        if reference.kind is ReferenceKind.SWITCH_ACCOUNT:
            other = self._other_account_id(account_id)
            if other is None:
                notes.append("只有这一个可用账户，已沿用原账户")
            else:
                account_id = other
                notes.append("已按上一笔改换账户")
        inherited: dict[str, object] = {"payee": memory.payee_id}
        if account_id is not None:
            inherited["account"] = account_id
        note = (
            f"已按上一笔补全：收款人 {payee.full_name}、金额 {Money(amount).yuan} 元"
            + ("；" + "；".join(notes) if notes else "")
        )
        rewritten = f"转给{payee.full_name} {Money(amount).yuan} 元"
        return rewritten, inherited, note

    def _other_account_id(self, current: str | None) -> str | None:
        """换账户：取本人名下另一个账户（确定序，不随机）。"""
        for record in self._deps.gateway.accounts_of(self._deps.user_id):
            if record.payload.account_id != current:
                return record.payload.account_id
        return None

    def _remember(self, plan: Plan) -> None:
        """记住最近一笔**已成功执行**的转账，供「再转一次 / 再转 200」使用。"""
        memory: DialogueMemory | None = None
        for node in plan.nodes:
            if node.action != "transfer.create":
                continue
            payee_id = node.params.get("payee_id")
            amount_cents = node.params.get("amount_cents")
            account_id = node.params.get("account_id")
            if not isinstance(payee_id, str) or not isinstance(amount_cents, int):
                continue
            if isinstance(amount_cents, bool):
                continue
            memory = DialogueMemory(
                action=node.action,
                payee_id=payee_id,
                account_id=account_id if isinstance(account_id, str) else None,
                amount_cents=amount_cents,
            )
        self._memory = memory

    def _submit(
        self,
        utterance: str,
        *,
        selected: Mapping[str, object] | None = None,
        now: datetime | None = None,
        execution_id: str,
        clarification_keys: frozenset[str] = frozenset(),
        on_stage: Callable[[str], None] | None = None,
        allow_freeform_clarification: bool = True,
    ) -> SubmissionView:
        """提交一句自然语言：装配意图 → 选场景 → 组装计划 → 裁决 → 计划卡。

        会话风控信号（G-03）**只**来自服务端可信源 `AppDeps.session_risk`，本方法不接受任何
        客户端风控入参。
        """
        if (
            allow_freeform_clarification
            and self._state is SessionState.NEEDS_CLARIFICATION
            and self._last_utterance is not None
            and utterance.strip() != self._last_utterance.strip()
        ):
            # 有待澄清项时，用户在聊天框里补一句（「李娜」「3000」）也应并入待补槽位，
            # 而不是被当成一条全新指令。解析不到任何槽位时照常走新指令。
            freeform = self._freeform_answers(utterance)
            if freeform:
                return self.clarify(freeform, on_stage=on_stage)
        try:
            _notify(on_stage, "guard")
            self._reset()
            self._execution_id = execution_id
            moment = now if now is not None else datetime.now()
            self._last_utterance = utterance
            self._last_selected = dict(selected) if selected is not None else None
            self._clarification_keys = clarification_keys
            # 注入是提交入口的系统性信号；即使后续在意图装配前被拒绝，也必须触发全局熔断。
            injection_detected = detect_injection(utterance)
            if injection_detected:
                self._trip_global(BreakerTrigger.INJECTION_DETECTED, "注入检测命中（H3）")
            refusal = _guard_utterance(utterance)
            if refusal is not None:
                return self._refuse(
                    refusal.message, utterance,
                    capability_boundary=refusal.capability_boundary,
                )
            if (
                any(word in utterance for word in ("所有", "全部"))
                and any(word in utterance for word in ("退订", "取消"))
                and any(word in utterance for word in ("退订", "订阅", "会员", "代扣"))
            ):
                merchants = "、".join(
                    record.payload.merchant
                    for record in self._deps.gateway.subscriptions_of(self._deps.user_id)
                )
                return self._refuse(
                    f"不支持批量取消，也不能跳过确认。请选择一项订阅后确认：{merchants}。",
                    utterance,
                )
            invalid = self._invalid_selection(selected or {})
            if invalid is not None:
                return self._refuse(invalid, utterance)
            # 第三人账户属于**能力边界**（不支持代他人操作），不是策略拒绝。
            third_party = _third_party_account_refusal(utterance)
            if third_party is not None:
                return self._refuse(third_party, utterance, capability_boundary=True)
            # 「改余额 + 买理财」同属**能力边界**：余额只能由真实资金变动产生，
            # 整句拒绝，避免拿伪造的余额去生成申购卡（S6-7）。
            balance_tamper = _balance_tamper_refusal(utterance)
            if balance_tamper is not None:
                return self._refuse(balance_tamper, utterance, capability_boundary=True)
            # 明确要测评就先出问卷：测评是申购的前置，即使句子里还挂着第二件事
            # （「先做个风险测评，再买理财」）也先测再办。第二件事由话术挂回来
            # （「测完再发我一次」），既不静默吞掉半句，也不反过来先追问金额。
            if _is_assessment_request(utterance):
                return self._assessment_invitation(defer=_has_second_clause(utterance))
            fact_answer = self._wealth_fact_answer(utterance)
            if fact_answer is not None:
                return fact_answer
            hold_answer = self._hold_fact_answer(utterance)
            if hold_answer is not None:
                return hold_answer
            recall = self._wealth_recall_answer(utterance)
            if recall is not None:
                return recall
            claimed = self._claimed_level_answer(utterance)
            if claimed is not None:
                return claimed
            _notify(on_stage, "understand")
            try:
                intents, asks, notes = self._assemble_intents(utterance, selected)
            except _WealthRefusal as refusal:
                return self._refuse(str(refusal), utterance)
            self._remember_wealth_attempt(intents)
            if asks:
                self._state = SessionState.NEEDS_CLARIFICATION
                self._message = _with_notes(
                    "信息不足，请补充：" + "；".join(ask.prompt for ask in asks), notes
                )
                return SubmissionView(
                    self._state, self._message, None, None, None,
                    tuple(ask.prompt for ask in asks), clarifications=asks,
                )
            if not intents:
                selection = _wealth_selection_fallback(utterance, self._deps)
                if selection is not None:
                    self._state = SessionState.IDLE
                    self._message = selection
                    return SubmissionView(self._state, self._message, None, None, None, ())
                verdict = triage(
                    utterance, low_confidence=any("置信度不足" in note for note in notes)
                )
                if verdict is not None:
                    return self._refuse(
                        verdict.message, utterance,
                        capability_boundary=True, triage_verdict=verdict,
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
            _notify(on_stage, "plan")
            outcome = run_scenario(handler, intents, self._deps.gateway)
            if outcome.plan is not None:
                self._note_risky_activity(
                    now=moment, risky=not plan_is_pure_query(outcome.plan)
                )
            _notify(on_stage, "review")
            return self._after_outcome(
                outcome,
                utterance,
                asks,
                candidate_actions=tuple(intent.action for intent in intents),
                accepted_intents=intents,
                notes=notes,
                now=moment,
                injection_already_tripped=injection_detected,
            )
        finally:
            _notify(on_stage, "done")

    @_serialized
    def clarify(
        self,
        answers: Mapping[str, object],
        *,
        on_stage: Callable[[str], None] | None = None,
    ) -> SubmissionView:
        """澄清闭环：用**补充答案**重跑上一次的指令（用户不必整句重述）。

        `answers` 是「槽位名 → 值」，与 `selected` 同口径——因此模板通道与 LLM 通道都能用，
        不依赖模型从补充文本里重新推断。没有待澄清的上一次指令时抛 `SessionStateError`。
        """
        if self._last_utterance is None:
            raise SessionStateError("尚无上一条指令：无法澄清")
        merged: dict[str, object] = dict(self._last_selected or {})
        merged.update(answers)
        delivery_time = answers.get("delivery_time")
        if isinstance(delivery_time, str) and delivery_time.strip():
            # 送达日期就是订单日期：让计划卡与记录显示用户确认的那一天。
            merged.setdefault("order_on", delivery_time.strip())
        execution_id = self._execution_id if self._pending is None else secrets.token_hex(16)
        view = self._submit(
            self._last_utterance, selected=merged, execution_id=execution_id,
            clarification_keys=self._clarification_keys | frozenset(answers),
            on_stage=on_stage,
            allow_freeform_clarification=False,
        )
        delivery = {
            key: str(value) for key, value in answers.items()
            if key in {"delivery_address", "delivery_time"} and value
        }
        if delivery and view.card is not None:
            labels = {"delivery_address": "配送地址", "delivery_time": "送达时间"}
            note = "；".join(f"{labels[key]}：{value}" for key, value in delivery.items())
            view = replace(view, message=f"{view.message}\n配送信息（演示，不实际配送）——{note}")
        return view

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
            review_handler=handler,
            review_kind="due_proposal",
            now=datetime.now(),
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
                if not isinstance(value, str):
                    continue
                if slot in {"payee", "payer"} and not value.startswith("p_"):
                    # 收款人允许用户填人名：交给名册解析（唯一命中才绑定，多个/没有都转澄清），
                    # 不能在这里先按「内部 ID」拒掉「李娜」这种正常输入。
                    continue
                if value not in identifiers:
                    return "所选对象不存在、已失效或不属于当前用户，请重新选择。"
        return None

    def _after_outcome(
        self,
        outcome: ScenarioOutcome,
        utterance: str,
        asks: tuple[ClarificationAsk, ...],
        *,
        candidate_actions: tuple[str, ...] = (),
        notes: tuple[str, ...] = (),
        review_handler: PendingHandler | None = None,
        review_kind: str = "user_request",
        accepted_intents: tuple[Intent, ...] = (),
        now: datetime | None = None,
        injection_already_tripped: bool = False,
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
        flags = self._context_flags(
            injection_already_tripped=injection_already_tripped,
        )
        risk = assess_plan(
            plan, registry=self._deps.registry, gateway=self._deps.gateway,
            user_id=self._deps.user_id, today=self._deps.today, flags=flags,
            floor=outcome.decision,
        )
        node_decisions = risk.node_decisions
        decision = risk.decision
        hit_rules = tuple(reason.value for reason in decision.reasons)
        card = self._render_card(
            plan, decision, now=now if now is not None else datetime.now(),
        )
        self._audit(KIND_DECISION, {
            "kind": "authorization_decision",
            "user_id": self._deps.user_id,
            "session_id": self._session_id,
            "execution_id": self._execution_id,
            "card_id": card.card_id,
            "frozen_hash": card.frozen_hash,
            "decision_level": decision.level.name,
            "reason_codes": list(hit_rules),
            "actions": [node.action for node in plan.nodes],
        })
        quota = (
            DailyQuota(risk.limit_cents, risk.accumulated_cents)
            if decision.level in (Level.YELLOW, Level.RED) and _plan_has_outflow(plan)
            else None
        )
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
            try:
                scenario_facts, proposals = (
                    review_handler.review_context(self._deps.gateway, plan)
                    if review_handler is not None else ([], {})
                )
                bill_facts, bill_proposals = bills_review_context(
                    self._deps.gateway, self._deps.user_id, accepted_intents,
                    self._review_intents, plan,
                )
                scenario_facts = [*scenario_facts, *bill_facts]
                proposals = {**proposals, **bill_proposals}
                facts = referenced_facts(
                    self._deps.gateway, self._deps.user_id, self._review_intents, plan,
                )
                facts = list({
                    str(fact["record_id"]): fact for fact in (*facts, *scenario_facts)
                }.values())
                node_context = node_review_context(
                    plan, facts, self._review_intents, proposals, review_kind=review_kind,
                )
                if any(context["missing_sources"] for context in node_context.values()):
                    raise ReviewerUnavailableError("审查上下文缺少必要来源，请重新检查或转人工。")
                request = ReviewRequest(
                    execution_id=self._execution_id,
                    cfhash=card.frozen_hash,
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
                        "facts": facts,
                        "node_context": node_context,
                    },
                    source_spans=(utterance,),
                    plan_summary=(
                        "到期待办提案：用户只请求检查；须确认计划卡并通过风险分级后才能执行。"
                        if review_kind == "due_proposal" else "用户提交的待确认计划"
                    ),
                )
                self._review_report = self._deps.reviewer.review(request)
            except ReviewerUnavailableError:
                review_error = "reviewer_unavailable"
                self._review_report = None
            except Exception:  # noqa: BLE001 - strict channel fail-closed when required
                review_error = "reviewer_error"
                self._review_report = None
            if self._review_report is not None:
                report = self._review_report
                if reviewer_enforced and report.status is ReviewStatus.ALLOW and (
                    report.diagnostic_only or report.failure_code is not None
                    or report.error is not None or report.fallback_reason is not None
                    or report.execution_id != request.execution_id
                    or report.cfhash != request.cfhash
                    or report.prompt_version != request.prompt_version
                ):
                    self._enter_takeover("独立审查证据无效，请转人工核对。", pending)
                    self._audit(KIND_DECISION, {
                        "execution_id": self._execution_id,
                        "frozen_hash": card.frozen_hash,
                        "review_status": "manual", "review_failure": "invalid_review_evidence",
                    })
                    return SubmissionView(self._state, self._message, card, decision, plan, ())
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
                    review_text = (
                        report.clarifications or report.findings
                        if report.channel != ReviewChannel.LLM_STRICT.value
                        else ()
                    )
                    if review_kind == "due_proposal":
                        reason = (
                            "到期待办审查需要重新检查：" + "；".join(review_text)
                            + "。请重新检查待办或转人工处理。"
                            if review_text
                            else "到期待办审查要求重新检查，请重新检查待办或转人工处理。"
                        )
                        self._enter_takeover(reason, pending)
                        return SubmissionView(
                            self._state, self._message, card, decision, plan, ()
                        )
                    self._pending = None
                    self._confirmation = None
                    clarification_text = review_text or (
                        "请补充或核对计划信息后重新提交。",
                    )
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
                    review_text = (
                        report.findings
                        if report.channel != ReviewChannel.LLM_STRICT.value
                        else ()
                    )
                    reason = (
                        "独立审查未通过：" + "；".join(review_text)
                        if review_text
                        else "独立审查未通过，请转人工核对。"
                    )
                    self._enter_takeover(
                        reason, pending,
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
        if reviewer_enforced and self._deps.reviewer is None:
            self._enter_takeover("独立审查通道未配置，请转人工处理。", pending)
            self._audit(KIND_DECISION, {
                "execution_id": self._execution_id, "frozen_hash": card.frozen_hash,
                "review_status": "manual", "review_failure": "reviewer_missing",
            })
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

    def _intent_context(self) -> dict[str, object]:
        """给理解层的会话上下文：最近几轮、上一个计划摘要、已选对象。

        只读摘要，不含控制事实；模型可据此理解「再下单」「那个冻结」这类指代，
        但槽位仍必须由服务端解析器或用户确认。
        """
        last_plan: list[dict[str, object]] = []
        if self._pending is not None:
            for node in self._pending.plan.nodes:
                keys = (
                    "merchant", "amount_cents", "order_on", "account_id", "payee_id",
                    "subscription_id", "card_id", "product_id", "budget_hold_id",
                )
                last_plan.append({
                    "action": node.action,
                    "params": {key: node.params[key] for key in keys if key in node.params},
                })
        selected: dict[str, object] = {}
        for key, value in (self._last_selected or {}).items():
            if key not in {"payee", "payer", "account", "card", "subscription", "product", "hold"}:
                continue
            if value is None:
                continue
            selected[key] = value.raw if isinstance(value, Value) else value
        return {
            "today": self._deps.today,
            "recent": list(self._recent[-3:]),
            "last_plan": last_plan,
            "selected": selected,
        }

    def _freeform_answers(self, utterance: str) -> dict[str, object]:
        """把聊天框里的短补充解析成待澄清槽位；解析不到任何槽位就返回空。

        只认金额 / 日期 / 收款人 / 简短商户名，不把完整新指令（含动作词）当答案。
        """
        text = utterance.strip()
        if not text or len(text) > 30:
            return {}
        candidate = text
        for prefix in ("收款人", "付款人", "金额", "商户名", "商户", "日期", "时间", "预算"):
            if candidate.startswith(prefix):
                candidate = candidate[len(prefix):].lstrip("：: ")
        if not candidate:
            return {}
        answers: dict[str, object] = {}
        amount = parse_amount(candidate, bare_amounts=True)
        if amount.matched and amount.value is not None:
            answers["amount"] = f"{amount.value.yuan} 元"
        parsed_date = parse_temporal(candidate, self._deps.today)
        if parsed_date.matched and parsed_date.when is not None:
            answers["when"] = parsed_date.when.isoformat()
        payees = tuple(
            record.payload for record in self._deps.gateway.payees_of(self._deps.user_id)
        )
        if any(payee.full_name and payee.full_name in candidate for payee in payees):
            answers["payee"] = candidate
        elif candidate in {"妈妈", "妈", "爸爸", "爸", "爱人", "老婆", "老公"}:
            answers["payee"] = candidate
        if not answers and re.fullmatch(r"[\u4e00-\u9fa5A-Za-z0-9（）()·\s]{1,12}", candidate):
            if not re.search(r"取消|查|转|买|卖|冻结|解冻|订阅|账单|余额", candidate):
                answers["merchant"] = candidate
        return answers

    def _remember_turn(self, utterance: str, intents: Sequence[Intent]) -> None:
        slots = {"merchant", "amount_cents", "budget", "order_on", "payee", "payee_id", "hold"}
        summary = [
            {
                "action": intent.action,
                "slots": {
                    key: (value.raw if isinstance(value, Value) else value)
                    for key, value in intent.slots.items()
                    if key in slots
                },
            }
            for intent in intents
        ]
        entry: dict[str, object] = {"user": utterance, "intents": summary}
        if self._recent and self._recent[-1].get("user") == utterance:
            self._recent[-1] = entry
        else:
            self._recent.append(entry)
            del self._recent[:-3]

    def _assemble_intents(
        self, utterance: str, selected: Mapping[str, object] | None,
    ) -> tuple[tuple[Intent, ...], tuple[ClarificationAsk, ...], tuple[str, ...]]:
        """装配意图，并返回**未采纳提示**（低置信度意图不再静默丢弃，必须让用户看得见）。"""
        observations = self._deps.model_observations
        intent_context = self._intent_context()
        if observations is None:
            proposal = self._deps.intent_model.propose(utterance, intent_context)
        else:
            with observations.scope(self._execution_id):
                proposal = self._deps.intent_model.propose(utterance, intent_context)
        self._planner_channel = proposal.channel or "injected"
        payees = tuple(
            record.payload for record in self._deps.gateway.payees_of(self._deps.user_id)
        )
        cards = tuple(
            record.payload for record in self._deps.gateway.cards_of(self._deps.user_id)
        )
        holds = tuple(
            record.payload for record in self._deps.gateway.active_holds_of(self._deps.user_id)
        )
        # 页面把未选的下拉框以 `null` 提交：显式 null 与「不传」同口径（丢弃空值后
        # 按受信任默认值补齐账户），否则同一句话会白问一轮账户。
        chosen: dict[str, object] = {
            key: value for key, value in (selected or {}).items() if value is not None
        }
        for key, value in _default_selected(self._deps).items():
            chosen.setdefault(key, value)
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
        life_event_id = detect_life_event(utterance)
        candidates = tuple(
            intent for intent in proposal.intents
            if intent.confidence >= CONFIDENCE_THRESHOLD
        )
        dropped.extend(
            intent.action for intent in proposal.intents
            if intent.confidence < CONFIDENCE_THRESHOLD
        )
        if life_event_id == "L1":
            # 「先把预算锁了，别让我乱花」只冻结、不下单：同一句里已有冻结意图，
            # 且没有任何下单 / 订花措辞时，丢掉被「生日」二字带出来的下单意图，
            # 并把重复的冻结意图去重（分句后可能命中多次）。
            order_words = re.search(r"订|买|送|花|蛋糕|礼物|安排|准备|庆祝|惊喜", utterance)
            if order_words is None and any(
                intent.action == "funds.freeze" for intent in candidates
            ):
                kept: list[Intent] = []
                freeze_seen = False
                for intent in candidates:
                    if intent.action == "merchant.order":
                        continue
                    if intent.action == "funds.freeze":
                        if freeze_seen:
                            continue
                        freeze_seen = True
                    kept.append(intent)
                candidates = tuple(kept)
        if not candidates:
            # 页面已受信任选中产品、用户只说「买 1000 元」：模型不给意图时，
            # 由**受信任选中值 + 明确动词**确定性合成申购意图（不猜产品、不猜金额）。
            synthetic = _subscribe_from_page_selection(
                utterance, chosen,
                tuple(
                    record.payload for record in self._deps.gateway.wealth_products()
                ) if chosen.get("product") else (),
            )
            if synthetic is not None:
                candidates = (synthetic,)
                # 同一动作已由确定性合成采纳，再说「另有意图未采纳」会自相矛盾。
                dropped = [action for action in dropped if action != synthetic.action]
        wealth_records = (
            tuple(self._deps.gateway.wealth_products())
            if any(intent.action in WEALTH_ACTIONS for intent in candidates)
            else ()
        )
        products = tuple(record.payload for record in wealth_records)
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
        contexts = _intent_semantic_contexts(
            utterance, spans,
            query_indexes=frozenset(
                index for index, intent in enumerate(candidates)
                if intent.action in {"account.balance", "account.transactions"}
            ),
        )
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
            slot_selected = _per_intent_selected(intent.action, intent_selected, self._deps)
            if intent.action == "subscription.cancel" and "subscription" not in slot_selected:
                matches = [
                    record.payload.subscription_id
                    for record in self._deps.gateway.subscriptions_of(self._deps.user_id)
                    if record.payload.merchant in span
                ]
                if len(matches) == 1:
                    slot_selected["subscription"] = matches[0]
            if (
                intent.action in {"account.balance", "account.transactions"}
                and selected is None
                and re.search(r"(?:这个|那个|这张|那张|该)(?:账户|帐户|银行卡|储蓄卡|卡)", span)
            ):
                # 默认账户不能证明指示词所指；显式选择或澄清答案才可绑定对象。
                slot_selected.pop("account", None)
            fill = fill_control_slots(
                intent.action,
                SlotInputs(
                    text=slot_text,
                    today=self._deps.today,
                    payees=payees,
                    cards=cards,
                    selected=slot_selected,
                    products=products,
                    holds=holds,
                    default_product=(
                        _SUBSCRIBE_DEFAULT_PRODUCT
                        if intent.action == "wealth.subscribe"
                        else None
                    ),
                ),
            )
            if fill.negated:
                continue
            scenario_asks: tuple[ClarificationAsk, ...] = ()
            if life_event_id == "L1" and intent.action == "merchant.order":
                fill, scenario_asks = _l1_fill(fill, span, self._deps.today)
            if (
                intent.action == "funds.release"
                and "hold" not in fill.slots
                and "budget_hold_id" not in fill.slots
                and holds
            ):
                # `hold` 在目录里是可选槽（跨场景预算链自带 budget_hold_id）；
                # 用户单独说「取消冻结」时必须从本人有效冻结里选，不能空着去执行。
                options = tuple(
                    ClarificationChoice(
                        label=f"冻结 {hold.amount.yuan} 元 · {hold.reason}", value=hold.hold_id,
                    )
                    for hold in holds
                )
                scenario_asks = (*scenario_asks, ClarificationAsk(
                    slot="hold", prompt="请选择要解冻的冻结记录。", options=options,
                ))
            # 只过滤「用户已经答过」的槽位；默认值仍要把候选给用户确认一次。
            scenario_asks = tuple(
                ask for ask in scenario_asks if ask.slot not in slot_selected
            )
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
            # 账单可选控制槽走调用方选择，不采纳模型 slots；保持 Value 的信任语义。
            for slot in ("year", "month", "category"):
                if slot not in slots:
                    continue
                value = slots[slot]
                if isinstance(value, Slot):
                    if value.kind is not SlotKind.CONTROL:
                        continue
                    value = value.value
                if isinstance(value, Value):
                    if value.trust is not Trust.TRUSTED:
                        continue
                    value = value.raw
                review_slots = self._review_intents[-1]["slots"]
                if isinstance(review_slots, dict):
                    review_slots[slot] = _review_value(value)
                sources[slot] = (
                    "clarification_answer" if slot in self._clarification_keys else "user_selection"
                )
            slots.setdefault("grant", "explicit")
            slots["span"] = span
            if remark is not None:
                slots["remark"] = Value(remark, Trust.UNVERIFIED)
            _inject_assessment(intent.action, slots, self._deps)
            _precheck_wealth(intent.action, slots, wealth_records, self._deps)
            asks.extend(scenario_asks)
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
                    candidates=self._clarification_choices(question),
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
        self._remember_turn(utterance, intents)
        return tuple(intents), tuple(asks), _dropped_notes(dropped)

    def _clarification_choices(
        self, question: SlotQuestion
    ) -> tuple[ClarificationChoice, ...]:
        """同名候选的可点选版本：补「手机尾号 / 账号尾号 / 最近一次转账」，回填值用 `payee_id`。

        `planning.slots` 已判定「必须澄清」并给出候选标签，本层只补事实（名册 + 已记账流水），
        **不改判定**。名册只存后四位，故不渲染完整号码——补全中间位就是生成事实。

        回填值必须是人名册里的 `payee_id`：`_invalid_selection` 只认本人事实列表中的对象 ID，
        自由文本（哪怕是「王伟 8812」）会被当成越权选择拒绝。
        """
        if not question.candidates:
            return ()
        if question.slot == "hold":
            holds = {
                f"冻结 {record.payload.amount.yuan} 元 · {record.payload.reason}": record.payload
                for record in self._deps.gateway.active_holds_of(self._deps.user_id)
            }
            return tuple(
                ClarificationChoice(label=label, value=hold.hold_id)
                for label in question.candidates
                if (hold := holds.get(label)) is not None
            )
        roster = {
            _payee_label(record.payload): record.payload
            for record in self._deps.gateway.payees_of(self._deps.user_id)
        }
        choices: list[ClarificationChoice] = []
        for label in question.candidates:
            payee = roster.get(label)
            if payee is None:
                continue
            last = self._last_transfer_on(payee)
            when = f"最近一次转账 {last.isoformat()}" if last is not None else "暂无转账记录"
            choices.append(
                ClarificationChoice(
                    label=(
                        f"{payee.full_name} · 手机尾号 {payee.phone_tail}"
                        f" · 账号尾号 {payee.account_tail} · {when}"
                    ),
                    value=payee.payee_id,
                )
            )
        return tuple(choices)

    def _last_transfer_on(self, payee: Payee) -> date | None:
        """该收款人最近一次**已记账**的转出日期（事实来自 01 网关；没有记录返回 `None`）。

        只认 `category=转账 / direction=out` 且有对应商户标记的流水：定时登记（未执行）
        不算「转过」，避免把「计划」说成「事实」。
        """
        merchants = {f"转给{payee.payee_id}", f"转给{payee.full_name}"}
        latest: date | None = None
        start = self._deps.today - timedelta(days=_PAYEE_HISTORY_DAYS)
        for account in self._deps.gateway.accounts_of(payee.user_id):
            for record in self._deps.gateway.transactions_between(
                account.payload.account_id, start, self._deps.today
            ):
                txn = record.payload
                if (
                    txn.category == "转账"
                    and txn.direction == "out"
                    and txn.merchant in merchants
                    and (latest is None or txn.booked_on > latest)
                ):
                    latest = txn.booked_on
        return latest

    def _render_card(
        self, plan: Plan, decision: Decision, *, now: datetime | None = None,
    ) -> PlanCard:
        card = render_plan_card(
            plan, decision, self._deps.gateway,
            expires_at=_naive_moment(now) + timedelta(seconds=PLAN_TTL_S),
            user_id=self._deps.user_id,
        )
        # 相同内容的新提交也不能接受上一轮的确认请求。
        return replace(card, card_id=f"{card.card_id}_{self._execution_id}")

    def _node_decision(self, node: PlanNode, flags: frozenset[str]) -> Decision:
        """单节点口径的兼容入口；产品执行另叠加完整计划风险。"""
        return evaluate(node_policy_request(
            node, spec=self._deps.registry.get(node.action), gateway=self._deps.gateway,
            user_id=self._deps.user_id, today=self._deps.today, flags=flags,
        ))

    def _context_flags(
        self, *, injection_already_tripped: bool = False,
    ) -> frozenset[str]:
        """上下文异常标记的**唯一生产者**，并同步接线熔断触发 2 / 5（**作用域不同**，裁决 #27）。

        - 注入检测命中 → 标记 `injection_detected`（PDP 单向升 `MANUAL`）+ **全局**熔断（触发 2）：
          注入是**系统性**威胁（可能来自任何会话 / 任何入参面），配得上全局拒绝；
        - 服务端可信风控（G-03）判定会话异常 → 标记 `device_change`（PDP 升黄）+ **会话级**熔断
          （触发 5）：设备 / 地点跳变是**单个会话**的信号，只惩罚该会话；
        - **无可信风控结果**（信号源缺失且要求严格，或信号源判定失败）→ 同样按异常升级，
          **不得默认放行**：宁可多一次人工核验，也不把未知当正常。
        """
        flags: set[str] = set()
        # 提交入口已检测并熔断；这里直接沿用结果，不重复扫描同一条输入。
        if injection_already_tripped:
            flags.add(FLAG_INJECTION_DETECTED)
        anomaly = self._session_risk_anomaly()
        if anomaly is None:
            flags.add(FLAG_DEVICE_CHANGE)
            self._trip_session(
                BreakerTrigger.SESSION_ANOMALY, "会话风控无可信结果：升级核验（G-03）"
            )
        elif anomaly:
            flags.add(FLAG_DEVICE_CHANGE)
            self._trip_session(
                BreakerTrigger.SESSION_ANOMALY, "会话异常：设备 / 地点跳变（服务端风控）"
            )
        return frozenset(flags)

    def _session_risk_anomaly(self) -> bool | None:
        """读取**服务端**可信风控结论（G-03）：`True` / `False` / `None`=无可信结果。

        未装配信号源时：严格模式视为「无可信结果」（升级），演示模式视为「无异常信号」；
        装配了信号源但判定失败（抛错）⇒ 一律视为「无可信结果」，不静默放行。
        """
        source = self._deps.session_risk
        if source is None:
            return None if self._deps.session_risk_strict else False
        try:
            return source.assess(user_id=self._deps.user_id, session_id=self._session_id)
        except Exception:
            return None

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

    def _note_verification_auth_failure(self, reason: str) -> None:
        """挑战错码、重放、过期或绑定异常计入全局连续授权失败熔断。"""
        trigger = BreakerTrigger.CONSECUTIVE_AUTH_FAILURES
        was_open = self._deps.breaker.is_open()
        self._deps.breaker.record_failure()
        if not was_open and self._deps.breaker.is_open():
            self._audit(KIND_BREAKER, {
                "trigger": trigger.value,
                "detail": f"强验证挑战异常：{reason}",
                "scope": "global",
                "state": self._deps.breaker.state().value,
                "user_id": self._deps.user_id,
                "session_id": self._session_id,
                "execution_id": self._execution_id,
            })

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
        triage_verdict: TriageVerdict | None = None,
    ) -> SubmissionView:
        """安全拒绝。

        `capability_boundary=True` = 「**未识别 / 无匹配场景**」：它是**能力边界**而非策略拒绝，
        因此**不计入**触发 4（裁决 #27）——否则用户正常的探索输入（演示时随口一句、口音误识别）
        会被当成攻击信号，连着三次就把系统锁死（这正是 `e2e_check.py` 自我污染的根因）。

        `triage_verdict` 非空时**不加**「安全拒绝：」前缀：那三类是「我听不懂 / 办不了」，
        不是风控拒绝，界面也不该盖「已拦截」印章。审计与状态机语义完全不变。
        """
        self._state = SessionState.BLOCKED
        self._message = reason if triage_verdict is not None else f"安全拒绝：{reason}"
        self._takeover = self._build_takeover(reason, decision, plan, card, utterance)
        if not capability_boundary:
            self._note_policy_rejection()
        return SubmissionView(
            self._state, self._message, card, decision, plan, (), triage=triage_verdict
        )

    def _assessment_invitation(self, *, defer: bool = False) -> SubmissionView:
        """「我要做风险测评」⇒ 直接出 5 题问卷（确定性判据，不经模型、不反问等级）。

        `defer=True`：句子里还挂着第二件事（「先做测评，再买理财」）。问卷照出，另加
        一句把第二件事挂回来，避免半句被静默丢掉。
        """
        self._state = SessionState.IDLE
        message = invitation_message()
        if defer:
            message = (
                f"{message}\n"
                "测评完成后，把你后面那句（买哪只、多少钱）再发我一次，我接着办。"
            )
        self._message = message
        return SubmissionView(
            self._state,
            self._message,
            None,
            None,
            None,
            (),
            questionnaire=questionnaire_payload(),
        )

    def _remember_wealth_attempt(self, intents: tuple[Intent, ...]) -> None:
        """记住上一轮理财申购提到的产品（**不论是否执行成功**）。

        只记产品 id，不记金额：金额必须由用户自己再给一次（或由起购门槛说清）。
        """
        for intent in intents:
            if intent.action != "wealth.subscribe":
                continue
            product_id = _slot_str(intent.slots, "product")
            if product_id:
                self._last_wealth = product_id

    def _wealth_recall_answer(self, utterance: str) -> SubmissionView | None:
        """「就买刚才那个」——上一轮被拒的理财申购，**只澄清、不执行**（实测 WEA-Q05）。

        为什么不自动补全：上一轮多半是起购 / 等级没过，替用户改金额等于替他做决定。
        这里把上一只的产品与起购门槛摆出来，让他自己确认。
        """
        if self._last_wealth is None:
            return None
        if _WEALTH_RECALL.search(utterance.strip()) is None:
            return None
        try:
            product = self._deps.gateway.wealth_product(self._last_wealth).payload
        except GatewayNotFoundError:
            return None
        self._state = SessionState.NEEDS_CLARIFICATION
        self._message = (
            f"上一轮说的是「{product.name}」（{product.risk_level}，"
            f"起购 {product.min_amount.yuan} 元），那笔还没有提交成功。"
            f"要办的话请再说一次金额，例如「买 {product.min_amount.yuan} 元 {product.name}」；"
            "我不会替你改金额。"
        )
        hint = f"买 {product.min_amount.yuan} 元 {product.name}"
        return SubmissionView(self._state, self._message, None, None, None, (hint,))

    def _claimed_level_answer(self, utterance: str) -> SubmissionView | None:
        """句子里自述等级 + 选品问法 ⇒ 确定性事实清单（实测 WEA-Q03 的稳定性）。

        走确定性规则而不是等模型，所以同一句话在任何通道下结论一致；
        等级**只允许更保守**：自述 R2 / 记录 R3 ⇒ 按 R2 筛，自述 R5 / 记录 R3 ⇒ 仍按 R3。
        """
        text = utterance.strip()
        if _CLAIMED_LEVEL.search(text) is None:
            return None
        if not is_recommendation_request(text):
            return None
        if len(text) > _WEALTH_SELECTION_MAX_LENGTH:
            return None
        self._state = SessionState.IDLE
        self._message = _catalog_answer(self._deps, claimed=text)
        return SubmissionView(self._state, self._message, None, None, None, ())

    def _wealth_fact_answer(self, utterance: str) -> SubmissionView | None:
        """理财**事实**直答（确定性判据，不经模型）：风险等级 / 收益 / 持仓对账。

        只陈述事实，不给建议、不推算收益；命中即早退，避免被低置信度意图丢掉。
        带动作词的句子（买 / 赎回 / 推荐 / 比较 …）一律交回意图装配，不在此接管。
        """
        text = utterance.strip()
        if len(text) > _WEALTH_FACT_MAX_LENGTH:
            return None
        if _WEALTH_ACTION_WORDS.search(text) is not None:
            return None
        if _ASSESSMENT_QUESTION.search(text):
            answer = _assessment_answer(self._deps)
        elif _INCOME_QUESTION.search(text) or _HOLDINGS_QUESTION.search(text):
            answer = _holdings_answer(self._deps)
        elif _HORIZON_QUESTION.search(text):
            answer = _catalog_answer(
                self._deps,
                horizon_days=parse_horizon_days(text),
                max_amount_cents=_budget_cents(text),
            )
        else:
            return None
        self._state = SessionState.IDLE
        self._message = answer
        return SubmissionView(self._state, self._message, None, None, None, ())

    def _hold_fact_answer(self, utterance: str) -> SubmissionView | None:
        """冻结事实直答：「上次那笔冻结退回来没」不猜 hold id，只陈述当前有效冻结。

        带「取消 / 解冻」动作词的句子交回意图装配（用户要办解冻，不是只问状态）。
        """
        text = utterance.strip()
        if not (_HOLD_QUERY.search(text) and _HOLD_STATUS.search(text)):
            return None
        holds = tuple(
            record.payload for record in self._deps.gateway.active_holds_of(self._deps.user_id)
        )
        if _HOLD_ACTION.search(text):
            # 要解冻：有冻结就交回意图装配（由 hold 候选选择）；没有就直接说清。
            if holds:
                return None
            self._state = SessionState.IDLE
            self._message = "当前没有可解冻的冻结记录：之前冻结的资金已释放，不占用可用余额。"
            return SubmissionView(self._state, self._message, None, None, None, ())
        if not holds:
            answer = (
                "当前没有有效冻结记录：之前冻结的资金已不再占用可用余额。"
                "历史冻结与释放的明细可在「记录与明细」查看。"
            )
        else:
            lines = "；".join(
                f"{hold.reason}（冻结 {hold.amount.yuan} 元）" for hold in holds
            )
            answer = (
                f"当前还有 {len(holds)} 笔有效冻结：{lines}。"
                "这些资金仍占用可用余额；如需退回，请说要解冻哪一笔。"
            )
        self._state = SessionState.IDLE
        self._message = answer
        return SubmissionView(self._state, self._message, None, None, None, ())

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
        待确认计划受**服务端** `expires_at` 约束（G-02）：过期即作废，不得继续确认。
        """
        if steps is not None and (
            isinstance(steps, bool) or not isinstance(steps, int)
            or not 1 <= steps <= MAX_CONFIRM_STEPS
        ):
            raise SessionStateError(
                f"steps 必须是 1..{MAX_CONFIRM_STEPS} 的整数（缺省表示执行全部待执行节点）"
            )
        pending = self._require_pending()
        if self._recovery_blocked:
            raise SessionStateError("业务证据待人工核对，禁止继续执行")
        if self._state is SessionState.COMPLETED:
            if card_id != pending.card.card_id:
                self._note_confirmation_auth_failure(
                    pending,
                    card_id=card_id,
                    frozen_hash=frozen_hash,
                    reason="card_mismatch",
                )
                raise ConfirmationMismatchError("计划卡不匹配")
            try:
                assert_frozen(pending.plan, frozen_hash)
            except StalePlanError:
                self._note_confirmation_auth_failure(
                    pending,
                    card_id=card_id,
                    frozen_hash=frozen_hash,
                    reason="hash_mismatch",
                )
                raise
            self._require_confirmation(pending)
            return self._execution_view()
        if self._state not in (
            SessionState.AWAITING_CONFIRM,
            SessionState.EXECUTING,
        ):
            raise SessionStateError(f"当前状态不允许确认：{self._state.value}")
        self._assert_not_expired(pending, now=now)
        if card_id != pending.card.card_id:
            self._note_confirmation_auth_failure(
                pending,
                card_id=card_id,
                frozen_hash=frozen_hash,
                reason="card_mismatch",
            )
            raise ConfirmationMismatchError(
                f"计划卡不匹配：收到 {card_id}｜待确认 {pending.card.card_id}"
            )
        try:
            assert_frozen(pending.plan, frozen_hash)
        except StalePlanError:
            self._note_confirmation_auth_failure(
                pending,
                card_id=card_id,
                frozen_hash=frozen_hash,
                reason="hash_mismatch",
            )
            raise
        proof = self._strong_verification_proof
        strong_verified = proof is not None and proof.binding == self._verification_binding(pending)
        if PROMPT_STRONG_VERIFY in pending.card.requires and not strong_verified:
            self._assert_strong_verification(
                pending, verification_code, now=now if now is not None else datetime.now(UTC)
            )
            proof = self._strong_verification_proof
            strong_verified = proof is not None
        self._confirmation = ConfirmationRecord(
            plan=pending.plan, frozen_hash=pending.card.frozen_hash,
            execution_id=self._execution_id, approved_level=pending.card.level,
            strong_verified=strong_verified,
        )
        blocked_by_breaker = self._breaker_blocks(pending)
        try:
            self._audit(KIND_DECISION, {
                "kind": "confirmation",
                "status": "blocked" if blocked_by_breaker else "accepted",
                "user_id": self._deps.user_id,
                "session_id": self._session_id,
                "execution_id": self._execution_id,
                "card_id": pending.card.card_id,
                "frozen_hash": pending.card.frozen_hash,
                "decision_level": pending.decision.level.name,
                "strong_verified": strong_verified,
            })
        except BaseException:
            self._confirmation = None
            self._strong_verification_proof = None
            raise
        if blocked_by_breaker:
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
        self._assert_not_expired(pending, now=now)
        if PROMPT_STRONG_VERIFY not in pending.card.requires:
            raise StrongVerificationRequiredError("当前计划卡无需强验证")
        moment = now if now is not None else datetime.now(UTC)
        self._assert_verification_allowed(pending, now=moment)
        self._invalidate_verification("replaced")
        self._verification_audit("requested", pending)
        try:
            if self._deps.verification_strict and getattr(
                self._verification_provider, "mode", "demo"
            ) != VerificationMode.EXTERNAL:
                raise VerificationError("delivery_unavailable")
            challenge, code = create_challenge(
                self._verification_binding(pending), key=self._verification_key, now=now,
            )
            delivery = self._verification_provider.deliver(
                challenge.binding, challenge.challenge_id, code, challenge.expires_at,
            )
            if self._deps.verification_strict and delivery.mode != VerificationMode.EXTERNAL:
                raise VerificationError("delivery_unavailable")
        except VerificationError as error:
            self._challenge = None
            self._verification_audit("failed", pending, reason=error.reason)
            raise StrongVerificationRequiredError(
                "强验证投递失败，请检查服务配置后重新申请"
            ) from None
        except Exception:
            self._challenge = None
            self._verification_audit("failed", pending, reason="delivery_failed")
            raise StrongVerificationRequiredError("强验证投递失败，请稍后重新申请") from None
        self._challenge = replace(challenge, provider_reference=delivery.reference)
        try:
            self._verification_audit(
                "delivered", pending, mode=delivery.mode.value, channel=delivery.channel,
                target=delivery.target,
            )
        except BaseException:
            self._challenge = invalidate_challenge(self._challenge)
            self._strong_verification_proof = None
            raise
        return VerificationPrompt(
            channel=delivery.channel,
            target=delivery.target,
            code=delivery.code if delivery.mode is VerificationMode.DEMO else None,
            expires_in_s=VERIFICATION_TTL_S,
            mode=delivery.mode.value,
        )

    def _assert_verification_allowed(self, pending: _Pending, *, now: datetime) -> None:
        """失败次数闸门：锁定期 / 退避中拒绝，且**不比较**验证码。

        计数按用户保存在服务端（`AppDeps.verification_limiter`），所以重新申请验证码、
        轮换挑战、过期或更换会话都不能把失败次数清零。
        """
        try:
            self._verification_limiter.check(self._deps.user_id, now=now)
        except VerificationError as error:
            self._verification_audit("throttled", pending, reason=error.reason)
            message = (
                "验证码错误次数过多，请稍后再试"
                if error.reason == "locked" else "验证码尝试过于频繁，请稍后再试"
            )
            raise StrongVerificationRequiredError(message) from None

    def _assert_strong_verification(
        self, pending: _Pending, code: str | None, *, now: datetime
    ) -> None:
        """红色闸门：必须提交**服务端下发**的验证码，且未过期、未使用、绑定当前冻结计划。"""
        challenge = self._challenge
        if challenge is None:
            raise StrongVerificationRequiredError(
                "红色（大额 / 不可逆）需强验证：请先申请验证码"
            )
        # 新验证尝试先清理旧绑定证明，签发或审计失败后不得残留授权。
        self._strong_verification_proof = None
        try:
            consumed_challenge = self._verification_limiter.verify_attempt(
                self._deps.user_id,
                now=now,
                compare=lambda: verify_challenge(
                    challenge, self._verification_binding(pending), code,
                    key=self._verification_key, now=now,
                ),
            )
        except VerificationError as error:
            if error.reason in {"locked", "throttled"}:
                self._verification_audit("throttled", pending, reason=error.reason)
                message = (
                    "验证码错误次数过多，请稍后再试"
                    if error.reason == "locked" else "验证码尝试过于频繁，请稍后再试"
                )
                raise StrongVerificationRequiredError(message) from None
            if error.reason == "expired":
                self._challenge = replace(challenge, status=ChallengeStatus.EXPIRED)
            failures = (
                self._verification_limiter.failures(self._deps.user_id)
                if error.reason == "invalid_code" else None
            )
            if error.reason in {"invalid_code", "binding_mismatch", "inactive", "expired"}:
                self._note_verification_auth_failure(error.reason)
            self._verification_audit(
                "expired" if error.reason == "expired" else "failed", pending,
                reason=error.reason, attempts=failures,
            )
            message = {
                "expired": "验证码已过期：请重新申请",
                "binding_mismatch": "验证码与当前计划不匹配：请重新申请",
                "inactive": "验证码已失效：请重新申请",
            }.get(error.reason, "验证码不正确")
            raise StrongVerificationRequiredError(message) from None
        self._challenge = consumed_challenge
        try:
            if not self._verification_key:
                raise StrongVerificationRequiredError("强验证密钥不可用，拒绝签发授权证明")
            proof = HmacStrongVerificationProofVerifier(self._verification_key).issue(
                self._challenge
            )
            self._verification_audit("consumed", pending)
            self._verification_limiter.reset(self._deps.user_id)
        except BaseException:
            self._strong_verification_proof = None
            raise
        self._strong_verification_proof = proof

    def _verification_binding(self, pending: _Pending) -> VerificationBinding:
        return VerificationBinding(
            self._deps.user_id, self._session_id, self._execution_id,
            pending.card.card_id, pending.card.frozen_hash,
        )

    def _verification_audit(self, status: str, pending: _Pending, **details: object) -> None:
        self._audit(KIND_DECISION, {
            "kind": "verification", "status": status,
            "user_id": self._deps.user_id, "session_id": self._session_id,
            "execution_id": self._execution_id, "card_id": pending.card.card_id,
            "frozen_hash": pending.card.frozen_hash,
            "challenge_id": self._challenge.challenge_id if self._challenge else None,
            **details,
        })

    def _note_confirmation_auth_failure(
        self,
        pending: _Pending,
        *,
        card_id: str,
        frozen_hash: str,
        reason: str,
    ) -> None:
        """记录伪造确认并计入全局连续授权失败熔断。"""
        self._audit(KIND_DECISION, {
            "kind": "confirmation",
            "status": "failed",
            "reason": reason,
            "user_id": self._deps.user_id,
            "session_id": self._session_id,
            "execution_id": self._execution_id,
            "card_id": pending.card.card_id,
            "frozen_hash": pending.card.frozen_hash,
            "supplied_card_id": card_id,
            "supplied_frozen_hash": frozen_hash,
        })
        self._note_verification_auth_failure(f"confirmation_{reason}")

    def _invalidate_verification(self, reason: str) -> None:
        self._strong_verification_proof = None
        if self._challenge is not None and self._challenge.status is ChallengeStatus.ACTIVE:
            self._challenge = invalidate_challenge(self._challenge)
            if self._pending is not None:
                self._verification_audit("replaced", self._pending, reason=reason)

    def _assert_not_expired(self, pending: _Pending, *, now: datetime | None) -> None:
        """服务端有效期闸门（G-02）：过期 ⇒ 计划作废、状态落 `BLOCKED`，且不得再执行。

        「作废」是**服务端终态转换**：清空待确认计划、绑定确认与待执行节点，并让未使用的强验证
        挑战失效；客户端无法把它改回待确认状态，只能重新提交（重新走裁决与冻结）。
        """
        expires_at = pending.card.expires_at
        if expires_at is None:
            raise SessionStateError("待确认计划缺少服务端有效期：拒绝执行，请重新提交")
        if _naive_moment(now) < expires_at:
            return
        self._invalidate_verification("plan_expired")
        self._audit(KIND_DECISION, {
            "kind": "plan_expired",
            "user_id": self._deps.user_id,
            "session_id": self._session_id,
            "execution_id": self._execution_id,
            "card_id": pending.card.card_id,
            "frozen_hash": pending.card.frozen_hash,
            "expires_at": expires_at.isoformat(),
            "state_before": self._state.value,
        })
        self._pending = None
        self._confirmation = None
        self._pending_node_ids = []
        self._quota = None
        self._state = SessionState.BLOCKED
        self._message = "计划已过期：原计划不可再执行，请重新提交"
        raise SessionStateError(self._message)

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
        self._hook_status = ""
        for node_id in remaining:
            self._require_confirmation(pending)
            if self._breaker_blocks(pending):
                self._enter_takeover("熔断打开：非纯查询转人工接管", pending)
                return self._execution_view()
            node = node_map[node_id]
            self._assert_dependencies_done(node)
            call = self._to_call(node)
            request_key = f"{self._execution_id}:{pending.card.frozen_hash}:{node_id}"
            is_query = self._deps.registry.get(node.action).is_pure_query
            if not is_query:
                self._in_flight = InFlightNode(
                    node_id, request_key, business_key_for(call, request_key, self._deps.user_id),
                    node.action, manifest_hash(call),
                )
                # 此检查点失败时绝不能开始业务写入。
                self._persist()
            try:
                # 写操作必须携带**确认后**由服务端签发的一次性凭据（Pre-Tool Hook 强制）。
                execution = None if is_query else self._deps.pep.issue_write_grant(
                    call,
                    pending.decision,
                    pending.plan,
                    user_id=self._deps.user_id,
                    session_id=self._session_id,
                    execution_id=self._execution_id,
                    card_id=pending.card.card_id,
                    approved_level=confirmation.approved_level,
                    verification_proof=self._strong_verification_proof,
                    today=self._deps.today,
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
                )
                result = self._deps.pep.execute(
                    call,
                    pending.decision,
                    pending.plan,
                    request_key,
                    execution=execution,
                    user_id=self._deps.user_id,
                    session_id=self._session_id,
                    execution_id=self._execution_id,
                )
            except WritePreconditionError as error:
                self._in_flight = None
                self._refresh_authorization(error.result)
                return self._execution_view()
            except ToolHookBlocked as error:
                # 统一写操作 Hook 拦截：业务写服务从未被调用，如实回报状态，不得谎报成功。
                self._in_flight = None
                self._hook_status = error.status.value
                self._message = f"写操作被安全策略拦截（{node.action}）：{error.reason}"
                self._state = (
                    SessionState.PARTIAL if self._completed_node_ids else SessionState.FAILED
                )
                self._release_open_holds(reason="tool_hook_blocked")
                return self._execution_view()
            except CommittedWriteError as error:
                self._unverified_results[node_id] = ExecResult(
                    status="committed_unverified", record_ids=error.receipt.record_ids,
                    message=f"业务已提交，后续核验或审计未完成：{error}",
                    action=node.action,
                )
                self._message = "业务已提交，核验或审计未完成；已暂停后续步骤，可恢复核对回执"
                self._state = SessionState.INTERRUPTED
                return self._execution_view()
            except (NotAuthorizedError, GatewayError) as error:
                self._in_flight = None
                self._message = f"执行受阻（{node.action}）：{error}"
                self._state = (
                    SessionState.PARTIAL if self._completed_node_ids else SessionState.FAILED
                )
                self._release_open_holds(reason="gateway_or_authorization_failed")
                return self._execution_view()
            except Exception as error:
                # 未预期异常同样属于「本计划不会再有后续成功」：先把已冻结预算退回，再如实报错，
                # 绝不把冻结额悬空在核心银行。
                self._in_flight = None
                self._message = f"执行失败（{node.action}）：{type(error).__name__}: {error}"
                self._state = (
                    SessionState.PARTIAL if self._completed_node_ids else SessionState.FAILED
                )
                self._audit(
                    "execution_error",
                    {
                        "node_id": node_id,
                        "action": node.action,
                        "error": f"{type(error).__name__}: {error}",
                        "user_id": self._deps.user_id,
                        "session_id": self._session_id,
                        "execution_id": self._execution_id,
                    },
                )
                self._release_open_holds(reason="unexpected_error")
                return self._execution_view()
            self._unverified_results.pop(node_id, None)
            self._exec_results.append(result)
            self._completed_node_ids.append(node_id)
            self._audit_ids.append(self._deps.audit.head())
            self._in_flight = None
            # 下一节点必须等待完成结果已落盘。
            self._persist()
        if len(self._completed_node_ids) == len(pending.node_ids):
            self._state = SessionState.COMPLETED
            self._message = f"计划执行完成（{len(self._completed_node_ids)} 步）"
            # 只有**全部执行成功**才记为「上一笔」：半成功不得被「再转一次」复刻。
            self._remember(pending.plan)
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
        self._invalidate_verification("plan_changed")
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

    def _release_open_holds(self, *, reason: str) -> tuple[str, ...]:
        """终态失败收尾：把本次执行已冻结的预算退回可用余额（不静默吞钱）。

        调用时机：**本计划不会再有后续成功**（安全策略拦截 / 执行受阻 / 未预期异常 /
        用户放弃 / 会话重置）。`WritePreconditionError`（可重新确认后继续）与
        `CommittedWriteError`（可恢复核对后重试）**不**收尾，否则会抽走后续节点依赖的冻结额。

        返回实际释放的记录；审计携带用户 / 会话 / 执行身份与原因，便于对账。
        """
        records = tuple(
            record_id
            for result in self._exec_results
            for record_id in result.record_ids
            if record_id.startswith("holds:") and record_id not in self._released_holds
        )
        # 只释放**仍处于冻结中**的记录：已完成计划里的冻结可能已由 `funds.release` 解冻，
        # 重复解冻是幂等空操作，不应计为一次新的退回。
        records = tuple(record for record in records if self._hold_is_active(record))
        if not records:
            return ()
        context: dict[str, object] = {
            "subject": "holds",
            "reason": reason,
            "user_id": self._deps.user_id,
            "session_id": self._session_id,
            "execution_id": self._execution_id,
            "requested": list(records),
        }
        service = self._deps.reversal
        if service is None:
            self._message = f"{self._message}；冻结预算未释放（未装配冲正通道）"
            self._audit(KIND_REVERSAL, {**context, "outcome": "release_unavailable"})
            return ()
        try:
            released = service.compensate(records)
        except CompensationError as error:
            self._message = f"{self._message}；冻结预算释放失败：{error}"
            self._audit(
                KIND_REVERSAL,
                {**context, "outcome": "release_failed", "error": str(error)},
            )
            return ()
        self._released_holds.update(records)
        self._message = f"{self._message}；已释放冻结预算 {len(released)} 笔"
        self._audit(
            KIND_REVERSAL,
            {**context, "outcome": "released_on_failure", "records": list(released)},
        )
        return tuple(released)

    def _hold_is_active(self, record: str) -> bool:
        """冻结记录是否仍占用可用余额（读不到时按「需要释放」处理，交由冲正通道如实报错）。"""
        hold_id = record.partition(":")[2]
        if not hold_id:
            return False
        try:
            return self._deps.gateway.hold(hold_id).payload.status == "active"
        except Exception:
            return True

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
        if self._recovery_blocked:
            raise SessionStateError("业务证据待人工核对，禁止继续执行")
        self._assert_not_expired(pending, now=None)
        if self._confirmation is None:
            self._state = SessionState.AWAITING_CONFIRM
            self._message = "计划尚未确认，请先确认当前计划卡"
            return self._execution_view()
        self._require_confirmation(pending)
        return self._execute(steps=None)

    @_serialized
    def reject(self) -> SessionView:
        """用户**主动放弃**当前待确认计划（G-02 的显式拒绝入口）。

        服务端把计划置为作废终态：清空待确认计划 / 绑定确认 / 待执行节点，并失效未使用的强验证
        挑战。之后同一 `card_id` 的确认、恢复与验证码申请一律失败，只能重新提交。
        """
        pending = self._require_pending()
        if self._state not in (SessionState.AWAITING_CONFIRM, SessionState.INTERRUPTED):
            raise SessionStateError(f"当前状态不可放弃：{self._state.value}")
        self._invalidate_verification("plan_rejected")
        self._message = ""
        released = self._release_open_holds(reason="plan_rejected")
        release_note = self._message
        self._audit(KIND_DECISION, {
            "kind": "plan_rejected",
            "user_id": self._deps.user_id,
            "session_id": self._session_id,
            "execution_id": self._execution_id,
            "card_id": pending.card.card_id,
            "frozen_hash": pending.card.frozen_hash,
            "state_before": self._state.value,
        })
        self._pending = None
        self._confirmation = None
        self._pending_node_ids = []
        self._quota = None
        self._state = SessionState.IDLE
        self._message = "已放弃当前计划：原计划作废，如仍需办理请重新提交"
        if released:
            self._message += f"；已退回冻结预算 {len(released)} 笔"
        elif release_note:
            self._message += release_note
        return SessionView(self._state, self._message)

    def _execution_view(self) -> ExecutionView:
        frozen_hash = self._pending.card.frozen_hash if self._pending is not None else ""
        results = (*self._exec_results, *self._unverified_results.values())
        bill_advice: tuple[BillAdvice, ...] = ()
        bill_advice_message = ""
        bill_findings: Mapping[str, object] | None = None
        try:
            bill_advice = self._completed_bill_advice()
        except Exception:
            # 只隔离附加建议：存储或计算失败不能掩盖已完成的业务回执。
            bill_advice_message = "消费建议暂不可用，请稍后重新分析账单。"
        try:
            bill_findings = self._completed_bill_findings()
        except Exception:
            # 识别失败同样只影响附加信息，不影响业务回执与账期事实。
            bill_findings = None
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
            plan=self._pending.plan if self._pending is not None else None,
            node_progress=self._node_progress(),
            bill_advice=bill_advice,
            bill_advice_message=bill_advice_message,
            bill_findings=bill_findings,
            hook_status=self._hook_status,
        )

    def _node_progress(self) -> tuple[NodeProgress, ...]:
        """逐节点执行状态：由会话的完成记录唯一决定，前端不得自行推导。"""
        pending = self._pending
        if pending is None:
            return ()
        done = set(self._completed_node_ids)
        unverified = set(self._unverified_results)
        stopped = self._state in {
            SessionState.FAILED,
            SessionState.PARTIAL,
            SessionState.REVERSED,
            SessionState.BLOCKED,
            SessionState.TAKEN_OVER,
        }
        rows: list[NodeProgress] = []
        for node in pending.plan.nodes:
            if node.node_id in done:
                status = (
                    "reversed"
                    if self._state is SessionState.REVERSED
                    else "completed"
                )
            elif node.node_id in unverified:
                status = "unverified"
            elif self._state is SessionState.INTERRUPTED:
                status = "suspended"
            elif stopped:
                status = "skipped"
            else:
                status = "pending"
            rows.append(NodeProgress(node_id=node.node_id, status=status))
        return tuple(rows)

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

    def _completed_bill_findings(self) -> Mapping[str, object] | None:
        """执行完成的月账单附带确定性识别结果；只读，不调用模型。"""
        if self._state is not SessionState.COMPLETED or self._pending is None:
            return None
        for node in self._pending.plan.nodes:
            if node.action == "bills.monthly" and node.node_id in self._completed_node_ids:
                report = analyze_bill(
                    self._deps.gateway, self._deps.user_id,
                    int(str(node.params["year"])), int(str(node.params["month"])),
                    as_of=self._deps.today,
                )
                return report_payload(report)
        return None

    def _reset(self) -> None:
        if self._recovery_blocked or self._unverified_results:
            raise SessionStateError("存在待核对业务回执，不能用新计划覆盖")
        # 上一计划若被中断 / 失败后直接重新提交，已冻结的预算必须先退回，否则新计划一开
        # 就永久悬空一笔冻结额。
        self._message = ""
        self._release_open_holds(reason="session_reset")
        release_note = self._message
        self._invalidate_verification("plan_changed")
        self._execution_id = secrets.token_hex(16)
        self._state = SessionState.IDLE
        self._message = "会话已重置" + release_note
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
        self._confirmation_history = []
        self._in_flight = None
        self._recovery_blocked = False
        self._planner_channel = None
        self._model_calls = []
        self._released_holds.clear()


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


def _notify(on_stage: Callable[[str], None] | None, stage: str) -> None:
    """阶段旁路通知：进度写失败不得影响状态机（观测层永远是从属的）。"""

    if on_stage is None:
        return
    try:
        on_stage(stage)
    except Exception:  # noqa: BLE001 - 观测失败必须静默
        return


@dataclass(frozen=True)
class _RefusalCopy:
    """守卫命中的拒绝文案 + 是否算「能力边界」（不计会话级触发 4）。"""

    message: str
    capability_boundary: bool = False


def _guard_utterance(utterance: str) -> _RefusalCopy | None:
    """确定性输入守卫：命中即**不进入规划**，直接安全拒绝。

    文案分两种，拒绝结论完全一样（BLOCKED / 不产生计划 / 审计留痕）：

    - `capability_boundary=True`：用户在问常识或索取做不到的东西（保本吗 / 念回执 / 翻倍），
      正面解释清楚即可，**不计**会话级触发 4；
    - 策略型（索取收益承诺、伪造结果）：照旧计数。

    「忽略所有规则 / 不用验证」按**加严**处理：仍是策略拒绝，会计数——
    熔断只加严不降级。真正的注入拦截在此之前由 `detect_injection` 负责。
    """
    if _SKIP_VERIFY.search(utterance):
        return _RefusalCopy(_NO_SKIP_VERIFY_REFUSAL)
    if _FAKE_RECEIPT.search(utterance):
        return _RefusalCopy(_NO_FAKE_RECEIPT_REFUSAL, capability_boundary=True)
    if _DOUBLE_MONEY.search(utterance):
        return _RefusalCopy(_NO_DOUBLE_REFUSAL, capability_boundary=True)
    if "保本" in utterance and _GUARANTEE_QUESTION.search(utterance):
        return _RefusalCopy(_NOT_GUARANTEED_REFUSAL, capability_boundary=True)
    if (
        _SCAM_LURE.search(utterance)
        and _SCAM_CANCEL.search(utterance)
        and _SCAM_TARGET.search(utterance)
    ):
        # 诈骗话术是**能力边界**而不是策略拒绝：不惩罚受害者，也不计入会话熔断。
        return _RefusalCopy(_SCAM_REFUSAL, capability_boundary=True)
    if any(hint in utterance for hint in FORBIDDEN_HINTS):
        return _RefusalCopy(_RETURN_PROMISE_REFUSAL)
    if any(marker in utterance for marker in FABRICATION_MARKERS):
        return _RefusalCopy(_FABRICATION_REFUSAL)
    if "别管商户有没有回复" in utterance or "商户已经取消" in utterance:
        return _RefusalCopy(_MERCHANT_CANCEL_REFUSAL)
    if (
        "第三方商户" in utterance
        and ("退回" in utterance or "退款" in utterance)
        and "保证" in utterance
    ):
        return _RefusalCopy(_REFUND_GUARANTEE_REFUSAL)
    return None


class _WealthRefusal(Exception):
    """意图装配阶段的理财拒绝（先拒后问）：由 `_submit` 转成安全拒绝视图。"""


_THIRD_PARTY_MARKERS = (
    "老婆", "老公", "媳妇", "爱人", "妻子", "丈夫", "爸爸", "妈妈", "父亲", "母亲",
    "儿子", "女儿", "朋友", "同事", "别人", "他人", "亲戚", "家人",
)
_ACCOUNT_MARKERS = ("账户", "帐户", "银行卡", "储蓄卡", "卡")
_WEALTH_MARKERS = ("买", "申购", "认购", "赎回", "理财", "产品")
_THIRD_PARTY_REFUSAL = (
    "能力边界：仅支持操作你本人名下的账户与银行卡，不支持代他人申购或赎回理财；"
    "如需帮家人办理，请由账户本人登录后操作。"
)


def _third_party_account_refusal(utterance: str) -> str | None:
    """第三人账户的**能力边界**话术（不是策略拒绝，不计触发 4）。

    三条件同时满足才算：第三人标记 + 账户 / 卡标记 + 理财动作词。只出现
    「帮朋友转账」这类不带理财动作的句子不受影响。
    """
    if not any(marker in utterance for marker in _THIRD_PARTY_MARKERS):
        return None
    if not any(marker in utterance for marker in _ACCOUNT_MARKERS):
        return None
    if not any(marker in utterance for marker in _WEALTH_MARKERS):
        return None
    return _THIRD_PARTY_REFUSAL


_BALANCE_MARKERS = ("余额", "存款")
_BALANCE_TAMPER_VERBS = (
    "改成", "改为", "改到", "改掉", "改一下",
    "设为", "设置成", "设置",
    "调到", "调成", "调整成", "调整到",
    "修改成", "修改为", "修改",
)
_BALANCE_TAMPER_REFUSAL = (
    "能力边界：不能修改账户余额（余额只能由真实资金变动产生）；"
    "我只能查询余额，并按你账户里的真实可用资金申购或赎回理财。"
)


def _balance_tamper_refusal(utterance: str) -> str | None:
    """「改余额」的**能力边界**话术（不是策略拒绝，不计触发 4）。

    只要求「余额 / 存款标记 + 篡改动词」：改余额本身在任何场景都不被支持，
    不再要求句子里同时出现理财动作——那会让「把余额改成 100 万」漏到未知意图。
    """
    if not any(marker in utterance for marker in _BALANCE_MARKERS):
        return None
    if not any(verb in utterance for verb in _BALANCE_TAMPER_VERBS):
        return None
    return _BALANCE_TAMPER_REFUSAL


_WEALTH_FACT_MAX_LENGTH = 24
"""理财事实直答的长度上界：超过即视为还有别的事，交回意图装配。"""

_WEALTH_ACTION_WORDS = re.compile(
    r"买|申购|认购|赎回|卖|转|付|扣|改|退|推荐|哪只|哪款|比较|对比"
)
_ASSESSMENT_QUESTION = re.compile(r"风险(?:测评)?(?:等级|级别)|测评等级")
_INCOME_QUESTION = re.compile(r"(?:收益|赚|盈亏|赔)[^，,。；;]{0,6}(?:多少|几|如何|怎么样)")
_HOLDINGS_QUESTION = re.compile(r"持仓|持有")
_HORIZON_QUESTION = re.compile(r"闲钱|多久不用|多久之内|放什么|闲置|半年内|几个月不用")
_CLAIMED_LEVEL = re.compile(r"R([1-5])", re.IGNORECASE)
_RISK_KEYS = ("R1", "R2", "R3", "R4", "R5")
_WEALTH_SELECTION_SIGNAL = re.compile(r"理财|产品|R[1-5]", re.IGNORECASE)
_WEALTH_SELECTION_MAX_LENGTH = 40
_WEALTH_RECALL = re.compile(r"(?:就|要)?买(?:刚才|上一)(?:那个|只|款)?|刚才那个|上一只|上一款")


def _assessment_answer(deps: AppDeps) -> str:
    """本人风险测评事实：等级 / 日期 / 有效期 / 来源（seed 必须如实标注）。"""
    if deps.risk is None:
        return "当前没有可用的风险测评数据源，无法给出你的风险测评等级。"
    assessment = to_risk_assessment(deps.risk.assessment_for(deps.user_id))
    if assessment is None:
        return "你还没有完成风险测评；未测评时不能申购理财，可以先做一次 5 题测评。"
    return describe_assessment(assessment, deps.today)


def _holdings_answer(deps: AppDeps) -> str:
    """持仓与收益口径：金额与起息日来自网关；收益为**模拟净值**折算的参考值，缺数据标未知。

    末尾给一句**对账结论**：合计由同一批持仓事实相加得出，所以能明确说「与本人持仓事实一致」；
    用户觉得对不上时，再按产品名 / 金额逐笔核对（实测 WEA-Q18 只要明细、没结论）。
    """
    rows = holdings_payload(deps.gateway, deps.user_id)
    if not rows:
        return "你当前没有理财持仓（演示数据）。"
    lines = ["你的理财持仓（演示数据）："]
    lines.extend(
        f"- {row['name']}（{row['risk_level']}）{row['amount_yuan']} 元"
        f"，申购日 {row['purchased_on']}，{row['current_income']}"
        for row in rows
    )
    total_cents = 0
    for row in rows:
        total_cents += round(float(str(row["amount_yuan"])) * 100)
    lines.append(
        f"对账结论：{len(rows)} 笔合计 {Money(total_cents).yuan} 元，"
        "与本人持仓事实合计一致；如果你在别处看到不一样，请指出是哪一笔，我按事实逐笔核对。"
    )
    return "\n".join(lines)


def _catalog_answer(
    deps: AppDeps,
    *,
    claimed: str = "",
    horizon_days: int | None = None,
    max_amount_cents: int | None = None,
) -> str:
    """按本人等级列产品**事实**：等级 / 期限 / 起购。

    不排序收益、不预测涨跌、不替用户选。「多久不用」「手里多少钱」只用来**收紧**候选：
    期限 / 起购超过用户自述的上限就剔除，期限仍由用户自己对照，不做收益排序。

    `claimed` 是用户原话：句子里自述的等级**只允许更保守**。
    自述 R2 而服务端记录 R3 ⇒ 按 R2 筛（更严，绝不放宽）；
    自述 R5 而记录 R3 ⇒ 仍按 R3 筛，并说明不按自述放宽。
    """
    if deps.risk is None:
        return "当前没有可用的风险测评数据源，无法判断哪些产品适合你。"
    assessment = to_risk_assessment(deps.risk.assessment_for(deps.user_id))
    if assessment is None:
        return "你还没有完成风险测评：未测评时任何产品都不能申购，先做一次 5 题测评。"
    level = assessment.risk_profile
    note = ""
    match = _CLAIMED_LEVEL.search(claimed)
    if match is not None:
        stated = f"R{match.group(1)}"
        if stated < level and stated in _RISK_KEYS:
            note = (
                f"\n（你说的是 {stated}，比服务端记录的 {level} 更保守，这里按 {stated} 筛。）"
            )
            level = stated
        elif stated != level:
            note = (
                f"\n（你说的是 {stated}，但服务端记录是 {level}；"
                f"等级不能按自述放宽，仍按 {level} 筛。）"
            )
    pool = filter_candidates(
        recommend_products(deps.gateway.wealth_products(), level),
        horizon_days,
        max_amount_cents,
    )
    bounds: list[str] = []
    if horizon_days is not None:
        bounds.append(f"期限 ≤ {horizon_days} 天")
    if max_amount_cents is not None:
        bounds.append(f"起购 ≤ {Money(max_amount_cents).yuan} 元")
    scope = f"，再按 {'、'.join(bounds)} 筛" if bounds else ""
    if not pool:
        return f"你的测评等级 {level} 下，当前条件下暂无适配产品（{scope or '无额外筛选'}）。{note}"
    lines = [f"按你的测评等级 {level}，可申购 {len(pool)} 只（只列事实{scope}）："]
    lines.extend(
        f"- {item.payload.name}（{item.payload.risk_level}）期限 {item.payload.term_days} 天"
        f"，起购 {item.payload.min_amount.yuan} 元"
        for item in pool
    )
    lines.append("期限请你对照这笔钱真正闲置的时间；收益我不预测、不排序，也不替你决定买哪只。")
    return "\n".join(lines) + note


_SUBSCRIBE_VERB = re.compile(r"买|申购|认购")
_HAS_DIGIT = re.compile(r"\d")


def _budget_cents(text: str) -> int | None:
    """句子里用户自己说的金额（只当**上限**用）：读不到就 `None`，绝不推断。"""
    parsed = parse_amount(text)
    if not parsed.matched or parsed.value is None:
        return None
    return parsed.value.cents


def _wealth_selection_fallback(utterance: str, deps: AppDeps) -> str | None:
    """选品问法在没有装配出意图时的**确定性**兜底（实测 WEA-Q03：同句 1/3 次失败）。

    判定全在本地规则里，不依赖模型置信度，所以同一句话连跑多次结论一致；
    产出仍是只读事实清单，不生成任何申购计划。
    """
    text = utterance.strip()
    if not text or len(text) > _WEALTH_SELECTION_MAX_LENGTH:
        return None
    if not is_recommendation_request(text):
        return None
    if _WEALTH_SELECTION_SIGNAL.search(text) is None:
        return None
    return _catalog_answer(deps, claimed=text)


def _subscribe_from_page_selection(
    utterance: str, chosen: Mapping[str, object], products: Sequence[WealthProduct],
) -> Intent | None:
    """页面已受信任选中产品 + 明确动词 + 金额 ⇒ 确定性合成申购意图。

    **只**在模型没有给出任何可用意图时兜底，且产品必须命中目录；金额仍由槽位解析器
    从原文读取，这里不猜金额、不猜产品。
    """
    product = chosen.get("product")
    if not isinstance(product, str) or not product:
        return None
    if not any(item.product_id == product for item in products):
        return None
    if _SUBSCRIBE_VERB.search(utterance) is None or _HAS_DIGIT.search(utterance) is None:
        return None
    return Intent(action="wealth.subscribe", confidence=1.0, evidence=utterance)


def _slot_str(slots: Mapping[str, object], key: str) -> str | None:
    value = slots.get(key)
    if isinstance(value, Value):
        value = value.raw
    return value if isinstance(value, str) else None


def _precheck_wealth(
    action: str,
    slots: Mapping[str, object],
    records: Sequence[Record[WealthProduct]],
    deps: AppDeps,
) -> None:
    """先拒后问：产品已定但适当性不通过时，不等用户补金额就先给拒绝与替代。

    只覆盖 `wealth.subscribe`（申购闸门）；赎回不做适当性判定。
    """
    if action != "wealth.subscribe" or not records:
        return
    product_id = _slot_str(slots, "product")
    product = next(
        (record.payload for record in records if record.payload.product_id == product_id),
        None,
    )
    if product is None:
        return
    raw = slots.get("assessment")
    assessment = raw if isinstance(raw, RiskAssessment) else None
    reason = suitability_refusal(assessment, product, records, deps.today)
    if reason is not None:
        raise _WealthRefusal(reason)


_MEASUREMENT_ONLY_MAX_LENGTH = 24
"""整句只问测评的长度上界：超过即视为还有别的事，交回意图装配。"""


def _is_assessment_request(utterance: str) -> bool:
    """命中测评话术即「先做测评」（长度仍设上界，避免把整段长句当测评）。

    与旧判据 `_is_pure_assessment_request` 的差别：**不再**因为句子里带第二件事的
    衔接词（逗号 / 再 / 然后 / 顺便 / 并且）就整句交回意图装配——那样
    「我想先做个风险测评，再买理财」会被当成申购去追问金额，把「先做测评」吞掉。
    第二件事是否要挂回来只影响话术，由 `_has_second_clause` 决定。
    """
    if not is_assessment_request(utterance):
        return False
    return len(utterance) <= _MEASUREMENT_ONLY_MAX_LENGTH


_SECOND_CLAUSE_MARKERS = ("，", ",", "再", "然后", "顺便", "并且")


def _has_second_clause(utterance: str) -> bool:
    """句子是否还挂着第二件事（衔接词判据）：只用于在问卷话术里把它挂回来。"""
    return any(marker in utterance for marker in _SECOND_CLAUSE_MARKERS)


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
    """身份始终取服务端；**不再**预置默认产品。

    默认产品曾在此静默覆盖「买 1000 元 R2」的等级意图（买成 R1）。现在产品槽由
    `planning.slots` 先按原文解析，解析不到才在**申购**动作上用兜底产品（记 `server_default`）。
    """
    selected = dict(chosen)
    selected["user_id"] = deps.user_id
    return selected


def _inject_assessment(action: str, slots: dict[str, object], deps: AppDeps) -> None:
    """受信任注入风险测评事实：`wealth.subscribe`（申购闸门）与推荐模式的 `wealth.list`。

    `wealth.list` 只在**推荐 / 比较**意图下注入，普通目录查询保持无测评依赖；
    未测评 / 过期时**不注入**，由场景层 `recommendation_profile` 硬拒绝（绝不默认等级）。

    **不包 `Value`**：场景层 `_assessment` 以 `isinstance(raw, RiskAssessment)` 判定，
    包装会导致其静默失效并退化为"未测评"拒绝。
    """
    if deps.risk is None:
        return
    if action != "wealth.subscribe" and not (
        action == "wealth.list" and is_recommendation_request(_span_text(slots))
    ):
        return
    assessment = to_risk_assessment(deps.risk.assessment_for(deps.user_id))
    if assessment is not None:
        slots["assessment"] = assessment


def _span_text(slots: Mapping[str, object]) -> str:
    span = slots.get("span")
    return span if isinstance(span, str) else ""


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


def _configured_reviewer_channel(reviewer: object | None) -> str | None:
    """返回当前装配 Reviewer 的通道；未知实现不视为可验证来源。"""
    if reviewer is None:
        return None
    channel = getattr(reviewer, "channel", None)
    if isinstance(channel, ReviewChannel):
        return channel.value
    if isinstance(channel, str):
        try:
            return ReviewChannel(channel).value
        except ValueError:
            return None
    if isinstance(reviewer, StrictPlanReviewer):
        return ReviewChannel.LLM_STRICT.value
    if isinstance(reviewer, TemplatePlanReviewer):
        return ReviewChannel.TEMPLATE.value
    if isinstance(reviewer, BypassPlanReviewer):
        return ReviewChannel.BYPASS.value
    if isinstance(reviewer, FallbackPlanReviewer):
        return _configured_reviewer_channel(reviewer._primary)
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


def _intent_semantic_contexts(
    utterance: str, spans: list[str], *, query_indexes: frozenset[int] = frozenset(),
) -> tuple[str, ...] | None:
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
    ranges: list[tuple[int, int]] = []
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
        ranges.append((left, right))
        cursor = span_end
    contexts: list[str] = []
    for index, (left, right) in enumerate(ranges):
        if index in query_indexes:
            # 前后对象限定可以另起短句；相邻已分配动作子句之外的片段仍属待核对范围。
            left = (
                next(
                    start for start, end in clauses
                    if start >= ranges[index - 1][1] and utterance[start:end].strip()
                )
                if index else 0
            )
            right = ranges[index + 1][0] if index + 1 < len(ranges) else len(utterance)
        contexts.append(utterance[left:right].partition("备注")[0])
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
    """多意图拆解：不再让用户复制原话，按**每一项**问它缺的金额 / 必要信息。

    `intent_<i>_<slot>` 与 `_per_intent_selected` 同口径，答案会回填到对应意图；
    金额类动作问金额，其余动作才退回问「对应哪句」。
    """
    asks: list[ClarificationAsk] = []
    for index, intent in enumerate(intents):
        label = _intent_label(intent.action)
        if "amount" in REQUIRED_SLOTS.get(intent.action, ()):
            asks.append(ClarificationAsk(
                slot=f"intent_{index}_amount",
                prompt=f"第 {index + 1} 项「{label}」：请给出金额（元）。",
            ))
        else:
            asks.append(ClarificationAsk(
                slot=f"intent_{index}_span",
                prompt=f"第 {index + 1} 项「{label}」：请补充对应信息（金额或日期）。",
            ))
    return tuple(asks)


_LIFE_BUDGET_WINDOW = re.compile(
    r"(?:预算|上限|以内|不超过|最多|控制在)[^，,。；;]{0,16}"
)


def _life_budget_cents(text: str) -> int | None:
    """从「预算 1000 以内 / 最多 500」这类片段取预算上限；读不到返回 None。"""
    for match in _LIFE_BUDGET_WINDOW.finditer(text):
        parsed = parse_amount(match.group(0), bare_amounts=True)
        if parsed.matched and parsed.value is not None:
            return parsed.value.cents
    return None


def _l1_fill(
    fill: SlotFill, text: str, today: date,
) -> tuple[SlotFill, tuple[ClarificationAsk, ...]]:
    """L1 预算锁定：把通用「商户 + 金额」追问换成场景问题（花店 / 花束 / 配送）。

    商户由系统按距离推荐（演示白名单），不再让用户填「视频会员」；预算只作上限，
    花束档位只列预算内的选项。地址与时间仅作演示字段，不写入执行计划。
    """
    budget = _life_budget_cents(text) or 100000
    offers = flower_offers(budget) or flower_offers()
    nearest = offers[0]
    bundle_label, bundle_price = nearest.bundles[0] if nearest.bundles else ("花束", 29900)
    slots = dict(fill.slots)
    slots.setdefault("budget_cents", budget)
    slots.setdefault("merchant", nearest.merchant)
    # 用户选过花束档位（`amount`）时以它为准；`amount_cents` 是 L1 场景读取的别名，
    # 两者必须同值，否则场景层会先读到默认档位而忽略用户选择。
    chosen_amount = slots.get("amount")
    if isinstance(chosen_amount, int) and chosen_amount > 0:
        slots["amount_cents"] = chosen_amount
    else:
        slots.setdefault("amount_cents", bundle_price)
    parsed_day = parse_temporal(text, today)
    if parsed_day.matched and parsed_day.when is not None:
        slots.setdefault("order_on", parsed_day.when.isoformat())
    questions = tuple(
        question for question in fill.questions
        if question.slot not in {"merchant", "amount", "amount_cents"}
    )
    merchant_options = tuple(
        ClarificationChoice(
            label=f"{offer.merchant} · 距离 {offer.distance_km} km", value=offer.merchant,
        )
        for offer in offers
    )
    amount_options = tuple(
        ClarificationChoice(label=f"{label} · {price // 100} 元", value=f"{price // 100} 元")
        for label, price in nearest.bundles
    ) or (ClarificationChoice(label=f"{bundle_label} · {bundle_price // 100} 元",
                              value=f"{bundle_price // 100} 元"),)
    asks = (
        ClarificationAsk(
            slot="merchant",
            prompt="选一家花店（演示推荐，按距离排序；不会真实配送）。",
            options=merchant_options,
        ),
        ClarificationAsk(
            slot="amount",
            prompt="选一个花束档位（都不超过你的预算）。",
            options=amount_options,
        ),
        ClarificationAsk(slot="delivery_address", prompt="送到哪里？（演示字段，不会真实配送）"),
        ClarificationAsk(slot="delivery_time", prompt="希望哪天送达？（演示字段）"),
    )
    return replace(fill, slots=slots, questions=questions), asks


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
        "merchant.order": "向商户下单",
        "transfer.create": "立即转账",
        "transfer.book": "登记预约转账",
        "transfer.schedule": "定时转账",
        "transfer.request_collection": "发起分摊收款",
        "wealth.redeem": "赎回理财",
        "wealth.subscribe": "申购理财",
        "card.lock": "锁定卡片",
        "card.set_limit": "调整卡片限额",
        "bills.yearly": "年度账单",
        "bills.monthly": "月度账单",
        "subscription.list": "订阅查询",
        "subscription.cancel": "取消订阅",
        "account.balance": "查询余额",
        "account.transactions": "查询交易",
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

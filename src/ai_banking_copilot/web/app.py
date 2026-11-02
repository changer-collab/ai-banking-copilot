"""FastAPI 渠道装配：依赖注入契约 `AppDeps` + 单页渠道 `create_app`。

`AppDeps` 是渠道层的**依赖注入契约**：会话层只依赖它，不自行装配上游组件，
以便测试注入临时数据库 / 假意图通道（LLM-on/off 差分）。

单页 HTML 由 `web.page` 提供（无前端构建链），版式**对齐设计仓 `demo.html`**；样式与设计令牌
随包分发于 `web/assets/{tokens.css,app.css}`（银行凭证语汇：防伪底纹 / 骑缝编号 / 等宽票据数字 /
三色戳记 / 熔断封条）；交互由 `web/assets/app.js` 以**真实 API** 驱动；
窄屏用**容器查询**而非视口媒体查询。
确认按钮回显**冻结哈希**（cfhash），确认与执行前由 `policy.gate` / `policy.pep` 做
计划级 + 节点级双层执行门校验。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import secrets
import time
from calendar import monthrange
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, StrictInt

from ai_banking_copilot.contracts.authorization import Decision
from ai_banking_copilot.contracts.plan import Plan, cfhash
from ai_banking_copilot.gateway.core_bank import DEFAULT_BOOKING_DATE
from ai_banking_copilot.gateway.errors import GatewayNotFoundError
from ai_banking_copilot.gateway.facts import FactGateway
from ai_banking_copilot.gateway.models import Card, Money, Payee
from ai_banking_copilot.gateway.payee_store import PayeeValidationError
from ai_banking_copilot.gateway.receipt_store import ReceiptReader
from ai_banking_copilot.gateway.risk_store import (
    RETAKES_PER_12M,
    RETAKES_PER_DAY,
    SOURCE_QUESTIONNAIRE,
    RiskAssessmentSource,
    RiskAssessmentWriter,
    retake_payload,
)
from ai_banking_copilot.gateway.seed import DEMO_USER_ID
from ai_banking_copilot.gateway.store import Store
from ai_banking_copilot.gateway.wealth_trade_store import WealthTradeWriter
from ai_banking_copilot.planning.llm import IntentModel
from ai_banking_copilot.planning.reviewer import Reviewer
from ai_banking_copilot.planning.task_observations import TaskObservations
from ai_banking_copilot.policy.audit import (
    KIND_CHANNEL,
    KIND_EXECUTION,
    KIND_OPERATOR,
    AuditEvent,
    AuditLog,
    ChainBrokenError,
)
from ai_banking_copilot.policy.breaker import CircuitBreaker
from ai_banking_copilot.policy.gate import StalePlanError
from ai_banking_copilot.policy.pdp import DAILY_LIMIT_YUAN
from ai_banking_copilot.policy.pep import Pep
from ai_banking_copilot.policy.reversal import ReversalService
from ai_banking_copilot.policy.verification import (
    VerificationAttemptLimiter,
    VerificationProvider,
)
from ai_banking_copilot.scenarios.bill_budget import budget_progress
from ai_banking_copilot.scenarios.bill_categories import (
    category_option_payload,
    is_valid_category,
    normalize_merchant,
)
from ai_banking_copilot.scenarios.bill_findings import (
    LEAD_STATE_LABELS,
    LEAD_STATES,
    BillNarrator,
    analyze_bill,
    bill_profile,
    category_shares,
    expense_trend,
    narrate_bill_findings,
    report_payload,
    top_merchants,
)
from ai_banking_copilot.scenarios.life_events import (
    LIFE_EVENT_COMBOS,
    LIFE_EVENT_PROTOTYPES,
    detect_life_combo,
    detect_life_event,
    detect_maturity_link,
    holding_maturity,
)
from ai_banking_copilot.scenarios.subscriptions import (
    CANCEL_STATE_EXTERNAL_CONFIRMED,
    CANCEL_STATE_LABELS,
    EXTERNAL_CANCEL_NOT_INTEGRATED,
    SUBSCRIPTION_CANCEL_BOUNDARY,
    cancel_state_from_receipt,
    insights_payload,
    subscription_report,
)
from ai_banking_copilot.scenarios.wealth import (
    PRODUCT_DATA_SOURCE,
    AssessmentState,
    holdings_payload,
    nav_by_product,
    product_catalog,
    to_risk_assessment,
)
from ai_banking_copilot.scenarios.wealth_assessment import (
    AssessmentError,
    evaluate_answers,
    questionnaire_payload,
)
from ai_banking_copilot.tools.registry import ToolRegistry
from ai_banking_copilot.web import life_funding, reminders, wealth_docs, wording
from ai_banking_copilot.web.auth import (
    DEMO_ACCOUNTS,
    SESSION_IDLE_SECONDS,
    TICKET_PURPOSE_DELETE_CONVERSATION,
    TICKET_TTL_SECONDS,
    AuthError,
    AuthRequiredError,
    AuthService,
    AuthSession,
    AuthUser,
)
from ai_banking_copilot.web.bill_analysis_store import (
    BillAnalysisStore,
    MemoryBillAnalysisStore,
    category_overrides_for,
    lead_states_for,
)
from ai_banking_copilot.web.channels.contracts import (
    CHANNEL_FEISHU,
    CHANNEL_LOCAL,
    ChannelError,
    ChannelRateLimited,
    ConfirmCommand,
    InboundMessage,
    OutboundMessage,
)
from ai_banking_copilot.web.channels.dispatcher import (
    DEFAULT_CHAT_ID,
    Dispatcher,
    DispatchOutcomeUnknown,
    channel_audit_payload,
)
from ai_banking_copilot.web.channels.feishu import (
    FeishuAdapter,
    FeishuConfig,
    FeishuProtocolError,
)
from ai_banking_copilot.web.channels.identity import (
    IdentityDirectory,
    MemoryIdentityDirectory,
    StoreIdentityDirectory,
    init_channel_schema,
)
from ai_banking_copilot.web.channels.ratelimit import (
    EventLedger,
    HealthRegistry,
    TokenBucketRateLimiter,
)
from ai_banking_copilot.web.channels.render import render_feishu_card, render_text
from ai_banking_copilot.web.conversation_store import ConversationStore
from ai_banking_copilot.web.insights import InsightService
from ai_banking_copilot.web.life_event_store import (
    PRIVATE_VISIBILITY,
    SCHEDULE_CANCELLED,
    STATUS_EXECUTED,
    STATUS_FAILED,
    STATUS_REJECTED,
    LifeEventRecord,
    LifeEventStore,
)
from ai_banking_copilot.web.page import INDEX_HTML, render_login_page
from ai_banking_copilot.web.plan_card import (
    PlanCard,
    describe_decision,
    plan_nodes_payload,
)
from ai_banking_copilot.web.progress import ProgressRegistry
from ai_banking_copilot.web.session import (
    MAX_CONFIRM_STEPS,
    ConfirmationMismatchError,
    ExecutionView,
    Session,
    SessionRiskSource,
    SessionStateError,
    SessionView,
    StrongVerificationRequiredError,
    SubmissionView,
    TakeoverPackage,
)
from ai_banking_copilot.web.session_manager import SessionManager
from ai_banking_copilot.web.session_repository import SessionPersistenceError, SessionRepository
from ai_banking_copilot.web.session_snapshot import SessionSnapshotError

AUTH_MODE_ENV = "AIB_AUTH_MODE"
AUTH_MODE_OFF = "off"
AUTH_MODE_DEMO = "demo"
"""登录模式。**默认 `off`**：既有基线（1631 条测试）不因新增鉴权而全量 401；
部署 / 验收显式设 `AIB_AUTH_MODE=demo` 才启用登录、Cookie 会话与 CSRF 校验。"""

AUTH_COOKIE_NAME = "aib_session"
"""会话 Cookie：`HttpOnly` + `SameSite=Lax`，页面脚本读不到 token。"""

LINK_CODE_TTL_SECONDS = 300
"""「连接飞书」绑定码的有效期（秒）：与登录票据同量级，够用户切到飞书发一句。"""

REVERIFY_HEADER = "X-Reverify-Ticket"
"""删除类动作的二次验证票据请求头（票据一次性、5 分钟有效）。"""

LOGGER = logging.getLogger(__name__)


class _BestEffortAuditDispatcher(Dispatcher):
    """成功结果已构造后，渠道审计故障不得改变业务与幂等结果。"""

    def _record_audit(
        self,
        inbound: InboundMessage | ConfirmCommand,
        outbound: OutboundMessage,
        *,
        user_id: str,
    ) -> None:
        try:
            super()._record_audit(inbound, outbound, user_id=user_id)
        except Exception as error:
            LOGGER.warning(
                "channel_audit channel=%s event_id=%s status=failed "
                "reason=audit_failed audit_status=sent error_type=%s",
                self._audit_channel(inbound),
                inbound.event_id or "",
                type(error).__name__,
            )


_CSRF_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
"""需要同源校验的写方法（含 `DELETE`：删除对话历史）。"""

_JSON_METHODS = frozenset({"POST", "PUT", "PATCH"})
"""必须是 `application/json` 的写方法；`DELETE` 无请求体，只做来源校验。"""

_ERROR_STATES = frozenset({"failed", "blocked", "taken_over"})
"""失败 / 阻断终态：对话历史里记为 `error`，不当成正常助手回答。"""

DOCS_ENV = "AIB_ENABLE_DOCS"
"""`/docs` 与 `/openapi.json` 的开关：默认关闭，仅本机调试时置 1。"""

COOKIE_SECURE_ENV = "AIB_COOKIE_SECURE"
"""会话 Cookie 的 `Secure` 开关：经 HTTPS 对外部署时置 1（默认关闭以便本机 http 调试）。"""


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _docs_enabled() -> bool:
    """接口清单默认不公开：只有显式开关才挂载 `/docs` 与 `/openapi.json`。"""
    return _env_flag(DOCS_ENV)


def _cookie_secure() -> bool:
    """HTTPS 部署时必须带 `Secure`；本机 http 调试默认不带，否则登录态写不进去。"""
    return _env_flag(COOKIE_SECURE_ENV)


_BILL_PERIOD = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_BILL_LEAD_ID = re.compile(r"^lead-[A-Za-z0-9_-]{1,80}$")


def _valid_bill_period(period: str) -> bool:
    return bool(_BILL_PERIOD.fullmatch(period))


def _valid_lead_id(lead_id: str) -> bool:
    return bool(_BILL_LEAD_ID.fullmatch(lead_id))


def _bill_lead_state_from_utterance(utterance: str) -> str | None:
    """确定性短语 → 线索状态；只处理明确说明，不做语义猜测。

    顺序：异常 > 忽略 > 正常。含金额的句子不在这里处理（由前端交给办理流程，
    避免把「转给李娜 500 元」误吞成一次线索核对）。
    """
    text = utterance.strip()
    if not text:
        return None
    if any(word in text for word in ("不是我", "盗刷", "异常", "报警", "冻结", "被骗")):
        return "abnormal"
    if any(word in text for word in ("忽略", "不用管", "别管")):
        return "ignored"
    if any(
        word in text
        for word in ("转给", "朋友", "家人", "本人", "正常", "我买的", "我知道", "还款")
    ):
        return "normal"
    return None


@dataclass(frozen=True)
class AppDeps:
    """渠道层依赖装配（全部来自上游公开符号；本层不做任何裁决、不持有写连接）。

    `risk` = 受信任风险测评事实源；`None ⇒ 申购 fail-closed 拒绝`。
    """

    intent_model: IntentModel
    registry: ToolRegistry
    gateway: FactGateway
    pep: Pep
    breaker: CircuitBreaker
    audit: AuditLog
    user_id: str = DEMO_USER_ID
    today: date = DEFAULT_BOOKING_DATE
    risk: RiskAssessmentSource | None = None
    risk_writer: RiskAssessmentWriter | None = None
    """风险测评**录入通道**（只写用户自助录入，`source='user_input'`）。

    `None ⇒ POST /api/risk-assessment` 一律 404：不得把 seed 演示数据当成真实测评。
    读路径仍走 `risk`，写路径与读路径分离，理财推荐只消费读路径事实。"""
    wealth_trades: WealthTradeWriter | None = None
    """理财交易确认留痕通道；`None ⇒ /api/wealth/acknowledgements` 404（fail-closed）。"""
    payee_writer: object | None = None
    """收款人名册登记通道；`None ⇒ POST /api/payees` 409，不静默假装成功。"""
    transfer_schedules: object | None = None
    """定时转账撤销通道；`None ⇒ POST /api/transfers/cancel` 409。"""
    reversal: ReversalService | None = None
    """补偿通道（组合根注入）：`None ⇒ 回退如实失败`，不谎称已退回（见 `policy/reversal.py`）。"""
    operator_token: str | None = None
    """**运维口令**（裁决 #27）：`POST /api/operator/breaker-unlock` 的凭证，取自环境变量
    `AIB_OPERATOR_TOKEN`。**未配置（`None` / 空串）⇒ 该端点一律 403**（fail-closed）——
    绝不允许「默认无口令即可解锁熔断」这种自毁式配置。"""
    reviewer: Reviewer | None = None
    """计划后置审查器；默认空值保持旧版测试装配，组合根可注入旁路或严格通道。"""
    reviewer_required: bool = False
    """是否把 Reviewer 结果接入执行前状态机；旁路模式只审计诊断。"""
    insights: InsightService | None = None
    """只读主动洞察服务；元数据存储与银行事实连接由组合根隔离装配。"""
    bill_analysis: BillAnalysisStore | None = None
    """账单线索核对状态与分类修正；不写银行事实库。"""
    verification_provider: VerificationProvider | None = None
    verification_key: bytes | None = None
    verification_strict: bool = False
    verification_limiter: VerificationAttemptLimiter = field(
        default_factory=VerificationAttemptLimiter
    )
    """强验证失败计数与退避（按用户）：默认组合根注入 SQLite store，跨 worker 与重启共享；
    手工构造 `AppDeps` 时默认仍为进程内，同一实例的多个会话共享计数。"""
    session_repository: SessionRepository | None = None
    receipts: ReceiptReader | None = None
    model_observations: TaskObservations | None = None
    session_risk: SessionRiskSource | None = None
    """**服务端**可信会话风控信号源（G-03）：`True` 异常 / `False` 正常 / `None` 无可信结果。

    **绝不**从请求体读取风控标记；所有渠道（API / Hook / IM）经同一 `Session` 取同一信号。
    未装配时按 `session_risk_strict` 处理（严格 ⇒ 升级核验，绝不默认放行）。"""
    session_risk_strict: bool = True
    """严格风控模式（G-03）：未装配可信信号源时视为「无可信结果」并升级核验。

    **默认失败关闭**：任何未显式装配 `session_risk` 的渠道（API / Hook / IM）都不会因
    缺失风控结论而放行。演示装配（`compose.build_deps`，本地单用户、无设备 / 地点遥测）
    通过 `AIB_SESSION_RISK_STRICT` 显式放开；生产装配应保持开启或提供真实信号源。"""
    bill_narrator: BillNarrator | None = None
    """账单结论式解释的模型通道；`None ⇒ 使用确定性文案并显式标注降级`。

    模型只解释 `scenarios.bill_findings` 已核实的识别结果，**不参与金额计算**；
    识别引擎与金额核算在装配缺失时仍完整可用（可测试、可重算）。"""
    channel_store: Store | None = None
    """渠道身份（绑定关系 / 绑定码）的持久化连接。

    `None ⇒ 进程内身份目录`：单进程演示与测试可用，重启即失效；组合根（`compose.build_deps`）
    注入写连接后，外部身份绑定跨重启仍生效。**任何情况下外部 id 都必须先绑定**（fail-closed）。"""
    auth: AuthService | None = None
    """登录凭证服务；`None` 或 `auth_mode != "demo"` 时**不启用鉴权**（沿用 `user_id` 单用户）。"""
    conversations: ConversationStore | None = None
    """对话历史持久化；`None ⇒ 历史接口返回空列表且不写库**（不谎称已保存）。"""
    life_events: LifeEventStore | None = None
    """生活事件记录；`None ⇒ 面板只读原型、不落库。"""
    feishu: FeishuAdapter | None = None
    """飞书适配器（组合根注入）；`None ⇒ `create_app` 自行按环境变量装配一份。

    **到期推送只认显式注入的这一份**：`None` 时即便开关打开也不挂调度器，避免单测
    意外启动后台线程。"""
    enable_push_scheduler: bool = False
    """到期推送调度器开关（**默认关闭**）：`True` 才在 lifespan 里每 60s 扫一次到期预约。

    测试与本地调试都走默认值；需要演示推送时由组合根按 `AIB_ENABLE_PUSH` 显式打开。"""
    progress: ProgressRegistry = field(default_factory=ProgressRegistry)
    """阶段进度表（进程内、按 `(user, session, request)`）：`generate` 前端的轮询数据源。"""
    auth_mode: str = AUTH_MODE_OFF
    """`off`（默认，单用户演示）/ `demo`（登录 + Cookie 会话 + CSRF）。"""
    demo_password: str | None = None
    """`demo` 模式下用于登录页提示的演示口令；`None` 时页面只写“由现场负责人提供”。"""
    wording_polisher: wording.Polisher | None = None
    """措辞层改写通道；`None` ⇒ 只用确定性模板（措辞异常一律退回模板，不影响业务结果）。"""


DEFAULT_SESSION_ID = "default"
"""缺省会话标识：请求体未带 `session_id` 时用它，保持既有单会话行为不变（多会话隔离的兜底）。"""

RISK_SOURCE_LABELS = {
    "seed": "演示 seed 数据（非真实测评）",
    "user_input": "用户自助录入",
    "questionnaire": "风险测评问卷",
}
"""测评来源标注：`seed` 必须显式说明不是真实测评。"""


class _SubmitRequest(BaseModel):
    utterance: str
    selected: dict[str, object] | None = None
    session_id: str = DEFAULT_SESSION_ID
    """会话标识：不同取值对应**相互隔离**的会话（各自 pending / 状态机独立）。

    **不接受** `session_anomaly` 一类风控入参（G-03）：风控结论只能由服务端可信源
    `AppDeps.session_risk` 生成，请求体里的同名字段一律无效果。
    """
    request_id: str | None = None
    """阶段进度标识：前端生成，仅用于 `GET /api/progress` 拉取处理阶段，不参与业务判定。"""


class _LoginRequest(BaseModel):
    """登录请求体：口令只在此处出现，不回显、不写日志。"""

    username: str
    password: str


class _CreatePayeeRequest(BaseModel):
    """收款人登记：只收尾号，不收完整卡号。"""

    full_name: str = Field(min_length=1, max_length=20)
    account_tail: str = Field(min_length=4, max_length=4)
    bank_name: str = Field(min_length=2, max_length=20)
    phone_tail4: str = Field(default="", max_length=4)


class _ReverifyRequest(BaseModel):
    """二次验证：校验当前用户口令，换取**一次性**短时票据（删除类动作用）。"""

    password: str
    purpose: str


class _ConfirmRequest(BaseModel):
    card_id: str
    frozen_hash: str
    verification_code: str | None = None
    steps: StrictInt | None = Field(default=None, ge=1, le=MAX_CONFIRM_STEPS)
    """分步执行上界：只接受真正的整数（拒绝 `"2"`、`2.0`、`true`、负数、0 与超大值）。"""
    session_id: str = DEFAULT_SESSION_ID


class _ClarifyRequest(BaseModel):
    """澄清补充答案：`槽位名 → 值`（与 `selected` 同口径）。"""

    answers: dict[str, object] = {}
    session_id: str = DEFAULT_SESSION_ID
    request_id: str | None = None
    """阶段进度标识：与 `_SubmitRequest` 同口径，仅用于拉取处理阶段。"""


class _EmptyRequest(BaseModel):
    """空请求体（中断 / 恢复 / 回退）。"""

    session_id: str = DEFAULT_SESSION_ID


class _ScheduleRequest(BaseModel):
    """到期预约的重试 / 放弃：只认服务端已有且属于本人的 `schedule_id`。"""

    session_id: str = DEFAULT_SESSION_ID
    schedule_id: str


class _UnlockRequest(BaseModel):
    """运维解锁请求体（裁决 #27）：**必须给理由**，凭证走请求头（不落请求体、不进日志）。"""

    reason: str


class _BudgetRequest(BaseModel):
    """只读洞察的提醒预算；单位为整数分，不进入银行授权。"""

    budget_cents: int | None = None
    month: str | None = None
    categories: dict[str, int] | None = None
    """分类预算（一级分类 → 整数分）；缺省表示不改动分类预算。"""


class _LeadStateRequest(BaseModel):
    """线索核对状态：只写账单分析元数据，不写银行事实。"""

    period: str
    state: str
    note: str = ""


class _CategoryOverrideRequest(BaseModel):
    """用户对商户二级分类的修正；按 merchant_key 记忆。"""

    merchant_key: str
    category: str


class _LeadResolutionRequest(BaseModel):
    """对话回写：用户对某条线索的说明，由确定性短语规则映射状态。"""

    lead_id: str
    period: str
    utterance: str


class _RiskAssessmentRequest(BaseModel):
    """用户侧测评录入：**身份只取服务端 `deps.user_id`**，不接受请求体用户 id。

    两条互斥路径：
    - `answers`：固定题库作答，等级**由服务端评分算出**（`source='questionnaire'`）；
    - `risk_profile`：旧的直接录入（`source='user_input'`，保留兼容）。
    两者同时提交一律 400——等级不允许既由用户自填又由问卷算出。
    """

    risk_profile: str | None = None
    answers: dict[str, str] | None = None
    confirm_change: bool = False
    """结果与最近一次不一致时，客户端再次确认后置 `True` 才落库。"""


class _WealthAckRequest(BaseModel):
    """理财交易确认留痕：四件套阅读、声明抄录、分红方式与类型差异确认。"""

    action: str
    product_id: str
    account_id: str | None = None
    doc_versions: dict[str, str]
    read_seconds: dict[str, int]
    declaration_text: str | None = None
    dividend_method: str | None = None
    auto_redeem_pref: bool | None = None
    experience_confirmed: bool = False
    loss_ack: bool = False
    senior_ack: bool = False


class _ImMessageRequest(BaseModel):
    """IM 入站文本：`user_id / chat_id / event_id` 全部可选，但**外部 id 必须先绑定**。

    不带 `user_id` 时走服务端身份（仅本地 IM 渠道）；`event_id` 相同即视为平台重投，
    直接回放首次结果，不重复执行。
    """

    text: str
    chat_id: str | None = None
    user_id: str | None = None
    event_id: str | None = None


class _ImVerificationRequest(BaseModel):
    """IM 强验证挑战：只用 chat_id 定位与待确认计划相同的 IM 会话。"""

    chat_id: str | None = None


class _ImConfirmRequest(BaseModel):
    """IM 计划卡确认：字段与 `POST /api/confirm` 同口径，复用同一冻结校验。"""

    card_id: str
    frozen_hash: str
    verification_code: str | None = None
    steps: StrictInt | None = Field(default=None, ge=1, le=MAX_CONFIRM_STEPS)
    chat_id: str | None = None
    user_id: str | None = None
    event_id: str | None = None


def build_session_manager(deps: AppDeps) -> SessionManager[Session]:
    """按 `AppDeps` 构造会话管理器；Web 与 IM 共用**同一份**实现（避免两套状态机）。

    会话工厂按**真实 user** 重建 deps：`deps.user_id` 是全局默认值，多用户场景下
    每个会话（含从快照恢复的会话）都必须持有自己的 user，否则会读到别人的账户事实。
    """
    return SessionManager(
        deps.session_repository,
        factory=lambda user, session_id, lock: Session(
            replace(deps, user_id=user), session_id=session_id, shared_lock=lock,
        ),
        restore=lambda snapshot, lock: Session.restore(
            replace(deps, user_id=snapshot.user_id), snapshot, lock,
        ),
    )


def create_app(deps: AppDeps) -> FastAPI:
    """装配单页渠道。计划卡与执行全部来自上游；本层只做 HTTP 与序列化。

    **多会话（P2-7）**：按 `session_id` 维护**相互隔离**的 `Session`（缺省 `DEFAULT_SESSION_ID`
    保持既有单会话行为）；`AppDeps` 的字段签名不变（上游依赖仍由组合根注入一次）。

    **多用户**：`AIB_AUTH_MODE=demo` 时启用登录 + Cookie 会话 + CSRF；每个请求的用户由
    `current_user` 解析，会话工厂与 IM 调度都按**真实 user** 构造，绝不共用默认用户。
    """
    auth_enabled = deps.auth is not None and deps.auth_mode == AUTH_MODE_DEMO
    bill_store: BillAnalysisStore = deps.bill_analysis or MemoryBillAnalysisStore()

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        task: asyncio.Task[None] | None = None
        service = deps.insights
        if service is not None:
            service.scan(deps.user_id, deps.today)

            async def periodic_scan() -> None:
                while True:
                    await asyncio.sleep(60)
                    service.scan(deps.user_id, deps.today)

            task = asyncio.create_task(periodic_scan())
        push_task: asyncio.Task[None] | None = None
        if (
            deps.enable_push_scheduler
            and deps.life_events is not None
            and deps.feishu is not None
        ):
            adapter = deps.feishu

            async def push_tick() -> int:
                """同步推送在线程里跑：SQLite 读与阻塞 HTTP 都不该占事件循环。"""
                return await asyncio.to_thread(reminders.push_due_once, deps, adapter=adapter)

            push_task = asyncio.create_task(reminders.run_forever(push_tick))
        try:
            yield
        finally:
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            if push_task is not None:
                push_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await push_task

    app = FastAPI(
        title="AI 银行副驾 · Web 渠道",
        version="0.1.0",
        lifespan=lifespan,
        # 接口清单本身就是攻击面：默认关闭 /docs 与 /openapi.json，只在显式设
        # `AIB_ENABLE_DOCS=1` 的本机调试环境开启，对外部署保持关闭。
        docs_url="/docs" if _docs_enabled() else None,
        openapi_url="/openapi.json" if _docs_enabled() else None,
        redoc_url=None,
    )

    @app.middleware("http")
    async def service_worker_scope(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """`/assets/sw.js` 需要 `Service-Worker-Allowed: /` 才能注册到站点根作用域。

        静态文件挂在 `/assets`，浏览器默认只允许 `/assets/` 作用域；缺这个响应头时
        注册会被拒（`SecurityError`），离线壳与安装能力都不成立。
        """
        response = await call_next(request)
        if request.url.path == "/assets/sw.js":
            response.headers["Service-Worker-Allowed"] = "/"
        return response

    app.mount(
        "/assets",
        StaticFiles(directory=str(Path(__file__).parent / "assets")),
        name="assets",
    )
    manager = build_session_manager(deps)
    identities: IdentityDirectory
    if deps.channel_store is not None:
        init_channel_schema(deps.channel_store)
        identities = StoreIdentityDirectory(deps.channel_store)
    else:
        identities = MemoryIdentityDirectory()
    feishu = deps.feishu if deps.feishu is not None else FeishuAdapter(FeishuConfig.from_env())
    feishu_audit_channel = "feishu" if feishu.config.mode == "adapter" else "replay"
    LOGGER.info(
        "channel_startup channel=feishu event_id= feishu=%s", feishu.config.mode
    )

    def _issue_link_code(user_id: str) -> tuple[str, int]:
        """签发 6 位一次性绑定码（5 分钟有效）；同一用户重签即覆盖旧码。"""
        store = deps.channel_store
        if store is None:
            raise HTTPException(status_code=409, detail="channel_store_unavailable")
        now = time.time()
        with store.transaction() as conn:
            conn.execute("DELETE FROM channel_link_codes WHERE expires_at <= ?", (now,))
            conn.execute(
                "DELETE FROM channel_link_codes WHERE channel = 'feishu' AND user_id = ?",
                (user_id,),
            )
            for _ in range(8):
                code = f"{secrets.randbelow(10**6):06d}"
                cursor = conn.execute(
                    "INSERT OR IGNORE INTO channel_link_codes"
                    " (code, channel, user_id, expires_at) VALUES (?, 'feishu', ?, ?)",
                    (code, user_id, now + LINK_CODE_TTL_SECONDS),
                )
                if cursor.rowcount:
                    return code, LINK_CODE_TTL_SECONDS
        raise HTTPException(status_code=409, detail="link_code_exhausted")

    def _claim_link_code(text: str) -> str | None:
        """用户在飞书里发来的 6 位码 → 对应 user_id；码一次性、过期即作废。"""
        store = deps.channel_store
        code = text.strip()
        if store is None or len(code) != 6 or not code.isdigit():
            return None
        rows = store.query(
            "SELECT user_id, expires_at FROM channel_link_codes"
            " WHERE code = ? AND channel = 'feishu'",
            (code,),
        )
        if not rows:
            return None
        with store.transaction() as conn:
            conn.execute("DELETE FROM channel_link_codes WHERE code = ?", (code,))
        if float(rows[0]["expires_at"]) <= time.time():
            return None
        return str(rows[0]["user_id"])

    def _bind_feishu_identity(external_user_id: str, user_id: str) -> None:
        store = deps.channel_store
        if store is None:
            return
        with store.transaction() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO channel_identities"
                " (channel, external_user_id, user_id, bound_at) VALUES ('feishu', ?, ?, ?)",
                (external_user_id, user_id, datetime.now().isoformat()),
            )

    def _unbind_feishu(user_id: str) -> int:
        store = deps.channel_store
        if store is None:
            return 0
        with store.transaction() as conn:
            cursor = conn.execute(
                "DELETE FROM channel_identities WHERE channel = 'feishu' AND user_id = ?",
                (user_id,),
            )
            return int(cursor.rowcount)

    def _feishu_linked(user_id: str) -> bool:
        store = deps.channel_store
        if store is None:
            return False
        rows = store.query(
            "SELECT 1 AS hit FROM channel_identities"
            " WHERE channel = 'feishu' AND user_id = ? LIMIT 1",
            (user_id,),
        )
        return bool(rows)

    def _send_feishu_notice(chat_id: str, text: str) -> None:
        """绑定成功后给用户回一条提示；失败只记日志，不回滚绑定结果。"""
        outbound = OutboundMessage(
            channel=CHANNEL_FEISHU,
            chat_id=chat_id,
            session_id=f"{CHANNEL_FEISHU}:{chat_id}",
            state="linked",
            text=text,
        )
        try:
            feishu.send(outbound)
        except Exception as error:  # noqa: BLE001 - 通知失败不影响绑定
            LOGGER.warning(
                "channel_send channel=%s event_id= status=failed "
                "reason=link_notice_failed error_type=%s",
                CHANNEL_FEISHU,
                type(error).__name__,
            )

    health = HealthRegistry()
    health.register("web")
    health.register(CHANNEL_LOCAL)
    health.register(CHANNEL_FEISHU, replay_mode=feishu.replay_mode)
    channel_limiter = TokenBucketRateLimiter()
    event_ledger: EventLedger | None = None
    memory_event_ids: set[str] = set()
    replay_responses: dict[str, dict[str, object]] = {}

    _dispatchers: dict[str, Dispatcher] = {}
    """按用户缓存的渠道调度器：IM 本地渠道沿用登录者身份，不借用默认用户。"""

    def _dispatcher_for(user_id: str) -> Dispatcher:
        with manager.lock:
            existing = _dispatchers.get(user_id)
            if existing is not None:
                return existing
            built = _BestEffortAuditDispatcher(
                manager=manager,
                identities=identities,
                submission_payload=_submission_payload,
                execution_payload=_execution_payload,
                audit=deps.audit,
                server_user_id=user_id,
                limiter=channel_limiter,
                feishu_configured=feishu.configured,
                feishu_audit_channel=feishu_audit_channel,
            )
            _dispatchers[user_id] = built
            return built

    def _append_channel_audit(
        *,
        user_id: str,
        channel: str,
        direction: str,
        status: str,
        reason: str = "",
        event_id: str | None = None,
        external_user_id: str | None = None,
        attempt_no: int | None = None,
        http_status: int | None = None,
        text: str | None = None,
        details: dict[str, object] | None = None,
        best_effort: bool = False,
    ) -> None:
        event = AuditEvent(
            event_id=f"{KIND_CHANNEL}:{channel}:{secrets.token_hex(8)}",
            kind=KIND_CHANNEL,
            payload=channel_audit_payload(
                user_id=user_id,
                channel=channel,
                direction=direction,
                status=status,
                reason=reason,
                event_id=event_id,
                external_user_id=external_user_id,
                attempt_no=attempt_no,
                http_status=http_status,
                text=text,
                details=details,
            ),
            prev_hash="",
        )
        try:
            deps.audit.append(event)
        except Exception as error:
            if not best_effort:
                raise
            LOGGER.warning(
                "channel_audit channel=%s event_id=%s status=failed "
                "reason=audit_failed audit_status=%s audit_reason=%s error_type=%s",
                channel,
                event_id or "",
                status,
                reason,
                type(error).__name__,
            )

    @app.exception_handler(SessionPersistenceError)
    @app.exception_handler(SessionSnapshotError)
    async def persistence_error(_request: Request, _error: Exception) -> JSONResponse:
        return JSONResponse(status_code=409, content={
            "detail": "会话存储或恢复失败，已暂停；请核对业务回执后重启服务",
        })

    @app.exception_handler(ChannelError)
    async def channel_error(request: Request, error: ChannelError) -> JSONResponse:
        """渠道层稳定错误：直接采用错误族自带的 `status_code`，不吞成 500。"""
        headers = {"Retry-After": "60"} if isinstance(error, ChannelRateLimited) else None
        message = (
            "发太快了，60 秒后再试"
            if isinstance(error, ChannelRateLimited)
            else str(error)
        )
        if isinstance(error, ChannelRateLimited):
            channel = (
                CHANNEL_FEISHU
                if request.url.path == "/api/im/feishu"
                else CHANNEL_LOCAL
                if request.url.path.startswith("/api/im/")
                else "web"
            )
            health.record_rate_limit(channel)
            health.record_event(channel, success=False)
        return JSONResponse(
            status_code=error.status_code,
            content={"detail": error.code, "message": message},
            headers=headers,
        )

    @app.exception_handler(AuthError)
    async def auth_error(_request: Request, error: AuthError) -> JSONResponse:
        """认证层稳定错误：401 未登录 / 429 锁定 / 403 票据失效，前端按 `detail` 分流。"""
        return JSONResponse(
            status_code=error.status_code,
            content={"detail": error.code, "message": str(error)},
        )

    def _auth() -> AuthService:
        if deps.auth is None:
            raise HTTPException(status_code=404, detail="登录未装配")
        return deps.auth

    def _check_csrf(request: Request) -> None:
        """跨站写请求拦截：只接受同日源的 JSON 写入。

        来源按 `Origin` → `Referer` 判定；两者都缺失时放行（同源 `fetch` 在部分浏览器
        不带这两个头），而浏览器跨站表单 / 跨站 `fetch` 一定带 `Origin`，因此都被拦住。
        """
        method = request.method.upper()
        if method not in _CSRF_METHODS:
            return
        if method in _JSON_METHODS:
            content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
            if content_type and content_type != "application/json":
                raise HTTPException(status_code=403, detail="cross_site_rejected")
        origin = request.headers.get("origin") or request.headers.get("referer") or ""
        if origin and urlsplit(origin).netloc != request.headers.get("host", ""):
            raise HTTPException(status_code=403, detail="cross_site_rejected")

    def _optional_user_id(request: Request) -> str | None:
        """解析「这是谁」：`off` 模式沿用单用户；`demo` 模式未登录返回 `None`（不抛错）。"""
        if not auth_enabled:
            return deps.user_id
        session = _auth().resolve(request.cookies.get(AUTH_COOKIE_NAME, ""))
        if session is None:
            return None
        request.state.auth_session = session
        return session.user_id

    def _authenticated(request: Request) -> bool:
        """是否已登录；`off` 模式没有访客概念，恒为真。"""
        return _optional_user_id(request) is not None

    def _read_user(request: Request) -> str:
        """只读接口身份：已登录用真实身份；`off` 模式沿用单用户演示。

        `demo` 模式未登录一律 401：页面入口已要求先登录，只读接口不再回落到演示身份，
        否则绕过页面直接打 API 仍能读到别人的数据。
        """
        user_id = _optional_user_id(request)
        if user_id is not None:
            return user_id
        if auth_enabled:
            raise AuthRequiredError("登录已失效，请重新登录")
        return DEMO_USER_ID

    def _is_anonymous(request: Request) -> bool:
        """本次请求是否只是访客（响应体据此显式标注演示数据）。"""
        return _optional_user_id(request) is None

    def current_user(request: Request) -> str:
        """当前登录用户；`off` 模式沿用单用户演示（`deps.user_id`），行为与旧版一致。"""
        if not auth_enabled:
            return deps.user_id
        _check_csrf(request)
        user_id = _optional_user_id(request)
        if user_id is None:
            raise AuthRequiredError("登录已失效，请重新登录")
        return user_id

    def _page_or_login(request: Request, html: str) -> HTMLResponse:
        """页面入口：`demo` 模式未登录时**返回登录页**（默认必须先登录）。

        登录页不引用 `app.js` / `workspace.css`，因此不会在未登录态触发需要身份的
        只读接口（`/api/options`、`/api/conversations` 均 401），首屏不会刷出
        「读取失败」类错误文案。
        已登录页面内嵌资源版本号，**必须 no-store**：否则浏览器缓存旧 HTML 后会一直引用
        旧的 `workspace.css?v=*`，出现「代码已更新、页面还是旧版」的假象。
        """
        if auth_enabled and not _authenticated(request):
            accounts = tuple(row[0] for row in DEMO_ACCOUNTS)
            return HTMLResponse(
                render_login_page(accounts, deps.demo_password),
                headers={"Cache-Control": "no-store"},
            )
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    def _session_for(session_id: str, user_id: str) -> Session:
        return manager.get(user_id, session_id)

    def _response(session_id: str, user_id: str, payload: dict[str, object]) -> dict[str, object]:
        return {**payload, "model": _session_for(session_id, user_id).model_metadata()}

    def _progress_callback(
        user_id: str, session_id: str, request_id: str | None
    ) -> Callable[[str], None] | None:
        """阶段旁路回调：写进度失败绝不影响问答主流程。"""
        if not request_id:
            return None

        def notify(stage: str) -> None:
            try:
                deps.progress.advance(user_id, session_id, request_id, stage)
            except Exception:
                LOGGER.warning("阶段进度写入失败：%s", stage, exc_info=True)

        return notify

    def _record_message(
        user_id: str,
        session_id: str,
        role: str,
        kind: str,
        text: str,
        payload: dict[str, object] | None = None,
    ) -> None:
        """对话历史落库（旁路）：未装配或写失败都不影响本次回答。"""
        store = deps.conversations
        if store is None:
            return
        try:
            store.append(
                user_id=user_id, session_id=session_id, role=role, kind=kind,
                text=text, payload=payload,
            )
        except Exception:
            LOGGER.warning("对话历史写入失败：%s / %s", user_id, session_id, exc_info=True)

    def _record_answer(user_id: str, session_id: str, view: SubmissionView) -> None:
        card = _card_payload(view.card, view.plan)
        if card is not None:
            _record_message(
                user_id, session_id, "assistant", "plan_card", view.message, {"card": card},
            )
        elif view.state.value in _ERROR_STATES:
            payload: dict[str, object] | None = (
                {"triage": {"kind": view.triage.kind}} if view.triage is not None else None
            )
            _record_message(
                user_id, session_id, "assistant", "error", view.message, payload,
            )
        else:
            _record_message(user_id, session_id, "assistant", "text", view.message)

    def _record_life_event(
        user_id: str,
        session_id: str,
        utterance: str,
        selected: dict[str, object] | None,
        view: SubmissionView,
    ) -> SubmissionView:
        """识别到生活事件且已出计划卡时登记一条 `planned`。

        金额优先取表单值，其次取原话里明写的「N 元」，都没有就留空（不估算）；
        标题记具体事件，金额不重复写进标题。可见性跟随本次提交：私密记录不进
        普通列表与共享推送，并在回执里说清楚应用内外的边界。
        """
        store = deps.life_events
        if store is None or view.card is None:
            return view
        event_id = detect_life_combo(utterance) or detect_life_event(utterance) or (
            _MATURITY_EVENT_ID if detect_maturity_link(utterance) else None
        )
        if event_id is None:
            return view
        event_name = _life_event_name(event_id)
        # 事件金额取**计划里的资金口径**（预留额 > 赎回额 > 下单额），
        # 不能把花束档位 299 当成整件事的 1000 预算。
        amount = _life_plan_amount(view.plan)
        if amount is None:
            amount = _selected_amount_cents(selected)
        if amount is None:
            amount = _utterance_amount_cents(utterance)
        hold_ref, occasion_ref = _life_plan_refs(view.plan)
        try:
            store.create(
                user_id=user_id,
                session_id=session_id,
                event_id=event_id,
                event_name=event_name,
                # 具体事件：优先用该类型的「常见事件」名，否则压掉金额后的原话
                event_title=_life_event_title(utterance, fallback=event_name),
                amount_cents=amount,
                steps=_life_plan_steps(view.plan),
                due_on=_life_plan_due_on(view.plan),
                visibility=view.visibility,
                utterance=utterance,
                hold_ref=hold_ref,
                occasion_ref=occasion_ref,
            )
        except Exception:
            LOGGER.warning("生活事件写入失败：%s / %s", user_id, session_id, exc_info=True)
        return _with_funding_note(view, user_id, amount)

    def _with_funding_note(
        view: SubmissionView, user_id: str, amount_cents: int | None
    ) -> SubmissionView:
        """在回复末尾补一句资金可用性提醒；资金来自自身赎回 / 已执行时不提示。"""

        plan = view.plan
        if plan is None or amount_cents is None:
            return view
        if any(node.action == "wealth.redeem" for node in plan.nodes):
            return view
        trigger = _life_plan_trigger_day(plan)
        if trigger is None:
            return view
        try:
            result = life_funding.check(
                deps.gateway,
                user_id,
                amount_cents=amount_cents,
                trigger_on=trigger,
                today=deps.today,
            )
        except Exception:
            LOGGER.warning("资金来源核查失败：%s", user_id, exc_info=True)
            return view
        message = life_funding.note(result)
        if message is None:
            return view
        return replace(view, message=f"{view.message}\n{message}")

    def _mark_life_event(
        user_id: str, session_id: str, status: str, view: ExecutionView | None = None
    ) -> None:
        store = deps.life_events
        if store is None:
            return
        try:
            if view is not None and view.exec_results:
                record = store.latest_record(user_id=user_id, session_id=session_id)
                if record is not None:
                    store.attach_execution(
                        user_id=user_id,
                        record_id=record.record_id,
                        results=tuple(
                            {
                                "action": item.action,
                                "status": item.status,
                                "record_ids": list(item.record_ids),
                            }
                            for item in view.exec_results
                        ),
                    )
            store.mark(user_id=user_id, session_id=session_id, status=status)
        except Exception:
            LOGGER.warning("生活事件状态写入失败：%s / %s", user_id, session_id, exc_info=True)

    def _session_state_of(user_id: str, session_id: str) -> str:
        repository = deps.session_repository
        if repository is None:
            return ""
        try:
            snapshot = repository.load(user_id, session_id)
        except Exception:
            LOGGER.warning("会话快照读取失败：%s / %s", user_id, session_id, exc_info=True)
            return ""
        return "" if snapshot is None else snapshot.state

    def _channel_binding_count(user_id: str) -> int:
        if deps.channel_store is None:
            return 0
        rows = deps.channel_store.query(
            "SELECT COUNT(*) AS total FROM channel_identities WHERE user_id = ?", (user_id,)
        )
        return int(rows[0]["total"]) if rows else 0

    @app.get("/api/session")
    def session_state(
        session_id: str = DEFAULT_SESSION_ID, user_id: str = Depends(current_user)
    ) -> dict[str, object]:
        """只恢复与读取状态，不触发计划执行或返回挑战秘密。

        会话按 **(`user_id`, `session_id`) 二元组**隔离：默认 `session_id` 只是「该用户
        自己的默认槽」，不同用户即便都不传也不会串会话；前端每次新开页面都会生成
        独立 `session_id`，因此同一账号两个窗口互不干扰。
        """
        view = _session_for(session_id, user_id).current_view()
        return _response(
            session_id, user_id, _execution_payload(view, deps.gateway, user_id, deps.today)
        )

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> HTMLResponse:
        return _page_or_login(request, INDEX_HTML)

    @app.get("/api/options")
    def options(user_id: str = Depends(current_user)) -> dict[str, object]:
        """对象选择只展示当前用户的事实；金额不由前端编造。"""
        gateway = deps.gateway
        account_kinds = {"savings": "储蓄", "current": "活期"}
        card_types = {"debit": "借记", "credit": "信用", "virtual": "虚拟"}
        statuses = {"active": "正常", "locked": "已锁定", "lost": "已挂失", "cancelled": "已取消"}
        return {
            "today": deps.today.isoformat(),
            # 场景①「额度卡」只读数据：限额取官方 1000 元与客户分档的较小者
            # （同 policy/execution.py 口径），已用取当日已记账转出合计。
            # 两者都由 gateway 给事实，前端不计算。
            "quota": _transfer_quota_payload(gateway, user_id, deps.today),
            "accounts": [
                {"id": record.payload.account_id,
                 "label": f"{account_kinds.get(record.payload.kind, record.payload.kind)}账户"
                          f" · 余额 {record.payload.balance.yuan} 元",
                 "balance_cents": record.payload.balance.cents}
                for record in gateway.accounts_of(user_id)
            ],
            "cards": [
                {"id": record.payload.card_id,
                 "label": f"{card_types.get(record.payload.card_type, record.payload.card_type)}卡"
                          f" · {statuses.get(record.payload.status, record.payload.status)}"}
                for record in gateway.cards_of(user_id)
            ],
            "subscriptions": [
                {"id": record.payload.subscription_id,
                 "label": f"{record.payload.merchant}"
                          f" · {statuses.get(record.payload.status, record.payload.status)}"}
                for record in gateway.subscriptions_of(user_id)
            ],
            "holds": [
                {"id": record.payload.hold_id,
                 "label": f"{record.payload.reason} · 冻结 {record.payload.amount.yuan} 元"}
                for record in gateway.active_holds_of(user_id)
            ],
            "subscription_details": insights_payload(
                subscription_report(gateway, user_id, as_of=deps.today)
            ),
            "subscription_capabilities": {
                "external_cancel_status": EXTERNAL_CANCEL_NOT_INTEGRATED,
                "external_cancel_integrated": False,
                "can_request_external_cancel": False,
                "can_local_cancel": True,
                "close_action_label": "关闭扣款协议",
                "can_close_bank_side": True,
                "boundary": SUBSCRIPTION_CANCEL_BOUNDARY,
                "states": [
                    {"state": state, "label": label}
                    for state, label in CANCEL_STATE_LABELS.items()
                ],
            },
        }

    @app.get("/api/cards")
    def cards(user_id: str = Depends(current_user)) -> dict[str, object]:
        """卡片面板只读事实：脱敏卡号 / 卡组织 / 分卡限额 / 状态允许动作。

        状态与额度一律读服务端，前端不得自行推导；`allowed_actions` 是本接口算出的
        **按钮唯一真源**，前端不得按状态自行扩展。
        """
        card_types = {"debit": "借记卡", "credit": "信用卡", "virtual": "虚拟卡"}
        statuses = {"active": "正常", "locked": "临时冻结（口头挂失）", "lost": "已正式挂失"}

        def status_label(card: Card, effective: str) -> str:
            if card.status == "lost" and card.replaced_by_card_id:
                return "已挂失 · 已补卡" if card.lost_at else "已补卡（旧卡作废）"
            if card.status == "locked" and effective == "active":
                return "临时冻结已到期（自动解挂）"
            return statuses.get(card.status, card.status)

        rows: list[dict[str, object]] = []
        for record in deps.gateway.cards_of(user_id):
            card = record.payload
            effective = _effective_card_status(card.status, card.temp_lock_until, deps.today)
            replacement = deps.gateway.card_replacement(card.card_id)
            rows.append(
                {
                    "card_id": card.card_id,
                    "type": card.card_type,
                    "type_label": card_types.get(card.card_type, card.card_type),
                    "status": card.status,
                    "effective_status": effective,
                    "status_label": status_label(card, effective),
                    "masked_no": _masked_card_no(card.last4),
                    "last4": card.last4 or "",
                    "brand": card.brand or "",
                    "brand_label": _CARD_BRAND_LABELS.get(card.brand or "", "卡组织未知"),
                    "expiry": _card_expiry(card.expiry_month, card.expiry_year),
                    "account_class": card.account_class or "",
                    "limits": _card_limits_payload(card),
                    "temp_lock": _temp_lock_payload(card, deps.today),
                    "lost": _lost_payload(card),
                    "replacement": replacement,
                    "allowed_actions": _allowed_card_actions(
                        card.card_type, card.status, effective, bool(card.replaced_by_card_id)
                    ),
                    "daily_limit_cents": card.daily_limit.cents,
                }
            )
        return {
            "as_of": deps.today.isoformat(),
            "synced_at": datetime.now(UTC).isoformat(),
            "default_shipping_address": DEFAULT_SHIPPING_ADDRESS,
            "cards": rows,
        }

    @app.get("/api/payees")
    def payees(user_id: str = Depends(current_user)) -> dict[str, object]:
        """收款人名册（只读事实）：姓名 + 账号尾号 + 手机尾号后四位。

        **不回传完整手机号**：名册层只存后四位的事实不进接口面；
        更不允许前端据此拼出完整号码。
        """

        def tail4(value: str | None) -> str:
            return (value or "")[-4:]

        return {
            "payees": [
                _payee_payload(record.payload, tail4)
                for record in deps.gateway.payees_of(user_id)
            ]
        }

    @app.post("/api/payees")
    def create_payee(
        request: _CreatePayeeRequest, user_id: str = Depends(current_user)
    ) -> dict[str, object]:
        """登记演示收款人：只保存姓名 + 账号后四位 + 开户行 + 手机号后四位。

        这不是资金动作；执行仍走 `transfer.create`，由核心银行校验收款人存在与 blocked。
        """
        writer = deps.payee_writer
        if writer is None:
            raise HTTPException(status_code=409, detail="收款人名册登记未装配")
        try:
            payee = writer.add(  # type: ignore[attr-defined]
                user_id=user_id,
                full_name=request.full_name,
                account_tail=request.account_tail,
                bank_name=request.bank_name,
                phone_tail=request.phone_tail4,
            )
        except PayeeValidationError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        deps.audit.append(
            AuditEvent(
                event_id=f"payee_create_{secrets.token_hex(8)}",
                kind=KIND_EXECUTION,
                payload={
                    "action": "payee.create",
                    "user_id": user_id,
                    "payee_id": str(payee.get("payee_id", "")),
                    "account_tail": str(payee.get("account_tail", "")),
                    "bank_name": str(payee.get("bank_name", "")),
                },
                prev_hash="",
            )
        )
        return {"payee": payee}

    @app.get("/api/risk-assessment")
    def risk_assessment_state(user_id: str = Depends(current_user)) -> dict[str, object]:
        """当前已认证用户的测评状态（只读）；未测评 / 过期如实返回，绝不默认等级。"""
        return _risk_payload(deps, user_id)

    @app.post("/api/risk-assessment", response_model=None)
    def save_risk_assessment(
        request: _RiskAssessmentRequest, user_id: str = Depends(current_user)
    ) -> dict[str, object] | JSONResponse:
        """用户侧测评录入（真实测评入口）。

        - **问卷路径**（`answers`）：固定 5 题，等级由 `scenarios.wealth_assessment` 算出，
          请求体不得同时给 `risk_profile`（模型不得自填等级），落库 `source='questionnaire'`；
        - **每日次数**：同一用户每个演示业务日最多提交 `RETAKES_PER_DAY` 次问卷，超限 429，
          额度与次数都随响应下发（口径是**演示产品规则**，不是监管要求）；
        - **直接录入路径**（`risk_profile`）：保留兼容，落库 `source='user_input'`，不计次；
        - **身份**只取登录用户，请求体不能指定他人；
        - 日期取服务端 `deps.today`，客户端不能把过期测评"刷新"成有效；
        - 只写测评事实，不触碰任何理财交易；录入后推荐仍走同一读路径与服务端硬筛选。
        """
        writer = deps.risk_writer
        if writer is None:
            raise HTTPException(status_code=404, detail="风险测评录入未装配")
        if request.answers is not None:
            if request.risk_profile is not None:
                raise HTTPException(
                    status_code=400,
                    detail="answers 与 risk_profile 只能二选一：等级由问卷算出，不能自填",
                )
            since = deps.today - timedelta(days=365)
            used_today = writer.attempts_on(user_id, deps.today)
            used_12m = writer.events_count_since(user_id, since)
            quota = retake_payload(
                used_today,
                deps.today,
                used_12m,
                writer.earliest_event_on(user_id, since),
            )
            if used_today >= RETAKES_PER_DAY or used_12m >= RETAKES_PER_12M:
                return JSONResponse(
                    status_code=429,
                    content={
                        "detail": (
                            "测评次数已用完（依据《金融机构产品适当性管理办法》第二十九条："
                            "单日不超过 2 次、12 个月累计不超过 8 次），"
                            f"下次可测：{quota['next_available_on']}；"
                            "演示环境按模拟业务日计数"
                        ),
                        "retake": quota,
                    },
                )
            try:
                result = evaluate_answers(request.answers)
            except AssessmentError as error:
                raise HTTPException(status_code=400, detail=str(error)) from error
            latest = (
                to_risk_assessment(deps.risk.assessment_for(user_id))
                if deps.risk is not None
                else None
            )
            changed = latest is not None and latest.risk_profile != result.risk_profile
            if changed and latest is not None and not request.confirm_change:
                return {
                    "state": "needs_reconfirmation",
                    "message": (
                        f"本次结果 {result.risk_profile}，与最近一次 {latest.risk_profile} "
                        "不一致；依据第二十九条需要提示并再次确认。"
                    ),
                    "previous_profile": latest.risk_profile,
                    "retake": quota,
                    "result": {
                        "risk_profile": result.risk_profile,
                        "score": result.score,
                        "max_score": result.max_score,
                    },
                }
            writer.record_attempt(user_id, deps.today)
            writer.save(
                user_id, result.risk_profile, deps.today, source=SOURCE_QUESTIONNAIRE,
            )
            writer.record_event(
                user_id,
                deps.today,
                result.risk_profile,
                result.score,
                source=SOURCE_QUESTIONNAIRE,
                previous_profile=latest.risk_profile if latest is not None else None,
            )
            return {
                "state": "updated",
                "assessment": _risk_payload(deps, user_id),
                "result": {
                    "risk_profile": result.risk_profile,
                    "score": result.score,
                    "max_score": result.max_score,
                    "answers": [
                        {"question_id": question_id, "option_id": option_id}
                        for question_id, option_id in result.answers
                    ],
                },
            }
        if request.risk_profile is None:
            raise HTTPException(status_code=400, detail="缺少 risk_profile 或 answers")
        try:
            writer.save(user_id, request.risk_profile.strip().upper(), deps.today)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return {"state": "updated", "assessment": _risk_payload(deps, user_id)}

    @app.get("/api/wealth/overview")
    def wealth_overview(user_id: str = Depends(current_user)) -> dict[str, object]:
        """理财面板聚合（只读）：测评状态 + 产品目录（逐只可申购判定与原因）+ 本人持仓。

        - 等级只认服务端测评事实：未测评 / 过期 ⇒ 目录全部标「未测评」，不默认等级；
        - 不可申购的产品**保留并写明原因**（页面据此灰显，不隐藏、不编造）；
        - 持仓金额 / 起息日来自 gateway 事实；净值曲线是 `gateway/seed.py` 生成的
          **模拟序列**（`simulated=true`），不接任何行情源；旧库无净值 ⇒ 空数组 + null，
          不填 0。
        """
        assessment = _risk_payload(deps, user_id)
        raw_profile = assessment["risk_profile"]
        risk_profile = (
            raw_profile
            if isinstance(raw_profile, str)
            and assessment["state"] == AssessmentState.VALID.value
            else None
        )
        products = deps.gateway.wealth_products()
        disclosures = {
            record.payload.product_id: record.payload
            for record in deps.gateway.wealth_disclosures()
        }
        profile = deps.gateway.customer_suitability_profile(user_id)
        birth_year = profile.payload.birth_year if profile is not None else None
        customer_age = deps.today.year - birth_year if birth_year is not None else None
        return {
            "assessment": assessment,
            "products": list(
                product_catalog(
                    products,
                    risk_profile,
                    nav=nav_by_product(deps.gateway, products),
                    disclosures=disclosures,
                )
            ),
            "holdings": list(
                holdings_payload(deps.gateway, user_id, disclosures=disclosures)
            ),
            "customer_age": customer_age,
            "senior_notice": customer_age is not None and customer_age >= 65,
            "birth_year_known": birth_year is not None,
            "data_source": PRODUCT_DATA_SOURCE,
        }

    @app.get("/api/wealth/products/{product_id}")
    def wealth_product_detail(
        product_id: str, user_id: str = Depends(current_user)
    ) -> dict[str, object]:
        """产品详情：披露、四份演示文件、是否首次购买、匹配意见。"""
        try:
            product = deps.gateway.wealth_product(product_id)
        except GatewayNotFoundError as error:
            raise HTTPException(status_code=404, detail="理财产品不存在") from error
        disclosure = deps.gateway.wealth_disclosure(product_id)
        disclosures = (
            {product_id: disclosure.payload} if disclosure is not None else {}
        )
        entry = product_catalog(
            [product], _valid_risk_profile(deps, user_id), disclosures=disclosures
        )[0]
        profile = deps.gateway.customer_suitability_profile(user_id)
        birth_year = profile.payload.birth_year if profile is not None else None
        customer_age = deps.today.year - birth_year if birth_year is not None else None
        held = any(
            record.payload.product_id == product_id
            for record in deps.gateway.wealth_holdings_of(user_id)
        )
        return {
            **entry,
            "documents": wealth_docs.documents_payload(),
            "declaration_text": wealth_docs.DECLARATION_TEXT,
            "first_purchase": not held,
            "customer_age": customer_age,
            "senior_notice": customer_age is not None and customer_age >= 65,
            "birth_year_known": birth_year is not None,
        }

    @app.get("/api/wealth/redemption-quote")
    def wealth_redemption_quote(
        product_id: str,
        mode: str = "all",
        value: str | None = None,
        user_id: str = Depends(current_user),
    ) -> dict[str, object]:
        """赎回试算：全部 / 按份额 / 按金额；返回到账、快赎、大额预警。"""
        if mode not in {"all", "shares", "amount"}:
            raise HTTPException(status_code=400, detail="mode 只能是 all / shares / amount")
        disclosures = {
            record.payload.product_id: record.payload
            for record in deps.gateway.wealth_disclosures()
        }
        rows = holdings_payload(deps.gateway, user_id, disclosures=disclosures)
        position = next((row for row in rows if row["product_id"] == product_id), None)
        if position is None:
            raise HTTPException(status_code=404, detail="没有该产品的持仓")
        disclosure = disclosures.get(product_id)
        position_shares = int(str(position["share_bp"]))
        position_cents = int(
            (Decimal(str(position["amount_yuan"])) * 100).to_integral_value()
        )
        series = deps.gateway.wealth_nav_of(product_id)
        nav_bp = series[-1].payload.nav_bp if series else 10000
        if not bool(position["redeemable"]):
            return {
                "redeemable": False,
                "reason": position["redeem_reason"],
                "position": _position_payload(position),
                "nav": _nav_text(nav_bp),
                "nav_note": "模拟净值，实际以 T 日净值为准",
            }
        if mode == "all":
            shares_bp = position_shares
            amount_cents = position_cents
        elif mode == "shares":
            if value is None:
                raise HTTPException(status_code=400, detail="按份额赎回需要 value")
            try:
                shares_bp = int((Decimal(value) * 10000).to_integral_value())
            except (InvalidOperation, ValueError) as error:
                raise HTTPException(status_code=400, detail="份额格式不正确") from error
            amount_cents = shares_bp * nav_bp // 1_000_000
        else:
            if value is None:
                raise HTTPException(status_code=400, detail="按金额赎回需要 value")
            try:
                amount_cents = int((Decimal(value) * 100).to_integral_value())
            except (InvalidOperation, ValueError) as error:
                raise HTTPException(status_code=400, detail="金额格式不正确") from error
            shares_bp = amount_cents * 1_000_000 // nav_bp
        if shares_bp <= 0 or amount_cents <= 0:
            raise HTTPException(status_code=400, detail="赎回份额或金额必须大于 0")
        if shares_bp > position_shares:
            raise HTTPException(status_code=400, detail="赎回份额超过当前持仓")
        if amount_cents > position_cents:
            raise HTTPException(status_code=400, detail="赎回金额超过当前持仓")
        product_type = (
            disclosure.product_type if disclosure is not None else "unknown"
        )
        fast_limit = (
            disclosure.fast_redeem_limit_cents
            if disclosure is not None
            else None
        )
        fast_redeem = (
            product_type == "cash"
            and fast_limit is not None
            and amount_cents <= fast_limit
        )
        redeem_mode = disclosure.redeem_mode if disclosure is not None else "unknown"
        if product_type == "cash":
            arrival_mode = "realtime" if fast_redeem else "t_plus_1"
            estimated = deps.today if fast_redeem else deps.today + timedelta(days=1)
        elif redeem_mode == "t_plus_1":
            arrival_mode = "t_plus_1"
            estimated = deps.today + timedelta(days=1)
        elif redeem_mode == "t_plus_3":
            arrival_mode = "t_plus_3"
            estimated = deps.today + timedelta(days=3)
        else:
            arrival_mode = "unknown"
            estimated = deps.today
        large = shares_bp * 2 > position_shares
        percent = round(shares_bp * 100 / position_shares, 1) if position_shares else 0.0
        return {
            "redeemable": True,
            "mode": mode,
            "shares": _share_text(shares_bp),
            "share_bp": shares_bp,
            "amount_yuan": _yuan_text(amount_cents),
            "amount_cents": amount_cents,
            "nav": _nav_text(nav_bp),
            "nav_note": "按最新模拟净值折算，实际以 T 日净值结算为准",
            "arrival": {
                "mode": arrival_mode,
                "label": _arrival_text(arrival_mode),
                "estimated_date": estimated.isoformat(),
            },
            "fast_redeem": fast_redeem,
            "fast_redeem_limit_yuan": (
                _yuan_text(fast_limit) if fast_limit is not None else None
            ),
            "large_redemption": large,
            "percent_of_holding": percent,
            "warning": (
                f"该操作将赎回您该产品持仓的 {percent}%，请确认" if large else ""
            ),
            "position": _position_payload(position),
        }

    @app.post("/api/wealth/acknowledgements")
    def wealth_acknowledgements(
        request: _WealthAckRequest, user_id: str = Depends(current_user)
    ) -> dict[str, object]:
        """保存交易确认留痕；四件套、阅读时长、抄录与类型差异确认不通过即拒绝。"""
        writer = deps.wealth_trades
        if writer is None:
            raise HTTPException(status_code=404, detail="交易确认留痕未装配")
        if request.action not in {"subscribe", "redeem"}:
            raise HTTPException(status_code=400, detail="action 只能是 subscribe / redeem")
        try:
            product = deps.gateway.wealth_product(request.product_id)
        except GatewayNotFoundError as error:
            raise HTTPException(status_code=404, detail="理财产品不存在") from error
        disclosure = deps.gateway.wealth_disclosure(request.product_id)
        display_risk = (
            disclosure.payload.display_risk_level
            if disclosure is not None
            else product.payload.risk_level
        )
        missing = [
            doc_id
            for doc_id in wealth_docs.DOCUMENT_IDS
            if doc_id not in request.doc_versions or doc_id not in request.read_seconds
        ]
        if missing:
            raise HTTPException(
                status_code=400, detail=f"缺少文件确认：{'、'.join(missing)}"
            )
        short = [
            doc_id
            for doc_id in wealth_docs.DOCUMENT_IDS
            if request.read_seconds[doc_id] < 3
        ]
        if short:
            raise HTTPException(
                status_code=400, detail=f"每份文件至少阅读 3 秒：{'、'.join(short)}"
            )
        held = any(
            record.payload.product_id == request.product_id
            for record in deps.gateway.wealth_holdings_of(user_id)
        )
        first_purchase = not held
        if first_purchase and request.action == "subscribe" and display_risk in {
            "R3", "R4", "R5"
        }:
            if request.read_seconds.get("risk_disclosure", 0) < 15:
                raise HTTPException(
                    status_code=400, detail="首次购买 R3 及以上产品，风险揭示书至少阅读 15 秒"
                )
            if (request.declaration_text or "").strip() != wealth_docs.DECLARATION_TEXT:
                raise HTTPException(
                    status_code=400, detail="风险揭示声明必须逐字抄录且完全一致"
                )
        product_type = disclosure.payload.product_type if disclosure is not None else ""
        if request.action == "subscribe" and product_type in {"mixed", "equity"}:
            if not request.experience_confirmed or not request.loss_ack:
                raise HTTPException(
                    status_code=400, detail="混合/权益类需确认投资经验与本金损失风险"
                )
        profile = deps.gateway.customer_suitability_profile(user_id)
        birth_year = profile.payload.birth_year if profile is not None else None
        customer_age = deps.today.year - birth_year if birth_year is not None else None
        if (
            request.action == "subscribe"
            and customer_age is not None
            and customer_age >= 65
            and display_risk in {"R3", "R4", "R5"}
            and not request.senior_ack
        ):
            raise HTTPException(status_code=400, detail="65 岁以上客户需确认特别提示")
        ack_id = writer.save_trade_ack(
            user_id=user_id,
            product_id=request.product_id,
            action=request.action,
            doc_versions=request.doc_versions,
            read_seconds=request.read_seconds,
            declaration_text=request.declaration_text,
            dividend_method=request.dividend_method,
            auto_redeem_pref=request.auto_redeem_pref,
            account_id=request.account_id,
        )
        return {
            "state": "recorded",
            "ack_id": ack_id,
            "first_purchase": first_purchase,
            "declaration_required": first_purchase
            and request.action == "subscribe"
            and display_risk in {"R3", "R4", "R5"},
        }

    @app.get("/api/bills/analysis")
    def bill_analysis(
        request: Request,
        year: int | None = None,
        month: int | None = None,
        user_id: str = Depends(_read_user),
    ) -> dict[str, object]:
        """只读账单异常识别 + 结论式解释；不写事实、不产生执行授权。

        缺省账期为服务端时钟的**当月**（数据截至今日）；显式传 `year` / `month` 时按参数。
        `period_basis` 如实说明账期来源，`data_through` 为该账期的数据截止日
        （当月最后一天与今日取小）。金额全部来自确定性识别引擎并按分返回，
        模型只补充文字解释（失败时 `narrative.degraded=true`，不得当作成功分析）。
        """
        explicit = year is not None or month is not None
        target_year = deps.today.year if year is None else year
        target_month = deps.today.month if month is None else month
        if not 2000 <= target_year <= 2100 or not 1 <= target_month <= 12:
            raise HTTPException(status_code=400, detail="账期非法，需为 2000-2100 年与 1-12 月")
        period_end = date(target_year, target_month, monthrange(target_year, target_month)[1])
        data_through = min(period_end, deps.today)
        period_str = f"{target_year:04d}-{target_month:02d}"
        lead_states = lead_states_for(bill_store, user_id, period_str)
        overrides = category_overrides_for(bill_store, user_id)
        report = analyze_bill(
            deps.gateway,
            user_id,
            target_year,
            target_month,
            as_of=deps.today,
            overrides=overrides,
        )
        narrative = narrate_bill_findings(report, deps.bill_narrator)
        profile = bill_profile(deps.gateway, user_id, report, overrides=overrides)
        shares = category_shares(
            deps.gateway, user_id, target_year, target_month, overrides=overrides
        )
        trend = expense_trend(
            deps.gateway, user_id, target_year, target_month, overrides=overrides
        )
        merchants = top_merchants(
            deps.gateway, user_id, target_year, target_month, overrides=overrides
        )
        total_budget, category_budgets = (
            deps.insights.budgets(user_id, period_str)
            if deps.insights is not None
            else (None, {})
        )
        month_start = date(target_year, target_month, 1)
        days_left = (
            (period_end - deps.today).days + 1
            if month_start <= deps.today <= period_end
            else 0
        )
        budget = budget_progress(
            month=period_str,
            consumption_cents=report.consumption_cents,
            shares=shares,
            total_budget_cents=total_budget,
            category_budgets=category_budgets,
            days_left=days_left,
            is_current_month=month_start <= deps.today <= period_end,
        )
        return {
            "demo": _is_anonymous(request),
            **report_payload(report, lead_states=lead_states),
            "period_basis": "explicit" if explicit else "current_month",
            "data_through": data_through.isoformat(),
            "server_month": deps.today.strftime("%Y-%m"),
            "trend": [
                {
                    "period": point.period,
                    "expense_cents": point.expense_cents,
                    "income_cents": point.income_cents,
                    "categories": [
                        {
                            "category": item.category,
                            "category_l1": item.category_l1,
                            "expense_cents": item.expense_cents,
                            "share": str(item.share),
                        }
                        for item in point.categories
                    ],
                    "mom": (
                        {
                            "previous_period": point.mom.previous_period,
                            "change_cents": point.mom.change_cents,
                            "change_pct": (
                                str(point.mom.change_pct)
                                if point.mom.change_pct is not None
                                else None
                            ),
                            "contributions": [
                                {
                                    "category": row.category,
                                    "change_cents": row.change_cents,
                                }
                                for row in point.mom.contributions
                            ],
                        }
                        if point.mom is not None
                        else None
                    ),
                }
                for point in trend
            ],
            "category_share": [
                {
                    "category": item.category,
                    "category_l1": item.category_l1,
                    "mcc": item.mcc,
                    "mcc_label": item.mcc_label,
                    "expense_cents": item.expense_cents,
                    "share": str(item.share),
                }
                for item in shares
            ],
            "top_merchants": [
                {
                    "merchant_key": item.merchant_key,
                    "merchant_label": item.merchant_label,
                    "amount_cents": item.amount_cents,
                    "count": item.count,
                    "share": str(item.share),
                    "category_l1": item.category_l1,
                    "category_l2": item.category_l2,
                    "mcc": item.mcc,
                    "mcc_label": item.mcc_label,
                }
                for item in merchants
            ],
            "category_options": category_option_payload(),
            "budget": budget,
            "profile": {
                "top_category": profile.top_category,
                "top_category_cents": profile.top_category_cents,
                "top_category_share": (
                    str(profile.top_category_share)
                    if profile.top_category_share is not None
                    else None
                ),
                "subscription_monthly_cents": profile.subscription_monthly_cents,
                "suspicious_count": profile.suspicious_count,
                "max_suspicious_cents": profile.max_suspicious_cents,
            },
            "narrative": {
                "summary": narrative.summary,
                "findings": [
                    {
                        "finding_id": item.finding_id,
                        "explanation": item.explanation,
                        "suggestion": item.suggestion,
                    }
                    for item in narrative.findings
                ],
                "degraded": narrative.degraded,
                "degrade_reason": narrative.degrade_reason,
                "channel": narrative.channel,
            },
        }

    @app.post("/api/bills/leads/{lead_id}")
    def set_bill_lead_state(
        lead_id: str,
        request: _LeadStateRequest,
        user_id: str = Depends(current_user),
    ) -> dict[str, object]:
        """核对线索：写独立元数据，刷新后不再重复提示。"""
        if not _valid_bill_period(request.period):
            raise HTTPException(status_code=400, detail="账期格式必须为 YYYY-MM")
        if request.state not in LEAD_STATES:
            raise HTTPException(status_code=400, detail="线索状态不合法")
        if not _valid_lead_id(lead_id):
            raise HTTPException(status_code=400, detail="线索编号不合法")
        try:
            bill_store.set_lead_state(
                user_id, request.period, lead_id, request.state, request.note[:200]
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return {
            "recorded": True,
            "lead_id": lead_id,
            "state": request.state,
            "state_label": LEAD_STATE_LABELS.get(request.state, request.state),
        }

    @app.post("/api/bills/category-override")
    def set_bill_category_override(
        request: _CategoryOverrideRequest,
        user_id: str = Depends(current_user),
    ) -> dict[str, object]:
        """记住用户对商户分类的修正；只影响后续分析归类。"""
        merchant_key = normalize_merchant(request.merchant_key)
        if not merchant_key or len(merchant_key) > 80:
            raise HTTPException(status_code=400, detail="商户标识不合法")
        if not is_valid_category(request.category):
            raise HTTPException(status_code=400, detail="分类不在可选范围内")
        try:
            bill_store.set_category_override(user_id, merchant_key, request.category)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return {
            "recorded": True,
            "merchant_key": merchant_key,
            "category": request.category,
        }

    @app.post("/api/bills/lead-resolution")
    def resolve_bill_lead(
        request: _LeadResolutionRequest,
        user_id: str = Depends(current_user),
    ) -> dict[str, object]:
        """对话回写：按确定性短语把用户说明映射为线索状态。

        这里不做资金判断、不产生执行计划；只把「这笔是转给朋友的」这类
        明确说明记录为核对结果，避免同一线索反复出现。
        """
        if not _valid_bill_period(request.period):
            raise HTTPException(status_code=400, detail="账期格式必须为 YYYY-MM")
        if not _valid_lead_id(request.lead_id):
            raise HTTPException(status_code=400, detail="线索编号不合法")
        state = _bill_lead_state_from_utterance(request.utterance)
        if state is None:
            return {
                "recorded": False,
                "lead_id": request.lead_id,
                "state": "",
                "state_label": "",
                "message": "没有识别到明确的核对结论；可以点「确认正常 / 标记异常 / 忽略」。",
            }
        bill_store.set_lead_state(
            user_id, request.period, request.lead_id, state, request.utterance[:200]
        )
        label = LEAD_STATE_LABELS.get(state, state)
        return {
            "recorded": True,
            "lead_id": request.lead_id,
            "state": state,
            "state_label": label,
            "message": f"已按您的说明把这条线索标记为「{label}」，之后不再重复提示。",
        }

    @app.get("/api/insights")
    def insights(
        request: Request, include_resolved: bool = False, user_id: str = Depends(_read_user)
    ) -> dict[str, object]:
        """读取当前用户的只读洞察收件箱；不会继承任何执行授权。

        启动时只扫默认用户；其他用户首次访问时**懒扫描一次**，否则永远空列表，
        用户会误以为「没有异常」而不是「还没扫过」。
        """
        demo = _is_anonymous(request)
        service = deps.insights
        if service is None:
            return {"demo": demo, "insights": [], "scanned_at": None, "scan_error": None}
        if service.scan_state(user_id) is None:
            service.scan(user_id, deps.today)
        state = service.scan_state(user_id)
        return {
            "demo": demo,
            "insights": [
                item.to_dict()
                for item in service.list_inbox(user_id, include_resolved=include_resolved)
            ],
            "scanned_at": state.scanned_at if state is not None else None,
            "scan_error": state.error if state is not None else None,
        }

    @app.get("/api/transfers")
    def transfer_records(
        days: int = 30, user_id: str = Depends(current_user)
    ) -> dict[str, object]:
        """只读转账记录（收款人脱敏）：AI 做过的事必须查得到、点得开。

        状态口径：已扣款且记账日不晚于业务日 ⇒「已到账」；记账日在未来 ⇒「待到账」；
        `scheduled_transfers` 里仍待执行的登记 ⇒「待到账」(定时)。
        本端点**只读**，不返回任何可执行动作，也不构成执行授权。
        """
        if not 1 <= days <= 365:
            raise HTTPException(status_code=400, detail="查询区间需为 1-365 天")
        gateway = deps.gateway
        payees = [record.payload for record in gateway.payees_of(user_id)]
        by_id = {payee.payee_id: payee for payee in payees}
        by_name: dict[str, list[Payee]] = {}
        for payee in payees:
            by_name.setdefault(payee.full_name, []).append(payee)
        accounts = gateway.accounts_of(user_id)
        kinds = {record.payload.account_id: record.payload.kind for record in accounts}
        labels = {"savings": "储蓄账户", "current": "活期账户"}
        card_tails: dict[str, str] = {}
        for record in gateway.cards_of(user_id):
            card = record.payload
            if card.account_id and card.last4:
                card_tails.setdefault(card.account_id, card.last4)
        start = deps.today - timedelta(days=days)
        horizon = deps.today + timedelta(days=days)
        items: list[dict[str, object]] = []

        def payee_for(raw: str) -> Payee | None:
            found = by_id.get(raw)
            if found is not None:
                return found
            same_name = by_name.get(raw, [])
            return same_name[0] if same_name else None

        for account in accounts:
            for txn_record in gateway.transactions_between(
                account.payload.account_id, start, horizon
            ):
                txn = txn_record.payload
                if txn.category != "转账" or txn.direction != "out":
                    continue
                raw_payee = txn.merchant.removeprefix("转给")
                matched_payee: Payee | None = payee_for(raw_payee)
                source = "seed" if _is_seed_transaction(txn.txn_id) else "live"
                method = txn.transfer_method or ("history" if source == "seed" else "realtime")
                purpose = txn.transfer_purpose or ""
                items.append(
                    {
                        "kind": "transaction",
                        "record_id": txn.txn_id,
                        "source": source,
                        "payee_id": matched_payee.payee_id if matched_payee else "",
                        "payee": _display_payee_name(
                            matched_payee.full_name if matched_payee else raw_payee
                        ),
                        "payee_account_tail": matched_payee.account_tail if matched_payee else "",
                        "payee_bank": (matched_payee.bank_name or "") if matched_payee else "",
                        "payee_phone_tail4": (
                            (matched_payee.phone_tail or "")[-4:] if matched_payee else ""
                        ),
                        "amount_cents": -txn.amount.cents,
                        "booked_on": txn.booked_on.isoformat(),
                        "status": "已到账" if txn.booked_on <= deps.today else "处理中",
                        "method": method,
                        "method_label": _transfer_method_label(method),
                        "eta_label": _transfer_eta_label(method),
                        "fee_cents": int(txn.transfer_fee_cents or 0),
                        "purpose": purpose,
                        "purpose_label": _transfer_purpose_label(purpose),
                        "remark": txn.transfer_remark or "",
                        "account": labels.get(kinds.get(account.payload.account_id, ""), "账户"),
                        "account_tail": card_tails.get(account.payload.account_id, ""),
                        "channel": txn.channel,
                        "can_repeat": matched_payee is not None and not matched_payee.blocked,
                        "can_cancel": False,
                    }
                )
        for booking_record in gateway.scheduled_transfers_of(user_id):
            booking = booking_record.payload
            if booking.status == "completed":
                continue
            if not start <= booking.booked_on <= horizon:
                continue
            scheduled_payee: Payee | None = by_id.get(booking.payee_id)
            method = booking.method or "next_day"
            cancelled = booking.status == "cancelled"
            items.append(
                {
                    "kind": "scheduled",
                    "record_id": booking.schedule_id,
                    "source": "live",
                    "payee_id": booking.payee_id,
                    "payee": _display_payee_name(
                        scheduled_payee.full_name
                        if scheduled_payee is not None
                        else booking.payee_id
                    ),
                    "payee_account_tail": (
                        scheduled_payee.account_tail if scheduled_payee else ""
                    ),
                    "payee_bank": (
                        (scheduled_payee.bank_name or "") if scheduled_payee else ""
                    ),
                    "payee_phone_tail4": (
                        (scheduled_payee.phone_tail or "")[-4:] if scheduled_payee else ""
                    ),
                    "amount_cents": booking.amount.cents,
                    "booked_on": booking.booked_on.isoformat(),
                    "status": (
                        "已撤销"
                        if cancelled
                        else (
                            "已发出申请·可撤销"
                            if booking.booked_on > deps.today
                            else "已发出申请·待处理"
                        )
                    ),
                    "method": method,
                    "method_label": _transfer_method_label(method),
                    "eta_label": _transfer_eta_label(method),
                    "fee_cents": int(booking.fee_cents or 0),
                    "purpose": booking.purpose or "",
                    "purpose_label": _transfer_purpose_label(booking.purpose or ""),
                    "remark": booking.remark,
                    "account": labels.get(kinds.get(booking.account_id, ""), "账户"),
                    "account_tail": card_tails.get(booking.account_id, ""),
                    "channel": "agent",
                    "can_repeat": scheduled_payee is not None and not scheduled_payee.blocked,
                    "can_cancel": booking.status == "scheduled",
                }
            )
        items.sort(key=lambda item: str(item["booked_on"]), reverse=True)
        items.sort(key=lambda item: item["status"] == "已到账")
        summary: dict[str, dict[str, object]] = {}
        for item in items:
            key = str(item["payee_id"] or item["payee"])
            row = summary.setdefault(
                key,
                {
                    "payee": item["payee"],
                    "payee_account_tail": item["payee_account_tail"],
                    "count": 0,
                    "total_cents": 0,
                    "last_on": item["booked_on"],
                },
            )
            row["count"] = int(str(row["count"])) + 1
            row["total_cents"] = int(str(row["total_cents"])) + int(str(item["amount_cents"]))
        return {
            "items": items,
            "payee_summary": sorted(
                summary.values(), key=lambda row: int(str(row["total_cents"])), reverse=True
            ),
            "as_of": deps.today.isoformat(),
            "window_days": days,
        }

    @app.post("/api/transfers/cancel")
    def cancel_scheduled_transfer(
        request: _ScheduleRequest, user_id: str = Depends(current_user)
    ) -> dict[str, object]:
        """撤销一笔**尚未执行**的次日/定时转账登记（登记时不扣款，撤销不动资金）。"""
        writer = deps.transfer_schedules
        if writer is None:
            raise HTTPException(status_code=409, detail="定时转账撤销未装配")
        changed = writer.cancel(  # type: ignore[attr-defined]
            user_id=user_id, schedule_id=request.schedule_id
        )
        if not changed:
            raise HTTPException(status_code=409, detail="该笔申请不可撤销或已执行")
        deps.audit.append(
            AuditEvent(
                event_id=f"transfer_cancel_{secrets.token_hex(8)}",
                kind=KIND_EXECUTION,
                payload={
                    "action": "transfer.cancel",
                    "user_id": user_id,
                    "schedule_id": request.schedule_id,
                    "status": "cancelled",
                },
                prev_hash="",
            )
        )
        return {"cancelled": True, "schedule_id": request.schedule_id}

    @app.post("/api/insights/budget")
    def set_insight_budget(
        request: _BudgetRequest, user_id: str = Depends(current_user)
    ) -> dict[str, object]:
        """保存提醒偏好并重新扫描；预算不是银行额度或执行授权。"""
        service = deps.insights
        if service is None:
            raise HTTPException(status_code=404, detail="主动洞察未装配")
        month = request.month or deps.today.strftime("%Y-%m")
        if len(month) != 7 or month[4] != "-":
            raise HTTPException(status_code=400, detail="月份格式必须为 YYYY-MM")
        try:
            if request.categories is None:
                service.set_budget(user_id, month, request.budget_cents)
            else:
                service.set_budgets(
                    user_id, month, request.budget_cents, request.categories
                )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        result = service.scan(user_id, deps.today)
        return {
            "state": "updated",
            "scan_error": result.scan_error,
            "insights": [item.to_dict() for item in result.insights],
        }

    @app.post("/api/due")
    def due(request: _EmptyRequest, user_id: str = Depends(current_user)) -> dict[str, object]:
        # 到期日只取服务端时钟；客户端不能提前触发未来预约。
        try:
            view = _session_for(request.session_id, user_id).check_due()
        except SessionStateError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        _record_answer(user_id, request.session_id, view)
        return _response(request.session_id, user_id, _submission_payload(view))

    @app.post("/api/life-schedules/retry")
    def retry_life_schedule(
        request: _ScheduleRequest, user_id: str = Depends(current_user)
    ) -> dict[str, object]:
        """`failed` 预约放回 `scheduled`：只认本人预约，次数受服务端上限约束。"""
        store = deps.life_events
        if store is None:
            raise HTTPException(status_code=409, detail="生活事件存储未装配")
        try:
            changed = store.retry_schedule(user_id=user_id, schedule_id=request.schedule_id)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        if not changed:
            raise HTTPException(status_code=409, detail="该预约不可重试（未失败或已达重试上限）")
        return {"retried": True, "schedule_id": request.schedule_id}

    @app.post("/api/life-schedules/cancel")
    def cancel_life_schedule(
        request: _ScheduleRequest, user_id: str = Depends(current_user)
    ) -> dict[str, object]:
        """放弃预约：置为 `cancelled`，非 `scheduled` 状态幂等不生效。"""
        store = deps.life_events
        if store is None:
            raise HTTPException(status_code=409, detail="生活事件存储未装配")
        changed = store.finish_schedule(
            user_id=user_id,
            schedule_id=request.schedule_id,
            status=SCHEDULE_CANCELLED,
        )
        if not changed:
            raise HTTPException(status_code=409, detail="该预约不可取消")
        return {"cancelled": True, "schedule_id": request.schedule_id}

    @app.post("/api/submit")
    def submit(request: _SubmitRequest, user_id: str = Depends(current_user)) -> dict[str, object]:
        session = _session_for(request.session_id, user_id)
        on_stage = _progress_callback(user_id, request.session_id, request.request_id)
        if request.request_id:
            deps.progress.start(user_id, request.session_id, request.request_id)
        try:
            view = session.submit(
                request.utterance,
                selected=request.selected,
                on_stage=on_stage,
            )
        except SessionStateError as error:
            _append_channel_audit(
                user_id=user_id,
                channel="web",
                direction="inbound",
                status="failed",
                reason="session_state_error",
                event_id=request.request_id,
                text=request.utterance,
                details={"session_id": request.session_id},
            )
            health.record_event("web", success=False)
            LOGGER.warning(
                "channel_event channel=web event_id=%s status=failed "
                "reason=session_state_error error_type=%s",
                request.request_id or "",
                type(error).__name__,
            )
            raise HTTPException(status_code=409, detail=str(error)) from error
        except Exception as error:
            _append_channel_audit(
                user_id=user_id,
                channel="web",
                direction="inbound",
                status="failed",
                reason="submit_failed",
                event_id=request.request_id,
                text=request.utterance,
                details={"session_id": request.session_id},
            )
            health.record_event("web", success=False)
            LOGGER.warning(
                "channel_event channel=web event_id=%s status=failed "
                "reason=submit_failed error_type=%s",
                request.request_id or "",
                type(error).__name__,
            )
            raise
        finally:
            if request.request_id:
                deps.progress.finish(user_id, request.session_id, request.request_id)
        _record_message(user_id, request.session_id, "user", "text", request.utterance)
        view = _record_life_event(
            user_id, request.session_id, request.utterance, request.selected, view
        )
        _record_answer(user_id, request.session_id, view)
        payload = _submission_payload(view)
        card = payload.get("card")
        card_map = card if isinstance(card, dict) else {}
        _append_channel_audit(
            user_id=user_id,
            channel="web",
            direction="inbound",
            status="sent",
            event_id=request.request_id,
            text=request.utterance,
            details={
                "session_id": request.session_id,
                "request_kind": "message",
                "state": str(payload.get("state", "")),
                "level": str(card_map.get("level", "")),
                "card_id": str(card_map.get("card_id", "")),
                "frozen_hash": "",
            },
        )
        health.record_event("web", success=True)
        return _response(request.session_id, user_id, payload)

    @app.post("/api/clarify")
    def clarify(
        request: _ClarifyRequest, user_id: str = Depends(current_user)
    ) -> dict[str, object]:
        """澄清闭环：用补充答案重跑上一条指令，用户无需整句重述。"""
        session = _session_for(request.session_id, user_id)
        pending_utterance = session.last_utterance()
        on_stage = _progress_callback(user_id, request.session_id, request.request_id)
        if request.request_id:
            deps.progress.start(user_id, request.session_id, request.request_id)
        try:
            view = session.clarify(request.answers, on_stage=on_stage)
        except SessionStateError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        finally:
            if request.request_id:
                deps.progress.finish(user_id, request.session_id, request.request_id)
        _record_message(
            user_id, request.session_id, "user", "clarify",
            "；".join(f"{slot}={value}" for slot, value in request.answers.items()),
        )
        if pending_utterance:
            view = _record_life_event(
                user_id, request.session_id, pending_utterance, request.answers, view,
            )
        _record_answer(user_id, request.session_id, view)
        return _response(request.session_id, user_id, _submission_payload(view))

    @app.post("/api/verify/request")
    def request_verification(
        request: _EmptyRequest, user_id: str = Depends(current_user)
    ) -> dict[str, object]:
        """申请与会话及冻结计划绑定的一次性强验证挑战。"""
        try:
            prompt = _session_for(request.session_id, user_id).request_verification()
        except (SessionStateError, StrongVerificationRequiredError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        result: dict[str, object] = {
            "mode": prompt.mode,
            "status": prompt.status,
            "channel": prompt.channel,
            "target": prompt.target,
            "expires_in_s": prompt.expires_in_s,
        }
        if prompt.mode == "demo":
            result["code"] = prompt.code
        return result

    @app.post("/api/confirm")
    def confirm(
        request: _ConfirmRequest, user_id: str = Depends(current_user)
    ) -> dict[str, object]:
        try:
            view = _session_for(request.session_id, user_id).confirm(
                request.card_id,
                frozen_hash=request.frozen_hash,
                verification_code=request.verification_code,
                steps=request.steps,
            )
        except (
            SessionStateError,
            ConfirmationMismatchError,
            StrongVerificationRequiredError,
            StalePlanError,
        ) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        if view.scheduled_for is not None:
            # 未来日期生活事件只登记预约：今天没有冻结、没有下单，记录保持 `planned`，
            # 到期重新提案并再次确认后才转 `executed`（由预约结算负责）。
            pass
        elif view.state.value in ("completed", "partial"):
            _mark_life_event(user_id, request.session_id, STATUS_EXECUTED, view)
        elif view.state.value in _ERROR_STATES:
            _mark_life_event(user_id, request.session_id, STATUS_FAILED, view)
        _record_message(
            user_id, request.session_id, "assistant", "receipt", view.message,
            {"receipt": _receipt_payload(view)},
        )
        return _response(
            request.session_id,
            user_id,
            _execution_payload(view, deps.gateway, user_id, deps.today),
        )

    @app.post("/api/reject")
    def reject(request: _EmptyRequest, user_id: str = Depends(current_user)) -> dict[str, object]:
        """**放弃当前待确认计划**（G-02）：服务端置作废终态，原计划与已发验证码一并失效。"""
        try:
            view = _session_for(request.session_id, user_id).reject()
        except SessionStateError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        _mark_life_event(user_id, request.session_id, STATUS_REJECTED)
        _record_message(
            user_id, request.session_id, "assistant", "receipt", view.message,
            {"receipt": _receipt_payload(view)},
        )
        return _session_payload(view)

    @app.post("/api/interrupt")
    def interrupt(
        request: _EmptyRequest, user_id: str = Depends(current_user)
    ) -> dict[str, object]:
        try:
            view = _session_for(request.session_id, user_id).interrupt()
        except SessionStateError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return _session_payload(view)

    @app.post("/api/resume")
    def resume(request: _EmptyRequest, user_id: str = Depends(current_user)) -> dict[str, object]:
        try:
            view = _session_for(request.session_id, user_id).resume()
        except SessionStateError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        _record_message(
            user_id, request.session_id, "assistant", "receipt", view.message,
            {"receipt": _receipt_payload(view)},
        )
        return _response(
            request.session_id,
            user_id,
            _execution_payload(view, deps.gateway, user_id, deps.today),
        )

    @app.post("/api/reverse")
    def reverse(request: _EmptyRequest, user_id: str = Depends(current_user)) -> dict[str, object]:
        """回退：对本次执行逐条走补偿事务（无 `settled` 参数：模拟核心银行执行即结算）。

        作用域与幂等口径：只补偿**当前用户 + 当前会话**已执行的回执，跨用户 / 跨会话
        不可达；不存在「未结算 ⇒ 无扣款」档，一律走补偿。已回退过的会话再次调用会
        因状态不是「已完成 / 部分完成」而返回 409，**不会重复冲正**；补偿失败如实
        返回 `failed`，不宣称已取消。
        """
        try:
            outcome = _session_for(request.session_id, user_id).reverse()
        except SessionStateError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        _record_message(
            user_id, request.session_id, "assistant", "receipt", outcome.message,
            {"receipt": {
                "state": outcome.state.value,
                "message": outcome.message,
                "results": [],
                "record_ids": [],
            }},
        )
        return {
            "state": outcome.state.value,
            "message": outcome.message,
            "compensated_steps": list(outcome.compensated_steps),
        }

    @app.get("/api/takeover")
    def takeover(
        session_id: str = DEFAULT_SESSION_ID, user_id: str = Depends(current_user)
    ) -> dict[str, object]:
        try:
            package = _session_for(session_id, user_id).takeover_package()
        except SessionStateError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return _takeover_payload(package)

    @app.post("/api/operator/breaker-unlock")
    def breaker_unlock(request: Request, body: _UnlockRequest) -> dict[str, object]:
        """**运维解锁**：全局熔断的**唯一**降级出口（裁决 #27）。

        纪律（每一条都不可放宽）：
        - **凭证**：请求头 `X-Operator-Token` 必须与 `AppDeps.operator_token`（环境变量
          `AIB_OPERATOR_TOKEN`）一致，用**定长比较**防时序侧信道；
        - **未配置口令 ⇒ 一律 403**（fail-closed）——不存在「无口令也能解锁」的配置；
        - **必须给理由**（空理由 400）——运维动作要能被人读懂；
        - **必须留痕**：解锁前后状态 + 触发项 + 理由写入审计链（`KIND_OPERATOR`），
          「唯一降级出口 + 二次确认 + 留痕」的纪律不变；
        - 只解锁**全局**熔断器；**会话级**熔断由会话自身生命周期决定（新开会话即隔离）。
        """
        expected = deps.operator_token or ""
        provided = request.headers.get("X-Operator-Token", "")
        if not expected or not secrets.compare_digest(provided, expected):
            raise HTTPException(status_code=403, detail="运维口令未配置或不正确")

        reason = body.reason.strip()
        if not reason:
            raise HTTPException(status_code=400, detail="必须填写解锁理由")

        with manager.lock:
            before_state = deps.breaker.state().value
            before_triggers = [trigger.value for trigger in deps.breaker.triggers()]
            try:
                deps.audit.append(
                    AuditEvent(
                        event_id=f"{KIND_OPERATOR}:breaker-unlock:{secrets.token_hex(8)}",
                        kind=KIND_OPERATOR,
                        payload={
                            "action": "breaker-unlock",
                            "reason": reason,
                            "scope": "global",
                            "state_before": before_state,
                            "triggers_before": before_triggers,
                            "state_after": before_state,
                            "authorized_state_after": "closed",
                            "unlock_completed": False,
                        },
                        prev_hash="",
                    )
                )
            except Exception as error:
                # Keep the shared breaker OPEN until the authorization is durable.
                raise HTTPException(status_code=503, detail="审计记录失败，熔断保持锁定") from error
            # The audit append above intentionally runs while the breaker is still OPEN.
            deps.breaker.unlock()
            try:
                deps.audit.append(
                    AuditEvent(
                        event_id=f"{KIND_OPERATOR}:breaker-unlock-completed:{secrets.token_hex(8)}",
                        kind=KIND_OPERATOR,
                        payload={
                            "action": "breaker-unlock-completed",
                            "reason": reason,
                            "scope": "global",
                            "state_before": before_state,
                            "state_after": deps.breaker.state().value,
                            "authorized_state_after": "closed",
                            "unlock_completed": True,
                        },
                        prev_hash="",
                    )
                )
            except Exception as error:
                deps.breaker.lock()
                raise HTTPException(status_code=503, detail="解锁结果未留痕，熔断已回锁") from error
        return {
            "action": "breaker-unlock",
            "reason": reason,
            "state_before": before_state,
            "triggers_before": before_triggers,
            "state_after": deps.breaker.state().value,
        }

    # ---------- 登录 / 账号 ----------

    @app.get("/login", response_class=HTMLResponse)
    def login_page() -> HTMLResponse:
        """登录页：未登录只读壳上的「登录」入口；未装配登录时统一 404。"""
        if deps.auth is None:
            raise HTTPException(status_code=404, detail="登录未装配")
        accounts = tuple(row[0] for row in DEMO_ACCOUNTS)
        return HTMLResponse(
            render_login_page(accounts, deps.demo_password),
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/api/auth/login")
    def auth_login(
        request: Request, body: _LoginRequest, response: Response
    ) -> dict[str, object]:
        """登录：校验口令后签发 Cookie 会话；口令不回显、不写日志。

        会话 Cookie 固定 `HttpOnly` + `SameSite=Lax` + `Path=/`；`Secure` 由
        `AIB_COOKIE_SECURE=1` 打开（对外 HTTPS 部署必开）。同一账号连续输错口令
        会**临时锁定**（起步 60 秒、逐次翻倍、上限 300 秒，见 `web/auth.py`），
        锁定期间返回 429；口令失败不会泄露账号是否存在（登录失败统一 401）。
        """
        if deps.auth is None:
            raise HTTPException(status_code=404, detail="登录未装配")
        _check_csrf(request)
        result = _auth().login(
            body.username.strip(),
            body.password,
            user_agent=request.headers.get("user-agent", "")[:200],
            client_ip=request.client.host if request.client is not None else "",
        )
        response.set_cookie(
            AUTH_COOKIE_NAME,
            result.session.token,
            max_age=SESSION_IDLE_SECONDS,
            httponly=True,
            samesite="lax",
            secure=_cookie_secure(),
            path="/",
        )
        return {
            "state": "signed_in",
            "user": _user_payload(result.user),
            "expires_at": result.session.expires_at.isoformat(),
        }

    @app.post("/api/auth/logout")
    def auth_logout(
        request: Request, response: Response, user_id: str = Depends(current_user)
    ) -> dict[str, object]:
        """退出：撤销 token → 清内存会话 → 清 Cookie；落库快照保留（下次登录可回看）。"""
        revoked = False
        if auth_enabled:
            revoked = _auth().logout(request.cookies.get(AUTH_COOKIE_NAME, ""))
            manager.drop(user_id)
        response.delete_cookie(AUTH_COOKIE_NAME, path="/")
        return {"state": "signed_out", "revoked": revoked}

    @app.post("/api/auth/reverify")
    def auth_reverify(
        body: _ReverifyRequest, user_id: str = Depends(current_user)
    ) -> dict[str, object]:
        """二次验证：校验口令换**一次性**短时票据（删除类动作用，不复用登录态）。"""
        if body.purpose != TICKET_PURPOSE_DELETE_CONVERSATION:
            raise HTTPException(status_code=400, detail="unsupported_purpose")
        service = _auth()
        if not service.verify_password(user_id, body.password):
            raise HTTPException(status_code=403, detail="reverify_failed")
        return {
            "state": "verified",
            "ticket": service.issue_ticket(user_id, body.purpose),
            "expires_in_s": TICKET_TTL_SECONDS,
        }

    @app.get("/api/me")
    def me(request: Request, user_id: str = Depends(current_user)) -> dict[str, object]:
        """个人中心：身份 / 本次登录 / 最近登录 / 关联账户数（只读）。"""
        profile: dict[str, object] = {"user_id": user_id}
        session_payload: dict[str, object] = {"authenticated": False}
        logins: list[dict[str, object]] = []
        if auth_enabled and deps.auth is not None:
            service = deps.auth
            found = service.user_by_id(user_id)
            if found is not None:
                profile = _user_payload(found)
            auth_session = getattr(request.state, "auth_session", None)
            if isinstance(auth_session, AuthSession):
                session_payload = {
                    "authenticated": True,
                    "issued_at": auth_session.issued_at.isoformat(),
                    "expires_at": auth_session.expires_at.isoformat(),
                }
            logins = [
                {
                    "issued_at": record.issued_at.isoformat(),
                    "expires_at": record.expires_at.isoformat(),
                    "user_agent": record.user_agent,
                    "client_ip": record.client_ip,
                    "revoked": record.revoked,
                }
                for record in service.recent_logins(user_id, limit=5)
            ]
        return {
            "user": profile,
            "session": session_payload,
            "recent_logins": logins,
            "channel_bindings": _channel_binding_count(user_id),
            "feishu": {
                "configured": feishu.configured,
                "mode": feishu.config.mode,
                "replay": feishu.replay_mode,
                "linked": _feishu_linked(user_id),
            },
            "payees": len(deps.gateway.payees_of(user_id)),
            "accounts": len(deps.gateway.accounts_of(user_id)),
            "auth_mode": deps.auth_mode,
        }

    # ---------- 对话历史 ----------

    @app.get("/api/conversations")
    def conversations(user_id: str = Depends(current_user)) -> dict[str, object]:
        """左栏会话列表；未装配存储时如实返回 `available=false`，不伪造空历史。"""
        store = deps.conversations
        if store is None:
            return {"conversations": [], "available": False}
        return {
            "conversations": [
                {**summary.to_dict(), "state": _session_state_of(user_id, summary.session_id)}
                for summary in store.list_conversations(user_id)
            ],
            "available": True,
        }

    @app.get("/api/conversations/{session_id}")
    def conversation_detail(
        session_id: str, user_id: str = Depends(current_user)
    ) -> dict[str, object]:
        """点开历史：消息流水 + 当前会话状态 + 待确认计划卡（若有）。"""
        store = deps.conversations
        messages = (
            [message.to_dict() for message in store.list_messages(user_id, session_id)]
            if store is not None
            else []
        )
        snapshot_state = _session_state_of(user_id, session_id)
        if not messages and not snapshot_state:
            raise HTTPException(status_code=404, detail="conversation_not_found")
        view = _session_for(session_id, user_id).current_view()
        return {
            "session_id": session_id,
            "messages": messages,
            "state": view.state.value,
            "snapshot_state": snapshot_state,
            "frozen_hash": view.frozen_hash,
            "card": _card_payload(view.card, view.plan),
            "available": store is not None,
        }

    @app.delete("/api/conversations/{session_id}")
    def delete_conversation(
        session_id: str, request: Request, user_id: str = Depends(current_user)
    ) -> dict[str, object]:
        """删除一条对话历史：消息 + 快照 + 内存会话；`demo` 模式要求一次性票据。"""
        if auth_enabled:
            _auth().consume_ticket(
                user_id,
                TICKET_PURPOSE_DELETE_CONVERSATION,
                request.headers.get(REVERIFY_HEADER, ""),
            )
        removed_messages = (
            deps.conversations.delete(user_id, session_id)
            if deps.conversations is not None
            else 0
        )
        removed_snapshot = (
            deps.session_repository.delete(user_id, session_id)
            if deps.session_repository is not None
            else 0
        )
        manager.drop_one(user_id, session_id)
        if removed_messages == 0 and removed_snapshot == 0:
            raise HTTPException(status_code=404, detail="conversation_not_found")
        return {
            "state": "deleted",
            "session_id": session_id,
            "removed_messages": removed_messages,
            "removed_snapshot": removed_snapshot,
        }

    # ---------- 生活事件 / 进度 / 审计 ----------

    @app.get("/api/life-events")
    def life_events(
        request: Request, user_id: str = Depends(_read_user)
    ) -> dict[str, object]:
        """生活事件面板：原型清单 + 组合律 + 本用户最近 5 条记录。

        未登录访客看到的是**演示账户**的只读视图，响应里 `demo=true` 显式标注。
        """
        store = deps.life_events
        records = () if store is None else store.list_for_user(user_id, limit=5)
        private_records = (
            ()
            if store is None
            else store.list_for_user(user_id, limit=5, visibility=PRIVATE_VISIBILITY)
        )
        # 已登记但未到期的预约：按 `record_id` 折叠进同一条记录，不再单独列一块。
        schedules = () if store is None else store.active_schedules(user_id)
        schedule_by_record = {
            schedule.record_id: schedule
            for schedule in schedules
            if schedule.record_id is not None
        }
        # 旧版本记录没有 `steps_json`，用它关联的预约节点快照做**只读**回填。
        schedule_nodes = {} if store is None else store.nodes_by_record(user_id)

        def _record_payload(record: LifeEventRecord) -> dict[str, object]:
            payload = record.to_dict()
            if not payload.get("steps"):
                payload["steps"] = list(
                    _steps_from_nodes(schedule_nodes.get(record.record_id, ()))
                )
            steps = payload.get("steps")
            payload["steps"] = _enrich_life_steps(
                deps.gateway, steps if isinstance(steps, list) else (),
            )
            schedule = schedule_by_record.get(record.record_id)
            trigger_text = (
                schedule.due_on.isoformat()
                if schedule is not None
                else record.due_on
            )
            trigger_on: date | None = None
            if trigger_text:
                try:
                    trigger_on = date.fromisoformat(str(trigger_text)[:10])
                except ValueError:
                    trigger_on = None
            payload.update({
                "schedule": None if schedule is None else {
                    "schedule_id": schedule.schedule_id,
                    "due_on": schedule.due_on.isoformat(),
                    "kind": schedule.kind,
                    "notify_on": (
                        None if schedule.notify_on is None else schedule.notify_on.isoformat()
                    ),
                },
                # 日历落点：有触发日就用触发日，没有才回落到创建时间
                "calendar_on": trigger_text or record.created_at,
                "budget": _life_budget_progress(
                    deps.gateway, user_id, record.hold_ref, record.occasion_ref,
                ),
                "funding": _life_funding_for(
                    deps.gateway,
                    user_id,
                    amount_cents=record.amount_cents,
                    trigger_on=trigger_on,
                    today=deps.today,
                    record=record,
                ),
                "orders": _life_orders(deps.gateway, record),
            })
            return payload

        record_payloads = [
            {
                **_record_payload(record),
                # 兼容字段：预约类型 / 触发日（与 `schedule` 同源）
                "schedule_kind": (
                    schedule_by_record[record.record_id].kind
                    if record.record_id in schedule_by_record
                    else "order"
                ),
                "schedule_due_on": (
                    schedule_by_record[record.record_id].due_on.isoformat()
                    if record.record_id in schedule_by_record
                    else None
                ),
            }
            for record in records
        ]
        private_payloads = [
            {**_record_payload(record), "budget": None} for record in private_records
        ]

        return {
            "demo": _is_anonymous(request),
            "prototypes": [
                {
                    "event_id": prototype.event_id,
                    "name": prototype.name,
                    "domains": list(prototype.domains),
                    "description": prototype.description,
                    "triggers": list(prototype.triggers),
                    "example": prototype.example,
                    # 该类型下的常见具体事件：类型只是链路形状，具体事件才是用户要办的事
                    "cases": [
                        {"label": label, "utterance": utterance}
                        for label, utterance in prototype.cases
                    ],
                    "steps": len(prototype.skeleton),
                    "skeleton": list(prototype.skeleton),
                }
                for prototype in LIFE_EVENT_PROTOTYPES
            ],
            "combos": [
                {"combo_id": combo.combo_id, "name": combo.name}
                for combo in LIFE_EVENT_COMBOS
            ],
            "records": record_payloads,
            # 已登记预约：只读列出到期日 / 类型 / 冻结的步骤；到期后由 /api/due 重新提案。
            "schedules": [
                {
                    "schedule_id": schedule.schedule_id,
                    "session_id": schedule.session_id,
                    "record_id": schedule.record_id,
                    "event_id": schedule.event_id,
                    "event_title": schedule.event_title or schedule.event_id,
                    "due_on": schedule.due_on.isoformat(),
                    "kind": schedule.kind,
                    "notify_on": (
                        None if schedule.notify_on is None else schedule.notify_on.isoformat()
                    ),
                    "status": schedule.status,
                    "steps": list(_steps_from_nodes(schedule.nodes)),
                }
                for schedule in schedules
            ],
            # 同一场合出现两个用钱日：只提示，不替用户改期 / 合并记录
            "date_conflicts": _life_date_conflicts([*record_payloads, *private_payloads]),
            # 私密记录单独返回：普通列表里看不到，前端默认折叠成一行计数。
            "private_records": private_payloads,
            "private_count": len(private_records),
            "records_available": store is not None,
            "maturities": _life_maturities(deps.gateway, user_id, deps.today),
        }

    @app.get("/api/progress")
    def progress(
        request_id: str = "",
        session_id: str = DEFAULT_SESSION_ID,
        user_id: str = Depends(current_user),
    ) -> dict[str, object]:
        """问答阶段：只有真实执行过的阶段才有记录，过期 / 未知一律返回空态。"""
        if not request_id:
            return _empty_progress()
        state = deps.progress.get(user_id, session_id, request_id)
        return _empty_progress() if state is None else state.to_dict()

    @app.get("/api/audit")
    def audit_trail(
        limit: int = 10, user_id: str = Depends(current_user)
    ) -> dict[str, object]:
        """个人中心「我的操作轨迹」：按审计 payload 里的 `user_id` 反向过滤。

        审计本身是**哈希链**：每条记录带 `prev_hash` / `entry_hash`，默认 `hmac-sha256`
        （密钥取 `AIB_AUDIT_KEY`，未配置时落在同目录 `audit.jsonl.key`）；链断裂即
        `chain_ok=false` 并停止返回轨迹。服务启动时会先校验全链，校验不过直接拒绝启动。
        响应里的 `chain_head` 是当前链头，可与离线副本比对。
        """
        bounded = max(1, min(limit, 50))
        chain_ok = deps.audit.verify_chain()
        try:
            events = deps.audit.events()
        except ChainBrokenError:
            LOGGER.warning("审计链读取失败，轨迹暂不可用", exc_info=True)
            return {"events": [], "chain_head": "", "chain_ok": False, "available": False}
        mine = [
            {
                "event_id": event.event_id,
                "kind": event.kind,
                "timestamp": event.timestamp,
                "payload": event.payload,
            }
            for event in events
            if str(event.payload.get("user_id", "")) == user_id
        ]
        mine.reverse()
        return {
            "events": mine[:bounded],
            "chain_head": deps.audit.head(),
            "chain_ok": chain_ok,
            "available": True,
        }

    @app.post("/api/im/message")
    def im_message(
        request: _ImMessageRequest, user_id: str = Depends(current_user)
    ) -> dict[str, object]:
        """IM 入站文本：走**与 Web 同一条**会话状态机（渠道只搬消息）。"""
        try:
            inbound = InboundMessage(
                channel=CHANNEL_LOCAL,
                external_user_id=request.user_id,
                external_chat_id=request.chat_id,
                event_id=request.event_id,
                text=request.text,
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        try:
            outbound = _dispatcher_for(user_id).dispatch(inbound)
            response = _channel_payload(outbound)
        except ChannelRateLimited:
            raise
        except Exception:
            health.record_event(CHANNEL_LOCAL, success=False)
            raise
        if outbound.replayed:
            health.record_replay(CHANNEL_LOCAL)
        else:
            health.record_event(CHANNEL_LOCAL, success=True)
        return response

    @app.post("/api/im/confirm")
    def im_confirm(
        request: _ImConfirmRequest, user_id: str = Depends(current_user)
    ) -> dict[str, object]:
        """IM 计划卡确认：`card_id + frozen_hash` 原样交给会话层做冻结校验。"""
        try:
            command = ConfirmCommand(
                channel=CHANNEL_LOCAL,
                external_user_id=request.user_id,
                external_chat_id=request.chat_id,
                event_id=request.event_id,
                card_id=request.card_id,
                frozen_hash=request.frozen_hash,
                verification_code=request.verification_code,
                steps=request.steps,
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        try:
            outbound = _dispatcher_for(user_id).dispatch(command)
            response = _channel_payload(outbound)
        except ChannelRateLimited:
            raise
        except (
            SessionStateError,
            ConfirmationMismatchError,
            StrongVerificationRequiredError,
            StalePlanError,
        ) as error:
            health.record_event(CHANNEL_LOCAL, success=False)
            raise HTTPException(status_code=409, detail=str(error)) from error
        except Exception:
            health.record_event(CHANNEL_LOCAL, success=False)
            raise
        if outbound.replayed:
            health.record_replay(CHANNEL_LOCAL)
        else:
            health.record_event(CHANNEL_LOCAL, success=True)
        return response

    @app.post("/api/im/verify/request")
    def request_im_verification(
        request: _ImVerificationRequest, user_id: str = Depends(current_user)
    ) -> dict[str, object]:
        """申请绑定到当前 IM 会话及冻结计划的一次性强验证挑战。"""
        session_id = f"{CHANNEL_LOCAL}:{request.chat_id or DEFAULT_CHAT_ID}"
        try:
            prompt = _session_for(session_id, user_id).request_verification()
        except (SessionStateError, StrongVerificationRequiredError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        result: dict[str, object] = {
            "mode": prompt.mode,
            "status": prompt.status,
            "channel": prompt.channel,
            "target": prompt.target,
            "expires_in_s": prompt.expires_in_s,
        }
        if prompt.mode == "demo":
            result["code"] = prompt.code
        return result

    @app.post("/api/channels/feishu/link-code")
    def create_feishu_link_code(
        user_id: str = Depends(current_user),
    ) -> dict[str, object]:
        """个人中心「连接飞书」：签发 6 位一次性绑定码。

        用户把这 6 位发给机器人（飞书侧），服务端在未绑定回调里消费该码并落绑定，
        因此 Web 端不需要知道自己的 `open_id`。

        防枚举口径：码 **5 分钟过期**、**一次性消费**、同一用户重签即覆盖旧码；
        飞书入站按外部身份限流（每分钟 20 次），且未绑定身份在绑定前一律 401。
        """
        if not feishu.configured:
            raise HTTPException(status_code=409, detail="feishu_not_configured")
        code, ttl = _issue_link_code(user_id)
        return {
            "channel": CHANNEL_FEISHU,
            "code": code,
            "expires_in_s": ttl,
            "instruction": "在飞书里直接把这 6 位数字发给「AI Banking」机器人即可完成绑定。",
        }

    @app.delete("/api/channels/feishu/link")
    def delete_feishu_link(user_id: str = Depends(current_user)) -> dict[str, object]:
        """解除当前账号的飞书绑定（只删自己这一行，不动别人的）。"""
        removed = _unbind_feishu(user_id)
        return {"channel": CHANNEL_FEISHU, "removed": removed}

    @app.get("/api/channels/health")
    def channels_health(user_id: str = Depends(current_user)) -> dict[str, object]:
        """渠道健康页：如实区分「已配置 / 未配置」，不把 stub 说成可用。"""
        body = _dispatcher_for(user_id).health()
        channels = body["channels"]
        if isinstance(channels, dict):
            channels["web"] = {"configured": True, "mode": "web", "reason": ""}
            channels[CHANNEL_FEISHU] = {
                "configured": feishu.configured,
                "mode": feishu.config.mode,
                "linked": _feishu_linked(user_id),
                "reason": (
                    "replay_mode" if feishu.replay_mode
                    else ("" if feishu.configured else "not_configured")
                ),
                "card_action": feishu.configured and not feishu.replay_mode,
            }
        body["health"] = health.snapshot()
        return body

    def _release_feishu_event(event_id: str | None) -> None:
        if not event_id:
            return
        if event_ledger is not None:
            event_ledger.release(event_id)
        else:
            memory_event_ids.discard(event_id)

    def _dispatch_live_feishu(
        material: InboundMessage | ConfirmCommand,
        user_id: str,
        event_id: str | None,
    ) -> None:
        dispatcher = _dispatcher_for(user_id)
        try:
            outbound = dispatcher.dispatch_live(material)
            if (
                isinstance(material, InboundMessage)
                and outbound.state == "awaiting_confirm"
                and outbound.level == "GREEN"
            ):
                # 绿＝只可能是纯查询。Web 端是浏览器拿到绿卡后自动点一次确认，
                # IM 没有前端，这里补同一步：直接执行并回执，不再要求用户点按钮。
                outbound = dispatcher.resume_green(material, outbound)
        except DispatchOutcomeUnknown as error:
            _append_channel_audit(
                user_id=user_id,
                channel="feishu",
                direction="inbound",
                status="failed",
                reason="dispatch_outcome_unknown",
                event_id=event_id,
                external_user_id=material.external_user_id,
                text=material.text if isinstance(material, InboundMessage) else None,
                best_effort=True,
            )
            LOGGER.warning(
                "channel_event channel=feishu event_id=%s status=failed "
                "reason=dispatch_outcome_unknown error_type=%s",
                event_id or "",
                type(error).__name__,
            )
            health.record_event(CHANNEL_FEISHU, success=False)
            return
        except Exception as error:
            _release_feishu_event(event_id)
            LOGGER.warning(
                "channel_event channel=feishu event_id=%s status=failed "
                "reason=dispatch_failed error_type=%s",
                event_id or "",
                type(error).__name__,
            )
            health.record_event(CHANNEL_FEISHU, success=False)
            return

        def record_attempt(
            attempt_no: int, status: int | None, error_type: str | None
        ) -> None:
            _append_channel_audit(
                user_id=user_id,
                channel="feishu",
                direction="outbound",
                status="attempt",
                reason=error_type or "",
                event_id=event_id,
                external_user_id=material.external_user_id,
                attempt_no=attempt_no,
                http_status=status,
                best_effort=True,
            )
            LOGGER.info(
                "channel_send channel=feishu event_id=%s status=attempt "
                "attempt_no=%s http_status=%s reason=%s",
                event_id or "",
                attempt_no,
                status if status is not None else "",
                error_type or "",
            )

        def record_retry(attempt_no: int, _delay: float) -> None:
            health.record_retry(CHANNEL_FEISHU)
            LOGGER.warning(
                "channel_send channel=feishu event_id=%s status=retry "
                "attempt_no=%s reason=retry_scheduled",
                event_id or "",
                attempt_no,
            )

        try:
            status = feishu.send(
                outbound,
                on_attempt=record_attempt,
                on_retry=record_retry,
            )
        except Exception as error:
            _append_channel_audit(
                user_id=user_id,
                channel="feishu",
                direction="outbound",
                status="failed",
                reason="send_failed",
                event_id=event_id,
                external_user_id=material.external_user_id,
                best_effort=True,
            )
            LOGGER.warning(
                "channel_send channel=feishu event_id=%s status=failed "
                "reason=send_failed error_type=%s",
                event_id or "",
                type(error).__name__,
            )
            health.record_event(CHANNEL_FEISHU, success=False)
            return
        success = 200 <= status < 300
        _append_channel_audit(
            user_id=user_id,
            channel="feishu",
            direction="outbound",
            status="sent" if success else "failed",
            reason="" if success else "send_failed",
            event_id=event_id,
            external_user_id=material.external_user_id,
            http_status=status,
            best_effort=True,
        )
        LOGGER.log(
            logging.INFO if success else logging.WARNING,
            "channel_send channel=feishu event_id=%s status=%s "
            "http_status=%s reason=%s",
            event_id or "",
            "sent" if success else "failed",
            status,
            "" if success else "send_failed",
        )
        health.record_event(CHANNEL_FEISHU, success=success)

    @app.post("/api/im/feishu")
    async def im_feishu(request: Request, background_tasks: BackgroundTasks) -> JSONResponse:
        """飞书回调：验签 → 解密 → 身份解析 → 幂等 → **按用户** dispatcher。

        免登录（外部回调），但**必须**验签 + 时间戳；身份只认 `channel_identities` 绑定，
        未绑定直接 401，**绝不**回落到 `deps.user_id`。
        """
        nonlocal event_ledger
        body = await request.body()
        if not feishu.verify(request.headers, body):
            # 飞书「请求地址校验」不带 X-Lark-* 签名头，challenge 分支改由
            # 「能解密（Encrypt Key）+ token 等于配置值」把关；事件推送不受此放行，
            # 缺签名头/签名不符照旧 401。
            challenge_text = feishu.challenge(body)
            if challenge_text is not None:
                health.record_event(CHANNEL_FEISHU, success=True)
                return JSONResponse(content={"challenge": challenge_text})
            _append_channel_audit(
                user_id="",
                channel=feishu_audit_channel,
                direction="inbound",
                status="rejected",
                reason="signature_invalid",
            )
            LOGGER.warning(
                "channel_event channel=%s event_id= status=rejected "
                "reason=signature_invalid",
                feishu_audit_channel,
            )
            health.record_event(CHANNEL_FEISHU, success=False)
            return JSONResponse(
                status_code=401,
                content={"detail": "signature_invalid", "channel": CHANNEL_FEISHU},
            )
        try:
            payload = feishu.decode_body(body)
            event = feishu.parse_event(payload)
        except FeishuProtocolError as error:
            reason = str(error)
            if reason.startswith("unsupported_event:"):
                # 飞书会推很多我们没实现的类型（如 im.message.message_read_v1）。
                # 对「已订阅但本应用不处理」的事件必须 ACK 200，否则飞书按失败重试，
                # 日志被重投刷屏；这里如实记 audited ignored，不假装处理过。
                _append_channel_audit(
                    user_id="",
                    channel=feishu_audit_channel,
                    direction="inbound",
                    status="ignored",
                    reason=reason,
                )
                LOGGER.info(
                    "channel_event channel=%s event_id= status=ignored reason=%s",
                    feishu_audit_channel,
                    reason,
                )
                return JSONResponse(content={"code": 0, "ignored": True})
            _append_channel_audit(
                user_id="",
                channel=feishu_audit_channel,
                direction="inbound",
                status="rejected",
                reason="protocol_error",
                details={"error": reason},
            )
            LOGGER.warning(
                "channel_event channel=%s event_id= status=rejected "
                "reason=protocol_error error_type=%s error=%s",
                feishu_audit_channel,
                type(error).__name__,
                reason,
            )
            health.record_event(CHANNEL_FEISHU, success=False)
            return JSONResponse(
                status_code=400,
                content={"detail": str(error), "channel": CHANNEL_FEISHU},
            )
        if event.challenge is not None:
            health.record_event(CHANNEL_FEISHU, success=True)
            return JSONResponse(content={"challenge": event.challenge})
        command = event.command
        inbound = event.inbound
        material = command if command is not None else inbound
        if material is None:
            _append_channel_audit(
                user_id="",
                channel=feishu_audit_channel,
                direction="inbound",
                status="rejected",
                reason="message_event_missing",
                event_id=event.event_id,
            )
            LOGGER.warning(
                "channel_event channel=%s event_id=%s status=rejected "
                "reason=message_event_missing",
                feishu_audit_channel,
                event.event_id or "",
            )
            health.record_event(CHANNEL_FEISHU, success=False)
            return JSONResponse(
                status_code=400,
                content={"detail": "message_event_missing", "channel": CHANNEL_FEISHU},
            )
        external_user_id = material.external_user_id
        binding = (
            identities.resolve(CHANNEL_FEISHU, external_user_id)
            if external_user_id
            else None
        )
        if binding is None:
            linked_user = (
                _claim_link_code(material.text)
                if isinstance(material, InboundMessage)
                and feishu.configured
                and external_user_id
                else None
            )
            if linked_user is not None:
                _bind_feishu_identity(external_user_id or "", linked_user)
                _append_channel_audit(
                    user_id=linked_user,
                    channel="feishu",
                    direction="inbound",
                    status="linked",
                    reason="link_code_accepted",
                    event_id=event.event_id,
                    external_user_id=external_user_id,
                )
                LOGGER.info(
                    "channel_event channel=%s event_id=%s status=linked "
                    "reason=link_code_accepted",
                    feishu_audit_channel,
                    event.event_id or "",
                )
                health.record_event(CHANNEL_FEISHU, success=True)
                background_tasks.add_task(
                    _send_feishu_notice,
                    material.external_chat_id or external_user_id or "",
                    "飞书已连接。现在可以直接在这里说「查余额」「给李娜转五百」。",
                )
                return JSONResponse(
                    content={
                        "code": 0,
                        "channel": CHANNEL_FEISHU,
                        "event_id": event.event_id,
                        "linked": True,
                    }
                )
            _append_channel_audit(
                user_id="",
                channel=feishu_audit_channel,
                direction="inbound",
                status="rejected",
                reason="identity_unbound",
                event_id=event.event_id,
                external_user_id=external_user_id,
                text=material.text if isinstance(material, InboundMessage) else None,
            )
            LOGGER.warning(
                "channel_event channel=%s event_id=%s status=rejected "
                "reason=identity_unbound",
                feishu_audit_channel,
                event.event_id or "",
            )
            health.record_event(CHANNEL_FEISHU, success=False)
            return JSONResponse(
                status_code=401,
                content={
                    "detail": "identity_unbound",
                    "channel": CHANNEL_FEISHU,
                    "message": "该飞书账号尚未绑定，请先在 Web 端输入 6 位绑定码完成绑定",
                },
            )
        event_id = event.event_id
        if event_id:
            if event_ledger is None and deps.channel_store is not None:
                event_ledger = EventLedger(deps.channel_store)
            claimed = (
                event_ledger.claim(event_id)
                if event_ledger is not None
                else event_id not in memory_event_ids
            )
            if event_ledger is None and claimed:
                memory_event_ids.add(event_id)
            if not claimed:
                health.record_replay(CHANNEL_FEISHU)
                cached_response = replay_responses.get(event_id)
                if cached_response is not None:
                    return JSONResponse(content={**cached_response, "replayed": True})
                return JSONResponse(
                    content={
                        "code": 0,
                        "channel": CHANNEL_FEISHU,
                        "event_id": event_id,
                        "replayed": True,
                    }
                )
        if feishu.configured and not feishu.replay_mode:
            rate_identity = material.external_user_id or binding.user_id
            rate_key = f"{material.channel}:{rate_identity}"
            if event_id is None or not channel_limiter.reserve(rate_key, event_id):
                _release_feishu_event(event_id)
                _append_channel_audit(
                    user_id=binding.user_id,
                    channel="feishu",
                    direction="inbound",
                    status="rejected",
                    reason="rate_limited",
                    event_id=event_id,
                    external_user_id=material.external_user_id,
                    text=material.text if isinstance(material, InboundMessage) else None,
                    best_effort=True,
                )
                LOGGER.warning(
                    "channel_event channel=feishu event_id=%s status=rejected "
                    "reason=rate_limited",
                    event_id or "",
                )
                raise ChannelRateLimited("消息过于频繁，请稍后再试")
            envelope = {
                "code": 0,
                "channel": CHANNEL_FEISHU,
                "event_id": event_id,
                "accepted": True,
            }
            if command is not None:
                # 卡片回调要求在 3 秒内返回 200；这里只 ACK + toast，执行放后台，
                # 否则飞书客户端会报 200671（回调响应失败）。
                envelope["toast"] = {"type": "info", "content": "已收到，正在办理…"}
            background_tasks.add_task(
                _dispatch_live_feishu, material, binding.user_id, event_id
            )
            return JSONResponse(content=envelope)
        try:
            outbound = _dispatcher_for(binding.user_id).dispatch_live(material)
        except DispatchOutcomeUnknown as error:
            _append_channel_audit(
                user_id=binding.user_id,
                channel=feishu_audit_channel,
                direction="inbound",
                status="failed",
                reason="dispatch_outcome_unknown",
                event_id=event_id,
                external_user_id=material.external_user_id,
                text=material.text if isinstance(material, InboundMessage) else None,
                best_effort=True,
            )
            LOGGER.warning(
                "channel_event channel=%s event_id=%s status=failed "
                "reason=dispatch_outcome_unknown error_type=%s",
                feishu_audit_channel,
                event_id or "",
                type(error).__name__,
            )
            health.record_event(CHANNEL_FEISHU, success=False)
            raise
        except Exception as error:
            # 未执行成功（401/429/409/状态机拒绝）不得消费事件，否则平台重投只会拿到空回放。
            _release_feishu_event(event_id)
            LOGGER.warning(
                "channel_event channel=%s event_id=%s status=failed "
                "reason=dispatch_failed error_type=%s",
                feishu_audit_channel,
                event_id or "",
                type(error).__name__,
            )
            if not isinstance(error, ChannelRateLimited):
                health.record_event(CHANNEL_FEISHU, success=False)
            raise
        health.record_event(CHANNEL_FEISHU, success=True)
        response = _channel_payload(outbound)
        response["rendered"] = {
            "text": render_text(outbound),
            "feishu": feishu.render(outbound),
        }
        envelope = {
            "code": 0,
            "channel": CHANNEL_FEISHU,
            "event_id": event_id,
            "replayed": outbound.replayed,
            "data": response,
            "card": feishu.render(outbound),
        }
        if event_id:
            replay_responses[event_id] = envelope
        return JSONResponse(content=envelope)

    @app.get("/im", response_class=HTMLResponse)
    def im_page(request: Request) -> HTMLResponse:
        """本地联调页（骨架）：**不参与产品验收**，只用来验证 IM 渠道同一条状态机。"""
        return _page_or_login(request, _IM_PAGE_HTML)

    return app


def _mask_payee_name(name: str) -> str:
    """收款人姓名脱敏：三字及以上保留首末字，两字保留首字；空名一律不显示。"""
    if len(name) >= 3:
        return f"{name[0]}*{name[-1]}"
    if len(name) == 2:
        return f"{name[0]}*"
    return "收款人已隐藏"


def _display_payee_name(name: str) -> str:
    """界面展示用：解析不到时写「收款人未知」，不把解析失败说成隐私隐藏。"""
    masked = _mask_payee_name(name)
    return "收款人未知" if masked == "收款人已隐藏" else masked


def _payee_payload(
    payee: Payee, tail4: Callable[[str | None], str]
) -> dict[str, object]:
    tail = str(payee.account_tail or "")
    return {
        "payee_id": payee.payee_id,
        "name": payee.full_name,
        "account_tail": tail,
        "masked_account": f"**** **** **** {tail}" if tail else "",
        "bank_name": payee.bank_name or "",
        "phone_tail4": tail4(payee.phone_tail) or tail4(payee.phone),
        "identity_confirmed": payee.identity_confirmed,
        "blocked": payee.blocked,
    }


_TRANSFER_METHOD_LABELS = {
    "realtime": "实时到账",
    "normal": "普通到账",
    "next_day": "次日到账",
    "large": "大额通道",
    "history": "历史转账",
}

_TRANSFER_ETA_LABELS = {
    "realtime": "7×24 小时，预计秒到",
    "normal": "预计 2 小时内到账",
    "next_day": "次日 00:00 后发出，到账前可撤销",
    "large": "工作日大额通道处理，周末与节假日顺延（演示环境按强验证处理）",
    "history": "历史记录，时效以当时渠道为准",
}

_TRANSFER_PURPOSE_LABELS = {
    "rent": "房租",
    "salary": "工资",
    "reimburse": "报销",
    "other": "其他",
}


def _is_seed_transaction(txn_id: str) -> bool:
    """种子流水 id 固定为 `txn_YYYYMMDD`；演示中真实执行的 id 是 `txn_<hex>`。"""
    return re.fullmatch(r"txn_\d{8}", txn_id) is not None


def _transfer_method_label(method: str) -> str:
    return _TRANSFER_METHOD_LABELS.get(method, method or "—")


def _transfer_eta_label(method: str) -> str:
    return _TRANSFER_ETA_LABELS.get(method, "以银行为准")


def _transfer_purpose_label(purpose: str) -> str:
    return _TRANSFER_PURPOSE_LABELS.get(purpose, purpose or "")


def _transfer_quota_payload(
    gateway: FactGateway, user_id: str, today: date
) -> dict[str, object]:
    """转账额度只读口径：单笔 / 日累计 / 月累计三个维度。

    日累计沿用既有 PDP 口径（只加严不放宽）；单笔取卡片转账通道限额；
    月累计是**演示口径**（日累计 × 30），页面必须标注“演示”。
    """
    daily_limit = min(DAILY_LIMIT_YUAN * 100, gateway.daily_transfer_limit(user_id).cents)
    daily_used = gateway.daily_transfer_total(user_id, today).cents
    transfer_limits = [
        record.payload.transfer_limit.cents
        for record in gateway.cards_of(user_id)
        if record.payload.transfer_limit is not None
        and record.payload.status == "active"
    ]
    single_limit = min(50_000_00, max(transfer_limits)) if transfer_limits else daily_limit
    month_start = today.replace(day=1)
    monthly_used = 0
    for account in gateway.accounts_of(user_id):
        for txn_record in gateway.transactions_between(
            account.payload.account_id, month_start, today
        ):
            txn = txn_record.payload
            if txn.category == "转账" and txn.direction == "out":
                monthly_used += -txn.amount.cents
    monthly_limit = daily_limit * 30
    return {
        "as_of": today.isoformat(),
        "daily_limit_cents": daily_limit,
        "daily_accumulated_cents": daily_used,
        "single_limit_cents": single_limit,
        "monthly_limit_cents": monthly_limit,
        "monthly_accumulated_cents": monthly_used,
        "limit_notes": [
            "单笔限额来自卡片转账通道；日累计限额由演示风控规则固定。",
            "如需调整卡片通道限额，可在「卡片」页发起调整。",
            "历史记录可能发生在限额调整前；单笔超过当前限额不代表异常。",
            "月累计为演示口径（日累计 × 30），不构成监管或银行承诺。",
        ],
    }


def _retake_payload(deps: AppDeps, user_id: str) -> dict[str, object] | None:
    """当日与 12 个月重测额度；写通道未装配时返回 `None`（前端据此不显示次数）。"""
    writer = deps.risk_writer
    if writer is None:
        return None
    since = deps.today - timedelta(days=365)
    return retake_payload(
        writer.attempts_on(user_id, deps.today),
        deps.today,
        writer.events_count_since(user_id, since),
        writer.earliest_event_on(user_id, since),
    )


def _risk_payload(deps: AppDeps, user_id: str) -> dict[str, object]:
    """测评状态回显：`state` / 等级 / 日期 / 来源全部如实给出（不做乐观默认）。"""
    source = deps.risk
    if source is None:
        raise HTTPException(status_code=404, detail="风险测评服务未装配")
    assessment = to_risk_assessment(source.assessment_for(user_id))
    questionnaire = list(questionnaire_payload())
    retake = _retake_payload(deps, user_id)
    if assessment is None:
        return {
            "state": AssessmentState.UNASSESSED.value,
            "risk_profile": None,
            "assessed_on": None,
            "expires_on": None,
            "source": None,
            "source_label": None,
            "retake": retake,
            "questionnaire": questionnaire,
        }
    expires_on = assessment.assessed_on + timedelta(days=assessment.valid_days)
    days_until_expiry = (expires_on - deps.today).days
    return {
        "state": assessment.state_on(deps.today).value,
        "risk_profile": assessment.risk_profile,
        "assessed_on": assessment.assessed_on.isoformat(),
        "expires_on": expires_on.isoformat(),
        "valid_days": assessment.valid_days,
        "days_until_expiry": days_until_expiry,
        "expiring_soon": 0 <= days_until_expiry <= 30,
        "source": assessment.source or None,
        "source_label": RISK_SOURCE_LABELS.get(assessment.source),
        "retake": retake,
        "questionnaire": questionnaire,
    }


def _valid_risk_profile(deps: AppDeps, user_id: str) -> str | None:
    """只返回**有效**测评等级；未测评 / 过期 ⇒ `None`（不默认等级）。"""
    assessment = _risk_payload(deps, user_id)
    raw = assessment.get("risk_profile")
    if isinstance(raw, str) and assessment.get("state") == AssessmentState.VALID.value:
        return raw
    return None


def _yuan_text(cents: int) -> str:
    return f"{cents // 100}.{cents % 100:02d}"


DEFAULT_SHIPPING_ADDRESS = "上海市浦东新区世纪大道 100 号 1 栋 1201 室（演示地址）"
"""补卡弹窗的默认收货地址：**演示数据**，不是真实用户地址。"""

_CARD_BRAND_LABELS = {"unionpay": "银联", "mastercard": "万事达", "visa": "VISA"}
_CARD_LOCK_REASON_LABELS = {
    "cant_find": "找不到卡",
    "suspected_risk": "疑似风险",
    "other": "其他",
}
_CARD_LOSS_REASON_LABELS = {"lost": "遗失", "stolen": "被盗", "other": "其他"}


def _masked_card_no(last4: str | None) -> str:
    return f"**** **** **** {last4}" if last4 else "卡号未知"


def _card_expiry(month: int | None, year: int | None) -> str:
    if month is None or year is None:
        return ""
    return f"{month:02d}/{year % 100:02d}"


def _effective_card_status(status: str, temp_lock_until: str | None, today: date) -> str:
    """临时冻结到期按**读取时业务日**判定自动解挂（演示不跑后台任务）。"""
    if status != "locked" or not temp_lock_until:
        return status
    try:
        until = date.fromisoformat(temp_lock_until)
    except ValueError:
        return status
    return "active" if until < today else status


def _money_cents_or_none(money: Money | None) -> int | None:
    return None if money is None else money.cents


def _card_limits_payload(card: Card) -> dict[str, object]:
    if card.card_type == "credit":
        return {
            "kind": "credit",
            "credit_limit_cents": _money_cents_or_none(card.credit_limit),
            "temp_credit_limit_cents": _money_cents_or_none(card.temp_credit_limit),
            "temp_credit_until": card.temp_credit_until,
            "cycle_limit_cents": _money_cents_or_none(card.cycle_limit),
        }
    return {
        "kind": "debit",
        "daily_limit_cents": card.daily_limit.cents,
        "atm_limit_cents": _money_cents_or_none(card.atm_limit),
        "transfer_limit_cents": _money_cents_or_none(card.transfer_limit),
        "purchase_limit_cents": _money_cents_or_none(card.purchase_limit),
        "overseas_limit_cents": _money_cents_or_none(card.overseas_limit),
    }


def _temp_lock_payload(card: Card, today: date) -> dict[str, object] | None:
    if card.status != "locked" or not card.temp_lock_until:
        return None
    try:
        until = date.fromisoformat(card.temp_lock_until)
    except ValueError:
        return None
    return {
        "until": card.temp_lock_until,
        "days_left": max((until - today).days, 0),
        "expired": until < today,
        "reason": card.lock_reason or "",
        "reason_label": _CARD_LOCK_REASON_LABELS.get(card.lock_reason or "", "未说明"),
    }


def _lost_payload(card: Card) -> dict[str, object] | None:
    if card.status != "lost":
        return None
    return {
        "at": card.lost_at or "",
        "reason": card.loss_reason or "",
        "reason_label": (
            _CARD_LOSS_REASON_LABELS.get(card.loss_reason or "", "未说明")
            if card.lost_at else "未挂失（补卡作废）"
        ),
        "replaced_by_card_id": card.replaced_by_card_id or "",
    }


def _allowed_card_actions(
    card_type: str, status: str, effective_status: str, replaced: bool
) -> list[str]:
    """服务端计算的状态允许动作：前端按钮的唯一真源。"""
    if effective_status == "lost" or status == "lost":
        return ["view_lost_detail"] if replaced else ["replace", "view_lost_detail"]
    if effective_status == "locked":
        return ["unlock", "report_lost", "view_lock_detail"]
    if effective_status == "active":
        limits = (
            ["adjust_credit_limit", "set_cycle_limit"]
            if card_type == "credit"
            else ["adjust_debit_limit"]
        )
        return ["temp_lock", *limits, "report_lost", "replace", "apply_virtual"]
    return []


def _share_text(share_bp: int) -> str:
    return f"{share_bp // 10000}.{share_bp % 10000:04d}"


def _nav_text(nav_bp: int) -> str:
    return f"{nav_bp // 10000}.{nav_bp % 10000:04d}"


def _arrival_text(mode: str) -> str:
    return {
        "realtime": "实时到账",
        "t_plus_1": "T+1 到账",
        "t_plus_3": "T+1 ~ T+3 到账",
    }.get(mode, "到账时间未知")


def _position_payload(position: Mapping[str, object]) -> dict[str, object]:
    keys = (
        "product_id",
        "name",
        "category",
        "risk_level",
        "amount_yuan",
        "shares",
        "maturity_on",
        "redeemable",
        "redeem_reason",
        "arrival_label",
        "purchased_on",
    )
    return {key: position.get(key) for key in keys}


def _submission_payload(view: SubmissionView) -> dict[str, object]:
    card = _card_payload(view.card, view.plan)
    if card is not None and view.quota is not None:
        card = {
            **card,
            "daily_limit_cents": view.quota.limit_cents,
            "daily_accumulated_cents": view.quota.accumulated_cents,
        }
    return {
        "state": view.state.value,
        "message": view.message,
        "questions": list(view.questions),
        "clarifications": [
            {
                "slot": ask.slot,
                "prompt": ask.prompt,
                "candidates": [
                    {"label": choice.label, "value": choice.value}
                    for choice in ask.candidates
                ],
                "options": [
                    {"label": choice.label, "value": choice.value}
                    for choice in ask.options
                ],
            }
            for ask in view.clarifications
        ],
        "card": card,
        "triage": None if view.triage is None else {
            "kind": view.triage.kind,
            "suggestions": [
                {"label": item.label, "text": item.text} for item in view.triage.suggestions
            ],
            "disclaimer": view.triage.disclaimer,
        },
        "decision": _decision_payload(view.decision),
        "plan_digest": view.card.frozen_hash if view.card is not None else "",
        "questionnaire": list(view.questionnaire),
        "redemption_prefill": view.redemption_prefill,
    }


def _subscription_cancel_payload(
    view: ExecutionView,
    gateway: FactGateway | None = None,
    user_id: str | None = None,
    as_of: date | None = None,
) -> dict[str, object] | None:
    """订阅取消的**操作状态**（与"取消指引/打开第三方页面"明确区分）。

    只有回执明确带 `confirmed_by_merchant` 才可能标记为商户确认；本项目当前没有真实
    商户取消接口，因此本地执行只会落在"本地已取消（商户未确认）"。
    传入事实依赖时补解约对账（解约日、最后一笔、解约后扣款）与**演示**短信回执。
    """
    results = [item for item in view.exec_results if item.action == "subscription.cancel"]
    if not results:
        return None
    last = results[-1]
    state = cancel_state_from_receipt(
        status=last.status, external_status=last.external_status
    )
    payload: dict[str, object] = {
        "state": state,
        "label": CANCEL_STATE_LABELS[state],
        "external_status": last.external_status or EXTERNAL_CANCEL_NOT_INTEGRATED,
        "external_confirmed": state == CANCEL_STATE_EXTERNAL_CONFIRMED,
        "previous_status": last.previous_status,
        "message": last.message,
        "record_ids": list(last.record_ids),
        "effect_ids": list(last.effect_ids),
        "boundary": SUBSCRIPTION_CANCEL_BOUNDARY,
        "cancelled_on": None,
        "last_charge_on": None,
        "last_charge_cents": None,
        "post_cancel_charges": [],
        "sms_receipt": None,
    }
    if gateway is None or user_id is None or as_of is None:
        return payload
    subscription_id = next(
        (
            record_id.split(":", 1)[1]
            for record_id in last.record_ids
            if record_id.startswith("subscriptions:")
        ),
        None,
    )
    if subscription_id is None:
        return payload
    report = subscription_report(gateway, user_id, as_of=as_of)
    item = next(
        (row for row in report.insights if row.subscription_id == subscription_id),
        None,
    )
    if item is None:
        return payload
    try:
        subscription = gateway.subscription(subscription_id).payload
    except GatewayNotFoundError:
        return payload
    cancelled_on = (
        subscription.cancelled_on.isoformat() if subscription.cancelled_on else None
    )
    payload.update({
        "cancelled_on": cancelled_on,
        "last_charge_on": (
            item.last_charged_on.isoformat() if item.last_charged_on else None
        ),
        "last_charge_cents": item.amount_cents if item.last_charged_on else None,
        "post_cancel_charges": [
            {
                "booked_on": charge.booked_on.isoformat(),
                "amount_cents": charge.amount_cents,
                "merchant_label": charge.merchant_label,
            }
            for charge in item.post_cancel_charges
        ],
        "sms_receipt": {
            "mode": "demo",
            "message": (
                f"【演示】您尾号 {item.card_tail or '----'} 的卡已关闭对"
                f"{item.merchant}的扣款协议，解约日期 "
                f"{cancelled_on or as_of.isoformat()}。"
                "关闭后本行不再向该商户放款。"
            ),
        },
    })
    return payload


def _execution_payload(
    view: ExecutionView,
    gateway: FactGateway | None = None,
    user_id: str | None = None,
    as_of: date | None = None,
) -> dict[str, object]:
    card = _card_payload(view.card, view.plan)
    if card is not None and view.quota is not None:
        card = {
            **card,
            "daily_limit_cents": view.quota.limit_cents,
            "daily_accumulated_cents": view.quota.accumulated_cents,
        }
    return {
        "state": view.state.value,
        "message": view.message,
        "frozen_hash": view.frozen_hash,
        "card": card,
        "node_progress": [
            {"node_id": row.node_id, "status": row.status} for row in view.node_progress
        ],
        "decision": _decision_payload(view.decision),
        "record_ids": list(view.record_ids),
        "bill_advice": [
            {
                "kind": item.kind,
                "period": item.period,
                "title": item.title,
                "evidence": item.evidence,
                "suggestion": item.suggestion,
                "source_refs": [
                    {"record_id": record_id, "content_hash": content_hash}
                    for record_id, content_hash in item.source_refs
                ],
            }
            for item in view.bill_advice
        ],
        "bill_advice_message": view.bill_advice_message,
        "bill_findings": view.bill_findings,
        "hook_status": view.hook_status,
        "scheduled_for": view.scheduled_for,
        # 失败终态时可退回的冻结与可重试的到期预约：前端只渲染，不自行推导。
        "released_holds": list(view.released_holds),
        "failed_schedules": list(view.failed_schedules),
        "results": [
            {
                "action": result.action,
                "status": result.status,
                "message": result.message,
                "record_ids": list(result.record_ids),
                "external_status": result.external_status,
                "previous_status": result.previous_status,
            }
            for result in view.exec_results
        ],
        "subscription_cancel": _subscription_cancel_payload(
            view, gateway, user_id, as_of
        ),
    }


def _receipt_payload(view: SessionView | ExecutionView) -> dict[str, object]:
    """执行回执快照：历史对话要能还原「执行了什么」，不能只剩一句文字。"""
    if isinstance(view, ExecutionView):
        results = [
            {
                "action": result.action,
                "status": result.status,
                "message": result.message,
                "record_ids": list(result.record_ids),
            }
            for result in view.exec_results
        ]
        record_ids = list(view.record_ids)
    else:
        results = []
        record_ids = []
    return {
        "state": view.state.value,
        "message": view.message,
        "results": results,
        "record_ids": record_ids,
    }


def _session_payload(view: SessionView) -> dict[str, object]:
    return {"state": view.state.value, "message": view.message}


def _takeover_payload(package: TakeoverPackage) -> dict[str, object]:
    return {
        "reason": package.reason,
        "level": package.level.name,
        "intent_text": package.intent_text,
        "frozen_hash": package.frozen_hash,
        "card": _card_payload(package.card, package.plan),
        "plan": _plan_payload(package.plan),
        "hit_rules": list(package.hit_rules),
        "audit_ids": list(package.audit_ids),
    }


def _plan_payload(plan: Plan | None) -> dict[str, object] | None:
    if plan is None:
        return None
    return {
        "plan_id": plan.plan_id,
        "frozen_hash": cfhash(plan.nodes),
        "nodes": [{"node_id": node.node_id, "action": node.action} for node in plan.nodes],
    }


def _utc_iso(value: datetime) -> str:
    """服务端时间统一带 UTC 时区输出（会话内部存 naive UTC，naive 一律按 UTC 解释）。

    不带时区的字符串会让浏览器按**本地时区**解析，东八区会把计划卡有效期提前 8 小时，
    刚生成的卡直接被判「已过期」——所以对外 JSON 必须带时区。
    """
    moment = value.replace(tzinfo=UTC) if value.tzinfo is None else value
    return moment.astimezone(UTC).isoformat()


def _card_payload(
    card: PlanCard | None, plan: Plan | None = None
) -> dict[str, object] | None:
    if card is None:
        return None
    return {
        "card_id": card.card_id,
        "frozen_hash": card.frozen_hash,
        "level": card.level.name,
        "lines": list(card.lines),
        "summary": [line.to_dict() for line in card.summary],
        "requires": list(card.requires),
        "expires_at": _utc_iso(card.expires_at) if card.expires_at is not None else None,
        "balance_trace": list(card.balance_trace),
        "impact": list(card.impact),
        "nodes": plan_nodes_payload(plan),
    }


def _decision_payload(decision: Decision | None) -> dict[str, object] | None:
    if decision is None:
        return None
    return {"level": decision.level.name, "reasons": list(describe_decision(decision))}


def _channel_payload(outbound: OutboundMessage) -> dict[str, object]:
    """出站消息统一成 HTTP 响应：会话原始 payload + 渠道元信息 + 双端渲染。

    `payload` 就是 Web 端同一份 `_submission_payload / _execution_payload`，
    故 IM 与 Web 看到的计划卡、等级与回执**完全一致**（渠道不二次加工事实）。
    """
    base: dict[str, object] = dict(outbound.payload or {})
    return {
        **base,
        "channel": outbound.channel,
        "chat_id": outbound.chat_id,
        "session_id": outbound.session_id,
        "state": outbound.state,
        "level": outbound.level,
        "kind": outbound.kind,
        "replayed": outbound.replayed,
        "rendered": {
            "text": render_text(outbound),
            "feishu": render_feishu_card(outbound),
        },
    }


def _user_payload(user: AuthUser) -> dict[str, object]:
    """当前登录用户的可展示字段（**不含** token / 口令哈希等凭证）。"""
    return {
        "user_id": user.user_id,
        "username": user.username,
        "display_name": user.display_name,
        "phone_tail": user.phone_tail,
    }


def _empty_progress() -> dict[str, object]:
    """没有进行中的请求时的进度外形：前端按同一结构渲染，不特判缺字段。"""
    return {"stage": "", "stages": [], "finished": True, "updated_at": ""}


_LIFE_STEP_LABELS = {
    "funds.freeze": "锁定预算（资金预留）",
    "funds.release": "释放剩余",
    "merchant.order": "向商户下单",
    "transfer.create": "转账",
    "transfer.book": "登记转账",
    "transfer.schedule": "定期转出",
    "wealth.redeem": "赎回",
    "wealth.list": "理财查询",
    "card.set_limit": "调整限额",
    "card.lock": "锁卡",
    "card.unlock": "解锁卡片",
    "bills.monthly": "月度账单",
    "bills.yearly": "年度账单",
    "subscription.list": "代扣协议查询",
    "subscription.cancel": "关闭扣款协议",
    "account.balance": "余额查询",
    "account.transactions": "交易查询",
    "card.list": "卡片查询",
    "transfer.request_collection": "发起分摊",
}
"""步骤摘要里的动作短名（比 `plan_card` 的对外全名短，适合列表一行）。"""


def _step_payload(
    index: int,
    action: str,
    params: Mapping[str, object],
    *,
    node_id: str = "",
    depends_on: Sequence[str] = (),
) -> dict[str, object]:
    """单个步骤的只读摘要：动作中文名 + 金额 + 日期 + 依赖 + 场景域。"""
    date_value = params.get("order_on") or params.get("booked_on") or params.get("executed_on")
    return {
        "index": index,
        "node_id": node_id,
        "action": action,
        "label": _LIFE_STEP_LABELS.get(action, action),
        "domain": action.split(".", 1)[0] if action else "",
        "depends_on": list(depends_on),
        "amount_cents": params.get("amount_cents"),
        "date": str(date_value) if date_value is not None else None,
        # 内部引用：只用于接口层查名册 / 账户，出卡前会被 `_enrich_life_steps` 换成可读标签
        "payee_id": params.get("payee_id"),
    }


def _life_plan_steps(plan: object) -> tuple[dict[str, object], ...]:
    """计划步骤的只读摘要：动作中文名 + 金额 + 日期 + 依赖，供生活页画任务图。"""

    nodes = getattr(plan, "nodes", ())
    steps: list[dict[str, object]] = []
    for index, node in enumerate(nodes, start=1):
        params: Mapping[str, object] = (
            node.params if isinstance(node.params, Mapping) else {}
        )
        steps.append(
            _step_payload(
                index,
                str(node.action),
                params,
                node_id=str(getattr(node, "node_id", "") or ""),
                depends_on=tuple(str(dep) for dep in getattr(node, "depends_on", ())),
            )
        )
    return tuple(steps)


def _steps_from_nodes(nodes: Sequence[Mapping[str, object]]) -> tuple[dict[str, object], ...]:
    """从预约里冻结的节点快照还原步骤摘要（与 `_life_plan_steps` 同一口径）。

    旧版本记录没有写 `steps_json`，但它关联的预约仍保存着确认时的节点快照：
    只做只读回填，不据此重新执行任何动作。
    """

    steps: list[dict[str, object]] = []
    for index, node in enumerate(nodes, start=1):
        action = node.get("action")
        deps = node.get("depends_on")
        raw_params = node.get("params")
        params: Mapping[str, object] = raw_params if isinstance(raw_params, Mapping) else {}
        steps.append(
            _step_payload(
                index,
                action if isinstance(action, str) else "",
                params,
                node_id=str(node.get("node_id") or ""),
                depends_on=tuple(str(dep) for dep in deps) if isinstance(deps, list) else (),
            )
        )
    return tuple(steps)


_LIFE_EVENT_KEYWORDS = ("生日", "纪念日", "寿宴", "过节", "搬家", "开学", "婚礼")
"""同一场合的关键词：只用来发现「同一件事出现两个用钱日」，不做事件合并。"""


def _life_date_conflicts(records: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    """同名场合出现多个用钱日 → 提示用户确认，不替用户改期。

    典型：一条记录说生日 11-04 下单，另一条把理财到期日 11-30 当作下单日。
    这里只做**确定性归组 + 日期比对**，不猜语义、不合并记录。
    """

    groups: dict[str, list[tuple[str, str]]] = {}
    for record in records:
        text = f"{record.get('event_title', '')} {record.get('utterance', '')}"
        schedule = record.get("schedule")
        due_on = ""
        if isinstance(schedule, Mapping):
            due_on = str(schedule.get("due_on") or "")[:10]
        if not due_on:
            due_on = str(record.get("due_on") or "")[:10]
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", due_on):
            continue
        for keyword in _LIFE_EVENT_KEYWORDS:
            if keyword in text:
                groups.setdefault(keyword, []).append((due_on, str(
                    record.get("event_title") or record.get("event_id") or ""
                )))
    conflicts: list[dict[str, object]] = []
    for keyword, items in groups.items():
        dates = sorted({day for day, _title in items})
        if len(dates) < 2:
            continue
        conflicts.append({
            "keyword": keyword,
            "dates": dates,
            "titles": sorted({title for _day, title in items if title}),
        })
    return conflicts


def _enrich_life_steps(
    gateway: FactGateway, steps: Sequence[object]
) -> list[dict[str, object]]:
    """把步骤里的内部引用换成可读标签；换不掉的内部键直接丢弃。"""

    enriched: list[dict[str, object]] = []
    for step in steps:
        if not isinstance(step, Mapping):
            continue
        item = dict(step)
        payee_id = item.pop("payee_id", None)
        if isinstance(payee_id, str) and payee_id:
            try:
                payee = gateway.payee(payee_id).payload
            except Exception:
                payee = None
            if payee is not None:
                item["payee_label"] = f"{payee.full_name}（尾号 {payee.account_tail}）"
        enriched.append(item)
    return enriched


def _life_plan_amount(plan: object) -> int | None:
    """事件金额：优先资金预留额，其次赎回额，最后下单额；都没有返回 `None`。"""

    by_action: dict[str, int] = {}
    for node in getattr(plan, "nodes", ()):
        params = node.params if isinstance(node.params, dict) else {}
        cents = params.get("amount_cents")
        if isinstance(cents, int) and not isinstance(cents, bool) and cents > 0:
            by_action.setdefault(str(node.action), cents)
    for action in ("funds.freeze", "wealth.redeem", "merchant.order"):
        if action in by_action:
            return by_action[action]
    return None


def _life_plan_refs(plan: object) -> tuple[str | None, str | None]:
    """计划里的「本事件自己的」冻结记录与下单幂等键；没有就 `None`（不猜）。"""

    hold_ref: str | None = None
    occasion_ref: str | None = None
    for node in getattr(plan, "nodes", ()):
        params = node.params if isinstance(node.params, dict) else {}
        if hold_ref is None:
            value = params.get("budget_hold_id") or params.get("hold_id")
            if isinstance(value, str) and value:
                hold_ref = value
        if occasion_ref is None:
            value = params.get("occasion_id")
            if isinstance(value, str) and value:
                occasion_ref = value
    return hold_ref, occasion_ref


def _life_plan_trigger_day(plan: object) -> date | None:
    """计划里的触发日（下单日 / 记账日）；没有则 `None`。"""

    for node in getattr(plan, "nodes", ()):
        params = node.params if isinstance(node.params, dict) else {}
        value = params.get("order_on") or params.get("booked_on")
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        if isinstance(value, str):
            try:
                return date.fromisoformat(value[:10])
            except ValueError:
                continue
    return None


def _life_plan_due_on(plan: object) -> str | None:
    day = _life_plan_trigger_day(plan)
    return None if day is None else day.isoformat()


_MATURITY_EVENT_ID = "maturity"
"""理财到期联动的事件 id：**不进** `LIFE_EVENT_PROTOTYPES`（对外口径仍是七条原型 L1–L7）。"""

_MATURITY_EVENT_NAME = "理财到期处理"
"""理财到期联动在记录页的显示名。"""


def _life_event_name(event_id: str) -> str:
    """事件 id → 可读名称；组合事件也查一遍，查不到返回空串（不臆造名字）。"""
    if event_id == _MATURITY_EVENT_ID:
        return _MATURITY_EVENT_NAME
    for prototype in LIFE_EVENT_PROTOTYPES:
        if prototype.event_id == event_id:
            return prototype.name
    for combo in LIFE_EVENT_COMBOS:
        if combo.combo_id == event_id:
            return combo.name
    return ""


_AMOUNT_IN_TEXT = re.compile(r"(\d+(?:\.\d+)?)\s*(?:元|块钱|块)")
_AMOUNT_STRIP = re.compile(r"(?:[¥￥]\s*\d+(?:\.\d+)?|\d+(?:\.\d+)?\s*(?:元|块钱|块))")
_EDGE_PUNCT = re.compile(r"^[\s，,、。；;：:！!？?]+|[\s，,、。；;：:！!？?]+$")
_PUNCT_SPACE = re.compile(r"\s*([，,、。；;：:！!？?])\s*|\s{2,}")


def _utterance_amount_cents(text: str) -> int | None:
    """从原话里取第一个「N 元」当作金额（分）；没写就返回 `None`（不估算）。"""
    match = _AMOUNT_IN_TEXT.search(text or "")
    if match is None:
        return None
    try:
        return int((Decimal(match.group(1)) * 100).to_integral_value())
    except InvalidOperation:
        return None


def _life_event_title(text: str, *, fallback: str = "") -> str:
    """把用户原话整理成「具体事件」标题。

    先在原型的「常见事件」里找完全匹配的话术，命中就用事件名（如「出差」）；
    否则只做删减：去掉金额与货币符号、压掉多余空白、去掉首尾标点。
    删完为空则回落到类型名——宁可回退，也不拼一句假话。
    """

    raw = (text or "").strip()
    for prototype in LIFE_EVENT_PROTOTYPES:
        for label, utterance in prototype.cases:
            if utterance and raw == utterance:
                return label
    cleaned = _AMOUNT_STRIP.sub("", raw)
    cleaned = _PUNCT_SPACE.sub(lambda m: m.group(1) or " ", cleaned).strip()
    cleaned = _EDGE_PUNCT.sub("", cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
    if len(cleaned) > 40:
        cleaned = cleaned[:40]
    return cleaned or fallback


def _life_budget_progress(
    gateway: FactGateway, user_id: str, hold_ref: str | None, occasion_ref: str | None = None
) -> dict[str, object] | None:
    """**本条事件自己的**预留进度；没有 `hold_ref` 或找不到记录返回 `None`。

    以前这里按用户取「最新一条 `life:L1:`」，导致四张不同金额的卡片显示同一个
    「冻结余额 ¥701.00」。现在只认记录自己的 `hold_ref`：
    - 预留：该 hold 存在（`amount_cents` 是**剩余**预留额，不是事件总额）；
    - 下单：`occasion_ref` 命中下单回执；
    - 释放：该 hold `status == 'released'`。
    """

    if not hold_ref:
        return None
    holds = [
        record.payload
        for account in gateway.accounts_of(user_id)
        for record in gateway.holds_of(account.payload.account_id)
        if record.payload.hold_id == hold_ref
    ]
    if not holds:
        return None
    hold = holds[0]
    return {
        "hold_id": hold.hold_id,
        "amount_cents": hold.amount.cents,
        "state": hold.status,
        "order_on": hold_ref.rsplit(":", 1)[-1] if ":" in hold_ref else None,
        "steps": {
            "reserve": True,
            "order": bool(occasion_ref)
            and gateway.merchant_occasion_completed(user_id, str(occasion_ref)),
            "release": hold.status == "released",
        },
    }


def _life_orders(gateway: FactGateway, record: LifeEventRecord) -> list[dict[str, object]]:
    """执行回执里的订单实体（商户 / 订单号 / 金额 / 下单日）；没有就不编。"""

    orders: list[dict[str, object]] = []
    for item in record.results:
        if item.get("action") != "merchant.order":
            continue
        record_ids = item.get("record_ids")
        if not isinstance(record_ids, list):
            continue
        for raw in record_ids:
            if not isinstance(raw, str) or not raw.startswith("merchant_orders:"):
                continue
            order_id = raw.split(":", 1)[1]
            try:
                order = gateway.merchant_order(order_id).payload
            except Exception:
                continue
            orders.append({
                "order_id": order.order_id,
                "merchant": order.merchant,
                "amount_cents": order.amount_cents,
                "order_on": order.order_on,
                "status": order.status,
            })
    return orders


def _life_funding_for(
    gateway: FactGateway,
    user_id: str,
    *,
    amount_cents: int | None,
    trigger_on: date | None,
    today: date,
    record: LifeEventRecord,
) -> dict[str, object] | None:
    """资金核查：已执行、或资金来自自身赎回的事件不查（钱已经花了 / 不必查）。"""

    if record.status == STATUS_EXECUTED:
        return None
    if any(
        isinstance(step, Mapping) and step.get("action") == "wealth.redeem"
        for step in record.steps
    ):
        return None
    result = life_funding.check(
        gateway,
        user_id,
        amount_cents=amount_cents,
        trigger_on=trigger_on,
        today=today,
    )
    return None if result.status == life_funding.STATUS_UNKNOWN else result.to_dict()


def _life_maturities(
    gateway: FactGateway, user_id: str, today: date
) -> list[dict[str, object]]:
    """本人理财持仓的到期日（按产品条款：起息日 + 期限），按到期日升序。

    起息日与期限都来自持仓 / 产品系统的官方字段，到期日由条款确定性得出，**不是从流水反推**。
    无期限产品（`term_days <= 0`，如活期）不列出——没有到期日就不编一个。
    兑付到账仍以银行回单为准（演示环境不接真实清算），`basis` 随行返回供页面原样展示。
    """
    products = {
        record.payload.product_id: record.payload for record in gateway.wealth_products()
    }
    rows: list[dict[str, object]] = []
    for record in gateway.wealth_holdings_of(user_id):
        holding = record.payload
        product = products.get(holding.product_id)
        if product is None:
            continue
        maturity = holding_maturity(holding.purchased_on, product.term_days)
        if maturity is None:
            continue
        rows.append({
            "product_id": product.product_id,
            "name": product.name,
            "amount_cents": holding.amount.cents,
            "purchased_on": holding.purchased_on.isoformat(),
            "term_days": product.term_days,
            "maturity_on": maturity.isoformat(),
            "status": "expired" if maturity < today else "upcoming",
            "days_left": (maturity - today).days,
            "basis": "按产品条款确定（起息日 + 期限）；兑付到账以银行回单为准",
        })
    rows.sort(key=lambda row: (str(row["maturity_on"]), str(row["product_id"])))
    return rows


def _selected_amount_cents(selected: dict[str, object] | None) -> int | None:
    """从表单参数取金额（分）；只认明确给出的值，缺失即 `None`（不估算）。"""
    if not selected:
        return None
    cents = selected.get("amount_cents")
    if isinstance(cents, int) and not isinstance(cents, bool):
        return cents
    for key in ("amount", "金额"):
        raw = selected.get(key)
        if raw is None:
            continue
        if isinstance(raw, bool):
            return None
        if isinstance(raw, int):
            return raw * 100
        if not isinstance(raw, str):
            return None
        cleaned = (
            raw.strip().replace("¥", "").replace("￥", "").replace("元", "").replace(",", "")
        )
        if not cleaned:
            return None
        try:
            return int((Decimal(cleaned) * 100).to_integral_value())
        except InvalidOperation:
            return None
    return None


_IM_PAGE_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AI 银行副驾 · IM 渠道本地联调页</title>
<style>
  :root { color-scheme: light; }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: #f5f6f8; color: #1b1f24;
    font: 14px/1.6 -apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
  }
  header {
    display: flex; flex-wrap: wrap; gap: 8px 16px; align-items: baseline;
    padding: 14px 20px; background: #fff; border-bottom: 1px solid #e3e6ea;
  }
  header h1 { margin: 0; font-size: 16px; }
  .tag { font-size: 12px; color: #6b7280; }
  main { max-width: 720px; margin: 0 auto; padding: 16px 20px 32px; }
  .health { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 16px; }
  .health span {
    display: inline-flex; align-items: center; gap: 6px;
    padding: 3px 10px; border: 1px solid #e3e6ea; border-radius: 999px;
    background: #fff; font-size: 12px; color: #374151;
  }
  .dot { width: 7px; height: 7px; border-radius: 50%; background: #9ca3af; }
  .dot.on { background: #16794a; }
  #bubbles { display: flex; flex-direction: column; gap: 10px; min-height: 120px; }
  .bubble { max-width: 88%; padding: 8px 12px; border-radius: 10px; white-space: pre-wrap; }
  .bubble--user { align-self: flex-end; background: #1f4e79; color: #fff; }
  .bubble--assistant { align-self: flex-start; background: #fff; border: 1px solid #e3e6ea; }
  .bubble--system { align-self: center; background: #fff7e6; color: #8a5a00; font-size: 12px; }
  .plan-card { align-self: flex-start; width: 100%; background: #fff; border: 1px solid #cfd6de;
    border-radius: 10px; padding: 12px 14px; }
  .plan-card h2 { margin: 0 0 8px; font-size: 13px; color: #1f4e79; font-weight: 600; }
  .plan-card dl { display: grid; grid-template-columns: max-content 1fr; gap: 4px 12px; margin: 0; }
  .plan-card dt { color: #6b7280; font-size: 12px; }
  .plan-card dd { margin: 0; font-variant-numeric: tabular-nums; }
  .plan-card ul { margin: 8px 0 0; padding-left: 18px; font-size: 12px; color: #4b5563; }
  .plan-card button { margin-top: 10px; }
  .verification-code { display: block; width: 100%; margin-top: 10px; }
  form { display: flex; gap: 8px; margin-top: 16px; }
  input[type=text] {
    flex: 1; padding: 8px 10px; border: 1px solid #cfd6de; border-radius: 8px;
    font: inherit; background: #fff;
  }
  button {
    padding: 8px 14px; border: 1px solid #1f4e79; border-radius: 8px;
    background: #1f4e79; color: #fff; font: inherit; cursor: pointer;
  }
  button:disabled { opacity: .5; cursor: not-allowed; }
  .note { margin-top: 12px; font-size: 12px; color: #6b7280; }
</style>
</head>
<body>
<header>
  <h1>IM 渠道 · 本地联调页</h1>
  <span class="tag">本地联调页 · 不参与产品验收</span>
</header>
<main>
  <div class="health" id="health"><span>读取渠道健康…</span></div>
  <div id="bubbles"></div>
  <form id="composer">
    <input type="text" id="text" placeholder="例如：这个月订阅花了多少" autocomplete="off">
    <button type="submit" id="send-button">发送</button>
  </form>
  <p class="note">本页与主对话页共用同一条会话状态机与冻结校验；仅用于验证本地 IM 渠道。</p>
</main>
<script>
(function () {
  "use strict";
  var CHAT_ID = "local-debug";
  var bubbles = document.getElementById("bubbles");
  var composer = document.getElementById("composer");
  var input = document.getElementById("text");
  var sendButton = document.getElementById("send-button");
  var health = document.getElementById("health");
  var composerLockTimer = null;
  var composerLockToken = 0;
  var healthHasData = false;

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) { node.className = cls; }
    if (text) { node.textContent = text; }
    return node;
  }

  function push(role, text) {
    bubbles.appendChild(el("div", "bubble bubble--" + role, text));
    bubbles.scrollTop = bubbles.scrollHeight;
  }

  function money(cents) {
    if (typeof cents !== "number") { return ""; }
    return "¥" + (cents / 100).toFixed(2);
  }

  function renderCard(card) {
    if (!card) { return; }
    var box = el("section", "plan-card");
    box.appendChild(el("h2", null, "计划卡 · " + (card.level || "")));
    var summary = card.summary || [];
    var lines = card.lines || [];
    if (summary.length) {
      var dl = document.createElement("dl");
      summary.forEach(function (row) {
        dl.appendChild(el("dt", null, row.label));
        dl.appendChild(el("dd", null, row.value));
      });
      box.appendChild(dl);
    } else if (lines.length) {
      var ul = document.createElement("ul");
      lines.forEach(function (line) { ul.appendChild(el("li", null, String(line))); });
      box.appendChild(ul);
    }
    if (card.daily_limit_cents) {
      var quota = el("p", "note", "今日额度 " + money(card.daily_limit_cents)
        + " · 已用 " + money(card.daily_accumulated_cents || 0));
      box.appendChild(quota);
    }
    var requiresStrongVerification = Array.isArray(card.requires)
      && card.requires.indexOf("strong_verify") !== -1;
    var verificationInput = null;
    if (requiresStrongVerification) {
      verificationInput = el("input", "verification-code");
      verificationInput.type = "text";
      verificationInput.inputMode = "numeric";
      verificationInput.autocomplete = "one-time-code";
      verificationInput.placeholder = "输入验证渠道收到的验证码";
      box.appendChild(verificationInput);
      var requestButton = el("button", null, "申请强验证");
      requestButton.type = "button";
      requestButton.addEventListener("click", function () {
        requestButton.disabled = true;
        post("/api/im/verify/request", { chat_id: CHAT_ID })
          .then(function (challenge) {
            if (typeof challenge.code === "string" && challenge.code.trim()) {
              verificationInput.value = challenge.code.trim();
              push("system", "演示验证码已填入，请确认执行。");
            } else {
              push("system", "强验证已发起，请填写验证渠道实际收到的验证码。");
              verificationInput.focus();
            }
          })
          .catch(function (error) { showRequestError(error, "申请强验证失败"); })
          .finally(function () { requestButton.disabled = false; });
      });
      box.appendChild(requestButton);
    }
    var btn = el("button", null, "确认执行");
    btn.type = "button";
    btn.addEventListener("click", function () {
      var verificationCode = verificationInput ? verificationInput.value.trim() : "";
      if (requiresStrongVerification && !verificationCode) {
        push("system", "请先申请强验证，并填写实际收到的验证码。");
        verificationInput.focus();
        return;
      }
      btn.disabled = true;
      var body = {
        card_id: card.card_id,
        frozen_hash: card.frozen_hash,
        chat_id: CHAT_ID
      };
      if (verificationCode) { body.verification_code = verificationCode; }
      post("/api/im/confirm", body)
        .then(render)
        .catch(function (error) {
          showRequestError(error, "确认失败");
          btn.disabled = false;
        });
    });
    box.appendChild(btn);
    bubbles.appendChild(box);
    bubbles.scrollTop = bubbles.scrollHeight;
  }

  function render(payload) {
    var text = (payload.rendered && payload.rendered.text) || payload.message || "";
    if (text) { push("assistant", text); }
    renderCard(payload.card);
    if (payload.state && payload.state !== "awaiting_confirm") {
      push("system", "状态：" + payload.state);
    }
  }

  function post(url, body) {
    return fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    }).then(function (resp) {
      return resp.json().catch(function () { return {}; }).then(function (data) {
        if (!resp.ok) {
          var detail = data.detail || data.message || ("HTTP " + resp.status);
          var error = new Error(detail);
          error.status = resp.status;
          error.retryAfter = resp.headers.get("Retry-After") || "";
          error.detail = detail;
          throw error;
        }
        return data;
      });
    });
  }

  function lockComposer() {
    composerLockToken += 1;
    var lockToken = composerLockToken;
    input.disabled = true;
    sendButton.disabled = true;
    if (composerLockTimer !== null) { window.clearTimeout(composerLockTimer); }
    composerLockTimer = window.setTimeout(function () {
      if (lockToken !== composerLockToken) { return; }
      input.disabled = false;
      sendButton.disabled = false;
      composerLockTimer = null;
      input.focus();
    }, 3000);
  }

  function showRequestError(error, prefix) {
    if (error && error.status === 429) {
      lockComposer();
      push("system", "发太快了，" + (error.retryAfter || "60") + " 秒后再试");
      return;
    }
    var detail = error && (error.detail || error.message);
    push("system", prefix + "：" + (detail || "渠道暂不可用"));
  }

  function loadHealth() {
    fetch("/api/channels/health", { credentials: "same-origin" })
      .then(function (resp) {
        if (!resp.ok) { throw new Error("HTTP " + resp.status); }
        return resp.json();
      })
      .then(function (data) {
        var channels = data.channels || {};
        var runtime = data.health || {};
        var names = ["web", "im", "feishu"];
        var fragment = document.createDocumentFragment();
        names.forEach(function (name) {
          var config = channels[name] || {};
          var item = runtime[name] || {};
          var chip = el("span", null, null);
          var configured = config.configured === true;
          chip.appendChild(el("i", "dot" + (configured ? " on" : "")));
          var rateLimits = item.ratelimit_hits || 0;
          var label = name + " · " + (configured ? "已接入" : "未接入")
            + " · 成功 " + (item.ok || 0)
            + " · 失败 " + (item.failed || 0)
            + " · 限流 " + rateLimits;
          if (rateLimits > 0) { label += " · 发太快了，请稍后再试"; }
          chip.appendChild(document.createTextNode(label));
          fragment.appendChild(chip);
        });
        health.replaceChildren(fragment);
        healthHasData = true;
      })
      .catch(function () {
        var previousFailure = health.querySelector(".health-error");
        if (previousFailure) { previousFailure.remove(); }
        var failure = el(
          "span",
          "health-error",
          healthHasData
            ? "渠道健康暂不可用，正在显示上次结果"
            : "渠道健康暂不可用，稍后自动重试"
        );
        if (healthHasData) {
          health.appendChild(failure);
        } else {
          health.replaceChildren(failure);
        }
      });
  }

  composer.addEventListener("submit", function (event) {
    event.preventDefault();
    var text = input.value.trim();
    if (!text) { return; }
    input.value = "";
    push("user", text);
    post("/api/im/message", { text: text, chat_id: CHAT_ID })
      .then(render)
      .catch(function (error) {
        showRequestError(error, "发送失败");
      });
  });

  loadHealth();
  window.setInterval(loadHealth, 5000);
})();
</script>
</body>
</html>
"""


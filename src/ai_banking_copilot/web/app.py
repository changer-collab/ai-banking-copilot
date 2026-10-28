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
import logging
import secrets
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
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
from ai_banking_copilot.gateway.facts import FactGateway
from ai_banking_copilot.gateway.receipt_store import ReceiptReader
from ai_banking_copilot.gateway.risk_store import (
    SOURCE_QUESTIONNAIRE,
    RiskAssessmentSource,
    RiskAssessmentWriter,
)
from ai_banking_copilot.gateway.seed import DEMO_USER_ID
from ai_banking_copilot.gateway.store import Store
from ai_banking_copilot.planning.llm import IntentModel
from ai_banking_copilot.planning.reviewer import Reviewer
from ai_banking_copilot.planning.task_observations import TaskObservations
from ai_banking_copilot.policy.audit import (
    KIND_OPERATOR,
    AuditEvent,
    AuditLog,
    ChainBrokenError,
)
from ai_banking_copilot.policy.breaker import CircuitBreaker
from ai_banking_copilot.policy.gate import StalePlanError
from ai_banking_copilot.policy.pep import Pep
from ai_banking_copilot.policy.reversal import ReversalService
from ai_banking_copilot.policy.verification import (
    VerificationAttemptLimiter,
    VerificationProvider,
)
from ai_banking_copilot.scenarios.bill_findings import (
    BillNarrator,
    analyze_bill,
    narrate_bill_findings,
    report_payload,
)
from ai_banking_copilot.scenarios.life_events import (
    LIFE_EVENT_COMBOS,
    LIFE_EVENT_PROTOTYPES,
    detect_life_combo,
    detect_life_event,
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
    product_catalog,
    to_risk_assessment,
)
from ai_banking_copilot.scenarios.wealth_assessment import (
    AssessmentError,
    evaluate_answers,
    questionnaire_payload,
)
from ai_banking_copilot.tools.registry import ToolRegistry
from ai_banking_copilot.web.auth import (
    SESSION_IDLE_SECONDS,
    TICKET_PURPOSE_DELETE_CONVERSATION,
    TICKET_TTL_SECONDS,
    AuthError,
    AuthRequiredError,
    AuthService,
    AuthSession,
    AuthUser,
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
from ai_banking_copilot.web.channels.dispatcher import Dispatcher
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
from ai_banking_copilot.web.channels.ratelimit import EventLedger, HealthRegistry
from ai_banking_copilot.web.channels.render import render_feishu_card, render_text
from ai_banking_copilot.web.conversation_store import ConversationStore
from ai_banking_copilot.web.insights import InsightService
from ai_banking_copilot.web.life_event_store import (
    STATUS_EXECUTED,
    STATUS_FAILED,
    STATUS_REJECTED,
    LifeEventStore,
)
from ai_banking_copilot.web.page import APP_HTML, INDEX_HTML, LOGIN_HTML
from ai_banking_copilot.web.plan_card import PlanCard, describe_decision
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

REVERIFY_HEADER = "X-Reverify-Ticket"
"""删除类动作的二次验证票据请求头（票据一次性、5 分钟有效）。"""

LOGGER = logging.getLogger(__name__)

_CSRF_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
"""需要同源校验的写方法（含 `DELETE`：删除对话历史）。"""

_JSON_METHODS = frozenset({"POST", "PUT", "PATCH"})
"""必须是 `application/json` 的写方法；`DELETE` 无请求体，只做来源校验。"""

_ERROR_STATES = frozenset({"failed", "blocked", "taken_over"})
"""失败 / 阻断终态：对话历史里记为 `error`，不当成正常助手回答。"""


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
    progress: ProgressRegistry = field(default_factory=ProgressRegistry)
    """阶段进度表（进程内、按 `(user, session, request)`）：`generate` 前端的轮询数据源。"""
    auth_mode: str = AUTH_MODE_OFF
    """`off`（默认，单用户演示）/ `demo`（登录 + Cookie 会话 + CSRF）。"""


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


class _UnlockRequest(BaseModel):
    """运维解锁请求体（裁决 #27）：**必须给理由**，凭证走请求头（不落请求体、不进日志）。"""

    reason: str


class _BudgetRequest(BaseModel):
    """只读洞察的提醒预算；单位为整数分，不进入银行授权。"""

    budget_cents: int | None = None
    month: str | None = None


class _RiskAssessmentRequest(BaseModel):
    """用户侧测评录入：**身份只取服务端 `deps.user_id`**，不接受请求体用户 id。

    两条互斥路径：
    - `answers`：固定题库作答，等级**由服务端评分算出**（`source='questionnaire'`）；
    - `risk_profile`：旧的直接录入（`source='user_input'`，保留兼容）。
    两者同时提交一律 400——等级不允许既由用户自填又由问卷算出。
    """

    risk_profile: str | None = None
    answers: dict[str, str] | None = None


class _ImMessageRequest(BaseModel):
    """IM 入站文本：`user_id / chat_id / event_id` 全部可选，但**外部 id 必须先绑定**。

    不带 `user_id` 时走服务端身份（仅本地 IM 渠道）；`event_id` 相同即视为平台重投，
    直接回放首次结果，不重复执行。
    """

    text: str
    chat_id: str | None = None
    user_id: str | None = None
    event_id: str | None = None


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
        try:
            yield
        finally:
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    app = FastAPI(title="AI 银行副驾 · Web 渠道", version="0.1.0", lifespan=lifespan)
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
    feishu = FeishuAdapter(FeishuConfig.from_env())
    health = HealthRegistry()
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
            built = Dispatcher(
                manager=manager,
                identities=identities,
                submission_payload=_submission_payload,
                execution_payload=_execution_payload,
                audit=deps.audit,
                server_user_id=user_id,
                feishu_configured=feishu.configured,
            )
            _dispatchers[user_id] = built
            return built

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
        if isinstance(error, ChannelRateLimited):
            channel = (
                CHANNEL_FEISHU
                if request.url.path == "/api/im/feishu"
                else CHANNEL_LOCAL
                if request.url.path.startswith("/api/im/")
                else "web"
            )
            health.record_rate_limit(channel)
        return JSONResponse(
            status_code=error.status_code,
            content={"detail": error.code, "message": str(error)},
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

    def current_user(request: Request) -> str:
        """当前登录用户；`off` 模式沿用单用户演示（`deps.user_id`），行为与旧版一致。"""
        if not auth_enabled:
            return deps.user_id
        _check_csrf(request)
        session = _auth().resolve(request.cookies.get(AUTH_COOKIE_NAME, ""))
        if session is None:
            raise AuthRequiredError("登录已失效，请重新登录")
        request.state.auth_session = session
        return session.user_id

    def _page_or_login(request: Request, html: str) -> HTMLResponse:
        """页面入口：`demo` 模式未登录时先给登录页，不把应用骨架发出去。"""
        if auth_enabled and _auth().resolve(request.cookies.get(AUTH_COOKIE_NAME, "")) is None:
            return HTMLResponse(LOGIN_HTML)
        return HTMLResponse(html)

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
        card = _card_payload(view.card)
        if card is not None:
            _record_message(
                user_id, session_id, "assistant", "plan_card", view.message, {"card": card},
            )
        elif view.state.value in _ERROR_STATES:
            _record_message(user_id, session_id, "assistant", "error", view.message)
        else:
            _record_message(user_id, session_id, "assistant", "text", view.message)

    def _record_life_event(
        user_id: str,
        session_id: str,
        utterance: str,
        selected: dict[str, object] | None,
        view: SubmissionView,
    ) -> None:
        """识别到生活事件且已出计划卡时登记一条 `planned`；金额只取表单值，不估算。"""
        store = deps.life_events
        if store is None or view.card is None:
            return
        event_id = detect_life_combo(utterance) or detect_life_event(utterance)
        if event_id is None:
            return
        try:
            store.create(
                user_id=user_id,
                session_id=session_id,
                event_id=event_id,
                event_name=_life_event_name(event_id),
                amount_cents=_selected_amount_cents(selected),
            )
        except Exception:
            LOGGER.warning("生活事件写入失败：%s / %s", user_id, session_id, exc_info=True)

    def _mark_life_event(user_id: str, session_id: str, status: str) -> None:
        store = deps.life_events
        if store is None:
            return
        try:
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
        """只恢复与读取状态，不触发计划执行或返回挑战秘密。"""
        view = _session_for(session_id, user_id).current_view()
        return _response(session_id, user_id, _execution_payload(view))

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> HTMLResponse:
        return _page_or_login(request, INDEX_HTML)

    @app.get("/m", response_class=HTMLResponse)
    def index_app(request: Request) -> HTMLResponse:
        """APP 端版式：同一份骨架 + `data-view="app"`，手机框内铺满。"""
        return _page_or_login(request, APP_HTML)

    @app.get("/api/options")
    def options(user_id: str = Depends(current_user)) -> dict[str, object]:
        """对象选择只展示当前用户的事实；金额不由前端编造。"""
        gateway = deps.gateway
        account_kinds = {"savings": "储蓄", "current": "活期"}
        card_types = {"debit": "借记", "credit": "信用", "virtual": "虚拟"}
        statuses = {"active": "正常", "locked": "已锁定", "lost": "已挂失", "cancelled": "已取消"}
        return {
            "today": deps.today.isoformat(),
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
                "boundary": SUBSCRIPTION_CANCEL_BOUNDARY,
                "states": [
                    {"state": state, "label": label}
                    for state, label in CANCEL_STATE_LABELS.items()
                ],
            },
        }

    @app.get("/api/risk-assessment")
    def risk_assessment_state(user_id: str = Depends(current_user)) -> dict[str, object]:
        """当前已认证用户的测评状态（只读）；未测评 / 过期如实返回，绝不默认等级。"""
        return _risk_payload(deps, user_id)

    @app.post("/api/risk-assessment")
    def save_risk_assessment(
        request: _RiskAssessmentRequest, user_id: str = Depends(current_user)
    ) -> dict[str, object]:
        """用户侧测评录入（真实测评入口）。

        - **问卷路径**（`answers`）：固定 5 题，等级由 `scenarios.wealth_assessment` 算出，
          请求体不得同时给 `risk_profile`（模型不得自填等级），落库 `source='questionnaire'`；
        - **直接录入路径**（`risk_profile`）：保留兼容，落库 `source='user_input'`；
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
            try:
                result = evaluate_answers(request.answers)
            except AssessmentError as error:
                raise HTTPException(status_code=400, detail=str(error)) from error
            writer.save(
                user_id, result.risk_profile, deps.today, source=SOURCE_QUESTIONNAIRE,
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
        - 持仓金额 / 起息日来自 gateway 事实；当前收益无净值数据 ⇒ 如实标未知。
        """
        assessment = _risk_payload(deps, user_id)
        raw_profile = assessment["risk_profile"]
        risk_profile = (
            raw_profile
            if isinstance(raw_profile, str)
            and assessment["state"] == AssessmentState.VALID.value
            else None
        )
        return {
            "assessment": assessment,
            "products": list(product_catalog(deps.gateway.wealth_products(), risk_profile)),
            "holdings": list(holdings_payload(deps.gateway, deps.user_id)),
            "data_source": PRODUCT_DATA_SOURCE,
        }

    @app.get("/api/bills/analysis")
    def bill_analysis(
        year: int | None = None,
        month: int | None = None,
        user_id: str = Depends(current_user),
    ) -> dict[str, object]:
        """只读账单异常识别 + 结论式解释；不写事实、不产生执行授权。

        缺省账期为服务端时钟的上一个月；金额全部来自确定性识别引擎并按分返回，
        模型只补充文字解释（失败时 `narrative.degraded=true`，不得当作成功分析）。
        """
        target = deps.today.replace(day=1) - timedelta(days=1)
        target_year = target.year if year is None else year
        target_month = target.month if month is None else month
        if not 2000 <= target_year <= 2100 or not 1 <= target_month <= 12:
            raise HTTPException(status_code=400, detail="账期非法，需为 2000-2100 年与 1-12 月")
        report = analyze_bill(
            deps.gateway, user_id, target_year, target_month, as_of=deps.today,
        )
        narrative = narrate_bill_findings(report, deps.bill_narrator)
        return {
            **report_payload(report),
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

    @app.get("/api/insights")
    def insights(
        include_resolved: bool = False, user_id: str = Depends(current_user)
    ) -> dict[str, object]:
        """读取当前用户的只读洞察收件箱；不会继承任何执行授权。

        启动时只扫默认用户；其他用户首次访问时**懒扫描一次**，否则永远空列表，
        用户会误以为「没有异常」而不是「还没扫过」。
        """
        service = deps.insights
        if service is None:
            return {"insights": [], "scanned_at": None, "scan_error": None}
        if service.scan_state(user_id) is None:
            service.scan(user_id, deps.today)
        state = service.scan_state(user_id)
        return {
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
        payee_names = {
            record.payload.payee_id: record.payload.full_name
            for record in gateway.payees_of(user_id)
        }
        accounts = gateway.accounts_of(user_id)
        kinds = {record.payload.account_id: record.payload.kind for record in accounts}
        labels = {"savings": "储蓄账户", "current": "活期账户"}
        start = deps.today - timedelta(days=days)
        horizon = deps.today + timedelta(days=days)
        items: list[dict[str, object]] = []
        for account in accounts:
            for txn_record in gateway.transactions_between(
                account.payload.account_id, start, horizon
            ):
                txn = txn_record.payload
                if txn.category != "转账" or txn.direction != "out":
                    continue
                payee_id = txn.merchant.removeprefix("转给")
                items.append(
                    {
                        "kind": "transaction",
                        "record_id": txn.txn_id,
                        "payee": _mask_payee_name(payee_names.get(payee_id, "")),
                        "amount_cents": -txn.amount.cents,
                        "booked_on": txn.booked_on.isoformat(),
                        "status": "已到账" if txn.booked_on <= deps.today else "待到账",
                        "account": labels.get(kinds.get(account.payload.account_id, ""), "账户"),
                        "remark": "",
                    }
                )
        for booking_record in gateway.scheduled_transfers_of(user_id, status="scheduled"):
            booking = booking_record.payload
            items.append(
                {
                    "kind": "scheduled",
                    "record_id": booking.schedule_id,
                    "payee": _mask_payee_name(payee_names.get(booking.payee_id, "")),
                    "amount_cents": booking.amount.cents,
                    "booked_on": booking.booked_on.isoformat(),
                    "status": "待到账",
                    "account": labels.get(kinds.get(booking.account_id, ""), "账户"),
                    "remark": booking.remark,
                }
            )
        items.sort(key=lambda item: str(item["booked_on"]), reverse=True)
        items.sort(key=lambda item: item["status"] == "已到账")
        return {"items": items, "as_of": deps.today.isoformat(), "window_days": days}

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
            service.set_budget(user_id, month, request.budget_cents)
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
            raise HTTPException(status_code=409, detail=str(error)) from error
        finally:
            if request.request_id:
                deps.progress.finish(user_id, request.session_id, request.request_id)
        _record_message(user_id, request.session_id, "user", "text", request.utterance)
        _record_answer(user_id, request.session_id, view)
        _record_life_event(user_id, request.session_id, request.utterance, request.selected, view)
        return _response(request.session_id, user_id, _submission_payload(view))

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
        _record_answer(user_id, request.session_id, view)
        if pending_utterance:
            _record_life_event(
                user_id, request.session_id, pending_utterance, request.answers, view,
            )
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
        if view.state.value in ("completed", "partial"):
            _mark_life_event(user_id, request.session_id, STATUS_EXECUTED)
        elif view.state.value in _ERROR_STATES:
            _mark_life_event(user_id, request.session_id, STATUS_FAILED)
        _record_message(user_id, request.session_id, "assistant", "receipt", view.message)
        return _response(request.session_id, user_id, _execution_payload(view))

    @app.post("/api/reject")
    def reject(request: _EmptyRequest, user_id: str = Depends(current_user)) -> dict[str, object]:
        """**放弃当前待确认计划**（G-02）：服务端置作废终态，原计划与已发验证码一并失效。"""
        try:
            view = _session_for(request.session_id, user_id).reject()
        except SessionStateError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        _mark_life_event(user_id, request.session_id, STATUS_REJECTED)
        _record_message(user_id, request.session_id, "assistant", "receipt", view.message)
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
        _record_message(user_id, request.session_id, "assistant", "receipt", view.message)
        return _response(request.session_id, user_id, _execution_payload(view))

    @app.post("/api/reverse")
    def reverse(request: _EmptyRequest, user_id: str = Depends(current_user)) -> dict[str, object]:
        """回退：对本次执行逐条走补偿事务（无 `settled` 参数：模拟核心银行执行即结算）。"""
        try:
            outcome = _session_for(request.session_id, user_id).reverse()
        except SessionStateError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        _record_message(user_id, request.session_id, "assistant", "receipt", outcome.message)
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

    @app.post("/api/auth/login")
    def auth_login(
        request: Request, body: _LoginRequest, response: Response
    ) -> dict[str, object]:
        """登录：校验口令后签发 Cookie 会话；口令不回显、不写日志。"""
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
            "card": _card_payload(view.card),
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
    def life_events(user_id: str = Depends(current_user)) -> dict[str, object]:
        """生活事件面板：原型清单 + 组合律 + 本用户最近 5 条记录。"""
        store = deps.life_events
        records = () if store is None else store.list_for_user(user_id, limit=5)
        return {
            "prototypes": [
                {
                    "event_id": prototype.event_id,
                    "name": prototype.name,
                    "domains": list(prototype.domains),
                    "description": prototype.description,
                    "triggers": list(prototype.triggers),
                    "example": prototype.example,
                    "steps": len(prototype.skeleton),
                }
                for prototype in LIFE_EVENT_PROTOTYPES
            ],
            "combos": [
                {"combo_id": combo.combo_id, "name": combo.name}
                for combo in LIFE_EVENT_COMBOS
            ],
            "records": [record.to_dict() for record in records],
            "records_available": store is not None,
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
        """个人中心「我的操作轨迹」：按审计 payload 里的 `user_id` 反向过滤。"""
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
        return _channel_payload(_dispatcher_for(user_id).dispatch(inbound))

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
        return _channel_payload(_dispatcher_for(user_id).dispatch(command))

    @app.get("/api/channels/health")
    def channels_health(user_id: str = Depends(current_user)) -> dict[str, object]:
        """渠道健康页：如实区分「已配置 / 未配置」，不把 stub 说成可用。"""
        body = _dispatcher_for(user_id).health()
        channels = body["channels"]
        if isinstance(channels, dict):
            channels["web"] = {"configured": True, "mode": "web", "reason": ""}
            channels[CHANNEL_FEISHU] = {
                "configured": feishu.configured,
                "mode": (
                    "adapter"
                    if feishu.configured
                    else ("replay" if feishu.replay_mode else "stub")
                ),
                "reason": (
                    ""
                    if feishu.configured
                    else ("replay_mode" if feishu.replay_mode else "not_configured")
                ),
                "card_action": feishu.configured,
            }
        body["health"] = health.snapshot()
        return body

    def _send_live_feishu(outbound: OutboundMessage) -> None:
        try:
            feishu.send(outbound)
            health.record_event(CHANNEL_FEISHU, success=True)
        except Exception:
            health.record_event(CHANNEL_FEISHU, success=False)
            health.record_retry(CHANNEL_FEISHU)

    @app.post("/api/im/feishu")
    async def im_feishu(request: Request, background_tasks: BackgroundTasks) -> JSONResponse:
        """飞书回调：验签 → 解密 → 身份解析 → 幂等 → **按用户** dispatcher。

        免登录（外部回调），但**必须**验签 + 时间戳；身份只认 `channel_identities` 绑定，
        未绑定直接 401，**绝不**回落到 `deps.user_id`。
        """
        nonlocal event_ledger
        body = await request.body()
        if not feishu.verify(request.headers, body):
            health.record_event(CHANNEL_FEISHU, success=False)
            return JSONResponse(
                status_code=401,
                content={"detail": "signature_invalid", "channel": CHANNEL_FEISHU},
            )
        try:
            payload = feishu.decode_body(body)
            event = feishu.parse_event(payload)
        except FeishuProtocolError as error:
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
            health.record_event(CHANNEL_FEISHU, success=False)
            return JSONResponse(
                status_code=401,
                content={
                    "detail": "identity_unbound",
                    "channel": CHANNEL_FEISHU,
                    "message": "该飞书账号尚未绑定，请先在网页个人中心完成绑定",
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
        try:
            outbound = _dispatcher_for(binding.user_id).dispatch(material)
        except Exception:
            # 未执行成功（401/429/409/状态机拒绝）不得消费事件，否则平台重投只会拿到空回放。
            if event_id:
                if event_ledger is not None:
                    event_ledger.release(event_id)
                else:
                    memory_event_ids.discard(event_id)
            health.record_event(CHANNEL_FEISHU, success=False)
            raise
        health.record_event(CHANNEL_FEISHU, success=True)
        response = _channel_payload(outbound)
        response["rendered"] = {
            "text": render_text(outbound),
            "feishu": feishu.render(outbound),
        }
        if feishu.configured and not feishu.replay_mode:
            background_tasks.add_task(_send_live_feishu, outbound)
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


def _risk_payload(deps: AppDeps, user_id: str) -> dict[str, object]:
    """测评状态回显：`state` / 等级 / 日期 / 来源全部如实给出（不做乐观默认）。"""
    source = deps.risk
    if source is None:
        raise HTTPException(status_code=404, detail="风险测评服务未装配")
    assessment = to_risk_assessment(source.assessment_for(user_id))
    questionnaire = list(questionnaire_payload())
    if assessment is None:
        return {
            "state": AssessmentState.UNASSESSED.value,
            "risk_profile": None,
            "assessed_on": None,
            "expires_on": None,
            "source": None,
            "source_label": None,
            "questionnaire": questionnaire,
        }
    expires_on = assessment.assessed_on + timedelta(days=assessment.valid_days)
    return {
        "state": assessment.state_on(deps.today).value,
        "risk_profile": assessment.risk_profile,
        "assessed_on": assessment.assessed_on.isoformat(),
        "expires_on": expires_on.isoformat(),
        "source": assessment.source or None,
        "source_label": RISK_SOURCE_LABELS.get(assessment.source),
        "questionnaire": questionnaire,
    }


def _submission_payload(view: SubmissionView) -> dict[str, object]:
    card = _card_payload(view.card)
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
            }
            for ask in view.clarifications
        ],
        "card": card,
        "decision": _decision_payload(view.decision),
        "plan_digest": view.card.frozen_hash if view.card is not None else "",
        "questionnaire": list(view.questionnaire),
    }


def _subscription_cancel_payload(view: ExecutionView) -> dict[str, object] | None:
    """订阅取消的**操作状态**（与"取消指引/打开第三方页面"明确区分）。

    只有回执明确带 `confirmed_by_merchant` 才可能标记为商户确认；本项目当前没有真实
    商户取消接口，因此本地执行只会落在"本地已取消（商户未确认）"。
    """
    results = [item for item in view.exec_results if item.action == "subscription.cancel"]
    if not results:
        return None
    last = results[-1]
    state = cancel_state_from_receipt(
        status=last.status, external_status=last.external_status
    )
    return {
        "state": state,
        "label": CANCEL_STATE_LABELS[state],
        "external_status": last.external_status or EXTERNAL_CANCEL_NOT_INTEGRATED,
        "external_confirmed": state == CANCEL_STATE_EXTERNAL_CONFIRMED,
        "previous_status": last.previous_status,
        "message": last.message,
        "record_ids": list(last.record_ids),
        "effect_ids": list(last.effect_ids),
        "boundary": SUBSCRIPTION_CANCEL_BOUNDARY,
    }


def _execution_payload(view: ExecutionView) -> dict[str, object]:
    card = _card_payload(view.card)
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
        "subscription_cancel": _subscription_cancel_payload(view),
    }


def _session_payload(view: SessionView) -> dict[str, object]:
    return {"state": view.state.value, "message": view.message}


def _takeover_payload(package: TakeoverPackage) -> dict[str, object]:
    return {
        "reason": package.reason,
        "level": package.level.name,
        "intent_text": package.intent_text,
        "frozen_hash": package.frozen_hash,
        "card": _card_payload(package.card),
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


def _card_payload(card: PlanCard | None) -> dict[str, object] | None:
    if card is None:
        return None
    return {
        "card_id": card.card_id,
        "frozen_hash": card.frozen_hash,
        "level": card.level.name,
        "lines": list(card.lines),
        "summary": [line.to_dict() for line in card.summary],
        "requires": list(card.requires),
        "expires_at": card.expires_at.isoformat() if card.expires_at is not None else None,
        "balance_trace": list(card.balance_trace),
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


def _life_event_name(event_id: str) -> str:
    """事件 id → 可读名称；组合事件也查一遍，查不到返回空串（不臆造名字）。"""
    for prototype in LIFE_EVENT_PROTOTYPES:
        if prototype.event_id == event_id:
            return prototype.name
    for combo in LIFE_EVENT_COMBOS:
        if combo.combo_id == event_id:
            return combo.name
    return ""


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
  .bubble.user { align-self: flex-end; background: #1f4e79; color: #fff; }
  .bubble.assistant { align-self: flex-start; background: #fff; border: 1px solid #e3e6ea; }
  .bubble.system { align-self: center; background: #fff7e6; color: #8a5a00; font-size: 12px; }
  .card { align-self: flex-start; width: 100%; background: #fff; border: 1px solid #cfd6de;
    border-radius: 10px; padding: 12px 14px; }
  .card h2 { margin: 0 0 8px; font-size: 13px; color: #1f4e79; font-weight: 600; }
  .card dl { display: grid; grid-template-columns: max-content 1fr; gap: 4px 12px; margin: 0; }
  .card dt { color: #6b7280; font-size: 12px; }
  .card dd { margin: 0; font-variant-numeric: tabular-nums; }
  .card ul { margin: 8px 0 0; padding-left: 18px; font-size: 12px; color: #4b5563; }
  .card button { margin-top: 10px; }
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
    <button type="submit">发送</button>
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
  var health = document.getElementById("health");

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) { node.className = cls; }
    if (text) { node.textContent = text; }
    return node;
  }

  function push(role, text) {
    bubbles.appendChild(el("div", "bubble " + role, text));
    bubbles.scrollTop = bubbles.scrollHeight;
  }

  function money(cents) {
    if (typeof cents !== "number") { return ""; }
    return "¥" + (cents / 100).toFixed(2);
  }

  function renderCard(card) {
    if (!card) { return; }
    var box = el("section", "card");
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
    var btn = el("button", null, "确认执行");
    btn.type = "button";
    btn.addEventListener("click", function () {
      btn.disabled = true;
      post("/api/im/confirm", {
        card_id: card.card_id,
        frozen_hash: card.frozen_hash,
        chat_id: CHAT_ID
      }).then(render).catch(function () { btn.disabled = false; });
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
          throw new Error(detail);
        }
        return data;
      });
    });
  }

  function loadHealth() {
    fetch("/api/channels/health", { credentials: "same-origin" })
      .then(function (resp) { return resp.json(); })
      .then(function (data) {
        health.textContent = "";
        var channels = data.channels || {};
        var names = Object.keys(channels);
        if (!names.length) { health.appendChild(el("span", null, "无渠道信息")); }
        names.forEach(function (name) {
          var item = channels[name] || {};
          var chip = el("span", null, null);
          chip.appendChild(el("i", "dot" + (item.configured ? " on" : "")));
          var label = name + " · " + (item.configured ? "已配置" : "未配置");
          chip.appendChild(document.createTextNode(label));
          health.appendChild(chip);
        });
      })
      .catch(function () {
        health.textContent = "";
        health.appendChild(el("span", null, "渠道健康暂不可用"));
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
        push("system", "发送失败：" + error.message + "；渠道暂不可用，请用主对话页。");
      });
  });

  loadHealth();
})();
</script>
</body>
</html>
"""


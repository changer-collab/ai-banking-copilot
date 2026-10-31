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
import os
import re
import secrets
import time
from calendar import monthrange
from collections.abc import AsyncIterator, Awaitable, Callable
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
    KIND_CHANNEL,
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
from ai_banking_copilot.scenarios.bill_findings import (
    BillNarrator,
    analyze_bill,
    bill_profile,
    category_shares,
    expense_trend,
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
    STATUS_EXECUTED,
    STATUS_FAILED,
    STATUS_REJECTED,
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
    demo_password: str | None = None
    """`demo` 模式下用于登录页提示的演示口令；`None` 时页面只写“由现场负责人提供”。"""


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
    feishu = FeishuAdapter(FeishuConfig.from_env())
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
        """页面入口：`demo` 模式未登录时先给登录页，不把应用骨架发出去。

        壳 HTML 内嵌资源版本号，**必须 no-store**：否则浏览器缓存旧 HTML 后会一直引用
        旧的 `workspace.css?v=*`，出现「代码已更新、页面还是旧版」的假象。
        """
        if auth_enabled and _auth().resolve(request.cookies.get(AUTH_COOKIE_NAME, "")) is None:
            accounts = tuple(row[0] for row in DEMO_ACCOUNTS)
            login_html = render_login_page(accounts, deps.demo_password)
            return HTMLResponse(login_html, headers={"Cache-Control": "no-store"})
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
    ) -> None:
        """识别到生活事件且已出计划卡时登记一条 `planned`。

        金额优先取表单值，其次取原话里明写的「N 元」，都没有就留空（不估算）；
        标题记具体事件，金额不重复写进标题。
        """
        store = deps.life_events
        if store is None or view.card is None:
            return
        event_id = detect_life_combo(utterance) or detect_life_event(utterance)
        if event_id is None:
            return
        event_name = _life_event_name(event_id)
        amount = _selected_amount_cents(selected)
        try:
            store.create(
                user_id=user_id,
                session_id=session_id,
                event_id=event_id,
                event_name=event_name,
                # 具体事件：优先用该类型的「常见事件」名，否则压掉金额后的原话
                event_title=_life_event_title(utterance, fallback=event_name),
                amount_cents=amount if amount is not None else _utterance_amount_cents(utterance),
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
        """只恢复与读取状态，不触发计划执行或返回挑战秘密。

        会话按 **(`user_id`, `session_id`) 二元组**隔离：默认 `session_id` 只是「该用户
        自己的默认槽」，不同用户即便都不传也不会串会话；前端每次新开页面都会生成
        独立 `session_id`，因此同一账号两个窗口互不干扰。
        """
        view = _session_for(session_id, user_id).current_view()
        return _response(session_id, user_id, _execution_payload(view))

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
            "quota": {
                "as_of": deps.today.isoformat(),
                "daily_limit_cents": min(
                    DAILY_LIMIT_YUAN * 100, gateway.daily_transfer_limit(user_id).cents
                ),
                "daily_accumulated_cents": gateway.daily_transfer_total(
                    user_id, deps.today
                ).cents,
            },
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

    @app.get("/api/cards")
    def cards(user_id: str = Depends(current_user)) -> dict[str, object]:
        """卡片面板只读事实：类型 / 状态 / 日限额。状态与额度一律读服务端，前端不得自行推导。"""
        card_types = {"debit": "借记卡", "credit": "信用卡", "virtual": "虚拟卡"}
        statuses = {"active": "正常", "locked": "已锁定", "lost": "已挂失", "cancelled": "已取消"}
        return {
            "as_of": deps.today.isoformat(),
            "cards": [
                {
                    "card_id": record.payload.card_id,
                    "type": record.payload.card_type,
                    "type_label": card_types.get(
                        record.payload.card_type, record.payload.card_type
                    ),
                    "status": record.payload.status,
                    "status_label": statuses.get(record.payload.status, record.payload.status),
                    "daily_limit_cents": record.payload.daily_limit.cents,
                    "last4": record.payload.last4 or "",
                }
                for record in deps.gateway.cards_of(user_id)
            ],
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
                {
                    "payee_id": record.payload.payee_id,
                    "name": record.payload.full_name,
                    "account_tail": record.payload.account_tail,
                    "phone_tail4": tail4(record.payload.phone_tail)
                    or tail4(record.payload.phone),
                    "blocked": record.payload.blocked,
                }
                for record in deps.gateway.payees_of(user_id)
            ]
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
        return {
            "assessment": assessment,
            "products": list(
                product_catalog(
                    products, risk_profile, nav=nav_by_product(deps.gateway, products)
                )
            ),
            "holdings": list(holdings_payload(deps.gateway, user_id)),
            "data_source": PRODUCT_DATA_SOURCE,
        }

    @app.get("/api/bills/analysis")
    def bill_analysis(
        year: int | None = None,
        month: int | None = None,
        user_id: str = Depends(current_user),
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
        report = analyze_bill(
            deps.gateway, user_id, target_year, target_month, as_of=deps.today,
        )
        narrative = narrate_bill_findings(report, deps.bill_narrator)
        profile = bill_profile(deps.gateway, user_id, report)
        return {
            **report_payload(report),
            "period_basis": "explicit" if explicit else "current_month",
            "data_through": data_through.isoformat(),
            "trend": [
                {"period": point.period, "expense_cents": point.expense_cents}
                for point in expense_trend(deps.gateway, user_id, target_year, target_month)
            ],
            "category_share": [
                {
                    "category": item.category,
                    "expense_cents": item.expense_cents,
                    "share": str(item.share),
                }
                for item in category_shares(deps.gateway, user_id, target_year, target_month)
            ],
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
        _record_answer(user_id, request.session_id, view)
        _record_life_event(user_id, request.session_id, request.utterance, request.selected, view)
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
        _record_message(
            user_id, request.session_id, "assistant", "receipt", view.message,
            {"receipt": _receipt_payload(view)},
        )
        return _response(request.session_id, user_id, _execution_payload(view))

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
        return _response(request.session_id, user_id, _execution_payload(view))

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
    def life_events(user_id: str = Depends(current_user)) -> dict[str, object]:
        """生活事件面板：原型清单 + 组合律 + 本用户最近 5 条记录。"""
        store = deps.life_events
        records = () if store is None else store.list_for_user(user_id, limit=5)
        budget = _life_budget_progress(deps.gateway, user_id)
        return {
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
            "records": [
                {
                    **record.to_dict(),
                    # 只有 L1 有可核的冻结链；其余事件不给预算块（前端不画，不造 0）
                    "budget": budget if record.event_id == "L1" else None,
                }
                for record in records
            ],
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


def _life_event_name(event_id: str) -> str:
    """事件 id → 可读名称；组合事件也查一遍，查不到返回空串（不臆造名字）。"""
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


def _life_budget_progress(gateway: FactGateway, user_id: str) -> dict[str, object] | None:
    """L1 预算链进度：三段状态都来自真实事实，缺数据返回 `None`（前端画空态，不造 0）。

    - 冻结：`holds` 里存在 `life:L1:` 记录即已冻结（`amount_cents` 为当前冻结余额）；
    - 下单：`merchant_occasion_completed` 命中下单回执（与 `_build_l1` 的 occasion 同口径）；
    - 释放：冻结记录 `status == 'released'`。

    `holds` 不记录会话，故取最新一条 `life:L1:` 记录（同一账户同一日只有一个，
    hold_id 末尾即下单日）；该配对只用于展示，不参与任何资金判断。
    """
    candidates = [
        record.payload
        for account in gateway.accounts_of(user_id)
        for record in gateway.holds_of(account.payload.account_id)
        if record.payload.hold_id.startswith("life:L1:")
    ]
    if not candidates:
        return None
    hold = max(candidates, key=lambda item: item.hold_id)
    order_day = hold.hold_id.rsplit(":", 1)[-1]
    return {
        "hold_id": hold.hold_id,
        "amount_cents": hold.amount.cents,
        "state": hold.status,
        "order_on": order_day,
        "steps": {
            "freeze": True,
            "order": gateway.merchant_occasion_completed(user_id, f"life:L1:{order_day}"),
            "release": hold.status == "released",
        },
    }


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


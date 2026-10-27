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
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
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
from ai_banking_copilot.policy.audit import KIND_OPERATOR, AuditEvent, AuditLog
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
    AssessmentState,
    to_risk_assessment,
)
from ai_banking_copilot.scenarios.wealth_assessment import (
    AssessmentError,
    evaluate_answers,
    questionnaire_payload,
)
from ai_banking_copilot.tools.registry import ToolRegistry
from ai_banking_copilot.web.channels.contracts import (
    CHANNEL_FEISHU,
    CHANNEL_LOCAL,
    ChannelError,
    ConfirmCommand,
    InboundMessage,
    OutboundMessage,
)
from ai_banking_copilot.web.channels.dispatcher import Dispatcher
from ai_banking_copilot.web.channels.identity import (
    IdentityDirectory,
    MemoryIdentityDirectory,
    StoreIdentityDirectory,
    init_channel_schema,
)
from ai_banking_copilot.web.channels.render import render_feishu_card, render_text
from ai_banking_copilot.web.insights import InsightService
from ai_banking_copilot.web.page import APP_HTML, INDEX_HTML
from ai_banking_copilot.web.plan_card import PlanCard, describe_decision
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
    """按 `AppDeps` 构造会话管理器；Web 与 IM 共用**同一份**实现（避免两套状态机）。"""
    return SessionManager(
        deps.session_repository,
        factory=lambda _user, session_id, lock: Session(
            deps, session_id=session_id, shared_lock=lock,
        ),
        restore=lambda snapshot, lock: Session.restore(deps, snapshot, lock),
    )


def create_app(deps: AppDeps) -> FastAPI:
    """装配单页渠道。计划卡与执行全部来自上游；本层只做 HTTP 与序列化。

    **多会话（P2-7）**：按 `session_id` 维护**相互隔离**的 `Session`（缺省 `DEFAULT_SESSION_ID`
    保持既有单会话行为）；`AppDeps` 的字段签名不变（上游依赖仍由组合根注入一次）。
    """
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
    dispatcher = Dispatcher(
        manager=manager,
        identities=identities,
        submission_payload=_submission_payload,
        execution_payload=_execution_payload,
        audit=deps.audit,
        server_user_id=deps.user_id,
    )

    @app.exception_handler(SessionPersistenceError)
    @app.exception_handler(SessionSnapshotError)
    async def persistence_error(_request: Request, _error: Exception) -> JSONResponse:
        return JSONResponse(status_code=409, content={
            "detail": "会话存储或恢复失败，已暂停；请核对业务回执后重启服务",
        })

    @app.exception_handler(ChannelError)
    async def channel_error(_request: Request, error: ChannelError) -> JSONResponse:
        """渠道层稳定错误：直接采用错误族自带的 `status_code`，不吞成 500。"""
        return JSONResponse(
            status_code=error.status_code,
            content={"detail": error.code, "message": str(error)},
        )

    def _session_for(session_id: str) -> Session:
        return manager.get(deps.user_id, session_id)

    def _response(session_id: str, payload: dict[str, object]) -> dict[str, object]:
        return {**payload, "model": _session_for(session_id).model_metadata()}

    @app.get("/api/session")
    def session_state(session_id: str = DEFAULT_SESSION_ID) -> dict[str, object]:
        """只恢复与读取状态，不触发计划执行或返回挑战秘密。"""
        return _response(session_id, _execution_payload(_session_for(session_id).current_view()))

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        return HTMLResponse(INDEX_HTML)

    @app.get("/m", response_class=HTMLResponse)
    def index_app() -> HTMLResponse:
        """APP 端版式：同一份骨架 + `data-view="app"`，手机框内铺满。"""
        return HTMLResponse(APP_HTML)

    @app.get("/api/options")
    def options() -> dict[str, object]:
        """对象选择只展示当前用户的事实；金额不由前端编造。"""
        gateway, user_id = deps.gateway, deps.user_id
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
    def risk_assessment_state() -> dict[str, object]:
        """当前已认证用户的测评状态（只读）；未测评 / 过期如实返回，绝不默认等级。"""
        return _risk_payload(deps)

    @app.post("/api/risk-assessment")
    def save_risk_assessment(request: _RiskAssessmentRequest) -> dict[str, object]:
        """用户侧测评录入（真实测评入口）。

        - **问卷路径**（`answers`）：固定 5 题，等级由 `scenarios.wealth_assessment` 算出，
          请求体不得同时给 `risk_profile`（模型不得自填等级），落库 `source='questionnaire'`；
        - **直接录入路径**（`risk_profile`）：保留兼容，落库 `source='user_input'`；
        - **身份**只取服务端 `deps.user_id`，请求体不能指定他人；
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
                deps.user_id, result.risk_profile, deps.today, source=SOURCE_QUESTIONNAIRE,
            )
            return {
                "state": "updated",
                "assessment": _risk_payload(deps),
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
            writer.save(deps.user_id, request.risk_profile.strip().upper(), deps.today)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return {"state": "updated", "assessment": _risk_payload(deps)}

    @app.get("/api/bills/analysis")
    def bill_analysis(year: int | None = None, month: int | None = None) -> dict[str, object]:
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
            deps.gateway, deps.user_id, target_year, target_month, as_of=deps.today,
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
    def insights(include_resolved: bool = False) -> dict[str, object]:
        """读取当前用户的只读洞察收件箱；不会继承任何执行授权。"""
        service = deps.insights
        if service is None:
            return {"insights": [], "scanned_at": None, "scan_error": None}
        state = service.scan_state(deps.user_id)
        return {
            "insights": [
                item.to_dict()
                for item in service.list_inbox(
                    deps.user_id, include_resolved=include_resolved
                )
            ],
            "scanned_at": state.scanned_at if state is not None else None,
            "scan_error": state.error if state is not None else None,
        }

    @app.get("/api/transfers")
    def transfer_records(days: int = 30) -> dict[str, object]:
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
            for record in gateway.payees_of(deps.user_id)
        }
        accounts = gateway.accounts_of(deps.user_id)
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
        for booking_record in gateway.scheduled_transfers_of(deps.user_id, status="scheduled"):
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
    def set_insight_budget(request: _BudgetRequest) -> dict[str, object]:
        """保存提醒偏好并重新扫描；预算不是银行额度或执行授权。"""
        service = deps.insights
        if service is None:
            raise HTTPException(status_code=404, detail="主动洞察未装配")
        month = request.month or deps.today.strftime("%Y-%m")
        if len(month) != 7 or month[4] != "-":
            raise HTTPException(status_code=400, detail="月份格式必须为 YYYY-MM")
        try:
            service.set_budget(deps.user_id, month, request.budget_cents)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        result = service.scan(deps.user_id, deps.today)
        return {
            "state": "updated",
            "scan_error": result.scan_error,
            "insights": [item.to_dict() for item in result.insights],
        }

    @app.post("/api/due")
    def due(request: _EmptyRequest) -> dict[str, object]:
        # 到期日只取服务端时钟；客户端不能提前触发未来预约。
        try:
            return _response(
                request.session_id,
                _submission_payload(_session_for(request.session_id).check_due()),
            )
        except SessionStateError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.post("/api/submit")
    def submit(request: _SubmitRequest) -> dict[str, object]:
        try:
            view = _session_for(request.session_id).submit(
                request.utterance,
                selected=request.selected,
            )
        except SessionStateError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return _response(request.session_id, _submission_payload(view))

    @app.post("/api/clarify")
    def clarify(request: _ClarifyRequest) -> dict[str, object]:
        """澄清闭环：用补充答案重跑上一条指令，用户无需整句重述。"""
        try:
            view = _session_for(request.session_id).clarify(request.answers)
        except SessionStateError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return _response(request.session_id, _submission_payload(view))

    @app.post("/api/verify/request")
    def request_verification(request: _EmptyRequest) -> dict[str, object]:
        """申请与会话及冻结计划绑定的一次性强验证挑战。"""
        try:
            prompt = _session_for(request.session_id).request_verification()
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
    def confirm(request: _ConfirmRequest) -> dict[str, object]:
        try:
            view = _session_for(request.session_id).confirm(
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
        return _response(request.session_id, _execution_payload(view))

    @app.post("/api/reject")
    def reject(request: _EmptyRequest) -> dict[str, object]:
        """**放弃当前待确认计划**（G-02）：服务端置作废终态，原计划与已发验证码一并失效。"""
        try:
            view = _session_for(request.session_id).reject()
        except SessionStateError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return _session_payload(view)

    @app.post("/api/interrupt")
    def interrupt(request: _EmptyRequest) -> dict[str, object]:
        try:
            view = _session_for(request.session_id).interrupt()
        except SessionStateError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return _session_payload(view)

    @app.post("/api/resume")
    def resume(request: _EmptyRequest) -> dict[str, object]:
        try:
            view = _session_for(request.session_id).resume()
        except SessionStateError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return _response(request.session_id, _execution_payload(view))

    @app.post("/api/reverse")
    def reverse(request: _EmptyRequest) -> dict[str, object]:
        """回退：对本次执行逐条走补偿事务（无 `settled` 参数：模拟核心银行执行即结算）。"""
        try:
            outcome = _session_for(request.session_id).reverse()
        except SessionStateError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return {
            "state": outcome.state.value,
            "message": outcome.message,
            "compensated_steps": list(outcome.compensated_steps),
        }

    @app.get("/api/takeover")
    def takeover(session_id: str = DEFAULT_SESSION_ID) -> dict[str, object]:
        try:
            package = _session_for(session_id).takeover_package()
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

    @app.post("/api/im/message")
    def im_message(request: _ImMessageRequest) -> dict[str, object]:
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
        return _channel_payload(dispatcher.dispatch(inbound))

    @app.post("/api/im/confirm")
    def im_confirm(request: _ImConfirmRequest) -> dict[str, object]:
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
        return _channel_payload(dispatcher.dispatch(command))

    @app.get("/api/channels/health")
    def channels_health() -> dict[str, object]:
        """渠道健康页：如实区分「已配置 / 未配置」，不把 stub 说成可用。"""
        return dispatcher.health()

    @app.post("/api/im/feishu")
    def im_feishu() -> JSONResponse:
        """飞书回调：**当前未接入**（M2 未完成），如实 501，不做假成功。"""
        return JSONResponse(
            status_code=501,
            content={
                "detail": "not_configured",
                "channel": CHANNEL_FEISHU,
                "reason": "feishu_adapter_not_integrated",
            },
        )

    @app.get("/im", response_class=HTMLResponse)
    def im_page() -> HTMLResponse:
        """IM 聊天页占位（前端冻结中，待组长放行后统一重做）。"""
        return HTMLResponse(_IM_PAGE_HTML)

    return app


def _mask_payee_name(name: str) -> str:
    """收款人姓名脱敏：三字及以上保留首末字，两字保留首字；空名一律不显示。"""
    if len(name) >= 3:
        return f"{name[0]}*{name[-1]}"
    if len(name) == 2:
        return f"{name[0]}*"
    return "收款人已隐藏"


def _risk_payload(deps: AppDeps) -> dict[str, object]:
    """测评状态回显：`state` / 等级 / 日期 / 来源全部如实给出（不做乐观默认）。"""
    source = deps.risk
    if source is None:
        raise HTTPException(status_code=404, detail="风险测评服务未装配")
    assessment = to_risk_assessment(source.assessment_for(deps.user_id))
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
            {"slot": ask.slot, "prompt": ask.prompt} for ask in view.clarifications
        ],
        "card": card,
        "decision": _decision_payload(view.decision),
        "plan_digest": view.card.frozen_hash if view.card is not None else "",
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


_IM_PAGE_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AI 银行副驾 · IM 渠道</title>
</head>
<body>
<main>
  <h1>IM 渠道</h1>
  <p>聊天页前端正在统一重做中，暂由 <code>POST /api/im/message</code> 与
  <code>POST /api/im/confirm</code> 提供同一套会话状态机。</p>
  <p>渠道健康：<a href="/api/channels/health">/api/channels/health</a></p>
</main>
</body>
</html>
"""


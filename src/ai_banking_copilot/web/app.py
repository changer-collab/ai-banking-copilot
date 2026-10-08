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

import secrets
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ai_banking_copilot.contracts.authorization import Decision
from ai_banking_copilot.contracts.plan import Plan, cfhash
from ai_banking_copilot.gateway.core_bank import DEFAULT_BOOKING_DATE
from ai_banking_copilot.gateway.facts import FactGateway
from ai_banking_copilot.gateway.risk_store import RiskAssessmentSource
from ai_banking_copilot.gateway.seed import DEMO_USER_ID
from ai_banking_copilot.planning.llm import IntentModel
from ai_banking_copilot.policy.audit import KIND_OPERATOR, AuditEvent, AuditLog
from ai_banking_copilot.policy.breaker import CircuitBreaker
from ai_banking_copilot.policy.gate import StalePlanError
from ai_banking_copilot.policy.pep import Pep
from ai_banking_copilot.policy.reversal import ReversalService
from ai_banking_copilot.tools.registry import ToolRegistry
from ai_banking_copilot.web.page import INDEX_HTML
from ai_banking_copilot.web.plan_card import PlanCard, describe_decision
from ai_banking_copilot.web.session import (
    ConfirmationMismatchError,
    ExecutionView,
    Session,
    SessionStateError,
    SessionView,
    StrongVerificationRequiredError,
    SubmissionView,
    TakeoverPackage,
)


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
    reversal: ReversalService | None = None
    """补偿通道（组合根注入）：`None ⇒ 回退如实失败`，不谎称已退回（见 `policy/reversal.py`）。"""
    operator_token: str | None = None
    """**运维口令**（裁决 #27）：`POST /api/operator/breaker-unlock` 的凭证，取自环境变量
    `AIB_OPERATOR_TOKEN`。**未配置（`None` / 空串）⇒ 该端点一律 403**（fail-closed）——
    绝不允许「默认无口令即可解锁熔断」这种自毁式配置。"""


DEFAULT_SESSION_ID = "default"
"""缺省会话标识：请求体未带 `session_id` 时用它，保持既有单会话行为不变（多会话隔离的兜底）。"""


class _SubmitRequest(BaseModel):
    utterance: str
    selected: dict[str, object] | None = None
    session_anomaly: bool = False
    """会话异常（设备 / 地点跳变）。**生产环境应由渠道风控判定后注入，不得直接取自客户端载荷**；
    演示渠道无风控模块，故开放该字段以便现场演示熔断触发 5。"""
    session_id: str = DEFAULT_SESSION_ID
    """会话标识：不同取值对应**相互隔离**的会话（各自 pending / 状态机独立）。"""


class _ConfirmRequest(BaseModel):
    card_id: str
    frozen_hash: str
    verification_code: str | None = None
    steps: int | None = None
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


def create_app(deps: AppDeps) -> FastAPI:
    """装配单页渠道。计划卡与执行全部来自上游；本层只做 HTTP 与序列化。

    **多会话（P2-7）**：按 `session_id` 维护**相互隔离**的 `Session`（缺省 `DEFAULT_SESSION_ID`
    保持既有单会话行为）；`AppDeps` 的字段签名不变（上游依赖仍由组合根注入一次）。
    """
    app = FastAPI(title="AI 银行副驾 · Web 渠道", version="0.1.0")
    app.mount(
        "/assets",
        StaticFiles(directory=str(Path(__file__).parent / "assets")),
        name="assets",
    )
    sessions: dict[str, Session] = {}

    def _session_for(session_id: str) -> Session:
        session = sessions.get(session_id)
        if session is None:
            session = Session(deps)
            sessions[session_id] = session
        return session

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        return HTMLResponse(INDEX_HTML)

    @app.post("/api/submit")
    def submit(request: _SubmitRequest) -> dict[str, object]:
        view = _session_for(request.session_id).submit(
            request.utterance,
            selected=request.selected,
            session_anomaly=request.session_anomaly,
        )
        return _submission_payload(view)

    @app.post("/api/clarify")
    def clarify(request: _ClarifyRequest) -> dict[str, object]:
        """澄清闭环：用补充答案重跑上一条指令，用户无需整句重述。"""
        try:
            view = _session_for(request.session_id).clarify(request.answers)
        except SessionStateError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return _submission_payload(view)

    @app.post("/api/verify/request")
    def request_verification(request: _EmptyRequest) -> dict[str, object]:
        """下发红色卡的强验证挑战。

        **演示声明**：未接入真实短信 / 人脸 / U 盾渠道，验证码随响应回显以便现场输入；
        校验为服务端一次性 + TTL + 与冻结计划绑定，接入真实渠道只需替换下发一步。
        """
        try:
            prompt = _session_for(request.session_id).request_verification()
        except (SessionStateError, StrongVerificationRequiredError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return {
            "channel": prompt.channel,
            "target": prompt.target,
            "code": prompt.code,
            "expires_in_s": prompt.expires_in_s,
        }

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
        return _execution_payload(view)

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
        return _execution_payload(view)

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

        before_state = deps.breaker.state().value
        before_triggers = [trigger.value for trigger in deps.breaker.triggers()]
        deps.breaker.unlock()
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
                    "state_after": deps.breaker.state().value,
                },
                prev_hash="",
            )
        )
        return {
            "action": "breaker-unlock",
            "reason": reason,
            "state_before": before_state,
            "triggers_before": before_triggers,
            "state_after": deps.breaker.state().value,
        }

    return app


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


def _execution_payload(view: ExecutionView) -> dict[str, object]:
    return {
        "state": view.state.value,
        "message": view.message,
        "frozen_hash": view.frozen_hash,
        "record_ids": list(view.record_ids),
        "results": [
            {
                "status": result.status,
                "message": result.message,
                "record_ids": list(result.record_ids),
            }
            for result in view.exec_results
        ],
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
    }


def _decision_payload(decision: Decision | None) -> dict[str, object] | None:
    if decision is None:
        return None
    return {"level": decision.level.name, "reasons": list(describe_decision(decision))}

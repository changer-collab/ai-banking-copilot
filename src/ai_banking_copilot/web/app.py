"""FastAPI 渠道装配：依赖注入契约 `AppDeps` + 单页渠道 `create_app`。

`AppDeps` 是渠道层的**依赖注入契约**：会话层只依赖它，不自行装配上游组件，
以便测试注入临时数据库 / 假意图通道（LLM-on/off 差分）。

单页 HTML **内联**（无前端构建链）：样式取值来自设计令牌（银行凭证语汇：防伪底纹 /
骑缝编号 / 等宽票据数字 / 三色戳记 / 熔断封条）；窄屏用**容器查询**而非视口媒体查询。
确认按钮回显**冻结哈希**（cfhash），确认与执行前由 `policy.gate` / `policy.pep` 做
计划级 + 节点级双层执行门校验。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from ai_banking_copilot.contracts.authorization import Decision
from ai_banking_copilot.contracts.plan import Plan, cfhash
from ai_banking_copilot.gateway.core_bank import DEFAULT_BOOKING_DATE
from ai_banking_copilot.gateway.facts import FactGateway
from ai_banking_copilot.gateway.seed import DEMO_USER_ID
from ai_banking_copilot.planning.llm import IntentModel
from ai_banking_copilot.policy.audit import AuditLog
from ai_banking_copilot.policy.breaker import CircuitBreaker
from ai_banking_copilot.policy.gate import StalePlanError
from ai_banking_copilot.policy.pep import Pep
from ai_banking_copilot.tools.registry import ToolRegistry
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
    """渠道层依赖装配（全部来自上游公开符号；本层不做任何裁决、不持有写连接）。"""

    intent_model: IntentModel
    registry: ToolRegistry
    gateway: FactGateway
    pep: Pep
    breaker: CircuitBreaker
    audit: AuditLog
    user_id: str = DEMO_USER_ID
    today: date = DEFAULT_BOOKING_DATE


class _SubmitRequest(BaseModel):
    utterance: str
    selected: dict[str, object] | None = None


class _ConfirmRequest(BaseModel):
    card_id: str
    frozen_hash: str
    strong_verified: bool = False
    steps: int | None = None


class _EmptyRequest(BaseModel):
    """空请求体（中断 / 恢复）。"""


class _ReverseRequest(BaseModel):
    settled: bool


def create_app(deps: AppDeps) -> FastAPI:
    """装配单页渠道。计划卡与执行全部来自上游；本层只做 HTTP 与序列化。"""
    app = FastAPI(title="AI 银行副驾 · Web 渠道", version="0.1.0")
    session = Session(deps)

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        return HTMLResponse(_INDEX_HTML)

    @app.post("/api/submit")
    def submit(request: _SubmitRequest) -> dict[str, object]:
        view = session.submit(request.utterance, selected=request.selected)
        return _submission_payload(view)

    @app.post("/api/confirm")
    def confirm(request: _ConfirmRequest) -> dict[str, object]:
        try:
            view = session.confirm(
                request.card_id,
                frozen_hash=request.frozen_hash,
                strong_verified=request.strong_verified,
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
            view = session.interrupt()
        except SessionStateError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return _session_payload(view)

    @app.post("/api/resume")
    def resume(request: _EmptyRequest) -> dict[str, object]:
        try:
            view = session.resume()
        except SessionStateError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return _execution_payload(view)

    @app.post("/api/reverse")
    def reverse(request: _ReverseRequest) -> dict[str, object]:
        try:
            outcome = session.reverse(now=datetime.now(), settled=request.settled)
        except SessionStateError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return {
            "state": outcome.state.value,
            "message": outcome.message,
            "compensated_steps": list(outcome.compensated_steps),
        }

    @app.get("/api/takeover")
    def takeover() -> dict[str, object]:
        try:
            package = session.takeover_package()
        except SessionStateError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return _takeover_payload(package)

    return app


def _submission_payload(view: SubmissionView) -> dict[str, object]:
    return {
        "state": view.state.value,
        "message": view.message,
        "questions": list(view.questions),
        "card": _card_payload(view.card),
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


_INDEX_HTML = """<!DOCTYPE html>
<html lang="zh-CN" data-theme="light">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>AI 银行副驾 · 计划卡</title>
  <style>
    /* 设计令牌（取自设计仓 assets/tokens.css；不引用任何第三方商标 / 官方色值） */
    :root {
      --paper: #f2f5fa;
      --paper-grid: rgba(11, 79, 216, 0.045);
      --voucher: #ffffff;
      --voucher-alt: #fbfcfe;
      --ink: #0a1b2e;
      --ink-2: #3f5570;
      --ink-3: #7b8ba2;
      --rule: #dbe3ee;
      --rule-strong: #c1cdde;
      --blue-600: #0b4fd8;
      --blue-ink: #062348;
      --lvl-green: #0f8a5f;
      --lvl-green-bg: #e6f5ef;
      --lvl-yellow: #a8710f;
      --lvl-yellow-bg: #fdf3e0;
      --lvl-red: #b3362a;
      --lvl-red-bg: #fdecea;
      --lvl-denied: #5b6473;
      --lvl-denied-bg: #eef0f3;
      --seal-line: rgba(179, 54, 42, 0.42);
      --font-display: "Source Han Serif SC", "Noto Serif SC", "Songti SC", SimSun, Georgia, serif;
      --font-body: "PingFang SC", "HarmonyOS Sans SC", "Microsoft YaHei UI", "Microsoft YaHei",
        system-ui, sans-serif;
      --font-mono: "Cascadia Mono", "JetBrains Mono", ui-monospace, Consolas, monospace;
      --fs-h1: 24px; --fs-h2: 17px; --fs-body: 14px; --fs-small: 12.5px; --fs-mono: 13px;
      --fs-amount: 27px; --lh-body: 1.65;
      --s-2: 8px; --s-3: 12px; --s-4: 16px; --s-5: 24px; --s-6: 32px;
      --r-card: 14px; --r-control: 10px;
      --sh-2: 0 1px 2px rgba(10, 27, 46, 0.06), 0 10px 30px -14px rgba(10, 27, 46, 0.22);
      --ease-out: cubic-bezier(0.2, 0.7, 0.2, 1);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0; color: var(--ink); font-family: var(--font-body); font-size: var(--fs-body);
      line-height: var(--lh-body);
      background-image:
        repeating-linear-gradient(45deg, var(--paper-grid) 0 1px, transparent 1px 9px),
        repeating-linear-gradient(-45deg, var(--paper-grid) 0 1px, transparent 1px 14px),
        radial-gradient(120% 90% at 12% -10%, rgba(46, 123, 255, 0.09), transparent 55%);
      background-color: var(--paper);
    }
    .shell {
      container-type: inline-size; max-width: 1040px; margin: 0 auto;
      padding: var(--s-6) var(--s-5);
    }
    .brand { font-family: var(--font-display); font-size: var(--fs-h1); letter-spacing: -0.02em; }
    .brand em { font-style: normal; color: var(--blue-600); }
    .lead { color: var(--ink-2); max-width: 62ch; }
    .panel {
      background: var(--voucher); border: 1px solid var(--rule); border-radius: var(--r-card);
      box-shadow: var(--sh-2); padding: var(--s-5); margin-top: var(--s-5);
    }
    label {
      display: block; color: var(--ink-3); font-size: var(--fs-small); letter-spacing: 0.06em;
    }
    textarea {
      width: 100%; margin-top: var(--s-2); padding: var(--s-3); min-height: 72px;
      font-family: var(--font-body); font-size: var(--fs-body); color: var(--ink);
      border: 1px solid var(--rule-strong); border-radius: var(--r-control);
      background: var(--voucher-alt);
    }
    .btn-row { display: flex; flex-wrap: wrap; gap: var(--s-2); margin-top: var(--s-3); }
    button {
      font: inherit; padding: 8px 14px; border-radius: var(--r-control); cursor: pointer;
      border: 1px solid var(--rule-strong); background: var(--voucher-alt); color: var(--ink);
    }
    button.primary { background: var(--blue-600); border-color: var(--blue-600); color: #fff; }
    button:disabled { opacity: 0.45; cursor: not-allowed; }
    .mono { font-family: var(--font-mono); font-size: var(--fs-mono); letter-spacing: 0.02em;
      font-variant-numeric: tabular-nums; }
    /* 凭证卡：撕口 + 防伪扭索 */
    .voucher {
      position: relative; background: var(--voucher); border: 1px solid var(--rule);
      border-radius: var(--r-card); box-shadow: var(--sh-2); padding: var(--s-5);
      margin-top: var(--s-4); overflow: hidden;
      animation: rise 460ms var(--ease-out) both;
    }
    .voucher::before {
      content: ""; position: absolute; top: -7px; left: 0; right: 0; height: 14px;
      background-image: radial-gradient(circle at 7px 7px, var(--paper) 6px, transparent 6.5px);
      background-size: 14px 14px; background-repeat: repeat-x; pointer-events: none;
    }
    .voucher::after {
      content: ""; position: absolute; inset: 0; pointer-events: none;
      background-image:
        repeating-linear-gradient(90deg, transparent 0 5px, rgba(11, 79, 216, 0.035) 5px 6px);
    }
    .voucher > * { position: relative; z-index: 1; }
    .voucher__head { display: flex; align-items: flex-start; gap: var(--s-4);
      padding-bottom: var(--s-3); border-bottom: 1px dashed var(--rule-strong); }
    .doc-no__label { font-size: var(--fs-small); color: var(--ink-3); letter-spacing: 0.08em; }
    .doc-no__value {
      font-family: var(--font-mono); font-size: var(--fs-mono); color: var(--blue-ink);
      word-break: break-all;
    }
    .voucher__body { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 200px);
      gap: var(--s-5); align-items: center; padding: var(--s-4) 0; }
    .steps { margin: 0; padding: 0; list-style: none; display: grid; gap: var(--s-2); }
    .steps li { display: grid; grid-template-columns: 68px minmax(0, 1fr); gap: var(--s-3);
      align-items: baseline; }
    .steps .key { color: var(--ink-3); font-size: var(--fs-small); letter-spacing: 0.06em; }
    .amount { font-family: var(--font-mono); font-variant-numeric: tabular-nums; }
    /* 三色戳记（分级以戳记可见） */
    .seal {
      position: relative; flex: none; width: 104px; height: 104px; border-radius: 50%;
      display: grid; place-content: center; text-align: center; gap: 2px;
      transform: rotate(-7deg); color: var(--lvl-denied); background: var(--lvl-denied-bg);
      border: 2px solid currentColor;
      box-shadow: inset 0 0 0 1px currentColor;
      animation: stamp 460ms var(--ease-out) both;
    }
    .seal--green { color: var(--lvl-green); background: var(--lvl-green-bg); }
    .seal--yellow { color: var(--lvl-yellow); background: var(--lvl-yellow-bg); }
    .seal--red { color: var(--lvl-red); background: var(--lvl-red-bg); }
    .seal--denied { color: var(--lvl-denied); background: var(--lvl-denied-bg); }
    .seal__level { font-family: var(--font-display); font-size: 26px; letter-spacing: 0.04em; }
    .seal__text { font-size: var(--fs-small); letter-spacing: 0.14em; }
    .seal--red::after { content: ""; position: absolute; inset: -8px; border-radius: 50%;
      border: 1px dashed var(--seal-line); }
    /* 熔断封条（加严态：封条样式） */
    .strip { display: none; margin-top: var(--s-3); padding: var(--s-2) var(--s-3);
      border: 1px dashed var(--lvl-red); border-radius: var(--r-control);
      background: var(--lvl-red-bg); color: var(--lvl-red); font-family: var(--font-mono);
      font-size: var(--fs-mono); letter-spacing: 0.14em; }
    .strip.on { display: block; }
    pre {
      background: var(--voucher-alt); border: 1px solid var(--rule);
      border-radius: var(--r-control); padding: var(--s-3); overflow: auto;
      font-family: var(--font-mono); font-size: var(--fs-mono);
    }
    @keyframes stamp {
      from { transform: rotate(-7deg) scale(1.5); opacity: 0; }
      to { transform: rotate(-7deg) scale(1); opacity: 1; }
    }
    @keyframes rise {
      from { transform: translateY(8px); opacity: 0; }
      to { transform: none; opacity: 1; }
    }
    /* 窄屏：容器查询（非视口媒体查询） */
    @container (max-width: 560px) {
      .voucher { padding: var(--s-4); }
      .voucher__head { flex-wrap: wrap; gap: var(--s-3); }
      .voucher__body { grid-template-columns: minmax(0, 1fr); gap: var(--s-3); }
      .seal { width: 78px; height: 78px; }
      .seal__level { font-size: 21px; }
      .steps li { grid-template-columns: minmax(0, 1fr); gap: 0; }
    }
  </style>
</head>
<body>
  <div class="shell">
    <p class="brand">AI 银行<em>副驾</em></p>
    <p class="lead">
      计划卡由确定性模板渲染；确认绑定冻结哈希（cfhash）；
      执行前经计划级 + 节点级双层执行门校验。
    </p>

    <div class="panel">
      <label for="utterance">一句话指令</label>
      <textarea id="utterance">转给李娜 500 元</textarea>
      <div class="btn-row">
        <button class="primary" onclick="submitUtterance()">提交</button>
        <button id="btn-confirm" disabled onclick="confirmCard()">确认并执行</button>
        <button id="btn-interrupt" disabled onclick="interruptRun()">中断</button>
        <button id="btn-resume" disabled onclick="resumeRun()">恢复</button>
        <button id="btn-reverse" disabled onclick="reverseRun()">回退</button>
        <button onclick="fetchTakeover()">人工接管包</button>
      </div>
      <div class="strip" id="breaker-strip">封条 · 熔断打开：非纯查询转人工接管</div>
    </div>

    <div id="stage"></div>
    <div class="panel">
      <p class="doc-no__label">机器可读回执</p>
      <pre id="out">等待操作…</pre>
    </div>
  </div>
  <script>
    let lastCard = null;
    async function post(path, body) {
      const response = await fetch(path, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body || {}),
      });
      return { status: response.status, data: await response.json() };
    }
    function sealClass(level) {
      return "seal--" + String(level || "denied").toLowerCase();
    }
    function renderSubmission(data) {
      const stage = document.getElementById("stage");
      if (!data.card) { stage.innerHTML = ""; return; }
      const card = data.card;
      const lines = card.lines.map(function (line) {
        return '<li><span class="key">步骤</span><span class="amount">' + line + "</span></li>";
      }).join("");
      stage.innerHTML =
        '<div class="voucher">' +
          '<div class="voucher__head">' +
            '<div class="doc-no"><span class="doc-no__label">骑缝哈希（cfhash）</span>' +
            '<span class="doc-no__value">' + card.frozen_hash + "</span></div>" +
          "</div>" +
          '<div class="voucher__body">' +
            '<ul class="steps">' + lines + "</ul>" +
            '<div class="seal ' + sealClass(card.level) + '">' +
              '<span class="seal__level">' + card.level + "</span>" +
              '<span class="seal__text">' +
              (card.requires.length ? "需确认" : "自动执行") + "</span>" +
            "</div>" +
          "</div>" +
          '<div class="doc-no"><span class="doc-no__label">计划卡</span>' +
          '<span class="mono">' + card.card_id + "</span></div>" +
        "</div>";
    }
    function syncButtons(state) {
      document.getElementById("btn-confirm").disabled = state !== "awaiting_confirm";
      const interruptible = state === "awaiting_confirm" || state === "executing";
      document.getElementById("btn-interrupt").disabled = !interruptible;
      document.getElementById("btn-resume").disabled = state !== "interrupted";
      const reversible = state === "completed" || state === "partial";
      document.getElementById("btn-reverse").disabled = !reversible;
    }
    function echoFrozenHash(card) {
      const button = document.getElementById("btn-confirm");
      button.textContent = card
        ? "确认并执行 · " + card.frozen_hash.slice(0, 12)
        : "确认并执行";
    }
    async function submitUtterance() {
      const text = document.getElementById("utterance").value;
      const result = await post("/api/submit", {
        utterance: text,
        // 演示页无选择器：按真实 UI 的做法声明「当前选中对象」（受信任调用方职责）
        selected: { account: "acc_savings", card: "card_debit", subscription: "sub_video" },
      });
      lastCard = result.data.card;
      echoFrozenHash(lastCard);
      document.getElementById("breaker-strip").classList.remove("on");
      renderSubmission(result.data);
      syncButtons(result.data.state);
      document.getElementById("out").textContent = JSON.stringify(result.data, null, 2);
    }
    async function confirmCard() {
      if (!lastCard) { return; }
      const result = await post("/api/confirm", {
        card_id: lastCard.card_id,
        frozen_hash: lastCard.frozen_hash,
      });
      if (result.data.state === "taken_over") {
        document.getElementById("breaker-strip").classList.add("on");
      }
      syncButtons(result.data.state);
      document.getElementById("out").textContent = JSON.stringify(result.data, null, 2);
    }
    async function interruptRun() {
      const result = await post("/api/interrupt", {});
      syncButtons(result.data.state);
      document.getElementById("out").textContent = JSON.stringify(result.data, null, 2);
    }
    async function resumeRun() {
      const result = await post("/api/resume", {});
      syncButtons(result.data.state);
      document.getElementById("out").textContent = JSON.stringify(result.data, null, 2);
    }
    async function reverseRun() {
      const result = await post("/api/reverse", { settled: true });
      document.getElementById("out").textContent = JSON.stringify(result.data, null, 2);
    }
    async function fetchTakeover() {
      const response = await fetch("/api/takeover");
      document.getElementById("out").textContent = JSON.stringify(await response.json(), null, 2);
    }
  </script>
</body>
</html>
"""

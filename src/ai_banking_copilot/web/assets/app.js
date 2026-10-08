/* ==========================================================================
   AI 银行副驾 · Web 渠道交互（**真实 API**）
   版式与组件词汇对齐设计仓 `demo.html` / `app.css`（凭证 / 戳记 / 步骤 / 链 / 时间线）。
   与设计仓 `demo.js` 的区别：本文件不做任何 mock——所有数据来自同源 `/api/*`。
   ========================================================================== */

const PRESETS = [
  "转给李娜 500 元",
  "转给李娜 1200 元，再查一下上月账单",
  "查一下上月账单",
  "冻结 500 元，转给李娜 300 元",
  "转给李娜 500 元，取消订阅",
  "订阅取消",
  "锁卡",
  "挂失卡片",
  "帮我做点什么",
];

/* 演示页无选择器：按真实 UI 的做法声明「当前选中对象」（受信任调用方职责） */
const SELECTED = { account: "acc_savings", card: "card_debit", subscription: "sub_video", daily_limit: 100000 };

const LEVEL = {
  GREEN: { cls: "green", code: "L0", name: "绿色", text: "核验通过", policy: "自动执行" },
  YELLOW: { cls: "yellow", code: "L1", name: "黄色", text: "待您确认", policy: "用户确认" },
  RED: { cls: "red", code: "L2", name: "红色", text: "强验证", policy: "服务端挑战" },
  DENIED: { cls: "denied", code: "L4", name: "拒绝", text: "拒绝并解释", policy: "安全拒绝" },
};

/* 术语表：机器标识 → 中文（与设计仓 GLOSS 同源；标识本身入 .tech，默认收起） */
const GLOSS = {
  rule: {
    QUERY_ONLY: "仅纯查询",
    STATE_CHANGE_SMALL: "小额状态变更",
    AMOUNT_OVER_DAILY_LIMIT: "日累计超限额",
    IRREVERSIBLE: "不可逆操作",
    MANUAL_REVIEW: "转人工复核",
    OBJECT_BLOCKED: "对象被阻断",
    OFFICIAL_RED: "官方点名红色",
  },
  require: {
    user_confirm: "需您确认",
    strong_verify: "需强验证（服务端验证码）",
    manual_takeover: "转人工接管",
    deny_and_explain: "拒绝并说明",
  },
};

const state = {
  card: null,
  decision: null,
  questions: [],
  stepStates: [],
  phase: "idle",
  chain: [],
  verification: null,
  clarifications: [],
};

function el(id) {
  return document.getElementById(id);
}

function esc(text) {
  return String(text).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

/* 金额格式化：整数分（cents）→ "¥1,000.00" */
function yuan(cents) {
  return (
    "¥" +
    (cents / 100).toLocaleString("zh-CN", {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    })
  );
}

/* 当日额度行：仅黄/红卡显示「本人当日限额 / 已用 / 剩余」；超额不出负数，改红色「已超额」。
   绿卡（纯查询）、转人工、拒绝一律不显示——只透结论性数字（min 后的有效阈值），不解释来源。 */
function quotaHtml(card) {
  if (card.level !== "YELLOW" && card.level !== "RED") return "";
  const limit = card.daily_limit_cents;
  const used = card.daily_accumulated_cents;
  if (typeof limit !== "number" || typeof used !== "number") return "";
  const remaining = limit - used;
  const head = `本人当日限额 <b class="quota__val">${yuan(limit)}</b> · 已用 <b class="quota__val">${yuan(used)}</b>`;
  if (remaining < 0) {
    return `<p class="quota quota--over">${head} · 已超额 <b class="quota__val">${yuan(-remaining)}</b></p>`;
  }
  return `<p class="quota">${head} · 剩余 <b class="quota__val">${yuan(remaining)}</b></p>`;
}

function clock() {
  const d = new Date();
  return [d.getHours(), d.getMinutes(), d.getSeconds()].map((n) => String(n).padStart(2, "0")).join(":");
}

function stripNo(line) {
  return String(line).replace(/^\s*\d+\.\s*/, "");
}

/* 会话标识（P2-7 · README 裁决 #27）：**每个页面实例一个新会话**。
   「新开页面 / 刷新 = 新会话」是本演示的恢复语义——裁决 #27 把触发 3/4/5 的熔断降为**会话级**
   （只让本会话转人工接管，不再锁死全局），因此需要这次隔离才能真正生效。 */
const SESSION_ID = (() => {
  try {
    if (window.crypto && crypto.randomUUID) return "web-" + crypto.randomUUID();
  } catch (_) {
    /* 降级到时间戳 + 随机串（老浏览器 / 非安全上下文） */
  }
  return "web-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 10);
})();

async function post(path, body) {
  const payload = Object.assign({ session_id: SESSION_ID }, body || {});
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  return { status: response.status, data: await response.json() };
}

/* ---------------- 右栏：审计链 / 时间线 ---------------- */

function renderChain() {
  const chain = el("chain");
  if (!state.chain.length) {
    chain.innerHTML = '<li class="chain__empty">尚未产生事件</li>';
    return;
  }
  chain.innerHTML = state.chain
    .map((node) => `<li class="chain__node">${esc(node.label)}<span class="tech">${esc(node.tech)}</span></li>`)
    .join("");
}

function chainAdd(label, tech) {
  state.chain.push({ label, tech });
  renderChain();
}

function timelineAdd(text, kind) {
  const list = el("timeline");
  if (list.dataset.empty !== "false") {
    list.innerHTML = "";
    list.dataset.empty = "false";
  }
  const item = document.createElement("li");
  item.className = `timeline__item${kind ? ` timeline__item--${kind}` : ""}`;
  item.innerHTML = `<span class="timeline__at">${clock()}</span>${esc(text)}`;
  list.appendChild(item);
}

function timelineReset() {
  const list = el("timeline");
  list.innerHTML = '<li class="timeline__item">等待操作…</li>';
  list.dataset.empty = "true";
}

/* ---------------- 凭证卡渲染 ---------------- */

function sealHtml(level) {
  const meta = LEVEL[level] || LEVEL.DENIED;
  return `<div class="seal seal--stamped seal--${meta.cls}" style="margin-left:auto">
    <span class="seal__level">${meta.code}</span>
    <span class="seal__text">${meta.text}</span>
    <span class="seal__policy">${meta.policy}</span>
  </div>`;
}

function stepHtml(line, index) {
  const status = state.stepStates[index] || "pending";
  const cls = status === "done" ? "step step--done" : status === "rolled" ? "step step--rolled" : "step step--pending";
  const label = status === "done" ? "已执行" : status === "rolled" ? "已回滚" : "待执行";
  return `<li class="${cls}">
    <span class="step__no">${String(index + 1).padStart(2, "0")}</span>
    <span class="step__main"><span class="step__action">${esc(stripNo(line))}</span></span>
    <span class="step__meta"><span class="step__state">${label}</span></span>
  </li>`;
}

function rulesHtml(reasons) {
  if (!reasons.length) return '<span class="muted">—</span>';
  return reasons
    .map((reason) => {
      const zh = GLOSS.rule[reason];
      const tech = `<span class="tech">${esc(reason)}</span>`;
      return zh ? `<span class="rule"><b>${esc(zh)}</b>${tech}</span>` : tech;
    })
    .join("");
}

function requiresHtml(requires) {
  if (!requires.length) return '<span class="muted">无额外要求</span>';
  return requires
    .map((item) => {
      const zh = GLOSS.require[item] || item;
      return `<span class="chip"><span class="chip__dot"></span>${esc(zh)}<span class="tech">${esc(item)}</span></span>`;
    })
    .join("");
}

function receiptNo(frozen) {
  const now = new Date();
  const stamp = [
    now.getFullYear(),
    String(now.getMonth() + 1).padStart(2, "0"),
    String(now.getDate()).padStart(2, "0"),
  ].join("");
  return `PLAN-${stamp}-${String(frozen).slice(0, 4).toUpperCase()}`;
}

function voucherHtml() {
  const card = state.card;
  const lines = card.lines || [];
  const frozen = card.frozen_hash || "";
  return `<article class="voucher">
    <div class="voucher__head">
      <div class="doc-no">
        <span class="doc-no__label">凭证号</span>
        <span class="doc-no__value">${esc(receiptNo(frozen))}</span>
        <span class="doc-no__label tech" style="margin-top: 6px">计划冻结哈希（cfhash）</span>
        <span class="tech">${esc(frozen)}</span>
      </div>
      ${sealHtml(card.level)}
    </div>

    <div class="voucher__body">
      <dl class="triple">
        <div class="triple__row"><dt class="triple__key">动作</dt>
          <dd class="triple__val">${lines.length} 步计划 <span class="tech">${esc(card.card_id)}</span></dd></div>
        <div class="triple__row"><dt class="triple__key">要求</dt>
          <dd class="triple__val triple__val--tokens">${requiresHtml(card.requires || [])}</dd></div>
        <div class="triple__row"><dt class="triple__key">时间</dt>
          <dd class="triple__val">${clock()}</dd></div>
      </dl>
      <div class="amount">
        <span class="amount__value">${lines.length} 步</span>
        <span class="amount__note">确认绑定 cfhash · 经计划级 + 节点级双层执行门</span>
      </div>
    </div>

    ${quotaHtml(card)}

    ${lines.length ? `<ol class="steps">${lines.map(stepHtml).join("")}</ol>` : ""}

    <p class="muted" style="margin-top: var(--s-3)">
      命中规则<span class="rules">${rulesHtml(state.decision ? state.decision.reasons || [] : [])}</span>
    </p>
  </article>`;
}

function interceptHtml(title, why, lock) {
  const cls = lock ? "intercept intercept--lock" : "intercept";
  return `<div class="${cls}" style="margin-top: var(--s-4)">
    <span class="seal seal--stamped seal--denied" style="width:60px;height:60px;border-width:1.5px">
      <span class="seal__text" style="font-size:10.5px">${lock ? "封条" : "已拦截"}</span>
    </span>
    <div>
      <div class="intercept__title">${esc(title)}</div>
      <div class="intercept__why">${esc(why)}</div>
    </div>
  </div>`;
}

function takeoverHtml(pack) {
  const nodes = pack.plan ? pack.plan.nodes.length : 0;
  return `<div class="takeover" style="margin-top: var(--s-4)">
    <div class="muted">人工接管包 · 交给人工判断的四要素</div>
    <dl class="takeover__grid">
      <dt>意图</dt><dd>${esc(pack.intent_text || "—")}</dd>
      <dt>计划</dt><dd class="mono">计划已冻结 · ${nodes} 个节点 <span class="tech">${pack.plan ? esc(pack.plan.plan_id) : ""}</span></dd>
      <dt>命中规则</dt><dd><span class="rules">${rulesHtml(pack.hit_rules || [])}</span></dd>
      <dt>审计 ID</dt><dd class="mono tech">${esc((pack.audit_ids || []).join(" · ") || "—")}</dd>
    </dl>
    <p class="muted" style="font-size: var(--fs-small); margin:0">
      接管包交付人工判断：意图 / 计划 / 命中规则 / 审计 ID 四要素齐备，人工可据此复核或改判。
    </p>
  </div>`;
}

/* ---------------- 渲染装配 ---------------- */

function renderStage() {
  const stage = el("stage");
  if (state.card) {
    stage.innerHTML = voucherHtml() + (state.phase === "strong-verify" ? verifyHtml() : "");
    return;
  }
  stage.innerHTML = state.panel || "";
}

/* 强验证挑战面板：验证码由服务端下发，一次性 + TTL；演示环境直接展示以便现场输入 */
function verifyHtml() {
  const v = state.verification || {};
  return `<div class="takeover" style="margin-top: var(--s-4)">
    <div class="muted">强验证 · 红色级别须通过服务端挑战后方可执行</div>
    <p class="muted" style="font-size: var(--fs-small); margin: var(--s-3) 0">
      已向 <span class="mono">${esc(v.target || "—")}</span> 下发验证码
      （渠道 ${esc(v.channel || "—")}，${esc(String(v.expires_in_s || "—"))} 秒内有效，一次性）。
      <br />演示环境未接入真实短信网关，验证码在此直接展示：
      <span class="mono">${esc(v.code || "—")}</span>
    </p>
    <input class="textarea" id="vcode" inputmode="numeric" maxlength="6" placeholder="输入 6 位验证码" />
  </div>`;
}

function setButtons() {
  const busy = state.phase === "executing";
  const awaiting = state.phase === "awaiting_confirm";
  const strongPhase = state.phase === "strong-verify";
  el("btn-submit").disabled = busy;
  const redPending = state.card && state.card.level === "RED" && !strongPhase;
  el("btn-confirm").textContent = strongPhase
    ? "提交验证码并执行"
    : redPending
      ? "申请强验证"
      : "确认并执行";
  el("btn-confirm").disabled = !(awaiting || strongPhase) || busy;
  el("btn-interrupt").disabled = !(busy || awaiting);
  el("btn-resume").disabled = state.phase !== "interrupted";
  el("btn-reverse").disabled = state.stepStates.filter((s) => s === "done").length === 0;
  el("btn-takeover").disabled = state.phase === "idle";
}

function reset(keepUtterance) {
  state.card = null;
  state.decision = null;
  state.questions = [];
  state.stepStates = [];
  state.phase = "idle";
  state.chain = [];
  state.panel = "";
  state.verification = null;
  state.clarifications = [];
  if (!keepUtterance) el("utterance").value = "转给李娜 500 元";
  renderStage();
  renderChain();
  timelineReset();
  setButtons();
}

function renderPresets() {
  el("presets").innerHTML = PRESETS.map(
    (text) => `<button class="btn btn--ghost" data-preset="${esc(text)}">${esc(text)}</button>`
  ).join("");
}

/* ---------------- 交互 ---------------- */

/* 把一次提交（/api/submit 或 /api/clarify）的结果落到界面：两条路径共用同一处理 */
function applySubmission(data) {
  if (data.state === "awaiting_confirm" && data.card) {
    state.card = data.card;
    state.decision = data.decision || null;
    state.stepStates = (data.card.lines || []).map(() => "pending");
    state.phase = "awaiting_confirm";
    chainAdd("计划卡已冻结", `cfhash ${String(data.card.frozen_hash).slice(0, 12)}`);
    renderStage();
    timelineAdd(`裁决：${(LEVEL[data.card.level] || {}).name || data.card.level} · 计划卡已冻结（等待确认）`, data.card.level === "RED" ? "warn" : "done");
    return;
  }
  state.phase = data.state === "blocked" ? "blocked" : "clarifying";
  state.clarifications = data.clarifications || [];
  const questions = (data.questions || []).map((q) => `· ${q}`).join("<br />");
  state.panel =
    interceptHtml(
      data.state === "blocked" ? "安全拒绝（未产生可执行计划）" : "需要澄清（不产出级别）",
      data.state === "blocked"
        ? data.message || "未知意图不猜测执行；已留痕。"
        : `${data.message || "请补充信息"}<br />${questions}`,
      false
    ) + clarifyHtml();
  renderStage();
  timelineAdd(data.state === "blocked" ? "安全拒绝 · 已留痕" : "转澄清 · 未产出级别", "stop");
}

/* 澄清闭环表单：按结构化提问逐槽位补值，回答后重跑原指令（无需整句重述） */
function clarifyHtml() {
  const asks = state.clarifications || [];
  if (!asks.length) return "";
  const fields = asks
    .map(
      (ask) =>
        `<label class="field__label" for="ask-${esc(ask.slot)}">${esc(ask.prompt)}</label>` +
        `<input class="textarea" id="ask-${esc(ask.slot)}" data-slot="${esc(ask.slot)}" placeholder="填写 ${esc(ask.slot)}" />`
    )
    .join("");
  return `<div class="takeover" style="margin-top: var(--s-4)">
    <div class="muted">补充后继续 · 在原指令上续答，不必重述</div>
    <div>${fields}</div>
    <div class="btn-row" style="margin-top: var(--s-3)">
      <button class="btn btn--primary" id="btn-clarify">补充并继续</button>
    </div>
  </div>`;
}

async function onClarify() {
  const answers = {};
  document.querySelectorAll("[data-slot]").forEach((node) => {
    const value = (node.value || "").trim();
    if (value) answers[node.dataset.slot] = value;
  });
  const result = await post("/api/clarify", { answers });
  if (result.status !== 200) {
    timelineAdd(`补充被拒绝：${result.data.detail || "当前无待澄清指令"}`, "stop");
    return;
  }
  timelineAdd(`已补充并继续：${Object.keys(answers).join("、") || "（未新增）"}`, "live");
  applySubmission(result.data);
  setButtons();
}

async function onSubmit() {
  reset(true);
  const text = el("utterance").value;
  state.phase = "submitting";
  setButtons();
  const result = await post("/api/submit", { utterance: text, selected: SELECTED });
  timelineAdd(`提交：${text}`, "live");
  applySubmission(result.data);
  setButtons();
}

/* 申请服务端强验证挑战（红色卡）：验证码一次性 + 有 TTL，与当前冻结计划绑定 */
async function requestVerification() {
  const result = await post("/api/verify/request", {});
  if (result.status !== 200) {
    timelineAdd(`强验证申请被拒绝：${result.data.detail || "当前计划卡无需强验证"}`, "stop");
    return;
  }
  state.verification = result.data;
  state.phase = "strong-verify";
  renderStage();
  timelineAdd(`红色：已下发强验证挑战（${result.data.channel} → ${result.data.target}）`, "warn");
  setButtons();
}

async function onConfirm() {
  if (!state.card) return;
  if (state.card.level === "RED" && state.phase !== "strong-verify") {
    await requestVerification();
    return;
  }
  const codeEl = el("vcode");
  const code = state.phase === "strong-verify" && codeEl ? codeEl.value.trim() : "";
  state.phase = "executing";
  setButtons();
  const payload = { card_id: state.card.card_id, frozen_hash: state.card.frozen_hash };
  if (code) payload.verification_code = code;
  const result = await post("/api/confirm", payload);
  if (result.status !== 200) {
    state.phase = state.verification ? "strong-verify" : "awaiting_confirm";
    state.panel = interceptHtml("确认被拒绝", String(result.data.detail || "确认不通过"), false);
    el("stage").insertAdjacentHTML("beforeend", state.panel);
    timelineAdd(`确认被拒绝：${result.data.detail || ""}`, "stop");
    setButtons();
    return;
  }
  const data = result.data;
  (data.results || []).forEach((item, index) => {
    if (state.stepStates[index] !== undefined) state.stepStates[index] = "done";
    if (item.record_ids && item.record_ids.length) chainAdd("已执行", item.record_ids[0]);
    timelineAdd(`${item.status}：${item.message}`, item.status === "settled" || item.status === "cancelled" ? "done" : "warn");
  });
  state.phase = "done";
  renderStage();
  timelineAdd(`执行完成 · 回执记录 ${(data.record_ids || []).length} 条`, "done");
  setButtons();
}

async function onInterrupt() {
  const result = await post("/api/interrupt", {});
  if (result.status !== 200) {
    timelineAdd(`中断被拒绝：${result.data.detail || "当前状态不可中断"}`, "stop");
    return;
  }
  state.phase = "interrupted";
  timelineAdd("用户中断：已执行节点保留、未执行节点挂起", "warn");
  renderStage();
  setButtons();
}

async function onResume() {
  const result = await post("/api/resume", {});
  const data = result.data;
  (data.results || []).forEach((item, index) => {
    if (state.stepStates[index] !== undefined && state.stepStates[index] !== "done") {
      state.stepStates[index] = "done";
    }
    if (item.record_ids && item.record_ids.length) chainAdd("已执行", item.record_ids[0]);
    timelineAdd(`${item.status}：${item.message}`, "done");
  });
  state.phase = "done";
  renderStage();
  timelineAdd("恢复执行：从未执行节点继续", "done");
  setButtons();
}

async function onReverse() {
  const result = await post("/api/reverse", {});
  const data = result.data;
  if (result.status !== 200) {
    timelineAdd(`回退被拒绝：${data.detail || "当前状态不可回退"}`, "stop");
    return;
  }
  const rolled = state.stepStates.lastIndexOf("done");
  if (rolled >= 0) state.stepStates[rolled] = "rolled";
  state.phase = "done";
  renderStage();
  timelineAdd(`${data.message || "已回退"} · 回滚 ${(data.compensated_steps || []).length} 步`, "stop");
  setButtons();
}

async function onTakeover() {
  const response = await fetch("/api/takeover?session_id=" + encodeURIComponent(SESSION_ID));
  if (response.status !== 200) {
    timelineAdd("当前状态无接管包", "warn");
    return;
  }
  const pack = await response.json();
  el("stage").insertAdjacentHTML("beforeend", takeoverHtml(pack));
  timelineAdd("已导出人工接管包（意图 / 计划 / 命中规则 / 审计 ID）", "warn");
}

/* ---------------- 装配 ---------------- */

renderPresets();
el("presets").addEventListener("click", (event) => {
  const button = event.target.closest("[data-preset]");
  if (button) el("utterance").value = button.dataset.preset;
});
el("btn-submit").addEventListener("click", onSubmit);
el("btn-confirm").addEventListener("click", onConfirm);
el("btn-interrupt").addEventListener("click", onInterrupt);
el("btn-resume").addEventListener("click", onResume);
el("btn-reverse").addEventListener("click", onReverse);
el("btn-takeover").addEventListener("click", onTakeover);
el("btn-reset").addEventListener("click", () => reset(false));
/* 澄清表单是动态渲染的，故用事件委托 */
el("stage").addEventListener("click", (event) => {
  if (event.target && event.target.id === "btn-clarify") onClarify();
});

function readTheme() {
  try {
    return localStorage.getItem("ab-theme");
  } catch (error) {
    return null;
  }
}

function saveTheme(theme) {
  try {
    localStorage.setItem("ab-theme", theme);
  } catch (error) {
    return;
  }
}

const themeToggle = el("theme-toggle");
if (readTheme() === "dark") {
  document.documentElement.dataset.theme = "dark";
  themeToggle.checked = true;
}
themeToggle.addEventListener("change", () => {
  const theme = themeToggle.checked ? "dark" : "light";
  document.documentElement.dataset.theme = theme;
  saveTheme(theme);
});

/* 技术详情：默认收起机器标识（cfhash / 记录号 / 审计 ID / 动作标识） */
const techToggle = el("tech-toggle");
if (techToggle) {
  techToggle.addEventListener("change", () => {
    if (techToggle.checked) {
      document.documentElement.dataset.tech = "on";
    } else {
      delete document.documentElement.dataset.tech;
    }
  });
}

timelineReset();
setButtons();

/* ==========================================================================
   AI 银行副驾 · Web 渠道交互（**真实 API**）
   版式与组件词汇对齐设计仓 `demo.html` / `app.css`（凭证 / 戳记 / 步骤 / 链 / 时间线）。
   与设计仓 `demo.js` 的区别：本文件不做任何 mock——所有数据来自同源 `/api/*`。
   ========================================================================== */

const PRESETS = [
  "转给李娜 500 元",
  "转给李娜 1200 元，再查一下上月账单",
  "查一下上月账单",
  "查余额",
  "代扣签约",
  "解冻资金",
  "登记预约转账 给李娜 100 元 今天",
  "冻结 500 元，转给李娜 300 元",
  "转给李娜 500 元，取消订阅",
  "订阅取消",
  "锁卡",
  "挂失卡片",
  "帮我做点什么",
];

const OBJECT_OPTIONS = {
  account: { key: "accounts", label: "账户" },
  card: { key: "cards", label: "卡片" },
  subscription: { key: "subscriptions", label: "订阅" },
  hold: { key: "holds", label: "冻结记录" },
};

const SLOT_LABELS = {
  account: "账户", card: "卡片", subscription: "订阅", hold: "冻结记录",
  payee: "收款人", payer: "付款人", amount: "金额", amount_cents: "金额",
  daily_limit: "日限额", when: "执行日期", next_charge_on: "下次扣费日期",
  period: "扣费周期", merchant: "商户名称", product: "理财产品",
};
const MONEY_SLOTS = new Set(["amount", "amount_cents", "daily_limit"]);

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
  options: null,
  busy: false,
  notice: "",
  resultCount: 0,
  task: null,
  response: null,
  taskCount: 0,
  pendingStart: null,
  requestLabel: "",
  operationUnknown: false,
  insights: [],
  offline: typeof navigator !== "undefined" && navigator.onLine === false,
  model: null,
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
function newSessionId() {
  try {
    if (window.crypto && crypto.randomUUID) return "web-" + crypto.randomUUID();
  } catch (_) {
    /* 降级到时间戳 + 随机串（老浏览器 / 非安全上下文） */
  }
  return "web-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 10);
}
let SESSION_ID = newSessionId();

async function post(path, body) {
  if (state.offline || (typeof navigator !== "undefined" && navigator.onLine === false)) {
    throw new Error("当前离线，已禁用需要在线确认的操作");
  }
  state.requestLabel = {
    "/api/submit": "正在请求生成办理计划…", "/api/clarify": "正在提交补充信息…",
    "/api/due": "正在读取到期待办…", "/api/verify/request": "正在申请强验证…",
    "/api/confirm": "正在请求执行，请等待服务端回执…", "/api/interrupt": "正在请求中断…",
    "/api/resume": "正在请求恢复并核对执行结果…", "/api/reverse": "正在请求回退已执行步骤…",
  }[path] || "正在请求服务端…";
  showRequestStatus();
  const payload = Object.assign({ session_id: SESSION_ID }, body || {});
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  state.operationUnknown = false;
  return { status: response.status, data: await response.json() };
}

function optionLabel(item) {
  const names = {
    savings: "储蓄", current: "活期", debit: "借记", credit: "信用",
    active: "有效", cancelled: "已取消", locked: "已锁定", lost: "已挂失",
  };
  return String(item.label).replace(/\b(savings|current|debit|credit|active|cancelled|locked|lost)\b/g,
    (name) => names[name]);
}

function optionsHtml(slot, selected = "") {
  const spec = OBJECT_OPTIONS[slot];
  const records = state.options ? state.options[spec.key] || [] : [];
  const placeholder = records.length ? `请选择${spec.label}` : `暂无可用${spec.label}`;
  return `<option value="">${placeholder}</option>` + records.map((item) =>
    `<option value="${esc(item.id)}"${item.id === selected ? " selected" : ""}>${esc(optionLabel(item))}</option>`
  ).join("");
}

async function refreshOptions() {
  const response = await fetch("/api/options", { cache: "no-store" });
  if (!response.ok) throw new Error("无法读取可选对象");
  state.options = await response.json();
  el("business-date").textContent = `演示业务日期：${state.options.today} · 使用模拟资金和演示商户订单`;
  Object.keys(OBJECT_OPTIONS).forEach((slot) => {
    const select = el(`select-${slot}`);
    select.innerHTML = optionsHtml(slot, select.value);
  });
  setButtons();
}

function insightRuleLabel(ruleId) {
  return {
    "subscription-renewal": "订阅续费提醒",
    "large-expense-review": "大额待核对",
    "monthly-budget": "月预算提醒",
  }[ruleId] || ruleId;
}

function renderInsights(data) {
  const payload = data || {};
  state.insights = Array.isArray(payload) ? payload : payload.insights || [];
  const status = el("insights-status");
  const list = el("insights-list");
  if (payload.scan_error) {
    status.textContent = `扫描失败（${payload.scanned_at || "未知时间"}）`;
    list.innerHTML = `<p class="muted">${esc(payload.scan_error)}；未修改既有洞察。</p>`;
    return;
  }
  status.textContent = `最近扫描：${payload.scanned_at || "服务端时间"}`;
  if (!state.insights.length) {
    list.innerHTML = '<p class="muted">当前没有待处理洞察。</p>';
    return;
  }
  list.innerHTML = state.insights.map((item) => {
    const refs = (item.source_refs || []).map((source) =>
      `<li class="mono"><span class="tech">${esc(source.record_id)} · ${esc(source.content_hash)}</span></li>`
    ).join("");
    const value = item.value === undefined ? "—" : JSON.stringify(item.value);
    return `<article class="takeover insight-item">
      <div><b>${esc(insightRuleLabel(item.rule_id))}</b>` +
      ` <span class="chip">${esc(item.status === "needs_setup" ? "待设置" : "待查看")}</span></div>
      <p>${esc(item.suggestion || "请查看来源后自行决定")}</p>
      <p class="muted">业务日 ${esc(item.business_day || "—")} · 窗口 ${esc(item.window_start || "—")} 至 ${esc(item.window_end || "—")}</p>
      <p class="muted">计算式：<span class="mono">${esc(item.formula || "—")}</span> · 值：<span class="mono">${esc(value)}</span></p>
      ${refs ? `<details><summary>来源（${(item.source_refs || []).length}）</summary><ul>${refs}</ul></details>` : "<p class=\"muted\">暂无来源记录（预算尚未设置）。</p>"}
    </article>`;
  }).join("");
}

async function refreshInsights() {
  if (state.offline || (typeof navigator !== "undefined" && navigator.onLine === false)) return;
  const response = await fetch("/api/insights", { cache: "no-store" });
  if (response.status === 404) {
    el("insights-status").textContent = "主动洞察服务尚未接入";
    return;
  }
  if (!response.ok) throw new Error("无法读取主动洞察");
  renderInsights(await response.json());
}

function selectedObjects() {
  const selected = {};
  Object.keys(OBJECT_OPTIONS).forEach((slot) => {
    const value = el(`select-${slot}`).value;
    if (value) selected[slot] = value;
  });
  return selected;
}

function clearObjectSelections() {
  Object.keys(OBJECT_OPTIONS).forEach((slot) => {
    el(`select-${slot}`).value = "";
  });
}

async function refreshAfterExecution() {
  try {
    await refreshOptions();
  } catch (_) {
    timelineAdd("业务结果已返回，对象列表刷新失败；请重新加载页面后核对。", "warn");
  }
}

function action(handler) {
  return async () => {
    if (state.busy) return;
    state.busy = true;
    const dangerous = ["onInterrupt", "onResume", "onReverse"].includes(handler.name);
    setButtons();
    try {
      await handler();
    } catch (error) {
      if (String(error).includes("fetch") || String(error).includes("离线")) {
        setOfflineState(true);
      }
      if (dangerous) {
        state.operationUnknown = true;
        state.notice = interceptHtml("请求结果未知", "未收到服务端回执。请先查看服务端状态或导出人工接管包，不要重复提交该操作。", false);
      } else {
        state.phase = state.card ? (state.verification ? "strong-verify" : "awaiting_confirm") : "failed";
        state.notice = interceptHtml("请求未能完成", "未收到服务端结果，请先核对当前办理。执行请求可用同一计划再次确认核对；不要重复创建相同资金操作。", false);
      }
      state.notice += '<button class="btn" id="btn-refresh-session" type="button">读取服务端状态</button>';
      renderStage();
      timelineAdd("请求未能完成，请重新查询业务状态。", "stop");
    } finally {
      state.busy = false;
      state.requestLabel = "";
      showRequestStatus();
      setButtons();
    }
  };
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

/* 只重绘最新回复；历史记录保存真实 DOM 快照，不再带授权控件。 */
function renderStage() {
  if (!state.response) return;
  const code = el("vcode") ? el("vcode").value : "";
  state.response.innerHTML = state.card
    ? voucherHtml() + (state.phase === "strong-verify" ? verifyHtml() : "") + state.notice
    : (state.panel || "") + state.notice;
  state.response.innerHTML += modelMetadataHtml();
  if (el("vcode")) el("vcode").value = code;
  if (state.task) state.task.appendChild(el("task-actions"));
  showRequestStatus();
}

function showRequestStatus() {
  let status = el("request-status");
  if (!state.requestLabel || !state.task) {
    if (status) status.remove();
    return;
  }
  if (!status) {
    status = document.createElement("p");
    status.id = "request-status";
    status.className = "request-status";
    status.setAttribute("role", "status");
    state.task.appendChild(status);
  }
  status.textContent = state.requestLabel;
}

function showModelMetadata(metadata) {
  state.model = metadata || null;
}

function modelMetadataHtml() {
  const metadata = state.model;
  if (!metadata) return "";
  const channels = {
    "template": "确定性模板", "bypass": "旁路诊断", "llm-strict": "真实模型严格通道",
    "template_fallback": "模板回退（真实模型不可用）", "injected": "测试注入通道",
  };
  const name = (channel) => channels[channel] || "未调用";
  const tokens = metadata.usage && metadata.usage.total_tokens;
  return `<p class="muted">意图：${esc(name(metadata.planner_channel))} · 审查：${esc(name(metadata.reviewer_channel))}
    <br />本任务真实 HTTP ${esc(String(metadata.http_attempts || 0))} 次 · 供应商 tokens ${esc(tokens == null ? "未提供" : String(tokens))}</p>`;
}

function appendMessage(role, text) {
  const message = document.createElement("div");
  message.className = `chat-message chat-message--${role}`;
  const by = document.createElement("span");
  by.className = "message-by";
  by.textContent = role === "user" ? "您" : "银行副驾";
  const body = document.createElement("div");
  body.className = "message-body";
  if (text) body.textContent = text;
  message.appendChild(by);
  message.appendChild(body);
  state.task.appendChild(message);
  return body;
}

function makeReadOnly(root) {
  root.querySelectorAll("input,select,textarea,button").forEach((control) => {
    const text = document.createElement("span");
    text.className = "history-value";
    text.textContent = control.tagName === "SELECT"
      ? (control.selectedOptions[0] ? control.selectedOptions[0].textContent : "未选择")
      : control.tagName === "BUTTON" ? "" : control.value || "未填写";
    control.replaceWith(text);
  });
  [root, ...root.querySelectorAll("*")].forEach((node) => {
    ["id", "for", "data-slot", "data-preset", "aria-controls"].forEach((name) => node.removeAttribute(name));
  });
}

function advanceResponse(answer) {
  if (state.response) makeReadOnly(state.response);
  if (answer) appendMessage("user", answer);
  state.response = appendMessage("assistant");
  state.response.id = "active-response";
  state.panel = "";
  state.notice = "";
}

function archiveTask(abandoned = false) {
  if (!state.task) return;
  el("actions-parking").appendChild(el("task-actions"));
  if (abandoned) {
    const note = document.createElement("p");
    note.className = "archive-note";
    note.textContent = "本次办理已结束：放弃未执行部分，已执行结果保留；此处为只读记录。";
    state.task.appendChild(note);
  }
  makeReadOnly(state.task);
  state.task.classList.add("chat-task--archived");
  state.task = null;
  state.response = null;
}

function hasUnfinishedTask() {
  return Boolean(state.task && !["idle", "done", "failed", "blocked", "reversed", "taken_over"].includes(state.phase));
}

function requestStart(start) {
  if (hasUnfinishedTask()) {
    state.pendingStart = start;
    el("task-guard-title").textContent = hasUnfinishedTask() ? "当前办理尚未结束" : "请先核对当前办理结果";
    el("task-guard").hidden = false;
    el("btn-continue-task").focus();
    return false;
  }
  return true;
}

function beginTask(text, selected = {}) {
  archiveTask();
  reset(true);
  SESSION_ID = newSessionId();
  const welcome = el("conversation-welcome");
  if (welcome) welcome.hidden = true;
  const task = document.createElement("section");
  task.className = "chat-task";
  task.dataset.session = SESSION_ID;
  task.setAttribute("aria-label", `办理 ${++state.taskCount}：${text}`);
  state.task = task;
  el("stage").appendChild(task);
  const record = document.createElement("button");
  record.type = "button";
  record.className = "conversation-record";
  record.textContent = text;
  record.addEventListener("click", () => task.scrollIntoView({ behavior: "smooth", block: "start" }));
  if (state.taskCount === 1) el("conversation-list").innerHTML = "";
  el("conversation-list").prepend(record);
  appendMessage("user", text);
  if (Object.keys(selected).length) {
    const objects = Object.keys(selected).map((slot) => {
      const node = el(`select-${slot}`);
      return node.selectedOptions[0] ? node.selectedOptions[0].textContent : selected[slot];
    });
    appendMessage("user", `本次办理对象：${objects.join("；")}`);
  }
  clearObjectSelections();
  advanceResponse();
  task.appendChild(el("task-actions"));
  setMobileDrawer("scenes", false);
}

async function startFreshConversation() {
  if (!requestStart(startFreshConversation)) return;
  archiveTask();
  reset(false);
  SESSION_ID = newSessionId();
  clearObjectSelections();
  el("utterance").focus();
  setMobileDrawer("scenes", false);
}

/* 强验证挑战面板：验证码由服务端下发，一次性 + TTL；演示环境直接展示以便现场输入 */
function verifyHtml() {
  const v = state.verification || {};
  const delivery = v.mode === "demo"
    ? `演示模式未发送真实短信，演示码：<span class="mono">${esc(v.code || "—")}</span>`
    : `验证提供方已接受投递，请从配置的第二信道读取验证码。`;
  return `<div class="takeover" style="margin-top: var(--s-4)">
    <div class="muted">强验证 · 红色级别须通过服务端挑战后方可执行</div>
    <p class="muted" style="font-size: var(--fs-small); margin: var(--s-3) 0">
      验证目标 <span class="mono">${esc(v.target || "—")}</span>
      （渠道 ${esc(v.channel || "—")}，${esc(String(v.expires_in_s || "—"))} 秒内有效，一次性）。
      <br />${delivery}
    </p>
    <input class="textarea" id="vcode" inputmode="numeric" maxlength="6" placeholder="输入 6 位验证码" />
    <button class="btn" id="btn-reverify" type="button">重新申请</button>
  </div>`;
}

function setOfflineState(offline) {
  state.offline = offline;
  const marker = el("offline-state");
  if (marker) marker.hidden = !offline;
  if (offline) timelineAdd("当前离线：提交、确认、挑战和其他写操作已禁用。", "warn");
  setButtons();
}

function setButtons() {
  const busy = state.busy || state.phase === "executing";
  const offline = state.offline || (typeof navigator !== "undefined" && navigator.onLine === false);
  const awaiting = state.phase === "awaiting_confirm";
  const strongPhase = state.phase === "strong-verify";
  const operationUnknown = state.operationUnknown;
  el("btn-submit").disabled = offline || busy || !state.options;
  el("btn-due").disabled = offline || busy || !state.options;
  el("btn-reset").disabled = busy;
  el("task-actions").hidden = !state.task || (!state.card && state.phase !== "blocked");
  Object.keys(OBJECT_OPTIONS).forEach((slot) => {
    el(`select-${slot}`).disabled = offline || busy || !state.options;
  });
  const clarifyButton = el("btn-clarify");
  if (clarifyButton) clarifyButton.disabled = offline || busy;
  const redPending = state.card && state.card.level === "RED" && !strongPhase;
  el("btn-confirm").textContent = strongPhase
    ? "提交验证码并执行"
    : redPending
      ? "申请强验证"
      : "确认并执行";
  el("btn-confirm").disabled = operationUnknown || offline || !(awaiting || strongPhase) || busy;
  el("btn-interrupt").disabled = operationUnknown || offline || state.busy || !(state.phase === "executing" || awaiting);
  el("btn-resume").disabled = operationUnknown || offline || busy || !state.card || state.phase !== "interrupted";
  el("btn-reverse").disabled = operationUnknown || offline || busy || state.phase === "reversed" || !state.card || state.card.level === "GREEN" || state.stepStates.filter((s) => s === "done").length === 0;
  el("btn-takeover").disabled = offline || busy || state.phase === "idle";
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
  state.notice = "";
  state.resultCount = 0;
  state.operationUnknown = false;
  state.model = null;
  if (!keepUtterance) el("utterance").value = "";
  renderStage();
  renderChain();
  timelineReset();
  setButtons();
}

function renderPresets() {
  const html = PRESETS.map(
    (text) => `<button class="btn btn--ghost" data-preset="${esc(text)}">${esc(text)}</button>`
  ).join("");
  ["quick-scenes", "presets"].forEach((id) => {
    const container = el(id);
    if (container) container.innerHTML = id === "presets" ? PRESETS.slice(0, 4).map((text) => `<button class="btn" data-preset="${esc(text)}">${esc(text)}</button>`).join("") : html;
  });
}

/* ---------------- 交互 ---------------- */

/* 把一次提交（/api/submit 或 /api/clarify）的结果落到界面：两条路径共用同一处理 */
async function applySubmission(data) {
  showModelMetadata(data.model);
  if (data.state === "awaiting_confirm" && data.card) {
    state.card = data.card;
    state.decision = data.decision || null;
    state.stepStates = (data.card.lines || []).map(() => "pending");
    state.phase = "awaiting_confirm";
    chainAdd("计划卡已冻结", `cfhash ${String(data.card.frozen_hash).slice(0, 12)}`);
    renderStage();
    const next = data.card.level === "GREEN" ? "自动执行查询" : "等待确认";
    timelineAdd(`裁决：${(LEVEL[data.card.level] || {}).name || data.card.level} · 计划卡已冻结（${next}）`, data.card.level === "RED" ? "warn" : "done");
    if (data.card.level === "GREEN") await onConfirm();
    return;
  }
  if (data.state === "idle") {
    state.phase = "idle";
    state.panel = `<div class="takeover"><p>${esc(data.message || "当前没有到期待办。")}</p></div>`;
    renderStage();
    timelineAdd(data.message || "当前没有到期待办。", "done");
    return;
  }
  state.phase = data.state === "blocked" ? "blocked" : "clarifying";
  state.clarifications = data.clarifications || [];
  state.panel = data.state === "blocked"
    ? interceptHtml("安全拒绝（未产生可执行计划）", data.message || "未知意图不猜测执行；已留痕。", false)
    : clarifyHtml() || `<div class="takeover">请补充所需信息后重新提交。</div>`;
  renderStage();
  timelineAdd(data.state === "blocked" ? "安全拒绝 · 已留痕" : "转澄清 · 未产出级别", "stop");
}

/* 澄清闭环表单：按结构化提问逐槽位补值，回答后重跑原指令（无需整句重述） */
function clarifyHtml() {
  const asks = (state.clarifications || []).filter((ask) => ask.slot !== "user_id");
  if (!asks.length) return "";
  const fields = asks
    .map(
      (ask) =>
        `<div class="field"><label class="field__label" for="ask-${esc(ask.slot)}">${esc(clarifyPrompt(ask))}</label>` +
        clarifyControl(ask) + `</div>`
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

function baseSlot(slot) {
  return slot.replace(/^intent_\d+_/, "");
}

function clarifyPrompt(ask) {
  const slot = baseSlot(ask.slot);
  if (MONEY_SLOTS.has(slot)) return `${ask.prompt}（金额单位：元）`;
  if (slot === "period") return "请选择扣费周期。";
  return ask.prompt;
}

function clarifyControl(ask) {
  const slot = baseSlot(ask.slot);
  const attrs = `class="input" id="ask-${esc(ask.slot)}" data-slot="${esc(ask.slot)}" required`;
  if (OBJECT_OPTIONS[slot]) return `<select ${attrs}>${optionsHtml(slot)}</select>`;
  if (slot === "period") {
    return `<select ${attrs}><option value="">请选择扣费周期</option>` +
      `<option value="monthly">每月</option><option value="yearly">每年</option></select>`;
  }
  if (slot === "when" || slot === "next_charge_on") return `<input ${attrs} type="date" />`;
  if (MONEY_SLOTS.has(slot)) {
    return `<input ${attrs} type="number" min="0.01" step="0.01" inputmode="decimal" placeholder="金额单位：元，例如 25" />` +
      `<span class="muted">${esc(SLOT_LABELS[slot])}单位为元，例如输入 25 表示 25 元。</span>`;
  }
  const label = SLOT_LABELS[slot] || "所需信息";
  return `<input ${attrs} type="text" placeholder="请填写${esc(label)}" />`;
}

async function onClarify() {
  const answers = {};
  const inputs = Array.from(state.response.querySelectorAll("[data-slot]"));
  if (!inputs.every((node) => node.reportValidity())) return;
  inputs.forEach((node) => {
    const value = (node.value || "").trim();
    if (value) answers[node.dataset.slot] = MONEY_SLOTS.has(baseSlot(node.dataset.slot)) ? `${value}元` : value;
  });
  const answerText = inputs.map((node) => {
    const value = node.tagName === "SELECT" && node.selectedOptions[0] ? node.selectedOptions[0].textContent : node.value;
    const slot = baseSlot(node.dataset.slot);
    return `${SLOT_LABELS[slot] || "补充信息"}：${value}${MONEY_SLOTS.has(slot) ? " 元" : ""}`;
  }).join("；");
  const result = await post("/api/clarify", { answers });
  if (result.status !== 200) {
    state.notice = interceptHtml("补充被拒绝", result.data.detail || "当前无待澄清指令", false);
    renderStage();
    timelineAdd(`补充被拒绝：${result.data.detail || "当前无待澄清指令"}`, "stop");
    return;
  }
  timelineAdd(`已补充并继续：${Object.keys(answers).map((slot) => SLOT_LABELS[slot] || "所需信息").join("、") || "（未新增）"}`, "live");
  advanceResponse(answerText);
  await applySubmission(result.data);
  setButtons();
}

async function onSubmit() {
  const text = el("utterance").value.trim();
  if (!text) { el("utterance").focus(); return; }
  const selected = selectedObjects();
  const start = () => submitTask(text, selected);
  if (!requestStart(start)) return;
  await start();
}

async function submitTask(text, selected) {
  beginTask(text, selected);
  el("utterance").value = "";
  state.phase = "submitting";
  const result = await post("/api/submit", { utterance: text, selected });
  timelineAdd(`提交：${text}`, "live");
  if (result.status !== 200) {
    state.phase = "failed";
    state.notice = interceptHtml("提交未完成", result.data.detail || "请检查输入后重试", false);
    renderStage();
    return;
  }
  await applySubmission(result.data);
}

async function onDue() {
  const start = async () => {
    beginTask("检查到期待办");
    const result = await post("/api/due", {});
    if (result.status !== 200) {
      state.phase = "failed";
      state.notice = interceptHtml("待办检查未完成", result.data.detail || "请稍后重试", false);
      renderStage();
      return;
    }
    timelineAdd("已检查到期待办，需确认后才会执行。", "live");
    await applySubmission(result.data);
  };
  if (!requestStart(start)) return;
  await start();
}

/* 申请服务端强验证挑战（红色卡）：验证码一次性 + 有 TTL，与当前冻结计划绑定 */
async function requestVerification() {
  const result = await post("/api/verify/request", {});
  if (result.status !== 200) {
    state.notice = interceptHtml("强验证申请被拒绝", result.data.detail || "当前计划卡无需强验证", false);
    renderStage();
    timelineAdd(`强验证申请被拒绝：${result.data.detail || "当前计划卡无需强验证"}`, "stop");
    return;
  }
  state.notice = "";
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
    state.notice = interceptHtml("确认被拒绝", String(result.data.detail || "确认不通过"), false);
    renderStage();
    timelineAdd(`确认被拒绝：${result.data.detail || ""}`, "stop");
    setButtons();
    return;
  }
  applyExecution(result.data);
  await refreshAfterExecution();
}

function billAdviceHtml(items) {
  const advice = Array.isArray(items) ? items.slice(0, 3) : [];
  if (!advice.length) return "";
  return `<section class="takeover" style="margin-top: var(--s-4)">
    <h3>消费建议</h3>
    ${advice.map((item) => {
      const refs = (item.source_refs || []).map((source) => {
        const recordId = String(source.record_id || "");
        const label = recordId.startsWith("bills:")
          ? `月度账单（${recordId.split(":").pop()}）`
          : recordId.startsWith("subscriptions:")
            ? "订阅记录"
            : recordId.startsWith("transactions:") ? "交易流水" : "事实记录";
        return `<li>${esc(label)} <span class="tech mono">记录编号：${esc(recordId)} · 内容校验：${esc(source.content_hash || "—")}</span></li>`;
      }).join("");
      return `<article>
        <p><b>${esc(item.title)}</b> <span class="chip">账期 ${esc(item.period)}</span></p>
        <p><span class="muted">事实依据：</span>${esc(item.evidence)}</p>
        <p><span class="muted">建议：</span>${esc(item.suggestion)}</p>
        ${refs ? `<details><summary>查看来源（${item.source_refs.length}）</summary><ul>${refs}</ul></details>` : '<p class="muted">暂无来源记录。</p>'}
      </article>`;
    }).join("")}
  </section>`;
}

function applyExecution(data) {
  showModelMetadata(data.model);
  if (data.card) state.card = data.card;
  if (data.decision) state.decision = data.decision;
  if (data.state === "awaiting_confirm") state.verification = null;
  const results = data.results || [];
  results.forEach((item, index) => {
    if (state.stepStates[index] !== undefined) state.stepStates[index] = "done";
    if (index < state.resultCount) return;
    if (item.record_ids && item.record_ids.length) chainAdd("已执行", item.record_ids[0]);
    timelineAdd(item.message, "done");
  });
  state.resultCount = results.length;
  state.phase = data.state === "completed" ? "done" : data.state;
  const complete = data.state === "completed";
  state.notice = complete
    ? `<div class="takeover"><p>${esc(data.message || "计划执行完成")}</p>` +
      results.map((item) => `<p>${esc(item.message)}</p>`).join("") + `</div>` +
      billAdviceHtml(data.bill_advice) +
      (data.bill_advice_message ? `<p class="muted">${esc(data.bill_advice_message)}</p>` : "")
    : interceptHtml("执行尚未完成", data.message || "请核对已执行步骤。", false);
  renderStage();
  timelineAdd(data.message || (complete ? "执行完成" : "执行尚未完成"), complete ? "done" : "stop");
  setButtons();
}

async function onInterrupt() {
  const result = await post("/api/interrupt", {});
  if (result.status !== 200) {
    state.notice = interceptHtml("中断被拒绝", result.data.detail || "当前状态不可中断", false);
    renderStage();
    timelineAdd(`中断被拒绝：${result.data.detail || "当前状态不可中断"}`, "stop");
    return;
  }
  state.phase = "interrupted";
  state.notice = `<div class="takeover"><p>${esc(result.data.message || "已中断：已执行节点保留、未执行节点挂起")}</p></div>`;
  timelineAdd("用户中断：已执行节点保留、未执行节点挂起", "warn");
  renderStage();
  setButtons();
}

async function onResume() {
  const result = await post("/api/resume", {});
  if (result.status !== 200) {
    await refreshSession();
    state.notice = interceptHtml("恢复被拒绝", result.data.detail || "当前状态不可恢复", false);
    renderStage();
    timelineAdd(`恢复被拒绝：${result.data.detail || "当前状态不可恢复"}`, "stop");
    return;
  }
  applyExecution(result.data);
  await refreshAfterExecution();
}

async function refreshSession() {
  const response = await fetch("/api/session?session_id=" + encodeURIComponent(SESSION_ID), { cache: "no-store" });
  const data = await response.json();
  if (!response.ok) throw new Error("服务端状态读取失败");
  state.operationUnknown = false;
  state.verification = null;
  applyExecution(data);
  timelineAdd(data.message || "已读取服务端状态，未执行操作。", "live");
  setButtons();
}

async function onReverse() {
  const result = await post("/api/reverse", {});
  const data = result.data;
  if (result.status !== 200) {
    state.notice = interceptHtml("回退被拒绝", data.detail || "当前状态不可回退", false);
    renderStage();
    timelineAdd(`回退被拒绝：${data.detail || "当前状态不可回退"}`, "stop");
    return;
  }
  const reversed = data.state === "reversed";
  state.phase = reversed ? "reversed" : "failed";
  if (reversed) state.stepStates = state.stepStates.map((step) => step === "done" ? "rolled" : step);
  state.notice = reversed
    ? `<div class="takeover"><p>${esc(data.message || "已完成回退")}</p></div>`
    : interceptHtml("回退未完成", data.message || "服务端未确认回退完成，请核对已执行结果。", false);
  renderStage();
  timelineAdd(data.message || (reversed ? "已完成回退" : "回退未完成"), reversed ? "done" : "stop");
  await refreshAfterExecution();
  setButtons();
}

async function onTakeover() {
  if (state.offline || (typeof navigator !== "undefined" && navigator.onLine === false)) return;
  const response = await fetch("/api/takeover?session_id=" + encodeURIComponent(SESSION_ID), {
    cache: "no-store",
  });
  if (response.status !== 200) {
    state.notice = interceptHtml("接管包读取失败", "当前状态无接管包，请核对当前办理。", false);
    renderStage();
    timelineAdd("当前状态无接管包", "warn");
    return;
  }
  const pack = await response.json();
  state.notice += takeoverHtml(pack);
  renderStage();
  timelineAdd("已导出人工接管包（意图 / 计划 / 命中规则 / 审计 ID）", "warn");
}

/* ---------------- 装配 ---------------- */

renderPresets();
function onPresetClick(event) {
  const button = event.target.closest("[data-preset]");
  const utterance = el("utterance");
  if (button && utterance) utterance.value = button.dataset.preset;
}
["quick-scenes", "presets"].forEach((id) => {
  const container = el(id);
  if (container) container.addEventListener("click", onPresetClick);
});
el("btn-submit").addEventListener("click", action(onSubmit));
el("btn-due").addEventListener("click", action(onDue));
el("btn-confirm").addEventListener("click", action(onConfirm));
el("btn-interrupt").addEventListener("click", action(onInterrupt));
el("btn-resume").addEventListener("click", action(onResume));
el("btn-reverse").addEventListener("click", action(onReverse));
el("btn-takeover").addEventListener("click", action(onTakeover));
el("btn-reset").addEventListener("click", action(startFreshConversation));
el("btn-continue-task").addEventListener("click", () => {
  state.pendingStart = null;
  el("task-guard").hidden = true;
  if (state.response) state.response.scrollIntoView({ behavior: "smooth", block: "center" });
});
el("btn-end-task").addEventListener("click", action(async () => {
  const next = state.pendingStart;
  state.pendingStart = null;
  el("task-guard").hidden = true;
  archiveTask(hasUnfinishedTask());
  reset(true);
  SESSION_ID = newSessionId();
  if (next) await next();
}));
el("utterance").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    if (!el("btn-submit").disabled) action(onSubmit)();
  }
});
const app = el("app");

function setMobileDrawer(name, open) {
  if (!app) return;
  const other = name === "scenes" ? "trace" : "scenes";
  app.classList.toggle(`is-${name}-open`, open);
  app.classList.remove(`is-${other}-open`);
  [name, other].forEach((drawerName) => {
    const isOpen = drawerName === name && open;
    el(`btn-${drawerName}-mobile`)?.setAttribute("aria-expanded", String(isOpen));
  });
  const persistentOnDesktop = window.matchMedia("(min-width: 1021px)").matches;
  el("conversation-sidebar").setAttribute(
    "aria-hidden",
    String(!app.classList.contains("is-scenes-open") && !persistentOnDesktop),
  );
  el("trace").setAttribute("aria-hidden", String(!app.classList.contains("is-trace-open")));
  if (open && name === "scenes") el("btn-reset")?.focus();
  if (open && name === "trace") el("btn-close-trace")?.focus();
}

["scenes", "trace"].forEach((name) => {
  const button = el(`btn-${name}-mobile`);
  if (button) {
    button.setAttribute("aria-expanded", "false");
    button.addEventListener("click", () => {
      if (!app) return;
      setMobileDrawer(name, !app.classList.contains(`is-${name}-open`));
    });
  }
});
setMobileDrawer("scenes", false);
setMobileDrawer("trace", false);
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    const openDrawer = app?.classList.contains("is-scenes-open")
      ? "scenes"
      : app?.classList.contains("is-trace-open") ? "trace" : null;
    if (openDrawer) {
      setMobileDrawer(openDrawer, false);
      el(`btn-${openDrawer}-mobile`)?.focus();
    }
  }
});
el("btn-close-trace").addEventListener("click", () => {
  setMobileDrawer("trace", false);
  el("btn-trace-mobile").focus();
});
/* 澄清表单是动态渲染的，故用事件委托 */
el("stage").addEventListener("click", (event) => {
  if (event.target && event.target.id === "btn-clarify") action(onClarify)();
  if (event.target && event.target.id === "btn-reverify") action(requestVerification)();
  if (event.target && event.target.id === "btn-refresh-session") action(refreshSession)();
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
if (state.offline) setOfflineState(true);
setButtons();
refreshOptions().catch(() => {
  el("business-date").textContent = "演示数据读取失败，请重新加载页面。";
  timelineAdd("可选对象读取失败，请重新加载页面。", "stop");
});
refreshInsights().catch(() => {
  el("insights-status").textContent = "主动洞察读取失败，请稍后重试。";
});

window.addEventListener("offline", () => setOfflineState(true));
window.addEventListener("online", () => {
  setOfflineState(false);
  refreshOptions().catch(() => {});
  refreshInsights().catch(() => {});
  timelineAdd("已恢复在线；请先刷新服务端状态后继续。", "live");
  if (state.card) refreshSession().catch(() => timelineAdd("服务端状态读取失败，请稍后重试。", "warn"));
});
if (typeof navigator !== "undefined" && "serviceWorker" in navigator) {
navigator.serviceWorker.register("/assets/sw.js?v=9", { scope: "/" }).catch(() => {
    timelineAdd("PWA 静态壳注册失败，当前仍可作为同源网页使用。", "warn");
  });
}
setInterval(() => {
  refreshInsights().catch(() => {});
}, 15000);

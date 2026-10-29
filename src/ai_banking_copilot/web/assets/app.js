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
  "我要做风险测评",
  "看看理财产品",
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
  optionsStatus: "loading",
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
  wealth: { products: [], holdings: [] },
  cards: [],
  bills: null,
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

/* 阶段步进器：服务端在 guard / understand / plan / review 四个真实边界推进，
   `finally` 里补 done。前端 500ms 轮询 + 400ms 最短展示，答案生成后收起。 */
const PROGRESS_POLL_MS = 500;
const PROGRESS_MIN_MS = 400;

/* 生活事件记录状态（与服务端 LifeEventStore 的 status 取值同口径） */
const LIFE_EVENT_STATUS = {
  planned: "已计划",
  executed: "已执行",
  rejected: "已放弃",
  failed: "失败",
};

/* 生活事件 DAG 节点展示名：只做「动作 → 中文」的展示映射，动作名仍以原始文本挂 title */
const LIFE_STEP_LABEL = {
  "funds.freeze": "冻结预算",
  "funds.release": "解冻剩余",
  "merchant.order": "下单结算",
  "transfer.schedule": "按期转出",
  "transfer.create": "转出",
  "transfer.request_collection": "发起分摊",
  "subscription.list": "代扣清单",
  "card.set_limit": "调整限额",
  "card.lock": "锁卡",
  "card.report_lost": "挂失",
  "card.apply_virtual": "补虚拟卡",
  "password.change": "改密码",
  "account.balance": "查余额",
  "account.transactions": "查交易",
  "wealth.redeem": "赎回",
  "bills.yearly": "年度账单",
  "bills.monthly": "月度账单",
};

function newRequestId() {
  try {
    if (window.crypto && crypto.randomUUID) return "req-" + crypto.randomUUID();
  } catch (_) {
    /* 同 newSessionId 的降级路径（老浏览器 / 非安全上下文） */
  }
  return "req-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 10);
}

/* 同源请求统一入口：带上 Cookie 会话；401（登录失效）直接回登录页，不弹错误面板。 */
async function apiFetch(method, path, body, headers) {
  const options = {
    method,
    credentials: "same-origin",
    cache: "no-store",
    headers: Object.assign({}, headers || {}),
  };
  if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  let data = {};
  try {
    data = await response.json();
  } catch (_) {
    data = {};
  }
  if (response.status === 401) window.location.replace("/");
  return { status: response.status, ok: response.ok, data };
}

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
  const staged = path === "/api/submit" || path === "/api/clarify";
  if (staged) {
    payload.request_id = newRequestId();
    startProgress(payload.request_id);
  }
  try {
    const result = await apiFetch("POST", path, payload);
    state.operationUnknown = false;
    return result;
  } finally {
    if (staged) endProgress();
    refreshConversations().catch(() => {});
  }
}

/* ---------------- 阶段步进器 ---------------- */

const progress = { requestId: "", timer: null, startedAt: 0 };

function renderProgressSteps(payload) {
  const root = el("progress-steps");
  if (!root) return;
  const stages = payload && Array.isArray(payload.stages) ? payload.stages : [];
  if (!stages.length) {
    root.hidden = true;
    root.innerHTML = "";
    return;
  }
  root.innerHTML = stages
    .map((stage) => {
      const phaseName = stage.done ? "done" : stage.active ? "active" : "pending";
      return `<span class="progress-step progress-step--${phaseName}">${esc(stage.label)}</span>`;
    })
    .join('<span class="progress-step__sep" aria-hidden="true">›</span>');
  root.hidden = false;
}

async function pollProgress(requestId) {
  if (!requestId || requestId !== progress.requestId) return;
  try {
    const result = await apiFetch(
      "GET",
      `/api/progress?request_id=${encodeURIComponent(requestId)}&session_id=${encodeURIComponent(SESSION_ID)}`,
    );
    if (requestId !== progress.requestId) return;
    if (result.ok) renderProgressSteps(result.data);
  } catch (_) {
    /* 阶段条只是过程反馈：网络抖动不打断主流程，也不弹错误面板 */
  }
}

function startProgress(requestId) {
  progress.requestId = requestId;
  progress.startedAt = Date.now();
  renderProgressSteps(null);
  if (progress.timer) window.clearInterval(progress.timer);
  progress.timer = window.setInterval(() => {
    pollProgress(requestId);
  }, PROGRESS_POLL_MS);
  pollProgress(requestId);
}

/* 答案已生成：先补最后一次轮询把「完成」画出来，再保证最短展示时长后收起 */
function endProgress() {
  const requestId = progress.requestId;
  if (!requestId) return;
  if (progress.timer) window.clearInterval(progress.timer);
  progress.timer = null;
  pollProgress(requestId).then(() => {
    const wait = Math.max(0, PROGRESS_MIN_MS - (Date.now() - progress.startedAt));
    window.setTimeout(() => {
      if (progress.requestId !== requestId) return;
      progress.requestId = "";
      renderProgressSteps(null);
    }, wait);
  });
}

function optionLabel(item) {
  const names = {
    savings: "储蓄", current: "活期", debit: "借记", credit: "信用",
    active: "有效", cancelled: "已取消", locked: "已锁定", lost: "已挂失",
  };
  return String(item.label).replace(/\b(savings|current|debit|credit|active|cancelled|locked|lost)\b/g,
    (name) => names[name]);
}

/* 理财产品是**目录**不是本人对象：选项来自 `/api/wealth/overview`，
   不可申购的置灰但仍列出（原因写在卡片上），绝不隐藏。 */
function productLabel(productId) {
  const row = (state.wealth.products || []).find((item) => item.product_id === productId);
  return row ? `${row.name}（${row.risk_level}）` : productId;
}

function productOptionsHtml(selected = "") {
  const rows = state.wealth.products || [];
  if (!rows.length) return '<option value="">暂无可用理财产品（请先加载理财面板）</option>';
  const options = rows.map((item) => {
    const off = item.eligible === false;
    const label = `${item.name}（${item.risk_level} · 起购 ${item.min_amount_yuan} 元）` +
      (off ? " · 不可申购" : "");
    return `<option value="${esc(item.product_id)}"${off ? " disabled" : ""}` +
      `${item.product_id === selected ? " selected" : ""}>${esc(label)}</option>`;
  }).join("");
  return '<option value="">请选择理财产品</option>' + options;
}

function optionsHtml(slot, selected = "") {
  if (slot === "product") return productOptionsHtml(selected);
  const spec = OBJECT_OPTIONS[slot];
  const records = state.options ? state.options[spec.key] || [] : [];
  const placeholder = records.length ? `请选择${spec.label}` : `暂无可用${spec.label}`;
  return `<option value="">${placeholder}</option>` + records.map((item) =>
    `<option value="${esc(item.id)}"${item.id === selected ? " selected" : ""}>${esc(optionLabel(item))}</option>`
  ).join("");
}

async function refreshOptions() {
  state.optionsStatus = "loading";
  renderSubscriptionDetails(state.options);
  if (currentRecordTab === "charges") renderRecordsTab("charges");
  try {
    const response = await fetch("/api/options", { cache: "no-store" });
    if (!response.ok) throw new Error("无法读取可选对象");
    state.options = await response.json();
    state.optionsStatus = "ready";
  } catch (error) {
    state.optionsStatus = "error";
    renderSubscriptionDetails(state.options);
    if (currentRecordTab === "charges") renderRecordsTab("charges");
    throw error;
  }
  el("business-date").textContent = `演示业务日期：${state.options.today} · 使用模拟资金和演示商户订单`;
  renderSubscriptionDetails(state.options);
  if (currentRecordTab === "charges") renderRecordsTab("charges");
  Object.keys(OBJECT_OPTIONS).forEach((slot) => {
    const select = el(`select-${slot}`);
    select.innerHTML = optionsHtml(slot, select.value);
  });
  setButtons();
  /* 生活事件面板的对象下拉依赖 options：拿到后重渲染一次，避免「展开只有占位」 */
  void refreshLifeEvents();
}

/* ---------------- 理财：风险测评 / 产品目录 / 持仓 ---------------- */

/* 问卷表单：只回传「题号 → 选项号」，等级由服务端算分（前端不给分值、不填等级）。
   `prefix` 区分面板内（panel）与聊天内（chat）两处表单，避免 radio 同名互相顶掉。 */
function assessmentFormHtml(prefix, questionnaire) {
  const items = questionnaire || [];
  if (!items.length) return '<p class="muted">服务端没有下发问卷，请刷新后重试。</p>';
  const fields = items.map((item, index) => {
    const options = (item.options || []).map((option) =>
      `<label class="wealth-option"><input type="radio" name="${esc(prefix)}-${esc(item.id)}"` +
      ` value="${esc(option.id)}" data-question="${esc(item.id)}" />` +
      `<span>${esc(option.label)}</span></label>`
    ).join("");
    return `<fieldset class="wealth-question"><legend>${index + 1}. ${esc(item.prompt)}</legend>${options}</fieldset>`;
  }).join("");
  return `<form class="wealth-form" data-assessment-form="${esc(prefix)}">
    <p class="muted">共 ${items.length} 道题。答完由服务端算分定级（R1–R5），等级不能自填。</p>
    ${fields}
    <p class="muted wealth-error" data-form-error hidden></p>
    <div class="btn-row"><button class="btn btn--primary" type="button" data-action="submit-assessment">提交测评</button></div>
  </form>`;
}

function wealthAssessmentLine(assessment) {
  const info = assessment || {};
  if (info.state === "valid") {
    const source = info.source_label ? ` · 来源：${esc(info.source_label)}` : "";
    return `<p><b>当前等级 ${esc(info.risk_profile)}</b> <span class="chip chip--green">测评有效</span></p>` +
      `<p class="muted">测评日 ${esc(info.assessed_on || "—")} · 有效期至 ${esc(info.expires_on || "—")}${source}</p>`;
  }
  if (info.state === "expired") {
    return `<p><b>上次等级 ${esc(info.risk_profile || "—")}</b> <span class="chip">测评已过期</span></p>` +
      `<p class="muted">有效期至 ${esc(info.expires_on || "—")}；过期后不能申购，请重新作答。</p>`;
  }
  return '<p><b>尚未测评</b> <span class="chip">未完成风险测评</span></p>' +
    '<p class="muted">未测评时任何产品都不能申购（服务端硬拦截），先答完这 5 道题。</p>';
}

/* 产品卡可展开解释：文案与图谱路径全部来自服务端
   （`/api/wealth/overview` 的 `products[*].explanation`），前端只渲染、不生成任何数字。 */
function wealthWhyHtml(item) {
  const why = item.explanation;
  if (!why || !why.title) return "";
  const reasons = (why.reasons || []).map((text) => `<li>${esc(text)}</li>`).join("");
  const path = (why.path || []).map((step) => esc(step)).join(" → ");
  return `<details class="wealth-why">
    <summary>${esc(why.title)}</summary>
    <ul>${reasons}</ul>
    ${path ? `<p class="muted">知识图谱路径：${path}</p>` : ""}
    <p class="muted"><span class="tech">${esc(why.data_source || "")}</span> · 只读解释，不含业绩承诺</p>
  </details>`;
}

/* 产品卡自带金额输入 + 申购按钮：点了就把 product 作为**受信任选择**随提交送上去，
   写操作仍由服务端出计划卡、确认后才执行（前端不直连任何写接口）。 */
function wealthProductHtml(item, assessment) {
  const disabled = item.eligible === false;
  const min = esc(item.min_amount_yuan);
  const chip = disabled
    ? '<span class="chip chip--denied">不可申购</span>'
    : '<span class="chip chip--green">可申购</span>';
  const action = disabled
    ? '<div class="wealth-action"><input class="input" type="number" disabled placeholder="当前不可申购" />' +
      '<button class="btn" type="button" disabled>申购</button></div>'
    : '<div class="wealth-action">' +
      `<input class="input" type="number" inputmode="decimal" min="${min}" step="0.01"` +
      ` placeholder="金额（元），起购 ${min}" data-amount-for="${esc(item.product_id)}" />` +
      '<button class="btn btn--primary" type="button" data-action="wealth-subscribe"' +
      ` data-product="${esc(item.product_id)}" data-min="${min}">申购</button></div>`;
  return `<article class="wealth-product${disabled ? " wealth-product--disabled" : ""}">
    <div><b>${esc(item.name)}</b> ${chip}</div>
    <p class="muted">${esc(item.risk_level)} · 期限 ${esc(item.term_days)} 天 · 起购 ${esc(item.min_amount_yuan)} 元</p>
    ${item.reason ? `<p class="wealth-reason">${esc(item.reason)}</p>` : ""}
    ${wealthWhyHtml(item)}
    ${wealthUnlockHtml(item, assessment)}
    ${action}
    <p class="muted"><span class="tech">${esc(item.product_id)}</span></p>
    <p class="muted wealth-error" data-wealth-error hidden></p>
  </article>`;
}

function wealthHoldingHtml(item) {
  const max = esc(item.amount_yuan);
  return `<article class="wealth-product">
    <div><b>${esc(item.name)}</b> <span class="chip">${esc(item.risk_level)}</span></div>
    <p>金额 ${max} 元 · 起息日 ${esc(item.purchased_on)}</p>
    <p class="muted">当前收益：${esc(item.current_income)}</p>
    <div class="wealth-action">
      <input class="input" type="number" inputmode="decimal" min="0.01" max="${max}" step="0.01"
        placeholder="金额（元），最多 ${max}" data-amount-for="${esc(item.product_id)}" />
      <button class="btn" type="button" data-action="wealth-redeem"
        data-product="${esc(item.product_id)}" data-max="${max}">赎回</button>
    </div>
    <p class="muted"><span class="tech">${esc(item.record_id)}</span></p>
    <p class="muted wealth-error" data-wealth-error hidden></p>
  </article>`;
}

/* ---------------- 理财：场景页可视化（阶梯 / 占比 / 收益） ---------------- */

/* R1 最保守 → R5 最激进（口径见 scenarios/wealth_assessment.py） */
const RISK_SCALE = ["R1", "R2", "R3", "R4", "R5"];

function riskRank(level) {
  return RISK_SCALE.indexOf(String(level || "").toUpperCase());
}

/* 灰显产品的「解锁路径」：只依据产品等级与当前测评状态给下一步，不承诺结果。 */
function wealthUnlockHtml(item, assessment) {
  if (item.eligible !== false) return "";
  if (!assessment || assessment.state !== "valid") {
    return '<p class="muted wealth-unlock">解锁路径：先完成风险测评；等级由服务端算分后，才判定可申购范围。</p>';
  }
  return `<p class="muted wealth-unlock">解锁路径：本只要求 ${esc(item.risk_level)}；` +
    `你当前 ${esc(assessment.risk_profile)}，可在上方「重新测评」重答（等级仍由服务端算分）。</p>`;
}

function wealthLadderHtml(assessment) {
  const info = assessment || {};
  const current = info.state === "valid" ? riskRank(info.risk_profile) : -1;
  const steps = RISK_SCALE.map((level, index) => {
    const hit = index === current;
    return `<li class="wealth-ladder__step${hit ? " is-current" : ""}"${hit ? ' aria-current="true"' : ""}>` +
      `<span class="wealth-ladder__code">${level}</span>` +
      `${hit ? '<span class="wealth-ladder__flag">当前</span>' : ""}</li>`;
  }).join("");
  const note = current >= 0
    ? `当前 ${esc(info.risk_profile)} · 有效期至 ${esc(info.expires_on || "—")}`
    : "尚未取得有效等级：先完成测评，产品才按等级判定。";
  return `<div class="wealth-viz__block">
    <p class="wealth-viz__k">风险等级阶梯 <span class="tech">R1 最保守 → R5 最激进</span></p>
    <ol class="wealth-ladder">${steps}</ol>
    <p class="muted">${note}</p>
  </div>`;
}

/* 持仓占比：金额全部来自接口，只做本人持仓内部的相对占比（合计 100%）。 */
function holdingsShareHtml(holdings) {
  const rows = (holdings || []).map((item) => ({
    name: item.name || "—",
    amount: Number(item.amount_yuan) || 0,
  }));
  const total = rows.reduce((sum, item) => sum + item.amount, 0);
  if (!rows.length) {
    return `<div class="wealth-viz__block">
      <p class="wealth-viz__k">持仓占比</p>
      <p class="muted">当前没有持仓，暂无可计算的占比。</p>
    </div>`;
  }
  const bars = rows.map((item) => {
    const pct = (total > 0 ? (item.amount / total) * 100 : 0).toFixed(1);
    return `<li class="wealth-share__row">
      <span class="wealth-share__name">${esc(item.name)}</span>
      <span class="wealth-share__track" role="img" aria-label="${esc(item.name)} 占 ${pct}%">
        <span class="wealth-share__fill" style="width:${pct}%"></span>
      </span>
      <span class="wealth-share__pct mono">${pct}%</span>
      <span class="wealth-share__amt mono">${esc(item.amount.toFixed(2))} 元</span>
    </li>`;
  }).join("");
  return `<div class="wealth-viz__block">
    <p class="wealth-viz__k">持仓占比 <span class="tech">合计 ${total.toFixed(2)} 元</span></p>
    <ul class="wealth-share">${bars}</ul>
  </div>`;
}

function incomeUnknown(row) {
  return /未知/.test(String((row && row.current_income) || ""));
}

/* 收益：服务端没有净值数据就标「未知」，前端不补零、不推算盈亏。 */
function wealthIncomeHtml(holdings) {
  const rows = holdings || [];
  if (!rows.length) {
    return `<div class="wealth-viz__block">
      <p class="wealth-viz__k">收益</p>
      <p class="muted">当前没有持仓，暂无收益。</p>
    </div>`;
  }
  const unknown = rows.filter(incomeUnknown).length;
  const allUnknown = unknown === rows.length;
  const text = allUnknown
    ? `全部 ${rows.length} 笔持仓均无净值数据，收益标「未知」，不推算盈亏。`
    : `${unknown} / ${rows.length} 笔持仓无净值数据，收益标「未知」。`;
  return `<div class="wealth-viz__block">
    <p class="wealth-viz__k">收益</p>
    <p class="wealth-income"><b>${allUnknown ? "未知" : "部分未知"}</b></p>
    <p class="muted">${esc(text)}</p>
  </div>`;
}

function wealthVizHtml(payload) {
  const data = payload || {};
  return wealthLadderHtml(data.assessment) +
    holdingsShareHtml(data.holdings) +
    wealthIncomeHtml(data.holdings);
}

function wealthLoadingHtml(label) {
  return `<div class="wealth-loading" role="status" aria-live="polite">
    <p class="muted">正在读取${esc(label)}…</p>
    <span class="skeleton"></span><span class="skeleton"></span><span class="skeleton"></span>
  </div>`;
}

/* 首次读取 / 失败重试时先把骨架摆回来，不留「正在读取…」的假运行态。 */
function showWealthLoading() {
  const viz = el("wealth-viz");
  if (viz) viz.innerHTML = wealthLoadingHtml("风险等级与持仓占比");
  ["wealth-products", "wealth-holdings"].forEach((id) => {
    const box = el(id);
    if (box) box.innerHTML = wealthLoadingHtml(id === "wealth-products" ? "产品目录" : "持仓");
  });
}

let wealthRendered = false;

function renderWealth(data) {
  const payload = data || {};
  /* 产品目录同时是「产品」槽的候选来源：先落到 state，再渲染 */
  state.wealth = {
    products: payload.products || [],
    holdings: payload.holdings || [],
  };
  wealthRendered = true;
  const assessment = payload.assessment || {};
  const status = el("wealth-status");
  if (status) status.innerHTML = wealthAssessmentLine(assessment);
  const viz = el("wealth-viz");
  if (viz) viz.innerHTML = wealthVizHtml(payload);
  const box = el("wealth-assessment");
  /* 表单一经渲染就保留用户已选答案：只在没有表单时（首次 / 提交后）重建 */
  if (box && !box.querySelector("[data-assessment-form]")) {
    const open = assessment.state === "valid" ? "" : " open";
    box.innerHTML = `<details class="wealth-retake"${open}>
      <summary>${assessment.state === "valid" ? "重新测评（覆盖当前等级）" : "开始答题"}</summary>
      ${assessmentFormHtml("panel", assessment.questionnaire)}
    </details>`;
  }
  const products = el("wealth-products");
  if (products) {
    const rows = payload.products || [];
    products.innerHTML = rows.length
      ? rows.map((item) => wealthProductHtml(item, assessment)).join("")
      : `<article class="state-card">
        <p class="state-card__k">产品目录 · 暂无产品</p>
        <p class="muted">服务端当前没有下发任何理财产品，目录更新后会自动出现。</p>
      </article>`;
  }
  const holdings = el("wealth-holdings");
  if (holdings) {
    const rows = payload.holdings || [];
    holdings.innerHTML = rows.length
      ? rows.map(wealthHoldingHtml).join("")
      : `<article class="state-card">
        <p class="state-card__k">持仓 · 暂无持仓</p>
        <p class="muted">还没有理财申购记录；在上方产品卡输入金额申购后，这里会出现持仓。</p>
      </article>`;
  }
  const source = el("wealth-source");
  if (source) source.textContent = payload.data_source ? `数据来源：${payload.data_source}` : "";
}

async function refreshWealth() {
  if (!wealthRendered) showWealthLoading();
  const result = await apiFetch("GET", "/api/wealth/overview");
  if (result.status === 404) {
    const message = "理财服务尚未接入（当前渠道没有该读接口）。";
    const status = el("wealth-status");
    if (status) status.textContent = message;
    ["wealth-viz", "wealth-products", "wealth-holdings"].forEach((id) => {
      const box = el(id);
      if (box) {
        box.innerHTML = `<article class="state-card">
          <p class="state-card__k">未接入</p>
          <p class="muted">${esc(message)}</p>
        </article>`;
      }
    });
    return;
  }
  if (!result.ok) throw new Error("理财面板读取失败");
  renderWealth(result.data);
}

function assessmentResultHtml(outcome) {
  const result = (outcome && outcome.result) || {};
  const assessment = (outcome && outcome.assessment) || {};
  const level = result.risk_profile || assessment.risk_profile || "—";
  const score = result.score === undefined ? "—" : `${result.score} / ${result.max_score}`;
  return `<div class="takeover wealth-result">
    <div><b>测评完成：${esc(level)}</b> <span class="chip chip--green">按问卷重新定级</span></div>
    <p>得分 ${esc(score)}（等级由服务端按得分算出，不能自填）。</p>
    <p class="muted">测评日 ${esc(assessment.assessed_on || "—")} · 有效期至 ${esc(assessment.expires_on || "—")}</p>
    <p class="muted">产品目录已按新等级重新判定可申购范围。</p>
  </div>`;
}

async function onAssessmentSubmit(form) {
  const error = form.querySelector("[data-form-error]");
  const answers = {};
  form.querySelectorAll("input[data-question]:checked").forEach((node) => {
    answers[node.dataset.question] = node.value;
  });
  const questions = Array.from(new Set(Array.from(form.querySelectorAll("input[data-question]"))
    .map((node) => node.dataset.question)));
  const missing = questions.filter((id) => !answers[id]);
  if (missing.length) {
    if (error) {
      error.hidden = false;
      error.textContent = `还有 ${missing.length} 道题没有选，请逐题完成后提交。`;
    }
    return;
  }
  const result = await post("/api/risk-assessment", { answers });
  if (result.status !== 200) {
    if (error) {
      error.hidden = false;
      error.textContent = (result.data && result.data.detail) || "提交未被接受，请核对答案后重试。";
    }
    return;
  }
  const card = assessmentResultHtml(result.data);
  const box = el("wealth-assessment");
  if (box && box.contains(form)) {
    box.innerHTML = "";
  } else {
    form.outerHTML = card;
  }
  await refreshWealth();
  timelineAdd(`风险测评已提交：服务端算分定级 ${result.data.result.risk_profile}`, "done");
}

/* 面板读取失败也要说人话：三块都写清楚「没读到」，不留「正在读取…」的假运行态 */
function showWealthFailure(message) {
  const status = el("wealth-status");
  if (status) status.textContent = message;
  ["wealth-viz", "wealth-products", "wealth-holdings"].forEach((id) => {
    const box = el(id);
    if (!box) return;
    box.innerHTML = `<article class="state-card">
      <p class="state-card__k">读取失败</p>
      <p class="muted">${esc(message)}</p>
      ${id === "wealth-viz" ? '<button class="btn btn--sm" type="button" data-panel-retry="wealth">重试</button>' : ""}
    </article>`;
  });
}

function wealthError(trigger, message) {
  const card = trigger.closest(".wealth-product");
  const box = card ? card.querySelector("[data-wealth-error]") : null;
  if (box) {
    box.hidden = false;
    box.textContent = message;
  } else {
    timelineAdd(message, "stop");
  }
}

/* 卡片上的「申购 / 赎回」：把产品作为受信任选择随文本一起提交，
   金额与上限在本地先挡一道（服务端仍会独立复核）。 */
async function onWealthAction(trigger) {
  const redeem = trigger.dataset.action === "wealth-redeem";
  const productId = trigger.dataset.product || "";
  const card = trigger.closest(".wealth-product");
  const input = card ? card.querySelector("[data-amount-for]") : null;
  if (!input) return;
  const raw = (input.value || "").trim();
  const amount = Number(raw);
  if (!raw || !Number.isFinite(amount) || amount <= 0) {
    wealthError(trigger, "请输入大于 0 的金额（单位：元）。");
    input.focus();
    return;
  }
  if (!productId) {
    wealthError(trigger, "未取到产品编号，请重新加载理财面板后再试。");
    return;
  }
  const limit = Number(redeem ? trigger.dataset.max : trigger.dataset.min);
  if (Number.isFinite(limit) && limit > 0) {
    if (!redeem && amount < limit) {
      wealthError(trigger, `低于起购金额 ${limit} 元，请调整金额后重试。`);
      input.focus();
      return;
    }
    if (redeem && amount > limit) {
      wealthError(trigger, `赎回金额不能超过持仓 ${limit} 元。`);
      input.focus();
      return;
    }
  }
  const name = card ? card.querySelector("b")?.textContent || productId : productId;
  const text = `${redeem ? "赎回" : "申购"} ${amount} 元 ${name}`;
  const selected = Object.assign(selectedObjects(), { product: productId });
  const start = () => submitTask(text, selected);
  if (!requestStart(start)) return;
  await start();
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
  status.textContent = `最近扫描：${payload.scanned_at || "服务端时间"} · ${state.insights.length} 条`;
  if (!state.insights.length) {
    list.innerHTML = '<p class="muted">当前没有待处理洞察。</p>';
    return;
  }
  list.innerHTML = state.insights.map((item) => {
    const refs = (item.source_refs || []).map((source) =>
      `<li class="mono"><span class="tech">${esc(source.record_id)} · ${esc(source.content_hash)}</span></li>`
    ).join("");
    const value = item.value === undefined ? "—" : JSON.stringify(item.value);
    return `<article class="insight-row">
      <div class="insight-row__head"><b>${esc(insightRuleLabel(item.rule_id))}</b>` +
      ` <span class="chip">${esc(item.status === "needs_setup" ? "待设置" : "待查看")}</span></div>
      <p class="insight-row__sug">${esc(item.suggestion || "请查看来源后自行决定")}</p>
      <details class="insight-row__more"><summary>依据</summary>
        <p class="muted">业务日 ${esc(item.business_day || "—")} · 窗口 ${esc(item.window_start || "—")} 至 ${esc(item.window_end || "—")}</p>
        <p class="muted">计算式：<span class="mono">${esc(item.formula || "—")}</span> · 值：<span class="mono">${esc(value)}</span></p>
        ${refs ? `<ul>${refs}</ul>` : '<p class="muted">暂无来源记录（预算尚未设置）。</p>'}
      </details>
    </article>`;
  }).join("");
}

/* ---- 转账记录（场景①）：AI 做过的事必须查得到、点得开；数据只来自 /api/transfers ---- */

function yuanText(cents) {
  return `¥${(Number(cents) / 100).toFixed(2)}`;
}

function renderTransfers(payload) {
  const status = el("transfers-status");
  const list = el("transfers-list");
  if (!status || !list) return;
  const items = (payload && payload.items) || [];
  status.textContent = `最近 ${payload.window_days} 天 · 业务日 ${payload.as_of}`;
  if (!items.length) {
    list.innerHTML = '<li class="muted">最近没有转账记录。</li>';
    return;
  }
  list.innerHTML = items.map((item) => {
    const kind = item.kind === "scheduled" ? "定时登记" : "即时转账";
    const remark = item.remark ? esc(item.remark) : "—";
    return `<li class="record">
      <details>
        <summary>
          <b>${esc(item.payee)}</b>
          <span class="chip">${esc(item.status)}</span>
          <span class="mono">${esc(yuanText(item.amount_cents))}</span>
          <span class="muted">${esc(item.booked_on)}</span>
        </summary>
        <p class="muted">${esc(kind)} · ${esc(item.account)} · 记账日 ${esc(item.booked_on)}</p>
        <p class="muted">备注：${remark} <span class="tech mono">· 记录号 ${esc(item.record_id)}</span></p>
      </details>
    </li>`;
  }).join("");
}

async function refreshTransfers() {
  const result = await apiFetch("GET", "/api/transfers");
  if (!result.ok) throw new Error("转账记录读取失败");
  renderTransfers(result.data);
}

function subscriptionStatusLabel(status, registeredStatus = "") {
  const local = { cancelled: "已取消订阅", locked: "已锁定", lost: "已挂失" };
  return local[registeredStatus] || { confirmed: "已确认订阅", suspected: "疑似订阅" }[status] || "未知状态";
}

function confidenceLabel(value) {
  const num = Number(value);
  return Number.isFinite(num) ? `${Math.round(num * 100)}%` : "—";
}

function subscriptionNoticesHtml(item) {
  const notices = item.notices || [];
  if (!notices.length) return "";
  return `<ul class="muted">${notices.map((notice) =>
    `<li>${esc(notice.message)}</li>`).join("")}</ul>`;
}

function subscriptionEvidenceHtml(item) {
  const evidence = item.evidence || [];
  if (!evidence.length) return '<p class="muted">无可核对流水。</p>';
  return `<details><summary>对应交易（${evidence.length}）</summary><ul>` +
    evidence.map((txn) =>
      `<li><span class="mono">${esc(txn.booked_on)} · ${yuan(Math.abs(txn.amount_cents))}</span>` +
      ` <span class="tech">${esc(txn.record_id)} · ${esc(txn.content_hash)} · 渠道 ${esc(txn.channel || "—")}</span></li>`
    ).join("") + "</ul></details>";
}

const CHANNEL_ORDER = ["web", "im", "feishu"];

function renderChannelHealth(data) {
  const status = el("channel-health-status");
  const body = el("channel-health-body");
  if (!status || !body) return;
  const channels = data && data.channels ? data.channels : {};
  const health = data && data.health ? data.health : {};
  const rows = CHANNEL_ORDER.map((name) => {
    const item = channels[name] || {};
    const stats = health[name] || {};
    const configured = item.configured !== false;
    const state = configured ? (item.mode || stats.status || "unknown") : "未接入";
    const lastEvent = stats.last_event_at || "暂无事件";
    const replay = stats.replay_mode || item.mode === "replay" ? "是" : "否";
    return `<tr><td>${esc(name)}</td><td>${esc(state)}</td><td>${esc(lastEvent)}</td>` +
      `<td>${esc(String(stats.ok || 0))}</td><td>${esc(String(stats.failed || 0))}</td>` +
      `<td>${esc(String(stats.ratelimit_hits || 0))}</td><td>${replay}</td></tr>`;
  });
  status.textContent = data && data.health ? "渠道状态已更新" : "渠道状态已读取";
  body.innerHTML = rows.length ? rows.join("\n") : '<tr><td colspan="7">暂无渠道状态。</td></tr>';
}

async function refreshChannelHealth() {
  const status = el("channel-health-status");
  if (status) status.textContent = "正在读取渠道状态…";
  const result = await apiFetch("GET", "/api/channels/health");
  if (!result.ok) throw new Error("无法读取渠道状态");
  renderChannelHealth(result.data);
}

function subscriptionItemHtml(item) {
  const next = item.next_charge_on
    ? `${esc(item.next_charge_on)}（${item.next_charge_is_prediction ? "预测值" : "登记值"}）`
    : "未提供";
  const price = item.price_increased_12m === true
    ? `近 12 个月涨价：${yuan(item.price_change_12m_from_cents)} → ${yuan(item.price_change_12m_to_cents)}`
    : item.price_increased_12m === false ? "近 12 个月未涨价" : "样本不足";
  const external = item.external_cancel_status === "not_integrated"
    ? "未接入（仅本地模拟或商户自助）" : esc(item.external_cancel_status || "—");
  return `<article class="takeover">
    <div><b>${esc(item.merchant)}</b> <span class="chip">${esc(subscriptionStatusLabel(item.status, item.registered_status))}</span>
      <span class="chip">置信度 ${esc(confidenceLabel(item.confidence))}</span></div>
    <p>扣费 ${yuan(item.amount_cents)} · ${esc(item.currency)} · ${esc(item.cycle_label)}扣费</p>
    <p class="subscription-price">${price}</p>
    <p class="muted">最近扣费 ${esc(item.last_charged_on || "—")} · 下次扣款日 ${next}</p>
    <p class="muted">估算依据：${esc(item.next_charge_basis || "—")}</p>
    ${subscriptionNoticesHtml(item)}
    <p><span class="muted">管理建议：</span>${esc(item.suggestion)}</p>
    <p><span class="muted">取消路径：</span>${esc(item.cancel_path)}</p>
    <p class="muted">外部取消接口：${external}</p>
    ${subscriptionEvidenceHtml(item)}
  </article>`;
}

function renderSubscriptionDetails(options) {
  const panel = el("subscription-panel");
  if (!panel) return;
  const details = options && options.subscription_details;
  const caps = options && options.subscription_capabilities;
  const status = el("subscription-status");
  const list = el("subscription-list");
  const boundary = el("subscription-boundary");
  if (state.optionsStatus !== "ready") {
    status.textContent = state.optionsStatus === "error"
      ? "订阅数据暂不可用，请稍后重试。" : "正在读取订阅识别结果…";
    list.innerHTML = "";
    boundary.hidden = true;
    return;
  }
  if (!details) {
    status.textContent = "订阅识别服务尚未接入。";
    list.innerHTML = "";
    boundary.hidden = true;
    return;
  }
  status.textContent = `数据来源：${details.data_source} · 业务日 ${details.as_of}`;
  if (caps && caps.boundary) {
    boundary.hidden = false;
    boundary.textContent = `能力边界：${caps.boundary}`;
  }
  const insights = details.insights || [];
  list.innerHTML = insights.length
    ? insights.map(subscriptionItemHtml).join("")
    : '<p class="muted">未识别到订阅。</p>';
  const notes = details.notes || [];
  if (notes.length) {
    list.innerHTML += `<details><summary>识别说明</summary><ul class="muted">${
      notes.map((note) => `<li>${esc(note)}</li>`).join("")}</ul></details>`;
  }
}

function renderSubscriptionCancel(cancel) {
  const box = el("subscription-cancel-result");
  if (!box) return;
  if (!cancel) {
    box.hidden = true;
    box.innerHTML = "";
    return;
  }
  box.hidden = false;
  const previous = cancel.previous_status
    ? ` · 操作前状态 ${esc(cancel.previous_status)}` : "";
  const confirmed = cancel.external_confirmed ? "商户已确认" : "商户未确认";
  box.innerHTML = `<div class="takeover">
    <p><b>取消操作状态：${esc(cancel.label)}</b></p>
    <p class="muted">外部状态：${esc(cancel.external_status)} · ${confirmed}${previous}</p>
    <p class="muted">${esc(cancel.boundary)}</p>
  </div>`;
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

/* ---------------- 卡片面板（④ 场景） ---------------- */

const CARD_ACTION_UTTERANCE = {
  lock: "锁卡",
  unlock: "解锁卡片",
  lost: "挂失卡片",
};

function cardsHtml(payload) {
  const cards = (payload && payload.cards) || [];
  if (!cards.length) {
    return `<article class="state-card">
      <p class="state-card__k">暂无卡片</p>
      <p class="muted">当前账户下没有可用卡片；补卡后可在这里看到状态与额度。</p>
    </article>`;
  }
  return cards.map((card) => {
    const id = esc(card.card_id);
    const buttons = [];
    if (card.status === "active") {
      buttons.push(`<button class="btn btn--ghost" type="button" data-card-action="lock" data-card-id="${id}">锁卡</button>`);
    }
    if (card.status === "active" || card.status === "locked") {
      buttons.push(`<button class="btn btn--danger" type="button" disabled title="红色动作且不可逆：需强验证（计划卡确认时输入演示验证码）；挂失后不可解锁">挂失</button>`);
      buttons.push(`<input class="input card-limit" type="number" min="1" step="100" placeholder="额度（元）" data-card-limit="${id}" />`);
      buttons.push(`<button class="btn" type="button" disabled title="红色动作：需强验证（计划卡确认时输入演示验证码）">调额</button>`);
    }
    if (card.status === "locked") {
      buttons.push(`<button class="btn btn--danger" type="button" disabled title="红色动作：解卡需强验证（计划卡确认时输入演示验证码）">解锁</button>`);
    }
    buttons.push(`<button class="btn btn--ghost" type="button" data-card-action="virtual" data-card-id="${id}">补虚拟卡</button>`);
    return `<article class="card-item">
      <div class="card-item__head">
        <strong>${esc(card.type_label)}</strong>
        <span class="mono">${id}</span>
        <span class="card-state card-state--${esc(card.status)}">${esc(card.status_label)}</span>
      </div>
      <p class="muted">日限额 ${yuan(card.daily_limit_cents)}</p>
      <div class="btn-row card-actions">${buttons.join("")}
        <button class="btn btn--sm" type="button" data-nav="chat" data-prefill="挂失卡片">去对话办理</button>
      </div>
    </article>`;
  }).join("") + `<p class="muted cards-hint">红色动作（挂失 / 解锁 / 调额）需强验证：点「去对话办理」生成计划卡，确认时输入演示验证码；灰显按钮不在面板直接执行。挂失不可逆——已挂失的卡不能再解锁，要继续用卡只能补卡。</p>`;
}

function cardsVisualHtml(cards) {
  if (!cards.length) return "";
  const statuses = [
    ["active", "正常"],
    ["locked", "已锁定"],
    ["lost", "已挂失"],
    ["cancelled", "已取消"],
  ];
  const total = cards.length || 1;
  const statusRows = statuses
    .map(([key, label]) => {
      const count = cards.filter((card) => card.status === key).length;
      const width = Math.round((count / total) * 100);
      return `<li><span class="cards-bar__label">${esc(label)}</span>
        <span class="cards-bar"><i style="width:${width}%"></i></span>
        <span class="mono">${count} 张</span></li>`;
    })
    .join("");
  const maxLimit = Math.max(...cards.map((card) => card.daily_limit_cents || 0), 1);
  const limitRows = cards
    .map((card) => {
      const width = Math.round(((card.daily_limit_cents || 0) / maxLimit) * 100);
      return `<li><span class="cards-bar__label">${esc(card.type_label)}</span>
        <span class="cards-bar"><i style="width:${width}%"></i></span>
        <span class="mono">${yuan(card.daily_limit_cents || 0)}</span></li>`;
    })
    .join("");
  return `<div class="cards-visual">
    <p class="panel__title panel__title--sub">状态分布</p>
    <ul class="cards-bars">${statusRows}</ul>
    <p class="panel__title panel__title--sub">日限额上限</p>
    <ul class="cards-bars">${limitRows}</ul>
    <p class="muted">流水未记录卡号，暂无法按卡统计当日已用；此处展示状态分布与日限额上限。</p>
  </div>`;
}

async function refreshCards() {
  if (state.offline) return;
  const status = el("cards-status");
  if (status) status.textContent = "正在读取卡片…";
  let result;
  try {
    result = await apiFetch("GET", "/api/cards");
  } catch (error) {
    panelFailure("cards-list", "卡片读取失败，请检查网络后重试。", "cards");
    throw error;
  }
  if (!result.ok) {
    panelFailure("cards-list", "卡片读取失败，请稍后重试。", "cards");
    throw new Error("无法读取卡片");
  }
  const payload = result.data;
  state.cards = payload.cards || [];
  el("cards-status").textContent = `共 ${state.cards.length} 张卡 · 状态与额度来自服务端（${payload.as_of || ""}）`;
  el("cards-list").innerHTML = cardsHtml(payload);
  const visual = el("cards-visual");
  if (visual) visual.innerHTML = cardsVisualHtml(state.cards);
}

function cardLimitCents(cardId) {
  const input = document.querySelector(`[data-card-limit="${cardId}"]`);
  const value = input ? Number(input.value) : NaN;
  if (!Number.isFinite(value) || value <= 0) return null;
  return Math.round(value * 100);
}

async function runPanelTask(text, selected, cardId) {
  const start = () => {
    if (cardId) {
      const select = el("select-card");
      if (select) select.value = cardId;
    }
    return submitTask(text, selected);
  };
  if (!requestStart(start)) return;
  await start();
}

async function cardActionFromPanel(kind, cardId) {
  if (CARD_ACTION_UTTERANCE[kind]) {
    await runPanelTask(CARD_ACTION_UTTERANCE[kind], { card: cardId }, cardId);
    return;
  }
  const cents = cardLimitCents(cardId);
  if (cents === null) {
    el("cards-status").textContent = "请先填写额度（元，正整数），再点「调额 / 补虚拟卡」。";
    return;
  }
  if (kind === "limit") {
    await runPanelTask(
      `把这张卡额度调到 ${cents / 100} 元`,
      { card: cardId, daily_limit: cents },
      cardId,
    );
    return;
  }
  if (kind === "virtual") {
    await runPanelTask("申请虚拟卡", { daily_limit: cents });
  }
}

/* ---------------- 账单分析面板（② 场景） ---------------- */

const FINDING_LABELS = {
  "duplicate-charge": "疑似重复扣款",
  "price-change": "价格上涨",
  "hidden-fee": "疑似隐藏费用",
  "recurring-subscription": "周期订阅",
  "abnormal-spend": "异常大额",
  "late-night": "深夜交易",
  "remote-city": "异地交易",
};
const FINDING_STATUS = { confirmed: "已确认", suspected: "疑似", normal: "正常" };

function findingEvidenceHtml(item) {
  const rows = (item.evidence || []).map((evidence) => {
    const moment = evidence.booked_at ? String(evidence.booked_at).slice(11, 16) : "";
    return `<li>${esc(evidence.booked_on)}${moment ? " " + esc(moment) : ""} · ${esc(evidence.city || "地点未知")} · ${esc(evidence.merchant_label)} · ${yuan(evidence.amount_cents)}</li>`;
  }).join("");
  return rows ? `<ul class="evidence">${rows}</ul>` : "";
}

function billsFindingsHtml(payload) {
  const findings = payload.findings || [];
  if (!findings.length) return '<p class="muted">本期未命中识别规则；未发现不等于不存在。</p>';
  return findings.map((item) => `<details class="finding">
    <summary>
      <strong>${esc(FINDING_LABELS[item.kind] || item.kind)}</strong>
      <span class="muted">${yuan(item.impact_cents || 0)} · 置信度 ${confidenceLabel(item.confidence)} · ${esc(FINDING_STATUS[item.status] || item.status)}</span>
    </summary>
    <p>${esc(item.detail)}</p>
    <p class="muted">为什么可疑：${esc(item.assumption)}</p>
    ${findingEvidenceHtml(item)}
    <p class="muted">${esc(item.suggestion)}</p>
  </details>`).join("");
}

function billsTableHtml(payload) {
  const rows = (payload.transactions || []).map((item) => {
    const moment = item.booked_at ? String(item.booked_at).slice(11, 16) : "";
    return `<tr><td>${esc(item.booked_on)}${moment ? " " + esc(moment) : ""}</td><td>${esc(item.merchant_label)}</td><td>${esc(item.category)}</td><td>${esc(item.city || "—")}</td><td>${yuan(item.amount_cents)}</td></tr>`;
  }).join("");
  return `<table class="mini-table"><thead><tr><th>时间</th><th>商户</th><th>分类</th><th>地点</th><th>金额</th></tr></thead><tbody>${rows || '<tr><td colspan="5">本期无支出流水</td></tr>'}</tbody></table>`;
}

function billsAnomalyChipsHtml(payload) {
  const counts = {};
  (payload.findings || []).forEach((item) => {
    if (item.status === "normal") return;
    counts[item.kind] = (counts[item.kind] || 0) + 1;
  });
  const entries = Object.entries(counts);
  if (!entries.length) return '<p class="muted">本期未命中异常类型。</p>';
  return `<p class="panel__title panel__title--sub">异常类型计数</p>
    <ul class="bills-chips">${entries.map(([kind, count]) => `<li>${esc(FINDING_LABELS[kind] || kind)} · ${count}</li>`).join("")}</ul>`;
}

function billsProfileHtml(profile) {
  const item = profile || {};
  const top = item.top_category
    ? `${esc(item.top_category)} · ${Math.round(Number(item.top_category_share || 0) * 100)}%`
    : "本期无分类支出";
  const topAmount = item.top_category_cents ? yuan(item.top_category_cents) : "—";
  const subscription = item.subscription_monthly_cents
    ? `${yuan(item.subscription_monthly_cents)}/月`
    : "¥0.00/月";
  const suspicious = item.suspicious_count ? `${item.suspicious_count} 条` : "0 条";
  const maxImpact = item.max_suspicious_cents ? yuan(item.max_suspicious_cents) : "—";
  return `<article class="bills-profile-card">
    <p class="panel__title panel__title--sub">用户画像 <span class="tech">只读 · 账单与订阅事实</span></p>
    <ul class="bills-profile-grid">
      <li><span class="muted">消费结构</span><strong>${top}</strong><small class="mono">${topAmount}</small></li>
      <li><span class="muted">订阅负担</span><strong>${subscription}</strong><small class="muted">在用订阅折算到每月</small></li>
      <li><span class="muted">大额风险</span><strong>${suspicious}</strong><small class="mono">最大 ${maxImpact}</small></li>
    </ul>
  </article>`;
}

function billsTrendSvg(trend) {
  const points = trend || [];
  if (!points.length) return '<p class="muted">暂无趋势数据。</p>';
  const width = 320;
  const height = 120;
  const baseline = height - 22;
  const slot = width / points.length;
  const barWidth = Math.max(14, Math.floor(slot * 0.55));
  const max = Math.max(...points.map((point) => point.expense_cents || 0), 1);
  const bars = points.map((point, index) => {
    const barHeight = Math.round(((point.expense_cents || 0) / max) * (baseline - 8));
    const x = Math.round(index * slot + (slot - barWidth) / 2);
    const y = baseline - barHeight;
    return `<rect x="${x}" y="${y}" width="${barWidth}" height="${barHeight}" rx="4" fill="#0b4fd8" opacity="0.85"></rect>
      <text x="${x + barWidth / 2}" y="${height - 6}" text-anchor="middle" font-size="10" fill="currentColor" opacity="0.65">${esc(String(point.period).slice(5))}月</text>`;
  }).join("");
  const last = points[points.length - 1];
  return `<p class="panel__title panel__title--sub">近 6 月支出趋势</p>
    <svg class="bills-trend" viewBox="0 0 ${width} ${height}" role="img" aria-label="近 6 月支出趋势">${bars}</svg>
    <p class="muted">最近一月 ${yuan(last.expense_cents || 0)}（数字来自 /api/bills/analysis.trend）</p>`;
}

function billsShareHtml(items) {
  const rows = (items || []).slice(0, 6);
  if (!rows.length) return '<p class="muted">本期无分类支出。</p>';
  return `<p class="panel__title panel__title--sub">分类占比</p>
    <ul class="share-list">${rows.map((item) => {
      const pct = Math.round(Number(item.share || 0) * 100);
      return `<li><span>${esc(item.category)}</span><span class="share-bar"><i style="width:${pct}%"></i></span><span class="mono">${pct}%</span></li>`;
    }).join("")}</ul>`;
}

function billsChartsHtml(payload) {
  return `<div class="bills-charts">${billsTrendSvg(payload.trend)}${billsShareHtml(payload.category_share)}${billsAnomalyChipsHtml(payload)}</div>`;
}

function billsTop3Html(payload) {
  const risky = (payload.findings || [])
    .filter((item) => item.status !== "normal")
    .sort((a, b) => (b.impact_cents || 0) - (a.impact_cents || 0))
    .slice(0, 3);
  if (!risky.length) {
    return '<p class="panel__title panel__title--sub">可疑前 3</p><p class="muted">暂无需要优先核对的可疑项。</p>';
  }
  return `<p class="panel__title panel__title--sub">可疑前 3 <span class="tech">不当作已确认</span></p>` +
    risky.map((item) => `<article class="bills-top">
      <strong>${esc(FINDING_LABELS[item.kind] || item.kind)}</strong>
      <span class="muted">${yuan(item.impact_cents || 0)} · 置信度 ${confidenceLabel(item.confidence)}</span>
      <p class="muted">${esc(item.detail || "")}</p>
    </article>`).join("");
}

function billsLoading() {
  const status = el("bills-status");
  if (status) status.textContent = "正在读取账单识别结果…";
  const skeleton = '<span class="skeleton"></span><span class="skeleton"></span>';
  ["bills-summary", "bills-profile", "bills-charts", "bills-top3", "bills-findings"].forEach((id) => {
    const node = el(id);
    if (node) node.innerHTML = skeleton;
  });
  const table = el("bills-table");
  if (table) table.innerHTML = "";
}

function billsFailure(message) {
  ["bills-summary", "bills-profile", "bills-charts", "bills-top3", "bills-table"].forEach((id) => {
    const node = el(id);
    if (node) node.innerHTML = "";
  });
  panelFailure("bills-findings", message, "bills");
}

function billsEmpty(payload) {
  const card = `<article class="state-card">
    <p class="state-card__k">本期无支出流水</p>
    <p class="muted">${esc(payload.period)} 没有支出交易，暂不生成画像与图表。</p>
  </article>`;
  el("bills-summary").innerHTML = card;
  el("bills-profile").innerHTML = "";
  el("bills-charts").innerHTML = "";
  el("bills-top3").innerHTML = "";
  el("bills-findings").innerHTML = '<p class="muted">本期未命中识别规则；未发现不等于不存在。</p>';
  el("bills-table").innerHTML = billsTableHtml(payload);
}

async function refreshBills() {
  if (state.offline) return;
  billsLoading();
  let result;
  try {
    result = await apiFetch("GET", "/api/bills/analysis");
  } catch (error) {
    billsFailure("账单识别读取失败，请检查网络后重试。");
    throw error;
  }
  if (!result.ok) {
    billsFailure("账单识别读取失败，请稍后重试。");
    throw new Error("无法读取账单分析");
  }
  const payload = result.data;
  state.bills = payload;
  const summary = payload.summary || {};
  el("bills-status").textContent = `${payload.period} · ${summary.transaction_count || 0} 笔 · 已确认 ${summary.confirmed_count || 0} · 疑似 ${summary.suspected_count || 0}`;
  const disclaimer = el("bills-disclaimer");
  disclaimer.hidden = false;
  disclaimer.textContent = `识别结果只作核对线索，不当作已确认；数据来源：${payload.data_source}`;
  if (!(payload.transactions || []).length) {
    billsEmpty(payload);
    return;
  }
  el("bills-summary").innerHTML = `<p class="muted">本期支出合计 ${yuan(summary.expense_total_cents || 0)}${payload.narrative && payload.narrative.degraded ? " · 文字解释已降级，以下按确定性明细展示" : ""}</p>`;
  el("bills-profile").innerHTML = billsProfileHtml(payload.profile);
  el("bills-charts").innerHTML = billsChartsHtml(payload);
  el("bills-top3").innerHTML = billsTop3Html(payload);
  el("bills-findings").innerHTML = billsFindingsHtml(payload);
  el("bills-table").innerHTML = billsTableHtml(payload);
}

if (el("cards-list")) {
  el("cards-list").addEventListener("click", (event) => {
    const button = event.target.closest("[data-card-action]");
    if (!button) return;
    action(() => cardActionFromPanel(button.dataset.cardAction, button.dataset.cardId))();
  });
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
  /* 申购 / 赎回会改余额与持仓，转账也改余额：执行后一并刷新，避免页面停在旧数字 */
  try {
    await refreshTransfers();
    await refreshWealth();
  } catch (_) {
    timelineAdd("办理已返回，但面板刷新失败；请重新加载页面后核对。", "warn");
  }
  try {
    await refreshCards();
  } catch (_) {
    el("cards-status").textContent = "卡片状态刷新失败，请重新加载页面后核对。";
  }
  try {
    await refreshBills();
  } catch (_) {
    el("bills-status").textContent = "账单识别刷新失败，请稍后重试。";
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

function traceHtml(trace) {
  const rows = trace || [];
  if (!rows.length) {
    return "";
  }
  return `<div class="trace">
      <div class="trace__title">可用余额轨迹</div>
      <ol class="trace__rows">${rows.map((row) => `<li>${esc(row)}</li>`).join("")}</ol>
    </div>`;
}

/* 执行前预演：三块全部回显服务端计划卡字段（summary / balance_trace / decision.reasons），
   绿卡不预演；缺字段就不画那一块，不估算、不补 0。 */
function previewHtml(card) {
  if (!card || !card.level || card.level === "GREEN") return "";
  const rows = card.summary || [];
  const trace = card.balance_trace || [];
  const reasons = state.decision ? state.decision.reasons || [] : [];
  const impact = rows.length
    ? `<dl class="preview__grid">${rows
        .map((row) => `<dt>${esc(row.label)}</dt><dd${row.emphasis ? ' class="preview__val--strong"' : ""}>${esc(row.value)}</dd>`)
        .join("")}</dl>`
    : "";
  return `<details class="preview">
    <summary>确认后会发生什么</summary>
    ${impact ? `<div class="preview__block"><span class="preview__title">影响面</span>${impact}</div>` : ""}
    ${trace.length ? `<div class="preview__block"><span class="preview__title">可用余额轨迹</span><ol class="preview__list">${trace.map((row) => `<li>${esc(row)}</li>`).join("")}</ol></div>` : ""}
    <div class="preview__block"><span class="preview__title">命中规则</span><p class="preview__rules">${rulesHtml(reasons)}</p></div>
    <p class="muted preview__note">以上数字均取自服务端计划卡，前端未做二次计算。</p>
  </details>`;
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
        <span class="amount__note">确认即锁定本计划 · 计划级 + 节点级双层执行门</span>
      </div>
    </div>

    ${quotaHtml(card)}

    ${previewHtml(card)}

    ${lines.length ? `<ol class="steps">${lines.map(stepHtml).join("")}</ol>` : ""}

    ${traceHtml(card.balance_trace)}

    ${card.expires_at ? `<p class="muted voucher__expiry">有效期至 ${esc(momentLabel(card.expires_at))}；过期后需重新生成计划。</p>` : ""}

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
  state.response.innerHTML += roleChainHtml();
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

/* AI 三角色链路：规划器 / 审查员是模型通道，规则风险官是 PDP 规则引擎（不是 LLM）。
   三格只回显服务端真实字段；缺数据写「未调用 / 待裁决」，不补也不猜。 */
function roleChainHtml() {
  const metadata = state.model;
  if (!metadata) return "";
  const channels = {
    "template": "确定性模板", "bypass": "旁路诊断", "llm-strict": "真实模型严格通道",
    "template_fallback": "模板回退（真实模型不可用）", "injected": "测试注入通道",
  };
  const name = (channel) => (channel ? channels[channel] || String(channel) : "未调用");
  const tokens = metadata.usage && metadata.usage.total_tokens;
  const decision = state.decision || null;
  const level = decision && decision.level ? String(decision.level) : "";
  const reasons = decision && Array.isArray(decision.reasons) ? decision.reasons : [];
  const tone = { GREEN: "green", YELLOW: "yellow", RED: "red" }[level] || "";
  const reviewer = metadata.reviewer_channel;
  const reviewerNote = reviewer
    ? (reviewer === "bypass" ? "旁路诊断通道：未逐条审查计划" : "逐条核对计划是否越权")
    : "当前通道旁路审查，不冒充已审查";
  return `<section class="role-chain" aria-label="AI 三角色链路">
    <div class="role-card">
      <span class="role-card__step">① 规划器</span>
      <span class="role-card__value">${esc(name(metadata.planner_channel))}</span>
      <span class="role-card__note">真实 HTTP ${esc(String(metadata.http_attempts || 0))} 次 · tokens ${esc(tokens == null ? "未提供" : String(tokens))}</span>
    </div>
    <div class="role-card">
      <span class="role-card__step">② 审查员</span>
      <span class="role-card__value">${esc(reviewer ? name(reviewer) : "未启用")}</span>
      <span class="role-card__note">${esc(reviewerNote)}</span>
    </div>
    <div class="role-card${tone ? ` role-card--${tone}` : ""}">
      <span class="role-card__step">③ 规则风险官</span>
      <span class="role-card__value">${esc(level || "待裁决")}</span>
      <span class="role-card__note">${esc(decision ? `命中规则 ${reasons.length} 条（规则引擎，非 LLM）` : "尚无裁决")}</span>
    </div>
  </section>`;
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
  /* 左栏列表改为读取服务端历史（`/api/conversations`），不再本地插入临时记录：
     消息入库发生在 `/api/submit` 返回之后，`post()` 的 finally 会刷新列表。 */
  appendMessage("user", text);
  if (Object.keys(selected).length) {
    const objects = Object.keys(selected).map((slot) => {
      if (slot === "product") return productLabel(selected[slot]);
      const node = el(`select-${slot}`);
      return node && node.selectedOptions[0] ? node.selectedOptions[0].textContent : selected[slot];
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
  /* 服务端已校验过期；前端只做「不让人白点」，不改服务端语义 */
  const expired = Boolean(
    state.card && state.card.expires_at && Date.now() >= Date.parse(state.card.expires_at),
  );
  el("btn-confirm").textContent = expired
    ? "计划已过期 · 重新发起"
    : strongPhase
      ? "提交验证码并执行"
      : redPending
        ? "申请强验证"
        : "确认并执行";
  el("btn-confirm").disabled = operationUnknown || offline || expired || !(awaiting || strongPhase) || busy;
  el("btn-interrupt").disabled = operationUnknown || offline || state.busy || !(state.phase === "executing" || awaiting);
  el("btn-resume").disabled = operationUnknown || offline || busy || !state.card || state.phase !== "interrupted";
  el("btn-reject").disabled = operationUnknown || offline || busy || !state.card || !(awaiting || state.phase === "interrupted");
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
  const container = el("presets");
  if (!container) return;
  container.innerHTML = PRESETS.slice(0, 6)
    .map((text) => `<button class="btn" data-preset="${esc(text)}">${esc(text)}</button>`)
    .join("");
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
  if (data.state === "idle" && Array.isArray(data.questionnaire) && data.questionnaire.length) {
    /* 「我要做风险测评」⇒ 聊天内直接出 5 题表单；等级仍由服务端算分，前端不填等级 */
    state.phase = "idle";
    state.panel = `<div class="takeover" data-assessment-host="chat">
      <p class="wealth-invite">${esc(data.message || "风险测评")}</p>
      ${assessmentFormHtml("chat", data.questionnaire)}
    </div>`;
    renderStage();
    timelineAdd("风险测评 · 已下发 5 道题（答完由服务端算分定级）", "done");
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
  /* 场景层追问（如「这笔 6800 是不是我刷的」）只有一句话、没有结构化槽位表单：
     此时必须把服务端那句 prompt 原样显示出来，不能退化成「请补充所需信息后重新提交。」——
     那句空话既没告诉用户缺什么，也和 API 返回的 message 不一致。 */
  const clarifyPrompt = data.message || (Array.isArray(data.questions) ? data.questions[0] : "");
  state.panel = data.state === "blocked"
    ? interceptHtml("安全拒绝（未产生可执行计划）", data.message || "未知意图不猜测执行；已留痕。", false)
    : clarifyHtml() || `<div class="takeover"><p>${esc(clarifyPrompt || "请补充所需信息后重新提交。")}</p></div>`;
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
  /* 有候选（同名收款人）：点选即可，候选信息全部来自后端事实（手机尾号 / 账号尾号 / 最近一次转账） */
  if (ask.candidates && ask.candidates.length) {
    const options = ask.candidates
      .map((item) => `<option value="${esc(item.value)}">${esc(item.label)}</option>`)
      .join("");
    return (
      `<select ${attrs}><option value="">请选择${esc(SLOT_LABELS[slot] || "其一")}</option>` +
      `${options}</select>` +
      `<span class="muted">候选来自名册与已记账流水，只列事实，不做猜测。</span>`
    );
  }
  if (OBJECT_OPTIONS[slot] || slot === "product") return `<select ${attrs}>${optionsHtml(slot)}</select>`;
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
  return result.data;
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

const BILL_STATUS_LABELS = {
  confirmed: "已确认",
  suspected: "疑似",
  normal: "正常（仅提醒）",
};

const BILL_SAVING_LABELS = {
  confirmed: "已确定节省",
  potential: "潜在节省（估算）",
  none: "不计入节省",
};

function billVerdict(summary) {
  const total = Number(summary.transaction_count) || 0;
  const confirmed = Number(summary.confirmed_count) || 0;
  const suspected = Number(summary.suspected_count) || 0;
  const normal = Number(summary.normal_count) || 0;
  if (!total) return "本期没有支出流水，无法给出消费结论。";
  if (!confirmed && !suspected) {
    return normal
      ? `本期 ${total} 笔支出，识别到 ${normal} 项正常提醒，未发现已确认或疑似问题；未发现不等于不存在。`
      : `本期 ${total} 笔支出未命中识别规则；未发现不等于不存在。`;
  }
  const parts = [];
  if (confirmed) parts.push(`${confirmed} 项已确认问题`);
  if (suspected) parts.push(`${suspected} 项疑似问题`);
  return `本期共 ${total} 笔支出，发现 ${parts.join("、")}，请按下方证据逐条核对。`;
}

function billConclusionHtml(payload) {
  const summary = payload.summary;
  const total = Number(summary.transaction_count) || 0;
  const expense = Number(summary.expense_total_cents) || 0;
  const confirmedSaving = Number(payload.confirmed_saving_cents) || 0;
  const potentialSaving = Number(payload.potential_saving_cents) || 0;
  const demo = payload.data_source_is_demo === true;
  const value = (text) => total ? text : "—";
  return `<section class="bill__block">
    <p class="bill__verdict">${esc(billVerdict(summary))}</p>
    <div class="bill__metrics">
      <div class="bill__metric"><span>已确认问题</span><b>${value(`${Number(summary.confirmed_count) || 0} 项`)}</b></div>
      <div class="bill__metric"><span>疑似问题</span><b>${value(`${Number(summary.suspected_count) || 0} 项`)}</b></div>
      <div class="bill__metric"><span>可核实的确定节省</span><b>${value(yuan(confirmedSaving))}</b></div>
      <div class="bill__metric"><span>潜在节省（估算）</span><b>${value(yuan(potentialSaving))}</b></div>
    </div>
    <p class="bill__note">数据覆盖：${esc(payload.period)} 共 ${total} 笔支出，合计 ${yuan(expense)}；历史回看 ${Number(summary.lookback_months) || 0} 个月，分析截至 ${esc(payload.as_of)}。</p>
    <p class="bill__note">${total
      ? `确定节省只统计可核实为已发生的金额，当前为 ${yuan(confirmedSaving)}；潜在节省是识别口径下的估算上限，尚未实现，不能当作已到账金额。`
      : "本期没有支出流水，节省金额与异常数量均无法计算。"}</p>
    <p class="bill__note">识别结果只作核对线索，不当作已确认；未发现不等于不存在，请照下方证据逐条核对。</p>
    <details class="bill__limits" open>
      <summary>数据来源与分析局限</summary>
      <p>数据来源：${esc(payload.data_source)}${demo ? "（演示数据，非真实账单）" : ""}</p>
      <ul>${(payload.notes || []).map((note) => `<li>${esc(note)}</li>`).join("")}</ul>
    </details>
  </section>`;
}

function billEvidenceHtml(evidence) {
  if (!evidence.length) {
    return '<p class="bill__note">没有可回读的交易明细，不能据此确认问题。</p>';
  }
  return `<details class="bill__evidence"><summary>对应交易明细（${evidence.length}）</summary><ul>` +
    evidence.map((txn) =>
      `<li>${esc(txn.booked_on)} · ${esc(txn.merchant_label)} · 支出 <b>${yuan(Math.abs(Number(txn.amount_cents) || 0))}</b>` +
      ` <span class="tech mono">${esc(txn.txn_id)} · ${esc(txn.record_id)} · ${esc(txn.content_hash)}</span></li>`
    ).join("") + "</ul></details>";
}

function billFindingHtml(item) {
  const status = BILL_STATUS_LABELS[item.status] || "未知状态";
  const saving = (item.saving_allocations || []).reduce(
    (sum, row) => sum + (Number(row.amount_cents) || 0), 0,
  );
  return `<article class="bill__finding">
    <div class="bill__finding-head">
      <b>${esc(FINDING_LABELS[item.kind] || item.kind)}</b>
      <span class="chip">${esc(status)}</span>
      <span class="chip">置信度 ${esc(confidenceLabel(item.confidence))}</span>
    </div>
    <p>${esc(item.detail || "")}</p>
    ${billEvidenceHtml(Array.isArray(item.evidence) ? item.evidence : [])}
    <p>影响金额：<b>${yuan(Number(item.impact_cents) || 0)}</b>
      <span class="muted">（计算口径：${esc(item.impact_basis || "未提供")}）</span></p>
    <p class="bill__note">估算假设：${esc(item.assumption || "—")}</p>
    <p class="bill__note">本项计入：${esc(BILL_SAVING_LABELS[item.saving_basis] || "未提供")}${
      saving > 0 ? ` · ${yuan(saving)}` : ""
    }</p>
    <p><span class="muted">建议：</span>${esc(item.suggestion || "请核对交易明细后自行决定。")}</p>
  </article>`;
}

function billAdviceItemHtml(item) {
  const refs = (item.source_refs || []).map((source) => {
    const recordId = String(source.record_id || "");
    const label = recordId.startsWith("bills:")
      ? `月度账单（${recordId.split(":").pop()}）`
      : recordId.startsWith("subscriptions:")
        ? "订阅记录"
        : recordId.startsWith("transactions:") ? "交易流水" : "事实记录";
    return `<li>${esc(label)} <span class="tech mono">记录编号：${esc(recordId)} · 内容校验：${esc(source.content_hash || "—")}</span></li>`;
  }).join("");
  return `<li><b>${esc(item.title)}</b>
    <span class="muted">（账期 ${esc(item.period)}）</span>：${esc(item.suggestion)}
    <details><summary>事实依据与来源（${(item.source_refs || []).length}）</summary>
      <p>${esc(item.evidence)}</p>${refs ? `<ul>${refs}</ul>` : ""}
    </details>
  </li>`;
}

function billActionsHtml(findings, advice, adviceMessage, hasSubscription) {
  const items = findings.map((item) =>
    `<li><b>${esc(FINDING_LABELS[item.kind] || item.kind)}</b>：${esc(item.suggestion || "请核对交易明细后自行决定。")}</li>`
  );
  const adviceItems = (Array.isArray(advice) ? advice : []).map(billAdviceItemHtml);
  return `<section class="bill__block">
    <h4>行动建议</h4>
    ${items.length || adviceItems.length
      ? `<ul class="bill__actions">${items.join("")}${adviceItems.join("")}</ul>`
      : '<p class="muted">本期没有可执行行动；请以核心结论与数据局限为准。</p>'}
    ${adviceMessage ? `<p class="muted">${esc(adviceMessage)}</p>` : ""}
    ${hasSubscription
      ? '<div class="btn-row"><button class="btn btn--ghost" type="button" data-action="open-subscriptions">查看订阅记录与取消口径</button></div>'
      : ""}
    <p class="bill__note">可执行范围：以上按钮只打开只读订阅面板，不会改变订阅状态。商户退款、投诉、代扣协议变更与商户侧取消尚未接入，本页不提供执行按钮，也不会把未接入的操作显示为成功。</p>
    ${hasSubscription
      ? '<p class="bill__note">如需取消订阅，请在对话中说明「取消订阅」：系统会生成计划卡，取消只更新本地订阅状态，商户侧是否停止扣费以商户结果为准。</p>'
      : ""}
  </section>`;
}

function billChartsHtml(transactions) {
  if (!transactions.length) {
    return `<section class="bill__block"><h4>统计图表（辅助）</h4>
      <p class="muted">本期无支出流水，无法绘制分类占比或消费分布。</p></section>`;
  }
  const byCategory = new Map();
  const byDay = new Map();
  transactions.forEach((item) => {
    const cents = Math.abs(Number(item.amount_cents) || 0);
    byCategory.set(item.category, (byCategory.get(item.category) || 0) + cents);
    byDay.set(item.booked_on, (byDay.get(item.booked_on) || 0) + cents);
  });
  const total = [...byCategory.values()].reduce((sum, value) => sum + value, 0) || 1;
  const categories = [...byCategory.entries()].sort((a, b) => b[1] - a[1]);
  const days = [...byDay.entries()].sort((a, b) => (a[0] < b[0] ? -1 : 1));
  const peak = Math.max(...days.map(([, value]) => value), 1);
  return `<section class="bill__block">
    <h4>统计图表（辅助）</h4>
    <p class="bill__note">图表只按本期交易明细汇总，用于辅助理解，不参与结论或金额判定。</p>
    <p><b>分类占比</b></p>
    ${categories.map(([category, cents]) => {
      const share = Math.round(cents / total * 1000) / 10;
      return `<div class="bill__bar">
        <span>${esc(category)}</span>
        <span class="bill__bar-track"><i style="width:${Math.max(share, 1)}%"></i></span>
        <span class="mono">${yuan(cents)} · ${share}%</span>
      </div>`;
    }).join("")}
    <p><b>消费分布（按日）</b></p>
    <div class="bill__days">${days.map(([day, cents]) =>
      `<div class="bill__day" title="${esc(day)} · ${yuan(cents)}">
        <i style="height:${Math.max(Math.round(cents / peak * 100), 2)}%"></i>
        <span>${Number(day.slice(8, 10))}日</span>
      </div>`).join("")}</div>
    <p class="bill__note">多期趋势未展示：当前账单接口只返回本期交易明细，缺少可核实的跨期序列。</p>
  </section>`;
}

function billAnalysisHtml(payload, advice, adviceMessage) {
  if (!payload || !payload.summary) return "";
  const findings = Array.isArray(payload.findings) ? payload.findings : [];
  const transactions = Array.isArray(payload.transactions) ? payload.transactions : [];
  const problems = findings.filter((item) => item.status !== "normal");
  const hasSubscription = findings.some((item) => item.kind === "recurring-subscription")
    || (Array.isArray(advice) && advice.some((item) => item.kind === "subscription-review"));
  return `<section class="bill">
    <div class="bill__head">
      <h3>账单分析结论</h3>
      <span class="chip">账期 ${esc(payload.period)}</span>
      ${payload.data_source_is_demo ? '<span class="chip">演示数据</span>' : ""}
    </div>
    ${billConclusionHtml(payload)}
    <section class="bill__block">
      <h4>问题与证据${problems.length ? `（${problems.length}）` : ""}</h4>
      ${findings.length > problems.length
        ? `<p class="muted">其余 ${findings.length - problems.length} 项为正常提醒（仅记录，不计入问题与节省）。</p>`
        : ""}
      ${findings.length
        ? findings.map(billFindingHtml).join("")
        : '<p class="muted">本期未命中识别规则；未发现不等于不存在，请结合下方数据局限理解。</p>'}
    </section>
    ${billActionsHtml(findings, advice, adviceMessage, hasSubscription)}
    ${billChartsHtml(transactions)}
  </section>`;
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

function resultMessage(item) {
  const message = String(item.message || "");
  if (item.external_status === "not_integrated") {
    const previous = item.previous_status ? `操作前状态：${item.previous_status}；` : "";
    return `${message}（仅本地模拟：尚未连接商户取消接口，${previous}外部商户未取消）`;
  }
  return message;
}

function applyExecution(data) {
  showModelMetadata(data.model);
  if (data.card) state.card = data.card;
  if (data.decision) state.decision = data.decision;
  if (data.state === "awaiting_confirm") state.verification = null;
  renderSubscriptionCancel(data.subscription_cancel);
  const results = data.results || [];
  results.forEach((item, index) => {
    if (state.stepStates[index] !== undefined) state.stepStates[index] = "done";
    if (index < state.resultCount) return;
    if (item.record_ids && item.record_ids.length) chainAdd("已执行", item.record_ids[0]);
    timelineAdd(resultMessage(item), "done");
  });
  state.resultCount = results.length;
  state.phase = data.state === "completed" ? "done" : data.state;
  const complete = data.state === "completed";
  const billHtml = complete
    ? billAnalysisHtml(data.bill_findings, data.bill_advice, data.bill_advice_message)
    : "";
  const adviceFallback = billAdviceHtml(data.bill_advice) +
    (data.bill_advice_message ? `<p class="muted">${esc(data.bill_advice_message)}</p>` : "");
  state.notice = complete
    ? `<div class="takeover takeover--receipt"><p>${esc(data.message || "计划执行完成")}</p>` +
      results.map((item) => `<p>${esc(resultMessage(item))}</p>`).join("") + `</div>` +
      (billHtml || adviceFallback) + provenancePanelHtml()
    : interceptHtml("执行尚未完成", data.message || "请核对已执行步骤。", false);
  renderStage();
  if (complete) void loadProvenance();
  timelineAdd(data.message || (complete ? "执行完成" : "执行尚未完成"), complete ? "done" : "stop");
  setButtons();
}

/* 数值溯源：只读审计里该用户最近一条 execution 事件的 provenance（record_id + content_hash），
   没有就如实说没有，绝不补哈希。 */
function provenancePanelHtml() {
  return `<details class="provenance" id="provenance-panel">
    <summary>数值溯源（record_id + content_hash）</summary>
    <p class="muted">读取审计…</p>
  </details>`;
}

async function loadProvenance() {
  const panel = el("provenance-panel");
  if (!panel) return;
  const head = "<summary>数值溯源（record_id + content_hash）</summary>";
  try {
    const result = await apiFetch("GET", "/api/audit?limit=20");
    if (!result.ok) throw new Error(String(result.status));
    const events = Array.isArray(result.data.events) ? result.data.events : [];
    const latest = events.find((event) => event.kind === "execution");
    const payload = latest && latest.payload ? latest.payload : {};
    const rows = Array.isArray(payload.provenance) ? payload.provenance : [];
    panel.innerHTML = head + (rows.length
      ? `<ul class="provenance__rows">${rows
          .map((row) => `<li><span class="mono">${esc(String(row.record_id || ""))}</span><span class="mono">${esc(String(row.content_hash || ""))}</span></li>`)
          .join("")}</ul>`
      : '<p class="muted">本次执行未产生可溯源记录（审计中没有 provenance），不展示哈希。</p>');
  } catch (_) {
    panel.innerHTML = head + '<p class="muted">审计暂不可读，溯源未展示。</p>';
  }
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

async function onReject() {
  const result = await post("/api/reject", {});
  if (result.status !== 200) {
    await refreshSession();
    state.notice = interceptHtml("放弃被拒绝", result.data.detail || "当前状态不可放弃", false);
    renderStage();
    timelineAdd(`放弃被拒绝：${result.data.detail || "当前状态不可放弃"}`, "stop");
    return;
  }
  state.card = null;
  state.decision = null;
  state.stepStates = [];
  state.verification = null;
  state.phase = "idle";
  state.notice = `<div class="takeover"><p>${esc(result.data.message || "已放弃当前计划：原计划作废")}</p></div>`;
  timelineAdd("已放弃当前计划：原计划与已发验证码一并作废", "warn");
  renderStage();
  setButtons();
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

/* ---------------- 历史对话 ---------------- */

const CONVERSATION_STATE_LABELS = {
  idle: "未开始",
  needs_clarification: "待补充信息",
  blocked: "已拒绝",
  awaiting_confirm: "待确认",
  executing: "执行中",
  interrupted: "已中断",
  completed: "已完成",
  partial: "部分完成",
  failed: "执行失败",
  taken_over: "已转人工",
  reversed: "已回退",
};

function conversationStateLabel(stateName) {
  return CONVERSATION_STATE_LABELS[stateName] || "";
}

/* 服务端存 ISO 时间（带 T）；列表里只展示到分钟 */
function momentLabel(stamp) {
  const text = String(stamp || "");
  if (!text) return "—";
  return text.replace("T", " ").slice(0, 16);
}

async function refreshConversations() {
  const list = el("conversation-list");
  if (!list || state.offline) return;
  let result;
  try {
    result = await apiFetch("GET", "/api/conversations");
  } catch (_) {
    list.innerHTML = '<p class="muted convs__empty">历史对话读取失败，请稍后重试。</p>';
    renderHomeRecent([]);
    return;
  }
  if (!result.ok) {
    list.innerHTML = '<p class="muted convs__empty">历史对话读取失败，请稍后重试。</p>';
    renderHomeRecent([]);
    return;
  }
  const payload = result.data || {};
  if (payload.available === false) {
    list.innerHTML = '<p class="muted convs__empty">当前部署未启用历史保存。</p>';
    renderHomeRecent([]);
    return;
  }
  const conversations = Array.isArray(payload.conversations) ? payload.conversations : [];
  if (!conversations.length) {
    list.innerHTML = '<p class="muted convs__empty">开始办理后，对话会保存在这里。</p>';
    renderHomeRecent([]);
    return;
  }
  list.innerHTML = conversations.map((item) => {
    const title = item.title || "未命名会话";
    const meta = [
      momentLabel(item.last_active_at),
      `${Number(item.message_count) || 0} 条消息`,
      conversationStateLabel(item.state),
    ].filter(Boolean).join(" · ");
    const active = item.session_id === SESSION_ID ? " conv-row--active" : "";
    return `<div class="conv-row${active}">
      <button class="conversation-record" type="button" data-open-session="${esc(item.session_id)}">
        <span class="conv-row__title">${esc(title)}</span>
        <span class="conv-row__meta">${esc(meta)}</span>
      </button>
      <button class="conv-delete" type="button" data-delete-session="${esc(item.session_id)}"
              aria-label="删除对话：${esc(title)}">删除</button>
    </div>`;
  }).join("");
  renderHomeRecent(conversations.slice(0, 4));
}

/* 窄屏首页的「最近对话」：与左栏同源，只保留可点开的标题行 */
function renderHomeRecent(items) {
  const host = el("home-recent");
  const list = el("home-recent-list");
  if (!host || !list) return;
  if (!items.length) {
    host.hidden = true;
    list.innerHTML = "";
    return;
  }
  host.hidden = false;
  list.innerHTML = items.map((item) => {
    const title = item.title || "未命名会话";
    return `<button class="conversation-record" type="button" data-open-session="${esc(item.session_id)}">${esc(title)}</button>`;
  }).join("");
}

/* 点开历史：待确认的计划卡恢复成可继续确认的活卡，其余一律只读 */
async function openConversation(sessionId) {
  if (!sessionId || sessionId === SESSION_ID) return;
  if (hasUnfinishedTask() && !window.confirm("当前办理尚未结束，切换后将保存为只读记录。继续？")) {
    return;
  }
  let result;
  try {
    result = await apiFetch("GET", `/api/conversations/${encodeURIComponent(sessionId)}`);
  } catch (_) {
    timelineAdd("历史对话读取失败，请稍后重试。", "warn");
    return;
  }
  if (!result.ok) {
    timelineAdd(
      result.status === 404 ? "这条对话已不存在。" : "历史对话读取失败，请稍后重试。",
      "warn",
    );
    refreshConversations().catch(() => {});
    return;
  }
  const data = result.data || {};
  archiveTask();
  reset(false);
  SESSION_ID = sessionId;
  const welcome = el("conversation-welcome");
  if (welcome) welcome.hidden = true;
  const task = document.createElement("section");
  task.className = "chat-task";
  task.dataset.session = sessionId;
  task.setAttribute("aria-label", `历史对话 ${++state.taskCount}`);
  state.task = task;
  el("stage").appendChild(task);
  (data.messages || []).forEach((message) => {
    appendMessage(message.role === "user" ? "user" : "assistant", message.text || "");
  });
  if (data.card && data.state === "awaiting_confirm") {
    state.card = data.card;
    state.decision = null;
    state.stepStates = (data.card.lines || []).map(() => "pending");
    state.phase = "awaiting_confirm";
    state.response = appendMessage("assistant");
    state.panel = "";
    state.notice = "";
    renderStage();
    timelineAdd("已恢复为可继续确认的计划卡（计划仍按原冻结哈希核对）。", "done");
  } else {
    makeReadOnly(task);
    state.task = null;
    state.response = null;
    state.phase = "idle";
    const note = document.createElement("p");
    note.className = "archive-note";
    note.textContent = "历史对话 · 只读记录；需要继续办理请新建对话。";
    task.appendChild(note);
    const label = conversationStateLabel(data.snapshot_state || data.state);
    timelineAdd(`已打开历史对话${label ? `（${label}）` : ""}。`, "live");
  }
  setButtons();
  refreshConversations().catch(() => {});
}

/* 删除单条历史：`demo` 模式下先试删，403 再补一次性票据重试 */
async function deleteConversation(sessionId) {
  if (!sessionId) return;
  if (!window.confirm("删除这条对话？消息记录与办理快照都会删除，且不可恢复。")) return;
  let result;
  try {
    result = await apiFetch("DELETE", `/api/conversations/${encodeURIComponent(sessionId)}`);
    if (result.status === 403) {
      const password = window.prompt("删除历史需要再次验证身份，请输入登录口令：");
      if (!password) return;
      const ticket = await apiFetch("POST", "/api/auth/reverify", {
        password,
        purpose: "delete_conversation",
      });
      if (!ticket.ok || !ticket.data || !ticket.data.ticket) {
        timelineAdd("口令校验未通过，删除已取消。", "warn");
        return;
      }
      result = await apiFetch(
        "DELETE",
        `/api/conversations/${encodeURIComponent(sessionId)}`,
        undefined,
        { "X-Reverify-Ticket": ticket.data.ticket },
      );
    }
  } catch (_) {
    timelineAdd("删除失败，请稍后重试。", "warn");
    return;
  }
  if (!result.ok) {
    timelineAdd(
      result.status === 404 ? "这条对话已不存在。" : "删除失败，请稍后重试。",
      "warn",
    );
    refreshConversations().catch(() => {});
    return;
  }
  if (sessionId === SESSION_ID) {
    const stale = state.task;
    archiveTask();
    if (stale) stale.remove();
    reset(false);
    SESSION_ID = newSessionId();
    const welcome = el("conversation-welcome");
    if (welcome && !el("stage").querySelector(".chat-task")) welcome.hidden = false;
  }
  timelineAdd("已删除一条历史对话。", "live");
  refreshConversations().catch(() => {});
}

/* ---------------- 个人中心 ---------------- */

function profileValue(value) {
  return value === null || value === undefined || value === "" ? "—" : String(value);
}

const AUTH_MODE_LABELS = { demo: "演示登录", off: "单用户演示" };

function renderProfile(me, audit) {
  const targets = [el("profile-body"), el("view-me-body")].filter(Boolean);
  if (!targets.length) return;
  const user = me.user || {};
  const session = me.session || {};
  const name = user.display_name || user.username || "当前用户";
  const entry = el("profile-entry-name");
  if (entry) entry.textContent = name;
  const initial = name.trim().slice(0, 1) || "用";
  const authMode = AUTH_MODE_LABELS[me.auth_mode] || "演示环境";
  const logins = (me.recent_logins || []).map((record) => {
    const revoked = record.revoked ? " · 已失效" : "";
    return `<li>${esc(momentLabel(record.issued_at))} 登录 · ${esc(profileValue(record.client_ip))} · ${esc(profileValue(record.user_agent))}${revoked}</li>`;
  }).join("");
  const trail = (audit.events || []).map((event) => {
    const payload = event.payload || {};
    const channel = payload.channel || "未记录";
    const digest = payload.text_digest;
    return `<li><span class="mono">${esc(momentLabel(event.timestamp))}</span> · ${esc(profileValue(event.kind))}` +
      ` · 渠道 ${esc(channel)} <span class="tech mono">${esc(profileValue(event.event_id))}</span>` +
      (digest ? `<details><summary>同摘要关联</summary><p class="mono audit-digest">${esc(digest)}</p></details>` : "") + "</li>";
  }).join("");
  const html = `
    <section class="profile-head">
      <span class="profile-avatar" aria-hidden="true">${esc(initial)}</span>
      <span class="profile-id">
        <b>${esc(name)}</b>
        <span class="profile-id__sub">${esc(user.username ? "@" + user.username : "")}${
          user.phone_tail ? ` · 手机尾号 ${esc(user.phone_tail)}` : ""
        }</span>
      </span>
      <span class="profile-chips">
        <span class="chip">${session.authenticated ? "已登录" : "演示环境"}</span>
        <span class="chip">${esc(authMode)}</span>
      </span>
    </section>
    <div class="profile-grid">
      <section class="profile-card">
        <h3>身份</h3>
        <dl>
          <dt>姓名</dt><dd>${esc(name)}</dd>
          <dt>账号</dt><dd class="mono">${esc(profileValue(user.username))}</dd>
          <dt>手机</dt><dd>${user.phone_tail ? `尾号 ${esc(user.phone_tail)}` : "—"}</dd>
          <dt>用户号</dt><dd class="mono tech">${esc(profileValue(user.user_id))}</dd>
        </dl>
      </section>
      <section class="profile-card">
        <h3>关联数据</h3>
        <dl>
          <dt>账户</dt><dd>${esc(String(me.accounts ?? 0))} 个</dd>
          <dt>收款人</dt><dd>${esc(String(me.payees ?? 0))} 个</dd>
          <dt>渠道绑定</dt><dd>${esc(String(me.channel_bindings ?? 0))} 个</dd>
        </dl>
      </section>
    </div>
    <section class="profile-card">
      <h3>登录安全</h3>
      <dl>
        <dt>本次登录</dt><dd>${esc(momentLabel(session.issued_at))}</dd>
        <dt>有效至</dt><dd>${esc(momentLabel(session.expires_at))}</dd>
      </dl>
      <h4>最近登录</h4>
      ${logins ? `<ul class="profile-trail">${logins}</ul>` : '<p class="muted">暂无可查看的登录记录。</p>'}
    </section>
    <details class="profile-card profile-card--fold">
      <summary>我的操作轨迹</summary>
      ${
        audit.available === false
          ? '<p class="muted">审计链读取失败，轨迹暂不可用。</p>'
          : trail
            ? `<ul class="profile-trail">${trail}</ul>`
            : '<p class="muted">暂无可查看的操作。</p>'
      }
    </details>
    ${me.auth_mode === "demo" ? '<div class="btn-row"><button class="btn btn--ghost" id="btn-logout" type="button">退出登录</button></div>' : ""}
  `;
  targets.forEach((body) => {
    body.innerHTML = html;
  });
}

/* 读一次账户信息并渲染：弹窗（桌面）与「我的」页（窄屏）共用 */
async function loadProfile(target, loadingText) {
  if (target) target.innerHTML = `<p class="muted">${loadingText}</p>`;
  try {
    const [me, audit] = await Promise.all([
      apiFetch("GET", "/api/me"),
      apiFetch("GET", "/api/audit?limit=8"),
    ]);
    if (!me.ok) {
      if (target) target.innerHTML = '<p class="muted">账户信息读取失败，请稍后重试。</p>';
      return;
    }
    renderProfile(me.data || {}, audit.ok ? audit.data || {} : { available: false });
  } catch (_) {
    if (target) target.innerHTML = '<p class="muted">账户信息读取失败，请稍后重试。</p>';
  }
}

async function openProfile() {
  const panel = el("profile-panel");
  const body = el("profile-body");
  const backdrop = el("profile-backdrop");
  if (!panel || !body) return;
  panel.hidden = false;
  if (backdrop) backdrop.hidden = false;
  el("btn-profile").setAttribute("aria-expanded", "true");
  el("btn-close-profile").focus();
  await loadProfile(body, "正在读取账户信息…");
}

function closeProfile() {
  const panel = el("profile-panel");
  const backdrop = el("profile-backdrop");
  if (!panel || panel.hidden) return;
  panel.hidden = true;
  if (backdrop) backdrop.hidden = true;
  el("btn-profile").setAttribute("aria-expanded", "false");
  el("btn-profile").focus();
}

/* 顶部入口只做身份标识：读不到就保留默认文案，不打断主流程 */
async function refreshProfileEntry() {
  const entry = el("profile-entry-name");
  const sideName = el("side-user-name");
  const avatar = document.querySelector(".side-user__avatar");
  if (!entry && !sideName && !avatar) return;
  try {
    const result = await apiFetch("GET", "/api/me");
    if (!result.ok) {
      if (avatar) avatar.textContent = "演示";
      return;
    }
    const user = (result.data || {}).user || {};
    const name = user.display_name || user.username || "当前用户";
    if (name) {
      if (entry) entry.textContent = name;
      if (sideName) sideName.textContent = name;
      /* 头像只取首字做视觉标识，无障碍名仍由 #side-user-name 提供 */
      if (avatar) avatar.textContent = Array.from(name)[0] || "示";
    }
  } catch (_) {
    /* 只影响入口文案，静默保留默认值 */
    if (avatar) avatar.textContent = "演示";
  }
}

/* ---------------- 生活事件 ---------------- */

/* 面板参数：键名与服务端槽位同名，发起后若仍需澄清，用同一份值自动续答 */
const LIFE_FORMS = {
  L1: [
    { key: "amount", label: "预算金额（元）", type: "number", placeholder: "800" },
    { key: "merchant", label: "商户", type: "text", placeholder: "鲜花礼品店" },
    { key: "account", label: "冻结账户", type: "account" },
  ],
  L2: [
    { key: "payee", label: "收款人", type: "text", placeholder: "李娜" },
    { key: "amount", label: "每月金额（元）", type: "number", placeholder: "500" },
  ],
  L3: [
    { key: "card", label: "卡片", type: "card" },
    { key: "daily_limit", label: "出行日限额（元）", type: "number", placeholder: "1000" },
  ],
  L4: [
    { key: "amount", label: "目标金额（元）", type: "number", placeholder: "500" },
    { key: "payee", label: "收款人", type: "text", placeholder: "李娜" },
    { key: "product", label: "赎回产品", type: "text", placeholder: "wp_cash" },
    { key: "account", label: "转出账户", type: "account" },
  ],
  L5: [{ key: "card", label: "卡片", type: "card" }],
  L6: [],
  L7: [
    { key: "payer", label: "付款人", type: "text", placeholder: "李娜" },
    { key: "amount", label: "分摊金额（元）", type: "number", placeholder: "100" },
  ],
};

const LIFE_UTTERANCE = {
  L1: (v) => `我妈下个月过生日，预算 ${v.amount || 800} 元，商户 ${v.merchant || "鲜花礼品店"}`,
  L2: (v) => `每月给${v.payee || "李娜"} ${v.amount || 500} 元生活费`,
  L3: (v) => `下周三出差，把卡片日限额降到 ${v.daily_limit || 1000} 元`,
  L4: (v) => `凑钱还房贷 ${v.amount || 500} 元给${v.payee || "李娜"}`,
  L5: () => "我的借记卡丢了",
  L6: () => "阶段盘点",
  L7: (v) => `分摊收款，${v.payer || "李娜"} 应付 ${v.amount || 100} 元`,
};

function lifeFieldHtml(eventId, field) {
  const attrs = `class="input" data-life-field="${esc(field.key)}" data-event="${esc(eventId)}"`;
  if (field.type === "account" || field.type === "card") {
    return `<select ${attrs}>${optionsHtml(field.type)}</select>`;
  }
  return `<input ${attrs} type="${field.type}" inputmode="${field.type === "number" ? "decimal" : "text"}" placeholder="${esc(field.placeholder || "")}" />`;
}

/* 横向 DAG 步骤条：节点与顺序全部来自服务端下发的 skeleton，前端不拼步骤、不猜动作 */
function lifeDagHtml(skeleton) {
  const steps = Array.isArray(skeleton) ? skeleton.filter((step) => typeof step === "string" && step) : [];
  if (!steps.length) return "";
  const nodes = steps.map((step, index) => {
    const label = LIFE_STEP_LABEL[step] || step;
    return `<li class="life-dag__node" title="${esc(step)}"><span class="life-dag__index tech mono">${index + 1}</span>${esc(label)}</li>`;
  });
  return `<ol class="life-dag" aria-label="链路步骤">${nodes.join('<li class="life-dag__arrow" aria-hidden="true">→</li>')}</ol>`;
}

const LIFE_BUDGET_STEPS = [
  ["freeze", "冻结"],
  ["order", "下单"],
  ["release", "释放"],
];

/* 预算执行进度：三段是否点亮由服务端按 holds / 下单回执算出，无数据即不画 */
function lifeBudgetHtml(budget) {
  if (!budget || typeof budget !== "object") return "";
  const steps = budget.steps || {};
  const segments = LIFE_BUDGET_STEPS.map(
    ([key, label]) => `<li class="life-budget__seg${steps[key] ? " is-done" : ""}">${label}</li>`,
  ).join("");
  const state = budget.state === "released" ? "已释放" : "冻结中";
  const amount = typeof budget.amount_cents === "number" ? ` · 冻结余额 ${yuan(budget.amount_cents)}` : "";
  const holdId = budget.hold_id ? ` · 冻结单 <span class="tech">${esc(budget.hold_id)}</span>` : "";
  return `<div class="life-budget">
        <ol class="life-budget__bar" aria-label="预算执行进度">${segments}</ol>
        <p class="muted life-budget__meta">${esc(state)}${esc(amount)}${holdId}</p>
      </div>`;
}

function lifeValues(form) {
  const values = {};
  Array.from(form.querySelectorAll("[data-life-field]")).forEach((node) => {
    const value = (node.value || "").trim();
    if (value) values[node.dataset.lifeField] = value;
  });
  return values;
}

async function runLifeEvent(eventId, form) {
  const values = form ? lifeValues(form) : {};
  const build = LIFE_UTTERANCE[eventId];
  const text = build ? build(values) : "";
  if (!text) return;
  const selected = {};
  if (values.card) selected.card = values.card;
  const start = async () => {
    const data = await submitTask(text, selected);
    if (!data || data.state !== "needs_clarification") return;
    const asks = (data.clarifications || []).filter((ask) => ask.slot !== "user_id");
    const answers = {};
    asks.forEach((ask) => {
      const slot = baseSlot(ask.slot);
      if (values[slot]) answers[ask.slot] = MONEY_SLOTS.has(slot) ? `${values[slot]}元` : values[slot];
    });
    if (!asks.length || Object.keys(answers).length !== asks.length) return;
    const result = await post("/api/clarify", { answers });
    if (result.status === 200) {
      timelineAdd("已按面板参数补齐澄清", "live");
      await applySubmission(result.data);
    }
  };
  if (!requestStart(start)) return;
  await start();
}

/* 加载态：与列表同骨架的占位卡，读完整体替换；失败由 panelFailure 覆盖 */
function lifeLoadingHtml() {
  const card = `<article class="life-card" aria-hidden="true">
    <span class="skeleton"></span><span class="skeleton"></span><span class="skeleton"></span>
  </article>`;
  return `<div class="life-grid" role="status" aria-live="polite">${card}${card}${card}</div>`;
}

async function refreshLifeEvents() {
  const status = el("life-events-status");
  const list = el("life-events-list");
  if (!status || !list || state.offline) return;
  /* 只有首次进入才铺骨架：`refreshOptions()` 拿到对象后会再刷一次，那一次不能闪回加载态 */
  if (!list.querySelector(".life-card")) {
    status.textContent = "正在读取生活事件原型…";
    list.innerHTML = lifeLoadingHtml();
  }
  let result;
  try {
    result = await apiFetch("GET", "/api/life-events");
  } catch (_) {
    status.textContent = "生活事件读取失败，请稍后重试。";
    panelFailure("life-events-list", "生活事件读取失败，请稍后重试。", "life");
    return;
  }
  if (!result.ok) {
    status.textContent = "生活事件读取失败，请稍后重试。";
    panelFailure("life-events-list", "生活事件读取失败，请稍后重试。", "life");
    return;
  }
  const payload = result.data || {};
  const prototypes = Array.isArray(payload.prototypes) ? payload.prototypes : [];
  const combos = Array.isArray(payload.combos) ? payload.combos : [];
  const records = Array.isArray(payload.records) ? payload.records : [];
  const recordsNote = payload.records_available === false
    ? " · 本部署未开启记录"
    : ` · 我的记录 ${records.length} 条`;
  status.textContent = `原型 ${prototypes.length} 个 · 组合律 ${combos.length} 个${recordsNote}`;

  const cards = prototypes.map((prototype) => {
    const eventId = typeof prototype.event_id === "string" ? prototype.event_id : "";
    const name = typeof prototype.name === "string" ? prototype.name : "";
    const description = typeof prototype.description === "string" ? prototype.description : "";
    const example = typeof prototype.example === "string" ? prototype.example : "";
    const steps = typeof prototype.steps === "number" ? `${prototype.steps} 步` : "";
    const dag = lifeDagHtml(prototype.skeleton);
    const fields = LIFE_FORMS[eventId] || [];
    const form = fields.length
      ? `<details class="life-form"><summary>填写参数</summary><div class="life-form__grid">
          ${fields.map((field) => `<label class="life-form__field"><span>${esc(field.label)}</span>${lifeFieldHtml(eventId, field)}</label>`).join("")}
        </div></details>`
      : "";
    return `<article class="life-card" data-event="${esc(eventId)}">
      <div class="life-card__head"><b>${esc(name || eventId)}<span class="tech mono"> · ${esc(eventId)}</span></b><span class="life-card__steps">${esc(steps)}</span></div>
      <p class="life-card__desc">${esc(description)}</p>
      ${dag}
      <p class="life-card__example">示例：${esc(example)}</p>
      ${form}
      <div class="life-card__foot">
        <button class="life-send" type="button" data-life-send="${esc(example)}" data-event="${esc(eventId)}">发起</button>
      </div>
    </article>`;
  }).join("");

  const recordRows = records.length
    ? records.map((record) => {
      const label = LIFE_EVENT_STATUS[record.status] || record.status;
      const amount = typeof record.amount_cents === "number" ? ` · ${yuan(record.amount_cents)}` : "";
      return `<article class="takeover insight-item">
      <div><b>${esc(record.event_name || record.event_id)}</b> <span class="chip">${esc(label)}</span></div>
      <p class="muted">${esc(momentLabel(record.updated_at || record.created_at))}${esc(amount)}</p>
      ${lifeBudgetHtml(record.budget)}
      <p class="muted">会话 <span class="tech">${esc(record.session_id)}</span>
        <button class="life-send" type="button" data-life-open="${esc(record.session_id)}">打开会话</button></p>
    </article>`;
    }).join("")
    : '<p class="muted">还没有生活事件记录；发起后会出现在这里。</p>';

  list.innerHTML = `<div class="life-grid">${cards || '<article class="state-card"><p class="state-card__k">生活事件 · 暂无原型</p><p class="muted">当前服务端没有可用的生活事件原型；这是空态，不是读取失败。</p></article>'}</div>
    <p class="side__label">我的生活事件</p>
    <div class="life-records">${recordRows}</div>`;
  /* 对象槽默认取第一个真实选项，避免「填了金额却卡在选卡片」 */
  Array.from(list.querySelectorAll("select[data-life-field]")).forEach((node) => {
    if (node.value) return;
    const first = Array.from(node.options).find((option) => option.value);
    if (first) node.value = first.value;
  });
}

/* ---------------- 装配 ---------------- */

renderPresets();
function onPresetClick(event) {
  const button = event.target.closest("[data-preset]");
  const utterance = el("utterance");
  if (button && utterance) utterance.value = button.dataset.preset;
}
el("presets")?.addEventListener("click", onPresetClick);
/* 生活事件卡片：面板参数 → 同一 `/api/submit`；仍缺槽位时用同一份参数自动续答 */
el("life-events-list").addEventListener("click", async (event) => {
  const target = event.target instanceof Element ? event.target : null;
  if (!target) return;
  const send = target.closest("[data-life-send]");
  if (send) {
    const card = send.closest("article.life-card");
    const form = card ? card.querySelector(".life-form") : null;
    send.disabled = true;
    try {
      await runLifeEvent(send.dataset.event || "", form);
    } finally {
      send.disabled = false;
    }
    return;
  }
  const open = target.closest("[data-life-open]");
  if (open) await openConversation(open.dataset.lifeOpen || "");
});
el("btn-submit").addEventListener("click", action(onSubmit));
el("btn-due").addEventListener("click", action(onDue));
el("btn-confirm").addEventListener("click", action(onConfirm));
el("btn-interrupt").addEventListener("click", action(onInterrupt));
el("btn-resume").addEventListener("click", action(onResume));
el("btn-reject").addEventListener("click", action(onReject));
el("btn-reverse").addEventListener("click", action(onReverse));
el("btn-takeover").addEventListener("click", action(onTakeover));
el("btn-reset").addEventListener("click", action(startFreshConversation));
el("btn-profile").addEventListener("click", openProfile);
el("btn-close-profile").addEventListener("click", closeProfile);
/* 个人中心内容动态渲染：退出登录按钮用事件委托（抽屉与「我的」视图共用） */
async function onProfileClick(event) {
  const target = event.target instanceof Element ? event.target : null;
  if (!target || target.id !== "btn-logout") return;
  try {
    await apiFetch("POST", "/api/auth/logout", {});
  } catch (_) {
    /* 退出失败也回登录页：Cookie 可能已失效 */
  }
  window.location.replace("/");
}
el("profile-body").addEventListener("click", onProfileClick);
/* 历史对话行同样动态渲染：点开 / 删除都用事件委托 */
function onConversationClick(event) {
  const target = event.target instanceof Element ? event.target : null;
  if (!target) return;
  const openButton = target.closest("[data-open-session]");
  if (openButton) {
    if (openButton.closest("#home-recent-list")) navigate("chat", {});
    openConversation(openButton.dataset.openSession).catch(() => {
      timelineAdd("历史对话读取失败，请稍后重试。", "warn");
    });
    return;
  }
  const deleteButton = target.closest("[data-delete-session]");
  if (deleteButton) {
    deleteConversation(deleteButton.dataset.deleteSession).catch(() => {
      timelineAdd("删除失败，请稍后重试。", "warn");
    });
  }
}
el("conversation-list").addEventListener("click", onConversationClick);
el("home-recent-list")?.addEventListener("click", onConversationClick);
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
  syncRailAria();
  if (open && name === "scenes") el("btn-reset")?.focus();
  if (open && name === "trace") el("btn-close-trace")?.focus();
}

/* 桌面端侧栏与处理详情是常驻可见的，`aria-hidden` 不能写死 true，
   否则读屏用户拿不到理财面板；视口变化时重算一次。
   左栏在 >600px 是真实一列；右栏在 >900px 才是常驻列（≤900 走抽屉）。 */
function syncRailAria() {
  if (!app) return;
  const navPersistent = window.matchMedia("(min-width: 601px)").matches;
  const tracePersistent = window.matchMedia("(min-width: 901px)").matches;
  el("conversation-sidebar").setAttribute(
    "aria-hidden",
    String(!app.classList.contains("is-scenes-open") && !navPersistent),
  );
  el("trace").setAttribute(
    "aria-hidden",
    String(!app.classList.contains("is-trace-open") && !tracePersistent),
  );
}

function isNarrowNav() {
  return isAppShell();
}

/* APP 版式（≤600px）与网页版式的分界，和 workspace.css 的媒体查询保持一致 */
function isAppShell() {
  return window.matchMedia("(max-width: 600px)").matches;
}

function isNarrowTrace() {
  return window.matchMedia("(max-width: 900px)").matches;
}

/* 顶栏两个按钮：宽屏收起整列，窄屏走抽屉；状态同步到 aria-expanded */
function syncColumnButtons() {
  if (!app) return;
  const navOpen = isNarrowNav()
    ? app.classList.contains("is-scenes-open")
    : !app.classList.contains("is-nav-collapsed");
  const traceOpen = isNarrowTrace()
    ? app.classList.contains("is-trace-open")
    : !app.classList.contains("is-trace-collapsed");
  el("btn-scenes-mobile")?.setAttribute("aria-expanded", String(navOpen));
  el("btn-trace-mobile")?.setAttribute("aria-expanded", String(traceOpen));
}

function toggleNav() {
  if (!app) return;
  if (isNarrowNav()) setMobileDrawer("scenes", !app.classList.contains("is-scenes-open"));
  else app.classList.toggle("is-nav-collapsed");
  syncColumnButtons();
}

function toggleTrace() {
  if (!app) return;
  if (isNarrowTrace()) setMobileDrawer("trace", !app.classList.contains("is-trace-open"));
  else app.classList.toggle("is-trace-collapsed");
  syncColumnButtons();
}

el("btn-scenes-mobile")?.addEventListener("click", toggleNav);
el("btn-trace-mobile")?.addEventListener("click", toggleTrace);
setMobileDrawer("scenes", false);
setMobileDrawer("trace", false);
syncColumnButtons();
window.addEventListener("resize", syncColumnButtons);
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    const openDrawer = app?.classList.contains("is-scenes-open")
      ? "scenes"
      : app?.classList.contains("is-trace-open") ? "trace" : null;
    if (openDrawer) {
      setMobileDrawer(openDrawer, false);
      el(`btn-${openDrawer}-mobile`)?.focus();
    }
    closeProfile();
  }
});
el("btn-close-trace").addEventListener("click", () => {
  setMobileDrawer("trace", false);
  el("btn-trace-mobile").focus();
});
/* 澄清表单是动态渲染的，故用事件委托 */
el("stage").addEventListener("click", (event) => {
  const target = event.target instanceof Element ? event.target : null;
  const trigger = target ? target.closest("[data-action]") : null;
  if (trigger && trigger.dataset.action === "open-subscriptions") {
    setMobileDrawer("trace", true);
    const panel = el("subscription-panel");
    if (panel) panel.scrollIntoView({ behavior: "smooth", block: "start" });
    return;
  }
  if (event.target && event.target.id === "btn-clarify") action(onClarify)();
  if (event.target && event.target.id === "btn-reverify") action(requestVerification)();
  if (event.target && event.target.id === "btn-refresh-session") action(refreshSession)();
});

/* 问卷表单在「面板」和「聊天内」两处动态渲染，故用文档级事件委托 */
document.addEventListener("click", async (event) => {
  const target = event.target instanceof Element ? event.target : null;
  const trigger = target ? target.closest('[data-action="submit-assessment"]') : null;
  if (!trigger) return;
  const form = trigger.closest("[data-assessment-form]");
  if (!form || form.dataset.busy === "1") return;
  form.dataset.busy = "1";
  trigger.disabled = true;
  try {
    await onAssessmentSubmit(form);
  } catch (_) {
    const error = form.querySelector("[data-form-error]");
    if (error) {
      error.hidden = false;
      error.textContent = "提交失败：未收到服务端回执，请稍后重试。";
    }
  } finally {
    form.dataset.busy = "0";
    trigger.disabled = false;
  }
});

/* 理财入口与卡片按钮：快捷场景和处理详情都是动态渲染，故用文档级事件委托 */
document.addEventListener("click", async (event) => {
  const target = event.target instanceof Element ? event.target : null;
  if (!target) return;
  if (target.closest('[data-action="open-wealth"]')) {
    setMobileDrawer("trace", true);
    const panel = el("wealth-panel");
    if (panel) panel.scrollIntoView({ behavior: "smooth", block: "start" });
    return;
  }
  const trigger = target.closest('[data-action="wealth-subscribe"], [data-action="wealth-redeem"]');
  if (!trigger || trigger.disabled) return;
  trigger.disabled = true;
  try {
    await onWealthAction(trigger);
  } catch (_) {
    wealthError(trigger, "提交失败：未收到服务端回执，请稍后重试。");
  } finally {
    trigger.disabled = false;
    /* 卡片按钮直接起办理时不经过 `action()` 包装，需在此补一次按钮态：
       否则 `beginTask` 期间被隐藏的 #task-actions 不会恢复，确认按钮点不到。 */
    setButtons();
  }
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

/* ---------------- 视图路由（首页 / 对话 / 场景 / 记录 / 渠道 / 我的） ----------------
   示例 IA 的落地：左侧栏是全局导航，对话保留原三栏布局；其余视图只占「左栏 + 主区」。
   场景页的内容由各场景负责人填进 `[data-scene-block]`，这里只负责切页与入口。 */

const SCENES = {
  transfer: {
    title: "转账",
    sub: "中文数字、完整手机号、日累计限额；大额与不可逆动作走强验证。",
    short: "额度、收款人；说一句话去对话",
    prefill: "转给李娜 500 元",
  },
  bills: {
    title: "账单分析",
    sub: "重复扣款 / 涨价 / 隐藏费用 / 深夜 / 异地；带置信度与免责说明。",
    short: "异常打标 + 置信度与免责",
    prefill: "查一下上月账单",
  },
  wealth: {
    title: "理财",
    sub: "风险测评 → 适当性 → 申购 / 赎回；超等级产品灰显并写原因。",
    short: "测评、按等级灰显与持仓",
    prefill: "查余额",
  },
  cards: {
    title: "卡片",
    sub: "锁卡 / 挂失 / 补卡 / 调额；红动作走强验证。",
    short: "锁卡 / 挂失 / 补卡 / 调额",
    prefill: "锁卡",
  },
  subscriptions: {
    title: "订阅",
    sub: "涨价、下次扣款日与扣款记录可查；取消前说清本地 / 商户边界。",
    short: "涨价与下次扣款可查",
    prefill: "订阅取消",
  },
  life: {
    title: "生活事件",
    sub: "一句话串多个域，每步一张卡、逐张确认。",
    short: "一句话串多个域，逐张确认",
    prefill: "",
  },
};

/* 窄屏首页的 2 列场景宫格（与「原型-app」一致）：图标 + 名称 + 一句说明 */
function renderHomeSceneList() {
  const host = el("home-scene-list");
  if (!host) return;
  host.innerHTML = Object.keys(SCENES).map((key) => {
    const spec = SCENES[key];
    return `<button class="home-tile" type="button" data-nav="scene" data-scene="${esc(key)}">
      <span class="home-tile__h"><span class="home-tile__ico" aria-hidden="true">${esc(spec.title.slice(0, 1))}</span>${esc(spec.title)}</span>
      <span class="home-tile__d">${esc(spec.short)}</span>
    </button>`;
  }).join("");
}
renderHomeSceneList();

/* 首页余额卡：分与元的格式化只做展示，数值一律来自服务端；读不到就保持「—」。 */
function yuanText(cents) {
  const [intPart, decPart] = ((Number(cents) || 0) / 100).toFixed(2).split(".");
  return `¥${intPart.replace(/\B(?=(\d{3})+(?!\d))/g, ",")}.${decPart}`;
}

async function refreshHomeBalance() {
  const totalEl = el("home-balance-total");
  if (!totalEl) return;
  try {
    const [optionsRes, cardsRes] = await Promise.all([
      fetch("/api/options", { cache: "no-store" }),
      fetch("/api/cards", { cache: "no-store" }),
    ]);
    if (!optionsRes.ok || !cardsRes.ok) throw new Error("读取余额失败");
    const options = await optionsRes.json();
    const cards = await cardsRes.json();
    const accounts = options.accounts || [];
    const centsOf = (item) => Number(item.balance_cents) || 0;
    const total = accounts.reduce((sum, item) => sum + centsOf(item), 0);
    const current = accounts
      .filter((item) => String(item.label || "").includes("活期"))
      .reduce((sum, item) => sum + centsOf(item), 0);
    const limit = (cards.cards || []).reduce(
      (sum, card) => sum + (Number(card.daily_limit_cents) || 0),
      0
    );
    totalEl.textContent = yuanText(total);
    el("home-balance-current").textContent = yuanText(current);
    el("home-balance-limit").textContent = yuanText(limit);
  } catch {
    /* 保持「—」：不编造金额 */
    totalEl.textContent = "—";
  }
}
void refreshHomeBalance();

const RECORD_TABS = {
  transfers: { label: "转账记录", hint: "转账完成后，这里显示收款人（脱敏）、金额、状态与业务流水渠道。" },
  holdings: { label: "持仓", hint: "理财申购后的持仓（产品 / 金额 / 起息日）；无净值数据时收益标「未知」。" },
  cards: { label: "卡片状态", hint: "锁卡 / 挂失 / 补卡后的最新状态与可执行动作。" },
  suspicions: { label: "可疑清单", hint: "账单异常标记、置信度与「不当作已确认」的免责说明。" },
  charges: { label: "扣款记录", hint: "订阅扣款证据：商户 / 金额 / 时间 / 渠道 / 记录号。" },
};

const RECORD_TAB_LOADERS = {
  transfers: () => apiFetch("GET", "/api/transfers"),
  holdings: () => apiFetch("GET", "/api/wealth/overview"),
  cards: () => apiFetch("GET", "/api/cards"),
  suspicions: () => apiFetch("GET", "/api/bills/analysis"),
  charges: () => (state.options
    ? Promise.resolve({ ok: true, status: 200, data: state.options })
    : apiFetch("GET", "/api/options")),
};

const RECORD_HEADERS = {
  transfers: ["记账日", "收款人", "金额", "状态", "渠道", "记录号"],
  holdings: ["产品", "风险等级", "持仓金额", "起息日", "当前收益", "渠道"],
  cards: ["卡号", "类型", "状态", "日限额", "渠道"],
  suspicions: ["可疑类型", "状态", "置信度", "影响金额", "证据商户", "渠道"],
  charges: ["商户", "金额", "扣费日", "渠道", "记录号"],
};

let recordsRequestToken = 0;

/* 业务流水当前只记 agent / 历史 seed 渠道；web / app / feishu 的逐次区分以审计事件为准。 */
function recordChannelLabel(value) {
  const channel = String(value || "").trim();
  if (!channel) return "—";
  return ({ web: "网页", app: "手机", feishu: "飞书", agent: "AI 办理" })[channel] || channel;
}

function recordsRow(cells) {
  return `<tr>${cells.map((cell) => `<td>${cell}</td>`).join("")}</tr>`;
}

function recordsTableHtml(headers, rows) {
  return `<div class="records-table-wrap"><table class="records-table">
    <thead><tr>${headers.map((item) => `<th>${esc(item)}</th>`).join("")}</tr></thead>
    <tbody>${rows.join("")}</tbody></table></div>`;
}

function recordsLoadingHtml(spec) {
  return `<div class="records-skeleton" role="status" aria-live="polite">
    <p class="muted">正在读取${esc(spec.label)}…</p>
    <span class="skeleton"></span><span class="skeleton"></span><span class="skeleton"></span>
  </div>`;
}

function recordsEmptyHtml(spec) {
  return `<article class="state-card">
    <p class="state-card__k">${esc(spec.label)} · 暂无记录</p>
    <p class="muted">${esc(spec.hint)}</p>
  </article>`;
}

function recordsErrorHtml(message) {
  return `<article class="state-card">
    <p class="state-card__k">读取失败</p>
    <p class="muted">${esc(message)}</p>
    <button class="btn btn--sm" type="button" data-records-retry="1">重试</button>
  </article>`;
}

function recordsRowsFor(key, payload) {
  const data = payload || {};
  if (key === "transfers") {
    return (data.items || []).map((item) => recordsRow([
      `<span class="mono">${esc(item.booked_on || "—")}</span>`,
      esc(item.payee || "—"),
      `<span class="mono">${yuanText(item.amount_cents || 0)}</span>`,
      esc(item.status || "—"),
      esc(recordChannelLabel(item.channel)),
      `<span class="tech">${esc(item.record_id || "—")}</span>`,
    ]));
  }
  if (key === "holdings") {
    return (data.holdings || []).map((item) => {
      const income = String(item.current_income || "");
      /* 收益列只给短标；「无净值数据」的完整口径放进 title，不占列宽 */
      const incomeCell = incomeUnknown(item)
        ? `<span title="${esc(income)}">未知</span>`
        : esc(income || "—");
      return recordsRow([
        esc(item.name || "—"),
        esc(item.risk_level || "—"),
        `<span class="mono">${esc(item.amount_yuan || "0")} 元</span>`,
        `<span class="mono">${esc(item.purchased_on || "—")}</span>`,
        incomeCell,
        esc(recordChannelLabel(item.channel)),
      ]);
    });
  }
  if (key === "cards") {
    return (data.cards || []).map((item) => recordsRow([
      `<span class="mono">${esc(item.card_id || "—")}</span>`,
      esc(item.type_label || "—"),
      esc(item.status_label || "—"),
      `<span class="mono">${yuan(item.daily_limit_cents || 0)}</span>`,
      "—",
    ]));
  }
  if (key === "suspicions") {
    return (data.findings || []).map((item) => {
      const evidenceLines = (item.evidence || []).map((row) => {
        const moment = row.booked_at ? String(row.booked_at).slice(11, 16) : "";
        return `${esc(row.booked_on)}${moment ? " " + esc(moment) : ""} · ${esc(row.city || "地点未知")} · ${esc(row.merchant_label || "—")}`;
      }).join("<br>");
      return recordsRow([
        esc(FINDING_LABELS[item.kind] || item.kind),
        esc(FINDING_STATUS[item.status] || item.status),
        esc(confidenceLabel(item.confidence)),
        `<span class="mono">${yuan(item.impact_cents || 0)}</span>`,
        `<span class="mono">${evidenceLines || "—"}</span>`,
        "—",
      ]);
    });
  }
  const rows = [];
  (data.subscription_details || []).forEach((item) => {
    (item.evidence || []).forEach((evidence) => {
      rows.push(recordsRow([
        esc(item.merchant || "—"),
        `<span class="mono">${yuan(Math.abs(Number(evidence.amount_cents) || 0))}</span>`,
        `<span class="mono">${esc(evidence.booked_on || "—")}</span>`,
        esc(recordChannelLabel(evidence.channel)),
        `<span class="tech">${esc(evidence.record_id || "—")}</span>`,
      ]));
    });
  });
  return rows;
}

const PANEL_RETRY = {
  transfers: () => refreshTransfers(),
  wealth: () => refreshWealth(),
  cards: () => refreshCards(),
  bills: () => refreshBills(),
  subscriptions: () => refreshOptions(),
  life: () => refreshLifeEvents(),
};

function panelFailure(containerId, message, retryKey) {
  const box = el(containerId);
  if (!box) return;
  box.innerHTML = `<article class="state-card">
    <p class="state-card__k">读取失败</p>
    <p class="muted">${esc(message)}</p>
    <button class="btn btn--sm" type="button" data-panel-retry="${esc(retryKey)}">重试</button>
  </article>`;
}

const VIEWS = ["home", "chat", "scene", "records", "health", "me"];
let currentScene = "transfer";
let currentRecordTab = "transfers";

function parseHash() {
  const raw = String(window.location.hash || "").replace(/^#\/?/, "");
  const parts = raw.split("/");
  const name = VIEWS.includes(parts[0]) ? parts[0] : "chat";
  return { name, arg: parts[1] || "" };
}

async function renderRecordsTab(tab) {
  const key = Object.prototype.hasOwnProperty.call(RECORD_TABS, tab) ? tab : "transfers";
  currentRecordTab = key;
  const spec = RECORD_TABS[key];
  document.querySelectorAll("#records-tabs [data-record-tab]").forEach((button) => {
    button.setAttribute("aria-selected", String(button.dataset.recordTab === key));
  });
  const body = el("records-body");
  if (!body) return;
  if (key === "charges") {
    renderChargeRecords(body);
    return;
  }
  const token = ++recordsRequestToken;
  body.innerHTML = recordsLoadingHtml(spec);
  let result;
  try {
    result = await RECORD_TAB_LOADERS[key]();
  } catch (_) {
    if (token === recordsRequestToken) {
      body.innerHTML = recordsErrorHtml(`${spec.label}读取失败，请稍后重试。`);
    }
    return;
  }
  if (token !== recordsRequestToken) return;
  if (!result.ok) {
    body.innerHTML = recordsErrorHtml(`${spec.label}读取失败，请稍后重试。`);
    return;
  }
  const rows = recordsRowsFor(key, result.data || {});
  const note = key === "suspicions"
    ? `<p class="muted records-note">识别结果只作核对线索，不当作已确认；渠道列无来源时显示「—」。数据来源：${esc((result.data || {}).data_source || "服务端事实")}。</p>`
    : "";
  body.innerHTML = rows.length
    ? `<p class="muted records-count">共 ${rows.length} 条 · 数据来自服务端事实</p>` +
      recordsTableHtml(RECORD_HEADERS[key], rows) + note
    : recordsEmptyHtml(spec);
}

function renderChargeRecords(body) {
  const details = state.options && state.options.subscription_details;
  let message = "";
  if (state.optionsStatus === "loading") message = "正在读取扣款记录…";
  else if (state.optionsStatus === "error" || !details) message = "扣款记录暂不可用，请稍后重试。";
  const seen = new Set();
  const records = [];
  if (!message) {
    for (const item of details.insights || []) {
      for (const txn of item.evidence || []) {
        if (seen.has(txn.record_id)) continue;
        seen.add(txn.record_id);
        records.push(txn);
      }
    }
    if (!records.length) message = "暂无可核对的订阅扣款记录。";
  }
  body.innerHTML = `<p class="muted" role="status">${esc(message || `共 ${records.length} 条订阅扣款记录`)}</p>` +
    '<div class="btn-row"><button class="btn btn--sm" type="button" data-action="refresh-subscriptions">刷新订阅数据</button></div>';
  if (message) return;
  body.innerHTML += `<div class="charge-records-table-wrap" tabindex="0" aria-label="订阅扣款记录">
    <table class="charge-records-table"><thead><tr><th>商户</th><th>金额</th><th>时间</th><th>卡尾号</th><th>渠道</th><th>记录 ID</th><th>内容哈希</th></tr></thead>
    <tbody>${records.map((txn) => `<tr><td>${esc(txn.merchant_label)}</td><td>${yuan(Math.abs(Number(txn.amount_cents) || 0))}</td>` +
      `<td>${esc(txn.booked_at || txn.booked_on)}</td><td>${esc(txn.card_tail || "未提供")}</td>` +
      `<td>${esc(txn.channel || "未记录")}</td><td class="mono">${esc(txn.record_id)}</td>` +
      `<td class="mono" title="${esc(txn.content_hash)}">${esc(txn.content_hash.slice(0, 12))}…</td></tr>`).join("")}</tbody></table></div>`;
}

function showScene(key) {
  currentScene = Object.prototype.hasOwnProperty.call(SCENES, key) ? key : "transfer";
  const spec = SCENES[currentScene];
  const title = el("scene-title");
  const sub = el("scene-sub");
  if (title) title.textContent = spec.title;
  if (sub) sub.textContent = spec.sub;
  document.querySelectorAll("[data-scene-block]").forEach((block) => {
    block.hidden = block.dataset.sceneBlock !== currentScene;
  });
}

function showView(name, options) {
  const next = VIEWS.includes(name) ? name : "home";
  const opts = options || {};
  const appNode = el("app");
  if (appNode) appNode.dataset.viewName = next;
  VIEWS.forEach((view) => {
    const node = el(`view-${view}`);
    if (node) node.hidden = view !== next;
  });
  if (next === "scene") showScene(opts.scene || currentScene);
  document.querySelectorAll("[data-nav]").forEach((button) => {
    const active = button.dataset.nav === next
      && (!button.dataset.scene || button.dataset.scene === currentScene);
    if (active) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  });
  if (next === "records") renderRecordsTab(opts.tab || currentRecordTab);
  if (next === "health") refreshChannelHealth().catch(() => {});
  /* 窄屏「我的」是独立一页；桌面仍走弹窗，这里只在页面视图可见时补数据 */
  if (next === "me") loadProfile(el("view-me-body"), "正在读取账户信息…");
  if (next === "chat") {
    const input = el("utterance");
    if (input) window.setTimeout(() => input.focus(), 0);
  }
}

function navigate(name, options) {
  const opts = options || {};
  const suffix = name === "scene"
    ? `/${opts.scene || currentScene}`
    : name === "records"
      ? `/${opts.tab || currentRecordTab}`
      : "";
  const hash = `#/${name}${suffix}`;
  if (window.location.hash !== hash) window.location.hash = hash;
  showView(name, opts);
}

document.addEventListener("click", (event) => {
  const target = event.target instanceof Element ? event.target : null;
  if (!target) return;
  if (target.closest('[data-action="refresh-subscriptions"]')) {
    refreshOptions().catch(() => {});
    return;
  }
  if (target.id === "profile-backdrop") {
    closeProfile();
    return;
  }
  const nav = target.closest("[data-nav]");
  if (nav) {
    event.preventDefault();
    const name = nav.dataset.nav || "home";
    if (name === "me") {
      /* 窄屏（PWA 安装态）把「我的」做成整页；宽屏仍是居中弹窗 */
      if (isAppShell()) navigate("me", {});
      else openProfile();
      if (el("app")?.classList.contains("is-scenes-open")) {
        setMobileDrawer("scenes", false);
        syncColumnButtons();
      }
      return;
    }
    const opts = {};
    if (nav.dataset.scene) opts.scene = nav.dataset.scene;
    if (nav.dataset.recordTab) opts.tab = nav.dataset.recordTab;
    const prefill = nav.dataset.prefill;
    if (prefill) {
      const input = el("utterance");
      if (input) input.value = prefill;
    }
    navigate(nav.dataset.nav || "home", opts);
    if (el("app")?.classList.contains("is-scenes-open")) {
      setMobileDrawer("scenes", false);
      syncColumnButtons();
    }
    return;
  }
  const tab = target.closest("#records-tabs [data-record-tab]");
  if (tab) {
    renderRecordsTab(tab.dataset.recordTab || "transfers");
    return;
  }
  if (target.closest("[data-records-retry]")) {
    renderRecordsTab(currentRecordTab);
    return;
  }
  const retry = target.closest("[data-panel-retry]");
  if (retry) {
    const fn = PANEL_RETRY[retry.dataset.panelRetry];
    if (fn) {
      retry.disabled = true;
      Promise.resolve(fn()).catch(() => {});
    }
  }
});

window.addEventListener("hashchange", () => {
  const { name, arg } = parseHash();
  const opts = name === "scene" ? { scene: arg } : name === "records" ? { tab: arg } : {};
  showView(name, opts);
});

el("btn-scene-chat")?.addEventListener("click", () => {
  const spec = SCENES[currentScene];
  if (spec && spec.prefill) {
    const input = el("utterance");
    if (input) input.value = spec.prefill;
  }
  navigate("chat", {});
});

el("btn-health-refresh")?.addEventListener("click", () => {
  const status = el("channel-health-status");
  if (status) status.textContent = "正在刷新渠道状态…";
  refreshChannelHealth().catch(() => {
    if (status) status.textContent = "渠道状态暂不可用，请稍后重试。";
  });
});

const initialRoute = parseHash();
if (!window.location.hash) window.history.replaceState(null, "", "#/chat");
showView(
  initialRoute.name,
  initialRoute.name === "scene"
    ? { scene: initialRoute.arg }
    : initialRoute.name === "records"
      ? { tab: initialRoute.arg }
      : {},
);

timelineReset();
if (state.offline) setOfflineState(true);
setButtons();
refreshOptions().catch(() => {
  el("business-date").textContent = "演示数据读取失败，请重新加载页面。";
  const status = el("subscription-status");
  if (status) status.textContent = "订阅识别读取失败，请稍后重试。";
  panelFailure("subscription-list", "订阅识别读取失败，请稍后重试。", "subscriptions");
  timelineAdd("可选对象读取失败，请重新加载页面。", "stop");
});
refreshChannelHealth().catch(() => {
  const status = el("channel-health-status");
    if (status) status.textContent = "渠道状态暂不可用，请稍后重试。";
});
refreshInsights().catch(() => {
  el("insights-status").textContent = "主动洞察读取失败，请稍后重试。";
});
refreshTransfers().catch(() => {
  el("transfers-status").textContent = "转账记录读取失败，请稍后重试。";
  panelFailure("transfers-list", "转账记录读取失败，请稍后重试。", "transfers");
});
refreshWealth().catch(() => {
  showWealthFailure("理财面板读取失败，请稍后重试。");
});
refreshCards().catch(() => {
  el("cards-status").textContent = "卡片读取失败，请稍后重试。";
  panelFailure("cards-list", "卡片读取失败，请稍后重试。", "cards");
});
refreshBills().catch(() => {
  el("bills-status").textContent = "账单识别读取失败，请稍后重试。";
  panelFailure("bills-findings", "账单识别读取失败，请稍后重试。", "bills");
});
refreshConversations().catch(() => {});
refreshLifeEvents().catch(() => {});
refreshProfileEntry().catch(() => {});

window.addEventListener("offline", () => setOfflineState(true));
window.addEventListener("online", () => {
  setOfflineState(false);
  refreshOptions().catch(() => {});
  refreshChannelHealth().catch(() => {});
  refreshInsights().catch(() => {});
  refreshTransfers().catch(() => {
    el("transfers-status").textContent = "转账记录读取失败，请稍后重试。";
    panelFailure("transfers-list", "转账记录读取失败，请稍后重试。", "transfers");
  });
  refreshWealth().catch(() => showWealthFailure("理财面板读取失败，请稍后重试。"));
  refreshCards().catch(() => {
    el("cards-status").textContent = "卡片读取失败，请稍后重试。";
    panelFailure("cards-list", "卡片读取失败，请稍后重试。", "cards");
  });
  refreshBills().catch(() => {
    el("bills-status").textContent = "账单识别读取失败，请稍后重试。";
    panelFailure("bills-findings", "账单识别读取失败，请稍后重试。", "bills");
  });
  refreshConversations().catch(() => {});
  refreshLifeEvents().catch(() => {});
  timelineAdd("已恢复在线；请先刷新服务端状态后继续。", "live");
  if (state.card) refreshSession().catch(() => timelineAdd("服务端状态读取失败，请稍后重试。", "warn"));
});

let installPrompt = null;
window.addEventListener("beforeinstallprompt", (event) => {
  event.preventDefault();
  installPrompt = event;
});
window.addEventListener("appinstalled", () => {
  installPrompt = null;
  const button = el("btn-install");
  const status = el("install-status");
  if (button) button.hidden = true;
  if (status) {
    status.hidden = false;
    status.textContent = "应用已安装。";
  }
});
el("btn-install")?.addEventListener("click", async () => {
  const status = el("install-status");
  if (status) status.hidden = false;
  if (!installPrompt) {
    if (status) status.textContent = "请在浏览器菜单选择安装应用或添加到主屏幕；若已安装，可直接打开应用。";
    return;
  }
  try {
    await installPrompt.prompt();
    const choice = await installPrompt.userChoice;
    if (status) status.textContent = choice.outcome === "accepted"
      ? "已提交安装，请按浏览器提示完成。" : "已取消安装，可稍后重试。";
    installPrompt = null;
  } catch {
    if (status) status.textContent = "暂时无法安装，请稍后重试或使用浏览器菜单。";
  }
});

if (typeof navigator !== "undefined" && "serviceWorker" in navigator) {
navigator.serviceWorker.register("/assets/sw.js?v=34", { scope: "/" }).catch(() => {
    timelineAdd("PWA 静态壳注册失败，当前仍可作为同源网页使用。", "warn");
  });
}
setInterval(() => {
  refreshInsights().catch(() => {});
}, 15000);

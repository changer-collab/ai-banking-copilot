/* ==========================================================================
   AI 银行副驾 · Web 渠道交互（**真实 API**）
   版式与组件词汇对齐设计仓 `demo.html` / `app.css`（凭证 / 戳记 / 步骤 / 链 / 时间线）。
   与设计仓 `demo.js` 的区别：本文件不做任何 mock——所有数据来自同源 `/api/*`。
   ========================================================================== */

const PRESETS = [
  "我的借记卡丢了",
  "转给李娜 1200 元，再查一下上月账单",
  "查一下上月账单",
  "查余额",
  "代扣签约",
  "解冻资金",
  "登记预约转账 给李娜 100 元 今天",
  "下周三出差，把卡片日限额降到 1000 元",
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
  nodeStatus: {},
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
  historyRestored: false,
  operationUnknown: false,
  insights: [],
  wealth: { products: [], holdings: [] },
  cards: [],
  bills: null,
  billsPeriod: null,
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

/* fetch 在后端不可达 / 网络中断时只会抛英文 TypeError（Failed to fetch / Load failed），
   直接显示给用户没有意义：统一换成中文说明；其余错误保留原文。 */
function fetchErrorMessage(error, fallback) {
  const raw = error && error.message ? String(error.message) : "";
  if (/Failed to fetch|Load failed|NetworkError|ERR_CONNECTION/i.test(raw)) {
    return "连不上服务端（服务可能正在重启或网络中断），请稍后重试。";
  }
  return raw || fallback;
}

async function post(path, body) {
  if (state.offline || (typeof navigator !== "undefined" && navigator.onLine === false)) {
    throw new Error("当前离线，已禁用需要在线确认的操作");
  }
  state.requestLabel = {
    "/api/submit": "正在请求生成办理计划…", "/api/clarify": "正在提交补充信息…",
    "/api/due": "正在读取到期待办…", "/api/verify/request": "正在获取验证码…",
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
    /* 请求结束后清掉「正在请求…」文案：否则计划卡下方会一直挂着上一句状态 */
    state.requestLabel = "";
    refreshConversations().catch(() => {});
  }
}

/* ---------------- 阶段步进器（挂在当前回复的头像旁） ---------------- */

const progress = { requestId: "", timer: null, startedAt: 0 };

/* 运行链路放进当前回复的消息体：运行中展开，跑完折叠成一行摘要。
   历史会话只存文本快照，因此旧回复不会带这条动态链路。 */
function runChainRoot() {
  if (!state.response) return null;
  let root = state.response.querySelector(".run-chain");
  if (!root) {
    root = document.createElement("details");
    root.className = "run-chain is-running";
    root.open = true;
    root.innerHTML = `<summary class="run-chain__summary">
        <span class="run-chain__title">运行链路</span>
        <span class="run-chain__hint" data-run-hint>准备中</span>
      </summary>
      <ol class="run-chain__steps" data-run-steps aria-live="polite"></ol>`;
    state.response.insertBefore(root, state.response.firstChild);
  }
  return root;
}

function renderProgressSteps(payload) {
  const root = runChainRoot();
  if (!root) return;
  const stages = payload && Array.isArray(payload.stages) ? payload.stages : [];
  const list = root.querySelector("[data-run-steps]");
  const hint = root.querySelector("[data-run-hint]");
  if (!list || !hint) return;
  if (!stages.length) {
    list.innerHTML = '<li class="run-chain__step run-chain__step--active">正在处理…</li>';
    hint.textContent = "进行中";
    root.classList.add("is-running");
    root.classList.remove("is-done");
    root.open = true;
    return;
  }
  list.innerHTML = stages
    .map((stage) => {
      const phaseName = stage.done ? "done" : stage.active ? "active" : "pending";
      return `<li class="run-chain__step run-chain__step--${phaseName}">${esc(stage.label)}</li>`;
    })
    .join("");
  const doneCount = stages.filter((stage) => stage.done).length;
  if (doneCount === stages.length) {
    root.classList.remove("is-running");
    root.classList.add("is-done");
    root.open = false;
    hint.textContent = `已完成 ${doneCount}/${stages.length}`;
  } else {
    root.classList.add("is-running");
    root.classList.remove("is-done");
    root.open = true;
    hint.textContent = `进行中 ${doneCount}/${stages.length}`;
  }
}

/* 请求已返回但最后一次轮询没拿到终态时，收起并标记结束（不伪造阶段结果） */
function settleRunChain() {
  if (!state.response) return;
  const root = state.response.querySelector(".run-chain");
  if (!root) return;
  const hint = root.querySelector("[data-run-hint]");
  root.classList.remove("is-running");
  root.classList.add("is-done");
  root.open = false;
  if (hint && /进行中|准备中/.test(hint.textContent || "")) hint.textContent = "已结束";
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
      settleRunChain();
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
  renderQuota(state.options.quota);
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

/* 等级条：等级由服务端算分定级，前端只展示；「重新测评」只负责展开问卷。 */
function wealthGradeHtml(assessment) {
  const info = assessment || {};
  const valid = info.state === "valid";
  const level = info.risk_profile
    ? (valid ? info.risk_profile : `${info.risk_profile}（已过期）`)
    : "未测评";
  const meta = valid
    ? `测评日 ${info.assessed_on || "—"} · 有效期至 ${info.expires_on || "—"}`
    : info.state === "expired"
      ? `有效期至 ${info.expires_on || "—"}；过期后不能申购`
      : "未测评时任何产品都不能申购";
  return `<div class="wealth-grade">
    <div class="wealth-grade__row">
      <span class="wealth-grade__k">风险等级</span>
      <span class="chip ${valid ? "chip--green" : "chip--yellow"}">${esc(level)}</span>
    </div>
    <div class="wealth-grade__row">
      <span class="wealth-grade__note">${esc(meta)}</span>
      <button class="btn btn--sm wealth-grade__btn" type="button" data-wealth-retake>${
        valid ? "重新测评" : "开始答题"}</button>
    </div>
  </div>`;
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

/* 产品行：名称 + 等级 + 期限/起购/费率，右侧只放金额输入与申购；
   费率由服务端给（缺数据时是「未知」），前端不估算。不可买的产品灰显并写原因。 */
function wealthProductHtml(item) {
  const disabled = item.eligible === false;
  const min = esc(item.min_amount_yuan);
  const fee = esc(item.fee || "未知");
  const meta = `期限 ${esc(item.term_days)} 天 · 起购 ${min} 元 · 费率 ${fee}`;
  const action = disabled
    ? '<span class="chip chip--denied">不可买</span>'
    : `<input class="input input--amount" type="number" inputmode="decimal" min="${min}" step="0.01"`
      + ` placeholder="金额" data-amount-for="${esc(item.product_id)}" />`
      + `<button class="btn btn--sm btn--primary" type="button" data-action="wealth-subscribe"`
      + ` data-product="${esc(item.product_id)}" data-min="${min}">申购</button>`;
  return `<article class="wealth-row${disabled ? " wealth-row--off" : ""}">
    <div class="wealth-row__main">
      <div class="wealth-row__name"><b>${esc(item.name)}</b>
        <span class="chip">${esc(item.risk_level)}</span></div>
      <p class="wealth-row__meta">${meta}</p>
      ${item.reason ? `<p class="wealth-reason">${esc(item.reason)}</p>` : ""}
    </div>
    <div class="wealth-row__act">${action}</div>
    ${wealthWhyHtml(item)}
    <p class="muted wealth-error" data-wealth-error hidden></p>
  </article>`;
}

/* 持仓行：名称 + 一行摘要，右侧只放赎回；不画空行（记录号本身在 .tech 里默认隐藏）。 */
function wealthHoldingHtml(item) {
  const max = esc(item.amount_yuan);
  return `<article class="wealth-row">
    <div class="wealth-row__main">
      <div class="wealth-row__name"><b>${esc(item.name)}</b>
        <span class="chip">${esc(item.risk_level)}</span></div>
      <p class="wealth-row__meta">金额 ${esc(item.amount_yuan)} 元 · 起息 ${esc(item.purchased_on)}` +
        ` · 收益 ${esc(item.current_income)}</p>
    </div>
    <div class="wealth-row__act">
      <input class="input input--amount" type="number" inputmode="decimal" min="0.01" max="${max}" step="0.01"
        placeholder="金额" data-amount-for="${esc(item.product_id)}" />
      <button class="btn btn--sm" type="button" data-action="wealth-redeem"
        data-product="${esc(item.product_id)}" data-max="${max}">赎回</button>
    </div>
    <p class="muted wealth-error" data-wealth-error hidden></p>
  </article>`;
}

/* ---------------- 理财：场景页可视化（等级条 / 持仓占比 / 持仓合计） ---------------- */

function holdingsTotal(holdings) {
  return (holdings || []).reduce((sum, item) => sum + (Number(item.amount_yuan) || 0), 0);
}

/* 持仓占比：金额全部来自接口，只做本人持仓内部的相对占比（合计 100%） */
function holdingsShareHtml(holdings) {
  const rows = (holdings || []).map((item) => ({
    label: item.name || "—",
    value: Number(item.amount_yuan) || 0,
  }));
  if (!rows.length) {
    return `<div class="wealth-viz__block">
      <p class="wealth-viz__k">持仓占比</p>
      <p class="muted">当前没有持仓，暂无可计算的占比。</p>
    </div>`;
  }
  return `<div class="wealth-viz__block">
    <p class="wealth-viz__k">持仓占比</p>
    ${donutChartHtml(rows, { label: "本人持仓占比", size: 132, stroke: 15, empty: "暂无可绘制占比" })}
  </div>`;
}

function incomeUnknown(row) {
  return /未知/.test(String((row && row.current_income) || ""));
}

/* 持仓合计：金额求和；收益无净值数据就标「未知」，不补零、不推算盈亏。 */
function holdingsTotalHtml(holdings) {
  const rows = holdings || [];
  if (!rows.length) {
    return `<div class="wealth-viz__block">
      <p class="wealth-viz__k">持仓合计</p>
      <b class="wealth-total__v">¥0.00</b>
      <p class="muted">还没有理财申购记录；在上方产品行输入金额申购后，这里会出现持仓。</p>
    </div>`;
  }
  const unknown = rows.filter(incomeUnknown).length;
  const note = unknown === rows.length
    ? `收益未知（${rows.length} 笔均无净值数据，不推算盈亏）`
    : unknown
      ? `收益未知（${unknown}/${rows.length} 笔无净值数据）`
      : "收益按服务端回执展示";
  const list = rows.map((item) => `<li><span>${esc(item.name || "—")}</span>
      <em>${yuan(Math.round((Number(item.amount_yuan) || 0) * 100))}</em></li>`).join("");
  return `<div class="wealth-viz__block">
    <p class="wealth-viz__k">持仓合计</p>
    <b class="wealth-total__v">${yuan(Math.round(holdingsTotal(rows) * 100))}</b>
    <p class="muted">${esc(note)}</p>
    <ul class="wealth-total__list">${list}</ul>
  </div>`;
}

function wealthVizHtml(payload) {
  const data = payload || {};
  /* 宽屏两栏：左「持仓占比」、右「持仓合计」；窄屏上下（见 .scene-charts） */
  return `<div class="wealth-overview">
    ${wealthGradeHtml(data.assessment)}
    <div class="scene-charts">
      <section>${holdingsShareHtml(data.holdings)}</section>
      <section>${holdingsTotalHtml(data.holdings)}</section>
    </div>
  </div>`;
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
  if (status) status.textContent = "测评与持仓已读取";
  const viz = el("wealth-viz");
  if (viz) viz.innerHTML = wealthVizHtml(payload);
  const box = el("wealth-assessment-body");
  /* 问卷只放在弹窗里，页面上只留「重新测评 / 开始答题」一个入口；
     表单一经渲染就保留用户已选答案：只在没有表单时（首次 / 提交后）重建 */
  if (box && !box.querySelector("[data-assessment-form]")) {
    box.innerHTML = assessmentFormHtml("panel", assessment.questionnaire);
  }
  const products = el("wealth-products");
  if (products) {
    const rows = payload.products || [];
    products.innerHTML = rows.length
      ? rows.map((item) => wealthProductHtml(item)).join("")
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
        <p class="muted">还没有理财申购记录；在上方产品行输入金额申购后，这里会出现持仓。</p>
      </article>`;
  }
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
  const box = el("wealth-assessment-body");
  if (box) box.innerHTML = card;
  await refreshWealth();
  timelineAdd(`风险测评已提交：服务端算分定级 ${result.data.result.risk_profile}`, "done");
}

/* 测评问卷是弹窗：入口只有等级条上的「重新测评 / 开始答题」一个按钮 */
function openWealthAssessment() {
  const dialog = el("wealth-assessment");
  if (!dialog) return;
  delete dialog.dataset.dirty;
  if (typeof dialog.showModal === "function") dialog.showModal();
  else dialog.setAttribute("open", "");
}

function closeWealthAssessment() {
  closeDialog(el("wealth-assessment"));
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
  const card = trigger.closest(".wealth-row");
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
  const card = trigger.closest(".wealth-row");
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
    status.textContent = `扫描失败（${payload.scanned_at ? formatStamp(payload.scanned_at) : "未知时间"}）`;
    list.innerHTML = `<p class="muted">${esc(payload.scan_error)}；未修改既有洞察。</p>`;
    return;
  }
  status.textContent = `最近扫描：${payload.scanned_at ? formatStamp(payload.scanned_at) : "服务端时间"} · ${state.insights.length} 条`;
  renderInsightCards();
  syncBudgetInput();
}

/* 已核对 = 本机隐藏（localStorage）：不改服务端状态，换设备仍会看到 */
const INSIGHT_ACK_KEY = "aib-insight-acked";

function insightAckKeys() {
  try {
    const raw = localStorage.getItem(INSIGHT_ACK_KEY);
    const list = raw ? JSON.parse(raw) : [];
    return new Set(Array.isArray(list) ? list.map(String) : []);
  } catch (_) {
    return new Set();
  }
}

function insightKey(item) {
  return [item.rule_id, item.business_day, JSON.stringify(item.value || null)].join("|");
}

function acknowledgeInsight(key) {
  const keys = insightAckKeys();
  keys.add(key);
  try {
    localStorage.setItem(INSIGHT_ACK_KEY, JSON.stringify(Array.from(keys)));
  } catch (_) {
    /* 隐私模式下写不进去：本次仍隐藏，刷新后会再出现 */
  }
}

function renderInsightCards() {
  const list = el("insights-list");
  if (!list) return;
  const acked = insightAckKeys();
  const visible = state.insights.filter((item) => !acked.has(insightKey(item)));
  if (!visible.length) {
    list.innerHTML = state.insights.length
      ? '<p class="muted">已全部核对（仅本机隐藏）。</p>'
      : '<p class="muted">当前没有待处理洞察。</p>';
    return;
  }
  /* 同一规则的洞察合并成一张卡：卡内按条目列事实，重复的「大额待核对」不再各占一块 */
  const groups = new Map();
  visible.forEach((item) => {
    const key = item.rule_id || "other";
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(item);
  });
  list.innerHTML = Array.from(groups.entries()).map(([ruleId, items]) => {
    const setup = items.every((item) => item.status === "needs_setup");
    const count = items.length > 1 ? `<span class="insight-card__count">${items.length} 条</span>` : "";
    const suggestion = items[0] && items[0].suggestion ? items[0].suggestion : "";
    const rows = items.map((item) => {
      const budget = item.rule_id === "monthly-budget";
      const value = item.value && typeof item.value === "object" ? item.value : {};
      const hasBudget = typeof value.budget_cents === "number";
      const action = budget
        ? `<button class="insight-item__ack" type="button" data-nav="scene" data-scene="bills">${hasBudget ? "去修改" : "去设置"}</button>`
        : ruleId === "subscription-renewal"
          ? '<button class="insight-item__ack" type="button" data-nav="scene" data-scene="subscriptions">去修改</button>'
          : `<button class="insight-item__ack" type="button" data-insight-ack="${esc(insightKey(item))}">已核对</button>`;
      return `<li class="insight-item">
        ${insightFactsHtml(item)}
        <div class="insight-item__meta-row">
          <span class="muted insight-item__meta">业务日 ${esc(item.business_day || "—")}</span>
          ${action}
        </div>
      </li>`;
    }).join("");
    return `<article class="insight-card">
      <div class="insight-card__head"><b>${esc(insightRuleLabel(ruleId))}</b>
        <span class="chip">${setup ? "待设置" : "待查看"}</span>${count}</div>
      ${suggestion ? `<p class="insight-card__sug">${esc(suggestion)}</p>` : ""}
      <ul class="insight-list">${rows}</ul>
    </article>`;
  }).join("");
}

/* 账单页「本月预算」：只是提醒偏好，保存后重新扫描洞察 */
function syncBudgetInput() {
  const input = el("budget-input");
  if (!input) return;
  const status = el("budget-status");
  const budget = state.insights.find((item) => item.rule_id === "monthly-budget");
  const value = budget && budget.value && typeof budget.value === "object" ? budget.value : {};
  if (typeof value.budget_cents === "number") {
    if (document.activeElement !== input) input.value = String(value.budget_cents / 100);
    if (status) {
      status.textContent =
        `当前预算 ${yuanText(value.budget_cents)}（业务月 ${budget.business_day || "—"}）` +
        " · 仅用于提醒，不是转账限额";
    }
    return;
  }
  if (status && !input.value) {
    status.textContent = "尚未设置；保存后才会判断是否超预算（仅用于提醒，不是转账限额）。";
  }
}

async function saveBudget() {
  const input = el("budget-input");
  const status = el("budget-status");
  if (!input) return;
  const raw = (input.value || "").trim();
  const amount = Number(raw);
  if (!raw || !Number.isFinite(amount) || amount < 0) {
    if (status) status.textContent = "请输入不小于 0 的金额（元）。";
    input.focus();
    return;
  }
  const cents = Math.round(amount * 100);
  if (status) status.textContent = "正在保存…";
  const result = await apiFetch("POST", "/api/insights/budget", { budget_cents: cents });
  if (!result.ok) {
    if (status) status.textContent = (result.data && result.data.detail) || "保存失败，请稍后重试。";
    return;
  }
  renderInsights(result.data || {});
  const after = el("budget-status");
  if (after) after.textContent = `已保存：本月预算 ${yuanText(cents)} · 仅用于提醒，不是转账限额`;
  timelineAdd(`本月预算已保存：${yuanText(cents)}（仅用于提醒，不影响额度）`, "done");
}

/* 事实行：预算规则单独排成「预算 X / 支出 Y」，其余规则按金额、商户顺序排 */
function insightFactsHtml(item) {
  const value = item && item.value && typeof item.value === "object" ? item.value : {};
  if (item && item.rule_id === "monthly-budget") {
    const hasBudget = typeof value.budget_cents === "number";
    const budget = hasBudget ? yuanText(value.budget_cents) : "未设置";
    const expense = typeof value.expense_cents === "number" ? yuanText(value.expense_cents) : "—";
    const over = hasBudget
      && typeof value.expense_cents === "number"
      && value.expense_cents > value.budget_cents;
    return `<p class="insight-item__budget${over ? " is-over" : ""}">
      <span class="insight-item__k">预算</span><b class="insight-item__v">${esc(budget)}</b>
      <span class="insight-item__sep">/</span>
      <span class="insight-item__k">支出</span><b class="insight-item__v">${esc(expense)}</b>
    </p>`;
  }
  const facts = insightFacts(item);
  return facts.length
    ? `<p class="insight-item__facts">${facts.map((text) => `<span>${esc(text)}</span>`).join("")}</p>`
    : "";
}

/* 洞察事实行：只取人能读的字段（金额 / 商户 / 预算），不展示原始 JSON 与计算式 */
function insightFacts(item) {
  const value = item && item.value && typeof item.value === "object" ? item.value : {};
  const facts = [];
  const amount = typeof value.amount_cents === "number"
    ? Math.abs(value.amount_cents)
    : typeof value.expense_cents === "number" ? Math.abs(value.expense_cents) : null;
  if (amount !== null) facts.push(yuanText(amount));
  if (typeof value.merchant === "string" && value.merchant) facts.push(value.merchant);
  if (value.budget_cents === null) facts.push("预算未设置");
  else if (typeof value.budget_cents === "number") facts.push(`预算 ${yuanText(value.budget_cents)}`);
  return facts;
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
  status.textContent = `最近 ${payload.window_days} 天 · 业务日 ${payload.as_of}${items.length > 5 ? ` · 共 ${items.length} 笔` : ""}`;
  if (!items.length) {
    list.innerHTML = '<li class="muted">最近没有转账记录。</li>';
    return;
  }
  /* 场景页只留最近 5 笔：完整流水在「记录与明细」，避免同一份数据在两处堆叠 */
  const recent = items.slice(0, 5);
  const more = items.length > recent.length
    ? `<li class="muted">还有 ${items.length - recent.length} 笔，见「记录与明细」。</li>`
    : "";
  list.innerHTML = recent.map((item) => {
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
        <p class="muted">备注：${remark} <span class="mono">· 记录号 ${esc(item.record_id)}</span></p>
      </details>
    </li>`;
  }).join("") + more;
}

async function refreshTransfers() {
  const result = await apiFetch("GET", "/api/transfers");
  if (!result.ok) throw new Error("转账记录读取失败");
  renderTransfers(result.data);
}

/* ---------------- 场景页「业务面板」公共件 ----------------
   6 个场景页都是「页内直接办事」：把中文指令交给同一套 /api/submit → /api/confirm，
   页内只渲染服务端给的计划卡（summary / lines / level），不新增后端接口、不在前端算钱。 */

const SCENE_PALETTE = ["#0b4fd8", "#00a6a6", "#f0a71b", "#7a5bd8", "#3f8f4f", "#8b96a8"];

let scenePending = null;
let sceneLastUtterance = "";

function sceneColor(index) {
  return SCENE_PALETTE[index % SCENE_PALETTE.length];
}

/* 环形图：手写 SVG（不引第三方库）；rows = [{label, value, note?}] */
function donutChartHtml(rows, options) {
  const spec = options || {};
  const list = (rows || []).filter((row) => Number(row.value) > 0);
  const total = list.reduce((sum, row) => sum + Number(row.value), 0);
  if (!total) return `<p class="muted">${esc(spec.empty || "暂无可绘制数据")}</p>`;
  const size = spec.size || 156;
  const stroke = spec.stroke || 18;
  const radius = (size - stroke) / 2;
  const circumference = 2 * Math.PI * radius;
  const center = size / 2;
  let offset = 0;
  const arcs = list.map((row, index) => {
    const dash = (Number(row.value) / total) * circumference;
    const color = row.color || sceneColor(index);
    const circle = `<circle cx="${center}" cy="${center}" r="${radius}" fill="none" ` +
      `stroke="${color}" stroke-width="${stroke}" ` +
      `stroke-dasharray="${dash.toFixed(2)} ${(circumference - dash).toFixed(2)}" ` +
      `stroke-dashoffset="${(-offset).toFixed(2)}" transform="rotate(-90 ${center} ${center})" />`;
    offset += dash;
    return circle;
  }).join("");
  const legend = list.map((row, index) => {
    const pct = Math.round((Number(row.value) / total) * 100);
    return `<li><i class="chart-dot" style="background:${row.color || sceneColor(index)}"></i>` +
      `<span class="chart-legend__name">${esc(row.label)}</span>` +
      `<span class="chart-legend__val">${pct}%${row.note ? ` · ${esc(row.note)}` : ""}</span></li>`;
  }).join("");
  return `<div class="chart-donut">
    <svg viewBox="0 0 ${size} ${size}" width="${size}" height="${size}" role="img" aria-label="${esc(spec.label || "占比图")}">
      <circle cx="${center}" cy="${center}" r="${radius}" fill="none" stroke="#e8edf6" stroke-width="${stroke}" />
      ${arcs}
    </svg>
    <ul class="chart-legend">${legend}</ul>
  </div>`;
}

/* 折线图：手写 SVG；rows = [{label, value}] */
function lineChartHtml(rows, options) {
  const spec = options || {};
  const list = rows || [];
  if (!list.length) return `<p class="muted">${esc(spec.empty || "暂无趋势数据")}</p>`;
  const width = 420;
  const height = 150;
  const padL = 12;
  const padR = 12;
  const padT = 22;
  const padB = 26;
  const values = list.map((row) => Number(row.value) || 0);
  const max = Math.max(...values, 1);
  const min = Math.min(...values, 0);
  const span = max - min || 1;
  const innerW = width - padL - padR;
  const innerH = height - padT - padB;
  const stepX = list.length > 1 ? innerW / (list.length - 1) : 0;
  const points = list.map((row, index) => ({
    x: padL + stepX * index,
    y: padT + innerH * (1 - ((Number(row.value) || 0) - min) / span),
    label: row.label || "",
  }));
  const polyline = points.map((point) => `${point.x.toFixed(1)},${point.y.toFixed(1)}`).join(" ");
  const dots = points.map((point) =>
    `<circle cx="${point.x.toFixed(1)}" cy="${point.y.toFixed(1)}" r="3" fill="#0b4fd8" />`).join("");
  const labels = points.map((point) =>
    `<text x="${point.x.toFixed(1)}" y="${height - 8}" text-anchor="middle" class="chart-axis">${esc(point.label)}</text>`).join("");
  /* 点数不多时标出每个点的数值：折线缩放后仍能看清月度差异 */
  const valueLabels = spec.formatValue && list.length <= 8
    ? points.map((point, index) =>
      `<text x="${point.x.toFixed(1)}" y="${Math.max(point.y - 8, 11).toFixed(1)}" text-anchor="middle" class="chart-value">${esc(spec.formatValue(values[index]))}</text>`).join("")
    : "";
  return `<div class="chart-line">
    <svg viewBox="0 0 ${width} ${height}" role="img" aria-label="${esc(spec.label || "趋势图")}">
      <line x1="${padL}" y1="${padT + innerH}" x2="${width - padR}" y2="${padT + innerH}" stroke="#e8edf6" stroke-width="1" />
      <polyline points="${polyline}" fill="none" stroke="#0b4fd8" stroke-width="2" stroke-linejoin="round" stroke-linecap="round" />
      ${dots}${valueLabels}${labels}
    </svg>
    ${spec.note ? `<p class="muted chart-line__note">${esc(spec.note)}</p>` : ""}
  </div>`;
}

function sceneLevelChip(level) {
  const info = LEVEL[level] || LEVEL.YELLOW;
  return `<span class="chip chip--${info.cls}">${esc(info.name)} · ${esc(info.text)}</span>`;
}

/* 页内迷你计划卡：只渲染服务端给的 summary，附冻结哈希；红色不给确认按钮 */
function sceneCardHtml(card) {
  const rows = (card.summary || []).map((row) =>
    `<div class="scene-plan__row${row.emphasis ? " scene-plan__row--strong" : ""}">` +
    `<dt>${esc(row.label)}</dt><dd>${esc(row.value)}</dd></div>`).join("");
  const lines = (card.lines || []).map((line) =>
    `<li>${esc(stripNo(line))}</li>`).join("");
  const need = {
    GREEN: "纯查询，服务端已自动执行。",
    YELLOW: "确认后由服务端执行；这一步不会替您跳过确认。",
    RED: "红色动作需服务端一次性验证码，本页不代做，请到对话完成强验证。",
  }[card.level] || "按服务端裁决处理。";
  const actions = card.level === "RED"
    ? '<button class="btn btn--sm" type="button" data-scene-chat>去对话做强验证</button>'
    : card.level === "YELLOW"
      ? '<button class="btn btn--sm btn--primary" type="button" data-scene-confirm>确认执行</button>' +
        '<button class="btn btn--sm btn--ghost" type="button" data-scene-dismiss>取消</button>'
      : "";
  return `<article class="scene-plan" data-level="${esc(card.level)}">
    <div class="scene-plan__head">${sceneLevelChip(card.level)}
      <span class="mono tech">cfhash ${esc(String(card.frozen_hash || "").slice(0, 12))}</span></div>
    ${rows ? `<dl class="scene-plan__grid">${rows}</dl>` : ""}
    ${lines ? `<ul class="scene-plan__lines">${lines}</ul>` : ""}
    <p class="muted scene-plan__note">${esc(need)}</p>
    ${actions ? `<div class="btn-row">${actions}</div>` : ""}
  </article>`;
}

function renderSceneCard(card, options) {
  scenePending = { card, options };
  const host = el(options.host);
  if (host) host.innerHTML = sceneCardHtml(card);
}

function clearSceneCard() {
  if (scenePending && scenePending.options) {
    const host = el(scenePending.options.host);
    if (host) host.innerHTML = "";
    const status = scenePending.options.status ? el(scenePending.options.status) : null;
    if (status) status.textContent = "";
  }
  scenePending = null;
}

async function sceneRefresh(options) {
  const run = options && options.refresh;
  if (!run) return;
  try {
    await run();
  } catch (_) {
    /* 面板刷新失败不覆盖已拿到的执行回执 */
  }
}

async function confirmSceneCard() {
  if (!scenePending || scenePending.busy) return null;
  const { card, options } = scenePending;
  scenePending.busy = true;
  const status = options.status ? el(options.status) : null;
  const setStatus = (text) => {
    if (status) status.textContent = text;
  };
  setStatus("正在请求执行，请等待服务端回执…");
  let result;
  try {
    result = await post("/api/confirm", { card_id: card.card_id, frozen_hash: card.frozen_hash });
  } catch (error) {
    scenePending.busy = false;
    setStatus(fetchErrorMessage(error, "确认失败，请稍后重试。"));
    return null;
  }
  if (result.status !== 200) {
    scenePending.busy = false;
    setStatus(String((result.data && result.data.detail) || "确认被拒绝"));
    return null;
  }
  const data = result.data || {};
  const results = data.results || [];
  const host = el(options.host);
  if (host) {
    host.innerHTML = `<div class="scene-reply scene-reply--done" role="status">
      <p><b>${esc(data.message || "计划执行完成")}</b></p>
      ${results.length ? `<ul>${results.map((item) => `<li>${esc(resultMessage(item))}</li>`).join("")}</ul>` : ""}
      <p class="muted">回执已记录，可在「记录与明细」核对。</p></div>`;
  }
  scenePending = null;
  setStatus("");
  renderSubscriptionCancel(data.subscription_cancel);
  await sceneRefresh(options);
  return data;
}

/* 页内办事统一入口：submit → 卡片 → （绿色自动确认 / 黄色等确认 / 红色引导去对话） */
async function sceneRunTask(options) {
  const host = el(options.host);
  const status = options.status ? el(options.status) : null;
  const setStatus = (text) => {
    if (status) status.textContent = text;
  };
  sceneLastUtterance = options.utterance;
  scenePending = null;
  if (host) host.innerHTML = '<p class="muted">正在生成办理计划…</p>';
  setStatus("正在生成办理计划…");
  let result;
  try {
    result = await post("/api/submit", { utterance: options.utterance, selected: options.selected || {} });
  } catch (error) {
    if (host) host.innerHTML = "";
    setStatus(fetchErrorMessage(error, "提交失败，请稍后重试。"));
    return null;
  }
  if (result.status !== 200) {
    if (host) host.innerHTML = "";
    setStatus(String((result.data && result.data.detail) || "服务端拒绝了这次请求"));
    return null;
  }
  const data = result.data || {};
  if (data.state === "awaiting_confirm" && data.card) {
    setStatus("");
    renderSceneCard(data.card, options);
    if (data.card.level === "GREEN") return confirmSceneCard();
    return data;
  }
  const goChat = '<button class="btn btn--sm" type="button" data-scene-goto-chat>去对话补充</button>';
  if (data.state === "blocked") {
    if (host) host.innerHTML = `<div class="scene-reply scene-reply--blocked">
      <p><b>安全拒绝</b>：${esc(data.message || "未产生可执行计划")}</p>
      <p class="muted">已留痕，未执行任何动作。</p></div>`;
    setStatus("");
    return data;
  }
  if (data.state === "clarifying") {
    const labels = (data.clarifications || [])
      .map((ask) => SLOT_LABELS[baseSlot(ask.slot)] || "所需信息");
    if (host) host.innerHTML = `<div class="scene-reply">
      <p>还需要补充：${esc(Array.from(new Set(labels)).join("、") || "所需信息")}</p>
      <p class="muted">计划尚未生成，未执行任何动作。</p>${goChat}</div>`;
    setStatus("");
    return data;
  }
  if (host) host.innerHTML = `<div class="scene-reply"><p>${esc(data.message || "当前没有可执行的动作。")}</p></div>`;
  setStatus("");
  return data;
}

/* 场景①「额度卡」：限额 / 已用 / 剩余全部读 /api/options.quota（gateway 给事实），
   前端只做展示与进度百分比，不自行推导额度或已用。 */
function renderQuota(quota) {
  const status = el("quota-status");
  const body = el("quota-body");
  if (!status || !body) return;
  if (!quota) {
    status.textContent = "服务端未下发额度数据，请联系组长核对 /api/options。";
    body.innerHTML = "";
    return;
  }
  const limit = Number(quota.daily_limit_cents);
  const used = Number(quota.daily_accumulated_cents);
  if (!Number.isFinite(limit) || !Number.isFinite(used)) {
    status.textContent = "额度字段缺失，按不可用处理（不猜数字）。";
    body.innerHTML = "";
    return;
  }
  const over = used > limit;
  const remaining = limit - used;
  const pct = limit > 0 ? Math.min(Math.round((used / limit) * 100), 100) : 0;
  status.textContent = `业务日 ${quota.as_of || "—"} · 数字均读服务端`;
  body.innerHTML = `<div class="stat-metrics">
      <div class="stat-metric"><span class="stat-metric__k">本人当日限额</span>
        <b class="stat-metric__v">${yuanText(limit)}</b></div>
      <div class="stat-metric"><span class="stat-metric__k">今日已用</span>
        <b class="stat-metric__v">${yuanText(used)}</b></div>
      <div class="stat-metric${over ? " stat-metric--over" : ""}">
        <span class="stat-metric__k">${over ? "已超额" : "剩余可用"}</span>
        <b class="stat-metric__v">${yuanText(Math.abs(remaining))}</b></div>
    </div>
    <span class="bill__bar-track quota__bar${over ? " quota__bar--over" : ""}" role="img" aria-label="今日转账额度已用 ${pct}%"><i style="width:${pct}%"></i></span>
    <p class="muted">已用 ${pct}%。${over ? "超出日限额的部分按红色（需强验证）处理；" : ""}这里不在前端计算任何数字。</p>`;
}

/* 场景①快速转账：把页内表单拼成中文指令，交给与对话相同的 /api/submit */
async function submitQuickTransfer() {
  const payeeInput = el("quick-payee");
  const amountInput = el("quick-amount");
  const status = el("quick-transfer-status");
  if (!payeeInput || !amountInput) return;
  const payee = payeeInput.value.trim();
  const amount = amountInput.value.trim();
  if (!payee) {
    payeeInput.focus();
    if (status) status.textContent = "请先填写收款人。";
    return;
  }
  if (!amount || Number(amount) <= 0) {
    amountInput.focus();
    if (status) status.textContent = "请先填写大于 0 的金额。";
    return;
  }
  await sceneRunTask({
    utterance: `转给${payee} ${amount} 元`,
    host: "quick-transfer-card",
    status: "quick-transfer-status",
    refresh: refreshAfterExecution,
  });
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

/* 只列出真实存在的对外渠道：网页端 + 飞书。
   本地 IM 只是服务端演示用的内部信道，不对外呈现，故不再出现在表格里。 */
const CHANNEL_ORDER = ["web", "feishu"];
const CHANNEL_LABELS = { web: "网页", feishu: "飞书" };
const CHANNEL_STATE_LABELS = {
  web: "网页内直连",
  local: "本地演示",
  adapter: "已接入",
  stub: "未接入",
  replay: "回放模式",
  ok: "正常",
  degraded: "降级",
  unknown: "未知",
};

function channelStateLabel(item, stats) {
  const raw = item.mode || stats.status || "";
  return CHANNEL_STATE_LABELS[raw] || raw || "未知";
}

/* 服务端时间戳（ISO 8601，带时区）→ 本地时区 `YYYY-MM-DD HH:mm:ss`；
   解析失败原样展示，空值给「暂无事件」——不编造时间。 */
function formatStamp(value) {
  const text = String(value || "").trim();
  if (!text) return "暂无事件";
  const date = new Date(text);
  if (Number.isNaN(date.getTime())) return text;
  const pad = (n) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ` +
    `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

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
    const state = configured ? channelStateLabel(item, stats) : "未接入";
    const lastEvent = configured ? formatStamp(stats.last_event_at) : "—";
    const replay = stats.replay_mode || item.mode === "replay" ? "是" : "否";
    return `<tr><td>${esc(CHANNEL_LABELS[name] || name)}</td><td>${esc(state)}</td><td>${esc(lastEvent)}</td>` +
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

/* 订阅卡（S2）：顶部色条 + 商户 / 金额 / 周期 + 下次扣款日 + 取消，其余依据收进折叠区。
   金额与日期全部来自服务端；涨价只做角标，不替代「识别说明」里的口径。 */
function subscriptionCardHtml(item, index) {
  const cancelled = item.registered_status === "cancelled";
  const next = item.next_charge_on
    ? `${esc(item.next_charge_on)}${item.next_charge_is_prediction ? "（预测）" : ""}`
    : "未提供";
  const price = item.price_increased_12m === true
    ? `近 12 个月涨价：${yuan(item.price_change_12m_from_cents)} → ${yuan(item.price_change_12m_to_cents)}`
    : item.price_increased_12m === false ? "近 12 个月未涨价" : "样本不足";
  const external = item.external_cancel_status === "not_integrated"
    ? "未接入（仅本地模拟或商户自助）" : esc(item.external_cancel_status || "—");
  const suspect = item.status === "confirmed" ? "" : '<span class="chip chip--yellow">疑似</span>';
  const raised = !cancelled && item.price_increased_12m === true
    ? '<span class="sub-card__badge">涨价</span>' : "";
  const state = cancelled ? '<span class="chip">已取消</span>' : "";
  const cancelBtn = !cancelled && item.subscription_id
    ? `<button class="sub-card__cancel" type="button" data-sub-cancel="${esc(item.subscription_id)}"
        data-sub-merchant="${esc(item.merchant)}" data-sub-amount="${yuan(item.amount_cents)}"
        data-sub-cycle="${esc(item.cycle_label)}" data-sub-next="${next}"
        data-sub-last="${esc(item.last_charged_on || "")}">取消</button>`
    : "";
  return `<article class="sub-card${cancelled ? " sub-card--off" : ""}">
    <span class="sub-card__bar" aria-hidden="true"
      style="background:${cancelled ? "var(--rule-strong)" : sceneColor(index)}"></span>
    <div class="sub-card__in">
      <div class="sub-card__top">
        <span class="sub-card__merchant">${esc(item.merchant)}</span>${state}${suspect}${raised}${cancelBtn}
      </div>
      <p class="sub-card__amount">${yuan(item.amount_cents)}</p>
      <p class="sub-card__meta">${esc(item.cycle_label)}扣费 · 下次 ${next}</p>
      <div class="sub-card__foot">
        <details class="sub-card__more">
          <summary>核对依据与取消口径</summary>
          <p>${price}</p>
          <p class="muted">最近扣费 ${esc(item.last_charged_on || "—")} · 币种 ${esc(item.currency)} · 置信度 ${esc(confidenceLabel(item.confidence))}</p>
          <p class="muted">估算依据：${esc(item.next_charge_basis || "—")}</p>
          ${subscriptionNoticesHtml(item)}
          <p><span class="muted">管理建议：</span>${esc(item.suggestion)}</p>
          <p><span class="muted">取消路径：</span>${esc(item.cancel_path)}</p>
          <p class="muted">外部取消接口：${external}</p>
          ${subscriptionEvidenceHtml(item)}
        </details>
      </div>
    </div>
  </article>`;
}

/* S1 概览行：本月待扣金额 + 在订项数 + 疑似待核对；涨价单独用提示条顶出来。 */
function subscriptionAlertsHtml(insights) {
  return insights
    .filter((item) => item.registered_status !== "cancelled" && item.price_increased_12m === true)
    .map((item) => `<p class="sub-alert">
      ${esc(item.merchant)} ${yuan(item.price_change_12m_from_cents)} → ${yuan(item.price_change_12m_to_cents)}（近 12 个月涨价）</p>`)
    .join("");
}

/* 顶部概览：在订数 / 本月待扣 / 疑似待核对。
   「本月待扣」= 下次扣款日落在业务月内的在订订阅，金额取最近一期扣费（年度订阅不折月）。 */
function subscriptionSummaryHtml(insights, asOf) {
  const month = String(asOf || "").slice(0, 7);
  const active = insights.filter((item) => item.registered_status !== "cancelled");
  const dueItems = active.filter((item) => month && String(item.next_charge_on || "").slice(0, 7) === month);
  const due = dueItems.reduce((sum, item) => sum + (Number(item.amount_cents) || 0), 0);
  const suspects = active.filter((item) => item.status !== "confirmed").length;
  return `<div class="sub-overview">
      <div class="sub-overview__main">
        <span class="sub-overview__k">本月待扣（${esc(month || "业务月")}）</span>
        <b class="sub-overview__v">${yuan(due)}</b>
      </div>
      <div class="sub-overview__chips">
        <span class="chip">在订 ${active.length} 项</span>
        <span class="chip${suspects ? " chip--yellow" : ""}">疑似待核对 ${suspects} 项</span>
      </div>
    </div>
    ${subscriptionAlertsHtml(insights)}
    <p class="muted scene-hint">本月待扣 = 下次扣款日在 ${esc(month || "业务月")} 内的在订订阅金额之和；
      金额取最近一期扣费，年度订阅未折算到月。</p>`;
}

/* 未来扣款时间轴（S1）：按下一次扣款日升序，标注「预测」，最多 6 条 */
function subscriptionTimelineHtml(insights) {
  const rows = insights
    .filter((item) => item.registered_status !== "cancelled" && item.next_charge_on)
    .sort((a, b) => String(a.next_charge_on).localeCompare(String(b.next_charge_on)))
    .slice(0, 6);
  const head = '<p class="panel__title panel__title--sub">未来扣款 <span class="tech">按下次扣款日排序</span></p>';
  if (!rows.length) {
    return head + '<p class="muted">暂无可排期的未来扣款（订阅未提供下次扣款日）。</p>';
  }
  const items = rows.map((item) => `<li class="sub-ev${item.price_increased_12m === true ? " is-hot" : ""}">
      <span class="sub-ev__date">${esc(item.next_charge_on)}</span>
      <span class="sub-ev__name">${esc(item.merchant)}${
        item.next_charge_is_prediction ? ' <span class="sub-ev__tag">预测</span>' : ""}</span>
      <span class="sub-ev__amt">${yuan(item.amount_cents)}</span>
    </li>`).join("");
  return head + `<ol class="sub-ev-list">${items}</ol>
    <p class="muted scene-hint">标注「预测」的是按流水周期推算，不是商户确认的扣款日。</p>`;
}

function renderSubscriptionDetails(options) {
  const panel = el("subscription-panel");
  if (!panel) return;
  const details = options && options.subscription_details;
  const caps = options && options.subscription_capabilities;
  const status = el("subscription-status");
  const list = el("subscription-list");
  const boundary = el("subscription-boundary");
  const summary = el("subscription-summary");
  const timeline = el("subscription-timeline");
  const reset = (text) => {
    status.textContent = text;
    if (summary) summary.innerHTML = "";
    if (timeline) timeline.innerHTML = "";
    list.innerHTML = "";
    boundary.hidden = true;
  };
  if (state.optionsStatus !== "ready") {
    reset(state.optionsStatus === "error"
      ? "订阅数据暂不可用，请稍后重试。" : "正在读取订阅识别结果…");
    return;
  }
  if (!details) {
    reset("订阅识别服务尚未接入。");
    return;
  }
  status.textContent = `订阅识别结果已更新 · 业务日 ${details.as_of}`;
  if (caps && caps.boundary) {
    boundary.hidden = false;
    boundary.textContent = `能力边界：${caps.boundary}`;
  }
  const insights = details.insights || [];
  if (!insights.length) {
    if (summary) summary.innerHTML = "";
    if (timeline) timeline.innerHTML = "";
    list.innerHTML = '<p class="muted">未识别到订阅。</p>';
  } else {
    if (summary) summary.innerHTML = subscriptionSummaryHtml(insights, details.as_of);
    if (timeline) timeline.innerHTML = subscriptionTimelineHtml(insights);
    const active = insights.filter((item) => item.registered_status !== "cancelled");
    const cancelled = insights.filter((item) => item.registered_status === "cancelled");
    const activeHtml = active.length
      ? `<p class="panel__title panel__title--sub">在订 <span class="tech">${active.length} 项</span></p>
        <div class="sub-wall">${active.map((item) =>
          subscriptionCardHtml(item, insights.indexOf(item))).join("")}</div>`
      : '<p class="panel__title panel__title--sub">在订</p><p class="muted">当前没有在订订阅。</p>';
    const cancelledHtml = cancelled.length
      ? `<p class="panel__title panel__title--sub">已取消 <span class="tech">${cancelled.length} 项</span></p>
        <div class="sub-wall">${cancelled.map((item) =>
          subscriptionCardHtml(item, insights.indexOf(item))).join("")}</div>`
      : "";
    list.innerHTML = activeHtml + cancelledHtml;
  }
  const notes = details.notes || [];
  if (notes.length) {
    list.innerHTML += `<details class="scene-note"><summary>识别说明（${notes.length}）</summary><ul>${
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

/* 卡面：类型 / 状态 / 卡号 / 日限额 + 横向对比条 + 四键快捷操作。
   进度条只表示「相对最高日限额」的横向对比——流水未记录卡号，服务端没有按卡的当日已用，绝不编造。 */
function cardFaceHtml(card, maxLimit) {
  const id = esc(card.card_id);
  const limit = Number(card.daily_limit_cents) || 0;
  const pct = maxLimit > 0 ? Math.round((limit / maxLimit) * 100) : 0;
  const off = card.status !== "active";
  const keys = [
    off
      ? `<button class="btn btn--sm" type="button" data-card-action="unlock" data-card-id="${id}">解锁</button>`
      : `<button class="btn btn--sm" type="button" data-card-action="lock" data-card-id="${id}">锁卡</button>`,
    `<button class="btn btn--sm" type="button" data-card-action="lost" data-card-id="${id}">挂失</button>`,
    `<button class="btn btn--sm" type="button" data-card-action="limit" data-card-id="${id}">调额</button>`,
    `<button class="btn btn--sm" type="button" data-card-action="virtual" data-card-id="${id}">补虚拟卡</button>`,
  ].join("");
  const limitInput = off
    ? ""
    : `<label class="card-face__limit-in"><span class="muted">额度（元）</span>
        <input class="input" type="number" inputmode="decimal" min="1" step="100"
          placeholder="调额 / 补卡金额" data-card-limit="${id}" /></label>`;
  return `<article class="card-face${off ? " card-face--off" : ""}">
    <div class="card-face__top">
      <span class="card-face__type">${esc(card.type_label)}</span>
      <span class="card-state card-state--${esc(card.status)}">${esc(card.status_label)}</span>
    </div>
    <p class="card-face__no mono">${id}</p>
    <p class="card-face__limit"><span class="muted">日限额</span> <b>${yuan(limit)}</b></p>
    <span class="bill__bar-track card-face__bar" role="img"
      aria-label="相对最高日限额 ${pct}%"><i style="width:${pct}%"></i></span>
    <div class="card-face__keys">${keys}</div>
    ${limitInput}
  </article>`;
}

function cardsHtml(payload) {
  const cards = (payload && payload.cards) || [];
  if (!cards.length) {
    return `<article class="state-card">
      <p class="state-card__k">暂无卡片</p>
      <p class="muted">当前账户下没有可用卡片；补卡后可在这里看到状态与额度。</p>
    </article>`;
  }
  const maxLimit = Math.max(...cards.map((card) => Number(card.daily_limit_cents) || 0), 1);
  return `<div class="card-wall">${cards.map((card) => cardFaceHtml(card, maxLimit)).join("")}</div>`
    + `<p class="muted cards-hint">红色动作（挂失 / 解锁 / 调额）需强验证：计划卡确认时输入演示验证码。挂失不可逆——已挂失的卡不能再解锁，要继续用卡只能补卡。</p>`;
}

/* 额度饼图：各卡日限额占全部卡片合计的比例（真实数字，仅用于横向对比） */
function cardsVisualHtml(cards) {
  if (!cards.length) return "";
  const rows = cards.map((card) => ({
    label: card.type_label || card.card_id,
    value: Number(card.daily_limit_cents) || 0,
    note: yuan(Number(card.daily_limit_cents) || 0),
  }));
  const counts = [
    ["active", "正常"],
    ["locked", "已锁定"],
    ["lost", "已挂失"],
    ["cancelled", "已取消"],
  ]
    .map(([key, label]) => {
      const count = cards.filter((card) => card.status === key).length;
      return count ? `${label} ${count} 张` : "";
    })
    .filter(Boolean)
    .join(" · ");
  return `<div class="cards-visual">
    <p class="panel__title panel__title--sub">日限额分布</p>
    ${donutChartHtml(rows, { label: "各卡日限额占比", size: 140, stroke: 16, empty: "卡片日限额均为 0，暂无可绘制占比。" })}
    <p class="muted scene-hint">状态：${esc(counts || "无")} · 数字读 /api/cards</p>
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

/* 卡片四键：页内拼中文指令 → /api/submit → 页内计划卡。
   绿色自动执行；黄色等确认；红色（挂失 / 解锁 / 调额）只给「去对话做强验证」。 */
async function cardPanelTask(kind, cardId) {
  const options = { host: "cards-plan", status: "cards-plan-status", refresh: refreshCards };
  const planHost = el("cards-plan");
  if (planHost) planHost.hidden = false;
  const planStatus = el("cards-plan-status");
  if (planStatus) planStatus.hidden = false;
  if (kind === "limit" || kind === "virtual") {
    const cents = cardLimitCents(cardId);
    if (cents === null) {
      if (planStatus) planStatus.textContent = "请先填写额度（元，正整数），再点「调额 / 补虚拟卡」。";
      return;
    }
    const utterance = kind === "limit"
      ? `把这张卡额度调到 ${cents / 100} 元`
      : `申请虚拟卡，额度 ${cents / 100} 元`;
    await sceneRunTask(Object.assign({
      utterance,
      selected: kind === "limit" ? { card: cardId, daily_limit: cents } : { daily_limit: cents },
    }, options));
    return;
  }
  await sceneRunTask(Object.assign({
    utterance: CARD_ACTION_UTTERANCE[kind] || "",
    selected: { card: cardId },
  }, options));
}

/* 订阅取消：卡片上的「取消」只开确认弹窗，确认后才发起计划。
   弹窗内容与卡片一致（商户 / 金额 / 周期 / 下次扣款），但形态是居中模态，不占页内版面。 */
function openSubCancelDialog(button) {
  const dialog = el("subscription-cancel-dialog");
  if (!dialog || !button) return;
  const data = button.dataset;
  dialog.dataset.subId = data.subCancel || "";
  const body = el("subscription-cancel-body");
  if (body) {
    const last = data.subLast ? ` · 最近扣费 ${esc(data.subLast)}` : "";
    body.innerHTML = `<p class="sub-cancel-dialog__merchant">${esc(data.subMerchant || "该订阅")}</p>
      <p class="sub-cancel-dialog__amount">${esc(data.subAmount || "—")}</p>
      <p class="muted sub-cancel-dialog__meta">${esc(data.subCycle || "")}扣费 · 下次扣款 ${esc(data.subNext || "未提供")}${last}</p>
      <p class="muted sub-cancel-dialog__note">取消只在本地把状态改为「已取消订阅」；商户侧是否停止扣费以商户回执为准，本页不承诺已停扣。</p>`;
  }
  if (typeof dialog.showModal === "function") dialog.showModal();
  else dialog.setAttribute("open", "");
}

function closeSubCancelDialog() {
  closeDialog(el("subscription-cancel-dialog"));
}

async function confirmSubCancel() {
  const dialog = el("subscription-cancel-dialog");
  const subscriptionId = dialog ? dialog.dataset.subId : "";
  closeSubCancelDialog();
  if (subscriptionId) await subscriptionPanelTask(subscriptionId);
}

/* 订阅取消：卡片上的「取消订阅」→ 中文指令 + 订阅 id 控制槽 → 页内计划卡。
   取消只落本地状态；商户侧是否停扣以回执里的外部状态为准，页内不承诺。 */
async function subscriptionPanelTask(subscriptionId) {
  const planHost = el("subscription-plan");
  if (planHost) planHost.hidden = false;
  const planStatus = el("subscription-plan-status");
  if (planStatus) planStatus.hidden = false;
  await sceneRunTask({
    utterance: "取消这个订阅",
    selected: { subscription: subscriptionId },
    host: "subscription-plan",
    status: "subscription-plan-status",
    refresh: refreshOptions,
  });
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

/* 全部识别线索：默认收起；卡内只留「结论 + 建议」，判断依据与证据再折叠一层 */
function billsFindingsHtml(payload) {
  const findings = payload.findings || [];
  const source = payload.data_source
    ? `<p class="muted finding__source">数据来源：${esc(payload.data_source)}</p>`
    : "";
  if (!findings.length) {
    return `<p class="muted">本期未命中识别规则；未发现不等于不存在。</p>${source}`;
  }
  const rows = findings.map((item) => {
    const impact = Number(item.impact_cents) || 0;
    const meta = [
      impact > 0 ? yuan(impact) : "仅提醒",
      `置信度 ${confidenceLabel(item.confidence)}`,
      FINDING_STATUS[item.status] || item.status,
    ].join(" · ");
    return `<details class="finding">
      <summary>
        <strong>${esc(FINDING_LABELS[item.kind] || item.kind)}</strong>
        <span class="muted">${esc(meta)}</span>
      </summary>
      <p>${esc(item.detail || "")}</p>
      <p class="muted finding__suggestion">建议：${esc(item.suggestion || "请核对交易明细后自行决定。")}</p>
      <details class="finding__basis">
        <summary>判断依据</summary>
        <p class="muted">${esc(item.assumption || "—")}</p>
        ${findingEvidenceHtml(item)}
      </details>
    </details>`;
  }).join("");
  return `<details class="bills-details">
    <summary>全部识别线索（${findings.length}）</summary>
    ${rows}${source}
  </details>`;
}

function billsTableHtml(payload) {
  const rows = (payload.transactions || []).map((item) => {
    const moment = item.booked_at ? String(item.booked_at).slice(11, 16) : "";
    return `<tr><td>${esc(item.booked_on)}${moment ? " " + esc(moment) : ""}</td><td>${esc(item.merchant_label)}</td><td>${esc(item.category)}</td><td>${esc(item.city || "—")}</td><td>${yuan(item.amount_cents)}</td></tr>`;
  }).join("");
  return `<table class="mini-table"><thead><tr><th>时间</th><th>商户</th><th>分类</th><th>地点</th><th>金额</th></tr></thead><tbody>${rows || '<tr><td colspan="5">本期无支出流水</td></tr>'}</tbody></table>`;
}

/* 本期概览：三个数字全部来自 /api/bills/analysis.summary */
function billsMetricsHtml(summary) {
  const item = summary || {};
  const pending = (Number(item.confirmed_count) || 0) + (Number(item.suspected_count) || 0);
  return `<div class="stat-metrics">
    <div class="stat-metric"><span class="stat-metric__k">本期支出</span>
      <b class="stat-metric__v">${yuan(item.expense_total_cents || 0)}</b></div>
    <div class="stat-metric"><span class="stat-metric__k">交易</span>
      <b class="stat-metric__v">${Number(item.transaction_count || 0)} 笔</b></div>
    <div class="stat-metric${pending ? " stat-metric--warn" : ""}">
      <span class="stat-metric__k">待核对</span>
      <b class="stat-metric__v">${pending} 项</b></div>
  </div>`;
}

/* 用户画像：只作概览下面的一行补充，不单独占一张卡（与「本期概览」三指标同屏） */
function billsProfileHtml(profile) {
  const item = profile || {};
  const top = item.top_category
    ? `${esc(item.top_category)} ${Math.round(Number(item.top_category_share || 0) * 100)}%`
    : "本期无分类支出";
  const subscription = `${yuan(item.subscription_monthly_cents || 0)}/月`;
  const suspicious = `${Number(item.suspicious_count || 0)} 条`;
  return `<p class="muted scene-hint">消费结构 ${top} · 订阅 ${subscription} · 大额可疑 ${suspicious}</p>`;
}

/* 两张图：左「分类占比」环形图，右「近 6 月支出趋势」折线；宽屏两栏、窄屏上下（见 .scene-charts） */
function billsChartsHtml(payload) {
  const share = (payload.category_share || [])
    .slice(0, 6)
    .map((item) => ({ label: item.category, value: item.expense_cents || 0 }));
  const trend = (payload.trend || []).map((point) => ({
    label: `${String(point.period || "").slice(5) || "—"}月`,
    value: point.expense_cents || 0,
  }));
  const last = (payload.trend || [])[(payload.trend || []).length - 1];
  return `<div class="scene-charts">
    <section>
      <p class="panel__title panel__title--sub">分类占比</p>
      ${donutChartHtml(share, { label: "本期支出分类占比", empty: "本期无分类支出。" })}
    </section>
    <section>
      <p class="panel__title panel__title--sub">近 6 月支出趋势</p>
      ${lineChartHtml(trend, {
        label: "近 6 月支出趋势",
        empty: "暂无趋势数据。",
        formatValue: (cents) => `¥${Math.round((Number(cents) || 0) / 100).toLocaleString("zh-CN")}`,
        note: last ? `最近一月 ${yuan(last.expense_cents || 0)}` : "",
      })}
    </section>
  </div>`;
}

function billsTop3Html(payload) {
  const risky = (payload.findings || [])
    .filter((item) => item.status !== "normal")
    .sort((a, b) => (b.impact_cents || 0) - (a.impact_cents || 0))
    .slice(0, 3);
  if (!risky.length) {
    return '<p class="panel__title panel__title--sub">重点核对</p><p class="muted">暂无需要优先核对的可疑项。</p>';
  }
  return `<p class="panel__title panel__title--sub">重点核对</p>` +
    risky.map((item) => `<article class="bills-top">
      <strong>${esc(FINDING_LABELS[item.kind] || item.kind)}</strong>
      <span class="muted">${Number(item.impact_cents) > 0 ? yuan(item.impact_cents) : "仅提醒"} · 置信度 ${confidenceLabel(item.confidence)}</span>
    </article>`).join("");
}

function billsLoading() {
  const status = el("bills-status");
  if (status) status.textContent = "正在读取账单识别结果…";
  const skeleton = '<span class="skeleton"></span><span class="skeleton"></span>';
  ["bills-summary", "bills-profile", "bills-charts", "bills-top3", "bills-findings"].forEach((id) => {
    const node = el(id);
    if (node) node.innerHTML = id === "bills-profile" ? "" : skeleton;
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

/* 记住最近一次账单回答的账期（形如 "2019-01"），供场景页按同一账期取数 */
function rememberBillsPeriod(findings) {
  const period = findings && typeof findings.period === "string" ? findings.period : "";
  const matched = /^(\d{4})-(\d{2})$/.exec(period);
  state.billsPeriod = matched
    ? { year: Number(matched[1]), month: Number(matched[2]) }
    : null;
}

async function refreshBills() {
  if (state.offline) return;
  billsLoading();
  /* 场景页跟随「最近一次账单问答」的账期：问「查 2019 年 1 月账单」就别再画本月的图。
     没问过时留空，服务端按缺省（上一个月）返回。 */
  const period = state.billsPeriod;
  const path = period
    ? `/api/bills/analysis?year=${period.year}&month=${period.month}`
    : "/api/bills/analysis";
  let result;
  try {
    result = await apiFetch("GET", path);
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
  const pending = (Number(summary.confirmed_count) || 0) + (Number(summary.suspected_count) || 0);
  el("bills-status").textContent = `${payload.period} · ${summary.transaction_count || 0} 笔 · ${pending} 项待核对`;
  const disclaimer = el("bills-disclaimer");
  disclaimer.hidden = false;
  disclaimer.textContent = payload.data_source_is_demo
    ? "演示数据 · 只作核对线索，不当作已确认"
    : "只作核对线索，不当作已确认";
  if (!(payload.transactions || []).length) {
    billsEmpty(payload);
    return;
  }
  const degraded = payload.narrative && payload.narrative.degraded
    ? '<p class="muted scene-hint">文字说明暂不可用，以下为明细数据。</p>'
    : "";
  el("bills-summary").innerHTML = billsMetricsHtml(summary) + degraded;
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
    action(() => cardPanelTask(button.dataset.cardAction, button.dataset.cardId))();
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
  /* 浮章：绝对定位在卡片右上，不占正文排版位 */
  return `<div class="seal seal--stamped seal--float seal--${meta.cls}">
    <span class="seal__level">${meta.code}</span>
    <span class="seal__text">${meta.text}</span>
    <span class="seal__policy">${meta.policy}</span>
  </div>`;
}

function stepHtml(line, index, stepStates, readOnly) {
  const states = Array.isArray(stepStates) ? stepStates : state.stepStates;
  const status = states[index] || "pending";
  const cls = status === "done" ? "step step--done" : status === "rolled" ? "step step--rolled" : "step step--pending";
  const label = readOnly ? "计划项" : status === "done" ? "已执行" : status === "rolled" ? "已回滚" : "待执行";
  return `<li class="${cls}">
    <span class="step__no">${String(index + 1).padStart(2, "0")}</span>
    <span class="step__main"><span class="step__action">${esc(stripNo(line))}</span></span>
    <span class="step__meta"><span class="step__state">${label}</span></span>
  </li>`;
}

const DAG_STATE = {
  pending: "待执行",
  completed: "已执行",
  unverified: "已提交·待核验",
  reversed: "已冲正",
  suspended: "已挂起",
  skipped: "未执行",
};

/* 任务 DAG：节点、依赖、状态**全部来自服务端**，前端只排版，不推导、不补节点。
   单节点计划不画 DAG（一句计划卡就够），回落到原步骤列表。 */
function dagHtml(nodes, lines, statuses, readOnly) {
  if (!Array.isArray(nodes) || nodes.length < 2) return "";
  const indexOf = (id) => {
    const found = nodes.findIndex((item) => item.node_id === id);
    return found >= 0 ? String(found + 1).padStart(2, "0") : String(id);
  };
  const items = nodes.map((node, index) => {
    const key = readOnly ? "pending" : (statuses && statuses[node.node_id]) || "pending";
    const label = readOnly ? "计划项" : DAG_STATE[key] || "待执行";
    const deps = Array.isArray(node.depends_on) ? node.depends_on : [];
    const edge = index === 0
      ? ""
      : deps.length
        ? `<span class="dag__edge dag__edge--serial">依赖 ${deps.map(indexOf).join("、")} · 串行（${esc(node.resource_label || "前置步骤")}）</span>`
        : `<span class="dag__edge dag__edge--parallel">并行（不同资源）</span>`;
    const text = stripNo(lines[index] || "") || node.label || node.action;
    return `<li class="dag__node dag__node--${readOnly ? "snapshot" : key}">
      ${edge}
      <div class="dag__row">
        <span class="dag__no">${String(index + 1).padStart(2, "0")}</span>
        <span class="dag__main"><span class="dag__action">${esc(node.label || node.action)}</span><span class="dag__text">${esc(text)}</span></span>
        <span class="dag__state">${esc(label)}</span>
      </div>
    </li>`;
  }).join("");
  return `<div class="dag">
    <div class="dag__title">任务 DAG · ${nodes.length} 个节点 · 依赖由服务端机械推导</div>
    <ol class="dag__nodes">${items}</ol>
    ${readOnly ? "" : `<p class="dag__note">同资源串行、异资源并行；冲正仅回补本项目模拟核心银行的记账，不等价于真实银行退回。</p>`}
  </div>`;
}

/* 单节点计划不画 DAG，但「本地冲正」的边界声明不能丢（P-4）：冲正后才出现。
   多节点由 dagHtml 的脚注统一声明，这里不重复。 */
function reversalNoteHtml(nodes, statuses, readOnly) {
  if (readOnly) return "";
  if (Array.isArray(nodes) && nodes.length > 1) return "";
  const reversed =
    state.phase === "reversed" || Object.values(statuses || {}).includes("reversed");
  return reversed
    ? '<p class="dag__note">本地冲正：仅回补本项目模拟核心银行的记账，不等价于真实银行退回。</p>'
    : "";
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
function previewHtml(card, decisionReasons) {
  if (!card || !card.level || card.level === "GREEN") return "";
  const rows = card.summary || [];
  const trace = card.balance_trace || [];
  const impactLines = card.impact || [];
  const reasons = Array.isArray(decisionReasons)
    ? decisionReasons
    : state.decision ? state.decision.reasons || [] : [];
  const impact = rows.length
    ? `<dl class="preview__grid">${rows
        .map((row) => `<dt>${esc(row.label)}</dt><dd${row.emphasis ? ' class="preview__val--strong"' : ""}>${esc(row.value)}</dd>`)
        .join("")}</dl>`
    : "";
  return `<details class="preview">
    <summary>确认后会发生什么</summary>
    ${impact ? `<div class="preview__block"><span class="preview__title">影响面</span>${impact}</div>` : ""}
    ${trace.length ? `<div class="preview__block"><span class="preview__title">可用余额轨迹</span><ol class="preview__list">${trace.map((row) => `<li>${esc(row)}</li>`).join("")}</ol></div>` : ""}
    ${impactLines.length ? `<div class="preview__block"><span class="preview__title">连带影响</span><ol class="preview__list">${impactLines.map((row) => `<li>${esc(row)}</li>`).join("")}</ol></div>` : ""}
    <div class="preview__block"><span class="preview__title">命中规则</span><p class="preview__rules">${rulesHtml(reasons)}</p></div>
    <p class="muted preview__note">以上数字均取自服务端计划卡，前端未做二次计算。</p>
  </details>`;
}

function voucherHtml(card = state.card, decision = state.decision, options = {}) {
  if (!card) return "";
  const opts = options || {};
  const lines = card.lines || [];
  const frozen = card.frozen_hash || "";
  const reasons = decision && Array.isArray(decision.reasons) ? decision.reasons : [];
  const readOnly = opts.readOnly === true;
  const stepStates = Array.isArray(opts.stepStates) ? opts.stepStates : state.stepStates;
  const stamp = opts.timeLabel || clock();
  return `<article class="voucher${readOnly ? " voucher--snapshot" : ""}">
    ${sealHtml(card.level)}
    <div class="voucher__head">
      <div class="doc-no">
        <span class="doc-no__label">${readOnly ? "计划编号" : "凭证号"}</span>
        <span class="doc-no__value">${esc(readOnly ? String(card.card_id || "—") : receiptNo(frozen))}</span>
        <span class="doc-no__label tech" style="margin-top: 6px">计划冻结哈希（cfhash）</span>
        <span class="tech">${esc(frozen)}</span>
      </div>
    </div>

    <div class="voucher__body">
      <dl class="triple">
        <div class="triple__row"><dt class="triple__key">动作</dt>
          <dd class="triple__val">${lines.length} 步计划 <span class="tech">${esc(card.card_id)}</span></dd></div>
        <div class="triple__row"><dt class="triple__key">要求</dt>
          <dd class="triple__val triple__val--tokens">${requiresHtml(card.requires || [])}</dd></div>
        <div class="triple__row"><dt class="triple__key">时间</dt>
          <dd class="triple__val">${esc(stamp)}</dd></div>
      </dl>
      <div class="amount">
        <span class="amount__value">${lines.length} 步</span>
        <span class="amount__note">${readOnly ? "历史计划快照 · 只读" : "确认即锁定本计划 · 计划级 + 节点级双层执行门"}</span>
      </div>
    </div>

    ${quotaHtml(card)}

    ${previewHtml(card, reasons)}

    ${dagHtml(card.nodes, lines, opts.nodeStatus || state.nodeStatus, readOnly)}
    ${(card.nodes || []).length > 1 ? "" : (lines.length ? `<ol class="steps">${lines.map((line, index) => stepHtml(line, index, stepStates, readOnly)).join("")}</ol>` : "")}
    ${reversalNoteHtml(card.nodes, opts.nodeStatus || state.nodeStatus, readOnly)}

    ${traceHtml(card.balance_trace)}

    ${card.expires_at ? `<p class="muted voucher__expiry">有效期至 ${esc(momentLabel(card.expires_at))}；过期后需重新生成计划。</p>` : ""}

    ${readOnly && !reasons.length
      ? ""
      : `<p class="muted" style="margin-top: var(--s-3)">命中规则<span class="rules">${rulesHtml(reasons)}</span></p>`}
  </article>`;
}

/* 历史里的执行回执：与实时回执同一视觉，只读；没有结果行就只留一句结论 */
function receiptSnapshotHtml(receipt, stamp) {
  const info = receipt || {};
  const completed = info.state === "completed";
  const lines = (info.results || [])
    .map((item) => String(item.message || "").replace(/｜审计ID\s*\S+\s*$/, "").trim())
    .filter(Boolean);
  const meta = [momentLabel(stamp), info.record_ids && info.record_ids.length
    ? `记录 ${info.record_ids.length} 条` : ""].filter(Boolean).join(" · ");
  return `<div class="takeover takeover--receipt history-receipt">
    <p class="receipt__title"><span class="receipt__tick" aria-hidden="true">${completed ? "✓" : "!"}</span>${esc(info.message || "执行回执")}</p>
    ${lines.length ? `<ul class="receipt__list">${lines.map((line) => `<li>${esc(line)}</li>`).join("")}</ul>` : ""}
    ${meta ? `<p class="muted history-receipt__meta">${esc(meta)}</p>` : ""}
  </div>`;
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
  /* 运行链路是这一条回复的过程记录，重绘正文时保留下来（已折叠的仍保持折叠） */
  const runChain = state.response.querySelector(".run-chain");
  state.response.innerHTML = state.card
    ? voucherHtml() + (state.phase === "strong-verify" ? verifyHtml() : "") + state.notice
    : (state.panel || "") + state.notice;
  if (runChain) state.response.insertBefore(runChain, state.response.firstChild);
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
  /* 每次回答只留一行摘要，展开才看三格：避免占掉对话正文的位置 */
  return `<details class="role-chain-fold" aria-label="AI 三角色链路">
    <summary>AI 链路 · 规划 ${esc(name(metadata.planner_channel))} · 审查 ${esc(reviewer ? name(reviewer) : "未启用")} · 裁决 ${esc(level || "待裁决")}</summary>
    <section class="role-chain">
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
    </section>
  </details>`;
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
  /* 上一条回复的链路先收尾，避免留在「进行中」 */
  if (state.response) settleRunChain();
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
  /* 从历史里恢复出来的计划卡只是快照：用户还没动手，不算「当前办理未结束」 */
  if (state.historyRestored) return false;
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
  state.historyRestored = false;
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
  /* 先把操作条挪回停车位，再清掉旧任务节点——否则会连带删掉 `#task-actions`，
     `setButtons()` 之后取不到该节点会报错。 */
  const parking = el("actions-parking");
  const actions = el("task-actions");
  if (parking && actions && actions.parentElement !== parking) parking.appendChild(actions);
  const stage = el("stage");
  if (stage) stage.querySelectorAll(".chat-task").forEach((node) => node.remove());
  const welcome = el("conversation-welcome");
  if (welcome) welcome.hidden = false;
  navigate("chat", {});
  el("utterance").focus();
  setMobileDrawer("scenes", false);
}

/* 红色动作的验证码面板：一次一码、有有效期；演示模式直接把码显示出来。
   文案只说「验证码发到哪、多久有效」，不堆安全术语。 */
function verifyHtml() {
  const v = state.verification || {};
  const ttl = v.expires_in_s ? `${esc(String(v.expires_in_s))} 秒内有效` : "";
  const delivery = v.mode === "demo"
    ? `演示模式不发短信，验证码：<b class="mono">${esc(v.code || "—")}</b>`
    : "验证码已发到配置的第二信道，请查收。";
  return `<div class="verify-panel">
    <p class="verify-panel__title">这一步需要验证码</p>
    <p class="verify-panel__meta">接收方 <span class="mono">${esc(v.target || "—")}</span>
      · ${esc(v.channel || "—")}${ttl ? ` · ${ttl}` : ""} · 只能用一次</p>
    <p class="verify-panel__meta">${delivery}</p>
    <div class="verify-panel__row">
      <input class="input" id="vcode" inputmode="numeric" maxlength="6" placeholder="输入 6 位验证码" />
      <button class="btn btn--sm btn--ghost" id="btn-reverify" type="button">重新获取</button>
    </div>
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
  /* 服务端已校验过期；前端只做「不让人白点」，不改服务端语义。
     `expires_at` 是不带时区的 UTC 裸串，必须按 UTC 解析，否则东八区会提前 8 小时判过期。 */
  const cardExpiresAt = state.card ? parseServerTime(state.card.expires_at) : NaN;
  const expired = !Number.isNaN(cardExpiresAt) && Date.now() >= cardExpiresAt;
  el("btn-confirm").textContent = expired
    ? "计划已过期 · 重新发起"
    : strongPhase
      ? "提交验证码并执行"
      : redPending
        ? "获取验证码"
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
  state.nodeStatus = {};
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
  container.innerHTML = PRESETS.slice(0, 8)
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
    state.nodeStatus = {};
    (data.card.nodes || []).forEach((node) => { state.nodeStatus[node.node_id] = "pending"; });
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
  state.historyRestored = false;
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

function billIssueHtml(item) {
  const status = BILL_STATUS_LABELS[item.status] || "未知状态";
  return `<details class="bill__issue">
    <summary>
      <b>${esc(FINDING_LABELS[item.kind] || item.kind)}</b>
      <span class="bill__issue-amt">${yuan(Number(item.impact_cents) || 0)}</span>
      <span class="chip">${esc(status)}</span>
    </summary>
    <p>${esc(item.detail || "")}</p>
    ${item.suggestion ? `<p class="bill__note">建议：${esc(item.suggestion)}</p>` : ""}
    <p class="bill__note">影响口径：${esc(item.impact_basis || "未提供")} · 估算假设：${esc(item.assumption || "—")}</p>
  </details>`;
}

/* 对话里的账单结论只保留「一句话结论 + 三个数字 + 最多三条问题」，
   图表、完整证据链与全部建议留在账单分析页（同一份事实，不重复占屏）。 */
function billAnalysisHtml(payload, advice, adviceMessage) {
  if (!payload || !payload.summary) return "";
  const summary = payload.summary;
  const total = Number(summary.transaction_count) || 0;
  const expense = Number(summary.expense_total_cents) || 0;
  const confirmedSaving = Number(payload.confirmed_saving_cents) || 0;
  const potentialSaving = Number(payload.potential_saving_cents) || 0;
  const problemCount = (Number(summary.confirmed_count) || 0) + (Number(summary.suspected_count) || 0);
  const problems = (Array.isArray(payload.findings) ? payload.findings : [])
    .filter((item) => item.status !== "normal")
    .sort((a, b) => (Number(b.impact_cents) || 0) - (Number(a.impact_cents) || 0));
  const top = problems.slice(0, 3);
  const rest = problems.length - top.length;
  const topAdvice = Array.isArray(advice) && advice.length ? advice[0] : null;
  const value = (text) => (total ? text : "—");
  return `<section class="bill bill--compact">
    <div class="bill__head">
      <h3>账单分析结论</h3>
      <span class="chip">账期 ${esc(payload.period)}</span>
      ${payload.data_source_is_demo ? '<span class="chip">演示数据</span>' : ""}
    </div>
    <p class="bill__verdict">${esc(billVerdict(summary))}</p>
    <div class="bill__metrics bill__metrics--compact">
      <div class="bill__metric"><span>本期支出</span><b>${value(yuan(expense))}</b></div>
      <div class="bill__metric"><span>需核对问题</span><b>${value(`${problemCount} 项`)}</b></div>
      <div class="bill__metric"><span>可核实节省</span><b>${value(yuan(confirmedSaving))}</b></div>
    </div>
    ${top.length
      ? `<div class="bill__issues">${top.map(billIssueHtml).join("")}</div>
         ${rest > 0 ? `<p class="bill__note">另有 ${rest} 项问题，可在账单分析页查看完整清单。</p>` : ""}`
      : `<p class="bill__note">${total
          ? "本期未命中需要优先核对的问题；未发现不等于不存在。"
          : "本期没有支出流水，无法给出消费结论。"}</p>`}
    ${topAdvice ? `<p class="bill__note">建议：${esc(topAdvice.suggestion)}</p>` : ""}
    <p class="bill__note">识别结果只作核对线索，不当作已确认；未发现不等于不存在，请照下方证据逐条核对。</p>
    ${potentialSaving > 0
      ? `<p class="bill__note">潜在节省 ${yuan(potentialSaving)} 为识别口径下的估算上限，未到账不计入。</p>`
      : ""}
    <details class="bill__limits">
      <summary>数据来源与分析局限</summary>
      <p>数据来源：${esc(payload.data_source)}${payload.data_source_is_demo ? "（演示数据，非真实账单）" : ""}；分析截至 ${esc(payload.as_of)}。</p>
      <ul>${(payload.notes || []).map((note) => `<li>${esc(note)}</li>`).join("")}</ul>
    </details>
    ${adviceMessage ? `<p class="bill__note">${esc(adviceMessage)}</p>` : ""}
    <div class="btn-row">
      <button class="btn btn--sm btn--ghost" type="button" data-nav="scene" data-scene="bills">查看完整分析与明细</button>
    </div>
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
  const progress = Array.isArray(data.node_progress) ? data.node_progress : [];
  progress.forEach((row) => { state.nodeStatus[row.node_id] = row.status; });
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
  rememberBillsPeriod(data.bill_findings);
  const billHtml = complete
    ? billAnalysisHtml(data.bill_findings, data.bill_advice, data.bill_advice_message)
    : "";
  const adviceFallback = billAdviceHtml(data.bill_advice) +
    (data.bill_advice_message ? `<p class="muted">${esc(data.bill_advice_message)}</p>` : "");
  const receiptLines = results.map((item) =>
    String(resultMessage(item)).replace(/｜审计ID\s*\S+\s*$/, "").trim()
  ).filter(Boolean);
  state.notice = complete
    ? `<div class="takeover takeover--receipt">
        <p class="receipt__title"><span class="receipt__tick" aria-hidden="true">✓</span>${esc(data.message || "计划执行完成")}</p>
        ${receiptLines.length ? `<ul class="receipt__list">${receiptLines.map((line) => `<li>${esc(line)}</li>`).join("")}</ul>` : ""}
      </div>` +
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
  Object.keys(state.nodeStatus || {}).forEach((nodeId) => {
    if (state.nodeStatus[nodeId] === "pending") state.nodeStatus[nodeId] = "suspended";
  });
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
  state.nodeStatus = {};
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
  if (reversed) {
    Object.keys(state.nodeStatus || {}).forEach((nodeId) => {
      if (state.nodeStatus[nodeId] === "completed") state.nodeStatus[nodeId] = "reversed";
    });
  }
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

/* 服务端时间戳有两种：带时区的（`+00:00`）和不带时区的 UTC 裸串（计划卡 `expires_at`）。
   裸串若直接交给 `Date.parse`，浏览器会按**本地时区**解释，东八区会比真实时间早 8 小时——
   计划卡刚生成就被判「已过期」。这里统一按 UTC 解析，再在展示时换成浏览器本地时间。 */
function parseServerTime(stamp) {
  const text = String(stamp || "").trim();
  if (!text) return NaN;
  /* 纯日期（业务日 / 账期）没有时区概念，按本地自然日处理 */
  if (/^\d{4}-\d{2}-\d{2}$/.test(text)) return Date.parse(`${text}T00:00:00`);
  const hasZone = /(?:[zZ]|[+-]\d{2}:?\d{2})$/.test(text);
  return Date.parse(hasZone ? text : `${text}Z`);
}

/* 服务端存 ISO 时间；展示时统一换成浏览器本地时间，列表只到分钟 */
function momentLabel(stamp) {
  const text = String(stamp || "");
  if (!text) return "—";
  if (/^\d{4}-\d{2}-\d{2}$/.test(text)) return text;
  const ms = parseServerTime(text);
  if (Number.isNaN(ms)) return text.replace("T", " ").slice(0, 16);
  const date = new Date(ms);
  const pad = (value) => String(value).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ` +
    `${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

/* 历史分组：今天 / 昨天 / 更早；按本地自然日计算，不按 24 小时滚动 */
function conversationGroupLabel(stamp) {
  const ms = parseServerTime(stamp);
  if (Number.isNaN(ms)) return "更早";
  const date = new Date(ms);
  const now = new Date();
  const dayStart = (value) => new Date(value.getFullYear(), value.getMonth(), value.getDate()).getTime();
  const diffDays = Math.round((dayStart(now) - dayStart(date)) / 86400000);
  if (diffDays <= 0) return "今天";
  if (diffDays === 1) return "昨天";
  return "更早";
}

/* 分组内不再重复完整日期：今天/昨天只显示时分，更早显示日期 + 时分 */
function conversationTimeLabel(stamp, group) {
  const label = momentLabel(stamp);
  if (!label || label === "—") return "";
  const time = label.slice(11, 16);
  return group === "更早" ? `${label.slice(0, 10)} ${time}` : time;
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
  const groups = new Map([["今天", []], ["昨天", []], ["更早", []]]);
  conversations.forEach((item) => {
    groups.get(conversationGroupLabel(item.last_active_at)).push(item);
  });
  list.innerHTML = Array.from(groups.entries())
    .filter(([, items]) => items.length)
    .map(([label, items]) => `<section class="convs__group" aria-label="${esc(label)}的对话">
      <p class="convs__group-label">${esc(label)}</p>
      ${items.map((item) => {
        const title = item.title || "未命名会话";
        const meta = [
          conversationTimeLabel(item.last_active_at, label),
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
                  title="删除对话" aria-label="删除对话：${esc(title)}">
            <svg viewBox="0 0 16 16" width="13" height="13" fill="none" stroke="currentColor"
                 stroke-width="1.4" stroke-linecap="round" aria-hidden="true" focusable="false">
              <path d="M3 4.5h10M6.5 4.5V3h3v1.5M4.6 4.5l.6 8.3h5.6l.6-8.3M6.7 7v3.6M9.3 7v3.6" />
            </svg>
          </button>
        </div>`;
      }).join("")}
    </section>`)
    .join("");
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
  if (!sessionId) return;
  if (sessionId === SESSION_ID) {
    /* 点的是当前会话：从场景页回到对话页即可，不重复加载快照 */
    if (el("app")?.dataset.viewName !== "chat") navigate("chat", {});
    return;
  }
  /* 只是翻看历史时不拦截：只有本页真实提交过、且尚未收尾的办理才提醒 */
  if (!state.historyRestored && hasUnfinishedTask()) {
    const proceed = await confirmAppDialog({
      title: "切换历史对话？",
      text: "当前办理尚未结束，切换后会保存为只读记录。",
      confirmText: "继续切换",
      danger: false,
    });
    if (!proceed) return;
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
  /* 从场景 / 记录 / 我的等页面点历史对话时，先把视图切回对话，否则内容会渲染在隐藏页里 */
  navigate("chat", {});
  archiveTask();
  reset(false);
  state.historyRestored = false;
  SESSION_ID = sessionId;
  const welcome = el("conversation-welcome");
  if (welcome) welcome.hidden = true;
  /* 一次只展示一条对话：先清掉上一条历史/办理留下的任务节点 */
  const stage = el("stage");
  stage.querySelectorAll(".chat-task").forEach((node) => node.remove());
  const task = document.createElement("section");
  task.className = "chat-task";
  task.dataset.session = sessionId;
  task.setAttribute("aria-label", `历史对话 ${++state.taskCount}`);
  state.task = task;
  stage.appendChild(task);
  const messages = Array.isArray(data.messages) ? data.messages : [];
  const resumeCard = Boolean(data.card && data.state === "awaiting_confirm");
  let livePlanIndex = -1;
  if (resumeCard) {
    for (let index = messages.length - 1; index >= 0; index -= 1) {
      if (messages[index].payload && messages[index].payload.card) {
        livePlanIndex = index;
        break;
      }
    }
  }
  messages.forEach((message, index) => {
    const body = appendMessage(message.role === "user" ? "user" : "assistant", message.text || "");
    const card = message.payload && message.payload.card;
    const receipt = message.payload && message.payload.receipt;
    if (card && index !== livePlanIndex) {
      /* 历史计划卡按快照渲染：不出现确认按钮，步骤状态只标「计划项」 */
      body.innerHTML = `<p class="muted history-card-lead">${esc(message.text || "计划卡快照")}</p>` +
        voucherHtml(card, null, {
          readOnly: true,
          stepStates: (card.lines || []).map(() => "pending"),
          timeLabel: momentLabel(message.created_at),
        });
    } else if (receipt) {
      /* 回执也是卡片：历史里不能只剩一句话，否则看不出执行了什么 */
      body.innerHTML = receiptSnapshotHtml(receipt, message.created_at);
    }
  });
  if (resumeCard) {
    state.card = data.card;
    state.decision = null;
    state.stepStates = (data.card.lines || []).map(() => "pending");
    state.nodeStatus = {};
    (data.card.nodes || []).forEach((node) => { state.nodeStatus[node.node_id] = "pending"; });
    state.phase = "awaiting_confirm";
    state.historyRestored = true;
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

/* 删除单条历史：确认文案与二次口令合并到同一个弹窗；`demo` 模式先换一次性票据再删。
   只删「对话消息 + 会话快照」，交易流水 / 回执 / 审计都不动，所以记录与明细不会少。 */
async function deleteConversation(sessionId) {
  if (!sessionId) return;
  const needsPassword = (await currentAuthMode()) === "demo";
  openAppDialog({
    title: "删除这条对话？",
    text: "消息记录与办理快照会一起删除，且不可恢复。交易流水、回执与审计记录不受影响，「记录与明细」里的记录不会减少。",
    password: needsPassword,
    confirmText: "删除",
    onConfirm: async (password) => {
      let ticket = "";
      if (needsPassword) {
        const verified = await apiFetch("POST", "/api/auth/reverify", {
          password,
          purpose: "delete_conversation",
        });
        if (!verified.ok || !verified.data || !verified.data.ticket) {
          return "登录口令不正确，删除已取消。";
        }
        ticket = String(verified.data.ticket);
      }
      const result = await apiFetch(
        "DELETE",
        `/api/conversations/${encodeURIComponent(sessionId)}`,
        undefined,
        ticket ? { "X-Reverify-Ticket": ticket } : undefined,
      );
      if (result.status === 404) {
        timelineAdd("这条对话已不存在。", "warn");
        refreshConversations().catch(() => {});
        return "";
      }
      if (!result.ok) return "删除失败，请稍后重试。";
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
      return "";
    },
  });
}

/* ---------------- 个人中心 ---------------- */

function profileValue(value) {
  return value === null || value === undefined || value === "" ? "—" : String(value);
}

const AUTH_MODE_LABELS = { demo: "演示登录", off: "单用户演示" };

/* 登录记录只给可读摘要：浏览器 · 系统；本机回环地址写作「本机」，原始 UA 放 title 备查 */
function deviceLabel(userAgent) {
  const text = String(userAgent || "");
  if (!text) return "未知设备";
  const browser = /Edg\//.test(text)
    ? "Edge"
    : /Firefox\//.test(text)
      ? "Firefox"
      : /Chrome\//.test(text)
        ? "Chrome"
        : /Safari\//.test(text)
          ? "Safari"
          : "浏览器";
  const system = /Windows/.test(text)
    ? "Windows"
    : /Android/.test(text)
      ? "Android"
      : /iPhone|iPad|iPod/.test(text)
        ? "iOS"
        : /Mac OS X|Macintosh/.test(text)
          ? "macOS"
          : /Linux/.test(text)
            ? "Linux"
            : "";
  return system ? `${browser} · ${system}` : browser;
}

function clientLabel(ip) {
  const text = String(ip || "").trim();
  return !text || text === "127.0.0.1" || text === "::1" ? "本机" : text;
}

function loginSummary(record) {
  return `${momentLabel(record.issued_at)} · ${clientLabel(record.client_ip)} · ${deviceLabel(record.user_agent)}`;
}

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
  const logins = Array.isArray(me.recent_logins) ? me.recent_logins : [];
  const latestLogin = logins[0] || null;
  const olderLogins = logins.slice(1);
  const olderHtml = olderLogins.map((record) => {
    const revoked = record.revoked ? " · 已失效" : "";
    return `<li title="${esc(profileValue(record.user_agent))}">${esc(loginSummary(record))}${revoked}</li>`;
  }).join("");
  const trail = (audit.events || []).map((event) => {
    const payload = event.payload || {};
    const channel = payload.channel || "未记录";
    const digest = payload.text_digest;
    return `<li><span class="mono">${esc(momentLabel(event.timestamp))}</span> · ${esc(profileValue(event.kind))}` +
      ` · 渠道 ${esc(channel)} <span class="tech mono">${esc(profileValue(event.event_id))}</span>` +
      (digest ? `<details><summary>同摘要关联</summary><p class="mono audit-digest">${esc(digest)}</p></details>` : "") + "</li>";
  }).join("");
  const feishu = me.feishu || null;
  const feishuHtml = !feishu
    ? '<p class="muted">渠道信息读取失败，请稍后重试。</p>'
    : !feishu.configured
      ? '<p class="muted">飞书渠道未接入：服务器还没有配置飞书应用凭据，配好后这里会出现「连接飞书」。</p>'
      : feishu.linked
        ? `<p class="muted">已连接：可以直接在飞书里对「AI Banking」机器人说「查余额」「给李娜转五百」。</p>
           <div class="btn-row"><button class="btn btn--ghost" id="btn-feishu-unlink" type="button">解除绑定</button></div>
           <div id="feishu-link-result"></div>`
        : `<p class="muted">未连接：点「连接飞书」生成 6 位绑定码，再到飞书里把这 6 位数字发给机器人。</p>
           <div class="btn-row"><button class="btn" id="btn-feishu-link" type="button">连接飞书</button></div>
           <div id="feishu-link-result"></div>`;
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
      <h3>飞书渠道</h3>
      ${feishuHtml}
    </section>
    <section class="profile-card">
      <h3>登录安全</h3>
      <dl>
        <dt>本次登录</dt><dd>${esc(momentLabel(session.issued_at))}</dd>
        <dt>有效至</dt><dd>${esc(momentLabel(session.expires_at))}</dd>
      </dl>
      ${latestLogin
        ? `<details class="login-history">
            <summary>
              <span class="login-history__label">最近登录</span>
              <span class="login-history__latest" title="${esc(profileValue(latestLogin.user_agent))}">${esc(loginSummary(latestLogin))}${latestLogin.revoked ? " · 已失效" : ""}</span>
            </summary>
            ${olderHtml
              ? `<ul class="profile-trail login-history__list">${olderHtml}</ul>`
              : '<p class="muted login-history__empty">暂无更早的登录记录。</p>'}
          </details>`
        : '<p class="muted">暂无可查看的登录记录。</p>'}
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
    if (result.data && result.data.auth_mode) authModeCache = result.data.auth_mode;
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

/* 步骤列表：节点与顺序全部来自服务端下发的 skeleton，前端不拼步骤、不猜动作。
   服务端没有逐步执行回执，所以只标序号，不写「已完成 / 待确认」这类假状态。 */
function lifeDagHtml(skeleton) {
  const steps = Array.isArray(skeleton) ? skeleton.filter((step) => typeof step === "string" && step) : [];
  if (!steps.length) return "";
  const nodes = steps.map((step, index) => {
    const label = LIFE_STEP_LABEL[step] || step;
    return `<li class="life-step" title="${esc(step)}">
      <i class="life-step__dot" aria-hidden="true"></i>
      <b>${esc(label)}</b>
      <span class="life-step__code">${index + 1}</span>
    </li>`;
  });
  return `<ol class="life-steps" aria-label="链路步骤">${nodes.join("")}</ol>`;
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

async function runLifeEvent(eventId, form, fallbackText) {
  const values = form ? lifeValues(form) : {};
  const build = LIFE_UTTERANCE[eventId];
  /* 组合律没有表单与固定句式：直接用卡片上那句服务端派生的示例话术 */
  const text = build ? build(values) : String(fallbackText || "");
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

/* 加载态：与任务台同骨架的占位块，读完整体替换；失败由 panelFailure 覆盖 */
function lifeLoadingHtml() {
  return `<div class="life-detail" role="status" aria-live="polite">
    <span class="skeleton"></span><span class="skeleton"></span><span class="skeleton"></span>
  </div>`;
}

/* ---------------- 生活日历（L4 视图为主） ---------------- */
/* 只按「本人生成过的记录日期」落点：不排商户日程、不预填未来日期、不补假数据。
   月份可前后翻，翻到空月份就显示空态。 */
const lifeCal = { month: "", day: "", records: [] };

function lifeMonthKey(date) {
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}`;
}

function lifeDateKey(value) {
  const text = String(value || "");
  return /^\d{4}-\d{2}-\d{2}/.test(text) ? text.slice(0, 10) : "";
}

function lifeShiftMonth(month, delta) {
  const [year, mon] = month.split("-").map(Number);
  return lifeMonthKey(new Date(year, mon - 1 + delta, 1));
}

function lifeCalendarHtml(records) {
  const month = lifeCal.month || lifeMonthKey(new Date());
  lifeCal.month = month;
  /* 翻月后旧选中日期已不在本月：直接清空，避免列表和日历对不上 */
  if (lifeCal.day && lifeCal.day.slice(0, 7) !== month) lifeCal.day = "";
  const [year, mon] = month.split("-").map(Number);
  const lead = (new Date(year, mon - 1, 1).getDay() + 6) % 7;
  const days = new Date(year, mon, 0).getDate();
  const today = lifeDateKey(new Date().toISOString());
  const byDay = {};
  records.forEach((record) => {
    const key = lifeDateKey(record.updated_at || record.created_at);
    if (!key || key.slice(0, 7) !== month) return;
    (byDay[key] = byDay[key] || []).push(record);
  });
  const cells = [];
  for (let i = 0; i < lead; i += 1) {
    cells.push('<li class="life-cal__cell--blank" aria-hidden="true"></li>');
  }
  for (let day = 1; day <= days; day += 1) {
    const key = `${month}-${String(day).padStart(2, "0")}`;
    const items = byDay[key] || [];
    const picked = key === lifeCal.day;
    const dots = items.slice(0, 3).map((record) =>
      `<i class="life-cal__dot" title="${esc(record.event_name || record.event_id)}"></i>`).join("");
    const marks = [items.length ? "is-marked" : "", key === today ? "is-today" : "", picked ? "is-picked" : ""]
      .filter(Boolean).join(" ");
    cells.push(`<li>
        <button class="life-cal__cell${marks ? ` ${marks}` : ""}" type="button" data-life-day="${key}"
          aria-pressed="${picked ? "true" : "false"}"
          aria-label="${key}${items.length ? `，${items.length} 条记录` : "，无记录"}">
          <span class="life-cal__day">${day}</span><span class="life-cal__dots">${dots}</span>
        </button></li>`);
  }
  const monthRecords = Object.keys(byDay).sort().reverse().flatMap((key) => byDay[key]);
  const other = records.length - monthRecords.length;
  const dayRecords = lifeCal.day ? (byDay[lifeCal.day] || []) : monthRecords;
  const filter = lifeCal.day
    ? `<p class="life-cal__filter">已选 <b>${esc(lifeCal.day)}</b> · ${dayRecords.length} 条
        <button class="life-cal__clear" type="button" data-life-day="">显示整月</button></p>`
    : "";
  const rows = dayRecords.length
    ? `<ul class="life-cal__list">${dayRecords.map(lifeRecordRowHtml).join("")}</ul>`
    : `<p class="muted">${lifeCal.day ? `${esc(lifeCal.day)} 没有生活事件记录。` : "本月没有生活事件记录。"}</p>`;
  const otherNote = other > 0
    ? `<p class="muted scene-hint">另有 ${other} 条记录不在本月，见下方最近记录。</p>` : "";
  return `<div class="life-cal">
      <div class="life-cal__head">
        <button class="btn btn--sm btn--ghost" type="button" data-life-month="-1" aria-label="上一个月">‹</button>
        <b class="life-cal__month">${esc(month)}</b>
        <button class="btn btn--sm btn--ghost" type="button" data-life-month="1" aria-label="下一个月">›</button>
        <span class="chip life-cal__count">${monthRecords.length} 个事件</span>
      </div>
      <ol class="life-cal__week" aria-hidden="true">${
        ["一", "二", "三", "四", "五", "六", "日"].map((day) => `<li>${day}</li>`).join("")}</ol>
      <ol class="life-cal__grid">${cells.join("")}</ol>
      ${filter}
      ${rows}
      ${otherNote}
    </div>`;
}

/* 记录行：先标「组合律 / 原型」，再写名称——组合律是跨原型的串接，不能混为一类。 */
function lifeRecordKind(record) {
  return String((record && record.event_id) || "").includes("->") ? "组合律" : "原型";
}

function lifeRecordRowHtml(record) {
  const label = LIFE_EVENT_STATUS[record.status] || record.status;
  const kind = lifeRecordKind(record);
  const title = record.event_title || record.event_name || record.event_id;
  const amount = typeof record.amount_cents === "number" ? ` ${yuan(record.amount_cents)}` : "";
  return `<li><span class="life-cal__date">${esc(lifeDateKey(record.updated_at || record.created_at).slice(5))}</span>
    <span class="life-cal__kind">${kind}</span>
    <span class="life-cal__name" title="${esc(record.event_name || record.event_id)}">${esc(title)}</span>
    <span class="chip">${esc(label)}</span><span class="muted">${esc(amount)}</span></li>`;
}

/* ---------------- 组合任务台（L1 选事件 + 参数填写 + L4 日历入口） ---------------- */
/* 原型全部来自服务端；L1 先选一个事件，选中后展开该事件的步骤与参数，
   参数折叠展开后才出现「开始组合」，避免还没填就误触。 */
const lifePick = { event: "" };
const lifeData = { prototypes: [] };

function lifeWorkbenchHtml(prototypes) {
  const list = Array.isArray(prototypes) ? prototypes : [];
  const chosen = list.find((item) => item.event_id === lifePick.event) || list[0] || null;
  const currentId = chosen ? chosen.event_id : "";
  const chips = list.map((item) => {
    const id = typeof item.event_id === "string" ? item.event_id : "";
    const name = typeof item.name === "string" ? item.name : id;
    const on = id === currentId;
    return `<button class="life-pick${on ? " is-on" : ""}" type="button" data-life-pick="${esc(id)}"
      aria-pressed="${on ? "true" : "false"}">${esc(name)}</button>`;
  }).join("");
  let detail;
  if (chosen) {
    const eventId = typeof chosen.event_id === "string" ? chosen.event_id : "";
    const name = typeof chosen.name === "string" ? chosen.name : eventId;
    const description = typeof chosen.description === "string" ? chosen.description : "";
    const example = typeof chosen.example === "string" ? chosen.example : "";
    const steps = typeof chosen.steps === "number" ? `<span class="chip">${chosen.steps} 步</span>` : "";
    const cases = Array.isArray(chosen.cases) ? chosen.cases : [];
    const casesHtml = cases.length
      ? `<p class="life-block__k">常见事件（点一下填入对话）</p>
        <div class="life-cases">${cases.map((item) => `<button class="life-case" type="button"
          data-life-fill="${esc(item.utterance)}" title="${esc(item.utterance)}">${esc(item.label)}</button>`).join("")}</div>`
      : "";
    const fields = LIFE_FORMS[eventId] || [];
    const cta = `<button class="life-cta" type="button" data-life-send="${esc(example)}" data-event="${esc(eventId)}">开始组合</button>`;
    /* 有参数的事件：「开始组合」收进「填写参数」里，展开后才看得到并提交；
       没有参数的事件没有可展开的内容，按钮直接给出。 */
    const form = fields.length
      ? `<details class="life-form"><summary>填写参数</summary><div class="life-form__grid">
          ${fields.map((field) => `<label class="life-form__field"><span>${esc(field.label)}</span>${lifeFieldHtml(eventId, field)}</label>`).join("")}
        </div>${cta}</details>`
      : cta;
    detail = `<div class="life-detail" data-life-panel>
      <div class="life-detail__head"><b>${esc(name)}</b>${steps}
        <span class="tech mono">${esc(eventId)}</span></div>
      <p class="life-detail__desc">${esc(description)}</p>
      ${lifeDagHtml(chosen.skeleton)}
      <p class="life-detail__example">示例：${esc(example)}</p>
      ${casesHtml}
      ${form}
    </div>`;
  } else {
    detail = '<article class="state-card"><p class="state-card__k">生活事件 · 暂无原型</p>' +
      '<p class="muted">当前服务端没有可用的生活事件原型；这是空态，不是读取失败。</p></article>';
  }
  return `<section class="life-block">
      <div class="life-block__head"><b>组合任务台</b>
        <span class="muted">先说目标，系统拆成多步，逐张确认</span></div>
      <p class="life-block__k">选一个事件</p>
      <div class="life-picks" role="group" aria-label="选择生活事件">${chips}</div>
      ${detail}
    </section>`;
}

/* 对象槽默认取第一个真实选项，避免「填了金额却卡在选卡片」 */
function applyLifeDefaults(body) {
  Array.from(body.querySelectorAll("select[data-life-field]")).forEach((node) => {
    if (node.value) return;
    const first = Array.from(node.options).find((option) => option.value);
    if (first) node.value = first.value;
  });
}

function renderLifeWorkbench() {
  const body = el("life-workbench-body");
  if (!body) return;
  body.innerHTML = lifeWorkbenchHtml(lifeData.prototypes);
  applyLifeDefaults(body);
}

async function refreshLifeEvents() {
  const status = el("life-events-status");
  const list = el("life-events-list");
  const calendar = el("life-calendar");
  const body = el("life-workbench-body");
  if (!status || !list || state.offline) return;
  /* 只有首次进入才铺骨架：`refreshOptions()` 拿到对象后会再刷一次，那一次不能闪回加载态 */
  if (calendar && !calendar.querySelector(".life-cal")) {
    status.textContent = "正在读取生活事件…";
    calendar.innerHTML = '<p class="muted">正在读取生活日历…</p>';
    if (body) body.innerHTML = lifeLoadingHtml();
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
  const records = Array.isArray(payload.records) ? payload.records : [];
  const recordsNote = payload.records_available === false
    ? " · 本部署未开启记录"
    : ` · 我的记录 ${records.length} 条`;
  status.textContent = `原型 ${prototypes.length} 个${recordsNote}`;

  lifeCal.records = records;
  if (calendar) calendar.innerHTML = lifeCalendarHtml(records);
  lifeData.prototypes = prototypes;
  if (!lifePick.event && prototypes.length) {
    lifePick.event = typeof prototypes[0].event_id === "string" ? prototypes[0].event_id : "";
  }
  if (body) renderLifeWorkbench();

  const recordRows = records.length
    ? records.map((record) => {
      const label = LIFE_EVENT_STATUS[record.status] || record.status;
      const typeName = record.event_name || record.event_id;
      const title = record.event_title || typeName;
      const kind = lifeRecordKind(record);
      const amount = typeof record.amount_cents === "number" ? ` · ${yuan(record.amount_cents)}` : "";
      const metaType = title === typeName ? "" : `${typeName} · `;
      return `<article class="takeover insight-item life-record">
      <div class="life-record__head">
        <span class="life-record__kind">${kind}</span>
        <b>${esc(title)}</b>
        <span class="chip">${esc(label)}</span>
      </div>
      <div class="life-record__meta">
        <p class="muted">${esc(metaType)}${esc(momentLabel(record.updated_at || record.created_at))}${esc(amount)}</p>
        <button class="life-send" type="button" data-life-open="${esc(record.session_id)}"
                aria-label="打开会话：${esc(title)}">打开会话</button>
      </div>
      ${lifeBudgetHtml(record.budget)}
    </article>`;
    }).join("")
    : '<p class="muted">还没有生活事件记录；发起后会出现在这里。</p>';
  list.innerHTML = `<div class="life-records">${recordRows}</div>`;
}

/* 统一关弹窗：showModal 打开的必须 close()，否则保留 open 属性会留下遮罩 */
function closeDialog(dialog) {
  if (!dialog) return;
  if (typeof dialog.close === "function" && dialog.open) dialog.close();
  dialog.removeAttribute("open");
}

/* 点弹窗外（遮罩或内边距）即关闭：二级弹窗统一这个行为，避免看起来像卡住 */
function bindDialogDismiss(dialog) {
  if (!dialog) return;
  dialog.addEventListener("click", (event) => {
    if (event.target !== dialog) return;
    closeDialog(dialog);
  });
}

/* ---------------- 通用确认弹窗（替换浏览器原生 confirm / prompt） ----------------
   一次只做一件事：确认文案 + 可选登录口令都在同一个弹窗里，不再二次弹原生框。
   `onConfirm` 返回字符串即视为失败，字符串作为行内错误显示、弹窗不关闭；
   返回空串或 undefined 即视为成功，弹窗关闭。 */

let appDialogState = null;
let appDialogResolve = null;
let authModeCache = null;

function settleAppDialog(value) {
  const resolve = appDialogResolve;
  appDialogResolve = null;
  if (resolve) resolve(value);
}

/* 只有「确认 / 取消」的简单追问：返回 Promise<boolean>，Esc、点遮罩都算取消 */
function confirmAppDialog(options) {
  return new Promise((resolve) => {
    appDialogResolve = resolve;
    openAppDialog({
      ...(options || {}),
      password: false,
      onConfirm: () => {
        appDialogResolve = null;
        resolve(true);
        return "";
      },
    });
  });
}

function showAppDialogError(message) {
  const node = el("app-dialog-error");
  if (!node) return;
  node.textContent = message || "";
  node.hidden = !message;
}

function closeAppDialog() {
  appDialogState = null;
  closeDialog(el("app-dialog"));
  settleAppDialog(false);
}

function openAppDialog(options) {
  const dialog = el("app-dialog");
  const opts = options || {};
  if (!dialog) {
    /* 壳里没有弹窗户时退回原生确认，宁可丑也不能让动作静默失效 */
    const accepted = window.confirm(opts.text || "确认继续？");
    if (appDialogResolve) settleAppDialog(accepted);
    else if (accepted && opts.onConfirm) Promise.resolve(opts.onConfirm("")).catch(() => {});
    return;
  }
  const needsPassword = Boolean(opts.password);
  const title = el("app-dialog-title");
  const text = el("app-dialog-text");
  const field = el("app-dialog-field");
  const input = el("app-dialog-password");
  const confirmButton = el("app-dialog-confirm");
  if (title) title.textContent = opts.title || "确认操作";
  if (text) text.textContent = opts.text || "";
  if (field) field.hidden = !needsPassword;
  if (input) {
    input.value = "";
    input.type = "password";
    input.autocomplete = "current-password";
  }
  if (confirmButton) {
    confirmButton.textContent = opts.confirmText || "确认";
    confirmButton.className = opts.danger === false ? "btn btn--primary" : "btn btn--danger";
    confirmButton.disabled = false;
  }
  showAppDialogError("");
  appDialogState = { onConfirm: opts.onConfirm || null, busy: false };
  if (typeof dialog.showModal === "function") dialog.showModal();
  else dialog.setAttribute("open", "");
  if (needsPassword && input) input.focus();
  else if (confirmButton) confirmButton.focus();
}

async function submitAppDialog() {
  const current = appDialogState;
  if (!current || current.busy) return;
  const field = el("app-dialog-field");
  const input = el("app-dialog-password");
  const confirmButton = el("app-dialog-confirm");
  const needsPassword = Boolean(field && !field.hidden);
  const password = needsPassword && input ? input.value : "";
  if (needsPassword && !password) {
    showAppDialogError("请输入登录口令。");
    if (input) input.focus();
    return;
  }
  current.busy = true;
  const label = confirmButton ? confirmButton.textContent : "";
  if (confirmButton) {
    confirmButton.disabled = true;
    confirmButton.textContent = "处理中…";
  }
  try {
    const failure = current.onConfirm ? await current.onConfirm(password) : "";
    if (typeof failure === "string" && failure) {
      showAppDialogError(failure);
      return;
    }
    closeAppDialog();
  } catch (_) {
    showAppDialogError("操作失败，请稍后重试。");
  } finally {
    current.busy = false;
    if (confirmButton) {
      confirmButton.disabled = false;
      confirmButton.textContent = label;
    }
  }
}

/* 登录模式只读缓存：决定删除历史这类动作是否需要二次口令 */
async function currentAuthMode() {
  if (authModeCache) return authModeCache;
  try {
    const result = await apiFetch("GET", "/api/me");
    if (result.ok && result.data && result.data.auth_mode) authModeCache = result.data.auth_mode;
  } catch (_) {
    /* 读不到就按演示模式处理：多要一次口令比少要一次安全 */
  }
  return authModeCache || "demo";
}

/* 组合任务台：桌面端弹窗查看，手机端铺满整屏（同一份 DOM，CSS 切形态） */
function openLifeWorkbench() {
  const dialog = el("life-workbench");
  if (!dialog) return;
  delete dialog.dataset.dirty;
  if (typeof dialog.showModal === "function") dialog.showModal();
  else dialog.setAttribute("open", "");
}

function closeLifeWorkbench() {
  closeDialog(el("life-workbench"));
}

/* ---------------- 装配 ---------------- */

renderPresets();
function onPresetClick(event) {
  const button = event.target.closest("[data-preset]");
  const utterance = el("utterance");
  if (button && utterance) utterance.value = button.dataset.preset;
}
el("presets")?.addEventListener("click", onPresetClick);
/* 生活事件卡片：面板参数 → 同一 `/api/submit`；仍缺槽位时用同一份参数自动续答。
   卡片在弹窗（组合任务台）里，记录在页内，两处共用同一套委托。 */
document.addEventListener("click", async (event) => {
  const target = event.target instanceof Element ? event.target : null;
  if (!target) return;
  const fill = target.closest("[data-life-fill]");
  if (fill) {
    /* 常见事件只把话术填进输入框，不替用户提交 */
    const input = el("utterance");
    if (input) {
      input.value = fill.dataset.lifeFill || "";
      input.focus();
    }
    closeLifeWorkbench();
    navigate("chat", {});
    return;
  }
  const send = target.closest("[data-life-send]");
  if (send) {
    const card = send.closest("[data-life-panel]");
    const form = card ? card.querySelector(".life-form") : null;
    send.disabled = true;
    try {
      await runLifeEvent(send.dataset.event || "", form, send.dataset.lifeSend || "");
    } finally {
      send.disabled = false;
    }
    /* 计划卡出在对话视图里：关掉任务台并跳过去，否则弹窗里看不出任何反应 */
    closeLifeWorkbench();
    navigate("chat", {});
    return;
  }
  const pick = target.closest("[data-life-pick]");
  if (pick) {
    lifePick.event = pick.dataset.lifePick || "";
    renderLifeWorkbench();
    return;
  }
  const day = target.closest("[data-life-day]");
  if (day) {
    const key = day.dataset.lifeDay || "";
    lifeCal.day = lifeCal.day === key ? "" : key;
    const calendar = el("life-calendar");
    if (calendar) calendar.innerHTML = lifeCalendarHtml(lifeCal.records);
    return;
  }
  const open = target.closest("[data-life-open]");
  if (open) {
    closeLifeWorkbench();
    await openConversation(open.dataset.lifeOpen || "");
  }
});
el("btn-life-workbench")?.addEventListener("click", openLifeWorkbench);
el("btn-wealth-assessment-close")?.addEventListener("click", closeWealthAssessment);
el("btn-life-workbench-close")?.addEventListener("click", closeLifeWorkbench);
/* 四个弹窗都支持「点外面关闭」 */
[el("life-workbench"), el("wealth-assessment"), el("subscription-cancel-dialog"), el("app-dialog")]
  .forEach(bindDialogDismiss);
/* 通用确认弹窗：取消 / Esc / 点遮罩都按「取消」结算，口令框回车等于点确认 */
el("app-dialog")?.addEventListener("click", (event) => {
  const target = event.target instanceof Element ? event.target : null;
  if (target && target.closest("[data-app-dialog-close]")) closeAppDialog();
});
el("app-dialog")?.addEventListener("close", () => {
  appDialogState = null;
  settleAppDialog(false);
});
el("app-dialog-confirm")?.addEventListener("click", () => {
  submitAppDialog().catch(() => {});
});
el("app-dialog-password")?.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.isComposing) {
    event.preventDefault();
    submitAppDialog().catch(() => {});
  }
});
/* 日历翻月：只用已读到的记录重绘，不重新请求 */
el("life-calendar")?.addEventListener("click", (event) => {
  const target = event.target instanceof Element ? event.target.closest("[data-life-month]") : null;
  if (!target) return;
  const delta = Number(target.dataset.lifeMonth) || 0;
  lifeCal.month = lifeShiftMonth(lifeCal.month || lifeMonthKey(new Date()), delta);
  lifeCal.day = "";
  const calendar = el("life-calendar");
  if (calendar) calendar.innerHTML = lifeCalendarHtml(lifeCal.records);
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
/* 个人中心内容动态渲染：退出登录 / 飞书绑定都用事件委托（抽屉与「我的」视图共用） */
async function onFeishuAction(action, button) {
  const box = el("feishu-link-result");
  if (button) button.disabled = true;
  try {
    if (action === "link") {
      const res = await apiFetch("POST", "/api/channels/feishu/link-code", {});
      if (!res.ok) {
        if (box) box.innerHTML = '<p class="muted">绑定码生成失败，请稍后重试。</p>';
        return;
      }
      const data = res.data || {};
      if (box) {
        box.innerHTML =
          `<p class="mono" style="font-size:24px;letter-spacing:6px;margin:8px 0">${esc(String(data.code || ""))}</p>` +
          `<p class="muted">${esc(String(data.instruction || "在飞书里发送这 6 位数字"))}</p>` +
          `<p class="muted">${esc(String(data.expires_in_s || 300))} 秒内有效、用一次即作废；绑定完成后重新打开个人中心即可看到「已连接」。</p>`;
      }
      return;
    }
    if (action === "unlink") {
      await apiFetch("DELETE", "/api/channels/feishu/link");
      await loadProfile(el("profile-body"), "正在同步绑定状态…");
    }
  } catch (_) {
    if (box) box.innerHTML = '<p class="muted">操作失败，请稍后重试。</p>';
  } finally {
    if (button) button.disabled = false;
  }
}

async function onProfileClick(event) {
  const target = event.target instanceof Element ? event.target : null;
  if (!target) return;
  if (target.id === "btn-feishu-link") {
    await onFeishuAction("link", target);
    return;
  }
  if (target.id === "btn-feishu-unlink") {
    await onFeishuAction("unlink", target);
    return;
  }
  if (target.id !== "btn-logout") return;
  try {
    await apiFetch("POST", "/api/auth/logout", {});
  } catch (_) {
    /* 退出失败也回登录页：Cookie 可能已失效 */
  }
  window.location.replace("/");
}
el("profile-body").addEventListener("click", onProfileClick);
if (el("view-me-body")) el("view-me-body").addEventListener("click", onProfileClick);
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
  /* 等级条上的「重新测评 / 开始答题」：打开问卷弹窗 */
  if (target.closest("[data-wealth-retake]")) {
    openWealthAssessment();
    return;
  }
  if (target.closest('[data-action="save-budget"]')) {
    await saveBudget();
    return;
  }
  const ack = target.closest("[data-insight-ack]");
  if (ack) {
    acknowledgeInsight(ack.dataset.insightAck || "");
    renderInsightCards();
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
  charges: () => apiFetch("GET", "/api/options"),
};

const RECORD_HEADERS = {
  transfers: ["记账日", "收款人", "金额", "状态", "渠道", "记录号", "详情"],
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
      `<span class="mono">${esc(item.record_id || "—")}</span>`,
      `<details class="records-detail"><summary>详情</summary>
        <p class="muted">${esc(item.kind === "scheduled" ? "定时登记（本次不扣款）" : "即时转账")}` +
        ` · 账户 ${esc(item.account || "—")}</p>
        <p class="muted">备注 ${esc(item.remark || "—")}</p>
        <p class="muted mono">记录号 ${esc(item.record_id || "—")}</p>
      </details>`,
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
  if (key === "charges") {
    /* 订阅扣款证据：`/api/options` 的 subscription_details.insights[].evidence[]，
       同一笔流水可能被多条洞察引用，按 record_id 去重。 */
    const details = data.subscription_details || {};
    const seen = new Set();
    const rows = [];
    (details.insights || []).forEach((item) => {
      (item.evidence || []).forEach((evidence) => {
        const key0 = evidence.record_id || `${evidence.booked_on}-${evidence.amount_cents}`;
        if (seen.has(key0)) return;
        seen.add(key0);
        const moment = evidence.booked_at ? String(evidence.booked_at).slice(11, 16) : "";
        rows.push(recordsRow([
          esc(evidence.merchant_label || item.merchant || "—"),
          `<span class="mono">${yuan(Math.abs(Number(evidence.amount_cents) || 0))}</span>`,
          `<span class="mono">${esc(evidence.booked_on || "—")}${moment ? " " + esc(moment) : ""}</span>`,
          esc(recordChannelLabel(evidence.channel)),
          `<span class="mono">${esc(evidence.record_id || "—")}</span>`,
        ]));
      });
    });
    return rows;
  }
  return [];
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

function showScene(key) {
  const switching = currentScene !== key;
  currentScene = Object.prototype.hasOwnProperty.call(SCENES, key) ? key : "transfer";
  /* 切场景时丢掉上一页未确认的计划卡：隐藏页里的「确认执行」不该还能被点到 */
  if (switching && scenePending) clearSceneCard();
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
  /* 选中态只写在可见导航容器里（左栏 / 底部标签栏 / 我的）；
     隐藏视图里的「去对话」「进入功能页」等按钮一律清空，避免读屏与观感上都像是「对话被选中」。 */
  document.querySelectorAll("[data-nav]").forEach((button) => button.removeAttribute("aria-current"));
  [el("app-nav"), el("btn-me-side"), el("app-tabbar")].forEach((scope) => {
    if (!scope) return;
    const buttons = scope.hasAttribute && scope.hasAttribute("data-nav")
      ? [scope]
      : Array.from(scope.querySelectorAll("[data-nav]"));
    buttons.forEach((button) => {
      const active = button.dataset.nav === next
        && (!button.dataset.scene || button.dataset.scene === currentScene);
      if (active) button.setAttribute("aria-current", "page");
    });
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
  const subCancel = target.closest("[data-sub-cancel]");
  if (subCancel) {
    openSubCancelDialog(subCancel);
    return;
  }
  if (target.closest("[data-sub-cancel-confirm]")) {
    confirmSubCancel().catch(() => {});
    return;
  }
  if (target.closest("[data-sub-cancel-close]")) {
    closeSubCancelDialog();
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
  /* 场景页「页内办事」：快速转账 / 计划卡确认 / 撤销 / 去对话强验证 */
  if (target.closest("#quick-transfer-submit")) {
    submitQuickTransfer().catch(() => {});
    return;
  }
  if (target.closest("[data-scene-confirm]")) {
    confirmSceneCard().catch(() => {});
    return;
  }
  if (target.closest("[data-scene-dismiss]")) {
    clearSceneCard();
    return;
  }
  if (target.closest("[data-scene-chat], [data-scene-goto-chat]")) {
    const input = el("utterance");
    if (input && sceneLastUtterance) input.value = sceneLastUtterance;
    navigate("chat", {});
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

/* 快速转账表单：两个输入框里按回车等同点「确认转账」 */
["quick-payee", "quick-amount"].forEach((id) => {
  el(id)?.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      submitQuickTransfer().catch(() => {});
    }
  });
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
navigator.serviceWorker.register("/assets/sw.js?v=59", { scope: "/" }).catch(() => {
    timelineAdd("PWA 静态壳注册失败，当前仍可作为同源网页使用。", "warn");
  });
}
setInterval(() => {
  refreshInsights().catch(() => {});
}, 15000);

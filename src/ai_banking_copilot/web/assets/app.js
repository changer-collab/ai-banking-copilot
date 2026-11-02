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
  "代扣解约",
  "查我的代扣协议",
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
  monthly_day: "每月转账日",
  period: "扣费周期", merchant: "商户名称", product: "理财产品",
  delivery_address: "配送地址", delivery_time: "送达时间", order_on: "订花日期",
  loss_type: "挂失类型",
};
const MONEY_SLOTS = new Set(["amount", "amount_cents", "daily_limit"]);
const MONEY_UNIT = /[元块万亿]/;

/* 金额槽只在**缺单位**时补「元」：下拉选项的文案和值本身已带「元」，
   再拼一次就是实测里那条「经典玫瑰 · 299 元 元」 */
function withMoneyUnit(slot, value) {
  const text = String(value || "").trim();
  if (!MONEY_SLOTS.has(slot) || !text) return text;
  return MONEY_UNIT.test(text) ? text : `${text} 元`;
}

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
  review: null,
  reviewBaseline: null,
  reviewConfirmed: false,
  reviewEditFrom: null,
  reviewError: "",
  decision: null,
  questions: [],
  stepStates: [],
  nodeStatus: {},
  phase: "idle",
  chain: [],
  verification: null,
  clarifications: [],
  triage: null,
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
  archiveReadOnly: false,
  operationUnknown: false,
  insights: [],
  wealth: { products: [], holdings: [] },
  wealthCompare: [],
  cards: [],
  bills: null,
  billsPeriod: null,
  billsPeriodChoice: "current",
  billsPeriodCounts: {},
  billsMerchantSort: "amount",
  billsTrendPeriod: "",
  billsHighlightTxn: "",
  pendingBillLead: null,
  billCategoryTarget: null,
  answerNote: "",
  offline: typeof navigator !== "undefined" && navigator.onLine === false,
  model: null,
  workflow: null,
  draft: null,
  draftEditing: false,
  draftError: "",
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
  /* 当日额度只属于转账：理财申购 / 冻结 / 下单没有「本人当日限额」这条口径，
     挂上去会让用户把转账阈值误读成理财限额（实测截图里就出现了 1000 / 已用 4000）。 */
  const transfer = (card.nodes || []).some(
    (node) => String((node && node.action) || "").startsWith("transfer."),
  );
  if (!transfer) return "";
  const limit = card.daily_limit_cents;
  const used = card.daily_accumulated_cents;
  if (typeof limit !== "number" || typeof used !== "number") return "";
  const remaining = limit - used;
  /* 转账的 1000 元是黄/红分档阈值，不是硬限额：用「免验证额度」表述，
     避免出现「已超额却仍可验证执行」的口径冲突。 */
  const limitLabel = "免验证额度";
  const head = `${limitLabel} <b class="quota__val">${yuan(limit)}</b> · 已用 <b class="quota__val">${yuan(used)}</b>`;
  if (remaining < 0) {
    const overLabel = transfer ? "超出" : "已超额";
    return `<p class="quota quota--over">${head} · ${overLabel} <b class="quota__val">${yuan(-remaining)}</b></p>`;
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
   `finally` 里补 done。前端 250ms 轮询 + 400ms 最短展示，答案生成后收起。 */
const PROGRESS_POLL_MS = 250;
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
  "funds.freeze": "锁定预算（资金预留）",
  "funds.release": "释放剩余",
  "merchant.order": "向商户下单",
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

/* 任务图上的场景标签：动作前缀 → 场景名。只做展示映射，不推断能力。 */
const LIFE_DOMAIN_LABEL = {
  funds: "资金",
  merchant: "商户",
  transfer: "转账",
  wealth: "理财",
  bills: "账单",
  subscription: "订阅",
  card: "卡片",
  account: "账户",
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
  /* 请求一旦成功就说明链路已恢复：离线标记必须自己解除，否则按钮会一直禁用 */
  if (state.offline) setOfflineState(false);
  let data = {};
  try {
    data = await response.json();
  } catch (_) {
    data = {};
  }
  /* 未登录只读壳里 401 是预期结果（只读 GET 才放行），保持页面不跳，写操作由点击拦截带走。 */
  if (response.status === 401 && document.documentElement.dataset.demoReadonly !== "1") {
    window.location.replace("/login");
  }
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

async function patchDraft(draftId, changes) {
  state.requestLabel = "正在重新核对并冻结计划…";
  showRequestStatus();
  try {
    return await apiFetch(
      "PATCH",
      `/api/drafts/${encodeURIComponent(draftId)}?session_id=${encodeURIComponent(SESSION_ID)}`,
      { changes },
    );
  } finally {
    state.requestLabel = "";
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
      /* 0ms 不代表「没理解」：模板通道本来就在 1ms 内完成。小于 1ms 如实写 <1ms。 */
      const cost = Number.isFinite(stage.duration_ms)
        ? ` · ${stage.duration_ms > 0 ? `${stage.duration_ms}ms` : "<1ms"}`
        : "";
      return `<li class="run-chain__step run-chain__step--${phaseName}">${esc(stage.label)}${esc(cost)}</li>`;
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

/* 链路跑完 ≠ 事情办完：按服务端真实状态收口，别让「已完成 5/5」盖住「还在等用户补参数」 */
const RUN_OUTCOME_HINTS = {
  needs_clarification: "待补充 · 未执行",
  awaiting_confirm: "待确认 · 未执行",
  blocked: "已拒绝 · 未执行",
  completed: "已执行 · 见回执",
  idle: "已结束",
};

function markRunOutcome(stateName) {
  if (!state.response) return;
  const root = state.response.querySelector(".run-chain");
  if (!root) return;
  const hint = root.querySelector("[data-run-hint]");
  const label = RUN_OUTCOME_HINTS[stateName];
  if (!hint || !label) return;
  hint.textContent = label;
  root.classList.remove("is-running");
  root.classList.add("is-done");
  /* 收口之后再让轮询把「已完成 N/N」写回去就是状态误报：直接停掉本次请求的进度渲染 */
  if (progress.timer) window.clearInterval(progress.timer);
  progress.timer = null;
  progress.requestId = "";
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
  el("business-date").textContent = `演示数据日期：${state.options.today} · 使用模拟资金和演示商户订单`;
  renderSubscriptionDetails(state.options);
  renderQuota(state.options.quota);
  renderTransferAccounts(state.options);
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
    <div class="btn-row"><button class="btn btn--primary" type="button" data-action="submit-assessment" data-requires-login>提交测评</button></div>
    <div class="wealth-confirm" data-assessment-confirm hidden>
      <p class="wealth-confirm__text">提交后等级按服务端算分更新，年内有效；每题只有一次选择机会。</p>
      <div class="btn-row">
        <button class="btn btn--sm btn--primary" type="button" data-action="assessment-confirm" data-requires-login>确认提交</button>
        <button class="btn btn--sm btn--ghost" type="button" data-assessment-back>返回修改</button>
      </div>
    </div>
    <p class="muted wealth-form__limit">演示环境：每日最多 3 次测评</p>
  </form>`;
}

/* 等级条：等级由服务端算分定级，前端只展示；「重新测评」只负责展开问卷。
   来源必须显示：`seed` 是演示播种，**不是**真实测评，后端已在 `source_label` 写明口径，
   前端只负责把它放到等级旁边（早前只藏在产品卡的推荐理由折叠区里，实测 WEA-P04）。 */
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
  const demo = info.source === "seed";
  const sourceText = info.source_label && !demo ? ` · ${info.source_label}` : "";
  /* 测评次数：单日 2 次、12 个月 8 次（《金融机构产品适当性管理办法》第二十九条）。
     接口没给数字时不编造；演示环境按模拟业务日计数。 */
  const retake = info.retake || {};
  const remainingToday = Number.isFinite(Number(retake.remaining_today))
    ? Number(retake.remaining_today)
    : null;
  const remaining12m = Number.isFinite(Number(retake.remaining_12m))
    ? Number(retake.remaining_12m)
    : null;
  const usedUp = (remainingToday !== null && remainingToday <= 0)
    || (remaining12m !== null && remaining12m <= 0);
  const retakeLabel = valid ? "重新测评" : "开始答题";
  const retakeText = remainingToday === null && remaining12m === null
    ? retakeLabel
    : `${retakeLabel}（今日还可 ${remainingToday ?? "—"} 次 / 12 个月还可 ${remaining12m ?? "—"} 次）`;
  const demoNote = demo
    ? `<p class="muted wealth-grade__warn">该等级来自演示种子，不是你的真实测评；点「${retakeLabel}」可换成问卷结果。</p>`
    : "";
  const nextOn = retake.next_available_on ? `下次可测：${retake.next_available_on}` : "";
  const limitNote = usedUp
    ? `<p class="muted wealth-grade__warn">测评次数已用完（依据第二十九条：单日不超过 2 次、12 个月累计不超过 8 次）；${esc(nextOn)}。</p>`
    : "";
  const expiryNote = info.state === "expired"
    ? '<p class="muted wealth-grade__warn">测评已过期，所有申购入口已阻断；请先重新测评。</p>'
    : info.expiring_soon
      ? `<p class="muted wealth-grade__warn">测评将于 ${esc(String(info.days_until_expiry))} 天后过期（${esc(info.expires_on || "—")}），请提前重新评估。</p>`
      : "";
  return `<div class="wealth-grade">
    <div class="wealth-grade__row">
      <span class="wealth-grade__k">风险等级</span>
      <span class="wealth-grade__level">
        <span class="chip ${valid ? "chip--green" : "chip--yellow"}">${esc(level)}</span>
        ${demo ? '<span class="chip chip--yellow">演示数据</span>' : ""}
      </span>
    </div>
    <div class="wealth-grade__row">
      <span class="wealth-grade__note">${esc(meta + sourceText)}</span>
      <button class="btn btn--sm wealth-grade__btn" type="button" data-wealth-retake${
        usedUp ? " disabled" : ""}>${esc(retakeText)}</button>
    </div>
    ${demoNote}${limitNote}${expiryNote}
    <p class="muted wealth-grade__warn">依据《金融机构产品适当性管理办法》第二十九条。</p>
  </div>`;
}

/* 产品卡只保留五要素：名称+风险/类别、业绩比较基准+期限+起购、一句「为什么选它」、
   产品详情/立即申购。费率、流动性、净值图、登记编码全部进右侧详情抽屉。
   不可买产品只保留一个「为什么不能买」，并给出重新测评/查看匹配产品的出路。 */
function wealthWhyNotBuyHtml(item) {
  if (item.reason_code === "DISCLOSURE_INCOMPLETE") {
    return `<details class="wealth-why">
      <summary>为什么不能买</summary>
      <p>产品披露信息不完整，暂不可售；可点「产品详情」查看缺失字段。</p>
    </details>`;
  }
  return `<details class="wealth-why">
    <summary>为什么不能买</summary>
    <p>按当前测评等级不能买。可以重新测评，或查看与你当前等级匹配的产品。</p>
    <div class="btn-row">
      <button class="btn btn--sm" type="button" data-wealth-retake>重新测评</button>
      <button class="btn btn--sm" type="button" data-wealth-matched="${esc(item.category || "")}">查看可买的同类产品</button>
    </div>
  </details>`;
}

function wealthProductHtml(item, options) {
  const opts = options || {};
  const disabled = item.eligible !== true;
  const minValue = Number(item.min_amount_yuan);
  const minText = Number.isFinite(minValue) ? yuan(Math.round(minValue * 100)) : "未知";
  const termValue = Number(item.term_days);
  const termText = Number.isFinite(termValue)
    ? (termValue === 0 ? "期限 0 天（随时申赎）" : `期限 ${termValue} 天`)
    : "期限未知";
  const benchmark = String(item.benchmark_label || "业绩比较基准未知")
    .replace("（模拟）", " ")
    .replace(/\s+/g, " ")
    .trim() || "业绩比较基准未知";
  const rawPick = disabled
    ? String(item.reason || "当前不可买")
    : String(item.advantage_line || item.recommend_reason || "风险等级与你的测评匹配");
  const pick = /[。！？]$/.test(rawPick) ? rawPick : `${rawPick}。`;
  const detail = `<button class="btn btn--sm" type="button"
    data-wealth-detail="${esc(item.product_id)}">产品详情</button>`;
  const action = disabled
    ? `<span class="chip chip--denied">不可买</span>${detail}`
    : `${detail}<button class="btn btn--sm btn--primary" type="button"
        data-wealth-subscribe="${esc(item.product_id)}" data-requires-login>立即申购</button>`;
  return `<article class="wealth-row${disabled ? " wealth-row--off" : ""}"
    data-wealth-cat-row="${esc(item.category || "")}">
    <div class="wealth-row__main">
      <div class="wealth-row__name"><b>${esc(item.name)}</b>
        <span class="chip">${esc(item.risk_level)}</span>
        ${item.category ? `<span class="chip">${esc(item.category)}</span>` : ""}
        ${item.disclosure_complete === false ? '<span class="chip chip--denied">暂不可售</span>' : ""}
        <label class="wealth-compare__pick"><input type="checkbox"
          data-wealth-compare="${esc(item.product_id)}"
          ${(state.wealthCompare || []).includes(item.product_id) ? "checked" : ""}>对比</label></div>
      <p class="wealth-row__meta">${esc(benchmark)} ｜ ${esc(termText)} ｜ 起购 ${minText}</p>
      <p class="${disabled ? "wealth-reason" : "wealth-row__pick"}">${opts.showRecommend ? "<b>推荐理由：</b>" : ""}${esc(pick)}</p>
    </div>
    <div class="wealth-row__act">${action}</div>
    <p class="muted wealth-error" data-wealth-error hidden></p>
    ${disabled ? wealthWhyNotBuyHtml(item) : ""}
  </article>`;
}

/* 「我要申购理财产品」的推荐视图：服务端已按测评等级硬筛选，前端只做分类过滤。
   产品卡、详情与申购弹窗全部复用理财页同一套组件，避免两套口径。 */
function wealthRecommendHtml(payload) {
  const data = payload || {};
  const products = Array.isArray(data.products) ? data.products : [];
  const categories = Array.isArray(data.categories) ? data.categories : [];
  const cards = products.map((item) => `<div data-wealth-card-cat="${esc(item.category || "未分类")}">
      ${wealthProductHtml(item, { showRecommend: true })}
    </div>`).join("");
  const chips = categories.length
    ? `<div class="btn-row wealth-recommend__cats">
        <button class="btn btn--sm" type="button" data-wealth-cat="" aria-pressed="true">全部</button>
        ${categories.map((item) => `<button class="btn btn--sm" type="button"
          data-wealth-cat="${esc(item.value)}" aria-pressed="false">${esc(item.label)}</button>`).join("")}
      </div>`
    : "";
  const source = data.data_source ? ` · 数据来源：${esc(data.data_source)}` : "";
  return `<div class="takeover wealth-recommend">
    <p class="wealth-invite">${esc(data.message || "先看可申购产品。")}</p>
    ${chips}
    <div class="wealth-recommend__list">${cards || `<p class="muted">当前没有可申购产品。</p>
      <div class="btn-row"><button class="btn btn--sm" type="button" data-wealth-retake>重新测评</button></div>`}</div>
    <p class="muted">${esc(data.disclaimer || "")}${source}</p>
  </div>`;
}

/* 产品对比：勾选 2–3 只，只比事实字段；数据全部来自同一份 `/api/wealth/overview`。 */
function renderWealthCompareBar() {
  const bar = el("wealth-compare-bar");
  if (!bar) return;
  const ids = state.wealthCompare || [];
  if (!ids.length) {
    bar.hidden = true;
    bar.innerHTML = "";
    return;
  }
  bar.hidden = false;
  bar.innerHTML = `<span class="wealth-compare-bar__count">已选 ${ids.length}/3 只</span>
    <button class="btn btn--sm btn--primary" type="button" data-wealth-compare-open
      ${ids.length < 2 ? "disabled" : ""}>对比</button>
    <button class="btn btn--sm btn--ghost" type="button" data-wealth-compare-clear>清空</button>`;
}

function wealthCompareHtml(products) {
  const facts = [
    ["风险等级", (item) => item.risk_level || "—"],
    ["业绩比较基准", (item) => String(item.benchmark_label || "—").replace("（模拟）", " ")],
    ["期限", (item) => {
      const term = Number(item.term_days);
      return Number.isFinite(term) ? (term === 0 ? "0 天（随时申赎）" : `${term} 天`) : "—";
    }],
    ["起购", (item) => (item.min_amount_yuan ? `¥${item.min_amount_yuan}` : "—")],
    ["费率", (item) => {
      const fees = item.fees || {};
      return `申购 ${fees.subscription || "未知"} · 销售服务 ${fees.service || "未知"}/年 · 托管 ${fees.custody || "未知"}/年`;
    }],
    ["流动性", (item) => (item.liquidity && item.liquidity.label) || "未知"],
  ];
  const head = products.map((item) =>
    `<th>${esc(item.name)}<span class="chip">${esc(item.risk_level || "—")}</span></th>`
  ).join("");
  const body = facts.map(([label, read]) =>
    `<tr><th>${label}</th>${products.map((item) => `<td>${esc(read(item) || "—")}</td>`).join("")}</tr>`
  ).join("");
  return `<div class="wealth-compare">
    <header class="wealth-compare__head"><b>产品对比</b>
      <button class="btn btn--sm btn--ghost" type="button" data-wealth-compare-close>关闭</button></header>
    <div class="wealth-compare__scroll"><table>
      <thead><tr><th>对比项</th>${head}</tr></thead>
      <tbody>${body}</tbody>
    </table></div>
  </div>`;
}

function openWealthCompare() {
  const ids = state.wealthCompare || [];
  const products = ids
    .map((id) => (state.wealth.products || []).find((item) => item.product_id === id))
    .filter(Boolean);
  if (products.length < 2) return;
  let dialog = el("wealth-compare-dialog");
  if (!dialog) {
    dialog = document.createElement("dialog");
    dialog.id = "wealth-compare-dialog";
    dialog.className = "wealth-sheet wealth-compare-dialog";
    document.body.appendChild(dialog);
    dialog.addEventListener("click", (event) => {
      if (event.target.closest("[data-wealth-compare-close]")) closeDialog(dialog);
    });
    bindDialogDismiss(dialog);
  }
  dialog.innerHTML = wealthCompareHtml(products);
  dialog.querySelectorAll("[data-wealth-compare-close]").forEach((button) => {
    button.addEventListener("click", () => closeDialog(dialog));
  });
  if (typeof dialog.showModal === "function") dialog.showModal();
  else dialog.setAttribute("open", "");
}

/* 持仓行：份额 / 最新净值 / 市值 / 累计收益 + 到期或下一开放日 + 赎回入口。
   净值与收益均来自模拟序列；无净值数据时如实标「未知」，不补 0、不推算。 */
function wealthHoldingHtml(item) {
  const redeemable = item.redeemable !== false;
  const shares = item.shares || "—";
  const navNow = item.nav_now || "未知";
  const marketValue = item.market_value_yuan ? `¥${item.market_value_yuan}` : "未知";
  const income = item.income_short || item.current_income || "未知";
  const nextOpen = item.next_open_on || item.maturity_on || "无固定到期日";
  return `<article class="wealth-row">
    <div class="wealth-row__main">
      <div class="wealth-row__name"><b>${esc(item.name)}</b>
        <span class="chip">${esc(item.risk_level)}</span>
        ${item.category ? `<span class="chip">${esc(item.category)}</span>` : ""}</div>
      <p class="wealth-row__meta">持有 ${esc(shares)} 份 · 最新净值 ${esc(navNow)}
        · 市值 ${esc(marketValue)} · 累计收益（模拟）${esc(income)}</p>
      <p class="wealth-row__meta">起息 ${esc(item.purchased_on)}
        · 到期/下一开放日 ${esc(nextOpen)}
        · 到账 ${esc(item.arrival_label || "未知")}</p>
      ${redeemable ? "" : `<p class="muted wealth-reason">${esc(item.redeem_reason || "封闭期内不可赎回")}</p>`}
    </div>
    <div class="wealth-row__act">
      <button class="btn btn--sm" type="button" data-wealth-redeem="${esc(item.product_id)}"
        data-requires-login${redeemable ? "" : " disabled"}>赎回</button>
    </div>
    <p class="muted wealth-error" data-wealth-error hidden></p>
  </article>`;
}

/* ---------------- 理财：场景页可视化（等级条 / 持仓占比 / 持仓合计） ---------------- */

function holdingsTotal(holdings) {
  return (holdings || []).reduce((sum, item) => sum + (Number(item.amount_yuan) || 0), 0);
}

/* 持仓占比：先按**产品大类**聚合（货币 / 债券 / 混合 / 权益），产品级明细留在下方列表。
   金额全部来自接口，只做本人持仓内部的相对占比；未持有的大类也列出并标「未持有」。 */
function holdingsCategoryRows(holdings) {
  const totals = new Map(WEALTH_CATEGORY_ORDER.map((name) => [name, 0]));
  (holdings || []).forEach((item) => {
    const name = item.category || "未分类";
    totals.set(name, (totals.get(name) || 0) + (Number(item.amount_yuan) || 0));
  });
  return Array.from(totals.entries()).map(([label, value], index) => ({
    label,
    value,
    note: value > 0 ? "" : "¥0.00 · 未持有",
    color: sceneColor(index),
  }));
}

function holdingsShareHtml(holdings) {
  const rows = holdingsCategoryRows(holdings);
  const total = rows.reduce((sum, row) => sum + row.value, 0);
  if (!total) {
    return `<div class="wealth-viz__block">
      <p class="wealth-viz__k">持仓占比（按大类）</p>
      <p class="muted">当前没有持仓，暂无可计算的占比。</p>
    </div>`;
  }
  return `<div class="wealth-viz__block">
    <p class="wealth-viz__k">持仓占比（按大类）</p>
    ${donutChartHtml(rows, {
      label: "本人持仓大类占比",
      size: 132,
      stroke: 15,
      empty: "暂无可绘制占比",
      percentDigits: 1,
      showZeroRows: false,
    })}
    <p class="muted wealth-viz__note">金额来自本人持仓事实；
      <button class="btn btn--sm btn--ghost" type="button" data-scroll-wealth-holdings>查看产品级持仓明细</button>
    </p>
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
  const unknown = rows.filter((row) => incomeUnknown(row) || row.income_cents == null).length;
  const note = unknown === rows.length
    ? `收益未知（${rows.length} 笔均无净值数据，不推算盈亏）`
    : unknown
      ? `收益未知（${unknown}/${rows.length} 笔无净值数据，不推算盈亏）`
      : "收益为「参考收益（模拟净值）」，由演示净值折算，不是真实行情";
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
  const assessment = payload.assessment || {};
  /* 产品目录同时是「产品」槽的候选来源：先落到 state，再渲染。
     测评问卷也放进 state：弹窗里留下回执卡后，点「再测一次」要靠它重建问卷。 */
  state.wealth = {
    assessment,
    products: payload.products || [],
    holdings: payload.holdings || [],
  };
  wealthRendered = true;
  const status = el("wealth-status");
  if (status) {
    const level = assessment.state === "valid" && assessment.risk_profile
      ? assessment.risk_profile
      : "未测评";
    const total = holdingsTotal(payload.holdings || []);
    status.textContent = `已载入你的风险测评（${level}）与持仓（${yuan(Math.round(total * 100))}）`;
  }
  const viz = el("wealth-viz");
  if (viz) viz.innerHTML = wealthVizHtml(payload);
  const box = el("wealth-assessment-body");
  /* 问卷只放在弹窗里，页面上只留「重新测评 / 开始答题」一个入口；
     表单一经渲染就保留用户已选答案：只在既没有表单、也没有提交成功卡时重建
     （早前只看有没有表单，导致提交成功卡被空白问卷当场覆盖 —— 实测 WEA-P08①） */
  if (box && !box.querySelector("[data-assessment-form]")
    && !box.querySelector("[data-assessment-result]")) {
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
  const validIds = new Set((payload.products || []).map((item) => String(item.product_id)));
  state.wealthCompare = (state.wealthCompare || []).filter((id) => validIds.has(String(id)));
  renderWealthCompareBar();
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

function assessmentResultHtml(outcome, withActions) {
  const result = (outcome && outcome.result) || {};
  const assessment = (outcome && outcome.assessment) || {};
  const level = result.risk_profile || assessment.risk_profile || "—";
  const score = result.score === undefined ? "—" : `${result.score} / ${result.max_score}`;
  /* 面板弹窗里给「再测一次」；聊天内那份表单没有对应弹窗，不放按钮以免点了没反应 */
  const actions = withActions
    ? '<div class="btn-row"><button class="btn btn--sm" type="button" data-wealth-retake>再测一次</button></div>'
    : "";
  /* `data-assessment-result` 是「成功卡已渲染」的标记：`renderWealth` 据此不再重建空白问卷 */
  return `<div class="takeover wealth-result" data-assessment-result>
    <div><b>测评完成：${esc(level)}</b> <span class="chip chip--green">按问卷重新定级</span></div>
    <p>得分 ${esc(score)}（等级由服务端按得分算出，不能自填）。</p>
    <p class="muted">测评日 ${esc(assessment.assessed_on || "—")} · 有效期至 ${esc(assessment.expires_on || "—")}</p>
    <p class="muted">产品目录已按新等级重新判定可申购范围。</p>
    ${actions}
  </div>`;
}

/* 收答案：只回传「题号 → 选项号」，分值一律由服务端算。 */
function assessmentAnswers(form) {
  const answers = {};
  form.querySelectorAll("input[data-question]:checked").forEach((node) => {
    answers[node.dataset.question] = node.value;
  });
  const questions = Array.from(new Set(Array.from(form.querySelectorAll("input[data-question]"))
    .map((node) => node.dataset.question)));
  return { answers, missing: questions.filter((id) => !answers[id]) };
}

function assessmentFormError(form, text) {
  const error = form.querySelector("[data-form-error]");
  if (!error) return;
  error.hidden = !text;
  error.textContent = text || "";
}

/* 「提交测评」不直接落库：先逐题校验，再过一道二次确认（实测 WEA-P08②）。 */
function openAssessmentConfirm(trigger) {
  const form = trigger.closest("[data-assessment-form]");
  if (!form) return;
  const { missing } = assessmentAnswers(form);
  if (missing.length) {
    assessmentFormError(form, `还有 ${missing.length} 道题没有选，请逐题完成后提交。`);
    return;
  }
  assessmentFormError(form, "");
  const bar = form.querySelector("[data-assessment-confirm]");
  if (bar) bar.hidden = false;
  const confirm = form.querySelector('[data-action="assessment-confirm"]');
  if (confirm) confirm.focus();
}

function closeAssessmentConfirm(bar) {
  if (bar) bar.hidden = true;
}

let pendingAssessmentAnswers = null;
let pendingAssessmentPrefix = "panel";

const RISK_ORDER = { R1: 1, R2: 2, R3: 3, R4: 4, R5: 5 };

function assessmentReconfirmHtml(data) {
  const result = data.result || {};
  const previous = data.previous_profile || "—";
  const next = result.risk_profile || "—";
  const diff = Math.abs((RISK_ORDER[next] || 0) - (RISK_ORDER[previous] || 0));
  const countdown = diff >= 2
    ? '<p class="muted" data-reconfirm-countdown>冷静期 10 秒…</p>'
    : "";
  return `<div class="takeover wealth-result" data-assessment-reconfirm>
    <div><b>本次结果 ${esc(next)}，与最近一次 ${esc(previous)} 不一致</b>
      <span class="chip chip--yellow">需再次确认</span></div>
    <p>依据《金融机构产品适当性管理办法》第二十九条，结果与最近一次不一致时需要提示并请投资者再次确认。</p>
    <p class="muted">演示环境用冷静期替代人工复核；人工复核未接入。</p>
    ${countdown}
    <div class="btn-row">
      <button class="btn btn--sm btn--primary" type="button" data-action="assessment-reconfirm"
        data-requires-login>再次确认提交</button>
    </div>
  </div>`;
}

function startReconfirmCountdown(data) {
  const result = data.result || {};
  const diff = Math.abs(
    (RISK_ORDER[result.risk_profile] || 0) - (RISK_ORDER[data.previous_profile] || 0)
  );
  if (diff < 2) return;
  const button = document.querySelector('[data-action="assessment-reconfirm"]');
  const note = document.querySelector("[data-reconfirm-countdown]");
  if (!button) return;
  let left = 10;
  button.disabled = true;
  const tick = () => {
    if (!document.body.contains(button)) return;
    if (left <= 0) {
      button.disabled = false;
      if (note) note.textContent = "冷静期已结束，可以再次确认。";
      return;
    }
    if (note) note.textContent = `冷静期 ${left} 秒…`;
    left -= 1;
    window.setTimeout(tick, 1000);
  };
  tick();
}

async function onAssessmentSubmit(form) {
  const { answers, missing } = assessmentAnswers(form);
  if (missing.length) {
    assessmentFormError(form, `还有 ${missing.length} 道题没有选，请逐题完成后提交。`);
    return;
  }
  const result = await post("/api/risk-assessment", { answers });
  if (result.status !== 200) {
    assessmentFormError(form, (result.data && result.data.detail) || "提交未被接受，请核对答案后重试。");
    return;
  }
  if (result.data && result.data.state === "needs_reconfirmation") {
    pendingAssessmentAnswers = answers;
    pendingAssessmentPrefix = form.dataset.assessmentForm || "panel";
    const host = form.parentNode;
    if (host) {
      const holder = document.createElement("div");
      holder.innerHTML = assessmentReconfirmHtml(result.data);
      const card = holder.firstElementChild;
      if (card) host.replaceChild(card, form);
    }
    startReconfirmCountdown(result.data);
    return;
  }
  /* 成功卡替换掉表单本身（面板弹窗与聊天内两处表单都适用），
     并带 `data-assessment-result` 标记，`renderWealth` 不会再用空白问卷覆盖它 */
  const host = form.parentNode;
  if (host) {
    const holder = document.createElement("div");
    holder.innerHTML = assessmentResultHtml(result.data, form.dataset.assessmentForm === "panel");
    const card = holder.firstElementChild;
    if (card) host.replaceChild(card, form);
  }
  await refreshWealth();
  timelineAdd(`风险测评已提交：服务端算分定级 ${result.data.result.risk_profile}`, "done");
}

async function submitAssessmentReconfirm(trigger) {
  if (!pendingAssessmentAnswers) return;
  trigger.disabled = true;
  const result = await post("/api/risk-assessment", {
    answers: pendingAssessmentAnswers,
    confirm_change: true,
  });
  if (result.status !== 200) {
    trigger.disabled = false;
    timelineAdd(
      `再次确认未通过：${(result.data && result.data.detail) || "请稍后重试"}`,
      "stop"
    );
    return;
  }
  const host = trigger.closest("[data-assessment-reconfirm]");
  if (host) {
    const holder = document.createElement("div");
    holder.innerHTML = assessmentResultHtml(
      result.data,
      pendingAssessmentPrefix === "panel"
    );
    const card = holder.firstElementChild;
    if (card) host.replaceWith(card);
  }
  pendingAssessmentAnswers = null;
  await refreshWealth();
  timelineAdd(
    `风险测评已确认：服务端算分定级 ${result.data.result.risk_profile}`,
    "done"
  );
}

/* 测评问卷是弹窗：入口只有等级条上的「重新测评 / 开始答题」一个按钮。
   上一次提交成功后弹窗里留下的是回执卡，点「再测一次」要把问卷放回来。 */
function openWealthAssessment() {
  const dialog = el("wealth-assessment");
  if (!dialog) return;
  const box = el("wealth-assessment-body");
  const questionnaire = (state.wealth && state.wealth.assessment
    && state.wealth.assessment.questionnaire) || [];
  if (box && !box.querySelector("[data-assessment-form]")) {
    box.innerHTML = assessmentFormHtml("panel", questionnaire);
  }
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
  if (!box) {
    /* 空消息是「清掉上一次的提示」，没有行内位就什么都不用做 */
    if (message) timelineAdd(message, "stop");
    return;
  }
  box.hidden = !message;
  box.textContent = message || "";
}

/* 卡片上的「立即申购 / 赎回」：不再在本页直接提交，统一打开交易确认弹窗。
   弹窗负责四件套、份额/金额、验证码与二次确认；执行仍走 /api/confirm → PEP。 */
function onWealthAction(trigger) {
  const productId = trigger.dataset.wealthRedeem || trigger.dataset.wealthSubscribe || "";
  if (!productId) {
    timelineAdd("未取到产品编号，请重新加载理财面板后再试。", "stop");
    return;
  }
  /* 对话内发起：弹窗确认后生成计划卡；理财页发起：沿用页内确认后执行 */
  const fromChat = Boolean(trigger.closest("#active-response"));
  if (trigger.hasAttribute("data-wealth-redeem")) {
    const holding = ((state.wealth && state.wealth.holdings) || []).find(
      (item) => String(item.product_id) === String(productId),
    ) || null;
    return openWealthRedeemSheet({ productId, holding, fromChat });
  }
  return openWealthTradeSheet({ mode: "subscribe", productId, fromChat });
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
          <span class="muted insight-item__meta">数据截至 ${esc(item.business_day || "—")}</span>
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
  /* 账单页的预算由 /api/bills/analysis 下发，避免被洞察页的旧值覆盖。 */
  if (state.bills && state.bills.budget) return;
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
  const categories = {};
  document.querySelectorAll("[data-budget-category]").forEach((node) => {
    const raw = String(node.value || "").trim();
    if (!raw) return;
    const value = Number(raw);
    if (Number.isFinite(value) && value >= 0) {
      categories[node.dataset.budgetCategory] = Math.round(value * 100);
    }
  });
  const month = state.bills && state.bills.period
    ? state.bills.period
    : new Date().toISOString().slice(0, 7);
  if (status) status.textContent = "正在保存…";
  const result = await apiFetch("POST", "/api/insights/budget", {
    budget_cents: cents,
    month,
    categories,
  });
  if (!result.ok) {
    if (status) status.textContent = (result.data && result.data.detail) || "保存失败，请稍后重试。";
    return;
  }
  timelineAdd(`本月预算已保存：${yuanText(cents)}（含 ${Object.keys(categories).length} 个分类预算）`, "done");
  await refreshBills();
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

let lastTransfers = [];

/* 旧服务进程把实时到账的未到记账日条目标成「处理中」；实时/历史渠道一律按已完成显示。
   新后端已直接给出正确状态，这里只是未重启进程的展示兜底。 */
function transferDisplayStatus(item) {
  const method = String((item && item.method) || "");
  const status = String((item && item.status) || "");
  if (status === "处理中" && (method === "realtime" || method === "history")) return "已到账";
  if (status === "处理中" && method === "unknown") return "待到账";
  return status;
}

function transferStatusClass(status) {
  const text = String(status || "");
  if (text.includes("已到账")) return "chip chip--green";
  if (text.includes("撤销")) return "chip chip--yellow";
  if (text.includes("处理中") || text.includes("已发出") || text.includes("待到账")) return "chip chip--yellow";
  return "chip";
}

function transferPayeeText(item) {
  const tail = item.payee_account_tail ? ` · 尾号 ${item.payee_account_tail}` : "";
  const bank = item.payee_bank ? ` · ${item.payee_bank}` : "";
  return `${item.payee || "收款人未知"}${tail}${bank}`;
}

function repeatAttrs(item) {
  return [
    `data-transfer-repeat="1"`,
    `data-repeat-payee-id="${esc(item.payee_id || "")}"`,
    `data-repeat-payee="${esc(item.payee || "")}"`,
    `data-repeat-account-tail="${esc(item.payee_account_tail || "")}"`,
    `data-repeat-bank="${esc(item.payee_bank || "")}"`,
    `data-repeat-amount="${esc(String(item.amount_cents || 0))}"`,
    `data-repeat-method="${esc(item.method || "realtime")}"`,
    `data-repeat-purpose="${esc(item.purpose || "")}"`,
    `data-repeat-remark="${esc(item.remark || "")}"`,
    `data-repeat-booked="${esc(item.booked_on || "")}"`,
  ].join(" ");
}

function transferRecordDetail(item) {
  const source = item.source === "seed" ? "演示历史" : "AI 办理";
  const fee = yuanText(item.fee_cents || 0);
  const purpose = item.purpose_label || "—";
  const remark = item.remark || "—";
  const channel = recordChannelLabel(item.channel);
  return `<dl class="record-detail">
    <div><dt>转账方式</dt><dd>${esc(item.method_label || "—")}</dd></div>
    <div><dt>到账时间</dt><dd>${esc(item.eta_label || "—")}</dd></div>
    <div><dt>手续费</dt><dd class="mono">${esc(fee)}</dd></div>
    <div><dt>用途</dt><dd>${esc(purpose)}</dd></div>
    <div><dt>附言</dt><dd>${esc(remark)}</dd></div>
    <div><dt>付款账户</dt><dd>${esc(item.account || "—")}${item.account_tail ? ` · 尾号 ${esc(item.account_tail)}` : ""}</dd></div>
    ${item.kind === "scheduled" ? `<div><dt>资金状态</dt><dd>${item.status === "已撤销" ? "已撤销，未扣款" : "登记中，未扣款"}</dd></div>` : ""}
    <div><dt>渠道</dt><dd>${esc(channel)}</dd></div>
    <div><dt>来源</dt><dd>${esc(source)}</dd></div>
    <div><dt>记录号</dt><dd class="mono">${esc(item.record_id || "—")}</dd></div>
  </dl>
  <div class="btn-row">
    ${item.can_repeat ? `<button class="btn btn--sm btn--ghost" type="button" ${repeatAttrs(item)}>再转一笔</button>` : ""}
    ${item.can_repeat ? `<button class="btn btn--sm" type="button" ${repeatAttrs(item)} data-transfer-repeat-chat="1">用 AI 再转一笔</button>` : ""}
    ${item.can_cancel ? `<button class="btn btn--sm btn--danger" type="button" data-transfer-cancel="${esc(item.record_id)}">撤销这笔申请</button>` : ""}
  </div>`;
}

function renderTransfers(payload) {
  const status = el("transfers-status");
  const list = el("transfers-list");
  if (!status || !list) return;
  const items = (payload && payload.items) || [];
  lastTransfers = items;
  status.textContent = `最近 ${payload.window_days} 天 · 数据截至 ${payload.as_of}${items.length > 5 ? ` · 共 ${items.length} 笔` : ""}`;
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
    const tag = item.source === "seed"
      ? '<span class="chip chip--muted">演示历史</span>'
      : '<span class="chip">AI 办理</span>';
    const scheduled = item.kind === "scheduled" ? '<span class="chip chip--yellow">定时</span>' : "";
    return `<li class="record">
      <details>
        <summary>
          <b>${esc(transferPayeeText(item))}</b>
          <span class="${transferStatusClass(transferDisplayStatus(item))}">${esc(transferDisplayStatus(item))}</span>
          ${scheduled}${tag}
          <span class="mono">${esc(yuanText(item.amount_cents))}</span>
          <span class="muted">${esc(item.booked_on)}</span>
        </summary>
        ${transferRecordDetail(item)}
      </details>
    </li>`;
  }).join("") + more;
}

async function refreshTransfers() {
  const result = await apiFetch("GET", "/api/transfers");
  if (!result.ok) throw new Error("转账记录读取失败");
  renderTransfers(result.data);
  updatePayeeRecent((result.data && result.data.items) || []);
  updateTransferFormState();
}

/* ---------------- 场景页「业务面板」公共件 ----------------
   6 个场景页都是「页内直接办事」：把中文指令交给同一套 /api/submit → /api/confirm，
   页内只渲染服务端给的计划卡（summary / lines / level），不新增后端接口、不在前端算钱。 */

const SCENE_PALETTE = ["#0b4fd8", "#00a6a6", "#f0a71b", "#7a5bd8", "#3f8f4f", "#8b96a8"];

let scenePending = null;
let sceneVerification = null;
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
  /* 图例默认只列有值的行；wealth 需要把「未持有」大类也列出来（showZeroRows）。 */
  const digits = Number.isInteger(spec.percentDigits) ? spec.percentDigits : 0;
  const legendRows = spec.showZeroRows ? (rows || []) : list;
  const legend = legendRows.map((row, index) => {
    const value = Number(row.value) || 0;
    const pct = ((value / total) * 100).toFixed(digits);
    const valText = value > 0
      ? `${pct}%${row.note ? ` · ${esc(row.note)}` : ""}`
      : esc(row.note || `${pct}%`);
    return `<li><i class="chart-dot" style="background:${row.color || sceneColor(index)}"></i>` +
      `<span class="chart-legend__name">${esc(row.label)}</span>` +
      `<span class="chart-legend__val">${valText}</span></li>`;
  }).join("");
  return `<div class="chart-donut">
    <svg viewBox="0 0 ${size} ${size}" width="${size}" height="${size}" role="img" aria-label="${esc(spec.label || "占比图")}">
      <circle cx="${center}" cy="${center}" r="${radius}" fill="none" stroke="#e8edf6" stroke-width="${stroke}" />
      ${arcs}
    </svg>
    <ul class="chart-legend">${legend}</ul>
  </div>`;
}

/* 折线图：手写 SVG；rows = [{label, value, secondary?, period?}]
   端点标签按首/末锚点对齐，避免长金额被 viewBox 裁切。 */
function lineChartHtml(rows, options) {
  const spec = options || {};
  const list = rows || [];
  if (!list.length) return `<p class="muted">${esc(spec.empty || "暂无趋势数据")}</p>`;
  const hasSecondary = list.some((row) => Number.isFinite(Number(row.secondary)) && Number(row.secondary) > 0);
  const width = 440;
  const height = hasSecondary ? 186 : 166;
  const padL = 20;
  const padR = 20;
  const padT = 26;
  const padB = 30;
  const values = list.map((row) => Number(row.value) || 0);
  const secondaryValues = list.map((row) => Number(row.secondary) || 0);
  const max = Math.max(...values, ...secondaryValues, 1);
  const min = Math.min(...values, ...secondaryValues, 0);
  const span = max - min || 1;
  const innerW = width - padL - padR;
  const innerH = height - padT - padB;
  const stepX = list.length > 1 ? innerW / (list.length - 1) : 0;
  const yOf = (value) => padT + innerH * (1 - ((Number(value) || 0) - min) / span);
  const points = list.map((row, index) => ({
    x: padL + stepX * index,
    y: yOf(row.value),
    secondaryY: yOf(row.secondary),
    label: row.label || "",
    period: row.period || "",
    index,
  }));
  const polyline = points.map((point) => `${point.x.toFixed(1)},${point.y.toFixed(1)}`).join(" ");
  const secondaryLine = hasSecondary
    ? points.map((point) => `${point.x.toFixed(1)},${point.secondaryY.toFixed(1)}`).join(" ")
    : "";
  const dots = points.map((point) =>
    `<circle cx="${point.x.toFixed(1)}" cy="${point.y.toFixed(1)}" r="3" fill="#0b4fd8" />`).join("");
  const secondaryDots = hasSecondary
    ? points.map((point) =>
      `<circle cx="${point.x.toFixed(1)}" cy="${point.secondaryY.toFixed(1)}" r="2.5" fill="#00a6a6" />`).join("")
    : "";
  const labels = points.map((point) =>
    `<text x="${point.x.toFixed(1)}" y="${height - 8}" text-anchor="middle" class="chart-axis">${esc(point.label)}</text>`).join("");
  /* 首点左对齐、末点右对齐：长金额（如 ¥18,719.80）不会越过 viewBox 右边界。 */
  const valueLabels = spec.formatValue && list.length <= 8
    ? points.map((point, index) => {
      const anchor = index === 0 ? "start" : index === list.length - 1 ? "end" : "middle";
      const x = index === 0 ? point.x + 2 : index === list.length - 1 ? point.x - 2 : point.x;
      return `<text x="${x.toFixed(1)}" y="${Math.max(point.y - 8, 12).toFixed(1)}" text-anchor="${anchor}" class="chart-value">${esc(spec.formatValue(values[index]))}</text>`;
    }).join("")
    : "";
  const hitPoints = points.map((point) => spec.clickable
    ? `<g class="chart-point" data-chart-period="${esc(point.period)}" tabindex="0" role="button"
         aria-label="${esc(point.label)}${spec.formatValue ? " " + esc(spec.formatValue(values[point.index])) : ""}">
         <circle cx="${point.x.toFixed(1)}" cy="${point.y.toFixed(1)}" r="12" fill="transparent" />
         <circle cx="${point.x.toFixed(1)}" cy="${point.y.toFixed(1)}" r="3" fill="#0b4fd8" />
       </g>`
    : dots).join("");
  const legend = hasSecondary
    ? `<p class="chart-line__legend muted"><span><i class="chart-dot" style="background:#0b4fd8"></i>${esc(spec.primaryLabel || "消费")}</span><span><i class="chart-dot" style="background:#00a6a6"></i>${esc(spec.secondaryLabel || "收入")}</span></p>`
    : "";
  return `<div class="chart-line">
    <svg viewBox="0 0 ${width} ${height}" role="img" aria-label="${esc(spec.label || "趋势图")}">
      <line x1="${padL}" y1="${padT + innerH}" x2="${width - padR}" y2="${padT + innerH}" stroke="#e8edf6" stroke-width="1" />
      ${hasSecondary ? `<polyline points="${secondaryLine}" fill="none" stroke="#00a6a6" stroke-width="1.6" stroke-dasharray="4 3" stroke-linejoin="round" stroke-linecap="round" />${secondaryDots}` : ""}
      <polyline points="${polyline}" fill="none" stroke="#0b4fd8" stroke-width="2" stroke-linejoin="round" stroke-linecap="round" />
      ${hitPoints}${valueLabels}${labels}
    </svg>
    ${legend}
    ${spec.note ? `<p class="muted chart-line__note">${esc(spec.note)}</p>` : ""}
  </div>`;
}

const WEALTH_CATEGORY_ORDER = ["货币类", "债券类", "混合类", "权益类"];

/* 模拟净值迷你折线：内联 SVG（不引图表库）；右上角常驻「模拟净值」标注。
   数据只来自接口的 `nav_series`，前端不生成任何净值。 */
function navSparkHtml(series, options) {
  const spec = options || {};
  const points = (series || [])
    .map((point) => Number(point && point.nav))
    .filter((value) => Number.isFinite(value));
  if (points.length < 2) {
    return `<span class="nav-spark nav-spark--empty">模拟净值 · ${esc(spec.empty || "暂无序列")}</span>`;
  }
  const width = 120;
  const height = 36;
  const pad = 2;
  const min = Math.min(...points);
  const max = Math.max(...points);
  const span = max - min || 1;
  const step = (width - pad * 2) / (points.length - 1);
  const coords = points.map((value, index) => {
    const x = pad + step * index;
    const y = height - pad - ((value - min) / span) * (height - pad * 2);
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(" ");
  return `<div class="nav-spark">
    <svg viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" role="img"
      aria-label="${esc(spec.label || "模拟净值走势（演示数据，非真实行情）")}">
      <polyline points="${coords}" fill="none" stroke="var(--blue-600)" stroke-width="1.6"
        stroke-linejoin="round" stroke-linecap="round" vector-effect="non-scaling-stroke" />
    </svg>
    <span class="nav-spark__tag">模拟净值</span>
  </div>`;
}

function sceneLevelChip(level) {
  const info = LEVEL[level] || LEVEL.YELLOW;
  return `<span class="chip chip--${info.cls}">${esc(info.name)} · ${esc(info.text)}</span>`;
}

/* 页内迷你计划卡：只渲染服务端给的 summary，附冻结哈希；红色不给确认按钮 */
function isWealthCard(card) {
  return Boolean(card && (card.nodes || []).some(
    (node) => String(node.action || "").startsWith("wealth.")
  ));
}

/* 申购弹窗已经收过交易信息：计划卡直接走「获取验证码 / 确认并执行」，
   不再二次打开弹窗（否则弹窗确认后又要回弹窗执行）。 */
function wealthSheetHandled(card) {
  return Boolean(card && card.sheet_done === true);
}

/* 卡片域写操作：对话里仍可生成计划，但确认与强验证统一收口到卡片弹窗（AI 不代执行）。 */
function isCardWriteCard(card) {
  return Boolean(card && (card.nodes || []).some(
    (node) => String(node.action || "").startsWith("card.")
      && String(node.action || "") !== "card.list"
  ));
}

function sceneCardHtml(card) {
  const rows = (card.summary || []).map((row) =>
    `<div class="scene-plan__row${row.emphasis ? " scene-plan__row--strong" : ""}">` +
    `<dt>${esc(row.label)}</dt><dd>${esc(row.value)}</dd></div>`).join("");
  const lines = (card.lines || []).map((line) =>
    `<li>${esc(stripNo(line))}</li>`).join("");
  const wealth = isWealthCard(card);
  const transfer = (card.nodes || []).some(
    (node) => String(node.action || "").startsWith("transfer.")
  );
  const subscriptionCancel = (card.nodes || []).some(
    (node) => String(node.action || "") === "subscription.cancel"
  );
  const inPageVerify = transfer || subscriptionCancel;
  const need = wealth
    ? "交易确认弹窗会收集四件套、份额/金额与验证码；确认后才由服务端执行。"
    : transfer
      ? (card.level === "RED"
        ? "大额转账需要一次性验证码；演示验证码不是真实短信、人脸或交易密码。"
        : "请核对收款人、账号、金额与到账时间；确认后进入验证并执行。")
      : subscriptionCancel
        ? "关闭扣款协议需要一次性验证码；演示验证码不是真实短信、人脸或交易密码。"
        : ({
          GREEN: "纯查询，服务端已自动执行。",
          YELLOW: "确认后由服务端执行；这一步不会替您跳过确认。",
          RED: "这笔操作需要一次性验证码，请在本页完成验证。",
        }[card.level] || "按服务端裁决处理。");
  const verifyPanel = sceneVerification
    ? `<div class="scene-verify">
        <p class="muted">一次性验证码 ${sceneVerification.mode === "demo"
          ? `（演示码：<b class="mono">${esc(sceneVerification.code || "—")}</b>）`
          : `已发到 ${esc(sceneVerification.target || "第二信道")}`}</p>
        <input class="input" id="scene-vcode" inputmode="numeric" maxlength="6" placeholder="输入 6 位验证码" />
        <button class="btn btn--sm btn--primary" type="button" data-scene-confirm-code data-requires-login>${transfer ? "提交验证码并转账" : "提交验证码并关闭协议"}</button>
      </div>`
    : "";
  const actions = wealth
    ? '<button class="btn btn--sm btn--primary" type="button" data-scene-wealth-trade data-requires-login>打开交易确认</button>' +
      '<button class="btn btn--sm btn--ghost" type="button" data-scene-dismiss>取消</button>'
    : card.level === "RED"
      ? (inPageVerify
        ? verifyPanel + (sceneVerification
          ? '<button class="btn btn--sm btn--ghost" type="button" data-scene-dismiss>取消</button>'
          : '<button class="btn btn--sm btn--primary" type="button" data-scene-verify data-requires-login>获取验证码</button>' +
            '<button class="btn btn--sm btn--ghost" type="button" data-scene-dismiss>取消</button>')
        : '<button class="btn btn--sm" type="button" data-scene-chat>去对话完成验证</button>')
      : card.level === "YELLOW"
        ? `<button class="btn btn--sm btn--primary" type="button" data-scene-confirm data-requires-login>${transfer ? "确认转账" : "确认执行"}</button>` +
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
  if (!scenePending || !scenePending.card || scenePending.card.card_id !== card.card_id) {
    sceneVerification = null;
  }
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
  sceneVerification = null;
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

async function requestSceneVerification() {
  if (!scenePending) return;
  const status = scenePending.options.status ? el(scenePending.options.status) : null;
  if (status) status.textContent = "正在获取一次性验证码…";
  const result = await post("/api/verify/request", {});
  if (result.status !== 200) {
    if (status) status.textContent = String((result.data && result.data.detail) || "验证码申请失败。");
    return;
  }
  sceneVerification = result.data || {};
  renderSceneCard(scenePending.card, scenePending.options);
  if (status) status.textContent = "验证码已下发，请输入后确认。";
}

async function confirmSceneCard(codeOverride) {
  if (!scenePending || scenePending.busy) return null;
  const { card, options } = scenePending;
  scenePending.busy = true;
  const status = options.status ? el(options.status) : null;
  const setStatus = (text) => {
    if (status) status.textContent = text;
  };
  setStatus("正在请求执行，请等待服务端回执…");
  let code = codeOverride || "";
  if (!code && sceneVerification) {
    const codeInput = el("scene-vcode");
    code = codeInput ? codeInput.value.trim() : "";
    if (!code) {
      scenePending.busy = false;
      setStatus("请输入一次性验证码。");
      return null;
    }
  }
  let result;
  try {
    const payload = { card_id: card.card_id, frozen_hash: card.frozen_hash };
    if (code) payload.verification_code = code;
    result = await post("/api/confirm", payload);
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
  sceneVerification = null;
  setStatus("");
  if (typeof options.onSuccess === "function") options.onSuccess(data);
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
    result = await post("/api/submit", { utterance: options.utterance, selected: options.selected || {}, new_intent: true });
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
  const notes = el("quota-notes");
  if (!status || !body) return;
  if (!quota) {
    status.textContent = "额度暂时不可用，请稍后刷新。";
    body.innerHTML = "";
    return;
  }
  const limit = Number(quota.daily_limit_cents);
  const used = Number(quota.daily_accumulated_cents);
  const single = Number(quota.single_limit_cents);
  const monthLimit = Number(quota.monthly_limit_cents);
  const monthUsed = Number(quota.monthly_accumulated_cents);
  if (![limit, used, single, monthLimit, monthUsed].every(Number.isFinite)) {
    status.textContent = "额度暂时不可用，请稍后刷新。";
    body.innerHTML = "";
    return;
  }
  const over = used > limit;
  const remaining = Math.max(0, limit - used);
  const pct = limit > 0 ? Math.min(Math.round((used / limit) * 100), 100) : 0;
  status.textContent = `数据截至 ${quota.as_of || "—"}`;
  body.innerHTML = `<div class="stat-metrics stat-metrics--quota">
      <div class="stat-metric"><span class="stat-metric__k">单笔限额</span>
        <b class="stat-metric__v">${yuanText(single)}</b></div>
      <div class="stat-metric"><span class="stat-metric__k">今日已用 / 日限额</span>
        <b class="stat-metric__v">${yuanText(used)} / ${yuanText(limit)}</b></div>
      <div class="stat-metric${over ? " stat-metric--over" : ""}">
        <span class="stat-metric__k">${over ? "今日已超额" : "今日剩余可用"}</span>
        <b class="stat-metric__v">${yuanText(remaining)}</b></div>
      <div class="stat-metric"><span class="stat-metric__k">本月累计（演示）</span>
        <b class="stat-metric__v">${yuanText(monthUsed)} / ${yuanText(monthLimit)}</b></div>
    </div>
    <span class="bill__bar-track quota__bar${over ? " quota__bar--over" : ""}" role="img" aria-label="今日转账额度已用 ${pct}%"><i style="width:${pct}%"></i></span>
    <p class="muted">今日已用 ${pct}%。单笔、日累计和账户余额会一起校验。</p>`;
  if (notes) {
    const rows = quota.limit_notes || [
      "单笔限额来自卡片转账通道；日累计限额由演示风控规则固定。",
      "月累计为演示口径，不构成监管或银行承诺。",
    ];
    notes.innerHTML = rows.map((text) => `<li>${esc(text)}</li>`).join("");
  }
}

/* ---- 收款人候选（只读）：名册 /api/payees + 最近转账去重合并 ----
   候选只来自服务端事实；命中不到时给「按此姓名继续」，**不猜收款人**——
   最终仍由 /api/submit → 服务端澄清与拦截。 */
let payeeRoster = null;
let payeeRosterPromise = null;
let payeeRecentNames = [];
let payeeMatches = [];
let payeeActiveIndex = -1;
let pickedPayee = null;
let lastTransferAccountId = "";

/* 转账记录里的姓名是脱敏的（`李*` / `王*伟`）：只用来给名册命中排序，绝不当作可选姓名；
   未脱敏且不在名册里的姓名才作为「最近转账」候选补进来。 */
function payeeMaskMatches(name, masked) {
  const text = String(name || "");
  const mask = String(masked || "");
  if (!mask || mask === "收款人已隐藏" || mask === "收款人未知" || mask.length !== text.length) return false;
  return mask.split("").every((ch, index) => ch === "*" || ch === text[index]);
}

function payeeOptionList() {
  const roster = payeeRoster || [];
  const rosterNames = new Set(roster.map((item) => item.name));
  const isRecent = (name) => payeeRecentNames.some((masked) => payeeMaskMatches(name, masked));
  const options = roster.map((item) => Object.assign({ inRoster: true, recent: isRecent(item.name) }, item));
  payeeRecentNames.forEach((name) => {
    if (name.includes("*") || rosterNames.has(name)) return;
    if (roster.some((item) => payeeMaskMatches(item.name, name))) return;
    options.push({
      payeeId: "", name, accountTail: "", phoneTail4: "", bankName: "",
      identityConfirmed: false, maskedAccount: "", blocked: false, inRoster: false, recent: true,
    });
  });
  /* 最近转过的排前面；其余保持名册顺序（sort 稳定） */
  return options.sort((a, b) => Number(b.recent) - Number(a.recent));
}

function filterPayees(query) {
  const needle = String(query || "").trim().toLowerCase();
  const all = payeeOptionList();
  if (!needle) return all;
  return all.filter((item) =>
    String(item.name || "").toLowerCase().includes(needle)
    || String(item.accountTail || "").includes(needle)
    || String(item.phoneTail4 || "").includes(needle));
}

function payeeCandidateHtml(item, index) {
  const meta = [];
  if (item.accountTail) meta.push(`账号尾号 ${item.accountTail}`);
  if (item.bankName) meta.push(item.bankName);
  if (item.phoneTail4) meta.push(`手机 ${item.phoneTail4}`);
  const tag = item.blocked
    ? '<span class="payee-candidates__tag">已停用</span>'
    : item.recent ? '<span class="payee-candidates__tag">最近转账</span>' : "";
  return `<li class="payee-candidates__item${index === payeeActiveIndex ? " is-active" : ""}` +
    `${item.blocked ? " is-blocked" : ""}" role="option" id="payee-option-${index}"` +
    ` aria-selected="${index === payeeActiveIndex}" aria-disabled="${item.blocked ? "true" : "false"}"` +
    ` data-payee-name="${esc(item.name)}" data-payee-id="${esc(item.payeeId || "")}"` +
    ` data-payee-blocked="${item.blocked ? "1" : "0"}">` +
    `<span class="payee-candidates__name">${esc(item.name)}</span>` +
    (meta.length ? `<span class="payee-candidates__meta">${esc(meta.join(" · "))}</span>` : "") +
    tag + "</li>";
}

function findPayeeByName(name) {
  const text = String(name || "").trim();
  return (payeeRoster || []).find((item) => item.name === text) || null;
}

function findPayeeById(payeeId) {
  const id = String(payeeId || "");
  if (!id) return null;
  return (payeeRoster || []).find((item) => String(item.payeeId) === id) || null;
}

function applyPickedPayee(item) {
  pickedPayee = item || null;
  const accountInput = el("quick-payee-account");
  const bankSelect = el("quick-payee-bank");
  const hint = el("payee-picked-hint");
  if (!item) {
    if (hint) hint.textContent = "新收款人：请填写收款账号与开户行。";
    return;
  }
  if (accountInput) accountInput.value = item.maskedAccount || item.accountTail || "";
  if (bankSelect && item.bankName) {
    const matched = Array.from(bankSelect.options).some((option) => option.value === item.bankName
      || option.textContent === item.bankName);
    if (matched) bankSelect.value = item.bankName;
  }
  if (hint) {
    const bank = item.bankName ? ` · ${item.bankName}` : "";
    const verified = item.identityConfirmed ? "已核对" : "未完成账户核验";
    const last = lastTransfers.find((row) =>
      String(row.payee_id || "") === String(item.payeeId || ""));
    const lastText = last
      ? ` · 上次 ${last.booked_on} 转 ${yuanText(last.amount_cents)}`
      : "";
    hint.textContent = `已选择常用收款人 · 尾号 ${item.accountTail || "—"}${bank} · ${verified}${lastText}`;
  }
}

function clearPickedPayee() {
  pickedPayee = null;
  const hint = el("payee-picked-hint");
  if (hint) hint.textContent = "新收款人：请填写收款账号与开户行。";
}

function closePayeeCandidates() {
  const input = el("quick-payee");
  const list = el("payee-candidates");
  if (list) list.hidden = true;
  if (input) {
    input.setAttribute("aria-expanded", "false");
    input.removeAttribute("aria-activedescendant");
  }
  payeeActiveIndex = -1;
}

function renderPayeeCandidates() {
  const input = el("quick-payee");
  const list = el("payee-candidates");
  if (!input || !list) return;
  const query = input.value.trim();
  payeeMatches = filterPayees(query);
  if (payeeActiveIndex >= payeeMatches.length) payeeActiveIndex = -1;
  const rows = payeeMatches.map(payeeCandidateHtml);
  const selectable = payeeMatches.filter((item) => !item.blocked);
  if (!selectable.length && query) {
    rows.push('<li class="payee-candidates__item payee-candidates__item--fallback" role="option"' +
      ' aria-selected="false" data-payee-fallback="1">' +
      `按「${esc(query)}」继续</li>`);
  }
  list.innerHTML = rows.join("");
  list.hidden = rows.length === 0;
  input.setAttribute("aria-expanded", String(rows.length > 0));
}

function choosePayee(item) {
  const picked = typeof item === "object" && item !== null ? item : findPayeeByName(item);
  const input = el("quick-payee");
  if (input) input.value = picked ? String(picked.name || "") : String(item || "");
  applyPickedPayee(picked);
  closePayeeCandidates();
  updateTransferFormState();
}

function movePayeeActive(step) {
  if (!payeeMatches.length) return;
  let index = payeeActiveIndex;
  for (let i = 0; i < payeeMatches.length; i += 1) {
    index = (index + step + payeeMatches.length) % payeeMatches.length;
    if (!payeeMatches[index].blocked) {
      payeeActiveIndex = index;
      break;
    }
  }
  renderPayeeCandidates();
  const input = el("quick-payee");
  if (input && payeeActiveIndex >= 0) {
    input.setAttribute("aria-activedescendant", `payee-option-${payeeActiveIndex}`);
  }
}

async function loadPayeeRoster() {
  if (payeeRoster) return payeeRoster;
  if (!payeeRosterPromise) {
    payeeRosterPromise = apiFetch("GET", "/api/payees").then((result) => {
      if (!result.ok) throw new Error("收款人名册读取失败");
      payeeRoster = ((result.data && result.data.payees) || []).map((item) => ({
        payeeId: String(item.payee_id || ""),
        name: String(item.name || ""),
        accountTail: String(item.account_tail || ""),
        maskedAccount: String(item.masked_account || ""),
        bankName: String(item.bank_name || ""),
        phoneTail4: String(item.phone_tail4 || ""),
        identityConfirmed: item.identity_confirmed === true,
        blocked: item.blocked === true,
      }));
      /* 名册晚于输入到达时补渲染：正在输入的用户不该只看到「最近转账」那一份 */
      if (document.activeElement === el("quick-payee")) renderPayeeCandidates();
      return payeeRoster;
    }).finally(() => {
      payeeRosterPromise = null;
    });
  }
  return payeeRosterPromise;
}

/* 最近转账姓名只更新候选，不覆盖输入框；名册未到时先记下，聚焦时一并渲染 */
function updatePayeeRecent(items) {
  payeeRecentNames = Array.from(new Set((items || [])
    .map((item) => String(item.payee || "").trim())
    .filter(Boolean)));
  const list = el("payee-candidates");
  if (list && !list.hidden) renderPayeeCandidates();
}

async function openPayeeCandidates() {
  try {
    await loadPayeeRoster();
  } catch (_) {
    /* 名册读取失败仍保留输入框：不猜收款人，直接按姓名提交由服务端澄清 */
  }
  renderPayeeCandidates();
}

/* 场景①转账：付款账户 + 收款人三要素 + 到账方式 + 金额/用途，统一交给 /api/submit。 */
const TRANSFER_METHOD_LABELS = {
  realtime: "实时到账",
  normal: "普通到账",
  next_day: "次日到账",
  large: "大额通道",
};

const TRANSFER_METHOD_NOTES = {
  realtime: "实时到账：7×24 小时，预计秒到；演示环境即时记账。",
  normal: "普通到账：预计 2 小时内到账；演示环境即时记账。",
  next_day: "次日到账：次日 00:00 后发出，登记时不扣款，可在记录中撤销。",
  large: "大额通道：单笔 5 万以上，仅工作日处理；演示环境按大额强验证处理。",
};

const SUSPICIOUS_WORDS = /安全账户|刷单|返利|验证码|屏幕共享|洗钱|投资导师|保证金/;

let transferFraudGate = null;
let transferFraudTimer = null;
let pendingSceneSelected = {};

function transferAccountList() {
  return (state.options && state.options.accounts) || [];
}

function selectedTransferAccount() {
  const select = el("transfer-account");
  const id = select ? select.value : "";
  return transferAccountList().find((item) => String(item.id) === String(id)) || null;
}

function selectedTransferMethod() {
  const checked = document.querySelector('input[name="transfer-method"]:checked');
  return checked ? checked.value : "realtime";
}

function transferSnapshot() {
  const account = selectedTransferAccount();
  const payeeInput = el("quick-payee");
  const accountInput = el("quick-payee-account");
  const bankSelect = el("quick-payee-bank");
  const phoneInput = el("quick-payee-phone");
  const amountInput = el("quick-amount");
  const purposeSelect = el("quick-purpose");
  const remarkInput = el("quick-remark");
  const payee = payeeInput ? payeeInput.value.trim() : "";
  const accountDigits = accountInput ? accountInput.value.replace(/\D/g, "") : "";
  const amountYuan = amountInput ? Number(amountInput.value) : NaN;
  const amountCents = Number.isFinite(amountYuan) ? Math.round(amountYuan * 100) : 0;
  return {
    account,
    accountId: account ? String(account.id) : "",
    balanceCents: account ? Number(account.balance_cents) || 0 : 0,
    payee,
    payeeId: pickedPayee ? pickedPayee.payeeId : "",
    picked: pickedPayee,
    accountDigits,
    accountTail: accountDigits ? accountDigits.slice(-4) : "",
    bank: bankSelect ? bankSelect.value : "",
    phone: phoneInput ? phoneInput.value.replace(/\D/g, "") : "",
    amountYuan: Number.isFinite(amountYuan) ? amountYuan : 0,
    amountCents,
    method: selectedTransferMethod(),
    purpose: purposeSelect ? purposeSelect.value : "",
    remark: remarkInput ? remarkInput.value.trim().slice(0, 50) : "",
  };
}

function transferHasHistory(snapshot) {
  if (!snapshot.payeeId) return false;
  return lastTransfers.some((item) => String(item.payee_id || "") === snapshot.payeeId);
}

function blockedPayeeByName(name) {
  const text = String(name || "").trim();
  if (!text) return null;
  return (payeeRoster || []).find((item) => item.blocked && item.name === text) || null;
}

function transferWarnings(snapshot) {
  const warnings = [];
  if (snapshot.picked && snapshot.picked.blocked) return warnings;
  const newPayee = !snapshot.picked || !snapshot.picked.identityConfirmed
    || !transferHasHistory(snapshot);
  if (newPayee) warnings.push("首次向该收款人转账，请先通过电话或当面核实姓名与账号，谨防冒充熟人。");
  if (snapshot.amountCents >= 100000) {
    warnings.push("本笔金额较大，请确认收款人身份，并预留冷静时间。");
  }
  if (SUSPICIOUS_WORDS.test(snapshot.remark)) {
    warnings.push("附言中出现“安全账户 / 刷单 / 返利 / 验证码”等高风险话术，可能是诈骗，请立即停止并核实。");
  }
  return warnings;
}

function resetTransferFraudGate(key) {
  transferFraudGate = { key, readyAt: Date.now() + 3000, acknowledged: false };
  if (transferFraudTimer) window.clearTimeout(transferFraudTimer);
  transferFraudTimer = window.setTimeout(() => updateTransferFormState(), 3000);
}

function renderTransferWarnings(snapshot, warnings) {
  const box = el("transfer-fraud");
  if (!box) return { needsAck: false, ready: true };
  if (!warnings.length) {
    box.hidden = true;
    box.innerHTML = "";
    transferFraudGate = null;
    return { needsAck: false, ready: true };
  }
  const key = `${snapshot.payee}|${snapshot.accountTail}|${snapshot.bank}|${snapshot.method}`;
  if (!transferFraudGate || transferFraudGate.key !== key) resetTransferFraudGate(key);
  const wait = Math.max(0, Math.ceil((transferFraudGate.readyAt - Date.now()) / 1000));
  const ready = wait <= 0;
  box.hidden = false;
  box.innerHTML = `<p class="fraud-alert__title">转账前请确认</p>
    <ul>${warnings.map((text) => `<li>${esc(text)}</li>`).join("")}</ul>
    <label class="fraud-alert__ack">
      <input type="checkbox" id="transfer-fraud-ack" ${transferFraudGate.acknowledged ? "checked" : ""} ${ready ? "" : "disabled"} />
      <span>${ready ? "我已知晓风险，收款人信息已核实" : `请阅读防诈提示（${wait} 秒后可勾选）`}</span>
    </label>`;
  return { needsAck: true, ready };
}

function validateTransferSnapshot(snapshot) {
  if (!snapshot.accountId) return "请选择付款账户。";
  if (!snapshot.payee) return "请填写收款人姓名。";
  if (snapshot.picked && snapshot.picked.blocked) return "该收款人已停用，无法转账。";
  if (!snapshot.picked && blockedPayeeByName(snapshot.payee)) return "该收款人已停用，无法转账。";
  if (!snapshot.picked && snapshot.accountDigits.length < 4) return "请填写收款账号（至少后四位）。";
  if (!snapshot.picked && !snapshot.bank) return "请选择收款人开户行。";
  if (!snapshot.amountCents || snapshot.amountCents <= 0) return "请填写大于 0 的转账金额。";
  const quota = (state.options && state.options.quota) || {};
  const single = Number(quota.single_limit_cents);
  if (snapshot.method !== "large" && Number.isFinite(single) && snapshot.amountCents > single) {
    return `超过单笔限额 ${yuanText(single)}；大额请选择「大额通道」。`;
  }
  if (snapshot.amountCents > snapshot.balanceCents) {
    return `付款账户可用余额不足（可用 ${yuanText(snapshot.balanceCents)}）。`;
  }
  return "";
}

/* 日累计 / 月累计超限不硬拦：服务端会把该笔升为红色强验证。
   这里如实提示原因，避免用户以为“页面没反应”。 */
function transferLimitNotices(snapshot) {
  const notices = [];
  const quota = (state.options && state.options.quota) || {};
  const dailyLimit = Number(quota.daily_limit_cents);
  const dailyUsed = Number(quota.daily_accumulated_cents);
  const monthLimit = Number(quota.monthly_limit_cents);
  const monthUsed = Number(quota.monthly_accumulated_cents);
  if (Number.isFinite(dailyLimit) && Number.isFinite(dailyUsed)) {
    const remaining = Math.max(0, dailyLimit - dailyUsed);
    if (snapshot.amountCents > remaining) {
      notices.push(`超过今日剩余额度 ${yuanText(remaining)}，本笔将进入强验证。`);
    }
  }
  if (Number.isFinite(monthLimit) && Number.isFinite(monthUsed)) {
    const remaining = Math.max(0, monthLimit - monthUsed);
    if (snapshot.amountCents > remaining) {
      notices.push(`超过本月剩余额度 ${yuanText(remaining)}（演示口径）。`);
    }
  }
  if (snapshot.method === "large" && snapshot.amountCents > 0 && snapshot.amountCents < 5000000) {
    notices.push("大额通道通常用于 5 万元以上；本笔仍按所选方式提交。");
  }
  return notices;
}

function updateTransferFormState() {
  const submit = el("quick-transfer-submit");
  const accountHint = el("transfer-account-balance");
  const methodNote = el("transfer-method-note");
  const amountHint = el("quick-amount-hint");
  const feeHint = el("transfer-fee");
  if (!submit || !el("quick-transfer-panel")) return;
  const snapshot = transferSnapshot();
  if (accountHint) {
    accountHint.textContent = snapshot.account
      ? `可用余额 ${yuanText(snapshot.balanceCents)}`
      : "请选择付款账户";
  }
  if (methodNote) methodNote.textContent = TRANSFER_METHOD_NOTES[snapshot.method] || "";
  if (feeHint) {
    const cross = !snapshot.bank
      ? "到账行待选择"
      : (snapshot.bank !== "招商银行" ? "跨行" : "本行");
    feeHint.textContent = `手续费 ¥0.00（演示免收）· ${cross} · 真实费用以银行实际为准`;
  }
  const warnings = transferWarnings(snapshot);
  const gate = renderTransferWarnings(snapshot, warnings);
  const error = validateTransferSnapshot(snapshot);
  const notices = transferLimitNotices(snapshot);
  const gateOk = !gate.needsAck || (gate.ready && transferFraudGate
    && transferFraudGate.acknowledged);
  if (amountHint) {
    amountHint.textContent = error || (notices.length ? notices[0] : (snapshot.amountCents > 0
      ? `将转出 ${yuanText(snapshot.amountCents)} · ${TRANSFER_METHOD_LABELS[snapshot.method]}`
      : ""));
    amountHint.classList.toggle("field__hint--error", Boolean(error));
    amountHint.classList.toggle("field__hint--warn", !error && notices.length > 0);
  }
  submit.disabled = Boolean(error) || !gateOk;
  submit.title = error || (gateOk ? "" : "请先阅读并确认防诈提示");
  return { snapshot, warnings, gate, error };
}

function renderTransferAccounts(options) {
  const select = el("transfer-account");
  if (!select) return;
  const accounts = (options && options.accounts) || [];
  let stored = "";
  try {
    stored = window.localStorage.getItem("aib.transferAccount") || "";
  } catch (_) {
    stored = "";
  }
  const remembered = lastTransferAccountId || stored;
  const current = select.value || remembered;
  select.innerHTML = '<option value="">请选择付款账户</option>' + accounts.map((item) => {
    const label = String(item.label || "").split(" · 余额")[0];
    return `<option value="${esc(item.id)}">${esc(label)}</option>`;
  }).join("");
  if (current && accounts.some((item) => String(item.id) === String(current))) {
    select.value = current;
  } else if (accounts.length === 1) {
    select.value = String(accounts[0].id);
  }
  lastTransferAccountId = select.value;
  updateTransferFormState();
}

function rememberTransferAccount() {
  const select = el("transfer-account");
  if (!select || !select.value) return;
  lastTransferAccountId = select.value;
  try {
    window.localStorage.setItem("aib.transferAccount", select.value);
  } catch (_) {
    /* 本地记忆失败不影响转账 */
  }
}

function buildTransferUtterance(snapshot) {
  const verb = {
    realtime: "转给",
    normal: "普通到账转给",
    next_day: "24小时延时转账给",
    large: "大额转账给",
  }[snapshot.method] || "转给";
  const remark = snapshot.remark ? `，备注：${snapshot.remark}` : "";
  return `${verb}${snapshot.payee} ${snapshot.amountYuan} 元${remark}`;
}

async function ensurePayeeId(snapshot) {
  if (snapshot.payeeId) return snapshot.payeeId;
  try {
    const result = await post("/api/payees", {
      full_name: snapshot.payee,
      account_tail: snapshot.accountTail,
      bank_name: snapshot.bank,
      phone_tail4: snapshot.phone ? snapshot.phone.slice(-4) : "",
    });
    if (result.status !== 200) {
      throw new Error(String((result.data && result.data.detail) || "收款人登记失败"));
    }
    const payee = (result.data && result.data.payee) || {};
    payeeRoster = null;
    await loadPayeeRoster().catch(() => {});
    return String(payee.payee_id || "");
  } catch (error) {
    const status = el("quick-transfer-status");
    if (status) status.textContent = fetchErrorMessage(error, "收款人登记失败，请稍后重试。");
    return "";
  }
}

async function submitQuickTransfer() {
  const status = el("quick-transfer-status");
  const checked = updateTransferFormState();
  if (!checked) return;
  closePayeeCandidates();
  if (checked.error) {
    if (status) status.textContent = checked.error;
    const target = checked.error.includes("金额") ? el("quick-amount") : el("quick-payee");
    if (target) target.focus();
    return;
  }
  const { snapshot } = checked;
  if (checked.gate.needsAck && transferFraudGate && !transferFraudGate.acknowledged) {
    if (status) status.textContent = "请先阅读并确认防诈提示。";
    return;
  }
  if (status) status.textContent = "正在核对收款人…";
  const payeeId = await ensurePayeeId(snapshot);
  if (!payeeId) return;
  rememberTransferAccount();
  const selected = {
    account: snapshot.accountId,
    payee: payeeId,
    amount: `${snapshot.amountYuan} 元`,
    transfer_method: snapshot.method,
    purpose: snapshot.purpose || undefined,
    remark: snapshot.remark || undefined,
  };
  if (status) status.textContent = "正在生成转账计划…";
  await sceneRunTask({
    utterance: buildTransferUtterance(snapshot),
    selected,
    host: "quick-transfer-card",
    status: "quick-transfer-status",
    refresh: refreshAfterExecution,
    onSuccess: quickTransferSucceeded,
  });
}

/* 转账成功后清空金额/用途/附言，保留付款账户与收款人，便于连转 */
function quickTransferSucceeded() {
  ["quick-amount", "quick-purpose", "quick-remark"].forEach((id) => {
    const node = el(id);
    if (node) node.value = "";
  });
  const amountInput = el("quick-amount");
  if (amountInput) amountInput.focus();
  updateTransferFormState();
}

/* 记录页「再转一笔」：把上次的全部字段带回表单，不直接提交。 */
function repeatTransferFromRecord(button, goScene = true) {
  if (!button) return;
  const data = button.dataset;
  const payeeName = data.repeatPayee || "";
  const payeeInput = el("quick-payee");
  const roster = findPayeeById(data.repeatPayeeId) || findPayeeByName(payeeName);
  if (roster) {
    if (payeeInput) payeeInput.value = roster.name;
    applyPickedPayee(roster);
  } else {
    if (payeeInput) payeeInput.value = payeeName;
    pickedPayee = null;
    const accountInput = el("quick-payee-account");
    if (accountInput) accountInput.value = data.repeatAccountTail || "";
    const bankSelect = el("quick-payee-bank");
    if (bankSelect && data.repeatBank) bankSelect.value = data.repeatBank;
  }
  const amountInput = el("quick-amount");
  if (amountInput) amountInput.value = (Number(data.repeatAmount || 0) / 100).toFixed(2);
  const method = data.repeatMethod || "realtime";
  const methodRadio = document.querySelector(`input[name="transfer-method"][value="${method}"]`);
  if (methodRadio) methodRadio.checked = true;
  const purpose = el("quick-purpose");
  if (purpose) purpose.value = data.repeatPurpose || "";
  const remark = el("quick-remark");
  if (remark) remark.value = data.repeatRemark || "";
  updateTransferFormState();
  if (goScene) navigate("scene", { scene: "transfer" });
  el("quick-transfer-panel")?.scrollIntoView({ behavior: "smooth", block: "start" });
  const status = el("quick-transfer-status");
  if (status) status.textContent = "已带入上一笔信息，请核对后确认转账。";
}

async function cancelTransferRecord(recordId) {
  if (!recordId) return;
  const status = el("transfers-status");
  if (status) status.textContent = "正在撤销这笔申请…";
  const result = await post("/api/transfers/cancel", { schedule_id: recordId });
  if (result.status !== 200) {
    if (status) status.textContent = String((result.data && result.data.detail) || "撤销失败，请稍后重试。");
    return;
  }
  await refreshTransfers();
  await refreshAfterExecution();
  const recordsView = el("view-records");
  if (recordsView && !recordsView.hidden) {
    await renderRecordsTab(currentRecordTab);
  }
}

/* 「用 AI 助理转账」：把已填字段带进对话，不空跳。 */
async function transferToChat() {
  const snapshot = transferSnapshot();
  let payeeId = snapshot.payeeId;
  if (!payeeId && snapshot.payee && snapshot.accountTail.length >= 4 && snapshot.bank) {
    payeeId = await ensurePayeeId(snapshot);
  }
  const input = el("utterance");
  if (input) {
    input.value = snapshot.payee && snapshot.amountCents > 0
      ? buildTransferUtterance(snapshot)
      : "我要转账";
  }
  const selected = {};
  if (snapshot.accountId) selected.account = snapshot.accountId;
  if (payeeId) selected.payee = payeeId;
  if (snapshot.amountCents > 0) selected.amount = `${snapshot.amountYuan} 元`;
  selected.transfer_method = snapshot.method;
  if (snapshot.purpose) selected.purpose = snapshot.purpose;
  if (snapshot.remark) selected.remark = snapshot.remark;
  pendingSceneSelected = selected;
  navigate("chat", {});
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

/* 只列出真实存在的对外渠道：网页端 + 飞书。
   本地 IM 只是服务端演示用的内部信道，不对外呈现，故不再出现在表格里。 */
const CHANNEL_ORDER = ["web", "feishu"];
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

/* 代扣协议卡（S2）：协议编号 / 签约 / 累计扣款 / 最近扣款 / 涨价 / 逾期 / 解约后对账。
   金额、日期与协议编号全部来自服务端；已解约卡不再以月费为主信息。 */
function agreementPriceHtml(item) {
  const change = item.price_change;
  if (!change) return "";
  return `<p class="sub-card__price-up">${yuan(change.from_cents)} → ${yuan(change.to_cents)}，
    自 ${esc(change.effective_on)} 起（+${esc(change.percent)}%）</p>`;
}

function agreementReconHtml(item) {
  const charges = item.post_cancel_charges || [];
  if (item.agreement_status !== "cancelled") return "";
  const detail = charges.length
    ? `<p class="sub-card__recon sub-card__recon--bad">解约后仍有 ${charges.length} 笔扣款：
        ${charges.map((c) => `${esc(c.booked_on)} ${yuan(Math.abs(c.amount_cents))}`).join("、")}
       </p>
       <button class="btn btn--sm btn--danger" type="button"
         data-prefill="申请追回：${esc(item.merchant)} 解约后仍扣款">申请追回</button>`
    : '<p class="sub-card__recon">解约后扣款：未发现</p>';
  return `<p class="muted sub-card__facts">解约日期 ${esc(item.cancelled_on || "未知")} ·
      最后一笔扣款 ${esc(item.last_charged_on || "未知")}
      ${item.last_charged_on ? yuan(item.amount_cents) : ""}</p>${detail}`;
}

function subscriptionCardHtml(item, index) {
  const cancelled = item.agreement_status === "cancelled";
  const suspect = item.agreement_status === "suspected";
  const next = item.next_charge_on
    ? `${esc(item.next_charge_on)}${item.next_charge_state === "overdue" ? "（已逾期）" : "（推算）"}`
    : "未提供";
  const windowText = item.window_charged_total_cents == null
    ? ""
    : ` · 近 ${Number(item.charge_window) || 12} 期 ${Number(item.window_charge_count) || 0} 笔 `
      + yuan(item.window_charged_total_cents);
  const total = item.charged_total_cents == null
    ? "累计扣款数据不足"
    : `已扣 ${Number(item.charge_count) || 0} 期 ${yuan(item.charged_total_cents)}${windowText}`;
  const typeChip = item.agreement_type_label
    ? `<span class="chip chip--muted">${esc(item.agreement_type_label)}</span>` : "";
  const state = cancelled ? '<span class="chip">已解约</span>'
    : suspect ? '<span class="chip chip--yellow">疑似</span>' : "";
  const closeBtn = !cancelled && !suspect && item.subscription_id
    ? `<button class="sub-card__cancel" type="button" data-sub-cancel="${esc(item.subscription_id)}"
        data-sub-merchant="${esc(item.merchant)}" data-sub-no="${esc(item.agreement_no || "")}"
        data-sub-type="${esc(item.agreement_type_label || "")}"
        data-sub-signed="${esc(item.signed_on || "")}"
        data-sub-total="${item.charged_total_cents == null ? "" : item.charged_total_cents}"
        data-sub-count="${Number(item.charge_count) || 0}"
        data-sub-last="${esc(item.last_charged_on || "")}"
        data-sub-next="${esc(item.next_charge_on || "")}"
        data-sub-amount="${yuan(item.amount_cents)}"
        data-sub-cycle="${esc(item.cycle_label || "")}">关闭扣款协议</button>`
    : suspect
      ? '<span class="chip chip--yellow">未匹配到本行协议</span>' : "";
  const overdue = item.next_charge_state === "overdue"
    ? `<p class="sub-card__overdue">预计 ${esc(item.next_charge_on || "—")} 扣款，
        已过 ${Number(item.overdue_days) || 0} 天未见扣款记录；按周期顺延。</p>` : "";
  return `<article class="sub-card${cancelled ? " sub-card--off" : ""}"
      data-merchant-key="${esc(item.merchant)}"
      data-card-tail="${esc(item.card_tail || "")}">
    <span class="sub-card__bar" aria-hidden="true"
      style="background:${cancelled ? "var(--rule-strong)" : sceneColor(index)}"></span>
    <div class="sub-card__in">
      <div class="sub-card__top">
        <span class="sub-card__merchant">${esc(item.merchant)}</span>${typeChip}${state}
      </div>
      ${cancelled ? "" : `<div class="sub-card__line">
        <p class="sub-card__amount">${yuan(item.amount_cents)}
          <span class="sub-card__meta"> / ${esc(item.cycle_label || "周期未知")} · 下次 ${next}</span></p>
        ${closeBtn}
      </div>`}
      <p class="muted sub-card__facts">协议 ${esc(item.agreement_no || "未匹配")} ·
        ${esc(item.signed_on || "签约时间未知")} 签约 · ${total}</p>
      <p class="muted sub-card__facts">最近扣款 ${esc(item.last_charged_on || "未知")}</p>
      ${cancelled ? "" : agreementPriceHtml(item)}${cancelled ? "" : overdue}${agreementReconHtml(item)}
      <button class="sub-card__more" type="button" data-sub-detail="${esc(item.insight_id)}"
        aria-haspopup="dialog">协议详情 · 扣款记录<span class="sub-card__chev" aria-hidden="true">›</span></button>
    </div>
  </article>`;
}

/* 协议详情（原卡片折叠区）：协议字段 + 最近 12 笔扣款 + 关闭影响，只在弹层渲染这一份。 */
function subscriptionBasisHtml(item) {
  const price = item.price_change
    ? `涨价：${yuan(item.price_change.from_cents)} → ${yuan(item.price_change.to_cents)}，
       自 ${esc(item.price_change.effective_on)} 起（+${esc(item.price_change.percent)}%）`
    : "未发现近 12 个月涨价";
  const total = item.charged_total_cents == null
    ? "累计扣款数据不足"
    : `累计 ${Number(item.charge_count) || 0} 期 ${yuan(item.charged_total_cents)}`;
  const charges = (item.evidence || []).slice(0, 12).map((txn) =>
    `<li><span class="mono">${esc(txn.booked_on)} · ${yuan(Math.abs(txn.amount_cents))}</span>
      <span class="tech">渠道 ${esc(txn.channel || "—")} ·
      付款账户 ${esc(txn.payment_account_label || (txn.card_tail ? `尾号 ${txn.card_tail}` : "未提供"))} ·
      ${esc(txn.record_no || txn.txn_id || txn.record_id)}</span></li>`).join("");
  const nonSettlement = (item.evidence || []).filter(
    (txn) => txn.is_settlement === false
  );
  return `<p class="sub-detail__lead">${esc(item.merchant)} ·
      ${esc(item.agreement_type_label || "类型未知")} · 协议 ${esc(item.agreement_no || "未匹配")}</p>
    <p>签约 ${esc(item.signed_on || "未知")} · ${total} · 最近扣款 ${esc(item.last_charged_on || "未知")}</p>
    <p>${price}</p>
    ${nonSettlement.length ? `<p class="sub-card__recon sub-card__recon--bad">有 ${nonSettlement.length} 笔扣款来自非结算账户（储蓄账户或信用卡），请核对代扣协议绑定账户。</p>` : ""}
    <p class="muted">下次扣款 ${esc(item.next_charge_on || "未知")}
      ${item.next_charge_on ? "（推算）" : ""} · 置信度 ${esc(confidenceLabel(item.confidence))}</p>
    ${item.agreement_status === "cancelled" ? agreementReconHtml(item) : ""}
    <p><span class="muted">关闭影响：</span>关闭后银行卡侧不再放款；商户是否继续服务由商户决定。</p>
    ${subscriptionNoticesHtml(item)}
    ${charges ? `<details open><summary>扣款记录（最近 ${item.evidence.length} 笔）</summary><ul>${charges}</ul></details>` : ""}`;
}

/* 订阅详情弹层：卡片第三行触发，正文复用 subscriptionBasisHtml（单一来源）。
   Esc / 点遮罩沿用原生 dialog 行为，与 #app-dialog 一致。 */
function openSubDetail(insightId) {
  const details = state.options && state.options.subscription_details;
  const insights = (details && details.insights) || [];
  const item = insights.find((row) => row.insight_id === insightId);
  const dialog = el("sub-detail");
  if (!item || !dialog) return;
  const body = el("sub-detail-body");
  if (body) body.innerHTML = subscriptionBasisHtml(item);
  if (typeof dialog.showModal === "function") dialog.showModal();
  else dialog.setAttribute("open", "");
}

function closeSubDetail() {
  closeDialog(el("sub-detail"));
}

/* 顶部概览：本月已扣 / 本月待扣 / 未来 30 天预计 + 在签/疑似/已解约计数。
   三个数字口径不同是有意为之，说明文字固定在下面。 */
function subscriptionSummaryHtml(details) {
  const summary = details.summary || {};
  const month = summary.month_label || String(details.as_of || "").slice(0, 7);
  return `<div class="sub-overview">
      <div class="sub-overview__main">
        <span class="sub-overview__k">本月已扣（${esc(month)}）</span>
        <b class="sub-overview__v">${yuan(summary.month_charged_cents || 0)}</b>
      </div>
      <div class="sub-overview__main">
        <span class="sub-overview__k">本月待扣（截至今天）</span>
        <b class="sub-overview__v">${yuan(summary.month_due_cents || 0)}</b>
      </div>
      <div class="sub-overview__main">
        <span class="sub-overview__k">未来 30 天预计</span>
        <b class="sub-overview__v">${yuan(summary.next_30d_cents || 0)}</b>
      </div>
      <div class="sub-overview__chips">
        <span class="chip">在签 ${Number(summary.active_count) || 0} 项</span>
        <span class="chip${summary.suspected_count ? " chip--yellow" : ""}">疑似待核对 ${Number(summary.suspected_count) || 0} 项</span>
        <span class="chip">已解约 ${Number(summary.cancelled_count) || 0} 项</span>
      </div>
    </div>
    <p class="muted scene-hint">本月已扣 = 本月已记账的协议扣款（年付只在扣款当月计入）；
      本月待扣 = 今天到月底尚未到期的扣款；未来 30 天预计按推算扣款日计入，年付不摊到每月。</p>`;
}

/* 筛选：按协议 / 按商户 / 按周期 + 扣款卡。卡选项只列真实出现在协议里的尾号。 */
function agreementFiltersHtml(insights) {
  const cards = Array.from(new Map(
    insights.filter((item) => item.card_tail)
      .map((item) => [item.card_tail, item.card_tail])
  ).keys());
  const view = state.subView || "agreement";
  const card = state.subCard || "all";
  return `<div class="sub-filters">
    <div class="sub-filters__group" role="group" aria-label="查看方式">
      ${[["agreement", "按协议"], ["merchant", "按商户"], ["cycle", "按周期"]]
        .map(([key, label]) => `<button class="sub-seg${view === key ? " is-active" : ""}"
          type="button" data-sub-view="${key}">${label}</button>`).join("")}
    </div>
    <div class="sub-filters__group" role="group" aria-label="扣款卡">
      <button class="sub-seg${card === "all" ? " is-active" : ""}"
        type="button" data-sub-card="all">全部卡</button>
      ${cards.map((tail) => `<button class="sub-seg${card === tail ? " is-active" : ""}"
        type="button" data-sub-card="${esc(tail)}">尾号 ${esc(tail)}</button>`).join("")}
    </div>
    ${card === "all" ? "" : '<span class="muted sub-filters__hint">已按该卡筛选，以下全部为这张卡的代扣协议</span>'}
  </div>`;
}

/* 列表分组：按协议（在签 / 已解约）/ 按商户 / 按周期。 */
function agreementListHtml(insights) {
  const view = state.subView || "agreement";
  const card = state.subCard || "all";
  const visible = card === "all"
    ? insights
    : insights.filter((item) => item.card_tail === card);
  if (view === "merchant") {
    const groups = new Map();
    visible.forEach((item) => {
      const key = item.merchant;
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(item);
    });
    return `<p class="panel__title panel__title--sub">按商户查看</p>` +
      Array.from(groups.entries()).map(([merchant, items]) => {
        const total = items.reduce((sum, item) => sum + (Number(item.charged_total_cents) || 0), 0);
        return `<p class="muted sub-group__head">${esc(merchant)} ·
          ${items.length} 个协议 · 累计 ${yuan(total)}</p>
          <div class="sub-wall">${items.map((item) =>
            subscriptionCardHtml(item, insights.indexOf(item))).join("")}</div>`;
      }).join("");
  }
  if (view === "cycle") {
    const cycles = [["monthly", "月度"], ["yearly", "年度"], ["unknown", "周期未知"]];
    return `<p class="panel__title panel__title--sub">按周期查看</p>` +
      cycles.map(([cycle, label]) => {
        const items = visible.filter((item) =>
          (cycle === "unknown" ? !["monthly", "yearly"].includes(item.cycle) : item.cycle === cycle));
        return items.length
          ? `<p class="muted sub-group__head">${label} · ${items.length} 项</p>
             <div class="sub-wall">${items.map((item) =>
               subscriptionCardHtml(item, insights.indexOf(item))).join("")}</div>`
          : "";
      }).join("");
  }
  const active = visible.filter((item) => item.agreement_status === "active");
  const suspected = visible.filter((item) => item.agreement_status === "suspected");
  const cancelled = visible.filter((item) => item.agreement_status === "cancelled");
  const activeHtml = active.length
    ? `<p class="panel__title panel__title--sub">在签协议 <span class="tech">${active.length} 项</span></p>
      <div class="sub-wall">${active.map((item) =>
        subscriptionCardHtml(item, insights.indexOf(item))).join("")}</div>`
    : '<p class="panel__title panel__title--sub">在签协议</p><p class="muted">当前没有在签协议。</p>';
  const suspectedHtml = suspected.length
    ? `<p class="panel__title panel__title--sub">疑似流水 <span class="tech">${suspected.length} 项 · 未匹配本行协议</span></p>
      <div class="sub-wall">${suspected.map((item) =>
        subscriptionCardHtml(item, insights.indexOf(item))).join("")}</div>`
    : "";
  const cancelledHtml = cancelled.length
    ? `<p class="panel__title panel__title--sub">已解约 <span class="tech">${cancelled.length} 项</span></p>
      <div class="sub-wall">${cancelled.map((item) =>
        subscriptionCardHtml(item, insights.indexOf(item))).join("")}</div>`
    : "";
  return activeHtml + suspectedHtml + cancelledHtml;
}

/* 账单线索深链：定位到商户卡；找不到时说明原因，不做无依据的跳转。 */
function applyAgreementFocus() {
  const key = state.pendingAgreementFocus;
  if (!key) return;
  state.pendingAgreementFocus = "";
  const cards = Array.from(document.querySelectorAll("#subscription-list .sub-card"));
  const target = cards.find((card) =>
    (card.dataset.merchantKey || "").includes(key) || key.includes(card.dataset.merchantKey || ""));
  const note = el("subscription-focus-note");
  if (!target) {
    if (note) {
      note.hidden = false;
      note.textContent = `没有找到「${key}」的本行代扣协议：可能是其他卡或其他渠道扣款。`;
    }
    return;
  }
  if (note) note.hidden = true;
  target.scrollIntoView({ behavior: "smooth", block: "center" });
  target.classList.add("sub-card--focus");
  window.setTimeout(() => target.classList.remove("sub-card--focus"), 3200);
  const detail = target.querySelector("[data-sub-detail]");
  if (detail) openSubDetail(detail.dataset.subDetail || "");
}

/* 未来扣款只列尚未扣的项：状态在签、下次扣款日不早于数据日，按日期升序最多 6 条。 */
function subscriptionTimelineHtml(insights) {
  const rows = insights
    .filter((item) => item.agreement_status === "active" && item.next_charge_on)
    .filter((item) => String(item.next_charge_on) >= (state.options.subscription_details.as_of || ""))
    .sort((a, b) => String(a.next_charge_on).localeCompare(String(b.next_charge_on)))
    .slice(0, 6);
  const head = '<p class="panel__title panel__title--sub">接下来要扣的</p>';
  if (!rows.length) {
    return head + '<p class="muted">暂无可排期的未来扣款。</p>';
  }
  const items = rows.map((item) => `<li class="sub-ev${item.next_charge_state === "overdue" ? " is-hot" : ""}">
      <span class="sub-ev__date">${esc(item.next_charge_on)}</span>
      <span class="sub-ev__name">${esc(item.merchant)}
        <span class="sub-ev__tag">推算</span></span>
      <span class="sub-ev__amt">${yuan(item.amount_cents)}</span>
    </li>`).join("");
  return head + `<ol class="sub-ev-list">${items}</ol>
    <p class="muted scene-hint">「推算」按历史扣费周期计算，实际扣款日以商户为准。</p>`;
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
    const filters = el("subscription-filters");
    if (filters) filters.innerHTML = "";
    const note = el("subscription-focus-note");
    if (note) {
      note.hidden = true;
      note.textContent = "";
    }
  };
  if (state.optionsStatus !== "ready") {
    reset(state.optionsStatus === "error"
      ? "代扣协议数据暂不可用，请稍后重试。" : "正在读取代扣协议…");
    return;
  }
  if (!details) {
    reset("代扣协议服务尚未接入。");
    return;
  }
  status.textContent = `数据截至 ${details.as_of} · 识别结果已同步`;
  if (caps && caps.boundary) {
    boundary.hidden = false;
    boundary.textContent = caps.boundary;
  }
  const insights = details.insights || [];
  const filters = el("subscription-filters");
  if (filters) {
    filters.innerHTML = insights.length ? agreementFiltersHtml(insights) : "";
  }
  if (!insights.length) {
    if (summary) summary.innerHTML = "";
    if (timeline) timeline.innerHTML = "";
    list.innerHTML = '<p class="muted">未识别到代扣协议。</p>';
  } else {
    if (summary) summary.innerHTML = subscriptionSummaryHtml(details);
    if (timeline) timeline.innerHTML = subscriptionTimelineHtml(insights);
    list.innerHTML = agreementListHtml(insights);
    applyAgreementFocus();
  }
}

/* 解约回执：解约日期 / 最后一笔 / 解约后扣款对账 / 演示短信 / 追回入口。 */
function renderSubscriptionCancel(cancel) {
  const box = el("subscription-cancel-result");
  if (!box) return;
  if (!cancel) {
    box.hidden = true;
    box.innerHTML = "";
    return;
  }
  box.hidden = false;
  const post = (cancel.post_cancel_charges || []);
  const recon = post.length
    ? `<p class="sub-card__recon sub-card__recon--bad">解约后仍有 ${post.length} 笔扣款：
        ${post.map((c) => `${esc(c.booked_on)} ${yuan(Math.abs(c.amount_cents))}`).join("、")}</p>
       <button class="btn btn--sm btn--danger" type="button"
         data-prefill="申请追回：${esc(post[0].merchant_label || "")} 解约后仍扣款">申请追回</button>`
    : '<p class="sub-card__recon">解约后扣款：未发现</p>';
  box.innerHTML = `<div class="takeover">
    <p><b>${esc(cancel.label)}</b></p>
    <p class="muted">解约日期 ${esc(cancel.cancelled_on || "未知")} ·
      最后一笔扣款 ${esc(cancel.last_charge_on || "未知")}
      ${cancel.last_charge_cents != null ? yuan(Math.abs(cancel.last_charge_cents)) : ""}</p>
    ${recon}
    <p class="muted">${esc(cancel.boundary)}</p>
    ${cancel.sms_receipt
      ? `<p class="muted">短信回执（演示，不发送真实短信）：${esc(cancel.sms_receipt.message)}</p>`
      : ""}
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

/* 卡面：脱敏卡号 / 卡组织 / 有效期 / 分卡限额 / 按状态允许的动作。
   按钮一律由服务端 `allowed_actions` 驱动；前端不按状态自行扩展或隐藏。 */
const CARD_ACTION_META = {
  temp_lock: { label: "临时冻结", cls: "btn--warn", group: "common" },
  unlock: { label: "解挂", cls: "btn--danger", group: "risk" },
  report_lost: { label: "正式挂失", cls: "btn--danger", group: "risk" },
  replace: { label: "申请补卡", cls: "btn--danger", group: "risk" },
  adjust_debit_limit: { label: "调整通道限额", cls: "", group: "common" },
  adjust_credit_limit: { label: "调整信用额度", cls: "", group: "common" },
  set_cycle_limit: { label: "账单周期限额", cls: "", group: "common" },
  apply_virtual: { label: "申请虚拟卡", cls: "btn--ghost", group: "common" },
  view_lock_detail: { label: "查看冻结详情", cls: "btn--ghost", group: "common" },
  view_lost_detail: { label: "查看挂失详情", cls: "btn--ghost", group: "common" },
};

function cardLimitSummary(limits) {
  if (!limits) return "限额未知";
  if (limits.kind === "credit") {
    const parts = [
      `固定额度 ${limits.credit_limit_cents == null ? "未配置" : yuan(limits.credit_limit_cents)}`,
    ];
    if (limits.temp_credit_limit_cents != null) {
      parts.push(
        `临时额度 ${yuan(limits.temp_credit_limit_cents)}`
        + (limits.temp_credit_until ? `（至 ${esc(limits.temp_credit_until)}）` : ""),
      );
    }
    parts.push(
      `账单周期限额 ${limits.cycle_limit_cents == null ? "未配置" : yuan(limits.cycle_limit_cents)}`,
    );
    return parts.join(" · ");
  }
  return [
    ["ATM 取现", limits.atm_limit_cents],
    ["转账", limits.transfer_limit_cents],
    ["消费", limits.purchase_limit_cents],
    ["境外", limits.overseas_limit_cents],
  ]
    .map(([label, value]) => `${label} ${value == null ? "未配置" : yuan(value)}`)
    .join(" · ");
}

function cardStatusNote(card) {
  const lock = card.temp_lock;
  if (card.status === "locked" && lock) {
    if (lock.expired) return `临时冻结已于 ${esc(lock.until)} 到期，自动解挂；刷新后按正常状态处理。`;
    return `临时冻结剩余 ${Number(lock.days_left) || 0} 天（至 ${esc(lock.until)} 自动解挂）`
      + ` · 原因：${esc(lock.reason_label || "未说明")}`;
  }
  if (card.status === "lost") {
    const lost = card.lost || {};
    if (card.replacement && !lost.at) {
      return `已补卡（旧卡作废）· 新卡尾号 ${esc(card.replacement.new_card_tail || "未知")}`;
    }
    const replaced = card.replacement
      ? ` · 已补卡，新卡尾号 ${esc(card.replacement.new_card_tail || "未知")}`
      : "";
    return `已正式挂失（${esc(lost.at || "日期未知")}，${esc(lost.reason_label || "原因未说明")}）`
      + ` · 不可解挂${replaced}`;
  }
  return "";
}

function cardFaceHtml(card) {
  const id = esc(card.card_id);
  const actions = Array.isArray(card.allowed_actions) ? card.allowed_actions : [];
  const actionable = actions.filter((key) => CARD_ACTION_META[key]);
  const button = (key) => {
    const meta = CARD_ACTION_META[key];
    return `<button class="btn btn--sm ${meta.cls}" type="button"
      data-card-action="${key}" data-card-id="${id}">${meta.label}</button>`;
  };
  const common = actionable.filter((key) => CARD_ACTION_META[key].group === "common");
  const risk = actionable.filter((key) => CARD_ACTION_META[key].group === "risk");
  const rows = [];
  if (common.length) {
    rows.push(`<div class="card-face__keys-row"><span class="card-face__keys-k">常用</span>${common.map(button).join("")}</div>`);
  }
  if (risk.length) {
    rows.push(`<div class="card-face__keys-row card-face__keys-row--risk"><span class="card-face__keys-k">风险操作</span>${risk.map(button).join("")}</div>`);
  }
  const note = cardStatusNote(card);
  const stateKey = card.effective_status || card.status;
  return `<article class="card-face card-face--${esc(stateKey)}">
    <div class="card-face__top">
      <span class="card-face__type">${esc(card.type_label)}
        <span class="muted">${esc(card.brand_label || "卡组织未知")}</span></span>
      <span class="card-state card-state--${esc(stateKey)}">${esc(card.status_label)}</span>
    </div>
    <p class="card-face__meta">
      <span class="card-face__no mono">${esc(card.masked_no || "卡号未知")}</span>
      <span class="muted">${card.expiry ? `有效期 ${esc(card.expiry)}` : "有效期未知"}</span>
    </p>
    ${card.account_class ? `<p class="muted card-face__class">${esc(card.account_class)} 类户</p>` : ""}
    <p class="card-face__limits">${esc(cardLimitSummary(card.limits))}</p>
    ${note ? `<p class="card-face__note">${note}</p>` : ""}
    <div class="card-face__keys">${rows.join("")}</div>
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
  return `<div class="card-wall">${cards.map((card) => cardFaceHtml(card)).join("")}</div>`
    + `<p class="muted cards-hint">临时冻结可解、到期自动解挂；正式挂失不可逆，需补卡换新卡号。`
    + `所有红色动作在弹窗内完成一次性验证码与二次确认（演示验证码，非真实人脸 / 短信 / U 盾）。</p>`;
}

/* 卡片使用概览：只做状态与限额的文字汇总，不画无决策价值的占比饼图，
   也不编造「近 30 天消费」——流水没有稳定卡号，服务端没有按卡聚合的事实。 */
function cardsOverviewHtml(cards) {
  if (!cards.length) return "";
  const counts = [
    ["active", "正常"],
    ["locked", "临时冻结"],
    ["lost", "已正式挂失"],
  ]
    .map(([key, label]) => {
      const count = cards.filter((card) => (card.effective_status || card.status) === key).length;
      return count ? `${label} ${count} 张` : "";
    })
    .filter(Boolean)
    .join(" · ");
  const rows = cards.map((card) => `<li>
    <b>${esc(card.type_label)} · ${esc(card.masked_no || "卡号未知")}</b>
    <span>${esc(card.status_label)}</span>
    <span class="muted">${esc(card.brand_label || "卡组织未知")}${card.account_class ? ` · ${esc(card.account_class)} 类户` : ""}</span>
    <span class="muted">${esc(cardLimitSummary(card.limits))}</span>
  </li>`).join("");
  return `<div class="cards-overview">
    <p class="panel__title panel__title--sub">卡片使用概览</p>
    <ul class="cards-overview__list">${rows}</ul>
    <p class="muted scene-hint">状态：${esc(counts || "无")} · 卡状态实时同步 · 数字读 /api/cards</p>
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
  state.cardPayload = payload;
  const synced = payload.synced_at ? new Date(payload.synced_at) : null;
  const syncedText = synced && !Number.isNaN(synced.getTime())
    ? ` · 卡状态实时同步（${String(synced.getHours()).padStart(2, "0")}:${String(synced.getMinutes()).padStart(2, "0")}）`
    : " · 卡状态实时同步";
  el("cards-status").textContent = `共 ${state.cards.length} 张卡 · 数据截至 ${payload.as_of || "—"}${syncedText}`;
  el("cards-list").innerHTML = cardsHtml(payload);
  const visual = el("cards-visual");
  if (visual) visual.innerHTML = cardsOverviewHtml(state.cards);
}

/* ---------------- 卡片操作弹窗（卡片页与对话共用） ----------------
   弹窗内完成「信息收集 → 后果明示 → 服务端计划 → 一次性验证码 → 二次确认」，
   不把红色动作甩到对话。对话里的卡片计划卡也打开同一个弹窗。 */
const CARD_FLOW_BY_ACTION = {
  temp_lock: "freeze",
  unlock: "unlock",
  report_lost: "report",
  replace: "replace",
  adjust_debit_limit: "debit_limit",
  adjust_credit_limit: "credit_limit",
  set_cycle_limit: "cycle",
  apply_virtual: "virtual",
};

const CARD_FLOW_TITLES = {
  freeze: "临时冻结（口头挂失）",
  report: "正式挂失",
  replace: "申请补卡",
  unlock: "解挂",
  debit_limit: "调整借记卡通道限额",
  credit_limit: "调整信用卡额度",
  cycle: "设置账单周期消费限额",
  virtual: "申请虚拟卡",
  lock_detail: "冻结详情",
  lost_detail: "挂失详情",
};

const CARD_FLOW_STEPS = {
  freeze: ["类型与原因", "后果确认", "验证与确认"],
  report: ["挂失类型与原因", "后果与补卡", "验证与确认"],
  replace: ["补卡方式", "寄送信息", "验证与确认"],
  unlock: ["解挂确认", "后果确认", "验证与确认"],
  debit_limit: ["选择通道与额度", "生效说明", "验证与确认"],
  credit_limit: ["选择额度类型", "生效说明", "验证与确认"],
  cycle: ["设置限额", "生效说明", "验证与确认"],
  virtual: ["设置额度", "生效说明", "确认执行"],
  lock_detail: ["冻结详情"],
  lost_detail: ["挂失详情"],
};

const CARD_LIMIT_KIND_LABELS = {
  atm: "ATM 取现",
  transfer: "转账",
  purchase: "消费",
  overseas: "境外",
};

const cardFlowState = {
  open: false, mode: "page", flow: "", card: null, plan: null,
  step: 1, form: {}, challenge: null, result: null, busy: false,
};

function cardFlowError(message) {
  const node = el("card-flow-error");
  if (!node) return;
  node.textContent = message || "";
  node.hidden = !message;
}

function cardFlowYuanToCents(text) {
  const value = Number(text);
  if (!Number.isFinite(value) || value <= 0) return null;
  return Math.round(value * 100);
}

function cardFlowDialogOpen() {
  const dialog = el("card-flow-dialog");
  if (!dialog) return;
  if (typeof dialog.showModal === "function") dialog.showModal();
  else dialog.setAttribute("open", "");
}

function openCardFlow(kind, cardId) {
  const card = (state.cards || []).find((item) => item.card_id === cardId);
  if (!card) return;
  if (kind === "view_lock_detail" || kind === "view_lost_detail") {
    openCardDetail(card, kind);
    return;
  }
  const flow = CARD_FLOW_BY_ACTION[kind];
  if (!flow) return;
  cardFlowState.open = true;
  cardFlowState.mode = "page";
  cardFlowState.flow = flow;
  cardFlowState.card = card;
  cardFlowState.plan = null;
  cardFlowState.challenge = null;
  cardFlowState.result = null;
  cardFlowState.step = 1;
  cardFlowState.busy = false;
  cardFlowState.form = {
    days: 7,
    lockReason: "cant_find",
    lossReason: "lost",
    withReplace: card.status === "lost" ? "yes" : "no",
    address: (state.cardPayload && state.cardPayload.default_shipping_address) || "",
    mailMethod: "standard",
    limitKind: "atm",
    amountYuan: "",
    creditMode: "temporary",
    months: 1,
  };
  renderCardFlow();
  cardFlowDialogOpen();
}

function openCardDetail(card, kind) {
  cardFlowState.open = true;
  cardFlowState.mode = "detail";
  cardFlowState.flow = kind === "view_lock_detail" ? "lock_detail" : "lost_detail";
  cardFlowState.card = card;
  cardFlowState.plan = null;
  cardFlowState.challenge = null;
  cardFlowState.result = null;
  cardFlowState.step = 1;
  cardFlowState.busy = false;
  renderCardFlow();
  cardFlowDialogOpen();
}

/* 对话里的卡片计划卡：沿用已冻结的计划，直接进入「验证与确认」一步。 */
async function openCardFlowFromPlan(plan) {
  const cardNodes = (plan.nodes || []).filter((item) => String(item.action || "").startsWith("card."));
  const node = cardNodes[0];
  const cardId = node && (node.card_id || (node.params && node.params.card_id)) || "";
  let card = (state.cards || []).find((item) => item.card_id === cardId) || null;
  if (!card && cardId) {
    try {
      const payload = await apiFetch("GET", "/api/cards");
      state.cards = (payload.data && payload.data.cards) || [];
      card = state.cards.find((item) => item.card_id === cardId) || null;
    } catch (_) {
      card = null;
    }
  }
  if (!card) {
    state.notice = interceptHtml("无法打开操作确认", "计划里没有找到可操作的卡片，请刷新卡片列表后重试。", false);
    renderStage();
    return;
  }
  const action = node ? String(node.action || "") : "";
  const actions = new Set(cardNodes.map((item) => String(item.action || "")));
  const composite = cardNodes.length > 1 || actions.size > 1;
  const flow = composite ? "" : ({
    "card.lock": "freeze",
    "card.report_lost": "report",
    "card.unlock": "unlock",
    "card.replace": "replace",
    "card.adjust_credit_limit": "credit_limit",
    "card.set_cycle_limit": "cycle",
    "card.set_limit": "debit_limit",
    "card.apply_virtual": "virtual",
  })[action] || "";
  cardFlowState.open = true;
  cardFlowState.mode = "plan";
  cardFlowState.flow = flow;
  cardFlowState.card = card;
  cardFlowState.plan = plan;
  cardFlowState.challenge = null;
  cardFlowState.result = null;
  cardFlowState.step = flow ? cardFlowTotalSteps() : 1;
  cardFlowState.busy = false;
  cardFlowState.form = {};
  renderCardFlow();
  cardFlowDialogOpen();
}

function closeCardFlow() {
  const dialog = el("card-flow-dialog");
  if (dialog) {
    if (typeof dialog.close === "function") dialog.close();
    else dialog.removeAttribute("open");
  }
  cardFlowState.open = false;
  cardFlowState.plan = null;
  cardFlowState.challenge = null;
  cardFlowState.result = null;
  cardFlowState.busy = false;
  cardFlowError("");
}

function cardFlowTotalSteps() {
  const steps = CARD_FLOW_STEPS[cardFlowState.flow];
  return Array.isArray(steps) ? steps.length : 1;
}

function cardFlowRadio(field, options, current) {
  return `<div class="card-flow__choices">${options
    .map(([key, label, desc]) => `<label class="card-flow__choice${current === key ? " is-active" : ""}">
      <input type="radio" name="${esc(field)}" value="${key}" data-card-flow-field="${esc(field)}"
        ${current === key ? "checked" : ""} />
      <span><b>${label}</b>${desc ? `<small>${desc}</small>` : ""}</span>
    </label>`)
    .join("")}</div>`;
}

function cardFlowAmountField(label, placeholder) {
  return `<label class="field card-flow__amount"><span class="field__label">${label}</span>
    <input class="input" type="number" min="1" step="100" inputmode="decimal"
      value="${esc(cardFlowState.form.amountYuan || "")}" placeholder="${placeholder}"
      data-card-flow-field="amountYuan" data-card-flow-live="1" /></label>`;
}

function cardFlowConsequences() {
  const flow = cardFlowState.flow;
  const card = cardFlowState.card;
  const items = {
    freeze: [
      "功能冻结：消费、取现、转账、代扣全部暂停。",
      "费用：免费。",
      "解挂：有效期内可线上解挂，到期自动解挂。",
      "不换卡号：找回卡后可直接恢复使用。",
    ],
    report: [
      "功能冻结：消费、取现、转账、代扣全部停止。",
      "费用：挂失费 ¥20（演示口径：只登记应收，不实际扣款）。",
      "解挂：不可线上解挂，只能本人带证件到柜台处理。",
      "换卡号：继续用卡必须补卡，新卡号、旧卡作废。",
    ],
    replace: [
      "新卡号：补卡会生成新卡号，旧卡立即作废。",
      "费用：普通邮寄 ¥10 / 加急 ¥20（演示口径：只登记应收，不实际扣款）。",
      "到卡时间：普通 3–5 个工作日 / 加急 1–2 个工作日（演示推算）。",
      "寄送：按您填写的地址寄出；演示环境不实际制卡或邮寄。",
    ],
    unlock: [
      "恢复范围：消费、取现、转账、代扣立即恢复。",
      "费用：解挂免费。",
      "风险提示：解挂会解除当前保护，请确认卡片在您本人手中。",
    ],
    debit_limit: [
      "生效范围：只调整所选通道，其余通道限额不变。",
      "生效时间：提交后立即生效。",
      "监管上限：Ⅱ / Ⅲ 类户仍受账户类别监管上限约束。",
    ],
    credit_limit: [
      "临时额度：立即生效，到期自动恢复；本演示默认 1 个月。",
      "固定额度：只登记审批，预计 3–5 个工作日；审批期间额度不变。",
      "额度用途：提高可用额度，不改变已出账单金额。",
    ],
    cycle: [
      "生效范围：只影响本账单周期的消费上限。",
      "生效时间：提交后立即生效。",
      "不是信用额度：不会提高或降低固定额度。",
    ],
    virtual: [
      "新卡：生成一张独立虚拟卡，不影响实体卡。",
      "额度：按您填写的日限额生效。",
    ],
  }[flow] || [];
  return `<ul class="card-flow__list">${items.map((item) => `<li>${esc(item)}</li>`).join("")}</ul>`
    + (flow === "report" || flow === "replace"
      ? `<p class="muted card-flow__note">卡片：${esc(card.type_label)} · ${esc(card.masked_no)}</p>`
      : "");
}

function cardFlowAddressFields() {
  const form = cardFlowState.form;
  return `<div class="card-flow__grid">
    <label class="field"><span class="field__label">寄送地址（可修改）</span>
      <input class="input" type="text" maxlength="80" value="${esc(form.address || "")}"
        data-card-flow-field="address" data-card-flow-live="1" /></label>
    <label class="field"><span class="field__label">邮寄方式</span>
      <select class="input" data-card-flow-field="mailMethod">
        <option value="standard" ${form.mailMethod === "standard" ? "selected" : ""}>普通邮寄 · ¥10 · 预计 3–5 个工作日</option>
        <option value="express" ${form.mailMethod === "express" ? "selected" : ""}>加急 · ¥20 · 预计 1–2 个工作日</option>
      </select></label>
  </div>
  <p class="muted card-flow__note">演示口径：只登记补卡请求与费用，不实际制卡、不实际邮寄。</p>`;
}

function cardFlowStepOne() {
  const flow = cardFlowState.flow;
  const form = cardFlowState.form;
  const card = cardFlowState.card;
  if (flow === "freeze" || flow === "report") {
    const type = flow === "freeze" ? "temporary" : "formal";
    return `<p class="card-flow__q">先确认挂失类型：</p>
      ${cardFlowRadio("__type", [
        ["temporary", "临时冻结（口头挂失）", "可解、免费、5–15 天、到期自动解挂"],
        ["formal", "正式挂失", "不可逆、可能收费、必须补卡换新卡号"],
      ], type)}
      ${flow === "freeze"
        ? `<div class="card-flow__grid">
            <label class="field"><span class="field__label">冻结时长</span>
              <select class="input" data-card-flow-field="days">
                ${[5, 7, 10, 15].map((day) => `<option value="${day}" ${Number(form.days) === day ? "selected" : ""}>${day} 天</option>`).join("")}
              </select></label>
          </div>
          <p class="card-flow__q">冻结原因：</p>
          ${cardFlowRadio("lockReason", [
            ["cant_find", "找不到卡", "先冻住，找到后可线上解挂"],
            ["suspected_risk", "疑似风险", "担心被人使用"],
            ["other", "其他", "稍后可在详情里补充"],
          ], form.lockReason)}`
        : `<p class="card-flow__q">挂失原因：</p>
          ${cardFlowRadio("lossReason", [
            ["lost", "遗失", "卡片丢失，未发现盗刷"],
            ["stolen", "被盗", "将登记防盗刷处置（演示：只登记风险标记）"],
            ["other", "其他", "其他原因"],
          ], form.lossReason)}
          ${form.lossReason === "stolen" ? '<p class="card-flow__warn">被盗会追加防盗刷提示；演示环境不接真实风控系统。</p>' : ""}`}`;
  }
  if (flow === "replace") {
    return `<p class="card-flow__q">补卡方式：</p>
      ${card.status === "lost"
        ? `<p class="card-flow__note">该卡已正式挂失，补卡将换新卡号，旧卡作废。</p>`
        : cardFlowRadio("withReplace", [
          ["no", "仅补卡（卡损坏）", "不挂失，直接换一张新卡"],
          ["yes", "挂失并补卡", "同时正式挂失并换新卡号"],
        ], form.withReplace)}`;
  }
  if (flow === "unlock") {
    const lock = card.temp_lock || {};
    return `<div class="card-flow__summary">
      <p><b>当前状态：</b>${esc(card.status_label)}</p>
      <p><b>冻结原因：</b>${esc(lock.reason_label || "未说明")}</p>
      <p><b>到期日：</b>${esc(lock.until || "未提供")}</p>
      <p class="muted">解挂后消费、取现、转账、代扣立即恢复。</p>
    </div>
    <p class="card-flow__q">确认要解挂这张卡吗？</p>`;
  }
  if (flow === "debit_limit") {
    return `<p class="card-flow__q">选择要调整的通道：</p>
      ${cardFlowRadio("limitKind", [
        ["atm", "ATM 取现", "现金取款"],
        ["transfer", "转账", "转出到其他账户"],
        ["purchase", "消费", "刷卡 / 扫码消费"],
        ["overseas", "境外", "境外交易"],
      ], form.limitKind)}
      ${cardFlowAmountField("新限额（元）", "例如 5000")}`;
  }
  if (flow === "credit_limit") {
    return `<p class="card-flow__q">选择额度类型：</p>
      ${cardFlowRadio("creditMode", [
        ["temporary", "临时额度", "立即生效，默认 1 个月后自动恢复"],
        ["permanent", "固定额度", "需审批 3–5 个工作日，审批期间额度不变"],
      ], form.creditMode)}
      ${cardFlowAmountField("申请额度（元）", "例如 10000")}
      ${form.creditMode === "temporary"
        ? `<label class="field"><span class="field__label">生效月数</span>
            <select class="input" data-card-flow-field="months">
              ${[1, 2, 3].map((month) => `<option value="${month}" ${Number(form.months) === month ? "selected" : ""}>${month} 个月</option>`).join("")}
            </select></label>`
        : ""}`;
  }
  if (flow === "cycle") {
    return `<p class="card-flow__q">设置本账单周期的消费限额：</p>
      ${cardFlowAmountField("账单周期消费限额（元）", "例如 8000")}`;
  }
  if (flow === "virtual") {
    return `<p class="card-flow__q">设置虚拟卡日限额：</p>
      ${cardFlowAmountField("日限额（元）", "例如 2000")}
      <p class="muted card-flow__note">虚拟卡与实体卡相互独立；与「补卡」不是同一件事。</p>`;
  }
  return "";
}

function cardFlowStepTwo() {
  const flow = cardFlowState.flow;
  const form = cardFlowState.form;
  if (flow === "replace") return cardFlowAddressFields();
  if (flow === "report") {
    return `${cardFlowConsequences()}
      <p class="card-flow__q">是否同时补卡？</p>
      ${cardFlowRadio("withReplace", [
        ["no", "只挂失，暂不补卡", "之后可在卡片上点「申请补卡」"],
        ["yes", "挂失并补卡", "换新卡号，旧卡作废"],
      ], form.withReplace)}
      ${form.withReplace === "yes" ? cardFlowAddressFields() : ""}`;
  }
  return cardFlowConsequences();
}

function cardFlowClientSummary() {
  const flow = cardFlowState.flow;
  const form = cardFlowState.form;
  const card = cardFlowState.card;
  const cents = cardFlowYuanToCents(form.amountYuan);
  const rows = [["卡片", `${card.type_label} · ${card.masked_no}`]];
  if (flow === "freeze") {
    rows.push(["类型", "临时冻结（口头挂失）"], ["时长", `${form.days} 天`], ["原因", form.lockReason]);
  } else if (flow === "report") {
    rows.push(["类型", "正式挂失"], ["原因", form.lossReason], ["是否补卡", form.withReplace === "yes" ? "是" : "否"]);
    if (form.withReplace === "yes") {
      rows.push(["地址", form.address], ["邮寄", form.mailMethod === "express" ? "加急 ¥20" : "普通 ¥10"]);
    }
  } else if (flow === "replace") {
    rows.push(["新卡号", "提交后生成"], ["地址", form.address], ["邮寄", form.mailMethod === "express" ? "加急 ¥20" : "普通 ¥10"]);
  } else if (flow === "unlock") {
    rows.push(["操作", "解挂"]);
  } else if (flow === "debit_limit") {
    rows.push(["通道", CARD_LIMIT_KIND_LABELS[form.limitKind] || form.limitKind], ["新限额", cents == null ? "未填写" : yuan(cents)]);
  } else if (flow === "credit_limit") {
    rows.push(["类型", form.creditMode === "temporary" ? `临时额度（${form.months} 个月）` : "固定额度（审批 3–5 个工作日）"], ["金额", cents == null ? "未填写" : yuan(cents)]);
  } else if (flow === "cycle") {
    rows.push(["账单周期消费限额", cents == null ? "未填写" : yuan(cents)]);
  } else if (flow === "virtual") {
    rows.push(["虚拟卡日限额", cents == null ? "未填写" : yuan(cents)]);
  }
  return `<dl class="card-flow__summary">${rows
    .map(([key, value]) => `<div><dt>${esc(key)}</dt><dd>${esc(value)}</dd></div>`).join("")}</dl>
    <p class="muted card-flow__note">点「生成确认单」后由服务端生成计划；红色动作会在本弹窗内下发一次性验证码。</p>`;
}

function cardFlowServerCard() {
  const plan = cardFlowState.plan;
  const rows = (plan.summary || [])
    .map((row) => `<div><dt>${esc(row.label)}</dt><dd>${esc(row.value)}</dd></div>`).join("");
  const lines = (plan.lines || []).map((line) => `<li>${esc(stripNo(line))}</li>`).join("");
  return `<div class="card-flow__plan">
    <div class="card-flow__plan-head">${sceneLevelChip(plan.level)}
      <span class="mono tech">cfhash ${esc(String(plan.frozen_hash || "").slice(0, 12))}</span></div>
    ${rows ? `<dl class="card-flow__summary">${rows}</dl>` : ""}
    ${lines ? `<ul class="card-flow__list">${lines}</ul>` : ""}
    <p class="muted card-flow__note">计划已冻结；确认后才会执行。</p>
    <p class="muted card-flow__note">卡片操作不涉及账户资金；账户余额只在转账、理财申赎、冻结/释放等资金动作后变化。</p>
    ${cardFlowBranchSwitch()}
  </div>`;
}

/* 对话生成的正式挂失计划：在操作上下文里给一次「改选临时冻结」的回退入口。 */
function cardFlowBranchSwitch() {
  const plan = cardFlowState.plan;
  const nodes = (plan && plan.nodes) || [];
  const action = nodes.length ? String(nodes[0].action || "") : "";
  const cardId = cardFlowState.card ? cardFlowState.card.card_id : "";
  if (!cardId) return "";
  if (action === "card.report_lost") {
    return `<p class="card-flow__note">如果只是暂时找不到卡，可改选临时冻结（可解、免费、到期自动解挂）。</p>
      <div class="btn-row"><button class="btn btn--sm btn--warn" type="button"
        data-card-flow-switch="temp_lock" data-card-id="${esc(cardId)}">改选临时冻结</button></div>`;
  }
  if (action === "card.lock") {
    return `<p class="card-flow__note">如果卡片确认找不回，可改选正式挂失（不可逆，需补卡）。</p>
      <div class="btn-row"><button class="btn btn--sm btn--danger" type="button"
        data-card-flow-switch="report_lost" data-card-id="${esc(cardId)}">改选正式挂失</button></div>`;
  }
  return "";
}

function cardFlowChallenge() {
  const challenge = cardFlowState.challenge;
  if (!challenge) {
    return `<div class="card-flow__verify">
      <p><b>需要强验证</b>：红色动作必须在本弹窗内完成一次性验证码。</p>
      <p class="muted">演示验证码，非真实人脸 / 短信 / U 盾。</p>
    </div>`;
  }
  return `<div class="card-flow__verify">
    <p><b>验证码已下发</b> · 通道 ${esc(challenge.channel || "—")} → ${esc(challenge.target || "—")}</p>
    <p class="muted">有效期 ${esc(String(challenge.expires_in_s || "—"))} 秒${challenge.code ? ` · 演示码 ${esc(challenge.code)}` : ""}</p>
    <label class="field"><span class="field__label">输入一次性验证码</span>
      <input class="input" id="card-flow-code" type="text" inputmode="numeric" autocomplete="one-time-code" /></label>
  </div>`;
}

function renderCardFlow() {
  const dialog = el("card-flow-dialog");
  if (!dialog) return;
  const flow = cardFlowState.flow;
  const total = cardFlowTotalSteps();
  const step = Math.min(Math.max(cardFlowState.step, 1), total);
  cardFlowState.step = step;
  const title = el("card-flow-title");
  const sub = el("card-flow-sub");
  if (title) title.textContent = flow ? (CARD_FLOW_TITLES[flow] || "卡片操作") : "组合操作确认";
  if (sub) {
    sub.textContent = cardFlowState.card
      ? `${cardFlowState.card.type_label} · ${cardFlowState.card.masked_no || "卡号未知"} · ${cardFlowState.card.status_label}`
      : "";
  }
  const stepsNode = el("card-flow-steps");
  if (stepsNode) {
    stepsNode.innerHTML = (CARD_FLOW_STEPS[flow] || []).map((label, index) => {
      const no = index + 1;
      const cls = no === step ? "is-active" : no < step ? "is-done" : "";
      return `<li class="${cls}"><span>${no}</span>${esc(label)}</li>`;
    }).join("");
  }
  const body = el("card-flow-body");
  if (body) {
    if (cardFlowState.result) {
      body.innerHTML = `<div class="card-flow__result" role="status">
        <p><b>${esc(cardFlowState.result.message || "操作已完成")}</b></p>
        ${(cardFlowState.result.items || []).length
          ? `<ul class="card-flow__list">${cardFlowState.result.items.map((item) => `<li>${esc(item)}</li>`).join("")}</ul>`
          : ""}
        <p class="muted">回执已记录；卡状态已刷新。</p>
        <p class="muted">卡片操作不涉及账户资金。</p></div>`;
    } else if (cardFlowState.mode === "detail") {
      body.innerHTML = cardFlowDetailHtml();
    } else if (cardFlowState.mode === "plan" || cardFlowState.plan) {
      body.innerHTML = cardFlowServerCard()
        + (cardFlowState.plan.level === "RED" ? cardFlowChallenge() : "");
    } else if (step === 1) {
      body.innerHTML = cardFlowStepOne();
    } else if (step === 2) {
      body.innerHTML = cardFlowStepTwo();
    } else {
      body.innerHTML = cardFlowClientSummary();
    }
  }
  const back = el("card-flow-back");
  const next = el("card-flow-next");
  if (back) {
    back.textContent = step > 1 && cardFlowState.mode !== "plan" && !cardFlowState.result ? "上一步" : "关闭";
    back.hidden = false;
  }
  if (next) {
    next.hidden = Boolean(cardFlowState.result) || cardFlowState.mode === "detail";
    next.disabled = cardFlowState.busy;
    if (cardFlowState.mode === "plan" || cardFlowState.plan) {
      const red = cardFlowState.plan && cardFlowState.plan.level === "RED";
      next.textContent = red && !cardFlowState.challenge
        ? "获取验证码"
        : cardFlowConfirmLabel();
    } else if (step < total) {
      next.textContent = "下一步";
    } else {
      next.textContent = "生成确认单";
    }
  }
  cardFlowError("");
}

function cardFlowDetailHtml() {
  const card = cardFlowState.card;
  const lock = card.temp_lock || {};
  const lost = card.lost || {};
  const replacement = card.replacement || null;
  if (cardFlowState.flow === "lock_detail") {
    return `<dl class="card-flow__summary">
      <div><dt>状态</dt><dd>${esc(card.status_label)}</dd></div>
      <div><dt>冻结原因</dt><dd>${esc(lock.reason_label || "未说明")}</dd></div>
      <div><dt>到期日</dt><dd>${esc(lock.until || "未提供")}</dd></div>
      <div><dt>剩余天数</dt><dd>${esc(String(lock.days_left == null ? "—" : lock.days_left))} 天</dd></div>
      <div><dt>解挂</dt><dd>有效期内容可线上解挂；到期自动解挂</dd></div>
    </dl>`;
  }
  return `<dl class="card-flow__summary">
    <div><dt>状态</dt><dd>${esc(card.status_label)}</dd></div>
    <div><dt>挂失日期</dt><dd>${esc(lost.at || "未提供")}</dd></div>
    <div><dt>挂失原因</dt><dd>${esc(lost.reason_label || "未说明")}</dd></div>
    <div><dt>是否补卡</dt><dd>${replacement ? `已补卡，新卡尾号 ${esc(replacement.new_card_tail || "未知")}` : "尚未补卡"}</dd></div>
    <div><dt>解挂</dt><dd>${replacement && !lost.at ? "旧卡已停用，新卡可直接使用" : "不可线上解挂；继续用卡请申请补卡"}</dd></div>
  </dl>`;
}

function cardFlowConfirmLabel() {
  const flow = cardFlowState.flow;
  if (!flow) return "确认执行";
  if (flow === "freeze") return "确认冻结";
  if (flow === "report") return cardFlowState.form.withReplace === "yes" ? "确认挂失并补卡" : "确认挂失";
  if (flow === "replace") return "确认补卡";
  if (flow === "unlock") return "确认解挂";
  if (flow === "credit_limit") return "确认调整额度";
  if (flow === "cycle") return "确认设置限额";
  if (flow === "debit_limit") return "确认调整限额";
  if (flow === "virtual") return "确认申请";
  return "确认";
}

function cardFlowSubmission() {
  const flow = cardFlowState.flow;
  const form = cardFlowState.form;
  const card = cardFlowState.card;
  const cardId = card.card_id;
  if (flow === "freeze") {
    return {
      utterance: `临时冻结这张卡 ${form.days} 天`,
      selected: { card: cardId, lock_days: Number(form.days), lock_reason: form.lockReason },
    };
  }
  if (flow === "report") {
    if (form.withReplace === "yes") {
      return {
        utterance: "申请补卡，同时正式挂失",
        selected: {
          card: cardId, address: form.address, mail_method: form.mailMethod,
          replace: "yes", loss_reason: form.lossReason,
        },
      };
    }
    return {
      utterance: "正式挂失这张卡",
      selected: { card: cardId, loss_type: "formal", loss_reason: form.lossReason },
    };
  }
  if (flow === "replace") {
    return {
      utterance: "申请补卡",
      selected: {
        card: cardId, address: form.address, mail_method: form.mailMethod,
        replace: form.withReplace, loss_reason: form.lossReason,
      },
    };
  }
  if (flow === "unlock") {
    return { utterance: "解挂这张卡", selected: { card: cardId } };
  }
  const cents = cardFlowYuanToCents(form.amountYuan);
  if (cents == null) return { error: "请填写正整数额度（元）" };
  if (flow === "debit_limit") {
    return {
      utterance: `把借记卡${CARD_LIMIT_KIND_LABELS[form.limitKind] || "消费"}限额调到 ${form.amountYuan} 元`,
      selected: { card: cardId, limit_kind: form.limitKind, daily_limit: cents },
    };
  }
  if (flow === "credit_limit") {
    return {
      utterance: form.creditMode === "temporary"
        ? `申请信用卡临时额度 ${form.amountYuan} 元`
        : `申请提高信用卡固定额度 ${form.amountYuan} 元`,
      selected: {
        card: cardId, credit_mode: form.creditMode, amount: cents,
        duration_months: Number(form.months),
      },
    };
  }
  if (flow === "cycle") {
    return {
      utterance: `把信用卡账单周期消费限额设为 ${form.amountYuan} 元`,
      selected: { card: cardId, amount: cents },
    };
  }
  if (flow === "virtual") {
    return { utterance: `申请虚拟卡，额度 ${form.amountYuan} 元`, selected: { daily_limit: cents } };
  }
  return { error: "未识别的操作" };
}

async function submitCardFlow() {
  const submission = cardFlowSubmission();
  if (submission.error) {
    cardFlowError(submission.error);
    return;
  }
  cardFlowState.busy = true;
  renderCardFlow();
  let result;
  try {
    result = await post("/api/submit", {
      utterance: submission.utterance, selected: submission.selected, new_intent: true,
    });
  } catch (error) {
    cardFlowState.busy = false;
    renderCardFlow();
    cardFlowError(fetchErrorMessage(error, "提交失败，请稍后重试。"));
    return;
  }
  cardFlowState.busy = false;
  if (result.status !== 200) {
    renderCardFlow();
    cardFlowError(String((result.data && result.data.detail) || "服务端拒绝了这次请求"));
    return;
  }
  const data = result.data || {};
  if (data.state !== "awaiting_confirm" || !data.card) {
    renderCardFlow();
    if (data.state === "needs_clarification" && data.clarifications?.length) {
      cardFlowError(`${data.message} 若缺失项不在本弹窗内，请回到对话补充后再提交。`);
      return;
    }
    cardFlowError(data.message || "这次操作没有生成可执行计划，请检查填写内容。");
    return;
  }
  cardFlowState.plan = data.card;
  cardFlowState.step = cardFlowTotalSteps();
  renderCardFlow();
  if (data.card.level === "GREEN") await confirmCardFlow();
}

async function requestCardVerification() {
  cardFlowState.busy = true;
  renderCardFlow();
  let result;
  try {
    result = await post("/api/verify/request", {});
  } catch (error) {
    cardFlowState.busy = false;
    renderCardFlow();
    cardFlowError(fetchErrorMessage(error, "验证码申请失败。"));
    return;
  }
  cardFlowState.busy = false;
  if (result.status !== 200) {
    renderCardFlow();
    cardFlowError(String((result.data && result.data.detail) || "验证码申请被拒绝"));
    return;
  }
  cardFlowState.challenge = result.data;
  renderCardFlow();
}

async function confirmCardFlow() {
  const plan = cardFlowState.plan;
  if (!plan) return;
  const codeEl = el("card-flow-code");
  const code = codeEl ? codeEl.value.trim() : "";
  if (plan.level === "RED" && !code) {
    cardFlowError("请输入一次性验证码");
    return;
  }
  cardFlowState.busy = true;
  renderCardFlow();
  let result;
  try {
    result = await post("/api/confirm", {
      card_id: plan.card_id,
      frozen_hash: plan.frozen_hash,
      verification_code: code || undefined,
    });
  } catch (error) {
    cardFlowState.busy = false;
    renderCardFlow();
    cardFlowError(fetchErrorMessage(error, "确认失败，请稍后重试。"));
    return;
  }
  cardFlowState.busy = false;
  if (result.status !== 200) {
    renderCardFlow();
    cardFlowError(String((result.data && result.data.detail) || "确认被拒绝"));
    return;
  }
  const data = result.data || {};
  const fromChat = cardFlowState.mode === "plan";
  cardFlowState.result = {
    message: data.message || "操作已完成",
    items: (data.results || []).map((item) => resultMessage(item)),
  };
  cardFlowState.plan = null;
  cardFlowState.challenge = null;
  renderCardFlow();
  await refreshCards();
  if (fromChat) {
    /* 对话里打开的计划卡：把执行回执同步回对话流，避免那边仍显示「待确认」。 */
    applyExecution(data);
    await refreshAfterExecution();
  }
  if (typeof refreshHomeBalance === "function") refreshHomeBalance().catch(() => {});
}

async function cardFlowNext() {
  if (cardFlowState.busy) return;
  const total = cardFlowTotalSteps();
  if (cardFlowState.plan) {
    if (cardFlowState.plan.level === "RED" && !cardFlowState.challenge) {
      await requestCardVerification();
      return;
    }
    await confirmCardFlow();
    return;
  }
  if (cardFlowState.step < total) {
    if (cardFlowState.step === 1) {
      const submission = cardFlowSubmission();
      if (submission.error) {
        cardFlowError(submission.error);
        return;
      }
    }
    cardFlowState.step += 1;
    renderCardFlow();
    return;
  }
  await submitCardFlow();
}

function cardFlowBack() {
  if (cardFlowState.result) {
    closeCardFlow();
    return;
  }
  if (cardFlowState.step > 1 && cardFlowState.mode !== "plan") {
    cardFlowState.step -= 1;
    renderCardFlow();
    return;
  }
  closeCardFlow();
}

(function bindCardFlow() {
  const dialog = el("card-flow-dialog");
  if (!dialog) return;
  dialog.addEventListener("click", (event) => {
    const target = event.target;
    if (!(target instanceof Element)) return;
    if (target.closest("[data-card-flow-close]")) {
      closeCardFlow();
      return;
    }
    if (target.closest("#card-flow-back")) {
      cardFlowBack();
      return;
    }
    if (target.closest("#card-flow-next")) {
      action(cardFlowNext)();
      return;
    }
    const switchButton = target.closest("[data-card-flow-switch]");
    if (switchButton) {
      const kind = switchButton.dataset.cardFlowSwitch;
      const cardId = switchButton.dataset.cardId;
      closeCardFlow();
      if (kind && cardId) openCardFlow(kind, cardId);
    }
  });
  dialog.addEventListener("change", (event) => {
    const target = event.target;
    if (!(target instanceof HTMLInputElement || target instanceof HTMLSelectElement)) return;
    const field = target.dataset.cardFlowField;
    if (!field) return;
    if (target instanceof HTMLInputElement && target.type === "radio" && !target.checked) return;
    if (field === "__type") {
      cardFlowState.flow = target.value === "formal" ? "report" : "freeze";
      cardFlowState.step = 1;
      renderCardFlow();
      return;
    }
    cardFlowState.form[field] = target.value;
    renderCardFlow();
  });
  dialog.addEventListener("input", (event) => {
    const target = event.target;
    if (!(target instanceof HTMLInputElement)) return;
    if (!target.dataset.cardFlowLive) return;
    cardFlowState.form[target.dataset.cardFlowField] = target.value;
  });
})();

/* 关闭扣款协议：卡片按钮 → 确认弹窗 → 页内红色计划卡 → 一次性验证码 →
   服务端写解约日期并返回对账回执。 */
function openSubCancelDialog(button) {
  const dialog = el("subscription-cancel-dialog");
  if (!dialog || !button) return;
  const data = button.dataset;
  dialog.dataset.subId = data.subCancel || "";
  const body = el("subscription-cancel-body");
  const total = data.subTotal
    ? `已扣 ${esc(data.subCount || "0")} 期 ${yuan(Number(data.subTotal))}`
    : "累计扣款数据不足";
  if (body) {
    body.innerHTML = `<p class="sub-cancel-dialog__merchant">${esc(data.subMerchant || "该代扣协议")}</p>
      <p class="sub-cancel-dialog__amount">${esc(data.subAmount || "—")} / ${esc(data.subCycle || "周期未知")}</p>
      <p class="muted sub-cancel-dialog__meta">${esc(data.subType || "类型未知")} ·
        协议 ${esc(data.subNo || "未匹配")} · ${esc(data.subSigned || "签约时间未知")} 签约</p>
      <p class="muted sub-cancel-dialog__meta">${total} · 最近扣款 ${esc(data.subLast || "未知")}
        · 下次扣款 ${esc(data.subNext || "未知")}（推算）</p>
      <p class="muted sub-cancel-dialog__note">关闭后这张卡不再向该商户放款；
        商户是否继续服务、是否停止计费由商户决定。已产生的扣款不会自动退回；
        关闭后如仍被扣款，可在本页核对并发起追回申请。</p>`;
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

/* 关闭扣款协议：卡片按钮 → 中文指令 + 协议 id 控制槽 → 页内红色计划卡。
   服务端写解约日期；回执与解约后对账由 /api/confirm 返回。 */
async function subscriptionPanelTask(subscriptionId) {
  const planHost = el("subscription-plan");
  if (planHost) planHost.hidden = false;
  const planStatus = el("subscription-plan-status");
  if (planStatus) planStatus.hidden = false;
  await sceneRunTask({
    utterance: "关闭扣款协议",
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
  "auto-renewal": "自动续费",
  "recurring-subscription": "周期订阅",
  "abnormal-spend": "异常大额",
  "late-night": "深夜交易",
  "remote-city": "异地交易",
};
const LEAD_STATE_LABELS = { normal: "确认正常", abnormal: "标记异常", ignored: "已忽略" };
const LEAD_STATE_ACTION_LABELS = { normal: "确认正常", abnormal: "标记异常", ignored: "忽略" };

function confidencePct(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "—";
  return `${Math.round(number * 100)}%`;
}

function billTierLabel(tier) {
  return { key: "高置信", more: "可继续核对", resolved: "已核对", hidden: "未展示" }[tier] || tier;
}

function findingEvidenceHtml(item) {
  const rows = (item.evidence || []).map((evidence) => {
    const moment = evidence.booked_at ? String(evidence.booked_at).slice(11, 16) : "";
    const recordNo = evidence.record_no || evidence.txn_id || "";
    return `<li>${esc(evidence.booked_on)}${moment ? " " + esc(moment) : ""} · ${esc(evidence.city || "地点未知")} · ${esc(evidence.merchant_label)} · ${yuan(evidence.amount_cents)}${recordNo ? ` · <span class="tech">${esc(recordNo)}</span>` : ""}</li>`;
  }).join("");
  return rows ? `<ul class="evidence">${rows}</ul>` : "";
}

function leadBasisHtml(lead) {
  const rows = (lead.basis || []).map((basis) => `<li>
    <p><b>${esc(FINDING_LABELS[basis.kind] || basis.label || basis.kind)}</b>
      <span class="muted">置信度 ${confidencePct(basis.confidence)}</span></p>
    <p class="muted">规则：${esc(basis.rule || "—")}</p>
    <p class="muted">阈值：${esc(basis.threshold || "—")}</p>
    <p>${esc(basis.detail || "")}</p>
    <p class="muted">影响口径：${esc(basis.impact_basis || "—")}</p>
    <p class="muted">${esc(basis.assumption || "")}</p>
  </li>`).join("");
  return rows ? `<ul class="lead-basis">${rows}</ul>` : "";
}

function leadActionsHtml(lead) {
  const id = esc(lead.lead_id || "");
  const period = esc(lead.period || "");
  const txn = esc((lead.txn_ids || [])[0] || "");
  const states = Object.keys(LEAD_STATE_ACTION_LABELS)
    .map((state) => `<button class="btn btn--sm btn--ghost" type="button"
      data-lead-state="${state}" data-lead-id="${id}" data-lead-period="${period}">${LEAD_STATE_ACTION_LABELS[state]}</button>`)
    .join("");
  const evidence = lead.evidence || [];
  const merchant = evidence.length ? evidence[evidence.length - 1].merchant_label : "";
  const canJump = merchant && (lead.kinds || []).some((kind) =>
    ["hidden-fee", "auto-renewal", "recurring-subscription"].includes(kind));
  const jump = canJump
    ? `<button class="btn btn--sm btn--ghost" type="button" dir="auto"
      data-nav="scene" data-scene="subscriptions"
      data-sub-focus="${esc(merchant)}">查看代扣协议</button>`
    : "";
  return `<div class="btn-row lead__actions">
    ${states}
    <button class="btn btn--sm btn--ghost" type="button" data-lead-txn="${txn}">查看交易详情</button>
    ${jump}
    <button class="btn btn--sm" type="button" data-lead-ask="${id}">问 AI</button>
  </div>`;
}

function leadHtml(lead) {
  const labels = (lead.labels || []).map((label) => `<span class="bills-chip bills-chip--tag">${esc(label)}</span>`).join(" ");
  const state = lead.state ? ` · ${esc(lead.state_label || LEAD_STATE_LABELS[lead.state] || lead.state)}` : "";
  return `<details class="lead lead--${esc(lead.tier || "more")}">
    <summary>
      <span class="lead__labels">${labels}</span>
      <span class="lead__amt">${yuan(Number(lead.impact_cents) || 0)}</span>
      <span class="bills-chip bills-chip--${esc(lead.status || "suspected")}">${confidencePct(lead.confidence)}${state}</span>
    </summary>
    <p class="muted lead__meta">${esc(billTierLabel(lead.tier))}</p>
    ${leadBasisHtml(lead)}
    ${findingEvidenceHtml(lead)}
    <p class="muted finding__suggestion">建议：${esc(lead.suggestion || "请核对交易明细后自行决定。")}</p>
    ${leadActionsHtml(lead)}
  </details>`;
}

/* 识别线索：账单页与可疑清单共用同一份 `leads` 与同一套分层/操作。 */
function leadsBlockHtml(payload) {
  const leads = payload.leads || [];
  const key = leads.filter((lead) => lead.tier === "key");
  const more = leads.filter((lead) => lead.tier === "more");
  const resolved = leads.filter((lead) => lead.tier === "resolved");
  const empty = !key.length && !more.length
    ? '<p class="muted">本期没有待核对的风险线索。识别结果仅供核对，不作最终结论。</p>'
    : "";
  return `<section class="bills-leads-block">
    <div class="scene-head">
      <p class="panel__title panel__title--sub">重点核对（${key.length}）</p>
      <span class="muted">置信度 ≥70% 且金额 ≥¥5 才进入这里；标记异常不受此限</span>
    </div>
    ${key.length ? key.map(leadHtml).join("") : empty}
    ${more.length ? `<details class="bills-more bills-more--inline">
      <summary>更多线索（${more.length} 条 · 50%–70%）</summary>
      ${more.map(leadHtml).join("")}
    </details>` : ""}
    ${resolved.length ? `<details class="bills-more bills-more--inline bills-resolved">
      <summary>已核对（${resolved.length} 条 · 已归档，不再重复提示）</summary>
      ${resolved.map(leadHtml).join("")}
    </details>` : ""}
  </section>`;
}

function billsFindingsHtml(payload) {
  return leadsBlockHtml(payload);
}

function billsTableHtml(payload) {
  const rows = (payload.transactions || []).map((item) => {
    const moment = item.booked_at ? String(item.booked_at).slice(11, 16) : "";
    const highlight = state.billsHighlightTxn && item.txn_id === state.billsHighlightTxn
      ? ' class="is-highlight"'
      : "";
    return `<tr${highlight}><td>${esc(item.booked_on)}${moment ? " " + esc(moment) : ""}</td><td>${esc(item.merchant_label)}</td><td>${esc(item.category_l2 || item.category)} <span class="muted">${esc(item.category)}</span></td><td>${esc(item.mcc || "—")}</td><td>${esc(item.city || "—")}</td><td>${yuan(item.amount_cents)}</td></tr>`;
  }).join("");
  return `<table class="mini-table"><thead><tr><th>时间</th><th>商户</th><th>二级分类</th><th>MCC</th><th>地点</th><th>金额</th></tr></thead><tbody>${rows || '<tr><td colspan="6">本期无消费流水</td></tr>'}</tbody></table>`;
}

/* 转账 / 还款单列：不进入消费画像，但仍可回读。 */
function billsTransfersHtml(payload) {
  const rows = (payload.transfers || []).map((item) => {
    const moment = item.booked_at ? String(item.booked_at).slice(11, 16) : "";
    return `<tr><td>${esc(item.booked_on)}${moment ? " " + esc(moment) : ""}</td><td>${esc(item.merchant_label)}</td><td>${esc(item.category)}</td><td>${esc(item.city || "—")}</td><td>${yuan(Math.abs(Number(item.amount_cents) || 0))}</td></tr>`;
  }).join("");
  if (!rows) return '<p class="muted">本期没有转账 / 还款等非消费资金流出。</p>';
  return `<table class="mini-table"><thead><tr><th>时间</th><th>对方 / 商户</th><th>类型</th><th>地点</th><th>金额</th></tr></thead><tbody>${rows}</tbody></table>`;
}

/* 本期概览：消费 / 收入 / 净现金流 / 待核对，全部来自 /api/bills/analysis.summary */
function billsMetricsHtml(summary) {
  const item = summary || {};
  const consumption = Number(item.consumption_cents ?? item.expense_total_cents) || 0;
  const income = Number(item.income_cents) || 0;
  const net = Number(item.net_cashflow_cents) || 0;
  const pending = Number(item.lead_count) || 0;
  const key = Number(item.key_lead_count) || 0;
  const transfer = Number(item.transfer_count) || 0;
  const note = transfer
    ? `<p class="muted bills-metrics__note">另有 ${transfer} 笔转账 / 还款等 ${yuan(item.transfer_out_cents || 0)}，不计入消费。</p>`
    : "";
  return `<div class="stat-metrics stat-metrics--4">
    <div class="stat-metric"><span class="stat-metric__k">本期消费</span>
      <b class="stat-metric__v">${yuan(consumption)}</b>
      <span class="muted">${Number(item.consumption_count) || 0} 笔</span></div>
    <div class="stat-metric"><span class="stat-metric__k">本期收入</span>
      <b class="stat-metric__v">${yuan(income)}</b></div>
    <div class="stat-metric${net < 0 ? " stat-metric--over" : ""}"><span class="stat-metric__k">净现金流</span>
      <b class="stat-metric__v">${yuan(net)}</b></div>
    <div class="stat-metric${pending ? " stat-metric--warn" : ""}">
      <span class="stat-metric__k">待核对</span>
      <b class="stat-metric__v">${pending} 条</b>
      <span class="muted">其中高置信 ${key} 条</span></div>
  </div>${note}`;
}

function billsBudgetHtml(payload) {
  const budget = payload.budget || {};
  const options = payload.category_options || [];
  const l1 = Array.from(new Set(options.map((item) => item.category_l1)));
  const categoryBudgets = {};
  (budget.categories || []).forEach((item) => {
    categoryBudgets[item.category] = Number(item.budget_cents) || 0;
  });
  const form = `<details class="budget-set budget-set--block" ${budget.needs_setup ? "open" : ""}>
    <summary>本月预算${budget.needs_setup ? " · 尚未设置" : ""}</summary>
    <div class="budget-set__row">
      <label class="budget-set__label" for="budget-input">总额（元）</label>
      <input class="input input--amount" id="budget-input" type="number" min="0" step="1"
             placeholder="如 8000" inputmode="decimal"
             value="${budget.total_cents ? esc(String(budget.total_cents / 100)) : ""}" />
      <button class="btn btn--sm btn--primary" type="button" data-action="save-budget">保存</button>
      <span class="muted budget-set__status" id="budget-status">仅用于提醒，不是转账限额。</span>
    </div>
    <div class="budget-cats">
      ${l1.map((category) => `<label class="budget-cat">
        <span>${esc(category)}</span>
        <input class="input input--amount" type="number" min="0" step="1" inputmode="decimal"
               data-budget-category="${esc(category)}"
               placeholder="不设"
               value="${categoryBudgets[category] ? esc(String(categoryBudgets[category] / 100)) : ""}" />
      </label>`).join("")}
    </div>
    <p class="muted budget-set__hint">分类预算可选；超支时对应分类会高亮。</p>
  </details>`;
  if (budget.needs_setup || budget.total_cents == null) {
    return `<section class="budget-panel">${form}</section>`;
  }
  const total = Number(budget.total_cents) || 0;
  const spent = Number(budget.spent_cents) || 0;
  const pct = total > 0 ? Math.min(Math.round((spent / total) * 100), 100) : 0;
  const over = Boolean(budget.over_budget);
  const remaining = Number(budget.remaining_cents) || 0;
  const daily = budget.daily_available_cents;
  const catRows = (budget.categories || []).map((item) => {
    const catPct = item.budget_cents > 0
      ? Math.min(Math.round((item.spent_cents / item.budget_cents) * 100), 100)
      : 0;
    return `<li class="budget-cat-row${item.over ? " is-over" : ""}">
      <span class="budget-cat-row__k">${esc(item.category)}</span>
      <span class="bill__bar-track budget-cat-row__bar"><i style="width:${catPct}%"></i></span>
      <span class="budget-cat-row__v">${yuan(item.spent_cents)} / ${yuan(item.budget_cents)}</span>
      <span class="budget-cat-row__left">${item.over ? `超 ${yuan(Math.abs(item.remaining_cents))}` : `剩 ${yuan(item.remaining_cents)}`}</span>
    </li>`;
  }).join("");
  return `<section class="budget-panel">
    <div class="scene-head">
      <p class="panel__title panel__title--sub">本月预算</p>
      <span class="muted">${budget.days_left ? `还剩 ${budget.days_left} 天` : "账期已结束"}</span>
    </div>
    <div class="stat-metrics stat-metrics--3">
      <div class="stat-metric"><span class="stat-metric__k">已花</span><b class="stat-metric__v">${yuan(spent)}</b></div>
      <div class="stat-metric${over ? " stat-metric--over" : ""}"><span class="stat-metric__k">${over ? "已超支" : "剩余"}</span>
        <b class="stat-metric__v">${yuan(Math.abs(remaining))}</b></div>
      <div class="stat-metric"><span class="stat-metric__k">日均可用</span>
        <b class="stat-metric__v">${typeof daily === "number" ? yuan(daily) : "—"}</b></div>
    </div>
    <span class="bill__bar-track budget__bar${over ? " budget__bar--over" : ""}" role="img" aria-label="预算已用 ${pct}%"><i style="width:${pct}%"></i></span>
    ${catRows ? `<ul class="budget-cat-list">${catRows}</ul>` : ""}
    ${form}
  </section>`;
}

function billsMomHtml(payload, period) {
  const points = payload.trend || [];
  const point = points.find((item) => item.period === period);
  if (!point) return "";
  if (!point.mom) {
    return `<div class="bills-mom"><p class="muted">${esc(point.period)} 没有上一账期可比数据。</p></div>`;
  }
  const change = Number(point.mom.change_cents) || 0;
  const pct = point.mom.change_pct === null ? "—" : `${Math.abs(Number(point.mom.change_pct)).toFixed(1)}%`;
  const direction = change >= 0 ? "增加" : "减少";
  const contributions = (point.mom.contributions || []).slice(0, 5).map((item) => {
    const amount = Number(item.change_cents) || 0;
    return `<li><span>${esc(item.category)}</span><b class="${amount >= 0 ? "is-up" : "is-down"}">${amount >= 0 ? "+" : "-"}${yuan(Math.abs(amount))}</b></li>`;
  }).join("");
  return `<div class="bills-mom" data-mom-period="${esc(point.period)}">
    <p><b>${esc(point.period)}</b> 比 ${esc(point.mom.previous_period)} ${direction}
      <b class="${change >= 0 ? "is-up" : "is-down"}">${yuan(Math.abs(change))}</b>
      <span class="muted">（${change >= 0 ? "+" : "-"}${pct}）</span></p>
    ${contributions ? `<ul class="bills-mom__list">${contributions}</ul>` : '<p class="muted">各分类变化不大。</p>'}
  </div>`;
}

function billsChartsHtml(payload) {
  const share = (payload.category_share || [])
    .slice(0, 8)
    .map((item) => ({ label: item.category, value: item.expense_cents || 0, note: item.category_l1 || "" }));
  const trend = (payload.trend || []).map((point) => ({
    label: `${String(point.period || "").slice(5) || "—"}月`,
    value: point.expense_cents || 0,
    secondary: point.income_cents || 0,
    period: point.period || "",
  }));
  const last = (payload.trend || [])[(payload.trend || []).length - 1];
  const selected = state.billsTrendPeriod || (last ? last.period : "");
  return `<div class="scene-charts scene-charts--bills">
    <section>
      <p class="panel__title panel__title--sub">收支趋势（近 6 月）</p>
      ${lineChartHtml(trend, {
        label: "近 6 月收支趋势",
        empty: "暂无趋势数据。",
        formatValue: (cents) => `¥${Math.round((Number(cents) || 0) / 100).toLocaleString("zh-CN")}`,
        primaryLabel: "消费",
        secondaryLabel: "收入",
        clickable: true,
      })}
      <p class="muted chart-line__note">点趋势点查看该月分类贡献。</p>
      ${billsMomHtml(payload, selected)}
    </section>
    <section>
      <p class="panel__title panel__title--sub">分类占比（二级）</p>
      ${donutChartHtml(share, { label: "本期消费二级分类占比", empty: "本期无消费分类。" })}
    </section>
  </div>`;
}

function billsMerchantsHtml(payload) {
  const items = (payload.top_merchants || []).slice();
  if (!items.length) return '<p class="muted">本期没有可排行的商户消费。</p>';
  const sort = state.billsMerchantSort === "count" ? "count" : "amount";
  items.sort((a, b) => sort === "count"
    ? (Number(b.count) - Number(a.count)) || (Number(b.amount_cents) - Number(a.amount_cents))
    : (Number(b.amount_cents) - Number(a.amount_cents)) || (Number(b.count) - Number(a.count)));
  const rows = items.map((item, index) => `<li class="merchant-row">
    <span class="merchant-row__rank">${index + 1}</span>
    <span class="merchant-row__name">${esc(item.merchant_label)}
      <span class="muted">${esc(item.category_l2)} · MCC ${esc(item.mcc)}</span></span>
    <span class="merchant-row__count">${Number(item.count) || 0} 笔</span>
    <span class="merchant-row__amt">${yuan(item.amount_cents)}</span>
    <button class="btn btn--sm btn--ghost" type="button"
      data-bills-category-edit="${esc(item.merchant_key)}"
      data-merchant-label="${esc(item.merchant_label)}"
      data-merchant-category="${esc(item.category_l2)}">改分类</button>
  </li>`).join("");
  return `<section class="bills-merchants">
    <div class="scene-head">
      <p class="panel__title panel__title--sub">Top 商户</p>
      <div class="btn-row">
        <button class="btn btn--sm ${sort === "amount" ? "btn--primary" : "btn--ghost"}" type="button" data-merchant-sort="amount">按金额</button>
        <button class="btn btn--sm ${sort === "count" ? "btn--primary" : "btn--ghost"}" type="button" data-merchant-sort="count">按笔数</button>
      </div>
    </div>
    <ul class="merchant-list">${rows}</ul>
  </section>`;
}

function billsAutoRenewHtml(payload) {
  const rows = (payload.auto_renewals || []).map((item) => {
    const label = FINDING_LABELS[item.kind] || item.kind;
    const amount = Number(item.impact_cents) || 0;
    const action = item.kind === "auto-renewal"
      ? '<button class="btn btn--sm btn--ghost" type="button" data-nav="scene" data-scene="subscriptions">管理自动续费</button>'
      : '<button class="btn btn--sm btn--ghost" type="button" data-nav="scene" data-scene="subscriptions">查看订阅</button>';
    return `<li class="autorenew-row">
      <div><b>${esc(label)}</b> <span class="muted">${esc((item.evidence || [])[0] ? item.evidence[0].merchant_label : "")}</span></div>
      <p class="muted">${esc(item.detail || "")}</p>
      <div class="btn-row"><span class="muted">${amount > 0 ? `单期 ${yuan(amount)}` : ""}</span>${action}</div>
    </li>`;
  }).join("");
  return rows
    ? `<ul class="autorenew-list">${rows}</ul>`
    : '<p class="muted">本期未识别到未登记的自动续费；已登记订阅可在订阅页查看。</p>';
}

/* 结论行：优先服务端叙事摘要；`narrative` 缺失或降级（degraded=true）时回退本地确定性结论 */
function billsVerdictText(payload) {
  const narrative = payload.narrative || {};
  const summary = typeof narrative.summary === "string" ? narrative.summary.trim() : "";
  if (summary && !narrative.degraded) return summary;
  return billVerdict(payload.summary || {});
}

function billsConclusionHtml(payload) {
  const degraded = payload.narrative && payload.narrative.degraded
    ? '<p class="muted scene-hint">文字说明暂不可用，以下为明细数据。</p>'
    : "";
  return `<p class="bills-verdict">${esc(billsVerdictText(payload))}</p>
    ${billsMetricsHtml(payload.summary || {})}
    ${degraded}`;
}

function billsLoading() {
  const status = el("bills-status");
  if (status) status.textContent = "正在读取账单识别结果…";
  const skeleton = '<span class="skeleton"></span><span class="skeleton"></span>';
  ["bills-summary", "bills-budget", "bills-charts", "bills-merchants", "bills-leads"].forEach((id) => {
    const node = el(id);
    if (node) node.innerHTML = skeleton;
  });
  const table = el("bills-table");
  if (table) table.innerHTML = "";
}

function billsFailure(message) {
  ["bills-budget", "bills-charts", "bills-merchants", "bills-leads", "bills-autorenew", "bills-table", "bills-transfers-table"].forEach((id) => {
    const node = el(id);
    if (node) node.innerHTML = "";
  });
  panelFailure("bills-summary", message, "bills");
}

function billsEmpty(payload) {
  const card = `<article class="state-card">
    <p class="state-card__k">本期无消费流水</p>
    <p class="muted">${esc(payload.period)} 没有消费交易；转账/还款等不计入消费分析。</p>
  </article>`;
  el("bills-summary").innerHTML = card;
  el("bills-budget").innerHTML = billsBudgetHtml(payload);
  el("bills-charts").innerHTML = "";
  el("bills-merchants").innerHTML = "";
  el("bills-leads").innerHTML = '<p class="muted">本期没有需要核对的线索。</p>';
  el("bills-autorenew").innerHTML = "";
  el("bills-table").innerHTML = billsTableHtml(payload);
  el("bills-transfers-table").innerHTML = billsTransfersHtml(payload);
}

/* ---------------- 账期切换：本月（服务端当月）/ 上月 / 指定月份 ----------------
   「本月」不带参数，走服务端当月默认；「上月」「选月份」带 `?year=&month=`。
   切换只重拉 /api/bills/analysis 并重渲染结论区，不整页刷新。 */

function billsPreviousPeriod(ref) {
  return ref.month === 1
    ? { year: ref.year - 1, month: 12 }
    : { year: ref.year, month: ref.month - 1 };
}

/* 服务端「当月」：优先用返回的 server_month，其次 data_through，最后本地日期 */
function billsServerMonth() {
  const payload = state.bills;
  if (payload && typeof payload.server_month === "string") {
    const matched = /^(\d{4})-(\d{2})$/.exec(payload.server_month);
    if (matched) return { year: Number(matched[1]), month: Number(matched[2]) };
  }
  if (payload && payload.period_basis === "current_month" && typeof payload.data_through === "string") {
    const matched = /^(\d{4})-(\d{2})/.exec(payload.data_through);
    if (matched) return { year: Number(matched[1]), month: Number(matched[2]) };
  }
  const now = new Date();
  return { year: now.getFullYear(), month: now.getMonth() + 1 };
}

function billsMonthOptions() {
  const server = billsServerMonth();
  let year = server.year;
  let month = server.month;
  const options = [];
  for (let index = 0; index < 24; index += 1) {
    options.push(`${year}-${String(month).padStart(2, "0")}`);
    month -= 1;
    if (month === 0) {
      month = 12;
      year -= 1;
    }
  }
  return options;
}

/* 同一账期共用一个计数键，用来算「含今日新增 N 笔」的差值 */
function billsPeriodKey(period) {
  const now = billsServerMonth();
  if (period.year === now.year && period.month === now.month) return "current";
  return `${period.year}-${period.month}`;
}

function billsRequestQuery() {
  if (state.billsPeriodChoice === "previous") {
    const period = billsPreviousPeriod(billsServerMonth());
    return { key: billsPeriodKey(period), query: `?year=${period.year}&month=${period.month}` };
  }
  if (state.billsPeriodChoice === "explicit" && state.billsPeriod) {
    const period = state.billsPeriod;
    return { key: billsPeriodKey(period), query: `?year=${period.year}&month=${period.month}` };
  }
  return { key: "current", query: "" };
}

function syncBillsPeriodControl() {
  document.querySelectorAll("#bills-period [data-bills-period]").forEach((button) => {
    const active = button.dataset.billsPeriod === state.billsPeriodChoice;
    button.classList.toggle("is-active", active);
    button.setAttribute("aria-pressed", active ? "true" : "false");
  });
  const pick = el("bills-period-month");
  if (!pick) return;
  const current = pick.value;
  const options = billsMonthOptions();
  const expected = ["", ...options].join("|");
  if (pick.dataset.options !== expected) {
    pick.innerHTML = '<option value="">请选择年月</option>' + options.map((value) => {
      const [year, month] = value.split("-");
      return `<option value="${value}">${year} 年 ${Number(month)} 月</option>`;
    }).join("");
    pick.dataset.options = expected;
  }
  pick.value = state.billsPeriodChoice === "explicit" && state.billsPeriod
    ? `${state.billsPeriod.year}-${String(state.billsPeriod.month).padStart(2, "0")}`
    : "";
  if (pick.value !== current && document.activeElement === pick) pick.blur();
}

/* 本月只显示「含今日新增 N 笔」；显式月份才显示截至日，避免同一语义写两遍。 */
function renderBillsPeriodHint(payload, count, previousCount) {
  const hint = el("bills-period-hint");
  if (!hint) return;
  const through = typeof payload.data_through === "string" ? payload.data_through : "";
  const parts = [];
  if (payload.period_basis === "current_month"
      && typeof previousCount === "number"
      && count > previousCount) {
    parts.push(`含今日新增 ${count - previousCount} 笔`);
  }
  if (payload.period_basis === "explicit" && /^\d{4}-\d{2}-\d{2}$/.test(through)) {
    parts.push(`截至 ${through.slice(5)}`);
  }
  hint.textContent = parts.join(" · ");
}

/* 记住最近一次账单回答的账期（形如 "2019-01"），供场景页按同一账期取数；
   问答落在服务端当月时仍走「本月」缺省，保留「含今日新增」的口径。 */
function rememberBillsPeriod(findings) {
  const period = findings && typeof findings.period === "string" ? findings.period : "";
  const matched = /^(\d{4})-(\d{2})$/.exec(period);
  const now = billsServerMonth();
  const isCurrent = matched
    && Number(matched[1]) === now.year
    && Number(matched[2]) === now.month;
  state.billsPeriod = matched && !isCurrent
    ? { year: Number(matched[1]), month: Number(matched[2]) }
    : null;
  state.billsPeriodChoice = state.billsPeriod ? "explicit" : "current";
  syncBillsPeriodControl();
}

async function refreshBills() {
  if (state.offline) return;
  billsLoading();
  syncBillsPeriodControl();
  const request = billsRequestQuery();
  let result;
  try {
    result = await apiFetch("GET", `/api/bills/analysis${request.query}`);
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
  const count = Number(summary.consumption_count ?? summary.transaction_count) || 0;
  const transfer = Number(summary.transfer_count) || 0;
  const pending = Number(summary.lead_count) || 0;
  const previousCount = state.billsPeriodCounts[request.key];
  state.billsPeriodCounts[request.key] = count;
  el("bills-status").textContent = [
    payload.period,
    `消费 ${count} 笔`,
    transfer ? `转账 ${transfer} 笔` : "",
    `待核对 ${pending} 条`,
  ].filter(Boolean).join(" · ");
  renderBillsPeriodHint(payload, count, previousCount);
  const demoBanner = el("bills-demo-banner");
  if (demoBanner) demoBanner.hidden = !payload.data_source_is_demo;
  const disclaimer = el("bills-disclaimer");
  if (disclaimer) {
    disclaimer.hidden = false;
    disclaimer.textContent = "识别结果供核对，不作最终结论。";
  }
  if (!(payload.transactions || []).length) {
    billsEmpty(payload);
    return;
  }
  const trend = payload.trend || [];
  const periods = trend.map((point) => point.period);
  if (!periods.includes(state.billsTrendPeriod)) {
    state.billsTrendPeriod = periods.length ? periods[periods.length - 1] : "";
  }
  el("bills-summary").innerHTML = billsConclusionHtml(payload);
  el("bills-budget").innerHTML = billsBudgetHtml(payload);
  el("bills-charts").innerHTML = billsChartsHtml(payload);
  el("bills-merchants").innerHTML = billsMerchantsHtml(payload);
  el("bills-leads").innerHTML = billsFindingsHtml(payload);
  el("bills-autorenew").innerHTML = billsAutoRenewHtml(payload);
  el("bills-table").innerHTML = billsTableHtml(payload);
  el("bills-transfers-table").innerHTML = billsTransfersHtml(payload);
}

function billsLeadById(leadId) {
  return (state.bills && state.bills.leads ? state.bills.leads : [])
    .find((lead) => lead.lead_id === leadId) || null;
}

async function setBillLeadState(leadId, period, leadState) {
  const status = el("bills-status");
  if (status) status.textContent = "正在记录核对结果…";
  const result = await post(`/api/bills/leads/${encodeURIComponent(leadId)}`, {
    period,
    state: leadState,
  });
  if (!result.ok) {
    if (status) status.textContent = (result.data && result.data.detail) || "记录失败，请稍后重试。";
    return;
  }
  await refreshBills();
  /* 可疑清单是账单页的同源视图：核对后同步重拉，避免两页状态分裂。 */
  const recordsView = el("view-records");
  if (currentRecordTab === "suspicions" && recordsView && !recordsView.hidden) {
    await renderRecordsTab("suspicions");
  }
}

function askBillLead(leadId) {
  const lead = billsLeadById(leadId);
  if (!lead) return;
  const evidence = (lead.evidence || [])[0] || {};
  const moment = evidence.booked_at ? String(evidence.booked_at).slice(0, 16).replace("T", " ") : evidence.booked_on || "";
  const basis = (lead.basis || []).map((item) => item.rule).filter(Boolean).join("；");
  const amount = yuan(Number(lead.impact_cents) || 0);
  state.pendingBillLead = {
    lead_id: lead.lead_id,
    period: lead.period,
    merchant: evidence.merchant_label || "",
    amount_cents: lead.impact_cents,
    txn_ids: lead.txn_ids || [],
  };
  const input = el("utterance");
  if (input) {
    input.value = `账单里这条线索：${moment} ${evidence.merchant_label || ""} ${amount}，`
      + `标签「${(lead.labels || []).join("、")}」。系统依据：${basis || "—"}。`
      + "这笔为什么可疑？我该怎么核对？";
  }
  navigate("chat", {});
  if (input) window.setTimeout(() => input.focus(), 0);
}

function openBillCategoryDialog(button) {
  const dialog = el("bills-category-dialog");
  const select = el("bills-category-select");
  if (!dialog || !select) return;
  const options = (state.bills && state.bills.category_options) || [];
  const grouped = new Map();
  options.forEach((item) => {
    if (!grouped.has(item.category_l1)) grouped.set(item.category_l1, []);
    grouped.get(item.category_l1).push(item);
  });
  select.innerHTML = Array.from(grouped.entries()).map(([l1, rows]) =>
    `<optgroup label="${esc(l1)}">${rows.map((row) =>
      `<option value="${esc(row.category_l2)}">${esc(row.category_l2)} · MCC ${esc(row.mcc)}</option>`).join("")}</optgroup>`
  ).join("");
  select.value = button.dataset.merchantCategory || "";
  state.billCategoryTarget = {
    merchant_key: button.dataset.billsCategoryEdit || "",
    label: button.dataset.merchantLabel || "",
  };
  const name = el("bills-category-merchant");
  if (name) name.textContent = `商户：${button.dataset.merchantLabel || "—"}`;
  const hint = el("bills-category-hint");
  if (hint) hint.textContent = `MCC 建议归类：${button.dataset.merchantCategory || "—"}（演示映射，不含用户占比）。保存后会记住该商户的归类。`;
  if (typeof dialog.showModal === "function") dialog.showModal();
  else dialog.setAttribute("open", "");
}

function closeBillCategoryDialog() {
  const dialog = el("bills-category-dialog");
  if (!dialog) return;
  if (typeof dialog.close === "function") dialog.close();
  else dialog.removeAttribute("open");
}

async function saveBillCategory() {
  const select = el("bills-category-select");
  const target = state.billCategoryTarget;
  if (!select || !target) return;
  const result = await post("/api/bills/category-override", {
    merchant_key: target.merchant_key,
    category: select.value,
  });
  if (!result.ok) {
    const hint = el("bills-category-hint");
    if (hint) hint.textContent = (result.data && result.data.detail) || "保存失败，请稍后重试。";
    return;
  }
  closeBillCategoryDialog();
  await refreshBills();
}

function csvCell(value) {
  const text = String(value ?? "");
  return `"${text.replace(/"/g, '""')}"`;
}

function exportBillsCsv() {
  const payload = state.bills;
  if (!payload) return;
  const header = ["日期", "时间", "商户", "一级分类", "二级分类", "MCC", "金额(元)", "城市", "类型"];
  const rows = (payload.transactions || []).map((item) => {
    const bookedAt = item.booked_at ? String(item.booked_at) : "";
    return [
      item.booked_on || "",
      bookedAt.includes("T") ? bookedAt.slice(11, 16) : "",
      item.merchant_label || "",
      item.category || "",
      item.category_l2 || "",
      item.mcc || "",
      ((Number(item.amount_cents) || 0) / 100).toFixed(2),
      item.city || "",
      "消费",
    ];
  });
  (payload.transfers || []).forEach((item) => {
    const bookedAt = item.booked_at ? String(item.booked_at) : "";
    rows.push([
      item.booked_on || "",
      bookedAt.includes("T") ? bookedAt.slice(11, 16) : "",
      item.merchant_label || "",
      item.category || "转账",
      "",
      "",
      (Math.abs(Number(item.amount_cents) || 0) / 100).toFixed(2),
      item.city || "",
      "转账/还款（不计入消费）",
    ]);
  });
  const csv = "\uFEFF" + [header, ...rows].map((row) => row.map(csvCell).join(",")).join("\r\n");
  const blob = new Blob([csv], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = `账单-${payload.period || "本期"}.csv`;
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 0);
}

/* 「问 AI」的追问：在对话里按识别规则解释这条线索，不进入交易筛选计划。 */
function billLeadQuestionText(text) {
  if (!state.pendingBillLead) return false;
  return /(为什么|怎么|如何|可疑|解释|核对|怎么办|啥意思|原因)/.test(text);
}

async function submitBillLeadQuestion(text) {
  const context = state.pendingBillLead;
  if (!context) return;
  const lead = billsLeadById(context.lead_id) || {
    lead_id: context.lead_id,
    period: context.period,
    labels: [],
    confidence: "0",
    impact_cents: context.amount_cents,
    basis: [],
    suggestion: "",
    txn_ids: context.txn_ids || [],
  };
  beginTask(text);
  state.phase = "idle";
  el("utterance").value = "";
  const evidence = (lead.evidence || [])[0] || {};
  const moment = evidence.booked_at
    ? String(evidence.booked_at).slice(0, 16).replace("T", " ")
    : "";
  state.panel = `<div class="takeover">
    <p><b>这条线索为什么需要核对</b></p>
    <p class="muted">${esc(moment)} · ${esc(evidence.merchant_label || context.merchant || "")} · ${yuan(Number(lead.impact_cents) || 0)} · 置信度 ${confidencePct(lead.confidence)}</p>
    ${leadBasisHtml(lead)}
    ${findingEvidenceHtml(lead)}
    <p class="muted">建议：${esc(lead.suggestion || "先核对商户与用途；如非本人操作，再锁卡并联系银行。")}</p>
    <p class="muted">以上解释来自识别规则与流水事实，不是对交易的最终定性。</p>
    <div class="btn-row">
      ${["normal", "abnormal", "ignored"].map((item) => `<button class="btn btn--sm btn--ghost" type="button"
        data-chat-lead-state="${item}" data-chat-lead-id="${esc(lead.lead_id)}"
        data-chat-lead-period="${esc(lead.period || context.period)}">${LEAD_STATE_ACTION_LABELS[item]}</button>`).join("")}
      <button class="btn btn--sm btn--ghost" type="button" data-nav="scene" data-scene="bills">回到账单分析</button>
    </div>
  </div>`;
  renderStage();
  timelineAdd("已按识别规则解释该线索", "done");
}

async function applyChatLeadState(leadState) {
  const context = state.pendingBillLead;
  if (!context || !leadState) return;
  const result = await post(`/api/bills/leads/${encodeURIComponent(context.lead_id)}`, {
    period: context.period,
    state: leadState,
  });
  const message = result.ok
    ? `已把这条线索标记为「${LEAD_STATE_LABELS[leadState] || leadState}」，之后不再重复提示。`
    : (result.data && result.data.detail) || "记录失败，请稍后重试。";
  state.panel = `<div class="takeover">
    <p><b>${result.ok ? "已记录核对结果" : "未能记录"}</b></p>
    <p class="muted">${esc(message)}</p>
    <div class="btn-row">
      <button class="btn btn--sm btn--ghost" type="button" data-nav="scene" data-scene="bills">回到账单分析</button>
    </div>
  </div>`;
  renderStage();
  timelineAdd(message, result.ok ? "done" : "stop");
  if (result.ok) {
    state.pendingBillLead = null;
    refreshBills().catch(() => {});
  }
}

/* 用户对线索说「这笔是我转给朋友的」时，走确定性回写，不进入转账计划。 */
function billLeadResolutionState(text) {
  if (!state.pendingBillLead) return null;
  if (/\d+(\.\d+)?\s*(元|块)/.test(text)) return null;
  if (/(不是我|盗刷|异常|报警|冻结|被骗)/.test(text)) return "abnormal";
  if (/(忽略|不用管|别管)/.test(text)) return "ignored";
  if (/(转给|朋友|家人|本人|正常|我买的|我知道|还款)/.test(text)) return "normal";
  return null;
}

async function submitBillLeadResolution(text, leadState) {
  const lead = state.pendingBillLead;
  if (!lead) return;
  beginTask(text);
  state.phase = "idle";
  el("utterance").value = "";
  const result = await post("/api/bills/lead-resolution", {
    lead_id: lead.lead_id,
    period: lead.period,
    utterance: text,
  });
  const message = result.ok
    ? (result.data && result.data.message) || "已记录核对结果。"
    : (result.data && result.data.detail) || "记录失败，请稍后重试。";
  state.panel = `<div class="takeover">
    <p><b>${result.ok ? "已记录核对结果" : "未能记录"}</b></p>
    <p class="muted">${esc(message)}</p>
    <p class="muted">线索：${esc(lead.merchant || "")} · ${yuan(lead.amount_cents || 0)} · ${esc(lead.period || "")}</p>
    <div class="btn-row">
      <button class="btn btn--sm btn--ghost" type="button" data-nav="scene" data-scene="bills">回到账单分析</button>
    </div>
  </div>`;
  renderStage();
  timelineAdd(result.ok ? `线索已标记为${LEAD_STATE_LABELS[leadState] || leadState}` : message, result.ok ? "done" : "stop");
  state.pendingBillLead = null;
  if (result.ok) refreshBills().catch(() => {});
}

if (el("bills-period")) {
  el("bills-period").addEventListener("click", (event) => {
    const button = event.target.closest("[data-bills-period]");
    if (!button) return;
    const choice = button.dataset.billsPeriod === "previous" ? "previous" : "current";
    if (choice === state.billsPeriodChoice) return;
    state.billsPeriodChoice = choice;
    state.billsPeriod = null;
    syncBillsPeriodControl();
    refreshBills().catch(() => {});
  });
  const pick = el("bills-period-month");
  if (pick) {
    pick.addEventListener("change", () => {
      const matched = /^(\d{4})-(\d{2})$/.exec(pick.value);
      if (!matched) {
        state.billsPeriod = null;
        state.billsPeriodChoice = "current";
      } else {
        state.billsPeriod = { year: Number(matched[1]), month: Number(matched[2]) };
        state.billsPeriodChoice = "explicit";
      }
      syncBillsPeriodControl();
      refreshBills().catch(() => {});
    });
  }
  const panel = el("bills-panel");
  if (panel) {
    panel.addEventListener("click", (event) => {
      const sort = event.target.closest("[data-merchant-sort]");
      if (sort) {
        state.billsMerchantSort = sort.dataset.merchantSort === "count" ? "count" : "amount";
        if (state.bills) el("bills-merchants").innerHTML = billsMerchantsHtml(state.bills);
        return;
      }
      const edit = event.target.closest("[data-bills-category-edit]");
      if (edit) {
        openBillCategoryDialog(edit);
        return;
      }
      const leadState = event.target.closest("[data-lead-state]");
      if (leadState) {
        action(() => setBillLeadState(
          leadState.dataset.leadId || "",
          leadState.dataset.leadPeriod || "",
          leadState.dataset.leadState || "",
        ))();
        return;
      }
      const leadAsk = event.target.closest("[data-lead-ask]");
      if (leadAsk) {
        askBillLead(leadAsk.dataset.leadAsk || "");
        return;
      }
      const leadTxn = event.target.closest("[data-lead-txn]");
      if (leadTxn) {
        state.billsHighlightTxn = leadTxn.dataset.leadTxn || "";
        const details = el("bills-details");
        if (details) details.open = true;
        if (state.bills) el("bills-table").innerHTML = billsTableHtml(state.bills);
        const row = el("bills-table")?.querySelector("tr.is-highlight");
        if (row) row.scrollIntoView({ block: "center", behavior: "smooth" });
        return;
      }
      if (event.target.closest("[data-bills-export]")) {
        exportBillsCsv();
        return;
      }
      const point = event.target.closest("[data-chart-period]");
      if (point && state.bills) {
        state.billsTrendPeriod = point.dataset.chartPeriod || "";
        el("bills-charts").innerHTML = billsChartsHtml(state.bills);
      }
    });
    panel.addEventListener("keydown", (event) => {
      if (event.key !== "Enter" && event.key !== " ") return;
      const point = event.target.closest("[data-chart-period]");
      if (!point || !state.bills) return;
      event.preventDefault();
      state.billsTrendPeriod = point.dataset.chartPeriod || "";
      el("bills-charts").innerHTML = billsChartsHtml(state.bills);
    });
  }
  const dialog = el("bills-category-dialog");
  if (dialog) {
    dialog.addEventListener("click", (event) => {
      if (event.target.closest("[data-bills-dialog-close]")) {
        closeBillCategoryDialog();
        return;
      }
      if (event.target.closest("[data-bills-category-save]")) {
        action(() => saveBillCategory())();
      }
    });
  }
}

if (el("cards-list")) {
  el("cards-list").addEventListener("click", (event) => {
    const button = event.target.closest("[data-card-action]");
    if (!button) return;
    const kind = button.dataset.cardAction;
    const cardId = button.dataset.cardId;
    /* 所有卡片写操作都进弹窗：类型 / 原因 / 后果 / 补卡 / 验证码 / 二次确认 */
    action(async () => {
      openCardFlow(kind, cardId);
    })();
  });
}


function selectedObjects() {
  const selected = Object.assign({}, pendingSceneSelected);
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
  /* 转账 / 申赎等资金动作会立即扣款：执行后刷新首页余额卡，避免页面停在旧数字。 */
  if (typeof refreshHomeBalance === "function") {
    await refreshHomeBalance().catch(() => {});
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

/* ---------------- 转账核对单（计划卡之前的一层） ---------------- */

function reviewAmountText(review) {
  const n = Number(review && review.amount);
  if (!Number.isFinite(n)) return (review && review.amount_display) || "¥—";
  return `¥${n.toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

function reviewFieldValue(key) {
  const values = (state.review && state.review.values) || {};
  return values[key] === undefined || values[key] === null ? "" : String(values[key]);
}

/* 来源标签：用户改过的字段优先显示「已修改」，其余用服务端给的来源。 */
function reviewSource(field) {
  const baseline = state.reviewBaseline || {};
  const key = field.key;
  if (Object.prototype.hasOwnProperty.call(baseline, key)
    && String(baseline[key] ?? "") !== reviewFieldValue(key)) {
    return "已修改";
  }
  return field.source || "";
}

function reviewCheckHtml(check) {
  const tone = check.tone === "danger" || check.tone === "warn" ? check.tone : "ok";
  const icon = tone === "danger" ? "✗" : tone === "warn" ? "!" : "✓";
  return `<div class="review-check review-check--${tone}">
    <span class="review-check__icon" aria-hidden="true">${icon}</span>
    <span class="review-check__label">${esc(check.label || "")}</span>
    <span class="review-check__value">${esc(check.value || "")}</span>
    <span class="review-check__status">${esc(check.status || "")}</span>
  </div>`;
}

function reviewRow(field) {
  const edited = reviewSource(field) === "已修改";
  return `<div class="review-row">
    <span class="review-key">${esc(field.label || "")}</span>
    <span class="review-val">${esc(field.display || "—")}</span>
    <span class="review-tag${edited ? " review-tag--edited" : ""}">${esc(reviewSource(field))}</span>
  </div>`;
}

function reviewEditRow(field) {
  const value = reviewFieldValue(field.key);
  const label = `<label class="review-key" for="review-${esc(field.key)}">${esc(field.label || "")}</label>`;
  if (field.control === "select") {
    const options = (field.options || []).map((item) => {
      const selected = String(item.value) === value ? " selected" : "";
      return `<option value="${esc(item.value)}"${selected}>${esc(item.label)}</option>`;
    }).join("");
    return `<div class="review-row review-row--edit">
      ${label}
      <select class="input" id="review-${esc(field.key)}" data-review-field="${esc(field.key)}">${options}</select>
    </div>`;
  }
  const maxlength = field.maxlength ? ` maxlength="${Number(field.maxlength)}"` : "";
  return `<div class="review-row review-row--edit">
    ${label}
    <input class="input" id="review-${esc(field.key)}" data-review-field="${esc(field.key)}"
      type="text" value="${esc(value)}" placeholder="选填"${maxlength} />
  </div>`;
}

function reviewHtml() {
  const review = state.review;
  if (!review) return "";
  const editing = state.phase === "review-edit";
  const fields = review.fields || [];
  const rows = fields.map((field) => (editing ? reviewEditRow(field) : reviewRow(field))).join("");
  const amount = editing
    ? `<input class="input review-amount__input" id="review-amount" data-review-field="amount"
        type="number" inputmode="decimal" min="0.01" step="0.01" value="${esc(reviewFieldValue("amount"))}" />`
    : `<span class="review-amount__value">${esc(reviewAmountText(review))}</span>`;
  const amountEdited = Object.prototype.hasOwnProperty.call(state.reviewBaseline || {}, "amount")
    && String((state.reviewBaseline || {}).amount ?? "") !== reviewFieldValue("amount");
  const confirmLabel = "确认信息，查看计划卡";
  const actions = editing
    ? `<button class="btn btn--primary" type="button" data-review-action="save">保存并重新核对</button>
       <button class="btn" type="button" data-review-action="cancel-edit">取消</button>`
    : `<button class="btn btn--primary" type="button" data-review-action="confirm"
         ${review.can_confirm === false ? "disabled" : ""}>${confirmLabel}</button>
       <button class="btn" type="button" data-review-action="edit">修改信息</button>`;
  const hint = !editing && review.can_confirm === false
    ? `<p class="review-hint">当前信息不能直接执行，请先修改收款人、付款账户或金额。</p>`
    : "";
  return `<article class="voucher review-card${editing ? " review-card--edit" : ""}">
    <div class="review-head">
      <div>
        <p class="review-kicker">转账核对单</p>
        <h3 class="review-title">${editing ? "修改转账信息" : "请核对这笔转账"}</h3>
        <p class="review-sub">${editing
          ? "改完会重新核对，不会直接执行"
          : "确认后展示计划卡；执行前仍需确认或验证"}</p>
      </div>
      <span class="review-badge">${editing ? "修改中" : "待核对"}</span>
    </div>
    <div class="review-amount">
      <span class="review-amount__label">转账金额${amountEdited
        ? ' <span class="review-tag review-tag--edited">已修改</span>'
        : ""}</span>
      ${amount}
    </div>
    <div class="review-rows">${rows}</div>
    ${(review.checks || []).length ? `<div class="review-checks">
      <p class="review-checks__title">执行前校验</p>
      ${(review.checks || []).map(reviewCheckHtml).join("")}
    </div>` : ""}
    ${hint}
    ${state.reviewError ? `<p class="review-error">${esc(state.reviewError)}</p>` : ""}
    <div class="review-actions">${actions}</div>
  </article>`;
}

function transactionDraftHtml(draft) {
  if (!draft) return "";
  const editing = state.draftEditing === true;
  const fields = Array.isArray(draft.fields) ? draft.fields : [];
  const editable = new Set(Array.isArray(draft.editable_fields) ? draft.editable_fields : []);
  const rows = fields.map((field) => {
    const key = String(field.key || "");
    const canEdit = editable.has(key) && field.editable !== false;
    const label = `<label class="draft-row__label" for="draft-${esc(key)}">${esc(field.label || key)}</label>`;
    if (editing && canEdit) {
      const money = /(?:amount|limit)/.test(key);
      return `<div class="draft-row draft-row--edit">${label}
        <input class="input" id="draft-${esc(key)}" data-draft-field="${esc(key)}"
          ${money ? 'type="number" min="0.01" step="0.01" inputmode="decimal"' : 'type="text"'}
          value="${esc(field.value || "")}" /></div>`;
    }
    return `<div class="draft-row">${label}<span class="draft-row__value">${esc(field.display || field.value || "未填写")}</span></div>`;
  }).join("");
  const actions = editing
    ? `<button class="btn btn--primary" type="button" data-draft-action="save">保存并重新冻结</button>
       <button class="btn" type="button" data-draft-action="cancel">取消</button>`
    : `<button class="btn btn--sm" type="button" data-draft-action="edit">修改信息</button>`;
  return `<section class="transaction-draft" aria-label="交易草稿">
    <div class="transaction-draft__head"><div><p class="transaction-draft__eyebrow">交易草稿</p>
      <h3>${esc({ transfer: "转账核对", wealth: "理财办理", subscription: "代扣解约", card: "卡片办理", life_event: "生活计划" }[draft.kind] || "业务核对")}</h3></div>
      <span class="chip">${esc(draft.risk_level || "")}</span></div>
    <div class="transaction-draft__rows">${rows}</div>
    <div class="transaction-draft__disclosures">${(draft.disclosures || []).map((line) => `<p>${esc(line)}</p>`).join("")}</div>
    ${state.draftError ? `<p class="review-error">${esc(state.draftError)}</p>` : ""}
    <div class="btn-row">${actions}</div>
  </section>`;
}

function voucherHtml(card = state.card, decision = state.decision, options = {}) {
  if (!card) return "";
  const opts = options || {};
  const lines = card.lines || [];
  const frozen = card.frozen_hash || "";
  const reasons = decision && Array.isArray(decision.reasons) ? decision.reasons : [];
  const readOnly = opts.readOnly === true;
  const reviewEditable = !readOnly && Boolean(state.review)
    && (state.phase === "awaiting_confirm" || state.phase === "strong-verify");
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

    ${reviewEditable ? `<div class="voucher__review-row">
      <span class="voucher__review-ok">信息已核对</span>
      <button class="btn btn--sm btn--ghost" type="button" data-review-action="edit">修改信息</button>
    </div>` : ""}

    ${quotaHtml(card)}
    ${sheetFieldsHtml(card)}

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

/* 申购弹窗填写的交易信息：计划卡上方原样回显，保证「卡上字段 = 弹窗填写值」 */
function sheetFieldsHtml(card) {
  const fields = card && card.sheet_fields;
  if (!fields) return "";
  return `<div class="takeover takeover--note" style="margin-bottom: var(--s-3)">
    <p class="muted">本次申购信息 · 与申购弹窗填写一致</p>
    <p><b>${esc(fields.product || "")}</b>
      ${fields.risk ? `<span class="chip">${esc(fields.risk)}</span>` : ""}</p>
    <p>申购金额 <b>${esc(String(fields.amount || "—"))} 元</b>
      · 交易账户 ${esc(fields.account || "—")}</p>
    <p>分红方式 ${esc(fields.dividend || "—")}</p>
  </div>`;
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

/* 查询答复：服务端确定性给出的「查到 / 没查到」结论，挂在计划卡或回执上方 */
function answerNoteHtml(note) {
  if (!note) return "";
  return `<div class="takeover takeover--note" style="margin-bottom: var(--s-3)">
    <p class="muted">查询答复</p>
    <p>${esc(note)}</p>
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
  const inReview = Boolean(state.review)
    && (state.phase === "review" || state.phase === "review-edit");
  const note = answerNoteHtml(state.answerNote);
  const workflow = workflowHtml(state.workflow);
  const draft = transactionDraftHtml(state.draft);
  state.response.innerHTML = inReview
    ? workflow + reviewHtml() + state.notice
    : state.card
      ? draft + note + workflow + voucherHtml() + (state.phase === "strong-verify" ? verifyHtml() : "") + state.notice
      : note + (state.panel || "") + state.notice;
  if (runChain) {
    /* 三分类 / 澄清回合没有可执行计划：运行链路里的「已拒绝 · 未执行」只会让人误以为被风控拦了 */
    runChain.hidden = Boolean(state.triage) || (state.phase === "blocked" && !state.decision);
    state.response.insertBefore(runChain, state.response.firstChild);
  }
  /* 没有裁决就没有「AI 链路」可回显；三分类不是裁决，不能挂调试行 */
  if (state.decision) state.response.innerHTML += roleChainHtml();
  if (el("vcode")) el("vcode").value = code;
  if (state.task) state.task.appendChild(el("task-actions"));
  showRequestStatus();
}

function workflowHtml(workflow) {
  if (!workflow || !Array.isArray(workflow.disclosures)) return "";
  const labels = {
    transfer: "转账核对", wealth: "理财办理", subscription: "代扣解约",
    card: "卡片办理", life_event: "生活计划",
  };
  const title = labels[workflow.kind] || "业务核对";
  return `<section class="workflow-note" aria-label="${esc(title)}">
    <div class="workflow-note__head"><span>${esc(title)}</span><span class="chip">${esc(workflow.stage || "review")}</span></div>
    ${workflow.disclosures.map((line) => `<p>${esc(line)}</p>`).join("")}
  </section>`;
}

/* 每条回复在收尾时把自己那一段时间线快照进消息里：
   之后新的一次办理会清空右栏，但旧回复仍能展开看当时发生了什么。 */
function snapshotTrace(response) {
  const host = response || state.response;
  const timeline = el("timeline");
  if (!host || !timeline || host.querySelector(".msg-trace")) return;
  if (timeline.dataset.empty !== "false") return;
  const items = Array.from(timeline.querySelectorAll("li"));
  if (!items.length) return;
  const details = document.createElement("details");
  details.className = "msg-trace";
  details.innerHTML = `<summary>处理详情 · ${state.chain.length} 条审计 · ${items.length} 条时间线</summary>
    <ul class="msg-trace__list">${items.map((node) => `<li>${node.innerHTML}</li>`).join("")}</ul>
    <p class="muted">本段快照来自当时的服务端回执；刷新页面后不再保留。</p>`;
  host.appendChild(details);
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
  const reviewStatus = metadata.review_status;
  const reviewNames = { allow: "通过", clarify: "需澄清", reject: "拒绝", manual: "转人工" };
  const reviewerNote = reviewer
    ? (reviewer === "bypass" ? "旁路诊断通道：未逐条审查计划" : `逐条核对计划是否越权${reviewStatus ? ` · ${reviewNames[reviewStatus] || reviewStatus}` : ""}`)
    : "当前通道旁路审查，不冒充已审查";
  /* 每次回答只留一行摘要，展开才看三格：避免占掉对话正文的位置 */
  return `<details class="role-chain-fold" aria-label="AI 三角色链路">
    <summary>AI 链路 · 规划 ${esc(name(metadata.planner_channel))} · 审查 ${esc(reviewer ? name(reviewer) : "未启用")}${reviewStatus ? `（${esc(reviewNames[reviewStatus] || reviewStatus)}）` : ""} · 裁决 ${esc(level || "待裁决")}</summary>
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

function requestStart(start, { allowAbandon = true } = {}) {
  if (hasUnfinishedTask()) {
    state.pendingStart = start;
    el("task-guard-title").textContent = "当前办理尚未结束";
    /* 只有“待确认/强验证”才允许放弃当前计划后在同一会话继续；
       executing / interrupted 等状态不提供该入口，避免绕过执行中/未恢复的计划。 */
    const abandon = el("btn-abandon-task");
    if (abandon) {
      abandon.hidden = !allowAbandon
        || !["awaiting_confirm", "strong-verify"].includes(state.phase);
    }
    /* 提示条是全局浮层，但对话内容在 #view-chat 里：先切回去，
       否则用户在记录页/场景页点「新建对话」会以为没反应。 */
    if (el("app")?.dataset.viewName !== "chat") navigate("chat", {});
    el("task-guard").hidden = false;
    el("btn-continue-task").focus();
    return false;
  }
  return true;
}

/* 任务操作条在任务之间复用同一个 DOM 节点，`<details>` 的 open 状态会被浏览器带过去；
   每次新计划卡生成时强制收起，避免上一笔展开的「更多办理操作」串到下一笔。 */
function collapseTaskMore() {
  const more = el("task-actions")?.querySelector("details");
  if (more) more.open = false;
}

function beginTask(text, selected = {}) {
  /* 同一会话连续对话：默认沿用当前 SESSION_ID，办完一笔再说话仍落在同一条对话。
     只有显式「新建对话 / 结束并新建 / 删除当前会话」或执行结果未知（需要重新核对回执）
     才换新会话；否则会把上一笔的待核对回执覆盖掉。 */
  const recoveryUnknown = state.operationUnknown;
  archiveTask();
  reset(true);
  state.historyRestored = false;
  if (recoveryUnknown) SESSION_ID = newSessionId();
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
  collapseTaskMore();
  task.appendChild(el("task-actions"));
  setMobileDrawer("scenes", false);
}

async function startFreshConversation() {
  if (!requestStart(startFreshConversation, { allowAbandon: false })) return;
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
  if (marker) {
    marker.hidden = !offline;
    if (offline && !el("btn-reconnect")) {
      /* 只提示不给出路等于把人锁死：给一个自己收回离线态的入口 */
      marker.innerHTML =
        '当前离线：提交、确认和挑战已禁用。' +
        '<button class="btn btn--sm" id="btn-reconnect" type="button">重新连接</button>';
      el("btn-reconnect").addEventListener("click", reconnect);
    }
  }
  if (offline) timelineAdd("当前离线：提交、确认、挑战和其他写操作已禁用。", "warn");
  setButtons();
}

/* 重新连接：用一次只读请求确认服务端可达；成功就解除离线态并刷新可选对象。 */
async function reconnect() {
  const button = el("btn-reconnect");
  if (button) button.disabled = true;
  try {
    await apiFetch("GET", "/api/options");
    setOfflineState(false);
    await refreshOptions();
    timelineAdd("已重新连接服务端。", "done");
  } catch (_) {
    timelineAdd("仍连不上服务端，请检查网络后重试。", "stop");
    if (button) button.disabled = false;
  }
}

function setButtons() {
  const busy = state.busy || state.phase === "executing";
  const offline = state.offline || (typeof navigator !== "undefined" && navigator.onLine === false);
  const awaiting = state.phase === "awaiting_confirm";
  const strongPhase = state.phase === "strong-verify";
  const inReview = state.phase === "review" || state.phase === "review-edit";
  const operationUnknown = state.operationUnknown;
  /* 只读历史：输入框与发送一律不可用，避免「文案说需新建对话、实际还能直接发」 */
  const archiveOnly = state.archiveReadOnly === true;
  const box = el("utterance");
  if (box) {
    box.disabled = archiveOnly;
    box.placeholder = archiveOnly
      ? "这是历史记录 · 点「新建对话」继续办理"
      : "说一句话，比如：转给李娜 500 元";
  }
  el("btn-submit").disabled = archiveOnly || offline || busy || !state.options;
  el("btn-due").disabled = archiveOnly || offline || busy || !state.options;
  el("btn-reset").disabled = busy;
  /* 核对单阶段由卡内按钮负责确认/修改，底部操作条整体让位，避免绕过核对直接执行。 */
  el("task-actions").hidden = inReview || !state.task || (!state.card && !state.decision);
  /* 没有计划卡就没有「确认并执行」：三分类 / 澄清回合不该出现一个永远点不动的按钮 */
  el("btn-confirm").hidden = !state.card || inReview;
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
    : isWealthCard(state.card) && !wealthSheetHandled(state.card)
      ? "打开交易确认"
      : isCardWriteCard(state.card)
        ? "打开操作确认"
        : strongPhase
        ? ((state.card && (state.card.nodes || []).some(
          (node) => String(node.action || "") === "subscription.cancel"
        )) ? "提交验证码并关闭协议" : "提交验证码并执行")
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
  state.review = null;
  state.reviewBaseline = null;
  state.reviewConfirmed = false;
  state.reviewEditFrom = null;
  state.reviewError = "";
  state.decision = null;
  state.questions = [];
  state.stepStates = [];
  state.nodeStatus = {};
  state.answerNote = "";
  state.phase = "idle";
  state.chain = [];
  state.panel = "";
  state.verification = null;
  state.clarifications = [];
  state.triage = null;
  state.notice = "";
  state.resultCount = 0;
  state.operationUnknown = false;
  state.archiveReadOnly = false;
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
  state.workflow = data.workflow || null;
  state.draft = data.draft || null;
  state.draftEditing = false;
  state.draftError = "";
  markRunOutcome(data.state);
  /* 查询答复（如「未查询到该笔交易」）：服务端确定性给出，挂在卡/回执上方一起展示 */
  state.answerNote = String(data.answer_note || "");
  if (data.state === "awaiting_confirm" && data.card) {
    state.card = data.card;
    state.decision = data.decision || null;
    state.stepStates = (data.card.lines || []).map(() => "pending");
    state.nodeStatus = {};
    (data.card.nodes || []).forEach((node) => { state.nodeStatus[node.node_id] = "pending"; });
    const review = data.review && data.review.kind === "transfer" ? data.review : null;
    if (review) {
      /* 转账先出核对单：用户核对/修改后才展示冻结的计划卡。 */
      state.review = review;
      if (!state.reviewBaseline) state.reviewBaseline = { ...review.values };
      state.reviewConfirmed = false;
      state.reviewEditFrom = null;
      state.reviewError = "";
      state.phase = "review";
      collapseTaskMore();
      chainAdd("核对单已生成", `待核对 ${review.amount_display || ""}`);
      renderStage();
      setButtons();
      timelineAdd(`转账核对单 · ${review.amount_display || ""} · 待核对`, "live");
      if (data.card.level !== "GREEN") snapshotTrace();
      return;
    }
    state.review = null;
    state.reviewBaseline = null;
    state.reviewConfirmed = false;
    state.reviewEditFrom = null;
    state.reviewError = "";
    state.phase = "awaiting_confirm";
    collapseTaskMore();
    chainAdd("计划卡已冻结", `cfhash ${String(data.card.frozen_hash).slice(0, 12)}`);
    if ((data.card.nodes || []).some((node) => node.action === "card.report_lost")) {
      state.notice += `<div class="btn-row"><button class="btn btn--sm btn--ghost" type="button"
        data-prefill="申请一张虚拟卡">申请补卡</button></div>`;
    }
    renderStage();
    const next = data.card.level === "GREEN" ? "自动执行查询" : "等待确认";
    timelineAdd(`裁决：${(LEVEL[data.card.level] || {}).name || data.card.level} · 计划卡已冻结（${next}）`, data.card.level === "RED" ? "warn" : "done");
    if (data.card.level !== "GREEN") snapshotTrace();
    if (data.card.level === "GREEN") await onConfirm();
    if (data.redemption_prefill) {
      openWealthRedeemSheet({
        productId: data.redemption_prefill.product_id,
        prefill: data.redemption_prefill,
        card: data.card,
      }).catch(() => {});
    }
    return;
  }
  state.review = null;
  state.reviewBaseline = null;
  state.reviewConfirmed = false;
  state.reviewEditFrom = null;
  state.reviewError = "";
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
  if (data.state === "idle" && data.wealth_recommend) {
    /* 「我要申购理财产品」：先按测评等级推荐，不先问金额；仍用理财页同一套产品卡 */
    state.phase = "idle";
    state.panel = wealthRecommendHtml(data.wealth_recommend);
    renderStage();
    timelineAdd("理财推荐 · 按测评等级硬筛选（只读，不生成计划）", "done");
    return;
  }
  if (data.state === "idle") {
    state.phase = "idle";
    state.panel = `<div class="takeover"><p>${esc(data.message || "当前没有到期待办。")}</p></div>`;
    renderStage();
    timelineAdd(data.message || "当前没有到期待办。", "done");
    return;
  }
  const triage = data.triage || null;
  state.triage = triage;
  /* 只有**真实裁决**（策略拒绝）才保留决策与链路调试行；三分类不是裁决 */
  state.decision = triage ? null : data.decision || null;
  state.phase = data.state === "blocked" ? "blocked" : "clarifying";
  state.clarifications = data.clarifications || [];
  /* 场景层追问（如「这笔 6800 是不是我刷的」）只有一句话、没有结构化槽位表单：
     此时必须把服务端那句 prompt 原样显示出来，不能退化成「请补充所需信息后重新提交。」——
     那句空话既没告诉用户缺什么，也和 API 返回的 message 不一致。 */
  const clarifyPrompt = data.message || (Array.isArray(data.questions) ? data.questions[0] : "");
  state.panel = triage
    ? triageHtml(triage, data.message || "")
    : data.state === "blocked"
      ? interceptHtml("安全拒绝（未产生可执行计划）", data.message || "未知意图不猜测执行；已留痕。", false)
      : clarifyHtml() || `<div class="takeover"><p>${esc(clarifyPrompt || "请补充所需信息后重新提交。")}</p></div>`;
  renderStage();
  timelineAdd(
    triage ? "已说明能力边界 · 未产生计划" : data.state === "blocked" ? "安全拒绝 · 已留痕" : "转澄清 · 未产出级别",
    triage ? "live" : "stop",
  );
}

const TRIAGE_TITLES = {
  smalltalk: "先聊两句",
  out_of_scope: "这个不在银行能办的范围内",
  ambiguous: "没太理解您的意思",
};

/* 三分类回应：中性卡 + 可点示例。不用红色封条——那不是风控拒绝。 */
function triageHtml(triage, message) {
  const chips = (triage.suggestions || [])
    .map((item) => `<button class="btn btn--sm" type="button" data-suggestion="${esc(item.text)}">${esc(item.label)}</button>`)
    .join("");
  return `<div class="triage">
    <p class="triage__head">${esc(TRIAGE_TITLES[triage.kind] || TRIAGE_TITLES.ambiguous)}</p>
    <p class="triage__body">${esc(message)}</p>
    ${triage.disclaimer ? `<p class="triage__note">${esc(triage.disclaimer)}</p>` : ""}
    ${chips ? `<div class="btn-row">${chips}</div>` : ""}
  </div>`;
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
      <button class="btn btn--primary" id="btn-clarify" data-requires-login>补充并继续</button>
    </div>
  </div>`;
}

function baseSlot(slot) {
  return slot.replace(/^intent_\d+_/, "");
}

function clarifyPrompt(ask) {
  const slot = baseSlot(ask.slot);
  /* 金额槽的单位与示例只在服务端 prompt 里说一次：这里再拼一遍会变成三处口径。 */
  if (slot === "period") return "请选择扣费周期。";
  return ask.prompt;
}

function clarifyControl(ask) {
  const slot = baseSlot(ask.slot);
  const attrs = `class="input" id="ask-${esc(ask.slot)}" data-slot="${esc(ask.slot)}" required`;
  /* 候选优先：同名收款人来自名册；options 来自场景（花店 / 花束档位）。
     两者都点选即可，不用用户回头查号码或自己拼商户名。 */
  const choices = (ask.candidates && ask.candidates.length)
    ? ask.candidates
    : (Array.isArray(ask.options) ? ask.options : []);
  if (choices.length) {
    const options = choices
      .map((item) => `<option value="${esc(item.value)}">${esc(item.label)}</option>`)
      .join("");
    const note = ask.candidates && ask.candidates.length
      ? "候选来自名册与已记账流水，只列事实，不做猜测。"
      : "选项来自演示白名单，仅用于补全参数；确认后才会执行。";
    return (
      `<select ${attrs}><option value="">请选择${esc(SLOT_LABELS[slot] || "其一")}</option>` +
      `${options}</select><span class="muted">${esc(note)}</span>`
    );
  }
  if (OBJECT_OPTIONS[slot] || slot === "product") return `<select ${attrs}>${optionsHtml(slot)}</select>`;
  if (slot === "period") {
    return `<select ${attrs}><option value="">请选择扣费周期</option>` +
      `<option value="monthly">每月</option><option value="yearly">每年</option></select>`;
  }
  if (slot === "loss_type") {
    return `<select ${attrs}><option value="">请选择挂失类型</option>` +
      `<option value="temporary">临时冻结（可解，5–15 天，免费）</option>` +
      `<option value="formal">正式挂失（不可逆，需要补卡）</option></select>`;
  }
  if (slot === "when" || slot === "next_charge_on" || slot === "delivery_time" || slot === "order_on") {
    return `<input ${attrs} type="date" />`;
  }
  if (MONEY_SLOTS.has(slot)) {
    /* 单位与示例只在 prompt 里出现一次；占位符只提示「填数字」，不再换一个示例金额。 */
    return `<input ${attrs} type="number" min="0.01" step="0.01" inputmode="decimal" placeholder="请输入数字" />`;
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
    const slot = baseSlot(node.dataset.slot);
    if (value) answers[node.dataset.slot] = withMoneyUnit(slot, value);
  });
  const answerText = inputs.map((node) => {
    const slot = baseSlot(node.dataset.slot);
    const shown = node.tagName === "SELECT" && node.selectedOptions[0]
      ? node.selectedOptions[0].textContent
      : node.value;
    return `${SLOT_LABELS[slot] || "补充信息"}：${withMoneyUnit(slot, String(shown || "").trim())}`;
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
  /* 未登录只读壳：回车发送也走登录，不把 401 当成交互反馈。 */
  if (document.documentElement.dataset.demoReadonly === "1") {
    window.location.assign("/login");
    return;
  }
  const text = el("utterance").value.trim();
  if (!text) { el("utterance").focus(); return; }
  if (billLeadQuestionText(text)) {
    const explainLead = () => submitBillLeadQuestion(text);
    if (!requestStart(explainLead)) return;
    await explainLead();
    return;
  }
  const leadState = billLeadResolutionState(text);
  if (leadState) {
    const resolveLead = () => submitBillLeadResolution(text, leadState);
    if (!requestStart(resolveLead)) return;
    await resolveLead();
    return;
  }
  const selected = selectedObjects();
  if (!/转账|转给|汇款|打款/.test(text)) {
    delete selected.payee;
    delete selected.transfer_method;
    delete selected.purpose;
  }
  pendingSceneSelected = {};
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
    snapshotTrace();
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

/* 失败预约重试：只把状态放回 `scheduled`（次数受服务端上限约束），
   再走一次「检查到期待办」重新提案，确认仍由用户点。 */
async function onRetrySchedule(scheduleId) {
  if (!scheduleId) return;
  const result = await post("/api/life-schedules/retry", { schedule_id: scheduleId });
  if (result.status !== 200) {
    state.notice = interceptHtml("重试未受理", result.data.detail || "请稍后再试", false);
    renderStage();
    timelineAdd(`重试未受理：${result.data.detail || "请稍后再试"}`, "stop");
    return;
  }
  timelineAdd("已把该预约放回待办，正在重新生成提案…", "live");
  await onDue();
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

/* ---------------- 核对单动作 ---------------- */

function confirmReview() {
  if (!state.review || !state.card || state.review.can_confirm === false) return;
  state.reviewConfirmed = true;
  state.reviewEditFrom = null;
  state.reviewError = "";
  state.phase = "awaiting_confirm";
  renderStage();
  setButtons();
  chainAdd("信息核对通过", "转入执行确认");
  timelineAdd("转账信息已核对 · 计划卡已展开", "done");
  snapshotTrace();
}

function openReviewEdit() {
  if (!state.review) return;
  state.reviewEditFrom = state.reviewConfirmed ? "awaiting_confirm" : "review";
  state.reviewError = "";
  state.phase = "review-edit";
  renderStage();
  setButtons();
}

function cancelReviewEdit() {
  state.phase = state.reviewEditFrom || "review";
  state.reviewError = "";
  renderStage();
  setButtons();
}

function readReviewControl(key) {
  const root = state.response;
  const node = root ? root.querySelector(`[data-review-field="${key}"]`) : null;
  return node ? String(node.value || "").trim() : "";
}

async function saveReview() {
  const review = state.review;
  if (!review) return;
  const amount = Number(readReviewControl("amount"));
  if (!Number.isFinite(amount) || amount <= 0) {
    state.reviewError = "金额需大于 0，请重新填写。";
    renderStage();
    return;
  }
  const payeeId = readReviewControl("payee");
  const accountId = readReviewControl("account");
  const method = readReviewControl("transfer_method") || "realtime";
  const remark = readReviewControl("remark").slice(0, 50);
  if (!payeeId || !accountId) {
    state.reviewError = "请选择收款人和付款账户。";
    renderStage();
    return;
  }
  const payeeField = (review.fields || []).find((field) => field.key === "payee");
  const payeeOption = payeeField && (payeeField.options || []).find(
    (item) => String(item.value) === String(payeeId),
  );
  const payeeName = String(
    (payeeOption && payeeOption.name)
    || (review.values && review.values.payee_name)
    || "",
  ).trim();
  if (!payeeName) {
    state.reviewError = "未能识别收款人姓名，请重新选择。";
    renderStage();
    return;
  }
  const methodLabel = TRANSFER_METHOD_LABELS[method] || "实时到账";
  /* 次日到账必须让「延时语义」出现在转账动词前，才能稳定落到 transfer.book（登记不扣款）。 */
  const utterance = method === "next_day"
    ? `次日到账转给${payeeName} ${amount} 元`
    : `给${payeeName}转 ${amount} 元，${methodLabel}`;
  const selected = {
    account: accountId,
    payee: payeeId,
    amount: `${amount} 元`,
    transfer_method: method,
  };
  if (remark) selected.remark = remark;
  state.reviewError = "";
  const result = await post("/api/submit", { utterance, selected });
  if (result.status !== 200) {
    state.reviewError = String(
      (result.data && result.data.detail) || "修改未提交成功，请稍后重试。",
    );
    renderStage();
    return;
  }
  await applySubmission(result.data);
  timelineAdd(`已修改核对单：${amount} 元 · ${methodLabel}`, "live");
}

async function runReviewSave() {
  if (state.busy) return;
  state.busy = true;
  setButtons();
  try {
    await saveReview();
  } catch (error) {
    state.reviewError = fetchErrorMessage(error, "修改未提交成功，请稍后重试。");
    state.phase = "review-edit";
    renderStage();
  } finally {
    state.busy = false;
    setButtons();
  }
}

function openDraftEdit() {
  if (!state.draft) return;
  state.draftEditing = true;
  state.draftError = "";
  renderStage();
  setButtons();
}

function cancelDraftEdit() {
  state.draftEditing = false;
  state.draftError = "";
  renderStage();
  setButtons();
}

async function saveDraftEdit() {
  const draft = state.draft;
  if (!draft || !state.response) return;
  const changes = {};
  const fields = Array.isArray(draft.fields) ? draft.fields : [];
  const editable = new Set(Array.isArray(draft.editable_fields) ? draft.editable_fields : []);
  fields.forEach((field) => {
    const key = String(field.key || "");
    if (!editable.has(key)) return;
    const input = state.response.querySelector(`[data-draft-field="${key}"]`);
    if (!input) return;
    const raw = String(input.value || "").trim();
    const original = String(field.value || "").trim();
    if (raw !== original) {
      changes[key] = /(?:amount|limit)/.test(key) && raw ? `${raw} 元` : raw;
    }
  });
  if (!Object.keys(changes).length) {
    cancelDraftEdit();
    return;
  }
  const result = await patchDraft(draft.draft_id, changes);
  if (!result.ok) {
    state.draftError = String((result.data && result.data.detail) || "草稿修改未被接受。");
    renderStage();
    return;
  }
  await applySubmission(result.data);
  timelineAdd("交易草稿已修改，原确认凭证已失效", "live");
}

async function onConfirm() {
  if (!state.card) return;
  state.historyRestored = false;
  if (isWealthCard(state.card) && !wealthSheetHandled(state.card)
    && (state.phase === "awaiting_confirm" || state.phase === "strong-verify")) {
    /* 理财交易统一进确认弹窗：弹窗负责四件套、份额/金额、验证码与二次确认。 */
    const redeem = (state.card.nodes || []).some((node) => node.action === "wealth.redeem");
    openWealthTradeSheet({
      mode: redeem ? "redeem" : "subscribe",
      card: state.card,
    }).catch(() => {});
    return;
  }
  if (isCardWriteCard(state.card)
    && (state.phase === "awaiting_confirm" || state.phase === "strong-verify")) {
    /* 卡片写操作：AI 只生成预填计划；确认与一次性验证码在卡片弹窗内完成。 */
    openCardFlowFromPlan(state.card).catch(() => {});
    return;
  }
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
  const total = Number(summary.consumption_count ?? summary.transaction_count) || 0;
  const transfer = Number(summary.transfer_count) || 0;
  const key = Number(summary.key_lead_count) || 0;
  const pending = Number(summary.lead_count) || 0;
  if (!total) return "本期没有消费流水，无法给出消费结论；转账/还款等不计入消费分析。";
  const parts = [`本期 ${total} 笔消费`];
  if (transfer) parts.push(`另有 ${transfer} 笔转账/还款等不计入消费`);
  if (key) parts.push(`${key} 条高置信待核对线索`);
  if (pending > key) parts.push(`${pending - key} 条可继续核对线索`);
  if (!pending) parts.push("未命中需优先核对的线索");
  return `${parts.join("，")}。`;
}

function billIssueHtml(item) {
  const status = BILL_STATUS_LABELS[item.status] || "未知状态";
  const basis = Array.isArray(item.basis) ? (item.basis[0] || {}) : (item.basis || {});
  const label = item.labels && item.labels.length
    ? item.labels.join(" · ")
    : FINDING_LABELS[item.kind] || item.kind;
  const detail = item.detail || basis.detail || "";
  return `<details class="bill__issue">
    <summary>
      <b>${esc(label)}</b>
      <span class="bill__issue-amt">${yuan(Number(item.impact_cents) || 0)}</span>
      <span class="chip">${esc(status)} · ${confidencePct(item.confidence)}</span>
    </summary>
    <p>${esc(detail)}</p>
    ${basis.threshold ? `<p class="bill__note">判断依据：${esc(basis.rule || "")}（阈值：${esc(basis.threshold)}）</p>` : ""}
    ${item.suggestion ? `<p class="bill__note">建议：${esc(item.suggestion)}</p>` : ""}
  </details>`;
}

/* 对话里的账单结论只保留「一句话结论 + 三个数字 + 最多三条高置信线索」，
   图表、完整证据链与全部建议留在账单分析页。 */
function billAnalysisHtml(payload, advice, adviceMessage) {
  if (!payload || !payload.summary) return "";
  const summary = payload.summary;
  const total = Number(summary.consumption_count ?? summary.transaction_count) || 0;
  const expense = Number(summary.consumption_cents ?? summary.expense_total_cents) || 0;
  const income = Number(summary.income_cents) || 0;
  const pending = Number(summary.lead_count) || 0;
  const transfer = Number(summary.transfer_count) || 0;
  const leads = (Array.isArray(payload.leads) ? payload.leads : [])
    .filter((item) => item.tier === "key");
  const fallback = (Array.isArray(payload.findings) ? payload.findings : [])
    .filter((item) => item.status !== "normal" && Number(item.confidence) >= 0.7);
  const problems = leads.length ? leads : fallback;
  const top = problems.slice(0, 3);
  const rest = problems.length - top.length;
  const topAdvice = Array.isArray(advice) && advice.length ? advice[0] : null;
  const value = (text) => (total || transfer ? text : "—");
  return `<section class="bill bill--compact">
    <div class="bill__head">
      <h3>账单分析结论</h3>
      <span class="chip">账期 ${esc(payload.period)}</span>
      ${payload.data_source_is_demo ? '<span class="chip">演示数据</span>' : ""}
    </div>
    <p class="bill__verdict">${esc(billVerdict(summary))}</p>
    <div class="bill__metrics bill__metrics--compact">
      <div class="bill__metric"><span>本期消费</span><b>${value(yuan(expense))}</b></div>
      <div class="bill__metric"><span>本期收入</span><b>${value(yuan(income))}</b></div>
      <div class="bill__metric"><span>待核对（高置信）</span><b>${value(`${pending} 条`)}</b></div>
    </div>
    ${transfer ? `<p class="bill__note">另有 ${transfer} 笔转账/还款等 ${yuan(summary.transfer_out_cents || 0)}，不计入消费分析。</p>` : ""}
    ${top.length
      ? `<div class="bill__issues">${top.map(billIssueHtml).join("")}</div>
         ${rest > 0 ? `<p class="bill__note">另有 ${rest} 条可继续核对线索，可在账单分析页查看。</p>` : ""}`
      : `<p class="bill__note">${total ? "本期没有高置信待核对线索。" : "本期没有消费流水，无法给出消费结论。"}</p>`}
    ${topAdvice ? `<p class="bill__note">建议：${esc(topAdvice.suggestion)}</p>` : ""}
    <p class="bill__note">识别结果供核对，不作最终结论。</p>
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

/* 动作前缀 → 记录页 Tab：回执上的「查看记录」直接落到对应清单 */
const RECORD_TAB_BY_ACTION = {
  transfer: "transfers",
  card: "cards",
  subscription: "charges",
  wealth: "holdings",
  funds: "transfers",
  merchant: "transfers",
  account: "transfers",
};

function recordTabFor(results) {
  for (const item of results) {
    const tab = RECORD_TAB_BY_ACTION[String(item.action || "").split(".")[0]];
    if (tab) return tab;
  }
  return "transfers";
}

/* 「查卡片」不该只回一句「共 2 张卡」：用卡片页同一份接口出列表卡，只读、无操作按钮。 */
async function loadChatCardList(host) {
  if (!host) return;
  const result = await apiFetch("GET", "/api/cards");
  if (!result.ok || !result.data || !Array.isArray(result.data.cards)) {
    host.innerHTML = '<p class="muted">卡片读取失败，请稍后重试。</p>';
    return;
  }
  const cards = result.data.cards;
  if (!cards.length) {
    host.innerHTML = '<p class="muted">名下还没有卡片；可在卡片页申请虚拟卡。</p>';
    return;
  }
  host.innerHTML = `<section class="chat-cards">
    <div class="chat-cards__head"><h3>我的卡片</h3><span class="chip">共 ${cards.length} 张</span></div>
    ${cards.map((card) => `<div class="chat-cards__row">
      <span class="chat-cards__name">${esc(card.type_label)} · 尾号 ${esc(card.last4 || "未知")}</span>
      <span class="chat-cards__meta">${esc(card.status_label)} · 日限额 ${yuan(Number(card.daily_limit_cents) || 0)}</span>
    </div>`).join("")}
    <div class="btn-row"><button class="btn btn--sm btn--ghost" type="button" data-nav="scene" data-scene="cards">去卡片页</button></div>
  </section>`;
}

/* 失败终态：如实说「已释放冻结 N 笔」，并给落为 `failed` 的到期预约一个显式重试入口
   （服务端不再自动重提，避免同一笔重复扣款）。 */
function executionFailureHtml(data) {
  const released = Array.isArray(data.released_holds) ? data.released_holds : [];
  const failedSchedules = Array.isArray(data.failed_schedules) ? data.failed_schedules : [];
  let html = interceptHtml("执行尚未完成", data.message || "请核对已执行步骤。", false);
  if (released.length) {
    html += `<p class="muted">已释放冻结 ${released.length} 笔，未产生扣款。</p>`;
  }
  if (failedSchedules.length) {
    html += `<div class="btn-row">${failedSchedules.map((scheduleId) =>
      `<button class="btn btn--sm" type="button" data-life-retry="${esc(scheduleId)}">重试该预约</button>`).join("")}</div>`;
  }
  return html;
}

function applyExecution(data) {
  showModelMetadata(data.model);
  if (data.card) {
    /* 同一张计划卡的服务端回执不带前端标记：保留「弹窗已确认」状态与回显字段，
       否则执行/恢复后按钮会退回「打开交易确认」。 */
    const previous = state.card;
    if (previous && previous.card_id === data.card.card_id && previous.sheet_done) {
      data.card.sheet_done = true;
      data.card.sheet_fields = previous.sheet_fields;
    }
    state.card = data.card;
  }
  if (data.decision) state.decision = data.decision;
  if (data.state === "completed" || data.state === "reversed") {
    state.draft = null;
    state.draftEditing = false;
  }
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
  markRunOutcome(data.state);
  const complete = data.state === "completed";
  const scheduledFor = data.scheduled_for || "";
  rememberBillsPeriod(data.bill_findings);
  const billHtml = complete
    ? billAnalysisHtml(data.bill_findings, data.bill_advice, data.bill_advice_message)
    : "";
  const adviceFallback = billAdviceHtml(data.bill_advice) +
    (data.bill_advice_message ? `<p class="muted">${esc(data.bill_advice_message)}</p>` : "");
  const receiptLines = results.map((item) =>
    String(resultMessage(item)).replace(/｜审计ID\s*\S+\s*$/, "").trim()
  ).filter(Boolean);
  const recordIds = results.flatMap((item) => (Array.isArray(item.record_ids) ? item.record_ids : []));
  const recordTab = recordTabFor(results);
  const actions = results.map((item) => String(item.action || ""));
  const showsCardList = complete && actions.includes("card.list");
  state.notice = scheduledFor
    ? `<div class="takeover">
        <p><b>已登记预约（${esc(scheduledFor)}）</b></p>
        <p class="muted">今天没有冻结余额、也没有扣款。到期后服务端会重新生成计划卡，需要你再次确认才会执行。</p>
        <div class="btn-row">
          <button class="btn btn--sm btn--ghost" type="button" data-nav="records" data-record-tab="${esc(recordTab)}">查看记录</button>
        </div>
      </div>`
    : complete
    ? `<div class="takeover takeover--receipt">
        <p class="receipt__title"><span class="receipt__tick" aria-hidden="true">✓</span>${esc(data.message || "计划执行完成")}</p>
        ${receiptLines.length ? `<ul class="receipt__list">${receiptLines.map((line) => `<li>${esc(line)}</li>`).join("")}</ul>` : ""}
        ${recordIds.length ? `<p class="receipt__record">记录号 <span class="mono">${esc(recordIds[0])}</span></p>` : ""}
        <div class="btn-row">
          <button class="btn btn--sm btn--primary" type="button" data-nav="records" data-record-tab="${esc(recordTab)}">查看记录</button>
        </div>
      </div>` +
      (billHtml || adviceFallback) + provenancePanelHtml()
    : executionFailureHtml(data);
  if (showsCardList) state.notice += '<div id="chat-card-list" class="muted">正在读取卡片…</div>';
  renderStage();
  if (showsCardList) void loadChatCardList(el("chat-card-list"));
  if (complete && !scheduledFor) void loadProvenance();
  timelineAdd(data.message || (complete ? "执行完成" : "执行尚未完成"), scheduledFor ? "warn" : complete ? "done" : "stop");
  snapshotTrace();
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
  state.draft = null;
  state.draftEditing = false;
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

function conversationStateLabel(stateName, statusKind) {
  if (stateName === "blocked") return statusKind === "triage" ? "已说明" : "已拦截";
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
        const biz = item.business ? `<span class="conv-row__biz">${esc(item.business)}</span>` : "";
        const status = conversationStateLabel(item.state, item.status_kind);
        const meta = [
          conversationTimeLabel(item.last_active_at, label),
          item.ref ? `记录 ${item.ref}` : "",
          `${Number(item.message_count) || 0} 条`,
        ].filter(Boolean).join(" · ");
        const active = item.session_id === SESSION_ID ? " conv-row--active" : "";
        return `<div class="conv-row${active}">
          <button class="conversation-record" type="button" data-open-session="${esc(item.session_id)}">
            <span class="conv-row__top">
              <span class="conv-row__title">${esc(title)}</span>
              ${status ? `<span class="conv-row__status">${esc(status)}</span>` : ""}
            </span>
            <span class="conv-row__meta">${biz}${biz && meta ? " · " : ""}${esc(meta)}</span>
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
    const head = item.business ? `${item.business} · ${title}` : title;
    return `<button class="conversation-record" type="button" data-open-session="${esc(item.session_id)}">${esc(head)}</button>`;
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
    /* 只读历史里禁用输入框与发送：文案说「需新建对话」，就不能让人在同一页继续发 */
    state.archiveReadOnly = true;
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

/* 「我的」里的渠道状态卡：只列 Web / 飞书两行，数据全部来自 /api/channels/health。
   configured=false 就写「未配置」，不把 stub / replay 说成可用；读不到时如实报错。 */
const CHANNEL_CARD_LABELS = { web: "Web", feishu: "飞书" };

function channelStatusLabel(name, item, stats) {
  if (item.configured === false) {
    const replay = item.mode === "replay" || item.reason === "replay_mode" || stats.replay_mode;
    return replay ? "未绑定 · replay" : "未绑定";
  }
  if (name === "web") return "正常";
  return channelStateLabel(item, stats);
}

function channelStatusHtml(data) {
  if (!data) {
    return '<p class="channel-status__note">渠道状态读取失败，点击刷新重试</p>';
  }
  const channels = data.channels || {};
  const health = data.health || {};
  const rows = CHANNEL_ORDER.map((name) => {
    const item = channels[name] || {};
    const state = channelStatusLabel(name, item, health[name] || {});
    return `<p class="channel-status__row"><span class="channel-status__name">${esc(CHANNEL_CARD_LABELS[name] || name)}</span>` +
      `<span class="channel-status__state">${esc(state)}</span></p>`;
  });
  return rows.join("");
}

function renderProfile(me, audit, channelHealth) {
  auditCache = audit && audit.available !== false
    ? audit
    : { events: [], chain_ok: false, chain_head: "", available: false };
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
  /* 绑定 / 解绑按钮进标题行右对齐；正文只放状态说明与绑定码结果 */
  let feishuAction = "";
  let feishuBody = "";
  if (!feishu) {
    feishuBody = '<p class="muted">渠道信息读取失败，请稍后重试。</p>';
  } else if (!feishu.configured) {
    const replay = feishu.replay === true || feishu.mode === "replay";
    const state = `${feishu.linked ? "已绑定" : "未绑定"}${replay ? " · replay" : ""}`;
    feishuBody = `<p class="muted"><b>${esc(state)}</b>：尚未配置飞书应用凭据，当前只能回放历史事件；配好后可绑定并直接办理。</p>`;
  } else if (feishu.linked) {
    feishuAction = '<button class="btn btn--sm btn--ghost" id="btn-feishu-unlink" type="button">解除绑定</button>';
    feishuBody = '<p class="muted">已连接：可以直接在飞书里对「AI Banking」机器人说「查余额」「给李娜转五百」。</p>';
  } else {
    feishuAction = '<button class="btn btn--sm" id="btn-feishu-link" type="button">连接飞书</button>';
    feishuBody = '<p class="muted">未连接：点「连接飞书」生成 6 位绑定码，再到飞书里把这 6 位数字发给机器人。</p>';
  }
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
      <div class="profile-card__head">
        <h3>渠道状态</h3>
        <button class="btn btn--sm" type="button" data-channel-refresh>刷新</button>
      </div>
      <div class="channel-status" data-channel-status>${channelStatusHtml(channelHealth)}</div>
    </section>
    <section class="profile-card">
      <div class="profile-card__head">
        <h3>飞书渠道</h3>
        ${feishuAction}
      </div>
      <p class="profile-card__note">绑定后可在飞书里直接办理；未绑定只影响飞书入口，网页端不受影响。</p>
      <div class="profile-card__body" id="feishu-link-result">${feishuBody}</div>
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
    <section class="profile-card">
      <div class="profile-card__head">
        <h3>审计与溯源</h3>
        <button class="btn btn--sm" type="button" data-audit-open>查看审计</button>
      </div>
      <p class="muted">哈希链${audit.chain_ok ? "校验通过" : "状态未知"} · 最近 ${(audit.events || []).length} 条事件；可按 execution_id 追到 record_id / content_hash。</p>
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
    const [me, audit, channelHealth] = await Promise.all([
      apiFetch("GET", "/api/me"),
      apiFetch("GET", "/api/audit?limit=8"),
      /* 渠道状态失败不拖垮整页：卡片里如实显示「读取失败」，不编造状态 */
      apiFetch("GET", "/api/channels/health").catch(() => ({ ok: false })),
    ]);
    if (!me.ok) {
      if (target) target.innerHTML = '<p class="muted">账户信息读取失败，请稍后重试。</p>';
      return;
    }
    renderProfile(
      me.data || {},
      audit.ok ? audit.data || {} : { available: false },
      channelHealth.ok ? channelHealth.data || {} : null,
    );
  } catch (_) {
    if (target) target.innerHTML = '<p class="muted">账户信息读取失败，请稍后重试。</p>';
  }
}

/* 「渠道状态」卡的刷新：只重拉渠道健康并重绘卡片，不动身份 / 登录等其它卡片 */
async function refreshProfileChannels() {
  const boxes = Array.from(document.querySelectorAll("[data-channel-status]"));
  const buttons = Array.from(document.querySelectorAll("[data-channel-refresh]"));
  boxes.forEach((box) => { box.innerHTML = '<p class="channel-status__note">正在读取渠道状态…</p>'; });
  buttons.forEach((button) => { button.disabled = true; });
  let data = null;
  try {
    const result = await apiFetch("GET", "/api/channels/health");
    if (result.ok) data = result.data || {};
  } catch (_) {
    /* 读取失败走下面的「读取失败」文案 */
  }
  boxes.forEach((box) => { box.innerHTML = channelStatusHtml(data); });
  buttons.forEach((button) => { button.disabled = false; });
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
    { key: "amount", label: "预留金额（元）", type: "number", placeholder: "800" },
    { key: "merchant", label: "商户（演示）", type: "text", placeholder: "鲜花礼品店" },
    { key: "account", label: "预留账户", type: "account" },
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
  ["reserve", "预留"],
  ["order", "下单"],
  ["release", "释放"],
];

/* 资金预留进度：三段是否点亮由服务端按**本事件自己的** hold / 下单回执算出；
   没有 hold 时不再显示「已释放 · 冻结余额」，只说明「尚未执行」。 */
function lifeBudgetHtml(budget, status) {
  if (!budget || typeof budget !== "object") {
    return status === "planned"
      ? '<p class="muted life-budget__idle">资金预留：尚未执行（确认后才预留与下单）</p>'
      : "";
  }
  const steps = budget.steps || {};
  const segments = LIFE_BUDGET_STEPS.map(
    ([key, label]) => `<li class="life-budget__seg${steps[key] ? " is-done" : ""}">${label}</li>`,
  ).join("");
  const released = budget.state === "released";
  const amount = typeof budget.amount_cents === "number" ? ` · 剩余 ${yuan(budget.amount_cents)}` : "";
  const state = released ? "预留已释放" : "剩余预留";
  const note = released ? "，已退回可用余额" : " · 目标完成或终止后释放";
  return `<div class="life-budget">
        <ol class="life-budget__bar" aria-label="资金预留执行进度">${segments}</ol>
        <p class="muted life-budget__meta">${esc(state)}${esc(amount)}${esc(note)}</p>
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
const lifeCal = { month: "", day: "", records: [], maturities: [] };

function lifeMonthKey(date) {
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}`;
}

function lifeDateKey(value) {
  const text = String(value || "");
  /* 带时间戳的 ISO 一律按**本地时区**归日：服务端存 UTC，直接切前 10 位会把
     11-01 01:39（UTC+8）算到 10-31，日历与列表就对不上。纯日期字符串不转换。 */
  if (/T|\s\d{2}:\d{2}/.test(text)) {
    const parsed = new Date(text);
    if (!Number.isNaN(parsed.getTime())) {
      const pad = (n) => String(n).padStart(2, "0");
      return `${parsed.getFullYear()}-${pad(parsed.getMonth() + 1)}-${pad(parsed.getDate())}`;
    }
  }
  return /^\d{4}-\d{2}-\d{2}/.test(text) ? text.slice(0, 10) : "";
}

function lifeShiftMonth(month, delta) {
  const [year, mon] = month.split("-").map(Number);
  return lifeMonthKey(new Date(year, mon - 1 + delta, 1));
}

/* 三类点各有来源，不在前端猜：
   - `maturity`：理财到期（到期日按产品条款确定：起息日 + 期限）；
   - `reminder`：记录关联的预约是提醒类（服务端给的 `schedule_kind`）；
   - `order`：其余下单记录。
   **同一天同类只画一个点**；「N 个事件」按唯一 record_id 计，与点一致。 */
const LIFE_CAL_KINDS = ["order", "reminder", "maturity"];
const LIFE_CAL_DOT_TITLE = {
  order: "下单", reminder: "提醒", maturity: "理财到期（按产品条款）",
};

function lifeRecordScheduleKind(record) {
  return record && record.schedule_kind === "reminder" ? "reminder" : "order";
}

/* 到期日已过时追加灰化类名；状态由服务端按「到期日 vs 今天」给出。 */
function maturityDotClass(item) {
  const past = item.status === "expired" ? " life-cal__dot--maturity-past" : "";
  return `life-cal__dot life-cal__dot--maturity${past}`;
}

function maturityDotTitle(item) {
  return item.status === "expired"
    ? "理财到期（按产品条款，已到期）" : LIFE_CAL_DOT_TITLE.maturity;
}

/* 日历点开某天时必须能看清「哪只产品到期」：只画点不写名字，等于没告诉用户。 */
function maturityRowHtml(item) {
  const expired = item.status === "expired";
  const amount = typeof item.amount_cents === "number" ? yuan(item.amount_cents) : "金额未知";
  const term = typeof item.term_days === "number" ? `期限 ${item.term_days} 天` : "期限未知";
  return `<li class="life-record-step">
      <span class="life-record-step__no"><i class="${maturityDotClass(item)}"
        title="${esc(maturityDotTitle(item))}"></i></span>
      <span class="life-record-step__body">
        <b>理财到期：${esc(item.name || item.product_id || "理财产品")}</b>
        <span class="muted">${esc(amount)} · ${esc(item.purchased_on || "起息日未知")} 起息
          · ${esc(term)} · 到期日 ${esc(item.maturity_on || "—")}${expired ? " · 已到期" : ""}</span>
        <span class="muted">${esc(item.basis || "按产品条款确定（起息日 + 期限）；兑付到账以银行回单为准")}</span>
      </span></li>`;
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
  const seen = new Set();
  records.forEach((record) => {
    /* 落点优先级：预约触发日 > 计划日期 > 创建日；同一 record_id 只算一次 */
    const key = lifeDateKey(record.calendar_on || record.updated_at || record.created_at);
    const recordId = record.record_id || "";
    if (!key || key.slice(0, 7) !== month || (recordId && seen.has(recordId))) return;
    if (recordId) seen.add(recordId);
    const bucket = (byDay[key] = byDay[key] || { kinds: new Set(), records: [], maturities: [] });
    bucket.records.push(record);
    bucket.kinds.add(lifeRecordScheduleKind(record));
  });
  lifeCal.maturities.forEach((item) => {
    const key = String(item.maturity_on || "").slice(0, 10);
    if (!key || key.slice(0, 7) !== month) return;
    const bucket = (byDay[key] = byDay[key] || { kinds: new Set(), records: [], maturities: [] });
    bucket.maturities.push(item);
    bucket.kinds.add("maturity");
  });
  const cells = [];
  for (let i = 0; i < lead; i += 1) {
    cells.push('<li class="life-cal__cell--blank" aria-hidden="true"></li>');
  }
  for (let day = 1; day <= days; day += 1) {
    const key = `${month}-${String(day).padStart(2, "0")}`;
    const bucket = byDay[key];
    const items = bucket ? bucket.records : [];
    const dayMaturities = bucket ? bucket.maturities : [];
    const picked = key === lifeCal.day;
    const dots = bucket ? LIFE_CAL_KINDS.filter((kind) => bucket.kinds.has(kind)).map((kind) => {
      const titles = kind === "maturity"
        ? dayMaturities.map((item) => item.name || item.product_id)
        : items.filter((record) => lifeRecordScheduleKind(record) === kind)
          .map((record) => record.event_title || record.event_name || record.event_id);
      return `<i class="life-cal__dot life-cal__dot--${kind}"
        title="${esc(LIFE_CAL_DOT_TITLE[kind])}：${esc(titles.join("、"))}"></i>`;
    }).join("") : "";
    const marked = items.length > 0 || dayMaturities.length > 0;
    const marks = [marked ? "is-marked" : "", key === today ? "is-today" : "", picked ? "is-picked" : ""]
      .filter(Boolean).join(" ");
    cells.push(`<li>
        <button class="life-cal__cell${marks ? ` ${marks}` : ""}" type="button" data-life-day="${key}"
          aria-pressed="${picked ? "true" : "false"}"
          aria-label="${key}${items.length ? `，${items.length} 个事件` : "，无事件"}${
            dayMaturities.length ? `，${dayMaturities.length} 笔${LIFE_CAL_DOT_TITLE.maturity}` : ""}">
          <span class="life-cal__day">${day}</span><span class="life-cal__dots">${dots}</span>
        </button></li>`);
  }
  const monthRecords = Object.keys(byDay).sort().reverse()
    .flatMap((key) => byDay[key].records);
  const other = records.length - monthRecords.length;
  const dayRecords = lifeCal.day ? (byDay[lifeCal.day] ? byDay[lifeCal.day].records : []) : monthRecords;
  /* 到期日不是「生活事件记录」：单列一块，把产品名 / 金额 / 起息 / 期限写清楚。
     选某天时只看当天；没选时看本月全部。 */
  const scopeMaturities = lifeCal.maturities.filter((item) => (
    lifeCal.day
      ? item.maturity_on === lifeCal.day
      : String(item.maturity_on || "").slice(0, 7) === month
  ));
  const maturityBlock = scopeMaturities.length
    ? `<section class="life-cal__maturity">
        <p class="life-cal__filter"><b>理财到期</b> · ${scopeMaturities.length} 笔
          <span class="muted">到期日按产品条款确定（起息日 + 期限）；兑付到账以银行回单为准</span></p>
        <ul class="life-cal__list">${scopeMaturities.map(maturityRowHtml).join("")}</ul>
      </section>`
    : "";
  const filter = lifeCal.day
    ? `<p class="life-cal__filter">已选 <b>${esc(lifeCal.day)}</b> · ${dayRecords.length} 个事件
        <button class="life-cal__clear" type="button" data-life-day="">显示整月</button></p>`
    : "";
  const rows = dayRecords.length
    ? `<ul class="life-cal__list">${dayRecords.map(lifeRecordRowHtml).join("")}</ul>`
    : `<p class="muted">${lifeCal.day
      ? `${esc(lifeCal.day)} 没有生活事件记录。`
      : "本月没有生活事件记录。"}${scopeMaturities.length ? "（上方为当天 / 本月到期的理财）" : ""}</p>`;
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
      <p class="life-cal__legend">
        <span><i class="life-cal__dot life-cal__dot--order"></i>下单</span>
        <span><i class="life-cal__dot life-cal__dot--reminder"></i>提醒</span>
        <span><i class="life-cal__dot life-cal__dot--maturity"></i>理财到期（到期日按产品条款）</span>
        <span class="muted">同一天同类只画一个点，点开看清单</span>
      </p>
      ${filter}
      ${maturityBlock}
      ${rows}
      ${otherNote}
    </div>`;
}

/* 日历下方的一行摘要：日期 + 标题 + 状态；不再挂「原型 / 组合律」这类开发标签。 */
function lifeRecordRowHtml(record) {
  const label = lifeStatusLabel(record);
  const title = record.event_title || record.event_name || record.event_id;
  const amount = typeof record.amount_cents === "number" ? ` ${yuan(record.amount_cents)}` : "";
  return `<li data-life-record="${esc(record.record_id || "")}" role="button" tabindex="0"
    aria-label="查看详情：${esc(title)}"><span class="life-cal__date">${
      esc(lifeDateKey(record.calendar_on || record.updated_at || record.created_at).slice(5))
    }</span>
    <span class="life-cal__name" title="${esc(record.event_name || record.event_id)}">${esc(title)}</span>
    <span class="chip">${esc(label)}</span><span class="muted">${esc(amount)}</span></li>`;
}

/* ---------------- 生活事件卡片（一条事件一张卡：原话 → 任务图 → 核查 → 状态） ---------------- */

function lifeStatusLabel(record) {
  if (record && record.schedule) {
    return record.schedule.kind === "reminder" ? "提醒已登记" : "已登记预约";
  }
  return LIFE_EVENT_STATUS[record.status] || record.status;
}

/* 任务图：节点顺序与依赖都来自服务端步骤快照，前端不拼步骤、不猜动作。 */
function lifeTaskGraphHtml(steps) {
  const list = Array.isArray(steps) ? steps.filter((step) => step && step.action) : [];
  if (!list.length) return "";
  const nodes = list.map((step, index) => {
    const amount = typeof step.amount_cents === "number" ? `${yuan(step.amount_cents)}` : "";
    const dep = Array.isArray(step.depends_on) && step.depends_on.length ? " · 依赖上一步" : "";
    const tag = LIFE_DOMAIN_LABEL[step.domain] || "场景";
    return `<span class="life-dag__node" title="${esc(step.action)}">
      <span class="life-dag__no">${index + 1}</span>
      <span class="life-dag__body"><b>${esc(step.label || step.action)}</b>
        <span class="muted">${esc(amount)}${esc(dep)}</span></span>
      <span class="life-dag__tag">${esc(tag)}</span>
    </span>`;
  });
  return `<div class="life-dag" role="list" aria-label="任务图">${
    nodes.join('<span class="life-dag__arrow" aria-hidden="true">→</span>')}</div>`;
}

/* 资金核查：ok 只写一句事实；later / short 才黄条，并给出三条可选路径。 */
function lifeFundingHtml(funding) {
  if (!funding || !funding.status || funding.status === "unknown") return "";
  if (funding.status === "ok") {
    return `<p class="life-funding is-ok">资金核查：${esc(funding.message || "可用余额可覆盖")}</p>`;
  }
  return `<p class="life-funding is-warn" role="status">
      <b>资金核查</b>：${esc(funding.message || "可用资金不足")}
      <span class="muted">可选：提前赎回 / 改日期 / 换资金来源</span></p>`;
}

function lifeOrdersHtml(orders) {
  if (!Array.isArray(orders) || !orders.length) return "";
  return orders.map((order) => `<p class="life-order">订单：${esc(order.merchant || "商户")}
    · 单号 <span class="tech">${esc(order.order_id || "—")}</span>
    · ${yuan(order.amount_cents || 0)} · 下单日 ${esc(order.order_on || "—")}</p>`).join("");
}

/* 定期出资：收款人 / 执行日来自计划步骤与名册；终止只对已登记的转账开放。 */
function lifeScheduleTransferHtml(record) {
  const step = (record.steps || []).find((item) => item && item.action === "transfer.schedule");
  if (!step) return "";
  const ids = (record.results || [])
    .filter((item) => item && item.action === "transfer.schedule")
    .flatMap((item) => (Array.isArray(item.record_ids) ? item.record_ids : []))
    .filter((id) => typeof id === "string" && id.startsWith("scheduled_transfers:"))
    .map((id) => id.split(":")[1]);
  const scheduleId = ids[0] || "";
  const payee = step.payee_label ? `收款人 ${step.payee_label}` : "收款人见计划卡";
  const when = step.date ? `执行日 ${step.date}` : "执行日未定";
  const executed = record.status === "executed";
  const state = executed ? "已转出" : "待转出";
  const cycle = executed ? "本次已扣款，不自动续期" : "本次仅登记下一笔，不自动续期";
  return `<p class="muted life-record__l2">定期出资：${esc(payee)} · ${esc(when)}
      · ${state} · ${cycle}</p>${
    scheduleId
      ? `<div class="life-record__actions"><button class="life-send" type="button"
          data-life-cancel-transfer="${esc(scheduleId)}">终止登记</button></div>`
      : ""}`;
}

/* 一条事件一张卡：同一事件的预约折叠在这里，不再在「已登记预约」里重复一份。 */
function lifeRecordCardHtml(record) {
  const title = record.event_title || record.event_name || record.event_id;
  const privacy = record.visibility === "private"
    ? '<span class="chip chip--yellow">仅本人可见</span>' : "";
  const utterance = record.utterance
    ? `<p class="life-record__utterance">原话：「${esc(record.utterance)}」</p>` : "";
  const trigger = record.schedule && record.schedule.due_on
    ? `触发日 ${esc(record.schedule.due_on)}`
    : (record.due_on ? `计划日期 ${esc(record.due_on)}` : "");
  const amount = typeof record.amount_cents === "number"
    ? `金额 ${yuan(record.amount_cents)}` : "";
  const meta = [trigger, amount, `创建于 ${momentLabel(record.created_at)}`]
    .filter(Boolean).join(" · ");
  const notify = record.schedule && record.schedule.notify_on
    ? ` · ${esc(record.schedule.notify_on)} 提前提醒` : "";
  return `<article class="takeover insight-item life-record" data-life-record="${esc(record.record_id || "")}">
      <div class="life-record__head">
        <b>${esc(title)}</b>
        <span class="chip">${esc(lifeStatusLabel(record))}</span>${privacy}
      </div>
      ${utterance}
      ${lifeTaskGraphHtml(record.steps)}
      <p class="muted life-record__meta">${esc(meta)}${notify}</p>
      ${lifeFundingHtml(record.funding)}
      ${lifeBudgetHtml(record.budget, record.status)}
      ${lifeOrdersHtml(record.orders)}
      ${lifeScheduleTransferHtml(record)}
      <div class="life-record__actions">
        <button class="life-send" type="button" data-life-open="${esc(record.session_id || "")}"
          aria-label="打开会话：${esc(title)}">打开会话</button>
      </div>
    </article>`;
}

/* 生活事件记录只读回放：步骤来自计划快照，状态按记录整体状态派生，不重新执行。 */
function openLifeRecord(recordId) {
  const record = (lifeData.records || []).find((item) => item.record_id === recordId);
  const dialog = el("life-record-dialog");
  const body = el("life-record-body");
  if (!record || !dialog || !body) return;
  const statusLabel = record.schedule
    ? (record.schedule.kind === "reminder"
      ? `提醒已登记（${record.schedule.due_on}）`
      : `已登记预约（触发日 ${record.schedule.due_on}）`)
    : (LIFE_EVENT_STATUS[record.status] || record.status);
  const stepState = record.schedule ? "已登记"
    : record.status === "executed" ? "已执行"
      : record.status === "planned" ? "待确认"
        : record.status === "rejected" ? "已拒绝" : "失败";
  const title = record.event_title || record.event_name || record.event_id;
  const steps = Array.isArray(record.steps) ? record.steps : [];
  const stepRows = steps.length
    ? steps.map((step, index) => {
      const amount = typeof step.amount_cents === "number" ? yuan(step.amount_cents) : "—";
      const when = step.date ? ` · ${esc(String(step.date))}` : "";
      const payee = step.payee_label ? ` · 收款人 ${esc(step.payee_label)}` : "";
      const tag = LIFE_DOMAIN_LABEL[step.domain] || "场景";
      return `<li class="life-record-step">
        <span class="life-record-step__no">${index + 1}</span>
        <span class="life-record-step__body">
          <b>${esc(String(step.label || step.action || "步骤"))}</b>
          <span class="muted">${amount}${when}${payee} · ${esc(tag)} · ${stepState}</span>
        </span></li>`;
    }).join("")
    : '<li class="muted">这一版没有保存步骤快照（旧版本记录）；已知信息见上方金额与计划日期。</li>';
  const amountNote = typeof record.amount_cents === "number"
    ? ` · 金额 ${yuan(record.amount_cents)}` : "";
  const utterance = record.utterance ? `<p class="muted">原话：「${esc(record.utterance)}」</p>` : "";
  body.innerHTML = `<p class="life-record-detail__name">${esc(title)}</p>
    ${utterance}
    <p class="muted">${esc(momentLabel(record.created_at))} 创建 · ${esc(statusLabel)}${
      amountNote
    }${record.due_on ? ` · 计划日期 ${esc(record.due_on)}` : ""}</p>
    <ul class="life-record-steps">${stepRows}</ul>
    ${lifeFundingHtml(record.funding)}
    ${lifeOrdersHtml(record.orders)}
    ${steps.length ? '<p class="muted">只读回放：展示历史计划的步骤与金额，不会重新执行。</p>' : ""}`;
  if (typeof dialog.showModal === "function") dialog.showModal();
  else dialog.setAttribute("open", "");
}

/* ---------------- 组合任务台（L1 选事件 + 参数填写 + L4 日历入口） ---------------- */
/* 原型全部来自服务端；L1 先选一个事件，选中后展开该事件的步骤与参数，
   参数折叠展开后才出现「开始组合」，避免还没填就误触。 */
const lifePick = { event: "" };
const lifeData = { prototypes: [], records: [], schedules: [] };

/* 已登记预约行：到期日 / 类型 / 冻结的步骤都在服务端算好，前端只渲染。
   到期后会由 `/api/due` 重新生成待确认提案，所以这里明确写「不会自动扣款」。 */
function lifeScheduleRowHtml(item) {
  const title = item.event_title || item.event_id;
  const kind = item.kind === "reminder" ? "提醒" : "到期下单";
  const steps = Array.isArray(item.steps) ? item.steps : [];
  const due = item.due_on ? `触发日 ${esc(item.due_on)}` : "触发日未定";
  const notify = item.notify_on ? ` · ${esc(item.notify_on)} 提前提醒` : "";
  return `<article class="takeover insight-item life-record">
      <div class="life-record__head">
        <span class="life-record__kind">已登记预约</span>
        <b>${esc(title)}</b>
        <span class="chip">${esc(kind)}</span>
      </div>
      <div class="life-record__meta">
        <p class="muted">${due}${notify} · ${steps.length} 步 · 到期后需再次确认，不会自动扣款</p>
        <button class="life-send" type="button" data-life-open="${esc(item.session_id || "")}"
                aria-label="打开会话：${esc(title)}">打开会话</button>
      </div>
    </article>`;
}

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
      <div class="life-detail__head"><b>${esc(name)}</b>${steps}</div>
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
  const privateRecords = Array.isArray(payload.private_records) ? payload.private_records : [];
  const schedules = Array.isArray(payload.schedules) ? payload.schedules : [];
  /* 预约已折叠进记录卡：这里只统计「关联不上记录」的孤儿预约 */
  const linkedRecords = new Set(
    [...records, ...privateRecords].map((record) => record.record_id),
  );
  const orphanSchedules = schedules.filter(
    (item) => !item.record_id || !linkedRecords.has(item.record_id),
  );
  const recordsNote = payload.records_available === false
    ? "本部署未开启记录"
    : `我的记录 ${records.length + privateRecords.length} 条`;
  const scheduleNote = orphanSchedules.length ? ` · 待关联预约 ${orphanSchedules.length} 条` : "";
  status.textContent = `${recordsNote}${scheduleNote}`;

  /* 日历是用户自己的：私密事件也落点（只是默认折叠、不进共享推送） */
  lifeCal.records = [...records, ...privateRecords];
  lifeCal.maturities = Array.isArray(payload.maturities) ? payload.maturities : [];
  if (calendar) calendar.innerHTML = lifeCalendarHtml(lifeCal.records);
  lifeData.prototypes = prototypes;
  lifeData.records = [...records, ...privateRecords];
  lifeData.schedules = schedules;
  if (!lifePick.event && prototypes.length) {
    lifePick.event = typeof prototypes[0].event_id === "string" ? prototypes[0].event_id : "";
  }
  if (body) renderLifeWorkbench();

  const warnings = [...records, ...privateRecords].filter(
    (record) => record.funding
      && (record.funding.status === "later" || record.funding.status === "short"),
  );
  const conflicts = Array.isArray(payload.date_conflicts) ? payload.date_conflicts : [];
  const alertItems = [
    ...warnings.map((record) => `${esc(
      record.event_title || record.event_name || record.event_id,
    )}：${esc(record.funding.message || "可用资金不足")}`),
    ...conflicts.map((item) => `「${esc(item.keyword || "同一场合")}」相关事件出现多个用钱日：
      ${esc((item.dates || []).join(" / "))}（${esc((item.titles || []).join("、"))}），
      请确认哪个是实际用钱日`),
  ];
  const alertBlock = alertItems.length
    ? `<section class="life-alert" role="status">
        <p><b>资金核查：${alertItems.length} 条需要你确认</b>
          <span class="muted">计划金额 / 日期与可用资金、到期日不匹配</span></p>
        <ul>${alertItems.map((text) => `<li>${text}</li>`).join("")}</ul>
      </section>`
    : "";
  const recordRows = records.length
    ? records.map(lifeRecordCardHtml).join("")
    : '<p class="muted">还没有生活事件记录；发起后会出现在这里。</p>';
  /* 只有关联不上记录的旧预约才单独兜底；正常预约已经折叠进上面的事件卡。 */
  const scheduleBlock = orphanSchedules.length
    ? `<section class="life-block">
        <div class="life-block__head"><b>已登记预约（未关联记录）</b>
          <span class="muted">到期后重新生成待确认提案，不会自动扣款</span></div>
        <div class="life-records">${orphanSchedules.map(lifeScheduleRowHtml).join("")}</div>
      </section>`
    : "";
  /* 私密事件默认折叠：不隐藏（用户自己要看得到），但不进共享推送与普通列表。 */
  const privateBlock = privateRecords.length
    ? `<details class="life-private">
        <summary>仅本人可见的事件 ${privateRecords.length} 条（默认折叠，不进共享推送）</summary>
        <div class="life-records">${privateRecords.map(lifeRecordCardHtml).join("")}</div>
      </details>`
    : "";
  list.innerHTML = `${alertBlock}<div class="life-records">${recordRows}</div>${privateBlock}${scheduleBlock}`;
}

/* 终止定期出资登记：登记时不扣款，终止只改状态，不动资金（与转账页同一接口）。 */
async function cancelLifeTransfer(scheduleId) {
  if (!scheduleId) return;
  const result = await post("/api/transfers/cancel", { schedule_id: scheduleId });
  if (result.status !== 200) {
    timelineAdd(
      String((result.data && result.data.detail) || "终止失败，请稍后重试。"), "error",
    );
    return;
  }
  timelineAdd("已终止这笔定期出资登记（登记时不扣款，终止不动资金）", "live");
  await refreshLifeEvents();
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
let auditCache = { events: [], chain_ok: false, chain_head: "", available: false };

/* 审计详情：时间倒序 + 每条事件的 payload（含 record_id / content_hash / execution_id）。 */
function openAuditDialog() {
  const dialog = el("audit-dialog");
  const body = el("audit-body");
  if (!dialog || !body) return;
  const events = Array.isArray(auditCache.events) ? auditCache.events : [];
  const rows = events.map((event) => {
    const payload = JSON.stringify(event.payload || {}, null, 2);
    return `<li><p><b>${esc(profileValue(event.kind))}</b>
      <span class="mono">${esc(momentLabel(event.timestamp))}</span></p>
      <p class="muted mono">event_id ${esc(profileValue(event.event_id))}</p>
      <details><summary>payload（可查 record_id / content_hash）</summary>
        <pre class="mono audit-digest">${esc(payload)}</pre></details></li>`;
  }).join("");
  body.innerHTML = `<p class="muted">链头 ${esc(profileValue(auditCache.chain_head) || "—")} · ${
    auditCache.chain_ok ? "哈希链校验通过" : "哈希链未校验"
  }</p>` + (rows ? `<ul class="profile-trail">${rows}</ul>` : '<p class="muted">暂无审计事件。</p>');
  if (typeof dialog.showModal === "function") dialog.showModal();
  else dialog.setAttribute("open", "");
}

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
  const cancelTransfer = target.closest("[data-life-cancel-transfer]");
  if (cancelTransfer) {
    cancelTransfer.disabled = true;
    try {
      await cancelLifeTransfer(cancelTransfer.dataset.lifeCancelTransfer || "");
    } finally {
      cancelTransfer.disabled = false;
    }
    return;
  }
  const open = target.closest("[data-life-open]");
  if (open) {
    closeLifeWorkbench();
    await openConversation(open.dataset.lifeOpen || "");
    return;
  }
  const retry = target.closest("[data-life-retry]");
  if (retry) {
    retry.disabled = true;
    try {
      await onRetrySchedule(retry.dataset.lifeRetry || "");
    } finally {
      retry.disabled = false;
    }
    return;
  }
  const record = target.closest("[data-life-record]");
  if (record) {
    openLifeRecord(record.dataset.lifeRecord || "");
  }
});
el("btn-life-workbench")?.addEventListener("click", openLifeWorkbench);
el("btn-life-record-close")?.addEventListener("click", () => closeDialog(el("life-record-dialog")));
el("btn-wealth-assessment-close")?.addEventListener("click", closeWealthAssessment);
el("btn-life-workbench-close")?.addEventListener("click", closeLifeWorkbench);
/* 五个弹窗都支持「点外面关闭」 */
[el("life-workbench"), el("life-record-dialog"), el("wealth-assessment"),
  el("subscription-cancel-dialog"), el("sub-detail"), el("audit-dialog"),
  el("app-dialog")].forEach(bindDialogDismiss);
/* 审计入口：我的页「查看审计」与弹窗关闭 */
document.addEventListener("click", (event) => {
  const target = event.target instanceof Element ? event.target : null;
  if (!target) return;
  if (target.closest("[data-audit-open]")) {
    openAuditDialog();
    return;
  }
  if (target.closest("[data-audit-close]")) closeDialog(el("audit-dialog"));
});
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
  if (target.closest("[data-channel-refresh]")) {
    await refreshProfileChannels();
    return;
  }
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
    if (el("app")?.classList.contains("is-scenes-open")) {
      setMobileDrawer("scenes", false);
      syncColumnButtons();
    }
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
el("btn-abandon-task")?.addEventListener("click", action(async () => {
  const next = state.pendingStart;
  state.pendingStart = null;
  el("task-guard").hidden = true;
  /* 同一会话放弃：旧计划保留为只读记录，新消息继续用当前 SESSION_ID。
     后端在下一次 /api/submit 时会重置待确认计划，旧计划的冻结哈希随即失效。 */
  archiveTask(true);
  reset(true);
  if (next) await next();
}));
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
  el("btn-history-mobile")?.setAttribute("aria-expanded", String(navOpen));
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
el("btn-history-mobile")?.addEventListener("click", toggleNav);
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
  const draftTrigger = target ? target.closest("[data-draft-action]") : null;
  if (draftTrigger) {
    const draftAction = draftTrigger.dataset.draftAction;
    if (draftAction === "edit") action(openDraftEdit)();
    else if (draftAction === "cancel") action(cancelDraftEdit)();
    else if (draftAction === "save") action(saveDraftEdit)();
    return;
  }
  const reviewTrigger = target ? target.closest("[data-review-action]") : null;
  if (reviewTrigger) {
    const reviewAction = reviewTrigger.dataset.reviewAction;
    if (reviewAction === "confirm") action(confirmReview)();
    else if (reviewAction === "edit") action(openReviewEdit)();
    else if (reviewAction === "cancel-edit") action(cancelReviewEdit)();
    else if (reviewAction === "save") runReviewSave().catch(() => {});
    return;
  }
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

/* 问卷表单在「面板」和「聊天内」两处动态渲染，故用文档级事件委托。
   点「提交测评」只展开二次确认条；真正落库要再过一次「确认提交」（实测 WEA-P08②）。 */
document.addEventListener("click", async (event) => {
  const target = event.target instanceof Element ? event.target : null;
  if (!target) return;
  const back = target.closest("[data-assessment-back]");
  if (back) {
    closeAssessmentConfirm(back.closest("[data-assessment-confirm]"));
    return;
  }
  const reconfirm = target.closest('[data-action="assessment-reconfirm"]');
  if (reconfirm) {
    submitAssessmentReconfirm(reconfirm).catch(() => {});
    return;
  }
  const confirm = target.closest('[data-action="assessment-confirm"]');
  const submit = target.closest('[data-action="submit-assessment"]');
  const trigger = confirm || submit;
  if (!trigger) return;
  const form = trigger.closest("[data-assessment-form]");
  if (!form || form.dataset.busy === "1") return;
  if (submit) {
    /* 先校验再确认：缺题时不该让人先确认一遍才报错 */
    openAssessmentConfirm(submit);
    return;
  }
  closeAssessmentConfirm(form.querySelector("[data-assessment-confirm]"));
  form.dataset.busy = "1";
  trigger.disabled = true;
  try {
    await onAssessmentSubmit(form);
  } catch (_) {
    assessmentFormError(form, "提交失败：未收到服务端回执，请稍后重试。");
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
  if (target.closest("[data-wealth-cat]")) {
    /* 推荐视图的分类筛选：只隐藏卡片，不改服务端结果 */
    const chip = target.closest("[data-wealth-cat]");
    const cat = chip.dataset.wealthCat || "";
    document.querySelectorAll("[data-wealth-cat]").forEach((node) => {
      node.setAttribute("aria-pressed", node === chip ? "true" : "false");
    });
    document.querySelectorAll("[data-wealth-card-cat]").forEach((row) => {
      row.hidden = Boolean(cat) && row.dataset.wealthCardCat !== cat;
    });
    return;
  }
  if (target.closest("[data-wealth-compare-open]")) {
    openWealthCompare();
    return;
  }
  if (target.closest("[data-wealth-compare-clear]")) {
    state.wealthCompare = [];
    document.querySelectorAll("[data-wealth-compare]").forEach((input) => {
      input.checked = false;
    });
    renderWealthCompareBar();
    return;
  }
  if (target.closest("[data-scroll-wealth-holdings]")) {
    const section = el("wealth-holdings");
    const anchor = section && section.previousElementSibling ? section.previousElementSibling : section;
    if (anchor) anchor.scrollIntoView({ behavior: "smooth", block: "start" });
    return;
  }
  if (target.closest("[data-wealth-detail]")) {
    const node = target.closest("[data-wealth-detail]");
    openWealthProductDetail(node.dataset.wealthDetail, {
      fromChat: Boolean(node.closest("#active-response")),
    }).catch(() => {});
    return;
  }
  if (target.closest("[data-wealth-matched]")) {
    const node = target.closest("[data-wealth-matched]");
    focusMatchedWealthProducts(node.dataset.wealthMatched || "");
    return;
  }
  const trigger = target.closest("[data-wealth-subscribe], [data-wealth-redeem]");
  if (!trigger || trigger.disabled) return;
  try {
    await onWealthAction(trigger);
  } catch (_) {
    wealthError(trigger, "提交失败：未收到服务端回执，请稍后重试。");
  } finally {
    /* 卡片按钮直接起办理时不经过 `action()` 包装，需在此补一次按钮态：
       否则 `beginTask` 期间被隐藏的 #task-actions 不会恢复，确认按钮点不到。 */
    setButtons();
  }
});

document.addEventListener("change", (event) => {
  const target = event.target instanceof Element ? event.target : null;
  const input = target ? target.closest("[data-wealth-compare]") : null;
  if (!input) return;
  const id = input.dataset.wealthCompare || "";
  if (!id) return;
  const selected = new Set(state.wealthCompare || []);
  if (input.checked) {
    if (selected.size >= 3) {
      input.checked = false;
      return;
    }
    selected.add(id);
  } else {
    selected.delete(id);
  }
  state.wealthCompare = Array.from(selected);
  renderWealthCompareBar();
});

function focusMatchedWealthProducts(category) {
  const selector = category
    ? `#wealth-products .wealth-row[data-wealth-cat-row="${category}"]:not(.wealth-row--off)`
    : "#wealth-products .wealth-row:not(.wealth-row--off)";
  let rows = Array.from(document.querySelectorAll(selector));
  if (!rows.length && category) {
    rows = Array.from(
      document.querySelectorAll("#wealth-products .wealth-row:not(.wealth-row--off)")
    );
    if (rows.length) {
      timelineAdd("同类暂无可买产品，已标出当前可买的其他产品。", "warn");
    }
  }
  if (!rows.length) {
    timelineAdd("当前等级下没有匹配产品，请先重新测评或联系客服。", "warn");
    return;
  }
  rows.forEach((row) => row.classList.add("wealth-row--focus"));
  rows[0].scrollIntoView({ behavior: "smooth", block: "center" });
  window.setTimeout(() => {
    rows.forEach((row) => row.classList.remove("wealth-row--focus"));
  }, 3200);
}

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
    sub: "从本人账户转出，收款人需核对姓名、账号与开户行。",
    short: "付款账户、收款人三要素与到账方式",
    prefill: "我要转账",
  },
  bills: {
    title: "账单分析",
    sub: "收支结构、分类修正、商户排行与预算；异常线索只是补充，且必须高置信、有依据。",
    short: "收支、分类、商户与预算",
    prefill: "查一下上月账单",
  },
  wealth: {
    title: "理财",
    sub: "先看适合你的产品，也可以赎回已有持仓。",
    short: "选产品、看持仓、申购赎回",
    prefill: "我想买理财，帮我推荐几只适合的",
  },
  cards: {
    title: "卡片",
    sub: "锁卡 / 挂失 / 补卡 / 调额；红动作走强验证。",
    short: "锁卡 / 挂失 / 补卡 / 调额",
    prefill: "锁卡",
  },
  subscriptions: {
    title: "订阅",
    sub: "管理代扣协议：查询已签协议、关闭银行卡侧扣款授权；累计扣款与解约后对账可查。",
    short: "代扣协议查询与解约对账",
    prefill: "关闭扣款协议",
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
/* 旧服务进程的 HTML 还没有「账单明细」tab：脚本补一个，避免必须重启才能看到。
   新服务端已自带该 tab，这里等价 no-op。 */
function ensureRecordBillsTab() {
  const tabs = el("records-tabs");
  if (!tabs || tabs.querySelector('[data-record-tab="bills"]')) return;
  const button = document.createElement("button");
  button.className = "tab";
  button.type = "button";
  button.setAttribute("role", "tab");
  button.dataset.recordTab = "bills";
  button.setAttribute("aria-selected", "false");
  button.textContent = "账单明细";
  tabs.insertBefore(button, tabs.firstChild);
}
ensureRecordBillsTab();
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
      .filter((item) => item.kind === "current"
        || String(item.label || "").includes("活期"))
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
  bills: { label: "账单明细", hint: "与账单分析页同源的消费明细；转账 / 还款不计入消费，见转账记录。" },
  transfers: { label: "转账记录", hint: "每笔含收款人（脱敏）、账号尾号、方式、手续费、状态与用途；可再转一笔。" },
  holdings: { label: "持仓", hint: "理财申购后的持仓（产品 / 金额 / 起息日）；无净值数据时收益标「未知」。" },
  cards: { label: "卡片状态", hint: "锁卡 / 挂失 / 补卡后的最新状态与可执行动作。" },
  suspicions: { label: "可疑清单", hint: "只显示待核对的风险线索；按置信度分层，可确认正常 / 标记异常 / 忽略。" },
  charges: { label: "扣款记录", hint: "代扣 / 自动续费流水；近 12 期与全量汇总同源。" },
};

const RECORD_TAB_LOADERS = {
  bills: () => {
    const request = billsRequestQuery();
    return apiFetch("GET", `/api/bills/analysis${request.query}`);
  },
  transfers: () => apiFetch("GET", "/api/transfers"),
  holdings: () => apiFetch("GET", "/api/wealth/overview"),
  cards: () => apiFetch("GET", "/api/cards"),
  suspicions: () => {
    const request = billsRequestQuery();
    return apiFetch("GET", `/api/bills/analysis${request.query}`);
  },
  charges: async () => {
    const result = await apiFetch("GET", "/api/records/charges");
    if (result.ok || result.status !== 404) return result;
    /* 兼容尚未重启的旧服务进程：用订阅证据回退，避免整页「读取失败」。 */
    const legacy = await apiFetch("GET", "/api/options");
    if (!legacy.ok) return result;
    return { status: 200, ok: true, data: legacyChargePayload(legacy.data) };
  },
};

const RECORD_HEADERS = {
  bills: ["时间", "商户", "分类", "MCC", "地点", "金额"],
  transfers: ["办理时间", "收款人", "金额", "方式", "状态", "用途", "记录号", "详情"],
  holdings: ["产品", "风险等级", "持仓金额", "起息日", "当前收益"],
  cards: ["卡号", "类型", "状态", "日限额"],
  suspicions: [],
  charges: ["时间", "类型", "商户", "金额", "方向", "付款账户", "状态", "记录号"],
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

/* 业务办理时间：优先 booked_at（精确到时分秒），缺失时退化为记账日 00:00:00。
   记录页统一用它排序，避免同日多笔只按日期并列、看起来「没按时间排」。 */
function recordBusinessTime(item) {
  const at = item && item.booked_at ? String(item.booked_at) : "";
  if (at) return at;
  const on = item && item.booked_on ? String(item.booked_on) : "";
  return on ? `${on}T00:00:00` : "";
}

function sortByBusinessTimeNewestFirst(items, tieBreak) {
  return [...(items || [])].sort((a, b) =>
    recordBusinessTime(b).localeCompare(recordBusinessTime(a))
    || String(tieBreak ? tieBreak(a) : "").localeCompare(String(tieBreak ? tieBreak(b) : ""))
  );
}

function recordsRowsFor(key, payload) {
  const data = payload || {};
  if (key === "bills") {
    /* 与账单分析页 `billsTableHtml` 同一份 payload.transactions，不另算一份。 */
    return sortByBusinessTimeNewestFirst(data.transactions, (item) => item.txn_id).map((item) => {
      const moment = item.booked_at ? String(item.booked_at).slice(11, 16) : "";
      return recordsRow([
        `<span class="mono">${esc(item.booked_on || "—")}${moment ? " " + esc(moment) : ""}</span>`,
        esc(item.merchant_label || "—"),
        `${esc(item.category_l2 || item.category || "—")} <span class="muted">${esc(item.category || "")}</span>`,
        esc(item.mcc || "—"),
        esc(item.city || "—"),
        `<span class="mono">${yuan(item.amount_cents || 0)}</span>`,
      ]);
    });
  }
  if (key === "transfers") {
    /* 同日并列时优先本次真实办理（live），历史种子在后；再按记录号稳定排序。 */
    const items = [...(data.items || [])].sort((a, b) =>
      recordBusinessTime(b).localeCompare(recordBusinessTime(a))
      || (a.source === "seed" ? 1 : 0) - (b.source === "seed" ? 1 : 0)
      || String(b.record_id || "").localeCompare(String(a.record_id || ""))
    );
    return items.map((item) => {
      const moment = item.booked_at ? String(item.booked_at).slice(11, 16) : "";
      return recordsRow([
        `<span class="mono">${esc(item.booked_on || "—")}${moment ? " " + esc(moment) : ""}</span>`,
        esc(transferPayeeText(item)),
        `<span class="mono">${yuanText(item.amount_cents || 0)}</span>`,
        esc(item.method_label || "—"),
        `<span class="${transferStatusClass(transferDisplayStatus(item))}">${esc(transferDisplayStatus(item) || "—")}</span>`,
        esc(item.purpose_label || item.remark || "—"),
        `<span class="mono">${esc(item.record_id || "—")}</span>`,
        `<details class="records-detail"><summary>详情</summary>
        ${transferRecordDetail(item)}
      </details>`,
      ]);
    });
  }
  if (key === "holdings") {
    return [...(data.holdings || [])].sort((a, b) =>
      String(b.purchased_on || "").localeCompare(String(a.purchased_on || ""))
      || String(a.name || "").localeCompare(String(b.name || ""))
    ).map((item) => {
      const income = String(item.current_income || "");
      /* 收益列只给短标；完整口径（含「模拟净值」）放进 title，不占列宽 */
      const incomeCell = incomeUnknown(item)
        ? `<span title="${esc(income)}">未知</span>`
        : item.income_cents != null
          ? `<span class="mono" title="${esc(income)}">参考 ${item.income_cents >= 0 ? "+" : "-"}` +
            `${yuan(Math.abs(item.income_cents))}</span>`
          : esc(income || "—");
      return recordsRow([
        esc(item.name || "—"),
        esc(item.risk_level || "—"),
        `<span class="mono">${esc(item.amount_yuan || "0")} 元</span>`,
        `<span class="mono">${esc(item.purchased_on || "—")}</span>`,
        incomeCell,
      ]);
    });
  }
  if (key === "cards") {
    return (data.cards || []).map((item) => recordsRow([
      `<span class="mono">${esc(item.card_id || "—")}</span>`,
      esc(item.type_label || "—"),
      esc(item.status_label || "—"),
      `<span class="mono">${yuan(item.daily_limit_cents || 0)}</span>`,
    ]));
  }
  return [];
}

/* 扣款明细：类型 / 状态 / 方向全部来自服务端流水读模型，前端不再按账户 label 猜余额。 */
function chargeEntryRowHtml(entry) {
  const moment = entry.booked_at ? String(entry.booked_at).slice(11, 16) : "";
  const amount = Number(entry.amount_cents) || 0;
  const signed = `${amount < 0 ? "-" : "+"}${yuan(Math.abs(amount))}`;
  const account = entry.payment_account || {};
  const accountLabel = account.display_label || account.account_label || "—";
  const warning = account.warning
    ? ` <span class="chip chip--yellow">${esc(account.warning)}</span>`
    : "";
  return recordsRow([
    `<span class="mono">${esc(entry.booked_on || "—")}${moment ? " " + esc(moment) : ""}</span>`,
    esc(entry.type_label || "—"),
    esc(entry.merchant_label || "—"),
    `<span class="mono">${signed}</span>`,
    esc(entry.direction_label || "—"),
    `${esc(accountLabel)}${warning}`,
    esc(entry.status_label || "—"),
    `<span class="mono">${esc(entry.record_no || entry.txn_id || "—")}</span>`,
  ]);
}

/* 旧服务进程没有 `/api/records/charges`：用 `/api/options` 的订阅证据回退。
   只用于「未重启」的兼容窗口；重启后自动走新流水读模型。 */
function legacyChargePayload(options) {
  const data = options || {};
  const details = data.subscription_details || {};
  const accountById = new Map(
    (data.accounts || []).map((item) => [String(item.id || ""), item])
  );
  const seen = new Set();
  const charges = [];
  (details.insights || []).forEach((item) => {
    (item.evidence || []).forEach((evidence) => {
      const key = evidence.record_id || `${evidence.booked_on}-${evidence.amount_cents}`;
      if (seen.has(key)) return;
      seen.add(key);
      const account = accountById.get(String(evidence.account_id || ""));
      const label = evidence.payment_account_label
        || (account ? String(account.label || "").split(" · 余额")[0] : "")
        || "—";
      charges.push({
        txn_id: evidence.txn_id || key,
        record_no: evidence.txn_id || evidence.record_id || key,
        booked_on: evidence.booked_on,
        booked_at: evidence.booked_at,
        merchant_label: evidence.merchant_label || item.merchant || "—",
        amount_cents: evidence.amount_cents,
        direction_label: "支出",
        type_label: "代扣",
        status_label: "成功",
        payment_account: { display_label: label, warning: "" },
      });
    });
  });
  const total = charges.reduce(
    (sum, item) => sum + Math.abs(Number(item.amount_cents) || 0),
    0
  );
  return {
    as_of: details.as_of || data.today || "",
    data_source: details.data_source || "服务端订阅证据（旧接口回退）",
    window: 12,
    fallback: true,
    summary: {
      window: 12,
      window_count: charges.length,
      window_total_cents: total,
      history_count: charges.length,
      history_total_cents: total,
    },
    groups: charges.length ? [{ merchant: "", charges }] : [],
  };
}

function chargesPanelHtml(payload) {
  const groups = payload.groups || [];
  const rows = [];
  groups.forEach((group) => {
    (group.charges || []).forEach((entry) => rows.push(entry));
  });
  if (!rows.length) return recordsEmptyHtml(RECORD_TABS.charges);
  rows.sort((a, b) => String(b.booked_on || "").localeCompare(String(a.booked_on || ""))
    || String(b.booked_at || "").localeCompare(String(a.booked_at || ""))
    || String(b.txn_id || "").localeCompare(String(a.txn_id || "")));
  const summary = payload.summary || {};
  const window = Number(summary.window) || 12;
  const count = Number(summary.window_count) || rows.length;
  const mismatch = count !== rows.length
    ? `<p class="muted records-note">计数 ${count} 与明细 ${rows.length} 不一致，请刷新后重试。</p>`
    : "";
  const totalLabel = payload.fallback ? "当前可见" : "全部历史";
  const fallbackNote = payload.fallback
    ? '<p class="muted records-note">当前服务端还是旧进程，暂时用订阅证据回退显示；重启服务后显示完整流水读模型。</p>'
    : "";
  return `<p class="muted records-count">
    近 ${window} 期 ${count} 笔 · ${yuan(summary.window_total_cents || 0)}；
    ${totalLabel} ${Number(summary.history_count) || 0} 笔 ·
    ${yuan(summary.history_total_cents || 0)} · 数据截至 ${esc(payload.as_of || "—")}
  </p>` + fallbackNote + recordsTableHtml(RECORD_HEADERS.charges, rows.map(chargeEntryRowHtml)) + mismatch;
}

function suspicionTableHtml(leads) {
  const headers = ["分层", "可疑类型", "状态", "置信度", "影响金额", "证据", "判断依据", "操作"];
  const rows = (leads || []).map((lead) => {
    const labels = (lead.labels || []).map(esc).join(" / ");
    const evidence = (lead.evidence || []).map((row) => {
      const moment = row.booked_at ? String(row.booked_at).slice(11, 16) : "";
      return `${esc(row.booked_on)}${moment ? " " + esc(moment) : ""} · ${esc(row.city || "地点未知")} · ${esc(row.merchant_label || "—")} · ${yuan(row.amount_cents)}`;
    }).join("<br>");
    const basis = `<details><summary>查看依据</summary>${leadBasisHtml(lead)}</details>`;
    return recordsRow([
      esc(billTierLabel(lead.tier)),
      labels || "—",
      esc(lead.state_label || "待核对"),
      esc(confidencePct(lead.confidence)),
      `<span class="mono">${yuan(lead.impact_cents || 0)}</span>`,
      evidence || "—",
      basis,
      leadActionsHtml(lead),
    ]);
  });
  return recordsTableHtml(headers, rows);
}

function suspicionPanelHtml(payload) {
  const summary = payload.summary || {};
  const leads = payload.leads || [];
  const key = leads.filter((lead) => lead.tier === "key");
  const more = leads.filter((lead) => lead.tier === "more");
  const resolved = leads.filter((lead) => lead.tier === "resolved");
  const head = `<p class="muted records-count">
    账期 ${esc(payload.period || "—")} · 待核对 ${Number(summary.lead_count) || 0} 条 ·
    已核对 ${Number(summary.resolved_lead_count) || 0} 条
  </p>`;
  if (!key.length && !more.length && !resolved.length) {
    return head + '<p class="muted">本期没有待核对的风险线索。识别结果仅供核对，不作最终结论。</p>';
  }
  return head
    + (key.length ? `<p class="panel__title panel__title--sub">重点核对（${key.length}）</p>${suspicionTableHtml(key)}` : "")
    + (more.length ? `<details class="records-summary"><summary>更多线索（${more.length} 条 · 50%–70%）</summary>${suspicionTableHtml(more)}</details>` : "")
    + (resolved.length ? `<details class="records-summary"><summary>已核对（${resolved.length} 条 · 已归档）</summary>${suspicionTableHtml(resolved)}</details>` : "");
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

const VIEWS = ["home", "chat", "scene", "records", "me"];
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
  const data = result.data || {};
  const note = key === "suspicions"
    ? `<p class="muted records-note">只显示风险类线索：异常大额 / 异地 / 深夜 / 重复扣款。自动续费、涨价、已登记订阅核验在订阅页与账单页；置信度 ≥70% 进重点核对、50–70% 折叠、<50% 不展示。核对结果写回同一线索存储，账单页同步翻转。数据来源：${esc(data.data_source || "服务端事实")}。</p>`
    : key === "charges"
      ? `<p class="muted records-note">扣款记录来自统一流水读模型：付款账户为扣款实际使用的卡/账户，不显示账户当前余额；记录号为 txn_ 开头。数据来源：${esc(data.data_source || "服务端事实")}。</p>`
      : key === "bills"
        ? `<p class="muted records-note">与账单分析页同一份消费明细（同账期、同数据源）；转账 / 还款不计入消费，见转账记录。数据来源：${esc(data.data_source || "服务端事实")}。</p>`
        : key === "transfers"
          ? '<p class="muted records-note">「AI 办理」是本次演示中真实执行的转账；「演示历史」是预置流水，用于账单与对账演示。次日到账显示「已发出申请·可撤销」，与「已到账」严格区分。</p>'
          : "";
  if (key === "charges") {
    body.innerHTML = chargesPanelHtml(data) + note;
    return;
  }
  if (key === "suspicions") {
    /* 与账单页共用同一份 payload / leads：核对动作、依据、分层都不再另起一套。 */
    state.bills = data;
    body.innerHTML = suspicionPanelHtml(data) + note;
    return;
  }
  const rows = recordsRowsFor(key, data);
  const summary = key === "transfers" ? payeeSummaryHtml(data.payee_summary) : "";
  body.innerHTML = rows.length
    ? summary +
      `<p class="muted records-count">共 ${rows.length} 条 · 数据来自服务端事实</p>` +
      recordsTableHtml(RECORD_HEADERS[key], rows) + note
    : recordsEmptyHtml(spec);
}

function payeeSummaryHtml(rows) {
  const items = rows || [];
  if (!items.length) return "";
  return `<details class="records-summary">
    <summary>按收款人汇总（${items.length}）</summary>
    <ul>${items.map((item) => {
      const tail = item.payee_account_tail ? ` · 尾号 ${esc(item.payee_account_tail)}` : "";
      return `<li><span>${esc(item.payee || "收款人未知")}${tail}</span>` +
        `<span class="mono">${esc(yuanText(item.total_cents || 0))} · ${esc(String(item.count || 0))} 笔</span></li>`;
    }).join("")}</ul>
  </details>`;
}

function showScene(key, opts) {
  const switching = currentScene !== key;
  currentScene = Object.prototype.hasOwnProperty.call(SCENES, key) ? key : "transfer";
  /* 切场景时丢掉上一页未确认的计划卡：隐藏页里的「确认执行」不该还能被点到 */
  if (switching && scenePending) clearSceneCard();
  const spec = SCENES[currentScene];
  const title = el("scene-title");
  const sub = el("scene-sub");
  if (title) title.textContent = spec.title;
  if (sub) sub.textContent = spec.sub;
  const chatButton = el("btn-scene-chat");
  if (chatButton) {
    chatButton.textContent = currentScene === "bills"
      ? "问 AI 账单问题"
      : currentScene === "transfer"
        ? "用 AI 助理转账"
        : currentScene === "wealth" ? "让 AI 帮我选理财" : "去对话补充";
  }
  document.querySelectorAll("[data-scene-block]").forEach((block) => {
    block.hidden = block.dataset.sceneBlock !== currentScene;
  });
  if (opts && opts.subFocus) state.pendingAgreementFocus = opts.subFocus;
  if (currentScene === "subscriptions" && state.pendingAgreementFocus
    && state.optionsStatus === "ready") applyAgreementFocus();
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
  if (next === "scene") showScene(opts.scene || currentScene, opts);
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
  const chatLeadState = target.closest("[data-chat-lead-state]");
  if (chatLeadState) {
    action(() => applyChatLeadState(chatLeadState.dataset.chatLeadState || ""))();
    return;
  }
  if (target.closest('[data-action="refresh-subscriptions"]')) {
    refreshOptions().catch(() => {});
    return;
  }
  const subView = target.closest("[data-sub-view]");
  if (subView) {
    state.subView = subView.dataset.subView || "agreement";
    renderSubscriptionDetails(state.options);
    return;
  }
  const subCard = target.closest("[data-sub-card]");
  if (subCard) {
    state.subCard = subCard.dataset.subCard || "all";
    renderSubscriptionDetails(state.options);
    return;
  }
  const subDetail = target.closest("[data-sub-detail]");
  if (subDetail) {
    openSubDetail(subDetail.dataset.subDetail || "");
    return;
  }
  if (target.closest("[data-sub-detail-close]")) {
    closeSubDetail();
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
  const suggestion = target.closest("[data-suggestion]");
  if (suggestion) {
    const text = suggestion.dataset.suggestion || "";
    document.querySelectorAll("[data-suggestion]").forEach((node) => node.remove());
    const input = el("utterance");
    if (input) input.value = text;
    if (!el("btn-submit").disabled) action(onSubmit)();
    return;
  }
  const prefill = target.closest("[data-prefill]");
  if (prefill && prefill.dataset.prefill) {
    const input = el("utterance");
    if (input) input.value = prefill.dataset.prefill;
    if (!el("btn-submit").disabled) action(onSubmit)();
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
    if (nav.dataset.subFocus) opts.subFocus = nav.dataset.subFocus;
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
  const recordsLeadState = target.closest("#records-body [data-lead-state]");
  if (recordsLeadState) {
    action(() => setBillLeadState(
      recordsLeadState.dataset.leadId || "",
      recordsLeadState.dataset.leadPeriod || "",
      recordsLeadState.dataset.leadState || "",
    ))();
    return;
  }
  const recordsLeadAsk = target.closest("#records-body [data-lead-ask]");
  if (recordsLeadAsk) {
    askBillLead(recordsLeadAsk.dataset.leadAsk || "");
    return;
  }
  const recordsLeadTxn = target.closest("#records-body [data-lead-txn]");
  if (recordsLeadTxn) {
    state.billsHighlightTxn = recordsLeadTxn.dataset.leadTxn || "";
    navigate("scene", { scene: "bills" });
    refreshBills().catch(() => {});
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
  /* 场景页「页内办事」：转账 / 计划卡确认 / 撤销 / 去对话验证 */
  if (target.closest("#quick-transfer-submit")) {
    submitQuickTransfer().catch(() => {});
    return;
  }
  if (target.closest("#quick-transfer-chat")) {
    transferToChat().catch(() => {});
    return;
  }
  if (target.closest("[data-transfer-repeat-chat]")) {
    repeatTransferFromRecord(target.closest("[data-transfer-repeat-chat]"), false);
    transferToChat().catch(() => {});
    return;
  }
  if (target.closest("[data-transfer-repeat]")) {
    repeatTransferFromRecord(target.closest("[data-transfer-repeat]"));
    return;
  }
  if (target.closest("[data-transfer-cancel]")) {
    cancelTransferRecord(target.closest("[data-transfer-cancel]").dataset.transferCancel || "")
      .catch(() => {});
    return;
  }
  if (target.closest("[data-scene-verify]")) {
    requestSceneVerification().catch(() => {});
    return;
  }
  if (target.closest("[data-scene-confirm-code]")) {
    confirmSceneCard().catch(() => {});
    return;
  }
  if (target.closest("[data-scene-wealth-trade]")) {
    const card = scenePending && scenePending.card;
    if (!card) return;
    const redeem = (card.nodes || []).some((node) => node.action === "wealth.redeem");
    openWealthTradeSheet({ mode: redeem ? "redeem" : "subscribe", card }).catch(() => {});
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
  if (currentScene === "transfer") {
    transferToChat().catch(() => {});
    return;
  }
  if (currentScene === "wealth") {
    const assessment = (state.wealth && state.wealth.assessment) || {};
    const level = assessment.state === "valid" && assessment.risk_profile
      ? assessment.risk_profile
      : "未测评";
    const total = holdingsTotal((state.wealth && state.wealth.holdings) || []);
    const input = el("utterance");
    if (input) {
      input.value = `我想买理财，我的风险测评是 ${level}，当前持仓 ${yuan(Math.round(total * 100))}；请推荐 2 只适合我的产品。`;
    }
    navigate("chat", {});
    return;
  }
  const spec = SCENES[currentScene];
  if (spec && spec.prefill) {
    const input = el("utterance");
    if (input) input.value = spec.prefill;
  }
  navigate("chat", {});
});

/* 快速转账表单：收款人是「输入 + 候选列表」，金额与备注回车等同点「确认转账」 */
el("quick-payee")?.addEventListener("focus", () => {
  openPayeeCandidates().catch(() => {});
});

el("quick-payee")?.addEventListener("input", () => {
  payeeActiveIndex = -1;
  if (pickedPayee && el("quick-payee").value.trim() !== pickedPayee.name) {
    clearPickedPayee();
    const accountInput = el("quick-payee-account");
    if (accountInput) accountInput.value = "";
  }
  renderPayeeCandidates();
  updateTransferFormState();
});

el("quick-payee")?.addEventListener("keydown", (event) => {
  const list = el("payee-candidates");
  const open = list ? !list.hidden : false;
  if (event.key === "ArrowDown" || event.key === "ArrowUp") {
    event.preventDefault();
    if (!open) {
      openPayeeCandidates().catch(() => {});
      return;
    }
    movePayeeActive(event.key === "ArrowDown" ? 1 : -1);
    return;
  }
  if (event.key === "Escape") {
    if (open) {
      event.preventDefault();
      closePayeeCandidates();
    }
    return;
  }
  if (event.key === "Enter") {
    event.preventDefault();
    /* 只有用方向键明确选中的候选才填入；否则按输入的姓名继续，交给服务端澄清（不猜） */
    if (open && payeeActiveIndex >= 0 && payeeMatches[payeeActiveIndex]
      && !payeeMatches[payeeActiveIndex].blocked) {
      choosePayee(payeeMatches[payeeActiveIndex]);
      return;
    }
    closePayeeCandidates();
    submitQuickTransfer().catch(() => {});
  }
});

el("payee-candidates")?.addEventListener("click", (event) => {
  if (event.target.closest("[data-payee-fallback]")) {
    closePayeeCandidates();
    return;
  }
  const item = event.target.closest("[data-payee-name]");
  if (!item) return;
  if (item.dataset.payeeBlocked === "1") return;
  const match = payeeMatches.find((row) =>
    String(row.payeeId || "") === String(item.dataset.payeeId || "")
    && row.name === item.dataset.payeeName);
  choosePayee(match || item.dataset.payeeName);
});

document.addEventListener("click", (event) => {
  const picker = el("payee-picker");
  if (picker && !picker.contains(event.target)) closePayeeCandidates();
});

["quick-amount", "quick-remark"].forEach((id) => {
  el(id)?.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      submitQuickTransfer().catch(() => {});
    }
  });
});

/* 转账表单联动：账户余额、方式说明、金额校验、防诈提示都在同一处刷新。 */
["transfer-account", "quick-amount", "quick-purpose", "quick-remark",
  "quick-payee-phone"].forEach((id) => {
  const node = el(id);
  if (!node) return;
  node.addEventListener("input", () => {
    if (id === "transfer-account") rememberTransferAccount();
    updateTransferFormState();
  });
  node.addEventListener("change", () => {
    if (id === "transfer-account") rememberTransferAccount();
    updateTransferFormState();
  });
});

document.querySelectorAll('input[name="transfer-method"]').forEach((node) => {
  node.addEventListener("change", () => updateTransferFormState());
});

el("quick-payee-account")?.addEventListener("input", () => {
  if (pickedPayee) clearPickedPayee();
  updateTransferFormState();
});

el("quick-payee-bank")?.addEventListener("change", () => {
  if (pickedPayee && el("quick-payee-bank").value !== pickedPayee.bankName) {
    clearPickedPayee();
  }
  updateTransferFormState();
});

document.addEventListener("change", (event) => {
  if (event.target && event.target.id === "transfer-fraud-ack") {
    if (transferFraudGate) transferFraudGate.acknowledged = Boolean(event.target.checked);
    updateTransferFormState();
  }
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
refreshInsights().catch(() => {
  el("insights-status").textContent = "主动洞察读取失败，请稍后重试。";
});
refreshTransfers().catch(() => {
  el("transfers-status").textContent = "转账记录读取失败，请稍后重试。";
  panelFailure("transfers-list", "转账记录读取失败，请稍后重试。", "transfers");
});
/* 收款人候选在页面加载时就取一次（不猜收款人；读不到时输入框仍可直接输入） */
loadPayeeRoster().catch(() => {});
refreshWealth().catch(() => {
  showWealthFailure("理财面板读取失败，请稍后重试。");
});
refreshCards().catch(() => {
  el("cards-status").textContent = "卡片读取失败，请稍后重试。";
  panelFailure("cards-list", "卡片读取失败，请稍后重试。", "cards");
});
refreshBills().catch(() => {
  el("bills-status").textContent = "账单识别读取失败，请稍后重试。";
  panelFailure("bills-summary", "账单识别读取失败，请稍后重试。", "bills");
});
refreshConversations().catch(() => {});
refreshLifeEvents().catch(() => {});
refreshProfileEntry().catch(() => {});

window.addEventListener("offline", () => setOfflineState(true));
window.addEventListener("online", () => {
  setOfflineState(false);
  refreshOptions().catch(() => {});
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
    panelFailure("bills-summary", "账单识别读取失败，请稍后重试。", "bills");
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

/* 未登录只读壳（`<html data-demo-readonly="1">`）：任何写操作先跳登录，不把 401 当交互反馈。 */
document.addEventListener("click", (event) => {
  if (document.documentElement.dataset.demoReadonly !== "1") return;
  const target = event.target instanceof Element
    ? event.target.closest("[data-requires-login]") : null;
  if (!target) return;
  event.preventDefault();
  event.stopImmediatePropagation();
  window.location.assign("/login");
}, true);

if (typeof navigator !== "undefined" && "serviceWorker" in navigator) {
  navigator.serviceWorker.register("/assets/sw.js?v=74", { scope: "/" }).catch(() => {
    timelineAdd("PWA 静态壳注册失败，当前仍可作为同源网页使用。", "warn");
  });
}
setInterval(() => {
  refreshInsights().catch(() => {});
}, 15000);

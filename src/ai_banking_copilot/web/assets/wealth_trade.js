/*
 * 理财交易确认弹窗（申购 / 赎回）。
 *
 * 铁律：
 * - 本文件只做“收集合规确认 + 调用现有 /api/submit → /api/verify/request → /api/confirm”；
 * - 不新增执行通道，不直接改资金；所有动账仍由服务端 PEP 裁决；
 * - 演示编码、模拟净值、演示验证码、双录/回访未接入等口径必须显示在界面上。
 *
 * 依赖 app.js 的全局函数：state / post / apiFetch / esc / el / refreshWealth /
 * confirmAppDialog / applyExecution / refreshAfterExecution。
 */

const WEALTH_DOC_ORDER = ["prospectus", "risk_disclosure", "investor_rights", "sales_agreement"];
const WEALTH_DOC_LABELS = {
  prospectus: "产品说明书",
  risk_disclosure: "风险揭示书",
  investor_rights: "投资者权益须知",
  sales_agreement: "销售协议",
};

let wealthSheetState = null;
let wealthDetailState = null;

function wealthCentsText(cents) {
  const value = Number(cents);
  if (!Number.isFinite(value)) return "—";
  return `${Math.floor(value / 100)}.${String(Math.abs(value % 100)).padStart(2, "0")}`;
}

function wealthSharesText(shareBp) {
  const value = Number(shareBp);
  if (!Number.isFinite(value)) return "—";
  return `${Math.floor(value / 10000)}.${String(Math.abs(value % 10000)).padStart(4, "0")}`;
}

function wealthRiskOrder(level) {
  return { R1: 1, R2: 2, R3: 3, R4: 4, R5: 5 }[level] || 0;
}

function wealthProductById(productId) {
  return (state.wealth && state.wealth.products || []).find(
    (item) => item.product_id === productId
  );
}

function wealthAccountById(accountId) {
  return ((state.options && state.options.accounts) || []).find(
    (item) => item.id === accountId
  );
}

function wealthEnsureDialog() {
  let dialog = document.getElementById("wealth-trade-dialog");
  if (dialog) return dialog;
  dialog = document.createElement("dialog");
  dialog.id = "wealth-trade-dialog";
  dialog.className = "wealth-sheet";
  document.body.appendChild(dialog);
  dialog.addEventListener("click", (event) => {
    if (event.target.closest("[data-wealth-sheet-close]")) {
      closeWealthTradeSheet();
    } else if (event.target.closest("[data-wealth-sheet-next]")) {
      wealthSheetNext().catch(() => {});
    } else if (event.target.closest("[data-wealth-sheet-confirm]")) {
      wealthSheetConfirm().catch(() => {});
    } else if (event.target.closest("[data-wealth-sheet-finish]")) {
      closeWealthTradeSheet();
    }
  });
  dialog.addEventListener("change", (event) => {
    const target = event.target;
    if (target.matches("[data-doc-check]")) {
      const id = target.dataset.docCheck;
      if (wealthSheetState) wealthSheetState.docs[id].checked = target.checked;
    } else if (target.id === "wealth-sheet-account") {
      if (wealthSheetState) wealthSheetState.accountId = target.value;
    } else if (target.id === "wealth-sheet-dividend") {
      if (wealthSheetState) wealthSheetState.dividend = target.value;
    } else if (target.id === "wealth-sheet-redeem-mode") {
      wealthSheetSetRedeemMode(target.value);
    } else if (target.id === "wealth-sheet-auto-redeem") {
      if (wealthSheetState) wealthSheetState.autoRedeem = target.checked;
    } else if (target.id === "wealth-sheet-experience") {
      if (wealthSheetState) wealthSheetState.experience = target.checked;
    } else if (target.id === "wealth-sheet-loss") {
      if (wealthSheetState) wealthSheetState.lossAck = target.checked;
    } else if (target.id === "wealth-sheet-senior") {
      if (wealthSheetState) wealthSheetState.seniorAck = target.checked;
    } else if (target.id === "wealth-sheet-declaration") {
      if (wealthSheetState) wealthSheetState.declaration = target.value;
    }
  });
  dialog.addEventListener("input", (event) => {
    const target = event.target;
    if (target.id === "wealth-sheet-amount") {
      wealthSheetSetAmount(target.value);
    } else if (target.id === "wealth-sheet-shares") {
      wealthSheetSetShares(target.value);
    } else if (target.id === "wealth-sheet-declaration") {
      if (wealthSheetState) wealthSheetState.declaration = target.value;
    }
  });
  dialog.addEventListener("toggle", (event) => {
    const details = event.target;
    if (!details.matches("details[data-doc-id]") || !details.open) return;
    wealthStartDocTimer(details.dataset.docId);
  }, true);
  return dialog;
}

async function openWealthProductDetail(productId) {
  const result = await apiFetch("GET", `/api/wealth/products/${encodeURIComponent(productId)}`);
  if (!result.ok) {
    timelineAdd(`产品详情读取失败：${result.data.detail || "请稍后重试"}`, "stop");
    return;
  }
  wealthDetailState = result.data;
  let dialog = document.getElementById("wealth-detail-dialog");
  if (!dialog) {
    dialog = document.createElement("dialog");
    dialog.id = "wealth-detail-dialog";
    dialog.className = "wealth-sheet wealth-sheet--detail";
    document.body.appendChild(dialog);
    dialog.addEventListener("click", (event) => {
      if (event.target.closest("[data-wealth-detail-close]")) closeDialog(dialog);
      if (event.target.closest("[data-wealth-detail-subscribe]")) {
        const id = event.target.closest("[data-wealth-detail-subscribe]").dataset.wealthDetailSubscribe;
        closeDialog(dialog);
        openWealthTradeSheet({ mode: "subscribe", productId: id }).catch(() => {});
      }
    });
  }
  dialog.innerHTML = wealthDetailHtml(wealthDetailState);
  if (typeof dialog.showModal === "function") dialog.showModal();
  else dialog.setAttribute("open", "");
}

function wealthDetailHtml(item) {
  const fees = item.fees || {};
  const liquidity = item.liquidity || {};
  const suitability = item.suitability || {};
  const risk = item.risk_level || "—";
  const canBuy = item.eligible === true;
  const docs = Object.keys(item.documents || {}).map((id) => {
    const doc = item.documents[id];
    return `<details class="wealth-doc"><summary>《${esc(doc.title)}》<span class="chip">${esc(doc.version)}</span></summary>
      <p class="muted">演示文本，非正式法律文件</p><p class="wealth-doc__body">${esc(doc.body)}</p></details>`;
  }).join("");
  return `<form class="wealth-sheet__form" onsubmit="return false">
    <header class="wealth-sheet__head">
      <div><b>${esc(item.name)}</b> <span class="chip">${esc(risk)}</span>
        <span class="chip">${esc(item.category || "")}</span></div>
      <button class="btn btn--sm btn--ghost" type="button" data-wealth-detail-close>关闭</button>
    </header>
    <p class="muted">${esc(item.risk_basis || "")}</p>
    <p class="muted">${esc(item.benchmark_label || "")} · ${esc(item.benchmark_note || "")}</p>
    <p class="muted">起购 ${esc(item.min_amount_yuan || "—")} 元 · 递增 ${esc(item.increment_yuan || "—")} 元
      · 期限 ${esc(String(item.term_days ?? "—"))} 天</p>
    <p class="muted">费率：申购 ${esc(fees.subscription || "未知")} · 销售服务 ${esc(fees.service || "未知")}/年
      · 托管 ${esc(fees.custody || "未知")}/年</p>
    <p class="muted">${esc(liquidity.label || "")} · 申购截止 ${esc(liquidity.purchase_cutoff || "—")}</p>
    <p class="muted">登记编码：<span class="mono">${esc(item.registration_code || "未披露")}</span>
      · ${esc(item.registration_note || "")}
      <a href="https://www.chinawealth.com.cn/" target="_blank" rel="noopener">中国理财网查询</a></p>
    <div class="wealth-sheet__notice">
      <b>匹配意见：</b>${esc(suitability.conclusion || "—")}<br>
      主要风险：${esc((suitability.main_risks || []).join("、") || "—")}<br>
      不适用人群：${esc((suitability.unsuitable_for || []).join("、") || "—")}<br>
      <span class="muted">${esc(suitability.basis || "")} · ${esc(suitability.disclaimer || "")}</span>
    </div>
    <div class="wealth-sheet__docs">${docs}</div>
    <footer class="wealth-sheet__foot">
      ${canBuy
        ? `<button class="btn btn--primary" type="button" data-wealth-detail-subscribe="${esc(item.product_id)}">立即申购</button>`
        : `<button class="btn btn--sm" type="button" data-wealth-detail-close>暂不可售</button>`}
    </footer>
  </form>`;
}

function openWealthRedeemSheet(options) {
  return openWealthTradeSheet(Object.assign({ mode: "redeem" }, options || {}));
}

async function openWealthTradeSheet(options) {
  const opts = options || {};
  const mode = opts.mode === "redeem" ? "redeem" : "subscribe";
  let productId = opts.productId || "";
  let card = opts.card || null;
  const prefill = Object.assign({}, opts.prefill || {});
  if (!productId && card) {
    const node = (card.nodes || []).find((item) =>
      item.action === (mode === "redeem" ? "wealth.redeem" : "wealth.subscribe")
    );
    if (node) {
      productId = node.product_id || "";
      if (node.amount_cents) prefill.amount_yuan = wealthCentsText(node.amount_cents);
      if (node.share_bp) {
        prefill.shares = wealthSharesText(node.share_bp);
        prefill.mode = "shares";
      }
      prefill.card = card;
    }
  }
  if (!productId) {
    timelineAdd("没有取到要交易的产品，请重新选择。", "stop");
    return;
  }
  if (!state.options) {
    try { await refreshOptions(); } catch (_) { /* 下面会给出明确错误 */ }
  }
  const detail = await apiFetch("GET", `/api/wealth/products/${encodeURIComponent(productId)}`);
  if (!detail.ok) {
    timelineAdd(`产品详情读取失败：${detail.data.detail || "请稍后重试"}`, "stop");
    return;
  }
  const product = wealthProductById(productId) || detail.data;
  const accounts = (state.options && state.options.accounts) || [];
  const defaultAccount = prefill.account_id || (accounts[0] && accounts[0].id) || "acc_savings";
  wealthSheetState = {
    mode,
    product,
    detail: detail.data,
    card: card || prefill.card || null,
    accounts,
    accountId: defaultAccount,
    amount: prefill.amount_yuan || "",
    redeemMode: prefill.mode || "all",
    shares: prefill.shares || "",
    quote: null,
    dividend: "reinvest",
    autoRedeem: false,
    experience: false,
    lossAck: false,
    seniorAck: false,
    seniorReady: false,
    declaration: "",
    docs: {},
    error: "",
    busy: false,
    challenge: null,
  };
  WEALTH_DOC_ORDER.forEach((id) => {
    wealthSheetState.docs[id] = { checked: false, seconds: 0, done: false };
  });
  const dialog = wealthEnsureDialog();
  renderWealthSheet();
  if (typeof dialog.showModal === "function") dialog.showModal();
  else dialog.setAttribute("open", "");
  if (
    wealthSheetState.detail.senior_notice
    && wealthRiskOrder(wealthSheetState.detail.risk_level) >= 3
    && mode === "subscribe"
  ) {
    wealthStartSeniorCountdown();
  }
}

function closeWealthTradeSheet() {
  const dialog = document.getElementById("wealth-trade-dialog");
  if (dialog) closeDialog(dialog);
  wealthSheetState = null;
}

function wealthRiskText(level) {
  return level || "—";
}

function renderWealthSheet() {
  const sheet = wealthSheetState;
  if (!sheet) return;
  const dialog = document.getElementById("wealth-trade-dialog");
  if (!dialog) return;
  const product = sheet.product;
  const detail = sheet.detail;
  const liquidity = detail.liquidity || {};
  const fees = detail.fees || {};
  const suitability = detail.suitability || {};
  const displayRisk = detail.risk_level || product.risk_level;
  const isRedeem = sheet.mode === "redeem";
  const locked = Boolean(sheet.card);
  const risk = wealthRiskText(displayRisk);
  const firstRiskDisclosure = sheet.mode === "subscribe"
    && detail.first_purchase === true
    && wealthRiskOrder(risk) >= 3;
  const accountOptions = sheet.accounts.map((item) =>
    `<option value="${esc(item.id)}"${item.id === sheet.accountId ? " selected" : ""}>${esc(item.label)}</option>`
  ).join("");
  const docs = WEALTH_DOC_ORDER.map((id) => {
    const doc = (detail.documents || {})[id] || { title: WEALTH_DOC_LABELS[id], body: "未提供", version: "—" };
    const required = id === "risk_disclosure" && firstRiskDisclosure ? 15 : 3;
    const docState = sheet.docs[id] || { checked: false, seconds: 0, done: false };
    const timerText = docState.done
      ? `已阅读 ${docState.seconds} 秒，可勾选`
      : `需阅读 ${required} 秒`;
    return `<details class="wealth-doc" data-doc-id="${esc(id)}" data-required="${required}">
      <summary>《${esc(doc.title)}》<span class="chip">${esc(doc.version || "—")}</span>
        <span class="muted" data-doc-timer="${esc(id)}">${timerText}</span></summary>
      <p class="muted">演示文本，非正式法律文件</p>
      <p class="wealth-doc__body">${esc(doc.body || "")}</p>
      <label class="wealth-check"><input type="checkbox" data-doc-check="${esc(id)}"
        ${docState.done ? "" : "disabled"}${docState.checked ? " checked" : ""}>
        我已阅读并同意《${esc(doc.title)}》</label>
    </details>`;
  }).join("");
  const typeFields = wealthTypeFields(sheet, detail, risk);
  const declaration = firstRiskDisclosure
    ? `<div class="wealth-sheet__notice">
        <b>请逐字抄录声明：</b><code>${esc(detail.declaration_text || "")}</code>
        <textarea id="wealth-sheet-declaration" class="input" rows="2"
          placeholder="请逐字抄录上方声明">${esc(sheet.declaration)}</textarea>
      </div>`
    : "";
  const redeemFields = isRedeem
    ? `<label class="wealth-field">赎回方式
        <select id="wealth-sheet-redeem-mode" class="input"${locked ? " disabled" : ""}>
          <option value="all"${sheet.redeemMode === "all" ? " selected" : ""}>全部赎回</option>
          <option value="shares"${sheet.redeemMode === "shares" ? " selected" : ""}>部分赎回（按份额）</option>
          <option value="amount"${sheet.redeemMode === "amount" ? " selected" : ""}>按金额赎回</option>
        </select>
      </label>
      <label class="wealth-field" id="wealth-sheet-redeem-value-wrap" ${sheet.redeemMode === "all" ? "hidden" : ""}>
        ${sheet.redeemMode === "amount" ? "赎回金额（元）" : "赎回份额（份）"}
        <input id="wealth-sheet-${sheet.redeemMode === "amount" ? "amount" : "shares"}" class="input"
          inputmode="decimal" value="${esc(sheet.redeemMode === "amount" ? sheet.amount : sheet.shares)}"${locked ? " readonly" : ""}>
      </label>
      <div id="wealth-sheet-quote" class="wealth-sheet__notice">切换赎回方式后会按最新模拟净值试算。</div>`
    : "";
  const amountField = isRedeem ? "" : `<label class="wealth-field">申购金额（元）
      <input id="wealth-sheet-amount" class="input" inputmode="decimal"
        value="${esc(sheet.amount)}" placeholder="≥ ${esc(product.min_amount_yuan || "")}，递增 ${esc(product.increment_yuan || "")}"${locked ? " readonly" : ""}>
    </label>
    ${locked ? '<p class="muted">计划卡已生成，金额/份额以计划卡为准；如需修改请取消后重新发起。</p>' : ""}`;
  const dividend = isRedeem ? "" : `<label class="wealth-field">分红方式
      <select id="wealth-sheet-dividend" class="input">
        <option value="reinvest"${sheet.dividend === "reinvest" ? " selected" : ""}>红利再投资（默认）</option>
        <option value="cash"${sheet.dividend === "cash" ? " selected" : ""}>现金分红</option>
      </select>
    </label>`;
  dialog.innerHTML = `<form class="wealth-sheet__form" onsubmit="return false">
    <header class="wealth-sheet__head">
      <div><b>${isRedeem ? "赎回确认" : "申购确认"}</b>
        <span class="muted">${esc(product.name)} · ${esc(risk)} · ${esc(product.category || "")}</span></div>
      <button class="btn btn--sm btn--ghost" type="button" data-wealth-sheet-close>关闭</button>
    </header>
    <section class="wealth-sheet__section">
      <h3>① 产品与适当性</h3>
      <p class="muted">${esc(product.benchmark_label || "")} · ${esc(product.benchmark_note || "")}
        · 起购 ${esc(product.min_amount_yuan || "—")} 元 · 递增 ${esc(product.increment_yuan || "—")} 元</p>
      <p class="muted">登记编码 <span class="mono">${esc(product.registration_code || "未披露")}</span>
        · ${esc(product.registration_note || "")}</p>
      <p class="muted">费率：申购 ${esc(fees.subscription || "未知")} · 销售服务 ${esc(fees.service || "未知")}/年
        · 托管 ${esc(fees.custody || "未知")}/年</p>
      <div class="wealth-sheet__notice"><b>匹配意见：</b>${esc(suitability.conclusion || "—")}<br>
        主要风险：${esc((suitability.main_risks || []).join("、") || "—")}<br>
        不适用人群：${esc((suitability.unsuitable_for || []).join("、") || "—")}<br>
        <span class="muted">${esc(suitability.basis || "")} · ${esc(suitability.disclaimer || "")}</span>
      </div>
      <p class="muted">${esc(liquidity.label || "")} · 申购截止 ${esc(liquidity.purchase_cutoff || "—")}</p>
      ${detail.senior_notice && wealthRiskOrder(risk) >= 3 && !isRedeem
        ? `<div class="wealth-sheet__warn">65 周岁以上客户购买 R3 及以上产品，需要更长考虑时间；
            人工回访未接入，演示环境仅延长考虑时间。
            <label class="wealth-check"><input type="checkbox" id="wealth-sheet-senior"${sheet.seniorAck ? " checked" : ""}>
              我已阅读特别提示</label></div>`
        : ""}
    </section>
    <section class="wealth-sheet__section">
      <h3>② 交易信息</h3>
      <label class="wealth-field">交易账户
        <select id="wealth-sheet-account" class="input">${accountOptions}</select>
      </label>
      ${amountField}${redeemFields}${dividend}
      ${typeFields}
      <p id="wealth-sheet-amount-error" class="wealth-sheet__error">${esc(sheet.error || "")}</p>
    </section>
    <section class="wealth-sheet__section">
      <h3>③ 文件与声明</h3>
      <p class="muted">四份文件需分别点开阅读；普通文件至少 3 秒，首次购买 R3 及以上时风险揭示书至少 15 秒。</p>
      <div class="wealth-sheet__docs">${docs}</div>
      ${declaration}
    </section>
    <section class="wealth-sheet__section">
      <h3>④ 验证与确认</h3>
      <p class="muted">申购/赎回均为红色动作，需要服务端一次性验证码；演示环境会直接显示验证码。</p>
      <div id="wealth-sheet-code-area" class="wealth-sheet__notice"${sheet.challenge ? "" : " hidden"}>
        <b>验证码已下发</b> <span id="wealth-sheet-code-target"></span>
        <input id="wealth-sheet-code" class="input" inputmode="numeric" maxlength="6" placeholder="6 位验证码">
        <p class="muted">演示环境验证码：<span id="wealth-sheet-demo-code" class="mono"></span></p>
      </div>
      <p id="wealth-sheet-error" class="wealth-sheet__error"></p>
    </section>
    <footer class="wealth-sheet__foot">
      <button class="btn btn--sm" type="button" data-wealth-sheet-close>取消</button>
      <button class="btn btn--sm btn--primary" type="button" data-wealth-sheet-next>下一步：获取验证码</button>
      <button class="btn btn--sm btn--primary" type="button" data-wealth-sheet-confirm hidden>${isRedeem ? "确认赎回" : "确认申购"}</button>
      <button class="btn btn--sm btn--primary" type="button" data-wealth-sheet-finish hidden>完成</button>
    </footer>
  </form>`;
}

function wealthTypeFields(sheet, detail, risk) {
  const type = detail.product_type || "";
  const parts = [];
  if (sheet.mode === "subscribe" && type === "cash") {
    parts.push(`<label class="wealth-check"><input type="checkbox" id="wealth-sheet-auto-redeem"${sheet.autoRedeem ? " checked" : ""}>
      同时设置自动赎回 / 消费抵扣（仅记录偏好，演示环境未接入自动赎回）</label>`);
  }
  if (sheet.mode === "subscribe" && type === "bond") {
    parts.push(`<div class="wealth-sheet__warn">该产品封闭 ${esc(String(detail.liquidity?.closed_days ?? "—"))} 天，
      期间不可赎回，请确认资金使用安排；今日 ${esc(detail.liquidity?.purchase_cutoff || "15:00")}
      前申购按今日净值确认。</div>`);
  }
  if (sheet.mode === "subscribe" && (type === "mixed" || type === "equity")) {
    parts.push(`<label class="wealth-check"><input type="checkbox" id="wealth-sheet-experience"${sheet.experience ? " checked" : ""}>
      本人近 12 个月有投资经验</label>
      <label class="wealth-check"><input type="checkbox" id="wealth-sheet-loss"${sheet.lossAck ? " checked" : ""}>
      您已知悉本产品可能损失本金</label>`);
  }
  if (sheet.mode === "subscribe" && wealthRiskOrder(risk) >= 4) {
    parts.push(`<div class="wealth-sheet__warn">R4 及以上产品需双录或书面确认；
      演示环境未接入双录，仅提示。</div>`);
  }
  return parts.join("");
}

function wealthSheetSetAmount(value) {
  if (!wealthSheetState) return;
  wealthSheetState.amount = value;
  wealthValidateSheet();
}

function wealthSheetSetShares(value) {
  if (!wealthSheetState) return;
  wealthSheetState.shares = value;
  wealthValidateSheet();
  wealthRefreshQuote().catch(() => {});
}

function wealthSheetSetRedeemMode(mode) {
  if (!wealthSheetState) return;
  wealthSheetState.redeemMode = mode;
  renderWealthSheet();
  if (wealthSheetState && wealthSheetState.redeemMode !== "all") {
    wealthRefreshQuote().catch(() => {});
  }
}

function wealthValidateSheet() {
  const sheet = wealthSheetState;
  if (!sheet) return "交易已关闭，请重新打开。";
  const product = sheet.product;
  const detail = sheet.detail;
  const errorNode = document.getElementById("wealth-sheet-amount-error");
  const setError = (text) => {
    if (errorNode) errorNode.textContent = text;
    return text;
  };
  if (sheet.mode === "subscribe") {
    const amount = Number(sheet.amount);
    if (!Number.isFinite(amount) || amount <= 0) return setError("请输入大于 0 的申购金额。");
    const min = Number(product.min_amount_yuan);
    if (Number.isFinite(min) && amount < min) {
      return setError(`低于起购金额 ${product.min_amount_yuan} 元，请重新输入。`);
    }
    const increment = Number(product.increment_yuan);
    if (Number.isFinite(increment) && increment > 0) {
      if (Math.round(amount * 100) % Math.round(increment * 100) !== 0) {
        return setError(`必须为 ${product.increment_yuan} 元的整数倍。`);
      }
    }
    const account = wealthAccountById(sheet.accountId);
    if (account && Number.isFinite(Number(account.balance_cents))) {
      if (amount * 100 > Number(account.balance_cents)) {
        return setError(`申购金额超过账户可用余额 ${wealthCentsText(account.balance_cents)} 元。`);
      }
    }
  } else if (sheet.redeemMode !== "all") {
    const value = Number(sheet.redeemMode === "amount" ? sheet.amount : sheet.shares);
    if (!Number.isFinite(value) || value <= 0) {
      return setError("请输入大于 0 的赎回金额或份额。");
    }
  }
  const missingDocs = WEALTH_DOC_ORDER.filter((id) => !sheet.docs[id].checked);
  if (missingDocs.length) {
    return setError(`还有 ${missingDocs.length} 份文件未勾选确认。`);
  }
  const firstRiskDisclosure = sheet.mode === "subscribe"
    && detail.first_purchase === true
    && wealthRiskOrder(detail.risk_level) >= 3;
  if (firstRiskDisclosure) {
    if ((sheet.declaration || "").trim() !== String(detail.declaration_text || "").trim()) {
      return setError("风险揭示声明必须逐字抄录且完全一致。");
    }
  }
  const type = detail.product_type || "";
  if (sheet.mode === "subscribe" && (type === "mixed" || type === "equity")) {
    if (!sheet.experience || !sheet.lossAck) {
      return setError("请确认投资经验与本金损失风险。");
    }
  }
  if (sheet.mode === "subscribe" && detail.senior_notice
    && wealthRiskOrder(detail.risk_level) >= 3) {
    if (!sheet.seniorAck) return setError("请确认 65 岁以上特别提示。");
    if (!sheet.seniorReady) return setError("请阅读特别提示满 15 秒后再继续。");
  }
  setError("");
  return "";
}

function wealthStartSeniorCountdown() {
  let left = 15;
  const tick = () => {
    const button = document.querySelector("[data-wealth-sheet-next]");
    if (!wealthSheetState || !button) return;
    if (left > 0) {
      button.disabled = true;
      button.textContent = `请阅读特别提示（${left}s）`;
      left -= 1;
      setTimeout(tick, 1000);
    } else {
      wealthSheetState.seniorReady = true;
      button.disabled = false;
      button.textContent = "下一步：获取验证码";
    }
  };
  tick();
}

function wealthStartDocTimer(docId) {
  const sheet = wealthSheetState;
  if (!sheet || !sheet.docs[docId] || sheet.docs[docId].done) return;
  const node = document.querySelector(`[data-doc-timer="${docId}"]`);
  const checkbox = document.querySelector(`[data-doc-check="${docId}"]`);
  const required = Number(
    document.querySelector(`details[data-doc-id="${docId}"]`)?.dataset.required || 3
  );
  const state = sheet.docs[docId];
  const timer = setInterval(() => {
    if (!wealthSheetState || wealthSheetState !== sheet) {
      clearInterval(timer);
      return;
    }
    state.seconds += 1;
    if (node) node.textContent = `已阅读 ${state.seconds} 秒 / 需 ${required} 秒`;
    if (state.seconds >= required) {
      clearInterval(timer);
      state.done = true;
      if (checkbox) checkbox.disabled = false;
      if (node) node.textContent = `已阅读 ${state.seconds} 秒，可勾选`;
    }
  }, 1000);
}

async function wealthRefreshQuote() {
  const sheet = wealthSheetState;
  if (!sheet || sheet.mode !== "redeem") return;
  if (sheet.redeemMode === "all") {
    const result = await apiFetch(
      "GET",
      `/api/wealth/redemption-quote?product_id=${encodeURIComponent(sheet.product.product_id)}&mode=all`
    );
    if (!result.ok) return;
    sheet.quote = result.data;
    wealthRenderQuote();
    return;
  }
  const raw = sheet.redeemMode === "amount" ? sheet.amount : sheet.shares;
  if (!raw) return;
  const result = await apiFetch(
    "GET",
    `/api/wealth/redemption-quote?product_id=${encodeURIComponent(sheet.product.product_id)}`
      + `&mode=${encodeURIComponent(sheet.redeemMode)}&value=${encodeURIComponent(raw)}`
  );
  if (!result.ok) {
    const node = document.getElementById("wealth-sheet-quote");
    if (node) node.textContent = result.data.detail || "试算失败";
    return;
  }
  sheet.quote = result.data;
  wealthRenderQuote();
}

function wealthRenderQuote() {
  const sheet = wealthSheetState;
  const node = document.getElementById("wealth-sheet-quote");
  if (!sheet || !node || !sheet.quote) return;
  const quote = sheet.quote;
  if (quote.redeemable === false) {
    node.innerHTML = `<b>当前不可赎回</b>：${esc(quote.reason || "封闭期内不可赎回")}`;
    return;
  }
  const parts = [
    `预计赎回 <b>${esc(quote.amount_yuan)} 元</b> / ${esc(quote.shares)} 份`,
    `净值 ${esc(quote.nav)}（模拟）`,
    `到账：${esc(quote.arrival?.label || "—")}（${esc(quote.arrival?.estimated_date || "—")}）`,
  ];
  if (quote.fast_redeem) parts.push("符合货币类快赎（单笔 ≤1 万元）");
  node.innerHTML = `<p>${parts.join(" · ")}</p>
    <p class="muted">${esc(quote.nav_note || "")}</p>
    ${quote.large_redemption ? `<p class="wealth-sheet__warn">${esc(quote.warning)}</p>` : ""}`;
}

async function wealthSheetNext() {
  const sheet = wealthSheetState;
  if (!sheet || sheet.busy) return;
  const stepError = document.getElementById("wealth-sheet-error");
  if (stepError) stepError.textContent = "";
  const error = wealthValidateSheet();
  if (error) {
    const node = document.getElementById("wealth-sheet-error");
    if (node) node.textContent = error;
    return;
  }
  if (sheet.mode === "redeem") {
    await wealthRefreshQuote();
    if (!sheet.quote || sheet.quote.redeemable === false) {
      const node = document.getElementById("wealth-sheet-error");
      if (node) node.textContent = sheet.quote?.reason || "当前不可赎回。";
      return;
    }
  }
  sheet.busy = true;
  const button = document.querySelector("[data-wealth-sheet-next]");
  if (button) { button.disabled = true; button.textContent = "正在提交确认…"; }
  try {
    const ack = await apiFetch("POST", "/api/wealth/acknowledgements", {
      action: sheet.mode,
      product_id: sheet.product.product_id,
      account_id: sheet.accountId,
      doc_versions: Object.fromEntries(
        WEALTH_DOC_ORDER.map((id) => [id, (sheet.detail.documents || {})[id]?.version || "demo-v1"])
      ),
      read_seconds: Object.fromEntries(
        WEALTH_DOC_ORDER.map((id) => [id, sheet.docs[id].seconds])
      ),
      declaration_text: sheet.declaration || null,
      dividend_method: sheet.mode === "subscribe" ? sheet.dividend : null,
      auto_redeem_pref: sheet.mode === "subscribe" ? sheet.autoRedeem : null,
      experience_confirmed: sheet.experience,
      loss_ack: sheet.lossAck,
      senior_ack: sheet.seniorAck,
    });
    if (!ack.ok) throw new Error(ack.data.detail || "交易确认留痕未通过");
    if (!sheet.card) {
      const utterance = wealthUtterance(sheet);
      const submit = await post("/api/submit", {
        utterance,
        selected: { product: sheet.product.product_id, account: sheet.accountId },
      });
      if (!submit.ok) throw new Error(submit.data.detail || "生成办理计划失败");
      if (submit.data.state === "blocked") {
        throw new Error(submit.data.message || "服务端拒绝了这次交易");
      }
      if (submit.data.state !== "awaiting_confirm" || !submit.data.card) {
        throw new Error(submit.data.message || "没有生成可确认的计划");
      }
      sheet.card = submit.data.card;
    }
    if ((sheet.card.requires || []).includes("strong_verify")) {
      const challenge = await post("/api/verify/request", {});
      if (!challenge.ok) throw new Error(challenge.data.detail || "获取验证码失败");
      sheet.challenge = challenge.data;
      const area = document.getElementById("wealth-sheet-code-area");
      const target = document.getElementById("wealth-sheet-code-target");
      const demo = document.getElementById("wealth-sheet-demo-code");
      if (area) area.hidden = false;
      if (target) target.textContent = `${challenge.data.channel || "演示"} → ${challenge.data.target || ""}`;
      if (demo) demo.textContent = challenge.data.code || "";
      const confirm = document.querySelector("[data-wealth-sheet-confirm]");
      if (confirm) confirm.hidden = false;
      if (button) button.hidden = true;
    } else {
      const confirm = document.querySelector("[data-wealth-sheet-confirm]");
      if (confirm) confirm.hidden = false;
      if (button) button.hidden = true;
    }
  } catch (error) {
    const node = document.getElementById("wealth-sheet-error");
    if (node) node.textContent = fetchErrorMessage(error, "提交失败，请稍后重试。");
  } finally {
    sheet.busy = false;
    if (button && !button.hidden) {
      button.disabled = false;
      button.textContent = "下一步：获取验证码";
    }
  }
}

function wealthUtterance(sheet) {
  if (sheet.mode === "subscribe") {
    return `申购 ${sheet.amount} 元 ${sheet.product.name}`;
  }
  if (sheet.redeemMode === "all") return `赎回 ${sheet.product.name}`;
  if (sheet.redeemMode === "shares") return `赎回 ${sheet.shares} 份 ${sheet.product.name}`;
  return `赎回 ${sheet.amount} 元 ${sheet.product.name}`;
}

async function wealthSheetConfirm() {
  const sheet = wealthSheetState;
  if (!sheet || !sheet.card || sheet.busy) return;
  const quote = sheet.quote || {};
  const summary = [
    `产品：${sheet.product.name}（${sheet.detail.risk_level || sheet.product.risk_level}）`,
    sheet.mode === "subscribe"
      ? `金额：${sheet.amount} 元`
      : `赎回：${quote.shares || sheet.shares || "—"} 份 / 预计 ${quote.amount_yuan || "—"} 元`,
    `账户：${(wealthAccountById(sheet.accountId) || {}).label || sheet.accountId}`,
    sheet.mode === "subscribe" ? `分红方式：${sheet.dividend === "cash" ? "现金分红" : "红利再投资"}` : "",
    sheet.mode === "redeem"
      ? `到账：${quote.arrival?.label || "—"}（${quote.arrival?.estimated_date || "—"}）`
      : "预计成交确认日：T+1（演示口径）",
    "演示环境：份额按模拟净值折算，实际以 T 日净值为准。",
  ].filter(Boolean).join("\n");
  const accepted = await confirmAppDialog({
    title: sheet.mode === "redeem" ? "确认赎回" : "确认申购",
    text: summary,
    confirmText: sheet.mode === "redeem" ? "确认赎回" : "确认申购",
    danger: false,
  });
  if (!accepted) return;
  const codeInput = document.getElementById("wealth-sheet-code");
  const code = codeInput ? codeInput.value.trim() : "";
  if (!code) {
    const node = document.getElementById("wealth-sheet-error");
    if (node) node.textContent = "请输入 6 位演示验证码。";
    return;
  }
  sheet.busy = true;
  try {
    const result = await post("/api/confirm", {
      card_id: sheet.card.card_id,
      frozen_hash: sheet.card.frozen_hash,
      verification_code: code,
    });
    if (!result.ok) throw new Error(result.data.detail || "确认被拒绝");
    const data = result.data || {};
    const form = document.querySelector("#wealth-trade-dialog .wealth-sheet__form");
    if (form) {
      form.innerHTML = `<header class="wealth-sheet__head"><b>交易已提交</b>
        <button class="btn btn--sm btn--ghost" type="button" data-wealth-sheet-finish>完成</button></header>
        <div class="wealth-sheet__notice"><b>${esc(data.message || "计划执行完成")}</b>
          <p class="muted">回执已记录；可在「记录与明细」核对。演示环境不产生真实资金划转。</p></div>
        <footer class="wealth-sheet__foot"><button class="btn btn--sm btn--primary" type="button" data-wealth-sheet-finish>完成</button></footer>`;
    }
    const sameChatCard = Boolean(
      state.card && sheet.card && state.card.card_id === sheet.card.card_id
    );
    if (sameChatCard && typeof applyExecution === "function") {
      applyExecution(data);
      await refreshAfterExecution();
    } else {
      await refreshWealth();
    }
    if (scenePending && typeof clearSceneCard === "function") {
      clearSceneCard();
    }
  } catch (error) {
    const node = document.getElementById("wealth-sheet-error");
    if (node) node.textContent = fetchErrorMessage(error, "确认失败，请稍后重试。");
  } finally {
    sheet.busy = false;
  }
}

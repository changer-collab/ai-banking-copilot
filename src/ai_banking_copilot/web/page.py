"""Web 渠道内联单页（版式对齐设计仓 `product-web.html` / `前端示例-APP端.html`）。

- 骨架：左侧栏（品牌 / 新建对话 / 导航 / 历史对话 / 我的）+ 主区（常驻顶栏 / 各视图）
  + 右侧「处理详情」栏；**同一份 HTML 按窗口宽度自适应**：≤600px 自动切 APP 版式
  （底部四 Tab、首页竖排场景列表、对话整屏），因此 PWA 安装后即是 APP，不需要 `/m` 入口。
- 样式与设计令牌**随包分发**：`web/assets/{tokens.css,app.css}`（取自设计仓，同一套令牌），
  页面层版式在本项目 `workspace.css` 中实现。
- 交互由**真实 API** 驱动：`web/assets/app.js`（本项目自有实现，非设计仓的 mock 脚本）。
- 本页不引用任何外部域名资源，保持自包含（除同源 `/assets/*`）。
"""

from __future__ import annotations

from collections.abc import Sequence
from html import escape

# Inline HTML intentionally keeps a few long semantic lines; layout checks cover structure.
# ruff: noqa: E501
_PAGE_HTML = """<!DOCTYPE html>
<html lang="zh-CN" data-theme="light">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<meta name="theme-color" content="#0b4fd8" />
<title>AI 银行副驾 · 对话办理</title>
<link rel="manifest" href="/assets/manifest.json?v=1" />
<link rel="icon" href="/assets/icon.svg?v=1" type="image/svg+xml" />
<link rel="stylesheet" href="/assets/tokens.css?v=2" />
<link rel="stylesheet" href="/assets/app.css?v=7" />
<link rel="stylesheet" href="/assets/workspace.css?v=60" />
</head>
<body>
<div class="layout layout--workspace" id="app" data-view-name="chat">
  <aside class="side" id="conversation-sidebar" aria-label="对话与场景" aria-hidden="false">
    <div class="side__brand topbar__brand">
      <span class="brand-mark">AI 银行<em>副驾</em></span>
      <span class="brand-sub">把想办的事说出来 · 每一步由您掌控</span>
    </div>
    <div class="side__pad">
      <button class="new-chat-card" id="btn-reset" type="button">
        <span class="new-chat-card__plus" aria-hidden="true">＋</span>
        <span>新建对话</span>
      </button>
    </div>
    <div class="side__scroll" id="side-scroll">
      <nav class="appnav" id="app-nav" aria-label="功能导航">
        <button class="appnav__item" type="button" data-nav="home">首页</button>
        <p class="side__label">场景</p>
        <div class="appnav__grid">
          <button class="appnav__tile" type="button" data-nav="scene" data-scene="transfer">转账</button>
          <button class="appnav__tile" type="button" data-nav="scene" data-scene="bills">账单</button>
          <button class="appnav__tile" type="button" data-nav="scene" data-scene="wealth">理财</button>
          <button class="appnav__tile" type="button" data-nav="scene" data-scene="cards">卡片</button>
          <button class="appnav__tile" type="button" data-nav="scene" data-scene="subscriptions">订阅</button>
          <button class="appnav__tile" type="button" data-nav="scene" data-scene="life">生活</button>
        </div>
        <p class="side__label">工具</p>
        <button class="appnav__item" type="button" data-nav="records">记录与明细</button>
      </nav>
      <p class="side__label">历史对话</p>
      <div class="convs" id="conversation-list"><p class="muted convs__empty">开始办理后，对话会保存在这里。</p></div>
    </div>
    <div class="side__foot">
      <button class="side-user" id="btn-me-side" type="button" data-nav="me">
        <span class="side-user__avatar" aria-hidden="true">演示</span>
        <span class="side-user__meta">
          <span class="side-user__name" id="side-user-name">我的</span>
          <span class="side-user__sub">个人中心 · 登录身份</span>
        </span>
      </button>
      <p class="side-note">模拟银行环境 · 不连接真实银行账户</p>
    </div>
  </aside>

  <main class="main" id="main" aria-label="银行办理对话">
    <header class="topbar">
      <div class="top__brand">
        <div class="top__t">AI 银行副驾</div>
        <div class="top__s">今天，有什么可以帮您？</div>
      </div>
      <div class="topbar__nav">
        <button class="btn btn--ghost" id="btn-install" type="button">安装应用</button>
        <button class="btn btn--ghost" id="btn-scenes-mobile" type="button"
                aria-controls="conversation-sidebar" aria-expanded="true">导航</button>
        <button class="btn btn--ghost" id="btn-history-mobile" type="button"
                aria-controls="conversation-sidebar" aria-expanded="false">历史</button>
        <button class="btn btn--ghost" id="btn-trace-mobile" type="button"
                aria-controls="trace" aria-expanded="true">处理详情</button>
        <label class="switch">
          <input type="checkbox" id="theme-toggle" />
          夜航主题
        </label>
        <button class="btn btn--ghost profile-entry" id="btn-profile" type="button"
                aria-controls="profile-panel" aria-expanded="false">
          <span class="profile-entry__name" id="profile-entry-name">个人中心</span>
        </button>
      </div>
    </header>
    <p class="muted" id="install-status" role="status" hidden></p>

    <section class="view" id="view-home" hidden>
      <header class="view__head">
        <div>
          <h2>今天想办什么？</h2>
          <p class="muted">六个场景都有入口。说一句话，AI 给计划卡，您确认后再执行。</p>
        </div>
        <span class="muted" id="home-note">模拟银行环境 · 不连接真实银行账户</span>
      </header>
      <!-- 窄屏首页余额卡：数字全部来自 /api/options 与 /api/cards，读不到就显示「—」，不编造 -->
      <section class="home-balance" id="home-balance" aria-label="账户余额">
        <p class="home-balance__k">账户余额合计 · 演示环境</p>
        <p class="home-balance__v" id="home-balance-total">—</p>
        <div class="home-balance__row">
          <div class="home-balance__chip">活期可用<b id="home-balance-current">—</b></div>
          <div class="home-balance__chip">卡片日限额<b id="home-balance-limit">—</b></div>
        </div>
      </section>
      <div class="scene-grid">
        <article class="scene-card">
          <p class="scene-card__k">① 转账</p>
          <p class="scene-card__d">选付款账户、核对收款人三要素、选择到账方式。</p>
          <div class="scene-card__row">
            <button class="btn btn--sm btn--primary" type="button" data-nav="scene" data-scene="transfer">进入功能页</button>
            <button class="btn btn--sm" type="button" data-nav="chat" data-prefill="转给李娜 500 元">去对话</button>
          </div>
        </article>
        <article class="scene-card">
          <p class="scene-card__k">② 账单分析</p>
          <p class="scene-card__d">收支结构、分类修正、商户排行与预算；异常线索高分才有依据。</p>
          <div class="scene-card__row">
            <button class="btn btn--sm btn--primary" type="button" data-nav="scene" data-scene="bills">进入功能页</button>
            <button class="btn btn--sm" type="button" data-nav="chat" data-prefill="查一下上月账单">去对话</button>
          </div>
        </article>
        <article class="scene-card">
          <p class="scene-card__k">③ 理财</p>
          <p class="scene-card__d">风险测评 → 适当性 → 申购 / 赎回；超等级灰显并写原因。</p>
          <div class="scene-card__row">
            <button class="btn btn--sm btn--primary" type="button" data-nav="scene" data-scene="wealth">进入功能页</button>
            <button class="btn btn--sm" type="button" data-nav="chat" data-prefill="查余额">去对话</button>
          </div>
        </article>
        <article class="scene-card">
          <p class="scene-card__k">④ 卡片</p>
          <p class="scene-card__d">锁卡 / 挂失 / 补卡 / 调额；红动作走强验证。</p>
          <div class="scene-card__row">
            <button class="btn btn--sm btn--primary" type="button" data-nav="scene" data-scene="cards">进入功能页</button>
            <button class="btn btn--sm" type="button" data-nav="chat" data-prefill="锁卡">去对话</button>
          </div>
        </article>
        <article class="scene-card">
          <p class="scene-card__k">⑤ 订阅</p>
          <p class="scene-card__d">管理代扣协议：查询已签协议、关闭银行卡侧扣款授权；累计扣款与解约后对账可查。</p>
          <div class="scene-card__row">
            <button class="btn btn--sm btn--primary" type="button" data-nav="scene" data-scene="subscriptions">进入功能页</button>
            <button class="btn btn--sm" type="button" data-nav="chat" data-prefill="关闭扣款协议">去对话</button>
          </div>
        </article>
        <article class="scene-card">
          <p class="scene-card__k">⑥ 生活事件</p>
          <p class="scene-card__d">一句话串多个域，每步一张卡、逐张确认。</p>
          <div class="scene-card__row">
            <button class="btn btn--sm btn--primary" type="button" data-nav="scene" data-scene="life">进入功能页</button>
            <button class="btn btn--sm" type="button" data-nav="chat">去对话</button>
          </div>
        </article>
      </div>
      <!-- 窄屏（≤600px，PWA 安装态）用 2 列场景宫格，与「原型-app」一致；桌面隐藏 -->
      <div class="home-tiles" id="home-scene-list" aria-label="场景宫格"></div>
      <section class="home-recent" id="home-recent" hidden>
        <p class="side__label">最近对话</p>
        <div class="convs" id="home-recent-list"></div>
      </section>
      <section class="panel">
        <p class="panel__title">工具</p>
        <div class="btn-row">
          <button class="btn" type="button" data-nav="records">记录与明细</button>
          <button class="btn" type="button" data-nav="me">我的</button>
        </div>
      </section>
    </section>

    <section class="view view--chat" id="view-chat">
    <div id="stage" class="stream" aria-live="polite" aria-relevant="additions text">
      <section id="conversation-welcome" class="conversation-welcome">
        <span class="welcome-mark" aria-hidden="true">AI</span>
        <h2>从一句话开始</h2>
        <p>查账单、办转账、管理卡片与订阅。缺少的信息，我会向您确认；需要授权的操作，会先给您<b>计划卡</b>。</p>
        <div class="btn-row" id="presets"></div>
        <p class="muted welcome-note">演示环境 · 模拟资金与演示商户订单</p>
        <p class="muted welcome-note">绿＝仅自动查询 · 黄＝由您确认 · 红＝需要强验证</p>
      </section>
    </div>

    <div id="actions-parking" hidden>
      <div class="task-actions" id="task-actions">
        <button class="btn btn--primary" id="btn-confirm" type="button" disabled>确认并执行</button>
        <details class="task-more"><summary>更多办理操作</summary><div class="btn-row">
          <button class="btn" id="btn-interrupt" type="button" disabled>中断</button>
          <button class="btn" id="btn-resume" type="button" disabled>恢复</button>
          <button class="btn btn--ghost" id="btn-reject" type="button" disabled>放弃计划</button>
          <button class="btn btn--danger" id="btn-reverse" type="button" disabled>回退已执行步骤</button>
          <button class="btn btn--ghost" id="btn-takeover" type="button" disabled>查看人工接管包</button>
        </div></details>
      </div>
    </div>

    <section class="composer" aria-label="发送办理需求">
      <div class="composer__wrap">
        <p class="muted" id="offline-state" hidden>当前离线：只读静态壳可用，提交、确认和挑战已禁用。</p>
        <details class="object-context" id="object-context">
          <summary>本次办理对象 <span class="muted">选填 · 也可在对话中补充</span></summary>
          <div class="object-selectors" id="object-selectors">
            <div class="field"><label class="field__label" for="select-account">操作账户</label><select class="input" id="select-account" disabled></select></div>
            <div class="field"><label class="field__label" for="select-card">操作卡片</label><select class="input" id="select-card" disabled></select></div>
            <div class="field"><label class="field__label" for="select-subscription">已有订阅</label><select class="input" id="select-subscription" disabled></select></div>
            <div class="field"><label class="field__label" for="select-hold">有效冻结记录</label><select class="input" id="select-hold" disabled></select></div>
          </div>
        </details>
        <div class="row">
          <label class="sr-only" for="utterance">您的办理需求</label>
          <textarea class="textarea" id="utterance" rows="1" placeholder="说一句话，比如：转给李娜 500 元"></textarea>
          <button class="btn btn--primary" id="btn-submit" type="button" data-requires-login>发送</button>
        </div>
        <div class="composer-footer">
          <button class="btn btn--ghost" id="btn-due" type="button">检查到期待办</button>
          <span class="muted composer-tip">Enter 发送 · Shift + Enter 换行</span>
          <p class="muted business-date" id="business-date">正在读取演示数据日期与可选对象…</p>
        </div>
      </div>
    </section>
    </section>

    <section class="view" id="view-scene" hidden>
      <header class="view__head">
        <div>
          <h2 id="scene-title">场景</h2>
          <p class="muted" id="scene-sub"></p>
        </div>
        <div class="btn-row">
          <button class="btn btn--sm" type="button" data-nav="home">返回首页</button>
          <button class="btn btn--sm btn--primary" type="button" id="btn-scene-chat">去对话补充</button>
        </div>
      </header>

      <div class="scene-block" data-scene-block="transfer" hidden>
        <section class="panel" id="quota-panel">
          <div class="scene-head">
            <p class="panel__title">转账额度</p>
            <span class="muted" id="quota-status">正在读取额度…</span>
          </div>
          <div id="quota-body"></div>
          <details class="transfer-limit-help">
            <summary>限额说明与调整方式</summary>
            <ul class="muted" id="quota-notes"></ul>
            <button class="btn btn--sm btn--ghost" type="button" data-nav="scene" data-scene="cards">去卡片页调整通道限额</button>
          </details>
        </section>

        <section class="panel" id="quick-transfer-panel">
          <div class="scene-head">
            <p class="panel__title">转账</p>
            <span class="muted">带 * 为必填</span>
          </div>
          <p class="muted scene-hint">从本人账户转出。提交后会先核对收款人信息，再进入验证环节。</p>
          <div class="transfer-form">
            <label class="field">
              <span class="field__label">付款账户 *</span>
              <select class="input" id="transfer-account"></select>
              <span class="field__hint" id="transfer-account-balance">可用余额 —</span>
            </label>

            <div class="field">
              <span class="field__label">收款人姓名 *</span>
              <div class="payee-picker" id="payee-picker">
                <input class="input" id="quick-payee" type="text" autocomplete="off" maxlength="20"
                       aria-label="收款人" placeholder="输入姓名，或从常用收款人里选" aria-autocomplete="list"
                       aria-controls="payee-candidates" aria-expanded="false" />
                <ul class="payee-candidates" id="payee-candidates" role="listbox" hidden></ul>
              </div>
              <span class="field__hint" id="payee-picked-hint">从常用收款人选择会自动带出账号与开户行。</span>
            </div>

            <div class="transfer-grid">
              <label class="field">
                <span class="field__label">收款账号 *</span>
                <input class="input" id="quick-payee-account" type="text" inputmode="numeric"
                       autocomplete="off" maxlength="23" placeholder="填写卡号；演示仅保存后四位" />
              </label>
              <label class="field">
                <span class="field__label">开户行 *</span>
                <select class="input" id="quick-payee-bank">
                  <option value="">请选择开户行</option>
                  <option>招商银行</option>
                  <option>工商银行</option>
                  <option>建设银行</option>
                  <option>农业银行</option>
                  <option>中国银行</option>
                  <option>交通银行</option>
                  <option>其他银行</option>
                </select>
              </label>
            </div>

            <label class="field">
              <span class="field__label">收款人手机号（选填）</span>
              <input class="input" id="quick-payee-phone" type="tel" inputmode="numeric"
                     maxlength="11" autocomplete="off" placeholder="用于到账通知" />
            </label>

            <fieldset class="transfer-methods" id="transfer-methods">
              <legend>转账方式 *</legend>
              <label class="method-card">
                <input type="radio" name="transfer-method" value="realtime" checked />
                <b>实时到账</b><span>7×24 秒到 · 演示即时记账</span>
              </label>
              <label class="method-card">
                <input type="radio" name="transfer-method" value="normal" />
                <b>普通到账</b><span>预计 2 小时内 · 演示即时记账</span>
              </label>
              <label class="method-card">
                <input type="radio" name="transfer-method" value="next_day" />
                <b>次日到账</b><span>次日 00:00 后发出 · 可撤销</span>
              </label>
              <label class="method-card">
                <input type="radio" name="transfer-method" value="large" />
                <b>大额通道</b><span>5 万以上 · 工作日处理</span>
              </label>
            </fieldset>
            <p class="muted" id="transfer-method-note">实时到账：预计秒到；单笔限额以卡片转账通道为准。</p>

            <label class="field">
              <span class="field__label">转账金额（元）*</span>
              <input class="input" id="quick-amount" type="number" min="0.01" step="0.01"
                     inputmode="decimal" placeholder="例如 1850" />
              <span class="field__hint" id="quick-amount-hint"></span>
            </label>

            <div class="transfer-grid">
              <label class="field">
                <span class="field__label">用途</span>
                <select class="input" id="quick-purpose">
                  <option value="">未选择</option>
                  <option value="rent">房租</option>
                  <option value="salary">工资</option>
                  <option value="reimburse">报销</option>
                  <option value="other">其他</option>
                </select>
              </label>
              <label class="field">
                <span class="field__label">附言（选填）</span>
                <input class="input" id="quick-remark" type="text" maxlength="50" autocomplete="off"
                       placeholder="如 十月房租" />
              </label>
            </div>

            <p class="muted transfer-fee" id="transfer-fee">手续费 ¥0.00（演示环境免收；真实跨行费用以银行实际为准）</p>
            <div class="fraud-alert" id="transfer-fraud" hidden></div>

            <div class="btn-row">
              <button class="btn btn--primary" type="button" id="quick-transfer-submit">确认转账</button>
              <button class="btn btn--ghost" type="button" id="quick-transfer-chat">用 AI 助理转账</button>
              <span class="muted" id="quick-transfer-status" role="status" aria-live="polite"></span>
            </div>
            <div id="quick-transfer-card"></div>
          </div>
        </section>

        <section class="panel" id="transfers-panel">
          <div class="scene-head">
            <p class="panel__title">最近转账</p>
            <button class="btn btn--sm btn--ghost" type="button" data-nav="records" data-record-tab="transfers">看全部</button>
          </div>
          <p class="muted" id="transfers-status">正在读取最近转账…</p>
          <ul class="records" id="transfers-list"></ul>
        </section>

        <details class="scene-note">
          <summary>转账说明</summary>
          <ul class="muted">
            <li>到账时间以银行实际处理为准；演示环境为即时记账。</li>
            <li>次日到账登记后不扣款，可在「记录与明细」中撤销。</li>
            <li>手续费以银行实际收取为准；演示环境免收。</li>
            <li>大额转账需要完成一次性验证码；演示验证码不是真实短信或人脸。</li>
          </ul>
        </details>
      </div>

      <div class="scene-block" data-scene-block="bills" hidden>
        <section class="panel" id="bills-panel">
          <div class="demo-banner" id="bills-demo-banner" hidden>
            当前为演示数据，接入真实账单后可用
          </div>
          <div class="bills-period" id="bills-period" role="group" aria-label="账期">
            <button class="bills-period__item is-active" type="button" data-bills-period="current" aria-pressed="true">本月</button>
            <button class="bills-period__item" type="button" data-bills-period="previous" aria-pressed="false">上月</button>
            <label class="bills-period__pick">选月份
              <select class="input" id="bills-period-month">
                <option value="">请选择年月</option>
              </select>
            </label>
            <span class="bills-period__hint muted" id="bills-period-hint"></span>
          </div>
          <p class="panel__title">账单分析</p>
          <p class="muted" id="bills-status">正在读取账单识别结果…</p>
          <div id="bills-summary"></div>
          <div id="bills-budget"></div>
          <div id="bills-charts"></div>
          <div id="bills-merchants"></div>
          <div id="bills-leads"></div>
          <details class="bills-more" id="bills-subscriptions">
            <summary>订阅 / 自动续费</summary>
            <div id="bills-autorenew"></div>
          </details>
          <details class="bills-details" id="bills-details">
            <summary>账单明细（本期消费）</summary>
            <div id="bills-table"></div>
          </details>
          <details class="bills-more" id="bills-transfers">
            <summary>转账 / 还款（不计入消费）</summary>
            <div id="bills-transfers-table"></div>
          </details>
          <div class="btn-row">
            <button class="btn btn--sm" type="button" data-bills-export>导出本期账单 CSV</button>
            <button class="btn btn--sm btn--ghost" type="button" data-nav="records" data-record-tab="suspicions">看可疑清单</button>
          </div>
          <p class="muted bills-more__note" id="bills-disclaimer" hidden>识别结果供核对，不作最终结论。</p>
        </section>
        <dialog class="bills-dialog" id="bills-category-dialog">
          <div class="bills-dialog__head">
            <p class="panel__title">修正商户分类</p>
            <button class="btn btn--sm btn--ghost" type="button" data-bills-dialog-close>关闭</button>
          </div>
          <p class="muted" id="bills-category-merchant"></p>
          <label class="field">
            <span class="field__label">归类到</span>
            <select class="input" id="bills-category-select"></select>
          </label>
          <p class="muted" id="bills-category-hint"></p>
          <div class="btn-row">
            <button class="btn btn--sm btn--primary" type="button" data-bills-category-save>保存并记住</button>
            <button class="btn btn--sm btn--ghost" type="button" data-bills-dialog-close>取消</button>
          </div>
        </dialog>
      </div>

      <div class="scene-block" data-scene-block="wealth" hidden>
        <section class="panel" id="wealth-panel">
          <div class="scene-head">
            <p class="panel__title">理财</p>
            <span class="muted" id="wealth-status">正在读取测评状态与持仓…</span>
          </div>
          <div id="wealth-viz">
            <div class="wealth-loading" role="status" aria-live="polite">
              <p class="muted">正在读取风险等级与持仓占比…</p>
              <span class="skeleton"></span><span class="skeleton"></span><span class="skeleton"></span>
            </div>
          </div>
          <dialog class="wealth-dialog" id="wealth-assessment">
            <div class="wealth-dialog__head">
              <p class="panel__title">风险测评</p>
              <button class="btn btn--sm btn--ghost" type="button" id="btn-wealth-assessment-close">关闭</button>
            </div>
            <div id="wealth-assessment-body"><p class="muted">尚未读取风险测评问卷。</p></div>
          </dialog>
          <p class="panel__title panel__title--sub">产品目录 <span class="tech">按测评等级判定，超等级不可买</span></p>
          <div id="wealth-products">
            <div class="wealth-loading" role="status" aria-live="polite">
              <p class="muted">正在读取产品目录…</p>
              <span class="skeleton"></span><span class="skeleton"></span><span class="skeleton"></span>
            </div>
          </div>
          <p class="panel__title panel__title--sub">我的持仓</p>
          <div id="wealth-holdings">
            <div class="wealth-loading" role="status" aria-live="polite">
              <p class="muted">正在读取持仓…</p>
              <span class="skeleton"></span><span class="skeleton"></span>
            </div>
          </div>
          <!-- 页内计划卡：申购 / 赎回都在本页出卡，不把用户甩到对话页去看不见的地方 -->
          <div id="wealth-plan" hidden></div>
          <p class="muted" id="wealth-plan-status" hidden></p>
          <div class="btn-row">
            <button class="btn btn--sm" type="button" data-nav="records" data-record-tab="holdings">看持仓记录</button>
          </div>
        </section>
      </div>

      <div class="scene-block" data-scene-block="cards" hidden>
        <section class="panel" id="cards-panel">
          <div class="scene-head">
            <p class="panel__title">卡片管理</p>
            <span class="muted" id="cards-status">正在读取卡片…</span>
          </div>
          <p class="muted scene-hint">临时冻结（口头挂失）可解、免费、到期自动解挂；正式挂失不可逆，需补卡换新卡号。所有操作在弹窗内完成一次性验证码与二次确认（演示验证码，非真实人脸 / 短信 / U 盾）。</p>
          <div id="cards-list"></div>
          <div id="cards-plan" hidden></div>
          <p class="muted" id="cards-plan-status" hidden></p>
          <div id="cards-visual"></div>
          <div class="btn-row">
            <button class="btn btn--sm" type="button" data-nav="records" data-record-tab="cards">看卡片状态</button>
          </div>
        </section>
      </div>

      <div class="scene-block" data-scene-block="subscriptions" hidden>
        <section class="panel" id="subscription-panel">
          <div class="scene-head">
            <p class="panel__title">订阅管理</p>
            <span class="muted" id="subscription-status">正在读取代扣协议…</span>
          </div>
          <p class="muted" id="subscription-boundary" hidden></p>
          <p class="muted" id="subscription-focus-note" hidden></p>
          <div id="subscription-summary"></div>
          <div id="subscription-filters"></div>
          <div id="subscription-timeline"></div>
          <div id="subscription-list"><p class="muted">尚未读取代扣协议。</p></div>
          <div id="subscription-plan" hidden></div>
          <p class="muted" id="subscription-plan-status" hidden></p>
          <div id="subscription-cancel-result" hidden></div>
          <dialog class="sub-cancel-dialog" id="subscription-cancel-dialog">
            <div class="sub-cancel-dialog__head">
              <p class="panel__title">关闭这个扣款协议？</p>
              <button class="btn btn--sm btn--ghost" type="button" data-sub-cancel-close>返回</button>
            </div>
            <div class="sub-cancel-dialog__body" id="subscription-cancel-body"></div>
            <div class="sub-cancel-dialog__foot">
              <button class="btn btn--ghost" type="button" data-sub-cancel-close>先不关闭</button>
              <button class="btn btn--danger" type="button" data-sub-cancel-confirm>关闭扣款协议</button>
            </div>
          </dialog>
          <details class="scene-note">
            <summary>这些商户是怎么识别出来的</summary>
            <ul>
              <li>已登记的代扣协议来自本行代扣登记；「疑似」来自流水周期推断，需本人核对。</li>
              <li>协议编号、协议类型与演示历史扣款由内置演示数据派生，不是真实银联/网联协议号。</li>
              <li>数据来源：内置演示数据；不是真实账单或真实银行协议。</li>
              <li>标注「推算」的下次扣款日按历史周期计算，实际扣款日以商户为准。</li>
              <li>本页只展示本行能看到的代扣协议与扣款；其他卡或其他渠道的扣款不在本页范围内。</li>
            </ul>
          </details>
          <div class="btn-row">
            <button class="btn btn--sm" type="button" data-nav="records" data-record-tab="charges">扣款记录</button>
            <button class="btn btn--sm" type="button" data-action="refresh-subscriptions">刷新数据</button>
          </div>
        </section>
      </div>

      <div class="scene-block" data-scene-block="life" hidden>
        <section class="panel" id="life-events-panel">
          <div class="scene-head">
            <p class="panel__title">生活日历</p>
            <span class="muted" id="life-events-status">正在读取生活事件…</span>
          </div>
          <p class="scene-hint">按预约触发日 / 计划日期落点；不是商户日程，同一天同类只画一个点，点开看清单。</p>
          <div id="life-calendar"><p class="muted">尚未读取生活日历。</p></div>
          <div class="life-bar">
            <div class="life-bar__text">
              <b>组合任务台</b>
              <span class="muted">选一个生活事件，系统拆成多步逐张确认。</span>
            </div>
            <button class="btn btn--sm btn--primary" type="button" id="btn-life-workbench">打开任务台</button>
          </div>
          <p class="panel__title panel__title--sub">最近记录</p>
          <div id="life-events-list"><p class="muted">尚未读取生活事件。</p></div>
        </section>
        <dialog class="life-dialog" id="life-workbench">
          <div class="life-dialog__head">
            <p class="panel__title">组合任务台</p>
            <button class="btn btn--sm btn--ghost" type="button" id="btn-life-workbench-close">返回</button>
          </div>
          <div id="life-workbench-body"><p class="muted">尚未读取事件原型。</p></div>
        </dialog>
        <dialog class="life-dialog" id="life-record-dialog">
          <div class="life-dialog__head">
            <p class="panel__title">生活事件详情</p>
            <button class="btn btn--sm btn--ghost" type="button" id="btn-life-record-close">关闭</button>
          </div>
          <div class="life-dialog__body" id="life-record-body"></div>
        </dialog>
      </div>
    </section>

    <section class="view" id="view-records" hidden>
      <header class="view__head">
        <div>
          <h2>记录与明细</h2>
          <p class="muted">AI 做过的事都在这里查得到；扣款记录按账户显示这笔钱从哪扣出。</p>
        </div>
      </header>
      <div class="tabs" id="records-tabs" role="tablist" aria-label="记录分类">
        <button class="tab" type="button" role="tab" data-record-tab="transfers" aria-selected="true">转账记录</button>
        <button class="tab" type="button" role="tab" data-record-tab="holdings" aria-selected="false">持仓</button>
        <button class="tab" type="button" role="tab" data-record-tab="cards" aria-selected="false">卡片状态</button>
        <button class="tab" type="button" role="tab" data-record-tab="suspicions" aria-selected="false">可疑清单</button>
        <button class="tab" type="button" role="tab" data-record-tab="charges" aria-selected="false">扣款记录</button>
      </div>
      <div id="records-body"></div>
    </section>

    <section class="view" id="view-me" hidden>
      <header class="view__head">
        <div>
          <h2>我的</h2>
          <p class="muted">身份、登录安全与关联数据。</p>
        </div>
        <button class="btn btn--sm" type="button" data-nav="home">返回首页</button>
      </header>
      <div id="view-me-body"><p class="muted">正在读取账户信息…</p></div>
    </section>
  </main>

  <section id="task-guard" class="task-guard" role="alertdialog" aria-modal="false"
           aria-labelledby="task-guard-title" hidden>
    <h2 id="task-guard-title">当前办理尚未结束</h2>
    <p>您可以继续当前办理，或结束后开始新的办理。结束仅放弃未执行部分，不会冲正已执行的资金操作；如需退回，请先使用当前计划的回退入口。</p>
    <div class="btn-row">
      <button class="btn btn--primary" id="btn-continue-task" type="button">继续当前办理</button>
      <button class="btn" id="btn-end-task" type="button">结束并新建</button>
    </div>
  </section>

  <aside class="trace rail" id="trace" aria-label="处理详情" aria-hidden="true">
    <div class="trace__head">
      <p class="panel__title">处理详情</p>
      <button class="btn btn--ghost" id="btn-close-trace" type="button">关闭</button>
    </div>

    <section class="panel">
      <p class="panel__title">执行时间线</p>
      <ul class="timeline" id="timeline"><li class="timeline__item">等待操作…</li></ul>
    </section>

    <section class="panel">
      <p class="panel__title">审计链 <span class="tech">sha256 链</span></p>
      <ul class="chain" id="chain"><li class="chain__empty">尚未产生事件</li></ul>
    </section>

    <section class="panel" id="insights-panel">
      <p class="panel__title">主动洞察</p>
      <p class="muted" id="insights-status">正在读取站内洞察…</p>
      <div id="insights-list"><p class="muted">尚未读取洞察。</p></div>
    </section>
  </aside>

  <div class="modal-backdrop" id="profile-backdrop" hidden></div>
  <section class="profile-modal" id="profile-panel" role="dialog" aria-modal="true"
           aria-labelledby="profile-title" hidden>
    <div class="profile-modal__head">
      <p class="panel__title" id="profile-title">个人中心</p>
      <button class="btn btn--ghost" id="btn-close-profile" type="button">关闭</button>
    </div>
    <div id="profile-body"><p class="muted">正在读取账户信息…</p></div>
  </section>
  <nav class="tabbar" id="app-tabbar" aria-label="底部导航">
    <button class="tabbar__item" type="button" data-nav="home"><i class="tabbar__icon" aria-hidden="true"></i>首页</button>
    <button class="tabbar__item" type="button" data-nav="chat"><i class="tabbar__icon" aria-hidden="true"></i>对话</button>
    <button class="tabbar__item" type="button" data-nav="records" data-record-tab="transfers"><i class="tabbar__icon" aria-hidden="true"></i>记录</button>
    <button class="tabbar__item" type="button" data-nav="me"><i class="tabbar__icon" aria-hidden="true"></i>我的</button>
  </nav>
</div>

<dialog class="app-dialog" id="app-dialog" aria-labelledby="app-dialog-title">
  <div class="app-dialog__head">
    <p class="panel__title" id="app-dialog-title">确认操作</p>
    <button class="btn btn--sm btn--ghost" type="button" data-app-dialog-close>关闭</button>
  </div>
  <div class="app-dialog__body">
    <p class="app-dialog__text" id="app-dialog-text"></p>
    <label class="field app-dialog__field" id="app-dialog-field" hidden>
      <span class="field__label">登录口令</span>
      <input class="input" id="app-dialog-password" type="password" autocomplete="current-password" />
    </label>
    <p class="app-dialog__error" id="app-dialog-error" role="alert" hidden></p>
  </div>
  <div class="app-dialog__foot">
    <button class="btn btn--ghost" type="button" data-app-dialog-close>取消</button>
    <button class="btn btn--danger" id="app-dialog-confirm" type="button" data-requires-login>确认</button>
  </div>
</dialog>

<dialog class="card-flow" id="card-flow-dialog" aria-labelledby="card-flow-title">
  <div class="card-flow__head">
    <div>
      <p class="panel__title" id="card-flow-title">卡片操作</p>
      <p class="muted card-flow__sub" id="card-flow-sub"></p>
    </div>
    <button class="btn btn--sm btn--ghost" type="button" data-card-flow-close>关闭</button>
  </div>
  <ol class="card-flow__steps" id="card-flow-steps"></ol>
  <div class="card-flow__body" id="card-flow-body"></div>
  <p class="card-flow__error" id="card-flow-error" role="alert" hidden></p>
  <div class="card-flow__foot">
    <button class="btn btn--ghost" type="button" data-card-flow-back id="card-flow-back">上一步</button>
    <button class="btn btn--primary" type="button" id="card-flow-next" data-requires-login>下一步</button>
  </div>
</dialog>

<dialog class="sub-detail" id="sub-detail" aria-labelledby="sub-detail-title">
  <div class="sub-detail__head">
    <p class="panel__title" id="sub-detail-title">协议详情 · 扣款记录</p>
    <button class="btn btn--sm btn--ghost" type="button" data-sub-detail-close>关闭</button>
  </div>
  <div class="sub-detail__body" id="sub-detail-body"></div>
</dialog>

<dialog class="sub-detail" id="audit-dialog" aria-labelledby="audit-dialog-title">
  <div class="sub-detail__head">
    <p class="panel__title" id="audit-dialog-title">审计与溯源</p>
    <button class="btn btn--sm btn--ghost" type="button" data-audit-close>关闭</button>
  </div>
  <div class="sub-detail__body" id="audit-body"></div>
</dialog>

<script src="/assets/app.js?v=92"></script>
<script src="/assets/wealth_trade.js?v=1"></script>
</body>
</html>
"""

INDEX_HTML = _PAGE_HTML

# 登录页：独立自包含页面（无外链资源、无设计仓依赖），仅同源调用 `/api/auth/login`。
LOGIN_HTML = """<!DOCTYPE html>
<html lang="zh-CN" data-theme="light">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<meta name="theme-color" content="#0b4fd8" />
<title>登录 · AI 银行副驾</title>
<style>
  :root { color-scheme: light; }
  * { box-sizing: border-box; }
  body {
    margin: 0; min-height: 100vh; display: flex; align-items: center; justify-content: center;
    background: radial-gradient(900px 520px at 50% 0%, #e8f0ff 0%, #f2f6fc 55%, #eef2f9 100%);
    color: #16233a; padding: 24px;
    font-family: "PingFang SC", "Microsoft YaHei", system-ui, -apple-system, sans-serif;
  }
  .login-card {
    width: 100%; max-width: 340px; background: #fff; border-radius: 16px; padding: 30px 26px;
    box-shadow: 0 20px 50px rgba(16, 38, 84, .12); border: 1px solid rgba(16, 38, 84, .05);
  }
  .brand { font-size: 22px; font-weight: 700; letter-spacing: .3px; margin: 0 0 6px; }
  .brand em { font-style: normal; color: #0b4fd8; }
  .sub { margin: 0 0 22px; font-size: 13px; line-height: 1.5; color: #6b7a90; }
  label { display: block; font-size: 12px; color: #40506b; margin: 12px 0 6px; }
  input {
    width: 100%; height: 40px; padding: 0 12px; font-size: 14px; color: #16233a;
    border: 1px solid #d5ddea; border-radius: 8px; background: #fff; outline: none;
  }
  input:focus { border-color: #0b4fd8; box-shadow: 0 0 0 3px rgba(11, 79, 216, .12); }
  button {
    width: 100%; height: 40px; margin-top: 20px; border: 0; border-radius: 8px; cursor: pointer;
    background: #0b4fd8; color: #fff; font-size: 14px; font-weight: 600;
  }
  button:disabled { opacity: .6; cursor: default; }
  .err {
    margin-top: 12px; padding: 10px 12px; border-radius: 8px; font-size: 12px;
    background: #fdecea; color: #b3362a; border: 1px solid #f5c6c1;
  }
  .demo {
    margin-top: 20px; padding: 10px 12px; border-radius: 10px; font-size: 12px; line-height: 1.6;
    background: #f2f6fc; color: #55637a; border: 1px solid #e3ebf7;
  }
  .demo b { color: #16233a; font-weight: 600; }
  .foot { margin-top: 16px; font-size: 12px; color: #93a0b4; text-align: center; }
  .preview {
    margin-top: 16px; padding: 10px 12px; border-radius: 10px; font-size: 12px; line-height: 1.6;
    background: #fbfcfe; color: #40506b; border: 1px solid #e3ebf7;
  }
  .preview summary { cursor: pointer; font-weight: 600; color: #16233a; }
  .preview-grid {
    display: grid; grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 6px; margin: 10px 0 8px;
  }
  .preview-grid span {
    padding: 6px 8px; border-radius: 8px; background: #eef4ff; color: #0b4fd8;
    font-weight: 600; text-align: center;
  }
  .preview ul { margin: 0; padding-left: 18px; }
  .preview li { margin: 4px 0; }
</style>
</head>
<body>
<form class="login-card" id="login-form" autocomplete="on">
  <p class="brand">AI 银行<em>副驾</em></p>
  <p class="sub">一句话，智能办理银行业务</p>
  <label for="username">用户名</label>
  <input id="username" name="username" type="text" autocomplete="username" required />
  <label for="password">密码</label>
  <input id="password" name="password" type="password" autocomplete="current-password" required />
  <button id="btn-login" type="submit">登录</button>
  <div class="err" id="login-error" hidden></div>
  <div class="demo">
    __DEMO_CREDENTIALS__<br />
    模拟银行环境，不连接真实银行账户。对话与办理记录只存在本演示库的本人账户下，
    演示结束后由部署方整体清除；AI 回复由上游模型服务生成，请勿输入真实敏感信息。
  </div>
  <details class="preview">
    <summary>先逛逛：功能预览（只读，不会办理）</summary>
    <div class="preview-grid">
      <span>智能转账</span><span>账单分析</span><span>卡片管理</span>
      <span>理财</span><span>订阅</span><span>生活事件</span>
    </div>
    <ul>
      <li>AI 先给出办理方案，确认后才执行；高风险动作需要额外验证。</li>
      <li>每一步都有审计 ID，可在「我的 → 审计与溯源」追溯。</li>
      <li>演示环境使用模拟资金，不连接真实银行账户。</li>
    </ul>
    <p class="muted">登录后可进入对话办理；未登录仅能浏览演示数据，不能提交业务。</p>
  </details>
  <p class="foot">模拟银行环境 · 不连接真实银行账户</p>
</form>
<script>
(function () {
  var form = document.getElementById("login-form");
  var btn = document.getElementById("btn-login");
  var err = document.getElementById("login-error");

  function showError(text) {
    err.textContent = text;
    err.hidden = false;
  }

  form.addEventListener("submit", function (event) {
    event.preventDefault();
    err.hidden = true;
    var username = document.getElementById("username").value.trim();
    var password = document.getElementById("password").value;
    if (!username || !password) {
      showError("请填写用户名和密码。");
      return;
    }
    btn.disabled = true;
    btn.textContent = "登录中…";
    fetch("/api/auth/login", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: username, password: password })
    }).then(function (resp) {
      return resp.json().catch(function () { return {}; }).then(function (body) {
        return { ok: resp.ok, status: resp.status, body: body || {} };
      });
    }).then(function (result) {
      if (result.ok) {
        window.location.replace("/");
        return;
      }
      if (result.status === 429) {
        showError(result.body.message || "尝试次数过多，请稍后再试。");
      } else if (result.status === 401) {
        showError("用户名或密码不正确，请使用页面下方的演示账号和口令。");
      } else {
        showError(result.body.message || "登录失败，请稍后再试。");
      }
      btn.disabled = false;
      btn.textContent = "登录";
    }).catch(function () {
      showError("网络异常，请检查连接后重试。");
      btn.disabled = false;
      btn.textContent = "登录";
    });
  });
})();
</script>
</body>
</html>
"""


def render_login_page(accounts: Sequence[str], demo_password: str | None) -> str:
    """注入演示账号与口令；口令为空时只提示向现场负责人获取。"""
    if demo_password:
        names = " / ".join(escape(name) for name in accounts) or "当前演示账号"
        credentials = (
            f"<b>演示账号</b>：{names}<br />"
            f"<b>演示口令</b>：<code>{escape(demo_password)}</code>"
        )
    else:
        credentials = "演示账号与口令由现场负责人提供，不在页面公开。"
    return LOGIN_HTML.replace("__DEMO_CREDENTIALS__", credentials)

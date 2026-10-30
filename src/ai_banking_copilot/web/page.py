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
<link rel="stylesheet" href="/assets/tokens.css?v=2" />
<link rel="stylesheet" href="/assets/app.css?v=7" />
<link rel="stylesheet" href="/assets/workspace.css?v=39" />
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
        <button class="appnav__item" type="button" data-nav="health">渠道健康</button>
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
          <p class="scene-card__d">中文数字、完整手机号、日累计限额；大额走强验证。</p>
          <div class="scene-card__row">
            <button class="btn btn--sm btn--primary" type="button" data-nav="scene" data-scene="transfer">进入功能页</button>
            <button class="btn btn--sm" type="button" data-nav="chat" data-prefill="转给李娜 500 元">去对话</button>
          </div>
        </article>
        <article class="scene-card">
          <p class="scene-card__k">② 账单分析</p>
          <p class="scene-card__d">重复扣款 / 涨价 / 隐藏费用 / 深夜 / 异地；带置信度与免责。</p>
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
          <p class="scene-card__d">涨价、下次扣款日与扣款记录可查；取消前说清边界。</p>
          <div class="scene-card__row">
            <button class="btn btn--sm btn--primary" type="button" data-nav="scene" data-scene="subscriptions">进入功能页</button>
            <button class="btn btn--sm" type="button" data-nav="chat" data-prefill="订阅取消">去对话</button>
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
          <button class="btn" type="button" data-nav="health">渠道健康</button>
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

    <section id="task-guard" class="task-guard" role="alertdialog" aria-modal="false"
             aria-labelledby="task-guard-title" hidden>
      <h2 id="task-guard-title">当前办理尚未结束</h2>
      <p>您可以继续当前办理，或结束后开始新的办理。结束仅放弃未执行部分，不会冲正已执行的资金操作；如需退回，请先使用当前计划的回退入口。</p>
      <div class="btn-row">
        <button class="btn btn--primary" id="btn-continue-task" type="button">继续当前办理</button>
        <button class="btn" id="btn-end-task" type="button">结束并新建</button>
      </div>
    </section>

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
          <button class="btn btn--primary" id="btn-submit" type="button">发送</button>
        </div>
        <div class="composer-footer">
          <button class="btn btn--ghost" id="btn-due" type="button">检查到期待办</button>
          <span class="muted composer-tip">Enter 发送 · Shift + Enter 换行</span>
          <p class="muted business-date" id="business-date">正在读取演示业务日期与可选对象…</p>
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
          <button class="btn btn--sm btn--primary" type="button" id="btn-scene-chat">去对话办这一笔</button>
        </div>
      </header>

      <div class="scene-block" data-scene-block="transfer" hidden>
        <section class="panel" id="quota-panel">
          <div class="scene-head">
            <p class="panel__title">今日转账额度</p>
            <span class="muted" id="quota-status">正在读取额度…</span>
          </div>
          <div id="quota-body"></div>
        </section>

        <section class="panel">
          <p class="panel__title">快速转账</p>
          <p class="muted scene-hint">先出计划卡，确认后才会执行；红色动作本页不代做，会引导去对话做强验证。</p>
          <div class="scene-form">
            <label class="field">
              <span class="field__label">收款人</span>
              <input class="input" id="quick-payee" type="text" placeholder="例如 李娜" autocomplete="off" />
            </label>
            <label class="field">
              <span class="field__label">金额（元）</span>
              <input class="input" id="quick-amount" type="number" min="0.01" step="0.01" inputmode="decimal" placeholder="例如 500" />
            </label>
          </div>
          <div class="btn-row">
            <button class="btn btn--primary" type="button" id="quick-transfer-submit">确认转账</button>
            <span class="muted" id="quick-transfer-status" role="status" aria-live="polite"></span>
          </div>
          <div id="quick-transfer-card"></div>
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
          <summary>转账规则说明</summary>
          <ul class="muted">
            <li>绿色 = 纯查询，服务端直接执行；黄色 = 状态变更，需您点确认。</li>
            <li>红色 = 大额 / 不可逆，需服务端一次性验证码，本页只提示去对话完成。</li>
            <li>额度条读 <span class="mono">/api/options.quota</span>；已用超过日限额时标红「已超额」。</li>
            <li>本页与对话共用 <span class="mono">/api/submit</span> → <span class="mono">/api/confirm</span>，不新增后端接口。</li>
          </ul>
        </details>
      </div>

      <div class="scene-block" data-scene-block="bills" hidden>
        <section class="panel" id="bills-panel">
          <p class="panel__title">账单分析</p>
          <p class="muted" id="bills-status">正在读取账单识别结果…</p>
          <p class="muted" id="bills-disclaimer" hidden></p>
          <div id="bills-summary"></div>
          <div class="budget-set">
            <label class="budget-set__label" for="budget-input">本月预算</label>
            <input class="input input--amount" id="budget-input" type="number" min="0" step="1"
                   placeholder="如 8000" inputmode="decimal" />
            <button class="btn btn--sm btn--primary" type="button" data-action="save-budget">保存</button>
            <span class="muted budget-set__status" id="budget-status">只用于「月预算提醒」洞察，不是转账限额；转账的单笔 / 当日累计限额在计划卡里按服务端口径判定。</span>
          </div>
          <div id="bills-profile"></div>
          <div id="bills-charts"></div>
          <div id="bills-top3"></div>
          <div id="bills-findings"></div>
          <details class="bills-details" id="bills-details">
            <summary>账单明细（本期交易）</summary>
            <div id="bills-table"></div>
          </details>
          <div class="btn-row">
            <button class="btn btn--sm" type="button" data-nav="records" data-record-tab="suspicions">看可疑清单</button>
          </div>
        </section>
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
          <p class="muted scene-hint">四键直接办理：锁卡 / 解锁、挂失、调额、补虚拟卡。红色动作页内只生成计划卡，强验证在对话里完成。</p>
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
            <span class="muted" id="subscription-status">正在读取订阅识别结果…</span>
          </div>
          <div id="subscription-summary"></div>
          <div id="subscription-timeline"></div>
          <div id="subscription-list"><p class="muted">尚未读取订阅详情。</p></div>
          <div id="subscription-plan" hidden></div>
          <p class="muted" id="subscription-plan-status" hidden></p>
          <div id="subscription-cancel-result" hidden></div>
          <dialog class="sub-cancel-dialog" id="subscription-cancel-dialog">
            <div class="sub-cancel-dialog__head">
              <p class="panel__title">取消这个订阅？</p>
              <button class="btn btn--sm btn--ghost" type="button" data-sub-cancel-close>返回</button>
            </div>
            <div class="sub-cancel-dialog__body" id="subscription-cancel-body"></div>
            <div class="sub-cancel-dialog__foot">
              <button class="btn btn--ghost" type="button" data-sub-cancel-close>先不取消</button>
              <button class="btn btn--danger" type="button" data-sub-cancel-confirm>确认取消</button>
            </div>
          </dialog>
          <p class="muted" id="subscription-boundary" hidden></p>
          <details class="scene-note">
            <summary>取消口径与数据边界</summary>
            <ul>
              <li>取消只在本地把订阅状态改为「已取消订阅」，商户侧是否停止扣费以商户结果为准，本页不承诺已停扣。</li>
              <li>「疑似」订阅来自流水推断，需本人核对后再决定是否取消，系统不会自动升级为已确认。</li>
              <li>下次扣款日标注「预测值」的是按扣费周期推算，不是商户确认的扣款日。</li>
            </ul>
          </details>
          <div class="btn-row">
            <button class="btn btn--sm" type="button" data-nav="records" data-record-tab="charges">扣款记录</button>
            <button class="btn btn--sm" type="button" data-action="refresh-subscriptions">刷新订阅数据</button>
          </div>
        </section>
      </div>

      <div class="scene-block" data-scene-block="life" hidden>
        <section class="panel" id="life-events-panel">
          <div class="scene-head">
            <p class="panel__title">生活日历</p>
            <span class="muted" id="life-events-status">正在读取生活事件…</span>
          </div>
          <p class="scene-hint">按本人生成过的记录落在日期上；不是商户日程，同一天多条只算一个事件点。</p>
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
      </div>
    </section>

    <section class="view" id="view-records" hidden>
      <header class="view__head">
        <div>
          <h2>记录与明细</h2>
          <p class="muted">AI 做过的事都在这里查得到；渠道列区分 web / app / feishu。</p>
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

    <section class="view" id="view-health" hidden>
      <header class="view__head">
        <div>
          <h2>渠道健康</h2>
          <p class="muted">未接入的渠道如实显示 replay/未接入；这一页是网页 / APP 里唯一的渠道产品页，不是聊天页。</p>
        </div>
        <button class="btn btn--sm" type="button" id="btn-health-refresh">刷新</button>
      </header>
      <section class="panel" id="channel-health-panel">
        <p class="panel__title">渠道状态</p>
        <p class="muted" id="channel-health-status">正在读取渠道状态…</p>
        <div class="channel-health-table-wrap" id="channel-health-list">
          <table class="channel-health-table">
            <thead><tr><th>渠道</th><th>状态</th><th>最后事件</th><th>成功</th><th>失败</th><th>限流</th><th>回放模式</th></tr></thead>
            <tbody id="channel-health-body"><tr><td colspan="7">尚未读取渠道状态。</td></tr></tbody>
          </table>
        </div>
      </section>
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

  <aside class="trace rail" id="trace" aria-label="处理详情" aria-hidden="true">
    <div class="trace__head">
      <p class="panel__title">处理详情</p>
      <button class="btn btn--ghost" id="btn-close-trace" type="button">关闭</button>
    </div>

    <section class="panel" id="insights-panel">
      <p class="panel__title">主动洞察</p>
      <p class="muted" id="insights-status">正在读取站内洞察…</p>
      <div id="insights-list"><p class="muted">尚未读取洞察。</p></div>
    </section>

    <section class="panel">
      <p class="panel__title">审计链 <span class="tech">sha256 链</span></p>
      <ul class="chain" id="chain"><li class="chain__empty">尚未产生事件</li></ul>
    </section>

    <section class="panel">
      <p class="panel__title">执行时间线</p>
      <ul class="timeline" id="timeline"><li class="timeline__item">等待操作…</li></ul>
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

<script src="/assets/app.js?v=60"></script>
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
</style>
</head>
<body>
<form class="login-card" id="login-form" autocomplete="on">
  <p class="brand">AI 银行<em>副驾</em></p>
  <p class="sub">登录后，您的对话、生活事件与办理记录都会保存在自己的账户下。</p>
  <label for="username">用户名</label>
  <input id="username" name="username" type="text" autocomplete="username" required />
  <label for="password">密码</label>
  <input id="password" name="password" type="password" autocomplete="current-password" required />
  <button id="btn-login" type="submit">登录</button>
  <div class="err" id="login-error" hidden></div>
  <div class="demo">
    演示账号：<b>chenming</b> / <b>linjing</b><br />
    演示口令由部署方通过环境变量下发，请向现场负责人获取。
  </div>
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
        showError("用户名或密码不正确。");
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

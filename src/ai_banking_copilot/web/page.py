"""Web 渠道内联单页（版式对齐设计仓 `product-web.html` / `product-app.html`）。

- 骨架：左侧栏（品牌 / 新建 / 本页记录 / 常用办理）+ 主区（顶栏 / 对话流 / 输入区）
  + 右侧「处理详情」栏；窄屏与 `data-view="app"` 切换为 APP 端手机框版式。
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
<link rel="stylesheet" href="/assets/workspace.css?v=7" />
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
        <button class="appnav__item" type="button" data-nav="scene" data-scene="transfer">转账</button>
        <button class="appnav__item" type="button" data-nav="scene" data-scene="bills">账单分析</button>
        <button class="appnav__item" type="button" data-nav="scene" data-scene="wealth">理财</button>
        <button class="appnav__item" type="button" data-nav="scene" data-scene="cards">卡片</button>
        <button class="appnav__item" type="button" data-nav="scene" data-scene="subscriptions">订阅</button>
        <button class="appnav__item" type="button" data-nav="scene" data-scene="life">生活事件</button>
        <p class="side__label">办理</p>
        <button class="appnav__item" type="button" data-nav="chat">对话</button>
        <p class="side__label">工具</p>
        <button class="appnav__item" type="button" data-nav="records">记录与明细</button>
        <button class="appnav__item" type="button" data-nav="health">渠道健康</button>
      </nav>
      <p class="side__label">历史对话</p>
      <div class="convs" id="conversation-list"><p class="muted convs__empty">开始办理后，对话会保存在这里。</p></div>
      <p class="side__label">常用办理</p>
      <section class="convs convs--scenes" id="quick-scenes" aria-label="快捷场景列表"></section>
    </div>
    <div class="side__foot">
      <button class="side-user" id="btn-me-side" type="button" data-nav="me">
        <span class="side-user__avatar" aria-hidden="true">AI</span>
        <span class="side-user__meta">
          <span class="side-user__name" id="side-user-name">我的</span>
          <span class="side-user__sub">个人中心 · 登录身份</span>
        </span>
      </button>
      <p class="side-note">模拟银行环境 · 不连接真实银行账户<br />绿色仅自动查询 · 黄色由您确认 · 红色需要强验证</p>
    </div>
  </aside>

  <main class="main" id="main" aria-label="银行办理对话">
    <header class="topbar">
      <div class="top__brand">
        <div class="top__t">AI 银行副驾</div>
        <div class="top__s">今天，有什么可以帮您？</div>
      </div>
      <div class="topbar__nav">
        <button class="btn btn--ghost" id="btn-scenes-mobile" type="button"
                aria-controls="conversation-sidebar" aria-expanded="true">导航</button>
        <button class="btn btn--ghost" id="btn-trace-mobile" type="button"
                aria-controls="trace" aria-expanded="true">处理详情</button>
        <label class="switch">
          <input type="checkbox" id="tech-toggle" />
          技术详情
        </label>
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

    <section class="view" id="view-home" hidden>
      <header class="view__head">
        <div>
          <h2>今天想办什么？</h2>
          <p class="muted">六个场景都有入口。说一句话，AI 给计划卡，您确认后再执行。</p>
        </div>
        <span class="muted" id="home-note">模拟银行环境 · 不连接真实银行账户</span>
      </header>
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
    <div class="progress-steps" id="progress-steps" aria-live="polite" hidden></div>

    <div id="stage" class="stream" aria-live="polite" aria-relevant="additions text">
      <section id="conversation-welcome" class="conversation-welcome">
        <span class="welcome-mark" aria-hidden="true">AI</span>
        <h2>从一句话开始</h2>
        <p>查账单、办转账、管理卡片与订阅。缺少的信息，我会向您确认；需要授权的操作，会先给您<b>计划卡</b>。</p>
        <div class="btn-row" id="presets"></div>
        <p class="muted welcome-note">演示环境 · 模拟资金与演示商户订单</p>
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
        <section class="panel" id="transfers-panel">
          <p class="panel__title">转账记录</p>
          <p class="muted" id="transfers-status">正在读取最近转账…</p>
          <ul class="records" id="transfers-list"></ul>
          <div class="btn-row">
            <button class="btn btn--sm" type="button" data-nav="records" data-record-tab="transfers">看全部转账记录</button>
          </div>
        </section>
      </div>

      <div class="scene-block" data-scene-block="bills" hidden>
        <section class="panel">
          <p class="panel__title">账单分析</p>
          <p class="muted">功能页数据待接入（负责人：林晓研）。当前可先用「去对话」查账单。</p>
          <div class="btn-row">
            <button class="btn btn--sm" type="button" data-nav="records" data-record-tab="suspicions">看可疑清单</button>
          </div>
        </section>
      </div>

      <div class="scene-block" data-scene-block="wealth" hidden>
        <section class="panel" id="wealth-panel">
          <p class="panel__title">理财 · 测评与持仓</p>
          <div class="muted" id="wealth-status">正在读取测评状态与持仓…</div>
          <div id="wealth-assessment"><p class="muted">尚未读取风险测评问卷。</p></div>
          <p class="panel__title panel__title--sub">产品目录 <span class="tech">按测评等级判定</span></p>
          <div id="wealth-products"><p class="muted">尚未读取产品目录。</p></div>
          <p class="panel__title panel__title--sub">我的持仓</p>
          <div id="wealth-holdings"><p class="muted">尚未读取持仓。</p></div>
          <p class="muted" id="wealth-source"></p>
          <div class="btn-row">
            <button class="btn btn--sm" type="button" data-nav="records" data-record-tab="holdings">看持仓记录</button>
          </div>
        </section>
      </div>

      <div class="scene-block" data-scene-block="cards" hidden>
        <section class="panel">
          <p class="panel__title">卡片</p>
          <p class="muted">功能页数据待接入（负责人：林晓研）。当前可先用「去对话」锁卡或挂失。</p>
          <div class="btn-row">
            <button class="btn btn--sm" type="button" data-nav="records" data-record-tab="cards">看卡片状态</button>
          </div>
        </section>
      </div>

      <div class="scene-block" data-scene-block="subscriptions" hidden>
        <section class="panel" id="subscription-panel">
          <p class="panel__title">订阅识别与取消状态</p>
          <p class="muted" id="subscription-status">正在读取订阅识别结果…</p>
          <p class="muted" id="subscription-boundary" hidden></p>
          <div id="subscription-list"><p class="muted">尚未读取订阅详情。</p></div>
          <div id="subscription-cancel-result" hidden></div>
        </section>
      </div>

      <div class="scene-block" data-scene-block="life" hidden>
        <section class="panel" id="life-events-panel">
          <p class="panel__title">生活事件</p>
          <p class="muted" id="life-events-status">正在读取生活事件原型…</p>
          <div id="life-events-list"><p class="muted">尚未读取生活事件。</p></div>
        </section>
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
          <p class="muted">未接入的渠道如实显示 stub；这一页是网页 / APP 里唯一的渠道产品页，不是聊天页。</p>
        </div>
        <button class="btn btn--sm" type="button" id="btn-health-refresh">刷新</button>
      </header>
      <section class="panel" id="channel-health-panel">
        <p class="panel__title">渠道状态</p>
        <p class="muted" id="channel-health-status">正在读取渠道状态…</p>
        <div id="channel-health-list"><p class="muted">尚未读取渠道状态。</p></div>
      </section>
    </section>

    <section class="view" id="view-me" hidden>
      <header class="view__head">
        <div>
          <h2>我的</h2>
          <p class="muted">登录身份、关联数据与操作轨迹。</p>
        </div>
      </header>
      <div id="me-body"><p class="muted">正在读取账户信息…</p></div>
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
      <p class="panel__title">三色分级（官方口径）</p>
      <ul class="legend">
        <li class="legend__item">
          <span class="legend__dot" style="background:#0f8a5f"></span>
          <span>
            <span class="legend__name">绿色 · 自动执行</span><br />
            <span class="legend__desc">仅纯查询</span>
          </span>
        </li>
        <li class="legend__item">
          <span class="legend__dot" style="background:#a8710f"></span>
          <span>
            <span class="legend__name">黄色 · 用户确认</span><br />
            <span class="legend__desc">日累计 ≤ ¥1,000</span>
          </span>
        </li>
        <li class="legend__item">
          <span class="legend__dot" style="background:#b3362a"></span>
          <span>
            <span class="legend__name">红色 · 强验证</span><br />
            <span class="legend__desc">强验证 · 大额 / 不可逆 / 理财申购 · 解挂</span>
          </span>
        </li>
      </ul>
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

  <aside class="profile-panel" id="profile-panel" aria-label="个人中心" hidden>
    <div class="trace__head">
      <p class="panel__title">个人中心</p>
      <button class="btn btn--ghost" id="btn-close-profile" type="button">关闭</button>
    </div>
    <div id="profile-body"><p class="muted">正在读取账户信息…</p></div>
  </aside>
  <nav class="tabbar" id="app-tabbar" aria-label="底部导航">
    <button class="tabbar__item" type="button" data-nav="home">首页</button>
    <button class="tabbar__item" type="button" data-nav="chat">对话</button>
    <button class="tabbar__item" type="button" data-nav="me">我的</button>
  </nav>
</div>

<script src="/assets/app.js?v=24"></script>
</body>
</html>
"""

INDEX_HTML = _PAGE_HTML

# APP 端：同一份骨架 + `data-view="app"`，由 workspace.css 切换为手机框版式。
APP_HTML = _PAGE_HTML.replace(
    '<html lang="zh-CN" data-theme="light">',
    '<html lang="zh-CN" data-theme="light" data-view="app">',
    1,
)

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
    background: radial-gradient(1200px 600px at 20% -10%, #dfe8ff 0%, #f6f8fc 55%, #eef1f7 100%);
    color: #16233a; padding: 24px;
    font-family: "PingFang SC", "Microsoft YaHei", system-ui, -apple-system, sans-serif;
  }
  .login-card {
    width: 100%; max-width: 400px; background: #fff; border-radius: 18px; padding: 32px 28px;
    box-shadow: 0 18px 48px rgba(16, 38, 84, .14); border: 1px solid rgba(16, 38, 84, .06);
  }
  .brand { font-size: 20px; font-weight: 700; letter-spacing: .5px; margin: 0 0 6px; }
  .brand em { font-style: normal; color: #0b4fd8; }
  .sub { margin: 0 0 24px; font-size: 13px; color: #6b7a90; }
  label { display: block; font-size: 13px; color: #40506b; margin: 14px 0 6px; }
  input {
    width: 100%; height: 44px; padding: 0 12px; font-size: 15px; color: #16233a;
    border: 1px solid #cfd8e6; border-radius: 10px; background: #fbfcfe; outline: none;
  }
  input:focus { border-color: #0b4fd8; background: #fff; box-shadow: 0 0 0 3px rgba(11, 79, 216, .12); }
  button {
    width: 100%; height: 46px; margin-top: 22px; border: 0; border-radius: 10px; cursor: pointer;
    background: #0b4fd8; color: #fff; font-size: 15px; font-weight: 600;
  }
  button:disabled { opacity: .6; cursor: default; }
  .err {
    margin-top: 14px; padding: 10px 12px; border-radius: 10px; font-size: 13px;
    background: #fdecea; color: #b3362a; border: 1px solid #f5c6c1;
  }
  .demo {
    margin-top: 22px; padding: 12px 14px; border-radius: 10px; font-size: 12px; line-height: 1.7;
    background: #f4f7fd; color: #55637a; border: 1px dashed #cfd8e6;
  }
  .demo b { color: #16233a; font-weight: 600; }
  .foot { margin-top: 18px; font-size: 12px; color: #93a0b4; text-align: center; }
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

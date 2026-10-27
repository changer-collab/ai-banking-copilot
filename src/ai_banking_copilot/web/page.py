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
<link rel="stylesheet" href="/assets/app.css?v=6" />
<link rel="stylesheet" href="/assets/workspace.css?v=6" />
</head>
<body>
<div class="layout layout--workspace" id="app">
  <aside class="side" id="conversation-sidebar" aria-label="对话与场景" aria-hidden="false">
    <div class="side__brand topbar__brand">
      <span class="brand-mark">AI 银行<em>副驾</em></span>
      <span class="brand-sub">把想办的事说出来 · 每一步由您掌控</span>
    </div>
    <div class="side__pad">
      <button class="btn" id="btn-reset" type="button">＋ 新建对话</button>
    </div>
    <p class="side__label">本页记录 <span class="history-badge">临时</span></p>
    <div class="convs" id="conversation-list"><p class="muted convs__empty">开始办理后，记录会出现在这里。</p></div>
    <p class="side__label">常用办理</p>
    <section class="convs convs--scenes" id="quick-scenes" aria-label="快捷场景列表"></section>
    <div class="side__foot">
      <p class="side-note">模拟银行环境 · 不连接真实银行账户<br />绿色仅自动查询 · 黄色由您确认 · 红色需要强验证</p>
    </div>
  </aside>

  <main class="main" aria-label="银行办理对话">
    <header class="topbar">
      <div class="top__brand">
        <div class="top__t">AI 银行副驾</div>
        <div class="top__s">今天，有什么可以帮您？</div>
      </div>
      <div class="topbar__nav">
        <button class="btn btn--ghost" id="btn-scenes-mobile" type="button"
                aria-controls="conversation-sidebar" aria-expanded="false">对话与场景</button>
        <button class="btn btn--ghost" id="btn-trace-mobile" type="button"
                aria-controls="trace" aria-expanded="false">处理详情</button>
        <label class="switch">
          <input type="checkbox" id="tech-toggle" />
          技术详情
        </label>
        <label class="switch">
          <input type="checkbox" id="theme-toggle" />
          夜航主题
        </label>
      </div>
    </header>

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

    <section class="panel" id="subscription-panel">
      <p class="panel__title">订阅识别与取消状态</p>
      <p class="muted" id="subscription-status">正在读取订阅识别结果…</p>
      <p class="muted" id="subscription-boundary" hidden></p>
      <div id="subscription-list"><p class="muted">尚未读取订阅详情。</p></div>
      <div id="subscription-cancel-result" hidden></div>
    </section>

    <section class="panel" id="wealth-panel">
      <p class="panel__title">理财 · 测评与持仓</p>
      <div class="muted" id="wealth-status">正在读取测评状态与持仓…</div>
      <div id="wealth-assessment"><p class="muted">尚未读取风险测评问卷。</p></div>
      <p class="panel__title panel__title--sub">产品目录 <span class="tech">按测评等级判定</span></p>
      <div id="wealth-products"><p class="muted">尚未读取产品目录。</p></div>
      <p class="panel__title panel__title--sub">我的持仓</p>
      <div id="wealth-holdings"><p class="muted">尚未读取持仓。</p></div>
      <p class="muted" id="wealth-source"></p>
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
</div>

<script src="/assets/app.js?v=16"></script>
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

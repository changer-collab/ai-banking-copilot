"""Web 渠道内联单页（版式对齐设计仓 `demo.html`：快捷场景 + 对话流 + 处理详情三栏）。

- 样式与设计令牌**随包分发**：`web/assets/{tokens.css,app.css}`（取自设计仓，同一套令牌）；
- 交互由**真实 API** 驱动：`web/assets/app.js`（本项目自有实现，非设计仓的 mock `demo.js`）；
- 本页不引用任何外部域名资源，保持自包含（除同源 `/assets/*`）。

设计仓导航（设计规范 / 交互演示 / 移动端）是**设计仓内部页面**，本页不含该导航，避免死链。
"""

from __future__ import annotations

# Inline HTML intentionally keeps a few long semantic lines; layout checks cover structure.
# ruff: noqa: E501
INDEX_HTML = """<!DOCTYPE html>
<html lang="zh-CN" data-theme="light">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<meta name="theme-color" content="#0b4fd8" />
<title>AI 银行副驾 · 对话办理</title>
<link rel="manifest" href="/assets/manifest.json?v=1" />
<link rel="stylesheet" href="/assets/tokens.css?v=2" />
<link rel="stylesheet" href="/assets/app.css?v=5" />
<link rel="stylesheet" href="/assets/workspace.css?v=4" />
</head>
<body>
<div class="paper-bg" aria-hidden="true"></div>

<div class="shell" id="app">
  <header class="topbar">
    <div class="topbar__brand">
      <span class="brand-mark">AI 银行<em>副驾</em></span>
      <span class="brand-sub">把想办的事说出来 · 每一步由您掌控</span>
    </div>
    <nav class="topbar__nav">
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
    </nav>
  </header>

  <div class="layout layout--workspace">
    <aside class="side" id="conversation-sidebar" aria-label="对话与场景" aria-hidden="false">
      <div class="side__actions">
        <button class="btn btn--primary" id="btn-reset" type="button">＋ 新建对话</button>
      </div>
      <section class="conversation-history" aria-label="本页对话记录">
        <p class="panel__title">本页记录 <span class="history-badge">临时</span></p>
        <p class="muted history-hint">仅保留在当前页面，刷新后不保证恢复。</p>
        <div id="conversation-list"><p class="muted">开始办理后，记录会出现在这里。</p></div>
      </section>
      <details class="scene-library" open>
        <summary>常用办理</summary>
        <section id="quick-scenes" aria-label="快捷场景列表"></section>
      </details>
      <p class="side-note">模拟银行环境<br />不连接真实银行账户</p>
    </aside>

    <main class="main" aria-label="银行办理对话">
      <header class="conversation-head">
        <div><p class="eyebrow">AI BANKING COPILOT</p><h1>今天，有什么可以帮您？</h1></div>
        <span class="conversation-status">对话办理</span>
      </header>
      <div id="stage" class="stream" aria-live="polite" aria-relevant="additions text">
        <section id="conversation-welcome" class="conversation-welcome">
          <span class="welcome-mark" aria-hidden="true">AI</span>
          <h2>从一句话开始</h2>
          <p>查账单、办转账、管理卡片与订阅。<br />缺少的信息，我会向您确认；需要授权的操作，会先给您计划卡。</p>
          <div class="btn-row" id="presets"></div>
          <p class="muted">绿色仅自动查询 · 黄色由您确认 · 红色需要强验证</p>
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
            <button class="btn btn--danger" id="btn-reverse" type="button" disabled>回退已执行步骤</button>
            <button class="btn btn--ghost" id="btn-takeover" type="button" disabled>查看人工接管包</button>
          </div></details>
        </div>
      </div>
      <section class="composer" aria-label="发送办理需求">
        <p class="muted" id="offline-state" hidden>当前离线：只读静态壳可用，提交、确认和挑战已禁用。</p>
        <details class="object-context" id="object-context">
          <summary>本次办理对象 <span class="muted">选填 · 也可在对话中补充</span></summary>
          <div id="object-selectors">
            <div class="field"><label class="field__label" for="select-account">操作账户</label><select class="input" id="select-account" disabled></select></div>
            <div class="field"><label class="field__label" for="select-card">操作卡片</label><select class="input" id="select-card" disabled></select></div>
            <div class="field"><label class="field__label" for="select-subscription">已有订阅</label><select class="input" id="select-subscription" disabled></select></div>
            <div class="field"><label class="field__label" for="select-hold">有效冻结记录</label><select class="input" id="select-hold" disabled></select></div>
          </div>
        </details>
        <label class="sr-only" for="utterance">您的办理需求</label>
        <textarea class="textarea" id="utterance" rows="2" placeholder="例如：转给李娜 500 元，或查一下上月账单"></textarea>
        <div class="composer-footer">
          <button class="btn btn--ghost" id="btn-due" type="button">检查到期待办</button>
          <span class="muted composer-tip">Enter 发送 · Shift + Enter 换行</span>
          <button class="btn btn--primary" id="btn-submit" type="button">发送 <span aria-hidden="true">↑</span></button>
        </div>
        <p class="muted business-date" id="business-date">正在读取演示业务日期与可选对象…</p>
      </section>
    </main>

    <aside class="trace rail" id="trace" aria-label="处理详情" aria-hidden="true">
      <div class="trace__head">
        <p class="panel__title">处理详情</p><button class="btn btn--ghost" id="btn-close-trace" type="button">关闭</button>
      </div>
      <div id="trace-panels">
      <div class="panel" id="insights-panel">
        <p class="panel__title">主动洞察</p>
        <p class="muted" id="insights-status">正在读取站内洞察…</p>
        <div id="insights-list"><p class="muted">尚未读取洞察。</p></div>
      </div>

      <div class="panel">
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
      </div>

      <div class="panel">
        <p class="panel__title">审计链 <span class="tech">sha256 链</span></p>
        <ul class="chain" id="chain"><li class="chain__empty">尚未产生事件</li></ul>
      </div>

      <div class="panel">
        <p class="panel__title">执行时间线</p>
        <ul class="timeline" id="timeline"><li class="timeline__item">等待操作…</li></ul>
      </div>
      </div>
    </aside>
  </div>
</div>

<script src="/assets/app.js?v=13"></script>
</body>
</html>
"""

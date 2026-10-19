"""Web 渠道内联单页（版式对齐设计仓 `demo.html`：快捷场景 + 对话流 + 处理详情三栏）。

- 样式与设计令牌**随包分发**：`web/assets/{tokens.css,app.css}`（取自设计仓，同一套令牌）；
- 交互由**真实 API** 驱动：`web/assets/app.js`（本项目自有实现，非设计仓的 mock `demo.js`）；
- 本页不引用任何外部域名资源，保持自包含（除同源 `/assets/*`）。

设计仓导航（设计规范 / 交互演示 / 移动端）是**设计仓内部页面**，本页不含该导航，避免死链。
"""

from __future__ import annotations

INDEX_HTML = """<!DOCTYPE html>
<html lang="zh-CN" data-theme="light">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<meta name="theme-color" content="#0b4fd8" />
<title>AI 银行副驾 · 计划卡（真实连通）</title>
<link rel="manifest" href="/assets/manifest.json?v=1" />
<link rel="stylesheet" href="/assets/tokens.css?v=2" />
<link rel="stylesheet" href="/assets/app.css?v=5" />
<link rel="stylesheet" href="/assets/workspace.css?v=2" />
</head>
<body>
<div class="paper-bg" aria-hidden="true"></div>

<div class="shell" id="app">
  <header class="topbar">
    <div class="topbar__brand">
      <span class="brand-mark">AI 银行<em>副驾</em></span>
      <span class="brand-sub">真实连通 · 每次操作都可复核</span>
    </div>
    <nav class="topbar__nav">
      <button class="btn btn--ghost" id="btn-scenes-mobile" type="button"
              aria-controls="quick-scenes" aria-expanded="false">快捷场景</button>
      <button class="btn btn--ghost" id="btn-trace-mobile" type="button"
              aria-controls="trace-panels" aria-expanded="false">处理详情</button>
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
    <aside class="side" aria-label="快捷场景">
      <div class="side__brand">
        <span class="brand-mark">AI 银行<em>副驾</em></span>
        <span class="brand-sub">真实连通 · 计划卡由确定性模板渲染，执行经双层执行门</span>
      </div>
      <div class="side__actions btn-row">
        <button class="btn btn--primary" id="btn-reset" type="button">新建 / 重置</button>
      </div>
      <section class="panel" id="quick-scenes" aria-label="快捷场景列表">
        <p class="panel__title">快捷场景</p>
        <p class="muted">选择常用场景后，在中间输入区确认并提交。</p>
      </section>
    </aside>

    <main class="main">
      <section class="panel" style="margin-top: var(--s-5)">
        <p class="muted" id="business-date">正在读取演示业务日期与可选对象…</p>
        <p class="muted" id="offline-state" hidden>
          当前离线：只读静态壳可用，提交、确认和挑战已禁用。
        </p>
        <div class="btn-row" id="object-selectors">
          <div class="field">
            <label class="field__label" for="select-account">操作账户</label>
            <select class="input" id="select-account" disabled></select>
          </div>
          <div class="field">
            <label class="field__label" for="select-card">操作卡片</label>
            <select class="input" id="select-card" disabled></select>
          </div>
          <div class="field">
            <label class="field__label" for="select-subscription">已有订阅</label>
            <select class="input" id="select-subscription" disabled></select>
          </div>
          <div class="field">
            <label class="field__label" for="select-hold">有效冻结记录</label>
            <select class="input" id="select-hold" disabled></select>
          </div>
        </div>
        <div class="field">
          <label class="field__label" for="utterance">一句话指令（可自由编辑，或点下方样例）</label>
          <textarea class="textarea" id="utterance">转给李娜 500 元</textarea>
        </div>
        <div class="btn-row" id="presets"></div>
        <div class="btn-row">
          <button class="btn btn--primary" id="btn-submit">提交</button>
          <button class="btn" id="btn-due">检查到期待办</button>
          <button class="btn" id="btn-confirm" disabled>确认并执行</button>
          <button class="btn" id="btn-interrupt" disabled>中断</button>
          <button class="btn" id="btn-resume" disabled>恢复</button>
          <button class="btn btn--danger" id="btn-reverse" disabled>回退</button>
          <button class="btn btn--ghost" id="btn-takeover">人工接管包</button>
        </div>
      </section>

      <div id="stage" aria-live="polite"></div>
    </main>

    <aside class="trace rail" aria-label="处理详情">
      <div class="trace__head">
        <p class="panel__title">处理详情</p>
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
              <span class="legend__desc">服务端验证码 · 大额 / 不可逆 / 理财申购 · 解挂</span>
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

<script src="/assets/app.js?v=10"></script>
</body>
</html>
"""

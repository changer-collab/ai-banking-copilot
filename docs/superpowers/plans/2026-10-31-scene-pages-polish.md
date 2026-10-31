# 场景页打磨 实现计划

> **For agentic workers:** 每一步都按「改什么 → 验证 → 提交」执行。规格见
> `docs/superpowers/specs/2026-10-31-scene-pages-polish-design.md`，实现时以规格为准。

**Goal:** 让账单页显示当月真实数据并随对话操作变化，同时把 7 个场景页按实测反馈收口。

**约束（全任务通用）**

- 分支：`feat/scene-pages-polish`（从 `main` 切出）。
- **不跑 pytest**（用户要求，最后统一跑）；每步只跑 `ruff check .`、`mypy`、`node --check`。
- 工作区可能有无关改动：**只 stage 本任务文件**，提交用 `git commit -m "..." -- <路径>`，禁止 `git add .`。
- 不改 `contracts/`、`policy/audit.py`、`policy/gate.py`、`policy/pep.py`；不新增业务动作。

---

## Task 1：P0 数据口径（时钟 / 种子 / 账单默认当月）

**Files**
- Modify: `src/ai_banking_copilot/gateway/core_bank.py`（`DEFAULT_BOOKING_DATE`）
- Modify: `src/ai_banking_copilot/gateway/seed.py`（`_SEED_LAST`、`_SECONDARY_LAST`、订阅日期）
- Modify: `src/ai_banking_copilot/web/app.py:1105`（默认账期）
- Modify: `tests/gateway/test_seed.py`（计数断言改下限）

**Steps**
1. `DEFAULT_BOOKING_DATE = date(2026, 10, 31)`，docstring 同步。
2. `_SEED_LAST = (2026, 10)`、`_SECONDARY_LAST = (2026, 10)`；确认 10 月流水生成逻辑按 1–31 日铺、最后一条 ≤ 10-31。
3. 订阅下次扣款顺移：`sub_video → 2026-11-08`、`sub_music → 2026-11-15`、`sub_b_video → 2026-11-12`（其余不变）。
4. `merchant_store` 的"生日前 2 天"生日常量改为落在 2026-10-31，注释同步。
5. `/api/bills/analysis` 默认账期：无 `year/month` 参数时取 `deps.today` 的**当月**；返回体加 `period_basis`（`current_month` / `explicit`）与 `data_through`（当月最后一天与今天取小，ISO）。
6. `test_seed.py`：`== 315` → `>= 330`，其它与 9 月绑定的等号断言按实际值改成下限或新值。
7. 全仓 grep 硬编码 `2026-10-01`，逐条判断是否需要跟随（只改**断言/种子**，不改文档）。
8. 验证：`ruff check .`、`mypy`。提交：`test: 演示时钟推进到 2026-10-31，种子补齐 10 月` → `feat(gateway): 演示时钟推进到 2026-10-31，种子补齐 10 月与账单默认当月`

---

## Task 2：账单页结论卡精简 + 账期切换

**Files**
- Modify: `src/ai_banking_copilot/web/page.py`（`#view-scene` 账单块加切换器容器）
- Modify: `src/ai_banking_copilot/web/assets/app.js`（`refreshBills` / `billsMetricsHtml` / `billsFindingsHtml` / `billsTop3Html`）
- Modify: `src/ai_banking_copilot/web/assets/workspace.css`（`.bills-period`、`.bills-summary`）

**Steps**
1. 页头加 segmented 控件 `[本月][上月][选月份▾]`，默认「本月」；「本月」标签在有 `period_basis=current_month` 且 `summary.transaction_count` 与上次不同时显示「含今日新增 N 笔」。
2. 首屏结构按规格 §2.2：结论行（优先 `narrative.summary`）→ 3 指标 → 最多 2 条重点核对 → 一个「展开完整分析」`details`。
3. 删除 `billsTop3Html` 与 `billsFindingsHtml` 的重复渲染（同一份 findings 只出现一次）。
4. 验证：`node --check`、`ruff`、`mypy`。提交：`feat(web): 账单页账期切换与结论卡精简`

---

## Task 3：卡片页

**Files**: `app.js`（`cardsHtml` / `cardsVisualHtml` / `cardFaceHtml`）、`workspace.css`（`.card-wall` / `.card-face`）

**Steps**
1. 删除「共 N 张卡 · 状态与额度来自服务端」（`app.js` 约 1719 行），保留 `as_of`。
2. `.card-face` 加 `aspect-ratio: 1.586`、收窄 padding；≥960px 三列。
3. 动作分两组：「常用」（锁定/解锁、调额）与「风险操作」（挂失、申请虚拟卡）；挂失按钮红色，点击先弹 `confirmAppDialog` 不可逆确认。
4. 验证 + 提交：`feat(web): 卡片页真实卡面比例与动作分组`

---

## Task 4：订阅页

**Files**: `app.js`（`subscriptionCardHtml` / `renderSubscriptionDetails`）、`workspace.css`（`.sub-card*`）

**Steps**
1. 卡片压到 3 行：商户+状态 / 金额·周期·下次扣款 / 「核对依据 · 取消口径 ›」。
2. `__meta` 去掉 ellipsis 截断。
3. 原 8 行折叠区改为点击卡片打开详情弹层（复用 `openAppDialog` 或新增 `#sub-detail` 弹层），弹层保留全部依据与「对应交易」。
4. 验证 + 提交：`feat(web): 订阅卡片精简为三行，依据移入详情弹层`

---

## Task 5：我的页 + 渠道并入

**Files**: `page.py`（左栏/首页工具区删渠道入口、`#view-me-body` 加渠道卡容器）、`app.js`（`renderProfile` 飞书卡、`renderChannelHealth` 复用）、`workspace.css`（`.profile-card*`）

**Steps**
1. 左栏 `data-nav="health"` 与首页工具区入口移除；`#/health` 路由保留。
2. 「我的」新增「渠道状态」卡片：Web 正常 / 飞书 `configured=false`，右侧「刷新」。
3. 飞书卡排版：标题行 + 右对齐按钮；标题下一行小字说明（`--fs-small`、`--ink-3`）；grid `gap` 收紧。
4. 验证 + 提交：`feat(web): 渠道状态并入我的，飞书卡排版收紧`

---

## Task 6：转账收款人选择器

**Files**: `web/app.py`（新增 `GET /api/payees`）、`page.py`（转账块表单）、`app.js`（选择器逻辑 + 快捷金额 + 备注）、`workspace.css`

**Steps**
1. `GET /api/payees` 返回 `[{payee_id, name, account_tail, phone_tail4}]`，只读名册；不含完整手机号。
2. 前端把 `<input id="quick-payee">` 换成可搜索组合框：输入过滤名册 + 最近转账去重收款人；无匹配显示「按此姓名继续」。
3. 金额加 100/500/1000 快捷；加备注输入，提交拼 `转给X N 元，备注：…`。
4. 成功后清空金额与备注、保留收款人。
5. 验证 + 提交：`feat(web): 转账页收款人可选名册并支持备注`

---

## Task 7：理财大类 + 净值曲线 + 推荐指标

**Files**: `gateway/store.py`（schema v7 + `wealth_nav` 表）、`gateway/seed.py`（净值序列）、`gateway/facts.py`（`wealth_nav_of`）、`gateway/models.py`（`WealthNav`）、`scenarios/wealth.py`（`category` 字段）、`web/app.py`（`/api/wealth/overview` 增字段）、`app.js` + `workspace.css`（环形图按大类、SVG 折线）

**Steps**
1. 建表 `wealth_nav(product_id, on_date, nav_bp)`；schema v7；`_OPTIONAL_COLUMNS` 不涉及。
2. 种子按确定性公式生成近 90 天：`nav_bp = 10000 + yield_bp * days / 36500 + 振幅 * sin(day)`，零随机。
3. `FactGateway.wealth_nav_of(product_id, days=90)`。
4. 产品 `category`：`wp_cash 货币类`、`wp_bond 债券类`、`wp_mix 混合类`、`wp_equity 权益类`。
5. `/api/wealth/overview`：`products[]`/`holdings[]` 带 `category`；`holdings[]` 带 `nav_series`、`nav_start`、`nav_now`、`income_cents`、`simulated: true`。
6. 前端：环形图按大类聚合；产品行加内联 SVG 折线 + 「模拟净值」标签；推荐理由引用年化/期限/起购/费率。
7. 验证 + 提交：`feat(wealth): 持仓按大类聚合，净值曲线（模拟数据）与推荐指标`

---

## 收尾（用户明确要求最后统一跑）

```bash
ruff check .
mypy
python -m pytest tests/ -q
# 另开终端：AIB_DISABLE_LLM=1 python -m ai_banking_copilot
python scripts/check_served_page.py
python scripts/e2e_check.py
python scripts/check_design_sync.py
```

# 账单分析可信度重构 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把账单分析页从「低置信度告警堆砌」改成「收支结构 + 分类修正 + 商户排行 + 订阅/预算 + 高置信闭环线索」，并让 AI 问答使用同一口径。

**Architecture:** 在 `scenarios/bill_findings.py` 内建立唯一消费账本（`_collect_window`），所有汇总、趋势、分类、商户、预算都从它派生；展示层新增 `Lead` 聚合与置信度分层；核对状态与分类修正写入独立元数据 SQLite，不回写银行账本；前端只渲染服务端数字。

**Tech Stack:** Python 3.14 / FastAPI / SQLite / 原生 JS + 手写 SVG / CSS。

**User constraint:** 永远不要运行 `pytest`。验证用 `ruff check`、`mypy`、`python -m compileall` 与页面实测。

---

### Task 1: 分类与 MCC 演示映射

**Files:**
- Create: `src/ai_banking_copilot/scenarios/bill_categories.py`
- Modify: `src/ai_banking_copilot/scenarios/bill_findings.py`（改为复用分类模块）

- [ ] 新建 `bill_categories.py`，定义 `NON_CONSUMPTION_CATEGORIES`、`CategoryInfo`、`category_info()`、`is_consumption()`、`category_options()`。
- [ ] 商户关键词映射覆盖种子数据：零食星球、外卖平台、周末聚餐、云购物商城、超市日用、地铁出行、打车出行、房租支出、水电燃气、宽带与话费、视频会员、电影与演出、未知商户。
- [ ] `category_info(merchant, category, override=None)`：用户修正优先，其次商户关键词，最后按一级分类兜底。
- [ ] 在 `bill_findings.py` 中删除重复的商户关键词表，改为调用 `bill_categories`。

### Task 2: 唯一消费账本与收支/净现金流

**Files:**
- Modify: `src/ai_banking_copilot/scenarios/bill_findings.py`

- [ ] `_collect_window(gateway, user_id, start, end, overrides)` 一次扫描返回：
  - `charges`：消费支出（`direction=out` 且 `is_consumption`）；
  - `income_cents`：`direction=in` 合计；
  - `transfer_out_cents` / `transfer_count`：非消费转出；
  - 每笔 `_Charge` 带 `category_l2`、`mcc`、`mcc_label`。
- [ ] `_collect_charges()` 改为 `_collect_window(...).charges`，保留函数名给规则层复用。
- [ ] `analyze_bill(..., overrides=None)` 使用同一窗口；`BillFindingsReport` 增加 `income_cents`、`transfer_out_cents`、`transfer_count`。
- [ ] `report_payload()` 的 `summary` 增加 `income_cents`、`transfer_out_cents`、`transfer_count`、`consumption_count`、`net_cashflow_cents`。
- [ ] `transactions` 增加 `category_l2`、`mcc`、`mcc_label`。

### Task 3: 趋势、分类占比、环比归因、Top 商户

**Files:**
- Modify: `src/ai_banking_copilot/scenarios/bill_findings.py`

- [ ] `expense_trend(..., overrides=None)` 改为逐月 `_collect_window`，`BillTrendPoint` 增加 `income_cents`、`categories`、`mom`。
- [ ] 新增 `BillMom` / `MomContribution`，由服务端计算 `change_cents`、`change_pct`、`contributions`。
- [ ] `category_shares(..., overrides=None)` 按二级分类聚合，返回 `category_l1`、`mcc`。
- [ ] `bill_profile(..., overrides=None)` 保持现有字段，`top_category` 变为二级分类。
- [ ] 新增 `top_merchants(..., limit=8)`，返回金额、笔数、占比、二级分类、MCC。
- [ ] `report_payload()` 增加 `top_merchants` 与 `category_options`（由路由填充亦可）。

### Task 4: 线索聚合、置信度分层、自动续费与异地组合判据

**Files:**
- Modify: `src/ai_banking_copilot/scenarios/bill_findings.py`

- [ ] 新增 `LEAD_KEY_CONFIDENCE=0.70`、`LEAD_MORE_CONFIDENCE=0.50`、`lead_tier()`。
- [ ] 新增 `ReviewLead` 与 `build_leads(findings, states=None)`：按主交易去重、合并多标签、`impact_cents` 不累加。
- [ ] `finding_basis(finding)` 返回 `rule/threshold/detail/assumption/confidence`。
- [ ] `FINDING_KINDS` 用 `auto-renewal` 替换 `hidden-fee`。
- [ ] `_hidden_fee_findings` 改为 `_auto_renewal_findings`：同商户 ≥3 次、固定周期、金额波动 ≤5%、未登记；`saving_basis="none"`。
- [ ] `_remote_city_findings`：异地 + 深夜/大额时置信度 0.75；单笔异地保持 0.60（展示层会隐藏）。
- [ ] `report_payload(..., lead_states=None)` 增加 `leads`、`auto_renewals`、`summary.lead_count`、`summary.key_lead_count`、`summary.review_amount_cents`。
- [ ] `_render_summary()` 删除「节省」，改为消费/收入/净现金流/高置信线索/转账分列。

### Task 5: 核对状态与分类修正持久化

**Files:**
- Create: `src/ai_banking_copilot/web/bill_analysis_store.py`
- Modify: `src/ai_banking_copilot/compose.py`
- Modify: `src/ai_banking_copilot/web/app.py`

- [ ] `bill_analysis_store.py` 提供 `BillAnalysisStore` 协议、`MemoryBillAnalysisStore`、`SQLiteBillAnalysisStore`。
- [ ] 表：`bill_lead_states(user_id, period, lead_id, state, note, updated_at)`、`bill_category_overrides(user_id, merchant_key, category, updated_at)`。
- [ ] `compose.py` 用 `{db_stem}-bill-analysis.db` 装配，并注册 `close`。
- [ ] `AppDeps` 增加 `bill_analysis: BillAnalysisStore | None = None`；`create_app` 内回退到内存实现。

### Task 6: 预算（总额 + 分类 + 进度 + 月初提醒）

**Files:**
- Modify: `src/ai_banking_copilot/web/insights.py`
- Modify: `src/ai_banking_copilot/web/app.py`

- [ ] `InsightMetadataStore` 增加 `get_category_budgets` / `set_category_budgets`；SQLite 表增加 `category_budgets_json`（`ALTER TABLE` 幂等）。
- [ ] `InsightService` 增加 `budgets()` / `set_budgets()`；`set_budget()` 保持兼容。
- [ ] `_BudgetRequest` 增加 `categories: dict[str, int] | None`。
- [ ] `/api/insights/budget` 同时保存总额与分类预算。
- [ ] `/api/bills/analysis` 返回 `budget`：`total/spent/remaining/daily_available/days_left/categories/over`。
- [ ] 未设置预算时返回 `needs_setup=true`，页面显示「本月还未设置预算」。

### Task 7: 分析接口与对话框回写

**Files:**
- Modify: `src/ai_banking_copilot/web/app.py`

- [ ] `/api/bills/analysis` 读取分类修正与线索状态，传给 `analyze_bill` / `report_payload`。
- [ ] 新增 `POST /api/bills/leads/{lead_id}`：`{period, state, note?}`，校验状态并落库。
- [ ] 新增 `POST /api/bills/category-override`：`{merchant_key, category}`，校验二级分类并落库。
- [ ] 新增 `POST /api/bills/lead-resolution`：`{lead_id, period, utterance}`，按确定性短语映射状态并落库，返回 `message`。
- [ ] 所有写接口只写元数据，不写银行账本、不产生执行授权。

### Task 8: AI 问答同步

**Files:**
- Modify: `src/ai_banking_copilot/scenarios/bills.py`
- Modify: `src/ai_banking_copilot/scenarios/bill_advice.py`
- Modify: `src/ai_banking_copilot/web/assets/app.js`

- [ ] `bills._monthly_bill` 的 detail 改为「消费 / 收入（不含转账 X 元）」。
- [ ] `bill_advice` 的分类占比分母改为消费口径，转账不参与。
- [ ] `billAnalysisHtml` 删除「可核实节省 / 潜在节省」。
- [ ] 指标改为「本期消费 / 本期收入 / 待核对」。
- [ ] 线索只列 `key`，显示置信度与依据；<70% 不抢首屏。
- [ ] 增加「另有 N 笔转账不计入消费」。
- [ ] 增加 `bill_lead_resolution` 回执卡。

### Task 9: 账单页结构与交互

**Files:**
- Modify: `src/ai_banking_copilot/web/page.py`
- Modify: `src/ai_banking_copilot/web/assets/app.js`
- Modify: `src/ai_banking_copilot/web/assets/workspace.css`

- [ ] 页面顶部增加演示黄条，正文只留一句免责。
- [ ] 账期改为 `<select>`，首项「请选择年月」；本月不再显示截止日。
- [ ] 四个指标：本期消费 / 本期收入 / 净现金流 / 待核对。
- [ ] 预算区：总额 + 分类输入、进度条、已花/剩余/日均、超支高亮。
- [ ] 图表：收支双线、端点标签不裁切、趋势点可点、环比归因、二级分类环形图。
- [ ] Top 商户：按金额/笔数切换、改分类弹窗。
- [ ] 线索区：重点核对 / 更多线索 / 已核对；每条有依据、证据、四个动作、问 AI。
- [ ] 订阅/自动续费区与「管理自动续费」入口。
- [ ] 明细表 + CSV 导出。
- [ ] 场景页头按钮改为「问 AI 账单问题」。
- [ ] `lineChartHtml` 修端点 `text-anchor` 与动态留白。

### Task 10: 页面闭环与验证

**Files:**
- Modify: `src/ai_banking_copilot/web/assets/app.js`

- [ ] 「问 AI」写入 `state.pendingBillLead`，导航到对话并预填完整上下文。
- [ ] 用户随后说「这笔是我转给朋友的」等，前端拦截并调用 `/api/bills/lead-resolution`，不进入转账计划。
- [ ] 含金额的消息不拦截（避免误吞真实转账）。
- [ ] 页面动作后刷新分析，已核对线索不再出现在重点核对。
- [ ] 运行 `ruff check .`、`mypy`（若可用）、`python -m compileall src`；不运行 pytest。
- [ ] `AIB_DISABLE_LLM=1` 启动，实测账单页 8 条验收项。

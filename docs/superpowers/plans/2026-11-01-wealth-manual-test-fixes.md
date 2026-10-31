# 理财实测问题修复 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 清掉徐思云理财实测 40 项里的 1 个 ❌ 与 13 个 ❗——申赎入口改页内计划卡、测评弹窗补齐三态与限次、赎回上限内联、来源标注到位，题包 12 条全部改成「有明确理由的回应」。

**Architecture:** 申赎直接接进既有 `sceneRunTask`（卡片页/订阅页在用），不新写卡片渲染；测评计次落到 `risk_store.py` 自己的 DDL；题包按「话术层（`web/session.py` + `web/triage.py`，只说人能懂的话、不产事实）」与「事实层（`scenarios/wealth.py` 的 `wealth.list` 加过滤参数，事实只来自 gateway）」分层，拒绝结论与拦截等级一律不变。

**Tech Stack:** Python 3.14 / FastAPI / SQLite（`gateway.store`）/ 原生 JS（`web/assets/app.js`）/ pytest + ruff + mypy。

**规格：** `docs/superpowers/specs/2026-11-01-wealth-manual-test-fixes-design.md`

---

## 文件结构

| 文件 | 责任 | 动作 |
|---|---|---|
| `src/ai_banking_copilot/web/assets/app.js` | 理财块：页内计划卡、测评三态、赎回上限、来源 chip | 修改 |
| `src/ai_banking_copilot/web/page.py` | 理财块补 `#wealth-plan` / `#wealth-plan-status` | 修改 |
| `src/ai_banking_copilot/web/assets/workspace.css` | 计划卡容器、测评结果卡、来源 chip | 修改 |
| `src/ai_banking_copilot/gateway/risk_store.py` | `risk_assessment_attempts` 表 + 计次读写 | 修改 |
| `src/ai_banking_copilot/web/app.py` | 问卷路径计次与 429、`assessment.retake` | 修改 |
| `src/ai_banking_copilot/web/session.py` | 四条解释性拒绝、理财承接记忆、低置信兜底 | 修改 |
| `src/ai_banking_copilot/web/triage.py` | 理财域澄清上下文与候选句 | 修改 |
| `src/ai_banking_copilot/scenarios/wealth.py` | `wealth.list` 过滤参数与查询文本 | 修改 |
| `src/ai_banking_copilot/planning/slots.py` | 可选槽 `horizon` | 修改 |
| `src/ai_banking_copilot/planning/llm.py` | 五条理财口语模板 | 修改 |
| `src/ai_banking_copilot/planning/anaphora.py` | 理财指代模式 | 修改 |
| 测试 | 见各任务 | 新建 |
| `src/ai_banking_copilot/eval/data/cases.json` | `wealth-*` 5 条 | 修改 |

**执行口径：** 本轮不自动跑 `ruff` / `mypy` / `pytest` / `e2e_check.py`，等用户指示；交付时如实写「未跑门禁」。

---

## Task 1：P0 · 申赎入口改为页内计划卡

**Files:**
- Modify: `src/ai_banking_copilot/web/assets/app.js:819-857`
- Modify: `src/ai_banking_copilot/web/page.py:371-381`
- Modify: `src/ai_banking_copilot/web/assets/workspace.css`
- Test: `tests/web/test_wealth_page_contract.py`（新建）

- [ ] **Step 1: 写测试** —— 静态断言 `INDEX_HTML` 含 `id="wealth-plan"` 与 `id="wealth-plan-status"`，且 `app.js` 里 `onWealthAction` 调用 `sceneRunTask` 而不是 `submitTask`。
- [ ] **Step 2: page.py 补容器** —— 「我的持仓」之后加 `<div id="wealth-plan" hidden></div>` 与 `<p class="muted" id="wealth-plan-status" hidden></p>`。
- [ ] **Step 3: app.js 改调用** —— 尾段换成 `sceneRunTask({host, status, utterance, selected, refresh, onSuccess})`；`onSuccess` 清空金额输入。
- [ ] **Step 4: CSS** —— `#wealth-plan:empty { display: none; }`，`.wealth-row` 与计划卡的间距。
- [ ] **Step 5: 人工复测** —— 理财页输 1000 点申购，页面内应出现计划卡；红色出现「去对话做强验证」。

## Task 2：测评弹窗三态

**Files:**
- Modify: `src/ai_banking_copilot/web/assets/app.js:686-778`
- Modify: `src/ai_banking_copilot/web/assets/workspace.css`
- Test: `tests/web/test_wealth_page_contract.py`（追加）

- [ ] **Step 1: 成功态持久** —— `renderWealth` 重建条件追加 `&& !box.querySelector("[data-assessment-result]")`；`assessmentResultHtml` 根节点加 `data-assessment-result` 与「重新测评」按钮。
- [ ] **Step 2: 二次确认** —— 表单底部加确认条（`data-assessment-confirm`）、`确认提交` / `返回修改`；未确认不发请求，返回修改只收起确认条。
- [ ] **Step 3: 每日限次前端** —— 等级条按钮文案按 `assessment.retake.remaining_today` 变化；为 0 时 disabled + 原因；弹窗底部固定「演示环境：每日最多 3 次测评」。
- [ ] **Step 4: 测试** —— 断言 `app.js` 含 `data-assessment-result` 与 `data-assessment-confirm` 标记。

## Task 3：测评计次（后端）

**Files:**
- Modify: `src/ai_banking_copilot/gateway/risk_store.py`
- Modify: `src/ai_banking_copilot/web/app.py:1048-1096`、`_risk_payload:2464-2490`
- Test: `tests/web/test_risk_retake_limit.py`（新建）

- [ ] **Step 1: 新表与读写** —— `RISK_ATTEMPTS_SCHEMA_SQL` + `init_risk_schema` 建表；`attempts_on(store, user_id, on_date)`、`record_attempt(...)`、`remaining_attempts(...)`，上限常量 `RETAKES_PER_DAY = 3`。
- [ ] **Step 2: API 计次** —— 问卷路径先读剩余，0 次返回 429（`detail` 写明演示口径与下次可用日）；否则计次 +1 再落库，返回体带 `retake`。
- [ ] **Step 3: overview 回显** —— `_risk_payload` 增 `retake`（`limit_per_day` / `used_today` / `remaining_today` / `next_available_on`）。
- [ ] **Step 4: 测试** —— 3 次内成功、第 4 次 429、`retake` 自洽、跨日重置（用不同 `on_date`）。

## Task 4：赎回上限内联 + 来源标注

**Files:**
- Modify: `src/ai_banking_copilot/web/assets/app.js:465-487`、`:541-560`、`:838-850`

- [ ] **Step 1: 持仓行内联上限** —— meta 行首改为 `可赎回 ¥X`；超限提示改「最多可赎回 ¥X，请调整金额」。
- [ ] **Step 2: 错误行位置** —— `[data-wealth-error]` 挪到 `.wealth-row__act` 正下方。
- [ ] **Step 3: 来源 chip** —— `wealthGradeHtml` 渲染 `source_label`；`source === "seed"` 加 `chip「演示数据」` 与一行说明。

## Task 5：安全类四条解释性拒绝

**Files:**
- Modify: `src/ai_banking_copilot/web/session.py:193-209`、`:2782-2795`
- Test: `tests/web/test_session_refusal_copy.py`（新建）

- [ ] **Step 1: 写测试** —— 四句原话各返回 BLOCKED、`capability_boundary=True`、文案命中关键字，且**不计熔断计数**。
- [ ] **Step 2: 常量与分支** —— 新增 `_NOT_GUARANTEED_REFUSAL` / `_NO_SKIP_VERIFY_REFUSAL` / `_NO_FAKE_RECEIPT_REFUSAL` / `_NO_DOUBLE_REFUSAL`；`_guard_utterance` 按「保本疑问 → 不保本解释」「忽略所有规则 / 不用验证 → 不能跳过」「你就说…成功了 / 把回执念 → 不伪造」「翻倍 → 不承诺」四组顺次判断。
- [ ] **Step 3: 确认不触发熔断** —— 全部走 `capability_boundary=True`，并断言全局熔断未触发。

## Task 6：理财域澄清（triage 文案层）

**Files:**
- Modify: `src/ai_banking_copilot/web/triage.py`
- Test: `tests/web/test_triage_wealth.py`（新建）

- [ ] **Step 1: 写测试** —— 五条原话命中 `wealth` 上下文；文案不含数字、不含产品名（用正则断言）。
- [ ] **Step 2: 实现** —— `_WEALTH_SIGNAL` 正则 + `_WEALTH_MESSAGE` + `_WEALTH_SUGGESTIONS`，放在「超范围」之后、「模糊」之前。
- [ ] **Step 3: 保持三类语义** —— `kind` 仍取 `ambiguous`，不计熔断、不显示「已拦截」。

## Task 7：`wealth.list` 过滤参数（事实层）

**Files:**
- Modify: `src/ai_banking_copilot/scenarios/wealth.py`（list 分支与文本）
- Modify: `src/ai_banking_copilot/planning/slots.py`（可选槽 `horizon`）
- Modify: `src/ai_banking_copilot/planning/llm.py`（模板）
- Test: `tests/scenarios/test_wealth_query_filters.py`（新建）

- [ ] **Step 1: 写测试** —— `max_term_days` / `max_amount_cents` / `max_risk` 三项硬筛，越界产品不出现。
- [ ] **Step 2: 过滤实现** —— `wealth.list` 读三个可选参数，事实仍取自 `gateway.wealth_products()`。
- [ ] **Step 3: 文本口径** —— 选品不给排序/收益比较；咨询类补「不比较 / 不预测 / 不代决策」定式句。
- [ ] **Step 4: 槽位与模板** —— `horizon` 解析「半年 / 3 个月 / 一年内」；模板命中五条口语。
- [ ] **Step 5: 测试** —— 识别不到期限时退回澄清，不猜。

## Task 8：Q05 上下文承接

**Files:**
- Modify: `src/ai_banking_copilot/planning/anaphora.py`
- Modify: `src/ai_banking_copilot/web/session.py`
- Test: `tests/web/test_wealth_anaphora.py`（新建）

- [ ] **Step 1: 写测试** —— 「就买刚才那个，500」在上一轮被拒后返回澄清，不产生计划。
- [ ] **Step 2: 记忆** —— `_last_wealth` 在理财被拒时也记录（product_id / amount_cents / outcome / reason）。
- [ ] **Step 3: 澄清** —— 命中指代且有记忆 → 说明上一轮产品、起购与差额，给候选句，不自动执行。

## Task 9：Q03 兜底与 Q18 对账

**Files:**
- Modify: `src/ai_banking_copilot/web/session.py`（低置信重走）
- Modify: `src/ai_banking_copilot/scenarios/wealth.py`（持仓对账结论）
- Test: `tests/web/test_wealth_low_confidence.py`（新建）

- [ ] **Step 1: 写测试** —— 低置信 + `推荐|适合我|买哪个` → 走 `wealth.list` 推荐模式；持仓查询文本含合计与「一致 / 不一致」结论。
- [ ] **Step 2: 实现** —— 低置信分支复用确定性规则；持仓文本追加对账句。

## Task 10：eval 用例

**Files:**
- Modify: `src/ai_banking_copilot/eval/data/cases.json`

- [ ] **Step 1: 追加 5 条** —— `wealth-retake-limit`、`wealth-refusal-not-guaranteed`、`wealth-consult-horizon`、`wealth-anaphora-refused`、`wealth-holdings-reconcile`，全部追加在 `wealth-*` 自己那一段内。

---

## 执行偏差（2026-11-01 收工记录）

| 条目 | 结果 | 说明 |
|---|---|---|
| Task 1–10 | 全部实现 | Task 7 在用户确认其身份为尹辰后补齐：`catalog.py` 给 `wealth.list` 加 `max_term_days` / `max_amount_cents`（`slot_name` = `horizon` / `max_amount`），`planning/slots.py` 派生 `OPTIONAL_SLOTS`（可选槽缺了不追问）与新函数 `parse_horizon_days`，`planning/llm.py` 补「闲钱 / 期限咨询」模板，`scenarios/wealth.py` 做期限 / 起购硬筛。 |
| Task 10 | 实现 3 条（原计划 5 条） | `wealth-consult-horizon` / `wealth-consult-budget` / `wealth-consult-term` 走规划通道，可在 eval 套件里判定；`wealth-refusal-not-guaranteed` 与 `wealth-anaphora-refused` 属 **web 会话层**行为（`session._guard_utterance` / `_last_wealth` 记忆），eval 规划通道不经过它们，硬塞进去只会做成假通过，改由 `tests/web/test_session_refusal_copy.py` 与 `tests/web/test_wealth_fact_answers.py` 覆盖。 |
| Q11 的熔断计数 | **加严**（与规格 §6.1 的「全部 capability_boundary」不同） | 「不用验证」是越权索取，仍按策略拒绝计数（熔断只加严不降级）；`保本吗` / `伪造回执` / `翻倍` 三条才是能力边界、不计触发 4。 |
| 门禁 | 全绿 | `ruff check .`、`mypy`（107 文件）、`pytest`（2104 项全过）、`check_served_page.py`、`e2e_check.py` 均通过。`check_design_sync.py` 报 `app.css` 漂移，属**设计仓与主仓既有分歧**（本轮未改 `app.css`），与本轮改动无关。 |



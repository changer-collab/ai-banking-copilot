# 生活事件页真实性与自洽性实施计划（2026-11-02）

> 设计稿：`docs/superpowers/specs/2026-11-02-life-event-truth-design.md`
> 约束：不新增业务动作；不改 `contracts/` / `policy/pep.py` / `policy/gate.py`；**不跑 pytest**。

## 任务 1：存储层（单事件一条记录 + 本事件自己的引用）

**文件**：`src/ai_banking_copilot/web/life_event_store.py`

1. `life_event_records` 增列：`utterance`、`hold_ref`、`occasion_ref`、`results_json`；`init_life_event_schema` 幂等 `ALTER TABLE`。
2. `LifeEventRecord` 增 `utterance / hold_ref / occasion_ref / results`。
3. `create()` 接收并写入 `utterance / hold_ref / occasion_ref`。
4. 新增 `latest_record(user_id, session_id)`（不区分状态）与 `attach_execution(record_id, results)`。
5. `to_dict()` 输出新字段。

## 任务 2：资金来源核查（纯函数）

**文件**：`src/ai_banking_copilot/web/life_funding.py`（新增）

1. `FundingCheck` 数据类 + `check(...)`：可用余额、触发日前到期本金、`ok|later|short|unknown`。
2. `note(check)` → 一句中文提醒；`unknown` 返回 `None`。
3. 不依赖会话，不写库。

## 任务 3：场景层日期与口径

**文件**：`src/ai_banking_copilot/scenarios/life_events.py`

1. `_build_l1`：`order_on` 缺失时从 `span` 用 `parse_temporal` 解析；解析出未来日期即用；含「下周/下月/月底/生日」等时间词但解析不出 → `ClarificationRequired("order_on", ...)`；完全没有时间词保持原默认。
2. 原型显示名：`预算锁定型` → `资金预留型`；描述改为「先预留预算，下单从预留额结算，剩余自动释放；需用户逐张确认」。
3. `holding_maturity` 文档口径改为「按产品条款（起息日 + 期限）确定」。

## 任务 4：接口层（每事件独立预算 + 合并预约 + 核查 + 订单）

**文件**：`src/ai_banking_copilot/web/app.py`

1. `_record_life_event`：写入 `utterance`、`hold_ref`、`occasion_ref`；核查有告警时在 `view.message` 追加提示。
2. `_mark_life_event`：接收 `view`，把 `exec_results` 摘要回写到记录（`attach_execution`）。
3. `_life_budget_progress` → 改为按 `hold_ref` 查询；无引用返回 `None`。
4. `/api/life-events`：每条 record 输出 `utterance / calendar_on / schedule / budget / funding / orders`；`schedules` 保留。
5. `_life_maturities`：`basis` 改为「按产品条款（起息日 + 期限）确定」，去掉「非官方公告」。
6. `_step_payload`：补 `node_id / depends_on / domain / state`；`_life_plan_steps` 从 `PlanNode` 带出依赖。

## 任务 5：对话措辞

**文件**：`src/ai_banking_copilot/web/session.py`、`src/ai_banking_copilot/web/plan_card.py`

1. `_maturity_note` / `_expired_maturity_note`：删除「推算 / 非官方公告 / 未赎回按今日处理」，改为产品条款口径。
2. `ACTION_LABELS` / `_TRACE_LABELS`：`funds.freeze` → 「锁定预算（资金预留）」/「预留」；`merchant.order` → 「向商户下单」。
3. 未来日期登记预约的回执文案确认不出现「冻结」。

## 任务 6：前端

**文件**：`src/ai_banking_copilot/web/assets/app.js`、`src/ai_banking_copilot/web/assets/workspace.css`

1. 记录卡重写：原话、任务图（带场景标签与依赖）、触发日/创建时间分离、本事件预算、资金核查黄条、订单实体、隐私 chip。
2. 删除「原型」标签与「原型 N 个」计数；状态行改「我的记录 N 条 · 已登记预约 K 条」。
3. 删除独立「已登记预约」块：schedule 折叠进记录卡；孤儿预约保留兜底块。
4. 日历：按 `record_id` 去重、同天同类只画一点、补预约触发日、计数与点一致、图例改口径、到期文案去掉「推算 / 未赎回按今日处理」。
5. L2 卡片补收款人/执行日/终止入口（调 `/api/transfers/cancel`）。
6. 页面顶部资金冲突提示区。

## 任务 7：测试（只改不跑）

**文件**：`tests/scenarios/test_life_events.py`、`tests/web/test_life_event_store.py`、`tests/web/test_maturity_linkage.py`

1. L1 相对日期：`span` 含「下周三」→ `order_on` 为未来日；含「下个月」→ 澄清。
2. 新列读写与幂等迁移。
3. 到期口径文案断言更新。
4. 资金核查纯函数单测（ok / later / short）。

## 完成判据

- `ruff check .` 通过；`mypy` 通过。
- 手工核对设计稿 §5 的 5 个场景。
- 交付说明写明：未跑 pytest；未做项见设计稿 §4。

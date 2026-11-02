# 转账页真实数据与安全建模实施计划（2026-11-02）

> 设计稿：`docs/superpowers/specs/2026-11-02-transfer-truth-and-safety-design.md`
> 约束：不新增业务动作；不跑 pytest（用户明确要求）；不改变审计 / 门禁硬约束。

## 任务 1：数据模型与迁移

**文件**
- `src/ai_banking_copilot/gateway/store.py`
- `src/ai_banking_copilot/gateway/models.py`
- `src/ai_banking_copilot/gateway/facts.py`
- `src/ai_banking_copilot/gateway/transaction_facts.py`
- `src/ai_banking_copilot/gateway/seed.py`
- `src/ai_banking_copilot/gateway/transfer_store.py`

**改动**
1. `SCHEMA_VERSION` 升到 9；`transactions` 增加可空列：`payee_id`、`transfer_method`、`transfer_fee_cents`、`transfer_purpose`、`transfer_remark`、`transfer_status`。
2. `payees` 增加可空列 `bank_name`。
3. `Payee` 增加 `bank_name: str | None = None`，读取层映射。
4. 种子收款人补齐演示开户行；新库/旧库都通过幂等 `UPDATE ... WHERE ... IS NULL` 回填。
5. `scheduled_transfers` 增加 `method`、`purpose`、`fee_cents`，`init_transfer_schema` 内做幂等 `ALTER TABLE`。
6. `ScheduledTransfer` 增加对应字段。

**完成判据**
- 旧库启动后自动补列；
- `/api/options`、`/api/payees`、`/api/transfers` 可正常读取；
- 旧数据缺失字段时按“未知/历史”处理，不抛异常。

## 任务 2：写入链路

**文件**
- `src/ai_banking_copilot/gateway/core_bank.py`
- `src/ai_banking_copilot/scenarios/transfer.py`
- `src/ai_banking_copilot/tools/catalog.py`

**改动**
1. `CoreBank.transfer` 增加可选参数：`transfer_method`、`transfer_purpose`、`transfer_fee_cents`、`transfer_remark`；写入 `transactions` 新列。
2. `CoreBank.book_transfer` 增加 `method`、`purpose`、`fee_cents`；写入 `scheduled_transfers`。
3. `build_money_node` 从 `intent.slots` 读取 `transfer_method` / `purpose`，放入节点参数。
4. `_transfer_handler` / `_book_transfer_handler` 透传参数。
5. `catalog.py` 为 `transfer.create` / `transfer.schedule` / `transfer.book` 增加可选 `transfer_method`、`purpose` 参数定义。

**完成判据**
- 页内转账与 AI 对话转账都能写入方式、用途、附言；
- 未提供新参数时行为与旧逻辑一致。

## 任务 3：记录接口与收款人接口

**文件**
- `src/ai_banking_copilot/web/app.py`
- `src/ai_banking_copilot/gateway/payee_store.py`（新增）
- `src/ai_banking_copilot/compose.py`

**改动**
1. `GET /api/payees` 返回 `bank_name`、`masked_account`、`identity_confirmed`。
2. 新增 `POST /api/payees`：姓名 + 账号后四位 + 开户行 + 手机号后四位；只保存尾号，幂等去重。
3. `AppDeps` 增加 `payee_writer`，组合根注入；未装配时 POST 返回 409，不静默假装成功。
4. `GET /api/options.quota` 增加：`single_limit_cents`、`monthly_limit_cents`、`monthly_accumulated_cents`、`limit_notes`。
5. `GET /api/transfers` 重写收款人解析与字段输出；修复“李娜 → 收款人已隐藏”。
6. 记录状态：`已到账` / `处理中` / `已发出申请·可撤销` / `已撤销`；区分 `seed` / `live`。

**完成判据**
- `转给李娜 68 元` 的种子记录显示“李* · 尾号 9021 · 招商银行”，不再显示“收款人已隐藏”。
- 执行一笔转账后，记录里能看到方式、手续费、用途、渠道、来源。

## 任务 4：转账场景页

**文件**
- `src/ai_banking_copilot/web/page.py`
- `src/ai_banking_copilot/web/assets/app.js`
- `src/ai_banking_copilot/web/assets/workspace.css`

**改动**
1. 重写 `[data-scene-block="transfer"]`：额度概览、付款账户、收款人三要素、方式四选一、金额、用途 + 附言、手续费、反诈提示、提交。
2. 删除快捷金额、调试文案、`去对话办这一笔`。
3. `renderQuota` 改为用户语言，展示单笔 / 日累计 / 月累计 / 剩余。
4. 新增表单状态与实时校验：账户余额、单笔、日累计、月累计、收款人必填、账号/开户行必填、可疑话术。
5. 首次收款人 3 秒提示；`blocked` 收款人拦截；大额冷静期文案。
6. 提交时组装 `selected`：`account`、`payee`、`amount`、`transfer_method`、`purpose`、`remark`；按方式选择 `transfer.create` / `transfer.book` 对应话术。
7. 计划卡确认文案改为“确认转账”；红色卡在本页申请一次性验证码并提交。
8. “用 AI 助理转账”把当前表单值带入对话，不空跳。

**完成判据**
- 不选账户、不填收款账号/开户行、金额超限时不能提交；
- 次日到账提交后余额不变，记录显示“已发出申请·可撤销”；
- 红色大额在本页能完成一次性验证码确认。

## 任务 5：记录与明细页

**文件**
- `src/ai_banking_copilot/web/assets/app.js`
- `src/ai_banking_copilot/web/page.py`

**改动**
1. 转账记录表头与单元格补全：方式、状态、用途、收款账号尾号、来源。
2. 详情补手续费、附言、付款账户、渠道。
3. 每条记录增加“再转一笔”；定时记录增加“撤销”。
4. 增加“按收款人汇总”折叠视图。
5. 场景页“最近转账”同步使用新字段，只显示最近 5 笔。

**完成判据**
- 记录页可直接对账；
- “再转一笔”能回填表单；
- 撤销后状态变为“已撤销”。

## 任务 6：AI 槽位与计划卡

**文件**
- `src/ai_banking_copilot/planning/slots.py`
- `src/ai_banking_copilot/planning/llm.py`
- `src/ai_banking_copilot/web/plan_card.py`

**改动**
1. `_CHOICE_ALIASES` 增加 `transfer_method` 与 `purpose`。
2. `_resolve_slot` 增加方式 / 用途分支；`selected` 优先，原文关键词兜底。
3. `llm.py` 增加“24 小时延时 / 次日到账 / 普通到账 / 大额转账”模板规则，放在通用转账规则之前。
4. 计划卡摘要增加方式、到账时间、手续费、用途、收款账号尾号。
5. 澄清提示补“开户行”。

**完成判据**
- “24 小时延时转账给李娜 500 元”生成 `transfer.book` 计划卡，卡片写“次日 00:00 后发出，可撤销”；
- “普通到账转给李娜 500 元”生成即时计划卡，卡片写“预计 2 小时内到账（演示即时记账）”。

## 任务 7：验证

**不运行 pytest。** 执行：

```bash
ruff check src/ai_banking_copilot/gateway/store.py \
  src/ai_banking_copilot/gateway/models.py \
  src/ai_banking_copilot/gateway/facts.py \
  src/ai_banking_copilot/gateway/transaction_facts.py \
  src/ai_banking_copilot/gateway/seed.py \
  src/ai_banking_copilot/gateway/transfer_store.py \
  src/ai_banking_copilot/gateway/core_bank.py \
  src/ai_banking_copilot/gateway/payee_store.py \
  src/ai_banking_copilot/scenarios/transfer.py \
  src/ai_banking_copilot/tools/catalog.py \
  src/ai_banking_copilot/planning/slots.py \
  src/ai_banking_copilot/planning/llm.py \
  src/ai_banking_copilot/web/app.py \
  src/ai_banking_copilot/web/page.py \
  src/ai_banking_copilot/web/plan_card.py
node --check src/ai_banking_copilot/web/assets/app.js
```

再用本地服务人工走查：
1. 转账页新增字段与校验；
2. 实时 / 次日两种方式的余额与记录差异；
3. 记录页字段、“再转一笔”、撤销；
4. 对话里说“24 小时延时转账给李娜 500 元”。

## 风险与回滚

- 新增列全部可空或带默认值，旧库可直接回滚代码继续运行。
- 不改 `policy/gate`、`policy/pep`、审计哈希。
- 月累计限额是演示口径，界面必须标注“演示”，不能写成监管要求。
- 不保存完整收款卡号；只保存后四位。

## 实现落地说明（2026-11-02）

已按本计划落地，具体口径如下：

1. **付款账户 / 收款人三要素 / 到账方式**：转账页新增账户下拉、姓名 + 账号 + 开户行 + 手机号、四种到账方式；选择名册收款人自动带出账号尾号与开户行，新收款人通过 `POST /api/payees` 登记。
2. **限额**：`/api/options.quota` 新增单笔、月累计与限额说明；单笔来自卡片转账通道，日累计沿用既有 PDP 口径，月累计是演示口径。日/月超限不硬拦，提示“本笔将进入强验证”；单笔超限与余额不足硬拦。
3. **延时与撤销**：次日到账走 `transfer.book`（登记不扣款），记录状态为“已发出申请·可撤销”；新增 `POST /api/transfers/cancel`，撤销只改登记状态并写审计，不产生资金动作。
4. **记录与明细**：`/api/transfers` 修复种子流水“转给李娜”解析失败导致的“收款人已隐藏”，补齐收款账号尾号、开户行、方式、手续费、用途、附言、渠道、来源；记录页增加“再转一笔 / 用 AI 再转一笔 / 撤销”和按收款人汇总。
5. **AI 对话**：新增可选槽位 `transfer_method` / `purpose`；计划卡摘要展示方式、到账时间、手续费、用途；“24 小时延时转账”模板落到 `transfer.book`。
6. **能力边界**：演示环境只保存收款账号后四位；一次性验证码由既有服务端挑战提供，**不是**真实短信 / 人脸 / 交易密码；手续费固定 0，真实费用以银行实际为准。
7. **测试夹具兼容**：`tests/scenarios/test_due_execution.py` 的 `scheduled_transfers` 插入改为显式列名，以适配新增的 `method / purpose / fee_cents` 列（断言语义未改）。

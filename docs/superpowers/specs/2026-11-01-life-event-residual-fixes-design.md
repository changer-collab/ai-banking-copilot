# 生活事件遗留五项修复设计（Q13 / Q16 / Q17 / Q18 / ME-02）

> 日期：2026-11-01
> 范围：`docs/2026-10-31/实测清单/实测清单-尹辰-生活事件与记录页.md` 中仍为 ❗ 的五条
> 约束：不新增业务动作、不改 `contracts/`、不改 `policy/pep.py` 三条硬约束、不改 `policy/gate.py` 判据

## 0. 决策记录（用户 2026-11-01 已确认）

| 议题 | 结论 |
|---|---|
| Q13 部分失败补偿 | 保留「整笔退回」，补失败清单文案 + 重试 / 放弃入口 + 审计关联 |
| Q16 定时提醒 | 站内提醒 + 飞书推送；不新增提醒实体，复用 `life_event_schedules`（`notify_on` + `order_on` 分离） |
| Q17 理财到期 | 日历标注（只读事实）+ 到期赎回下单联动预约 |
| Q18 推送隔离 | 真实 `visibility` 字段；飞书只推本人私聊；管不到的外部渠道如实说明 |
| 飞书确认路径 | **绿卡 / 黄卡可在飞书确认**；**红卡与缺数据的卡片一律回 Web**（强验证与补参没有飞书交互面） |
| LLM 分工 | 字段剔除与标签映射**确定性**完成（可测、不泄漏）；LLM 只做措辞与候选功能排序，不可用时退回模板 |
| 测试口径 | 先写测试文件；**全程不执行 `pytest`（用户最后统一跑）**，每步只跑 `ruff` / `mypy` |

## 1. 目标与非目标

**目标**：让这五条从「后端没有 / 答非所问」变成「有确定性行为、有可复测的页面表现、说不清的地方如实说明」。

**非目标**：
- 不新增工具动作（当前 25 个不动）；
- 不接真实支付 / 真实配送 / 真实对方渠道；
- 不改 PDP 判据与 PEP 硬约束；
- 不做「自动重试」——重试必须用户点。

## 2. 前置项：呈现层字段白名单（五项共用）

**问题**：`plan_card.py` 直接把节点参数铺到卡片上，`occasion_id`、`budget_hold_id`、`account_id`、`hold_id` 这类内部键会外露（Q16 实测已复现）。

**设计**：新增 `web/present.py`。

```
CARD_FIELDS: Mapping[str, Mapping[str, FieldSpec]]
    action -> { 参数名 -> (中文标签, 格式化器) }

build_fact_lines(action, params, result) -> tuple[FactLine, ...]
    只输出白名单内的字段；白名单外的键一律丢弃，
    并把丢弃的键名写进 present 审计（证明「不是漏了，是不允许出卡」）。
```

- 金额统一走 `Money` 格式化（分 → 元，两位小数），禁止字符串拼接；
- 日期统一 `YYYY-MM-DD`，需要相对表达（「2 天后」）时由服务端按 `deps.today` 算好再放进事实束；
- **未登记白名单的 action 不改变现有渲染**，只剔除 `_INTERNAL_KEYS` 常量表里的内部键
  （`user_id` / `occasion_id` / `budget_hold_id` / `hold_id` / `account_id` / `card_id` /
  `product_id` / `payee_id` / `subscription_id` / `schedule_id` / `grant` / `span`），
  避免一次性改动 25 个动作的卡片外观。

**LLM 措辞层**：新增 `web/wording.py`。

```
polish(kind, facts, fallback_template) -> str
    输入只有已白名单的事实束（dict），不含内部 ID；
    LLM 仅允许改写措辞，不得新增数字、日期、商户、状态；
    输出做一次事实回查：出现事实束之外的数字/日期即判失败 → 用模板。
    AIB_DISABLE_LLM=1 或超时 / 异常 → 直接模板。
```

这样「隐藏原始字段」由代码保证，「话说得像人」才交给模型。

## 3. Q13 · 部分失败补偿（下单失败不留半截）

**现状**：`Session._release_open_holds` 已在节点失败时释放本次执行已冻结的 hold，并写 `KIND_REVERSAL` 审计（`outcome=released_on_failure`）。缺的是**可操作性与可读性**。

**改动**：

1. `ExecutionView` 增加 `released_holds: tuple[str, ...]`（现有 `message` 里的「已释放冻结预算 N 笔」改为结构化字段 + 模板文案）。
2. 失败文案固定三段：失败节点与原因 / 已释放清单（含金额）/ 未执行的步骤。
3. `life_event_schedules` 允许 `failed → scheduled` 转移，新增 `retry_count INTEGER NOT NULL DEFAULT 0`；重试不清空 `plan_json`（下次 `/api/due` 重新走提案与确认）。
4. 新端点 `POST /api/life-schedules/retry`（body: `schedule_id`）与 `POST /api/life-schedules/cancel`；记录页对 `failed` 预约渲染「重试 / 放弃」两个按钮。
5. `_settle_due_schedules` 写审计时补 `schedule_id`（从 `pending.life_nodes` 反查），使释放动作与预约对得上账。

**不做**：不按净效应计算「只释放剩余」。理由：现有整笔退回更安全，且能避免与 PEP 节点成员资格、幂等键的耦合。

**验收**：让 `merchant.order` 失败（金额超冻结），确认后 ① `holds` 中该笔为 `released`；② 页面出现「已释放冻结 1 笔」；③ 记录页该预约状态 `failed` 且有「重试」；④ 审计事件含 `schedule_id`。

## 4. Q16 · 定时提醒（站内 + 飞书）

**关键事实**：现有机制里 `due_on` 是「什么时候该提案」，节点参数 `order_on` 是「什么时候下单」，两者本来就分离。提醒不需要新实体。

**改动**：

1. `life_event_schedules` 增加 `kind TEXT NOT NULL DEFAULT 'order'`（`order` | `reminder` | `maturity_link`）与 `notify_on TEXT`；`due_on` 统一为「提案日」。
2. 解析：「生日前 2 天提醒我订蛋糕和鲜花，预算 500」→
   - `notify_on = birthday - 2`、`due_on = notify_on`；
   - 节点 `order_on = birthday`；
   - `budget_cents = 50000`（修 `预算500` 无空格时的金额抽取）。
3. 提案文案按 `kind` 分支：
   - `reminder`：「提醒：{2} 天后（{birthday}）是{场合}。现在下单、当天送达，预算 {amount} 元；确认后执行。」
   - `order`：沿用现有「已登记预约」文案。
4. 前端记录页：日历上区分「提醒日」与「下单日」两种点；卡片走 `present.py` 白名单，不再出现 `occasion_id` / `budget_hold_id`。
5. 飞书：到点推提醒卡——黄卡带「确认办理」按钮（可直接在飞书确认），红卡与缺数据的卡只带「打开网站处理」链接。

**飞书推送实现**（新模块 `web/reminders.py`）：

| 组成 | 做法 |
|---|---|
| 收件人 | `channel_identities` 里该用户已绑定的飞书 `open_id`（`receive_id_type=open_id`，本人私聊） |
| 触发 | app 启动时的 asyncio 定时任务，默认 60s 一轮，只扫 `due_on <= today` 且 `status=scheduled` |
| 内容 | 调 `Session.check_due()` 拿到真实计划卡 → 按级别渲染 → `FeishuAdapter.send` |
| 幂等 | 以 `push:{user_id}:{schedule_id}:{due_on}:{kind}` 作为 `event_id` 落 `channel_audit`，命中即跳过 |
| 无凭据 | replay 模式不真发：落审计 `outcome=replay`，站内照常，前端标「演示未真发」 |

**卡片按级别分流**（与既有 `dispatcher.resume_green` / `FeishuAdapter.render` 语义一致）：

| 卡片级别 | 飞书表现 | 说明 |
|---|---|---|
| GREEN（纯查询） | 调度层自动执行，回执不带按钮 | 沿用 `resume_green`，本轮不改 |
| YELLOW | 卡片带「确认办理」按钮，`card.action.trigger` 走同一条冻结校验 | 已有路径，本轮只需把提醒/到期预约也送进它 |
| RED | **只读卡 + 「打开网站处理」链接**，不附确认按钮 | 强验证没有飞书交互面，点了也会被拒，不如直接引导 |
| 缺数据（`needs_clarification`） | **只读卡 + 「打开网站补齐信息」链接** | 飞书不做补参表单，避免半套交互 |

**改动面**：`feishu.py` 的 `send()` 支持 `receive_id_type`（默认仍 `chat_id`，不破坏既有回调路径）；`render()` 改按 `outbound.level` 分流——`YELLOW` 保留「确认办理」按钮，`RED` 与 `needs_clarification` 换成 `url` 类型按钮，`GREEN` 维持现状。

## 5. Q17 · 理财到期联动

**关键事实**：到期日可确定性推导，不需要新数据：`maturity = WealthHolding.purchased_on + WealthProduct.term_days`。

**改动**：

1. `GET /api/life-events` 增加 `maturities: [{product_id, name, amount, purchased_on, term_days, maturity_on, basis}]`；`basis` 固定写「按起息日 + 产品期限推算，非官方到期公告」。
2. 记录页日历标注到期日（与提醒日、下单日并列，样式区分）。
3. 联动预约：`kind = maturity_link`，`due_on = maturity_on`，节点链：
   ```
   wealth.redeem(amount = 持仓金额, product_id, account_id)
     → merchant.order(merchant, amount_cents, order_on, occasion_id, budget_hold_id=None)
   ```
   - 赎回金额**只取持仓事实**，不得由对话补大；
   - 到期日未到不提案（`due_on <= today` 才进 `_due_life_schedules`，沿用现有 fail-closed）；
   - `maturity_on < today` 时允许补提案，但文案标注「该产品已于 {maturity_on} 到期」。
4. 重放验证：`wealth.redeem` 是 `LARGE_FUNDS` 红动作，确认时走强验证（Web）。

**已知边界**：本仓库不建模「到期自动到账」，赎回仍是需要确认的动作；不把「到期」表述为「已到账」。

## 6. Q18 · 推送隔离 + 未理解时的人性化承接

### 6.1 可见性

**现状澄清**：`merchant.order` 只写 `merchant_orders` 与递减 `holds`，**不产生银行流水**（`gateway/core_bank.py:805-825`）。所以「账单里暴露」在本应用内指的是记录页 / 消息中心 / 推送三处。

**改动**：

1. `life_event_records` 与执行审计增加 `visibility TEXT NOT NULL DEFAULT 'normal'`（`normal` | `private`）。
2. 解析命中隐私语句（「别在…账单推送里暴露」「别让她看到」「私密」等）→ 本次登记 / 执行标 `private`。
3. 过滤：
   - 记录页列表与消息中心：`private` 记录默认折叠，需二次点开（不隐藏本身，避免用户自己也看不到）；
   - 共享 / 家庭维度的推送与摘要：不含 `private`；
   - 飞书：本来就是本人 `open_id` 私聊，不额外降级。
4. 回执文案（模板固定，不交给 LLM 改写）：
   > 已按私密处理：本应用内的共享账单与推送不会出现这笔。管不到的：对方手机银行流水、短信通知、以及共同还款人看到的账单，本应用没有权限处理。

### 6.2 未理解时的承接

`triage.py` 规则判定后，若仍然落到 `_FALLBACK_MESSAGE`，走 `web/suggest.py`：

```
suggest(utterance, capabilities, gateway_facts) -> SuggestionBundle
    capabilities = 固定能力清单（25 个动作的中文入口，不含事实）
    LLM：从清单里挑 3 个 + 写一句承接；不得新增清单外入口、不得给数字/日期
    校验：返回项必须 ⊆ 清单，否则整体判失败
    兜底：LLM 不可用 / 校验失败 → 现有规则建议（_DEFAULT_SUGGESTIONS 等）
```

`_FALLBACK_MESSAGE` 同步改为「不确定具体办哪件，先给你几个可能用到的入口」这类承接语，不再写死生日模板。

## 7. ME-02 · 未登录匿名浏览

**现状**：`AIB_AUTH_MODE=demo` 时 `_page_or_login` 未登录直接返回登录页，连应用骨架都不给。

**改动**：

1. `_page_or_login`：未登录返回页面壳 + `anonymous: true`，不再重定向到登录页。
2. 新增只读演示通道：未登录的 `GET` 只读接口返回按 seed 生成的演示数据，响应带 `demo: true`。
3. 所有写接口（`/api/submit`、`/api/confirm`、`/api/clarify`、`/api/due`、测评、订阅变更）未登记一律 401，不因匿名浏览放宽。
4. 前端：顶部固定「演示数据 · 未登录只读」banner；写操作按钮文案改为「登录后办理」，点击跳登录。
5. 登录后 `demo` 标记消失，走真实会话数据。

**边界**：演示数据必须显式标注，不得让访客误以为是自己账户的真实余额。

## 8. 数据与迁移

`web/life_event_store.py` 的建表是 `CREATE TABLE IF NOT EXISTS`，已有库不会自动加列，故 `init_life_event_schema` 增加幂等迁移：

```
ALTER TABLE life_event_schedules ADD COLUMN kind TEXT NOT NULL DEFAULT 'order';
ALTER TABLE life_event_schedules ADD COLUMN notify_on TEXT;
ALTER TABLE life_event_schedules ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE life_event_records ADD COLUMN visibility TEXT NOT NULL DEFAULT 'normal';
```

（按 `PRAGMA table_info` 判断列是否存在后再执行，重复启动无副作用。）

## 9. 测试（先写；执行口径由用户当次指示决定）

| 文件 | 覆盖 |
|---|---|
| `tests/web/test_present_fields.py` | 白名单：`occasion_id` / `budget_hold_id` 不出卡；未知字段记 dropped；金额格式 |
| `tests/web/test_due_failure_compensation.py` | 下单失败 → hold 释放、状态 failed、审计含 schedule_id；重试把状态回 scheduled |
| `tests/web/test_reminder_schedule.py` | 「前 2 天提醒」→ notify_on = 生日-2、order_on = 生日；提案文案含天数 |
| `tests/web/test_maturity_linkage.py` | maturity = purchased_on + term_days；未到期不提案；链为 redeem→order |
| `tests/web/test_visibility.py` | 私密语句 → 记录 private；共享推送不含该记录；回执含边界说明 |
| `tests/web/test_anonymous_browse.py` | 未登录 GET 页面壳 200 + demo 标记；写接口 401 |
| `tests/web/test_suggest_fallback.py` | LLM 返回清单外功能 → 整体失败并退回规则建议 |
| `tests/web/test_feishu_card_level.py` | YELLOW 带确认按钮；RED 与 `needs_clarification` 只出 URL 按钮、无 `card.action` |

## 10. 执行顺序

1. `present.py` + `wording.py`（前置，五项都要用）
2. Q13 失败收尾与重试（后端 + 端点 + 记录页按钮）
3. ME-02 未登录浏览（后端只读通道 + 前端 banner）
4. Q16 提醒（解析 + 存储迁移 + 提案文案 + 日历标注）
5. `reminders.py` 飞书推送（open_id 发送 + 幂等 + replay 审计）
6. Q17 到期联调（maturities + 联动预约链）
7. Q18 可见性 + `suggest.py` 承接
8. 补测试文件，按用户指示决定是否执行 `pytest`

**改动文件清单**（仅供执行时对照，用户为组长，无需变更申请卡）：

| 文件 | 改动 |
|---|---|
| `web/present.py`、`web/wording.py`、`web/suggest.py`、`web/reminders.py` | 新增 |
| `web/life_event_store.py` | 新列幂等迁移、`failed → scheduled` 重试、`visibility` |
| `web/session.py` | `released_holds`、重试/取消、提醒解析、隐私解析、到期联动 |
| `web/triage.py` | 兜底文案改为承接语 + 走 `suggest.py` |
| `web/app.py` | `retry|cancel` 端点、`maturities`、匿名只读、推送任务挂载 |
| `web/assets/app.js` | 日历标注、私密折叠、匿名 banner、重试按钮 |
| `web/channels/feishu.py`、`web/channels/render.py` | `receive_id_type`、按级别分流按钮 |
| `scenarios/life_events.py`、`scenarios/pending.py` | 提醒 / 联动链的构建与到期重建 |

`tools/catalog.py`、`policy/**`、`contracts/` **不动**。

## 11. 已知边界（交付说明必须写）

- 飞书侧只做 **GREEN 自动执行 / YELLOW 按钮确认**；**RED 与缺数据的卡片一律回 Web**（强验证与补参没有飞书交互面），飞书卡片只给 URL 按钮；
- 未配置 `AIB_FEISHU_*` 时推送为 replay：站内提醒照常，飞书不真发且界面标「演示未真发」；
- 理财到期日是**推算值**（起息日 + 产品期限），不是官方到期公告；不做「已到账」表述；
- `private` 只覆盖本应用内的记录、消息中心与推送；对方手机银行、短信、共同还款人账单本应用无权限；
- LLM 只做措辞与候选排序，禁用时不降级为「看起来是 AI」（`AIB_DISABLE_LLM=1` 走模板）；
- 匿名浏览展示的是 seed 演示数据，与访客本人无关。

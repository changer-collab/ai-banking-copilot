# 卡片管理重构：临时冻结 / 正式挂失拆分、限额模型分卡、补卡与弹窗强验证

来源：用户 2026-11-02 对卡片页的七条问题清单 + 本轮确认的 4 个决策
关联代码：`web/assets/app.js`（卡片块）、`web/page.py`、`web/app.py`、`web/plan_card.py`、`scenarios/cards.py`、`gateway/models.py`、`gateway/store.py`、`gateway/seed.py`、`gateway/facts.py`、`gateway/core_bank.py`、`tools/catalog.py`、`policy/pdp.py`、`planning/slots.py`、`planning/llm.py`

## 0. 决策记录（用户已确认）

| # | 决策 | 取值 |
|---|---|---|
| 1 | 落地范围 | **全栈改造**：扩展卡表与 `/api/cards`、重做卡片页与弹窗、补卡/额度申请落库；执行仍走 `/api/submit → /api/verify/request → /api/confirm`，审计链不变 |
| 2 | 动作集 | **允许新增动作**：`card.replace`、`card.adjust_credit_limit`、`card.set_cycle_limit`；动作数由 25 变为 28，六处同步补齐 |
| 3 | AI 对话侧 | **方案 A：对话内办理 + 统一弹窗**。AI 负责识别意图、反问关键分支、生成预填参数；计划卡按钮改为「打开操作确认」，执行与强验证统一在卡片弹窗完成；AI 自己不调用 confirm |
| 4 | 强验证呈现 | **复用服务端一次性验证码**（现有红色挑战机制），并在界面如实标注「演示验证码，非真实人脸 / 短信 / U 盾」 |
| 5 | 测试纪律 | 先写测试；**本轮不执行 pytest**（用户明确要求），只跑 `ruff` / `mypy` 与页面自检 |

## 1. 问题现状（截图与代码核对）

- 临时冻结与正式挂失混成一个「挂失」动作：[cards.py](../../../src/ai_banking_copilot/scenarios/cards.py) 只有 `card.lock / card.unlock / card.report_lost` 三个状态动作，页面把 `locked` 与 `lost` 都叫「挂失」周边文案；
- 已挂失卡仍渲染「解锁 / 调整日限额 / 申请虚拟卡」：[app.js](../../../src/ai_banking_copilot/web/assets/app.js) 的 `cardFaceHtml` 只按 `status !== "active"` 二分，不看 `lost` 与 `locked` 的区别；
- 「调整日限额」对信用卡语义错误：`cards` 表只有 `daily_limit_cents`，信用卡没有日限额概念；
- 挂失只有一个「确认挂失」弹窗，未收集类型、原因、后果、补卡信息，也未在页内做强验证；红色动作被引导到对话完成；
- 卡号直接回显 `card_credit / card_debit`（后端主键）；缺有效期、卡组织、账户类别；
- 底部「日限额分布」饼图（96% vs 4%）对用户无决策价值；
- 对话侧「卡丢了」被 `planning/llm.py` 模板直接识别成 `card.lock` 并生成可确认计划，AI 承担了不该承担的动账/状态变更入口。

## 2. 目标与非目标

**目标**

1. 临时冻结（可解、免费、到期自动解挂）与正式挂失（不可逆、可能收费）彻底拆开，按钮文案、颜色、位置、后果说明严格区分。
2. 按钮由服务端 `allowed_actions` 按卡状态驱动，已挂失卡不出现「解挂 / 调额」。
3. 借记卡与信用卡限额模型分开：借记卡四通道日限额；信用卡固定额度、临时额度、账单周期消费限额。
4. 补卡是独立流程：地址、邮寄方式、费用、预计到卡，新卡号、旧卡作废。
5. 所有卡片写操作在弹窗内完成「信息收集 → 后果明示 → 强验证 → 二次确认」，不跳对话。
6. AI 只识别意图与反问分支，不代执行；对话内仍可办理，但执行收口到同一个弹窗。
7. 卡片信息脱敏，状态实时同步，删除无决策价值的饼图。

**非目标**

- 不接真实人脸、短信、U 盾、邮寄、制卡、征信、额度审批系统；未接入能力一律标注演示。
- 不实现后台定时任务；「临时冻结到期自动解挂」按读取时业务日判定。
- 不实现真实扣收挂失费 / 补卡工本费；只登记应收费用，明确标注「演示不扣款」。
- 不改 `contracts/`、`policy/audit.py` 哈希算法、`policy/gate.py` 判据、`policy/pep.py` 三条硬约束。
- 不把演示地址、演示卡号说成真实信息。

## 3. 卡片状态机

```
active ──临时冻结(card.lock, 5–15 天)──> locked ──到期 / 解挂(card.unlock)──> active
   │                                       │
   │                                       └──转正式挂失──┐
   └──────────正式挂失(card.report_lost)──────────────────> lost（不可逆）
                                                             │
                                                      补卡(card.replace)
                                                             ↓
                                               lost + replaced_by（新卡 active、新卡号）
```

| 状态 | 语义 | 可逆 | 页面标签 |
|---|---|---|---|
| `active` | 正常 | — | 正常 |
| `locked` | **临时冻结（口头挂失）**，带 `temp_lock_until`、`lock_reason` | 可解 / 到期自动解 | 临时冻结（口头挂失） |
| `lost` | **正式挂失**，带 `lost_at`、`loss_reason` | 不可逆 | 已正式挂失 |
| `lost` + `replaced_by_card_id` | 已补卡，旧卡作废 | — | 已挂失 · 已补卡 |

**术语统一**：页面不再出现单独的「锁定」二字。两种状态固定叫「临时冻结（口头挂失）」与「正式挂失」，避免用户分不清哪个可解、哪个不可逆。

- 不新增 `temp_locked` 状态值，避免改 SQLite `CHECK (status IN ('active','lost','locked'))`；`locked` 即临时冻结。
- `effective_status`：读取时若 `status == "locked"` 且 `temp_lock_until < 业务日`，对外状态按 `active` 返回，并带 `temp_lock_expired = true`。演示环境不跑后台任务，这一点在页面与文档中如实标注。
- 解挂只允许 `locked → active`；`lost` 调 `card.unlock` 一律拒绝（保留现有 `_reject_unlock_of_lost_card` 语义并补测试）。

## 4. 动作集（25 → 28）

### 4.1 动作与参数

| 动作 | 业务含义 | 级别 | 关键参数 | 备注 |
|---|---|---|---|---|
| `card.list` | 卡片列表 | 绿 | `user_id` | 不变 |
| `card.lock` | **临时冻结** | 黄 | `card_id`、`duration_days` 5–15、`reason` | 施加保护；可逆 |
| `card.unlock` | 解挂 | 红 | `card_id` | 仅 `locked` 可解 |
| `card.report_lost` | **正式挂失** | 红（不可逆） | `card_id`、`reason`（lost/stolen/other） | 不可解挂 |
| `card.replace`（新） | 补卡 | 红 | `card_id`、`address`、`mail_method`（standard/express）、`with_report`（是否同时挂失） | 新卡号；旧卡 `replaced_by` |
| `card.set_limit` | **借记卡**通道限额 | 红 | `card_id`、`limit_kind`（atm/transfer/purchase/overseas）、`daily_limit_cents`（沿用现有参数名） | 信用卡不走此动作 |
| `card.adjust_credit_limit`（新） | 信用卡额度 | 红 | `card_id`、`mode`（temporary/permanent）、`amount_cents`、`duration_months`（临时默认 1） | 临时立即生效；固定转审批 |
| `card.set_cycle_limit`（新） | 信用卡账单周期消费限额 | 黄 | `card_id`、`amount_cents` | 即时生效 |
| `card.apply_virtual` | 申请虚拟卡 | 黄 | `user_id`、`daily_limit` | 与补卡彻底分开 |
| `password.change` | 修改交易密码 | 红 | `user_id` | 不变 |

**`card.replace` 的复合口径**：`with_report=false` 表示「仅补卡（卡损坏）」；`with_report=true` 表示「挂失并补卡」，由 `CoreBank.replace_card` 在**同一事务**内完成「置 `lost` + 写补卡请求 + 发新卡」，避免出现「挂失了但没补卡」或「补了卡但旧卡还能用」的中间态。只挂失不补卡仍走独立的 `card.report_lost`。

### 4.2 三色口径

- `policy/pdp.py` 的 `OFFICIAL_RED_ACTIONS` 新增 `card.replace`、`card.adjust_credit_limit`（只加严，不放宽）。
- `card.set_cycle_limit` 是**收紧**消费上限，走默认黄色（状态变更、用户确认）。
- `card.lock` 保持黄色（施加保护）；`card.unlock`、`card.set_limit` 保持红色。
- `card.replace` 虽无金额，但涉及换卡号、邮寄地址与卡片生命周期，按红色要求强验证。

### 4.3 六处同步清单（新增动作必改）

1. `tools/catalog.py` 的 `_CARDS_SPECS`：动作、参数、`slot_name`、`kind`、`risk`、`is_pure_query`；
2. `scenarios/cards.py`：`CARD_ACTIONS`、`build` 分支、`WRITE_HANDLERS`；
3. `gateway/core_bank.py`：`replace_card`、`set_credit_limit`、`request_credit_limit_change`、`set_cycle_limit`、`set_card_status` 扩展（到期时间 / 原因）；
4. `policy/pdp.py`：`OFFICIAL_RED_ACTIONS`；
5. `planning/slots.py` + `planning/llm.py`：新槽位提示与口语模板；
6. `eval/data/cases.json`、`eval/data/web_cases.json`、`tests/**`：新动作与分支反问用例。

## 5. 数据模型

### 5.1 `cards` 扩展列（全部可空，走 `_OPTIONAL_COLUMNS`）

| 列 | 类型 | 适用 | 说明 |
|---|---|---|---|
| `brand` | TEXT | 全部 | `unionpay` / `mastercard` / `visa` |
| `expiry_month` / `expiry_year` | INTEGER | 全部 | 有效期展示 `09/29` |
| `account_class` | TEXT | 借记卡 | `Ⅰ` / `Ⅱ` / `Ⅲ` 类户 |
| `atm_limit_cents` | INTEGER | 借记卡 | ATM 取现 |
| `transfer_limit_cents` | INTEGER | 借记卡 | 转账 |
| `purchase_limit_cents` | INTEGER | 借记卡 | 消费 |
| `overseas_limit_cents` | INTEGER | 借记卡 | 境外 |
| `credit_limit_cents` | INTEGER | 信用卡 | 固定额度 |
| `temp_credit_limit_cents` | INTEGER | 信用卡 | 临时额度（生效中时非空） |
| `temp_credit_until` | TEXT | 信用卡 | 临时额度到期日 |
| `cycle_limit_cents` | INTEGER | 信用卡 | 账单周期消费限额 |
| `temp_lock_until` | TEXT | 全部 | 临时冻结到期日 |
| `lock_reason` | TEXT | 全部 | `cant_find` / `suspected_risk` / `other` |
| `lost_at` | TEXT | 全部 | 正式挂失时间 |
| `loss_reason` | TEXT | 全部 | `lost` / `stolen` / `other` |
| `replaced_by_card_id` | TEXT | 全部 | 补卡后的新卡 id |

`daily_limit_cents` 保留为兼容字段：借记卡等同于 `purchase_limit_cents` 的兜底，信用卡不再对外展示为「日限额」。读取层优先新列，缺失时回退 `daily_limit_cents`，并在 API 中带 `limit_source: "explicit" | "fallback"`，不凭空补数字。

### 5.2 新表

```sql
CREATE TABLE IF NOT EXISTS card_status_events (
    event_id    TEXT PRIMARY KEY,
    card_id     TEXT NOT NULL,
    action      TEXT NOT NULL,            -- temp_lock / unlock / report_lost
    reason      TEXT,
    note        TEXT,
    occurred_on TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS card_replacement_requests (
    request_id   TEXT PRIMARY KEY,
    card_id      TEXT NOT NULL,
    new_card_id  TEXT NOT NULL,
    address      TEXT NOT NULL,
    mail_method  TEXT NOT NULL CHECK (mail_method IN ('standard','express')),
    fee_cents    INTEGER NOT NULL,
    eta_on       TEXT NOT NULL,
    status       TEXT NOT NULL,           -- submitted / issued（演示直接 issued）
    created_on   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS card_limit_applications (
    application_id TEXT PRIMARY KEY,
    card_id        TEXT NOT NULL,
    mode           TEXT NOT NULL CHECK (mode IN ('temporary','permanent')),
    requested_cents INTEGER NOT NULL,
    status         TEXT NOT NULL,         -- pending / effective
    submitted_on   TEXT NOT NULL,
    eta_on         TEXT
);
```

### 5.3 种子数据

- `card_debit`：`account_class=Ⅰ`、`brand=unionpay`、有效期 `09/29`、四通道限额（ATM 5000 / 转账 20000 / 消费 20000 / 境外 5000）；
- `card_credit`：`brand=mastercard`、有效期 `11/28`、固定额度 50000、账单周期消费限额 8000；
- 不预置临时冻结 / 已挂失状态，演示时由用户操作触发；补卡请求与额度审批只由操作产生。

## 6. 接口契约

### 6.1 `GET /api/cards`

```json
{
  "as_of": "2026-10-31",
  "synced_at": "2026-11-02T10:21:03Z",
  "default_shipping_address": "上海市浦东新区世纪大道 100 号 1 栋 1201 室（演示）",
  "cards": [
    {
      "card_id": "card_credit",
      "type": "credit",
      "type_label": "信用卡",
      "status": "active",
      "effective_status": "active",
      "status_label": "正常",
      "masked_no": "**** **** **** 9021",
      "last4": "9021",
      "brand": "mastercard",
      "brand_label": "万事达",
      "expiry": "11/28",
      "account_class": null,
      "limits": {
        "kind": "credit",
        "credit_limit_cents": 5000000,
        "temp_credit_limit_cents": null,
        "temp_credit_until": null,
        "cycle_limit_cents": 800000
      },
      "temp_lock": null,
      "lost": null,
      "replacement": null,
      "allowed_actions": ["temp_lock", "adjust_credit_limit", "set_cycle_limit", "report_lost", "replace", "apply_virtual"]
    }
  ]
}
```

- `allowed_actions` 由服务端按 `effective_status` 计算，是页面按钮的唯一真源；
- `limits.kind` 为 `debit` 或 `credit`，字段不同；
- `temp_lock`：`{ "until": "2026-11-05", "days_left": 3, "reason": "cant_find" }`；
- `lost`：`{ "at": "2026-10-28", "reason": "stolen" }`；
- `replacement`：`{ "request_id": "...", "new_card_tail": "8821", "mail_method": "express", "eta_on": "2026-11-04", "fee_cents": 2000 }`；
- `masked_no` 只由服务端拼装，前端不得用 `card_id` 当卡号展示。

`allowed_actions` 是**界面操作键**，与规范动作的映射固定如下（前端不得自行扩展）：

| 操作键 | 规范动作 | 出现状态 |
|---|---|---|
| `temp_lock` | `card.lock` | `active` |
| `unlock` | `card.unlock` | `locked` |
| `report_lost` | `card.report_lost` | `active` / `locked` |
| `replace` | `card.replace` | `active` / `lost` |
| `adjust_debit_limit` | `card.set_limit` | `active`（仅借记卡） |
| `adjust_credit_limit` | `card.adjust_credit_limit` | `active`（仅信用卡） |
| `set_cycle_limit` | `card.set_cycle_limit` | `active`（仅信用卡） |
| `apply_virtual` | `card.apply_virtual` | `active` |
| `view_lock_detail` | 只读 | `locked` |
| `view_lost_detail` | 只读 | `lost` |

### 6.2 卡片动作提交

不新增执行通道，全部复用：

1. `POST /api/submit`：中文指令 + `selected`（受信任控制槽，如 `{card, duration_days, limit_kind, amount_cents, ...}`）；
2. 红色动作：`POST /api/verify/request` → 弹窗内输入一次性验证码；
3. `POST /api/confirm`：带 `verification_code`；
4. 返回回执，刷新 `/api/cards`。

## 7. 页面设计

### 7.1 卡面

- 顶部：类型 + 状态 chip（正常 / 临时冻结 / 已正式挂失 / 已挂失·已补卡）；
- 卡号：`**** **** **** 4471`，旁边显示卡组织与有效期；
- 借记卡：`Ⅰ类户` + 四通道限额；信用卡：固定额度、临时额度（生效中显示到期日）、账单周期消费限额；
- 临时冻结：显示「临时冻结剩余 3 天（至 2026-11-05 自动解挂）」+ 冻结原因；
- 已挂失：显示「已正式挂失（2026-10-28，被盗）· 不可解挂」；已补卡再显示新卡尾号；
- 按钮按 `allowed_actions` 渲染，分组为「常用 / 风险操作」，颜色严格区分：
  - 临时冻结：黄色描边；
  - 解挂、正式挂失、补卡、调额：红色或红色描边；
  - 查看详情：文字按钮。

### 7.2 页面文案

- 顶部说明改为：「临时冻结可解、到期自动解挂；正式挂失不可逆，需补卡换新卡号。所有操作在弹窗内完成验证与二次确认。」
- 右上：`共 N 张卡 · 数据截至 2026-10-31 · 卡状态实时同步（同步于 10:21）`；
- 删除「日限额分布」饼图与 `cardsVisualHtml` 的环形图，改为文字版「卡片使用概览」：每张卡一行，列状态、卡组织、账户类别、限额摘要、临时冻结倒计时；不编造近 30 天消费（流水没有稳定卡号）。

## 8. 弹窗流程（卡片页与对话共用）

同一个 `<dialog id="card-flow-dialog">`，三步 + 结果页。

### 第 1 步：类型与原因

顶部固定展示「临时冻结（口头挂失）」与「正式挂失」两个选项，进入弹窗时按入口预选，但**允许在这一步改选**，改选后后续字段与后果说明随之切换。

| 入口 | 选项 |
|---|---|
| 临时冻结（口头挂失） | 时长 5 / 7 / 10 / 15 天（默认 7）；原因：找不到卡 / 疑似风险 / 其他 |
| 正式挂失 | 原因：遗失 / 被盗 / 其他；选「被盗」追加提示「将登记防盗刷处置（演示：只登记风险标记，不接真实风控系统）」 |
| 解挂 | 直接进入后果页，显示当前冻结原因与到期日 |
| 补卡 | 选择「仅补卡（卡损坏）」或「挂失并补卡」；已挂失卡默认后者且不可改 |
| 调额 | 先选卡类型对应的额度项 |

### 第 2 步：后果与补卡

- 功能冻结范围：消费、取现、转账、代扣分别说明；
- 费用：临时冻结免费；正式挂失 ¥20（演示口径，登记应收、不实际扣款）；补卡工本费普通 ¥10 / 加急 ¥20；
- 是否可解：临时冻结可线上解挂；正式挂失不可解挂；
- 补卡：地址（默认地址可编辑）、邮寄方式、预计到卡时间（普通 3–5 个工作日 / 加急 1–2 个工作日）、新卡号（演示：新卡尾号在提交后生成）。

### 第 3 步：汇总、强验证、二次确认

1. 点「生成确认单」→ `/api/submit`，展示**服务端返回**的 `summary / lines / level`；
2. 红色动作：点「获取验证码」→ `/api/verify/request`，在弹窗内显示通道、目标、有效期与演示码；输入验证码；
3. 按钮文案：`确认挂失` / `确认冻结` / `确认解挂` / `确认补卡` / `确认调额`，不使用「提交」；
4. `/api/confirm` 成功后展示回执、刷新卡面与概览；
5. 取消或关闭弹窗不产生任何动作；已生成的计划卡留在会话中，但不在页面提供二次确认入口。

## 9. AI 对话侧（方案 A）

### 9.1 职责边界

- AI：识别意图、查事实、生成预填参数与计划（分支确认在弹窗第 1 步）；
- 用户：在弹窗内完成强验证与二次确认；
- 服务端：唯一执行面，AI 不调用 `/api/confirm`。

### 9.2 分支反问与确认位置

分支问题放在**操作上下文**（弹窗第 1 步），对话只负责识别与预填；避免同一问题在对话和弹窗里各问一遍。

| 用户说法 | AI 的处理 |
|---|---|
| 临时冻结 / 口头挂失 | 直接生成 `card.lock` 预填计划，弹窗里确认时长与原因 |
| 正式挂失 / 挂失 / 卡丢了 | 按正式挂失预填计划；弹窗第 1 步强制确认类型，可一键改选临时冻结 |
| 卡片被盗 | 预填正式挂失 + 补卡；弹窗内追加防盗刷提示（演示：只登记风险标记） |
| 调额 / 提额 | 借记卡先选通道；信用卡先选临时额度、固定额度或账单周期限额 |
| 信用卡固定额度 | 弹窗明确写「需审批 3–5 个工作日，审批期间额度不变」 |

**实现口径**：`card.report_lost` 的 `loss_type` 是可选控制槽，缺省为 `formal`；`loss_type=temporary` 时场景层改出 `card.lock` 节点（7 天）。真正的类型选择在弹窗内完成，用户不会在未看到「可解 / 不可逆」差异的情况下确认。

### 9.3 对话内交互

- 对话里的卡片计划卡保留，但按钮从「确认并执行」改为「打开操作确认」；
- 点击后打开同一个 `card-flow-dialog`，带入 `card_id / frozen_hash / level`，在弹窗内完成验证与确认；
- 弹窗确认成功后把回执同步回对话流（`applyExecution` + `refreshAfterExecution`），避免对话仍显示「待确认」；
- 回执回到对话流与卡片页；
- 黄色动作（临时冻结、账单周期限额）在弹窗内确认即可，不强制验证码；红色动作必须验证码。

### 9.4 口语模板调整

- `planning/llm.py`：把「卡丢了 / 丢失 / 被盗」改为先触发分支反问，不再直接落到 `card.lock` 可确认计划；
- 新增「临时冻结」「正式挂失」「补卡」「临时提额」「固定额度」「账单周期限额」模板；
- 「额度 / 限额」不再一律映射 `card.set_limit`，按卡类型与关键词分流，缺关键槽位时追问。

新增槽位（`ParamSpec.slot_name` → 规划层提示）：

| 槽位 | 参数 | 提示语 |
|---|---|---|
| `lock_days` | `duration_days` | 临时冻结几天？可选 5 / 7 / 10 / 15 天 |
| `lock_reason` | `reason` | 是找不到卡、疑似风险，还是其他原因？ |
| `loss_reason` | `reason` | 卡片是遗失、被盗，还是其他情况？ |
| `replace` | `with_report` | 是否同时补卡？补卡会换新卡号 |
| `mail_method` | `mail_method` | 普通邮寄（3–5 个工作日）还是加急（1–2 个工作日）？ |
| `address` | `address` | 新卡寄到哪里？可修改默认地址 |
| `limit_kind` | `limit_kind` | 调整借记卡哪一项限额：ATM 取现 / 转账 / 消费 / 境外？ |
| `credit_mode` | `mode` | 临时额度（1 个月自动恢复）还是固定额度（需审批 3–5 个工作日）？ |
| `duration_months` | `duration_months` | 临时额度生效几个月？（默认 1） |

## 10. 异常与边界

| 场景 | 处理 |
|---|---|
| 已挂失卡点解挂 | 服务端拒绝「挂失不可逆，只能补卡」；页面本就不展示该按钮 |
| 已挂失卡点调额 | 服务端拒绝；页面不展示 |
| 已补卡旧卡再补卡 | 拒绝「该卡已补卡，新卡尾号 xxxx」 |
| 临时冻结已到期 | 读取按 `active` 返回；解挂返回「已自动解挂，无需操作」 |
| 冻结时长超 15 天 | 服务端拒绝，弹窗选项本身只到 15 天 |
| 补卡地址为空 / 超长 | 服务端校验，弹窗内联提示 |
| 验证码错误 / 过期 | 弹窗内联错误，保留输入与计划，可重新获取 |
| 计划过期 | 弹窗提示「计划已过期，重新生成」，不静默重提 |
| 卡片不存在 | 服务端拒绝，弹窗关闭并刷新列表 |

## 11. 诚实性边界

- 一次性验证码是**演示验证码**，不是真实人脸 / 短信 / U 盾；界面、文档、PPT 均按此口径；
- 补卡不会真的制卡或邮寄；`status` 直接落 `issued`，ETA 是演示推算；
- 挂失费 / 补卡费只登记应收，不实际扣款；
- 固定额度审批没有真实审批系统，`pending` 不会自动转 `effective`；
- 「到期自动解挂」是读取时判定，不是后台定时任务；
- 演示地址、演示卡号、演示卡组织信息均在界面标注「演示」。

## 12. 优先级与验收

### P0（资损与不可逆）

1. 状态机拆分：`locked` = 临时冻结，`lost` = 正式挂失；
2. `/api/cards` 返回 `allowed_actions`，页面按状态渲染；
3. 解挂、正式挂失、补卡、调额在弹窗内完成强验证；
4. AI 不调用 confirm，对话内计划卡改为打开弹窗。

### P1

1. 借记卡四通道限额、信用卡固定/临时/账单周期三条线；
2. 补卡完整流程（地址、邮寄、费用、ETA、新卡号）；
3. 临时冻结倒计时与到期自动解挂判定。

### P2

1. 卡号脱敏、卡组织、有效期、账户类别；
2. 删除饼图，改为卡片使用概览；
3. 「数据截至」保留并新增「卡状态实时同步」。

**验收口径**

- 已挂失卡页面只有「申请补卡 / 查看挂失详情」；
- 临时冻结卡页面只有「解挂 / 转正式挂失 / 查看冻结详情」；
- 正常卡才出现全部操作；
- 信用卡不出现「调整日限额」；
- 所有红色动作的验证码在弹窗内完成；
- 对话里说「卡丢了」先被反问，不直接产生可执行计划。

## 13. 测试与门禁

**先写测试**（按项目规矩，新增测试文件，不改既有语义断言）：

- `tests/scenarios/test_cards_state_machine.py`：临时冻结可解、正式挂失不可解、已补卡不可再补、到期判定；
- `tests/web/test_cards_allowed_actions.py`：三种状态下 `allowed_actions` 精确集合；
- `tests/web/test_cards_dialog_contract.py`：弹窗所需的 `/api/submit` 参数与验证码链路（走 TestClient）；
- `tests/gateway/test_card_metadata.py`：脱敏、卡组织、有效期、账户类别、四通道限额、信用额度字段；
- `tests/scenarios/test_card_replace.py`：补卡新卡号、旧卡 `replaced_by`、费用与 ETA 登记；
- `eval/data/cases.json` / `web_cases.json`：新动作与分支反问用例。

**本轮不执行 pytest**（用户明确要求）。可执行的静态门禁：`ruff check .`、`mypy`；涉及页面时用 `scripts/check_served_page.py` 做结构自检。pytest 全绿留给后续经用户同意的完整回归。

## 14. 残留与风险

- 没有后台任务，「到期自动解挂」只在读取时生效；跨业务日长时间挂起的会话可能看到旧状态，刷新即一致。
- 固定额度审批是演示状态，不会自动完成。
- 补卡不产生真实制卡/邮寄动作，只落请求记录与新卡记录。
- 对话侧仍可生成卡片计划卡，服务端未新增「仅弹窗可确认」的强约束；本轮通过 UI 收口，若后续要求服务端级强制，需要新增确认来源标记（属于契约面改动，另行评审）。
- 新增 3 个动作后，动作数口径由 25 变为 28，需要同步修改 README、`docs/RESULTS.md` 口径表与计数断言（下限断言不受影响）。

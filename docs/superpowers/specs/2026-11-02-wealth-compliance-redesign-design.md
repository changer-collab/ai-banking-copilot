# 理财合规与交易链路改造设计（2026-11-02）

来源：用户提供的五模块整改方案 + 本轮确认的 4 个关键选择
关联代码：`web/assets/app.js`（理财块）、`web/app.py`、`web/session.py`、`web/triage.py`、`scenarios/wealth.py`、`gateway/risk_store.py`、`gateway/core_bank.py`、`planning/slots.py`、`planning/llm.py`

## 0. 决策记录（用户已确认）

| # | 决策 | 取值 |
|---|---|---|
| 1 | AI 对话与交易的关系 | **决策/执行分层**：AI 只做理解、查事实、推荐、生成草案；交易由统一确认弹窗收集合规信息后执行。不把交易搬到独立页面，用户仍在对话内完成 |
| 2 | 交易密码 / 生物识别 | **复用服务端一次性验证码**（现有红色挑战机制）；不新增假密码、假人脸 |
| 3 | 65+ 特别照顾 / R4+ 双录 | **只做提示**：65+ 增加提示与更长考虑时间；R4+ 显示双录提示；不登记回访、不做书面留痕、不宣称已双录 |
| 4 | 赎回改份额 | **新增份额字段**：持仓表保存份额与买入净值；核心银行按份额赎回，金额由份额 × 最新模拟净值折算 |
| 5 | 数据与接口 | 前后端一起改；演示数据必须如实标注 |
| 6 | 范围 | P0 + P1 + P2 全量落地，但严格遵守“不新增业务动作、不改三条硬约束” |

## 1. 问题现状

截图与代码核对结果：

- 产品卡在 [app.js](file:///d:/ai-banking/ai-banking-copilot/src/ai_banking_copilot/web/assets/app.js#L581-L637) 里直接放金额输入框 + 申购/赎回按钮，列表页和交易混在一起；
- 产品数据只有 `name / risk_level / term_days / yield_bp / min_amount`，没有费率、登记编码、流动性、发行/销售评级；
- 测评限次是“每日 3 次”的演示口径，[risk_store.py](file:///d:/ai-banking/ai-banking-copilot/src/ai_banking_copilot/gateway/risk_store.py#L49-L54) 明确写着“不是监管要求”，且没有 12 个月累计限制、没有结果变更再确认；
- 权益类/混合类没有本金损失确认，R4+ 没有双录提示；
- 产品卡“不可买”只有一句等级不匹配，没有三条可执行路径；
- 推荐理由折叠区没有匹配结论、主要风险、不适用人群；
- 赎回按金额输入，没有份额、到账时间、快赎识别、大额预警；
- AI 已通过计划卡 + 强验证执行，不存在静默动账；但确认面太薄，缺四件套、抄录、二次确认。

## 2. 目标与非目标

**目标**

1. 产品列表页只做展示，交易统一进入确认弹窗。
2. 申购/赎回弹窗按产品类型增减字段，金额/份额实时校验。
3. 适当性合规按《金融机构产品适当性管理办法》第 18 / 22 / 29 条落地。
4. AI 问答同步修正：只陈述事实、不硬推、不静默执行、赎回自动反查持仓。
5. 所有演示数据、未接入能力都在界面和文案里如实标注。

**非目标（硬约束）**

- 不新增业务动作，仍为 25 个；`wealth.list / wealth.subscribe / wealth.redeem` 不变。
- 不改 `contracts/`、`policy/audit.py` 哈希算法、`policy/gate.py` 判据、`policy/pep.py` 三条硬约束。
- 不接真实净值、真实产品、真实支付、真实短信、真实双录、真实外呼。
- 不把演示编码说成官方登记编码，不把模拟净值说成实时净值。
- 不在对话流里新增任何“静默冻结/扣款/解冻”通道。

## 3. 总体架构：决策 / 执行分层

```
用户（对话 or 场景页）
  → AI：意图识别 + 事实查询 + 推荐 + 生成计划草案（不执行）
  → 交易确认弹窗（唯一合规入口）
       产品摘要 / 账户 / 金额或份额 / 分红方式 / 类型差异字段
       四件套阅读与勾选 / 风险揭示抄录 / 适当性意见
       服务端一次性验证码 / 二次确认
  → 现有 /api/confirm → PEP → CoreBank
  → 回执回到对话 / 场景页
```

- 聊天内：财富计划卡的“确认执行”改为“打开交易确认”，打开同一个弹窗。
- 场景页：产品卡“立即申购”、持仓卡“赎回”也打开同一个弹窗。
- 弹窗只调用现有的 `/api/submit`、`/api/verify/request`、`/api/confirm`，不新增执行通道。
- 申购本来就是红色（`OFFICIAL_RED_ACTIONS` 含 `wealth.subscribe`）；本设计将 `wealth.redeem` 也纳入红色（只加严），让赎回复用同一套服务端验证码。

## 4. 模块一：产品列表页

### 4.1 卡片只展示

每张产品卡只保留：

- 产品名、风险标签（展示“孰高”后的等级）、产品类型；
- 业绩比较基准（标注“业绩比较基准≠实际收益”，演示数据再加“模拟”）；
- 起购金额、递增单位；
- 产品登记编码（演示编码 + 中国理财网查询入口 + “未在官方登记”说明）；
- 三项费率：申购费率、销售服务费、托管费；任一缺失则整卡标“暂不可售（信息披露不完整）”；
- 按类型差异化标签：
  - 货币类：实时赎回单日 1 万 / 普通赎回 T+1；
  - 封闭/定开债券类：封闭期内不可赎回 / 下一开放日 X 月 X 日；
  - 滚动发行类：本期申购截止 X 点；
- 主按钮：“产品详情” + “立即申购”。

移除卡片内的金额输入框和直接申购/赎回按钮。

### 4.2 不可买引导

不可买卡片显示“为什么不能买”折叠区，展开后包含：

1. 当前测评等级 + 有效期；
2. 三条路径：重新测评 / 查看与 R2 匹配的产品 / 联系客服；
3. 重新测评按钮直接打开合规版测评弹窗；
4. 若原因是“信息披露不完整”，路径改为“查看缺失字段 / 联系客服”，不提供申购入口。

### 4.3 产品详情

新增产品详情弹窗，展示：

- 完整产品事实与披露字段；
- 发行方评级 / 销售方评级 / 孰高结论；
- 四份文件入口（演示文本，明确标注）；
- 适当性匹配意见（结论、主要风险、不适用人群、依据、免责声明）；
- 当前是否可买及原因。

### 4.4 测评次数文案

等级条按钮文案改为：

- `重新测评（今日还可 X 次 / 12 个月还可 Y 次）`
- 当日次数用尽：按钮置灰，显示 `下次可测：YYYY-MM-DD`
- 12 个月次数用尽：按钮置灰，显示 `12 个月测评次数已用完，下次可测：YYYY-MM-DD`
- 固定小字：`依据《金融机构产品适当性管理办法》第二十九条；演示环境按模拟业务日计数`

## 5. 模块二：申购确认弹窗

### 5.1 统一骨架

所有产品类型都包含：

1. 产品摘要回显（名称、展示风险等级、业绩比较基准、登记编码、四份文件入口）。
2. 交易账户选择（默认上次使用账户，显示可用余额）。
3. 申购金额：
   - 实时校验：≥ 起购金额、符合递增单位、≤ 账户可用余额；
   - 不合规立即红字提示具体原因，例如“低于起购金额 1,000.00 元，请重新输入”；
   - 金额单位只在输入框后缀出现一次，不在提示里重复“单位：元”。
4. 分红方式：现金分红 / 红利再投资，默认红利再投资，可改。
5. 协议勾选：四份文件分别勾选；普通文件必须点开且阅读 ≥ 3 秒后才可勾选；首次购买 R3 及以上产品时，《风险揭示书》必须阅读 ≥ 15 秒后才可勾选。
6. 风险揭示声明抄录（首次购买 R3 及以上产品）：
   - 固定文案：`本人已充分理解产品风险，自愿承担相关投资风险`；
   - 用户必须逐字抄录，完全一致才允许下一步；
   - 服务端保存抄录文本哈希，不保存明文以外的额外信息。
7. 适当性匹配意见：
   - 结论：`该产品风险 R2，与您的测评等级 R2 匹配`；
   - 主要风险点、不适用人群、依据、免责声明；
   - 不匹配时不允许进入下一步，只展示三条路径。
8. 交易验证：调用现有 `/api/verify/request`，展示“演示验证码已发送至 138****3301”，输入一次性验证码。
9. 二次确认页：汇总产品、金额、账户、分红方式、预计确认日、费率、风险等级；按钮文案为“确认申购”，旁边显示预计成交确认日。

### 5.2 按类型增减字段

- 货币类：
  - 金额校验放宽到 1 元起；
  - 增加“是否同时设置自动赎回 / 消费抵扣”开关；
  - 开关只记录偏好，界面标注“演示环境仅记录偏好，未接入自动赎回”。
- 债券类（封闭/定开）：
  - 弹窗顶部强制流动性提示条：`该产品封闭 30 天，期间不可赎回，请确认资金使用安排`；
  - 申购截止提示：`今日 15:00 前申购按今日净值确认`。
- 混合/权益类：
  - 增加“本人近 12 个月投资经验”确认项；
  - 增加“您已知悉本产品可能损失本金”独立勾选；
  - R4 及以上额外显示提示：`R4 及以上产品需双录或书面确认；演示环境未接入双录，仅提示`。
- 首次购买某只产品：
  - 风险揭示书全文强制阅读，倒计时 `已阅读 X 秒`，满 15 秒才可勾选；
  - 首次购买判定：当前持仓中不存在该产品（按持仓事实判断，界面标注口径）。

### 5.3 65 岁以上客户

- 客户年龄来自新增 `birth_year` 字段；缺失时显示“年龄未登记，无法判断 65+ 特别提示”，不默认年龄。
- 年龄 ≥ 65 且产品风险 ≥ R3 时：
  - 进入弹窗前显示独立提示：`65 周岁以上客户购买 R3 及以上产品，需要更长考虑时间`；
  - 二次确认按钮前增加 15 秒倒计时；
  - 明确标注“人工回访未接入，演示环境仅延长考虑时间”。

## 6. 模块三：赎回确认弹窗

### 6.1 入口与方式

- 入口只放在持仓卡；产品列表页不再出现赎回按钮。
- 赎回方式三选一：
  - 全部赎回；
  - 部分赎回（输入份额）；
  - 按金额赎回（输入金额，反算份额并回显让用户确认）。
- 输入份额时实时显示按最新模拟净值估算的预计到账金额，固定标注：
  `按最新模拟净值折算，实际以 T 日净值结算为准`。

### 6.2 账户与到账

- 到账账户选择：默认绑定卡，可改。
- 到账时间预估：
  - 货币类：实时 / T+1；
  - 债券类：T+1 ~ T+3；
  - 封闭期产品：不可赎回，显示下一开放日。
- 快赎识别：
  - 货币类单笔 ≤ 1 万元走快赎实时到账；
  - 大于 1 万元自动切普通赎回并提示到账日；
  - 非货币类不显示快赎。

### 6.3 协议、验证与二次确认

- 与申购共用：四件套勾选、交易验证码、二次确认页。
- 赎回不需要风险揭示抄录（除非产品规则另有约定）。
- 大额赎回预警：
  - 赎回份额超过当前持仓 50% 时，弹出独立提示：`该操作将赎回您该产品持仓的 80%，请确认`；
  - 全部赎回显示 100%；
  - 提示必须在二次确认页之前出现。

## 7. 模块四：适当性合规

### 7.1 测评次数与有效期

依据《金融机构产品适当性管理办法》第二十九条（已核实原文）：

- 单日不超过 2 次；
- 12 个月累计不超过 8 次；
- 评估结果有效期原则上 12 个月（本仓库按 365 天实现并标注口径）；
- 结果与最近一次不一致的，提示并请投资者再次确认。

实现：

- 新增 `risk_assessment_events` 表，记录每次**已提交**的测评（日期、等级、得分、来源）。
- 每日 2 次：沿用 `risk_assessment_attempts` 按业务日计数。
- 12 个月 8 次：从 `risk_assessment_events` 统计最近 365 天。
- `retake` 返回：
  ```json
  {
    "limit_per_day": 2,
    "limit_per_12m": 8,
    "used_today": 0,
    "used_12m": 0,
    "remaining_today": 2,
    "remaining_12m": 8,
    "next_available_on": "2026-11-01",
    "basis": "regulatory",
    "basis_label": "《金融机构产品适当性管理办法》第二十九条",
    "demo_note": "演示环境按模拟业务日计数"
  }
  ```
- 超限返回 429，附 `retake`。
- 有效期：`expires_on = assessed_on + 365 天`；新增 `days_until_expiry`、`expiring_soon`（≤ 30 天）。
- 到期前 30 天：理财页顶部 + 申购弹窗顶部提醒 `测评将于 X 天后过期，请提前重新评估`。
- 过期后：所有申购入口禁用，弹窗不可打开，文案为 `测评已过期，请重新测评`。

### 7.2 结果不一致

- 提交时若新等级与最近一次不同：
  - 未带 `confirm_change=true`：返回 `state="needs_reconfirmation"`，不落库、不计次；
  - 带 `confirm_change=true`：落库、记事件、更新最新等级。
- 前端显示 `上次 R3 → 本次 R1`，要求再次确认。
- 等级差异 ≥ 2 档时：
  - 增加 10 秒冷静期倒计时；
  - 文案标注 `演示环境用冷静期替代人工复核；人工复核未接入`。
- 计数只在最终确认提交时发生，取消不消耗次数。

### 7.3 风险等级孰高

- 产品披露新增 `issuer_risk_level`、`seller_risk_level`；
- 展示等级 = 两者较高者；
- 详情页同时展示两个评级和孰高说明；
- 适当性判定用展示等级，不用较低评级放宽。
- 演示数据中 `wp_mix` 设为发行方 R2 / 销售方 R3，展示 R3，用来演示孰高原则。

### 7.4 65 岁以上

见 5.3。只做提示与更长考虑时间；不登记回访、不宣称已回访。

### 7.5 匹配意见与推荐理由

每只产品返回 `suitability`：

```json
{
  "conclusion": "该产品风险 R2，与您的测评等级 R2 匹配",
  "main_risks": ["利率风险", "信用风险", "封闭期内不可赎回"],
  "unsuitable_for": ["无法接受本金损失", "短期需用资金"],
  "basis": "依据风险等级、产品类型、期限与起购金额（演示数据）",
  "disclaimer": "不构成投资建议"
}
```

- 匹配意见必须在产品详情和申购弹窗可见；
- “推荐理由”折叠区必须包含结论、主要风险、不适用人群，不能只写“通过硬筛选”；
- 不匹配时结论写清原因和三条路径，不展示申购按钮。

## 8. 模块五：AI 对话链路

### 8.1 不直接执行

- AI 仍只生成计划草案；所有动账走 `/api/confirm` → PEP → CoreBank。
- 对话文案不得出现未发生的“已冻结 / 已扣款 / 已解冻 / 已成交”；新增守卫测试。
- 回执只在 `/api/confirm` 返回 200 后展示。

### 8.2 赎回意图自动反查持仓

- 新增 `_resolve_redeem_holding(utterance, deps)`：
  - 用户说“赎回那笔到期的理财”时，按持仓的 `maturity_on` 匹配；
  - 唯一命中：直接注入产品、全部份额、默认账户，生成 `redemption_prefill`，打开预填赎回弹窗；
  - 多笔命中：用持仓选项让用户选一笔（显示产品名、份额、金额、到期日），不让用户从产品白名单里挑；
  - 无命中：如实说明没有可赎回持仓，不猜。
- 演示数据中 `wp_bond`（2026-03-18 买入，90 天）已到期，`wp_short` 未到期，因此“到期的理财”唯一命中 `wp_bond`。
- `redemption_prefill` 只包含事实，不执行；用户在弹窗里确认。

### 8.3 参数一次性抽全

- 申购：产品、账户、金额能从上下文拿到的先填好；用户只补缺口。
- 赎回：产品、份额、到账账户能从上下文/持仓拿到的先填好。
- 补参提示改为：
  - `请给出申购金额（元）。`
  - `请给出赎回份额（份）。`
  - 不再出现“单位：元，例如 500”这类重复单位与无关示例。

### 8.4 不匹配不硬推

- 等级不匹配时，对话只给两条路径：`重新测评` / `查看与 R2 匹配的产品`；
- 不展示“立即申购”；
- 卡片上也不出现申购按钮。

### 8.5 理财事实问答扩展

新增确定性直答（不经模型、数字只来自 gateway）：

| 问法 | 回答要点 |
|---|---|
| 费率 / 手续费 / 托管费 | 三项费率；标注演示数据、以说明书为准 |
| 多久到账 / 到账时间 | 按产品类型：货币实时/T+1、债券 T+1~T+3、封闭不可赎 |
| 为什么不能买 / 怎么才能买 | 当前等级 + 有效期 + 三条路径 |
| 还能测几次 / 什么时候过期 | 今日剩余、12 个月剩余、有效期、下次可测日期 |
| 登记编码 / 怎么验真 | 演示编码、未在官方登记、中国理财网查询入口 |
| 封闭期 / 开放日 / 能赎回吗 | 封闭天数、下一开放日、不可赎原因 |
| 分红方式 | 现金分红 / 红利再投资，默认红利再投资 |
| 能赎回多少 / 份额 | 持仓份额、可赎回份额、按最新模拟净值估算 |

## 9. 数据模型

### 9.1 新增 `wealth_disclosures`

独立表，不改 `wealth_products` 核心表与 `WealthProduct` 哈希：

```sql
CREATE TABLE IF NOT EXISTS wealth_disclosures (
    product_id             TEXT PRIMARY KEY REFERENCES wealth_products(product_id),
    product_type           TEXT NOT NULL,
    registration_code      TEXT NOT NULL,
    issuer                 TEXT NOT NULL,
    issuer_risk_level      TEXT NOT NULL,
    seller_risk_level      TEXT NOT NULL,
    subscription_fee_bp    INTEGER,
    service_fee_bp         INTEGER,
    custody_fee_bp         INTEGER,
    increment_cents        INTEGER NOT NULL,
    redeem_mode            TEXT NOT NULL,
    fast_redeem_limit_cents INTEGER,
    closed_days            INTEGER NOT NULL DEFAULT 0,
    next_open_date         TEXT,
    purchase_cutoff        TEXT NOT NULL DEFAULT '15:00',
    sale_status            TEXT NOT NULL DEFAULT 'on_sale',
    updated_on             TEXT NOT NULL
);
```

`subscription_fee_bp / service_fee_bp / custody_fee_bp` 为 `NULL` 表示未披露；任一为 `NULL` 即 `disclosure_complete=false`，该产品不可售。

### 9.2 新增 `risk_assessment_events`

```sql
CREATE TABLE IF NOT EXISTS risk_assessment_events (
    event_id      TEXT PRIMARY KEY,
    user_id       TEXT NOT NULL,
    event_on      TEXT NOT NULL,
    risk_profile  TEXT NOT NULL,
    score         INTEGER NOT NULL,
    source        TEXT NOT NULL,
    previous_profile TEXT
);
```

### 9.3 新增 `wealth_trade_acks`

```sql
CREATE TABLE IF NOT EXISTS wealth_trade_acks (
    ack_id           TEXT PRIMARY KEY,
    user_id          TEXT NOT NULL,
    product_id       TEXT NOT NULL,
    action           TEXT NOT NULL,
    doc_versions     TEXT NOT NULL,
    read_seconds     TEXT NOT NULL,
    declaration_text TEXT,
    declaration_hash TEXT,
    dividend_method  TEXT,
    auto_redeem_pref INTEGER,
    account_id       TEXT,
    created_at       TEXT NOT NULL
);
```

### 9.4 新增 `customer_suitability_profiles`

```sql
CREATE TABLE IF NOT EXISTS customer_suitability_profiles (
    user_id    TEXT PRIMARY KEY,
    birth_year INTEGER
);
```

演示用户播种 `birth_year=1990`，不触发 65+ 路径；65+ 路径用测试数据覆盖。

### 9.5 `wealth_holdings` 增列

```sql
ALTER TABLE wealth_holdings ADD COLUMN share_bp INTEGER NOT NULL DEFAULT 0;
ALTER TABLE wealth_holdings ADD COLUMN nav_at_purchase_bp INTEGER NOT NULL DEFAULT 0;
```

旧库迁移：对 `share_bp=0` 的持仓，按 `purchased_on` 取最近模拟净值，计算
`share_bp = amount_cents * 1_000_000 // nav_bp`；无净值时按 `nav_bp=10000` 折算并标注。

### 9.6 份额与金额口径

- 份额（`share_bp`）= 份额 × 10,000，整数存储；
- 净值（`nav_bp`）= 净值 × 10,000；
- 申购：`share_bp = amount_cents * 1_000_000 // nav_bp`；
- 按份额赎回：`amount_cents = share_bp * nav_bp // 1_000_000`；
- 按金额赎回：`share_bp = amount_cents * 1_000_000 // nav_bp`；
- 全部赎回：直接使用持仓的 `share_bp` 与 `amount_cents`，避免尾差；
- 金额仍是核心银行的资金权威，份额用于展示与精确扣减；
- 界面固定标注“按模拟净值折算，实际以 T 日净值结算为准”。

## 10. 接口契约

| 接口 | 变更 |
|---|---|
| `GET /api/wealth/overview` | 产品增披露字段、展示等级、费率、登记编码、流动性、可买原因码、`suitability`；测评增 `days_until_expiry`、`expiring_soon`、新 `retake`；增 `customer_age` / `senior_notice` |
| `GET /api/wealth/products/{product_id}` | 新增：产品详情 + 四份演示文件 + 是否首次购买 + 匹配意见 |
| `GET /api/wealth/redemption-quote` | 新增：按全部/份额/金额试算，返回到账时间、快赎、大额预警、是否可赎 |
| `POST /api/wealth/acknowledgements` | 新增：保存四件套阅读时长、抄录哈希、分红方式、偏好、账户；返回 `ack_id` |
| `POST /api/risk-assessment` | 改为每日 2 次、12 个月 8 次；新增 `confirm_change`、`needs_reconfirmation`、事件表；返回新 `retake` |
| `POST /api/submit` | 赎回计划支持 `share_bp`；返回 `redemption_prefill`（如命中持仓） |
| `POST /api/verify/request` / `POST /api/confirm` | 契约不变；`wealth.redeem` 升为红色后复用同一挑战 |

## 11. 前端结构

- [page.py](file:///d:/ai-banking/ai-banking-copilot/src/ai_banking_copilot/web/page.py#L346-L385) 理财块新增：
  - `<dialog id="wealth-product-detail">`
  - `<dialog id="wealth-trade">`
  - `<script src="/assets/wealth_trade.js" defer>`
- 新增 `web/assets/wealth_trade.js`：
  - `openWealthTradeSheet({mode: "subscribe"|"redeem", product, prefill})`
  - 四步：产品与适当性 → 交易信息 → 文件与声明 → 验证与确认
  - 共用 `post()` / `esc()` / `yuan()`；不复制 app.js 的状态机
- [app.js](file:///d:/ai-banking/ai-banking-copilot/src/ai_banking_copilot/web/assets/app.js) 理财块：
  - 产品卡改为只展示 + “产品详情” + “立即申购”
  - 持仓卡改为只展示 + “赎回”
  - 等级条按新 `retake` 渲染
  - 财富计划卡 CTA 改为“打开交易确认”
  - 处理 `redemption_prefill` 自动打开赎回弹窗
- `workspace.css` 新增弹窗、步骤条、文件阅读、倒计时、警告条样式。
- `sw.js` 缓存版本号递增，避免旧 `app.js` 缓存。

## 12. 合规规则对照

| 规则 | 实现 | 证据 |
|---|---|---|
| 单日测评 ≤ 2 次 | `risk_assessment_attempts` + 429 | 新增测试 |
| 12 个月 ≤ 8 次 | `risk_assessment_events` 统计 365 天 | 新增测试 |
| 有效期 12 个月 | 365 天 + `days_until_expiry` | 既有 `state_on` + 新字段 |
| 结果不一致再确认 | `needs_reconfirmation` + `confirm_change` | 新增测试 |
| 发行/销售评级孰高 | `display_risk_level = max` | 新增测试 |
| 65+ 特别提示 | `birth_year` + 15 秒倒计时 | 新增测试 |
| 不匹配不硬推 | 后端 `eligible=false` + 前端三路径 | 页面契约测试 |
| 四件套阅读 | ack 接口校验阅读时长 | 新增测试 |
| 风险揭示抄录 | 文本完全匹配 + 哈希落库 | 新增测试 |
| 交易验证 | 复用 `/api/verify/request` | 既有测试 + 新增赎回红色测试 |
| 二次确认 | 弹窗最终摘要页 | 页面契约测试 |
| 大额赎回预警 | quote 返回 `large_redemption` | 新增测试 |
| 快赎识别 | quote 返回 `fast_redeem` | 新增测试 |
| 无静默执行 | 仍走 PEP；新增文案守卫测试 | 新增测试 |

## 13. 测试与门禁

新增测试：

- `tests/gateway/test_wealth_disclosure.py`：披露完整性、孰高、缺失费率不可售。
- `tests/gateway/test_wealth_shares.py`：申购写份额、按份额/金额/全部赎回、余额与持仓校验。
- `tests/web/test_risk_retake_regulatory.py`：2/日、8/12 月、429、下次可测日、结果变更再确认、未确认不落库。
- `tests/web/test_wealth_overview.py`：新增字段、`suitability`、过期提醒、不可买原因码。
- `tests/web/test_wealth_trade_ack.py`：四件套、阅读时长、抄录不匹配拒绝。
- `tests/web/test_wealth_redemption_quote.py`：全部/份额/金额、到账、快赎、大额、封闭不可赎。
- `tests/web/test_triage_wealth.py`：新增问答、赎回反查、不匹配双路径、无执行宣称。
- `tests/web/test_wealth_page_contract.py`：新 DOM id、脚本、按钮文案。

门禁（用户明确要求时才跑）：

```bash
ruff check .
mypy
pytest
python scripts/check_served_page.py
python scripts/e2e_check.py
python scripts/check_design_sync.py
```

## 14. 实施顺序

**P0（合规与资损）**

1. 测评 2/日、8/12 月、12 个月有效期、30 天提醒、结果不一致再确认。
2. 申购/赎回统一确认弹窗骨架：四件套、抄录、验证码、二次确认。
3. 赎回改份额：持仓表增列、核心银行按份额赎回、按金额反算。
4. 金额/份额实时校验、余额与持仓上限。
5. AI 与交易分层：计划卡 CTA 打开弹窗；赎回反查持仓预填；无静默执行。

**P1（体验与披露）**

6. 按产品类型增减字段、差异化流动性标签。
7. 到账时间预估、快赎识别、大额赎回预警。
8. 不可买三路径、产品登记编码展示、费率三项展示。
9. 风险等级孰高展示、推荐理由补全。

**P2（体验优化）**

10. 声明抄录交互、首次购买强制阅读、R4+ 双录提示、65+ 提示与倒计时。

## 15. 已知边界与诚实性口径

- 产品、净值、登记编码、费率均为演示数据；登记编码不是官方编码。
- 净值和份额按模拟序列折算，不是实时行情；实际以 T 日净值为准。
- 交易验证复用服务端一次性验证码；未接真实交易密码、生物识别。
- 双录、人工回访、外呼均未接入；界面只做提示，不宣称已执行。
- 自动赎回/消费抵扣只记录偏好，不执行。
- 测评 12 个月按 365 天实现；界面标注口径。
- 65+ 路径用测试数据覆盖，演示用户不触发。
- 四件套是演示文本，不是正式法律文件。
- 人工复核未接入；结果差异 ≥ 2 档时用冷静期替代，并如实标注。

## 16. 变更申请卡（共享文件）

```
改什么：web/assets/app.js（理财块）、web/assets/wealth_trade.js（新增）、web/page.py（理财块 + 脚本）、
        web/assets/workspace.css、web/assets/sw.js（缓存版本）、web/app.py（理财路由）、
        web/session.py（赎回反查 + 问答）、web/triage.py（理财澄清）、web/present.py（份额事实行）、
        scenarios/wealth.py（披露/匹配意见/份额）、gateway/core_bank.py（份额赎回）、
        gateway/store.py（持仓增列迁移入口）、gateway/risk_store.py（事件表 + 2/8 次）、
        gateway/wealth_disclosure_store.py（新增）、gateway/wealth_trade_store.py（新增）、
        gateway/customer_profile_store.py（新增）、tools/catalog.py（share_bp 可选参数）、
        planning/slots.py（shares 槽 + 提示）、planning/llm.py（赎回份额口述）、
        policy/pdp.py（wealth.redeem 升红，只加严）、compose.py + eval/pipeline.py（初始化新表）
为什么：清掉五模块整改项，补齐适当性合规与交易确认面，不降低任何拦截等级
影响谁：理财场景主、规划段（shares 槽）、前端页面、政策段（赎回升红）
怎么回滚：按文件 revert；新表惰性建表，回滚后残留空表不影响运行；持仓新列回滚后保留但不再读取
要不要结对：planning/slots.py + policy/pdp.py + gateway/core_bank.py 建议结对复核
```

## 17. 实施状态（2026-11-02）

已落地：

- 披露表、交易留痕表、客户出生年份表、测评事件表与种子数据；
- 持仓份额列与核心银行按份额申购/赎回；
- 测评 2 次/日、8 次/12 个月、365 天有效期、结果不一致再确认、30 天过期提醒；
- 产品目录披露、孰高评级、匹配意见、不可买原因码；
- 产品详情、赎回试算、交易确认留痕接口；
- AI 费率/到账/为什么不能买/测评次数/登记编码/封闭期/分红问答；
- 赎回意图按“到期”反查持仓并返回 `redemption_prefill`，聊天内自动打开预填赎回弹窗；
- 前端产品卡只展示、持仓卡只展示、等级条新文案、统一交易确认弹窗、二次确认、验证码、
  大额赎回预警、快赎识别、65+ 提示、R4+ 双录提示；
- `wealth.redeem` 升为红色（只加严）。

验证：

- `ruff check`：通过；
- `mypy`（改动模块）：通过；
- `node --check`（app.js / wealth_trade.js）：通过；
- 浏览器验收：理财页卡片无内联金额框，点击“立即申购”打开交易确认弹窗，四份文件与验证码步骤可见；
- 后端接口冒烟：overview / detail / quote / ack / submit / confirm / 赎回反查均返回预期结构。

**按用户要求未运行 pytest**；新增/更新的测试文件已写好，待用户指示后再执行。

已知残留：

- 演示库中 `wp_bond` 的买入净值早于 90 天模拟序列，份额按序列首日 1.0000 折算；
- 65+ 路径用测试数据覆盖，演示用户（1990 年生）不触发；
- 双录、人工回访、真实短信、真实支付均未接入，界面已如实标注。

# 账单分析可信度重构设计（P0–P2）

日期：2026-11-02
上游：用户截图与逐条问题清单（账单分析页 + 对应 AI 问答）
状态：待用户确认后实施

## 0. 一句话目标

把「告警列表」改回「账单分析」：先讲清收支结构、分类、商户与预算，
再用高置信、有依据、可闭环的线索补充异常识别；页面与 AI 问答共用同一套事实口径。

## 1. 现状问题归因

| 现象 | 根因 |
|---|---|
| 本期支出 ¥18,719.80，趋势最后一点 ¥18,119.80 | 结论区用 `report.transactions` 求和，趋势用 `gateway.monthly_bill`，两个来源与过滤条件不同 |
| 折线最后一点显示成「¥18,1?2」 | `lineChartHtml` 的端点标签以 `text-anchor=middle` 画在 `width - padR`，超出 `viewBox` 被裁切 |
| 同一笔 ¥6,880 生成「异常大额 + 异地交易」两条 | 不同规则各自产出 `Finding`，页面按 finding 逐条渲染，没有按交易聚合 |
| ¥1,200 / ¥1,080 转账被当成异常消费 | 识别窗口只按 `direction=out` 过滤，没有剔除「转账」等非消费分类 |
| 5 条线索全部 45%–60%，却把两条放「重点核对」 | 前端只按影响金额排序，没有置信度分层；也没有按交易去重 |
| 核对完没有任何记录 | 线索状态只存在于一次请求内存，没有落库与回写接口 |
| 「隐藏费用」 | 术语与规则都不对，真实业务对象是周期性自动续费 |
| 分类只有一级「购物 46%」「转账 12%」 | 分类只来自账本 `category` 字段，没有 MCC/二级类目，也没有用户修正入口 |
| 预算保存后无反馈 | 只有总额输入与一次 POST，没有进度、剩余、分类与超支高亮 |
| 只有支出没有收入/净现金流 | 汇总只取负向流水，趋势只画 `total_expense` |
| 「--- 年 -- 月」「本月 + 截至 10-31」 | 原生 `input[type=month]` 空值文案 + 当月仍渲染截止日 |
| 三段免责散落正文、右上角「去对话办这一笔」 | 共享场景页头与旧文案未按场景隔离 |

## 2. 数据真相模型（P0 的核心）

### 2.1 唯一消费口径

新增 `scenarios/bill_categories.py`，集中定义：

- `NON_CONSUMPTION_CATEGORIES = {"转账", "信用卡还款", "还款", "内部转账", "理财"}`
- `is_consumption(txn) -> bool`：`direction == "out"` 且分类不在非消费集合。
- MCC/一级/二级分类演示映射（商户关键词 → `(mcc, mcc_label, l1, l2)`）。
  当前仓库没有真实 MCC 字段，映射明确标注「演示口径」。

`bill_findings.py` 中所有账单统计都走同一套 `_collect_charges()`：

1. 只收 `direction=out`；
2. 剔除 `is_consumption=false`（转账、信用卡还款、内部转账、理财）；
3. 应用用户分类修正覆盖（按 `merchant_key`）。

由此：

- `summary.expense_total_cents` = 消费明细合计；
- `category_share` 合计 = `summary.expense_total_cents`；
- `trend[-1].expense_cents` = `summary.expense_total_cents`；
- `profile`、`top_merchants`、`mom`、预算进度全部基于同一集合。

转账不进消费分析，但**单独汇总**：

```
transfer_out_cents / transfer_count
```

页面显示「另有转账 N 笔 ¥X，不计入消费分析」。

### 2.2 收支与净现金流

新增服务端字段：

| 字段 | 定义 |
|---|---|
| `income_cents` | 本期 `direction=in` 合计（工资、转入等） |
| `consumption_cents` | 本期消费合计（= 原 `expense_total_cents`） |
| `transfer_out_cents` | 本期转账转出合计 |
| `net_cashflow_cents` | `income_cents - consumption_cents - transfer_out_cents` |
| `consumption_count` / `transfer_count` | 对应笔数 |

趋势点同时返回 `expense_cents` 与 `income_cents`，前端画收支两条线。

### 2.3 趋势与环比归因

`expense_trend()` 改为基于同一消费集合，每个点返回：

```json
{
  "period": "2026-10",
  "expense_cents": 1871980,
  "income_cents": 1850000,
  "categories": [{"category": "购物", "expense_cents": 680000}],
  "mom": {
    "previous_period": "2026-09",
    "change_cents": 258000,
    "change_pct": "16.0",
    "contributions": [{"category": "购物", "change_cents": 300000}]
  }
}
```

`mom` 由服务端计算，前端只渲染。首月无前序账期时 `mom=null`。

## 3. 识别线索引擎（P1）

### 3.1 保留确定性引擎，新增展示层聚合

不重写 `Finding` 模型（保留审计、证据、叙事校验能力），新增：

```python
@dataclass(frozen=True)
class Lead:
    lead_id: str              # lead-{主交易 txn_id}，同一交易稳定
    period: str
    kinds: tuple[str, ...]    # 同一交易的多重命中
    labels: tuple[str, ...]
    status: str
    confidence: Decimal       # 取该交易所有命中中的最高值
    tier: str                 # key / more / hidden
    txn_ids: tuple[str, ...]
    evidence: tuple[FindingEvidence, ...]
    impact_cents: int         # 同一交易只取一次，不累加
    basis: tuple[dict, ...]   # 每条命中的「为什么」
    suggestion: str
```

聚合规则：

- 按「主交易」分组（`finding.evidence[-1].txn_id`）；
- 同一交易的多条命中合并为一条 Lead，展示多个标签；
- `status=normal` 的订阅周期核验不进入 Lead（单独「订阅/自动续费」区）；
- `impact_cents` 取该交易金额，不把多条命中的金额相加。

### 3.2 置信度分层

| 层级 | 置信度 | 展示 |
|---|---|---|
| `key` | ≥ 0.70 | 「重点核对」 |
| `more` | 0.50–0.70 | 「更多线索」折叠 |
| `hidden` | < 0.50 | API 保留 `tier=hidden`，页面不展示、不计数 |

`summary.pending_lead_count` 只统计 `key + more`，且按 Lead 去重。
另有 `summary.key_lead_count` 用于「其中高置信 N 条」。

### 3.3 每条线索必须能回答「为什么」

每条 Lead 的 `basis` 包含结构化字段，而不是一句「疑似」：

```json
{
  "kind": "abnormal-spend",
  "rule": "本期单笔 ≥ 同分类其他商户中位数的 3 倍",
  "threshold": "≥ ¥1,000 且 ≥ 3 倍中位数",
  "baseline": "同分类其他商户近 12 个月中位 ¥860.00",
  "sample": "样本 23 笔",
  "detail": "未知商户 9921 本期 ¥6,880.00"
}
```

### 3.4 规则修正

1. **剔除转账**：在 `_collect_charges` 层完成，转账不参与任何消费规则。
2. **同笔去重**：展示层聚合；`Finding` 仍保留，供审计与 AI 叙事。
3. **异地加时间上下文**：
   - 单笔异地本身不再进入 `key`；
   - 仅当「异地 + 深夜」或「异地 + 金额 ≥ 异常大额阈值」时置信度提到 0.75；
   - 依据里写明「异地单独不构成风险，需与时间/金额组合」。
4. **隐藏费用 → 自动续费**：
   - `kind` 改为 `auto-renewal`，文案「自动续费」；
   - 规则：同商户、≥3 次扣款、固定周期（25–35 天或 350–380 天）、金额波动 ≤5%、未在订阅登记；
   - 依据展示：商户、周期、最近三次金额、下次预计扣款日；
   - 出口：「管理自动续费」跳到订阅页；未登记商户给「问 AI / 忽略」。

### 3.5 核对闭环

新增 `web/bill_analysis_store.py`（SQLite + 内存实现），表：

```sql
bill_lead_states(
  user_id, period, lead_id, state, note, updated_at,
  PRIMARY KEY(user_id, period, lead_id)
)
```

`state ∈ {normal, abnormal, ignored}`。接口：

| 方法 | 作用 |
|---|---|
| `GET /api/bills/analysis` | 每条 Lead 带 `state`；已核对的不进 `key/more` |
| `POST /api/bills/leads/{lead_id}` | `{period, state, note?}`，写入核对结果 |
| `POST /api/bills/lead-resolution` | 对话回写：由前端带 `lead_context` + 用户原话，服务端按确定性短语规则落库并返回处理结果 |

页面每条线索提供：`确认正常 / 标记异常 / 忽略 / 查看交易详情 / 问 AI`。
「已核对」只在折叠区显示数量，不再混入待核对。

## 4. 分类与商户（P1/P2）

### 4.1 二级分类

- 一级：餐饮、购物、交通、居住、生活缴费、娱乐、其他（`转账` 等非消费不出现）。
- 二级：外卖、堂食、零食饮品、日用百货、数码、服饰、生鲜、地铁公交、网约车、
  房租、水电燃气、物业、通讯、影音会员、电影演出、游戏等。
- 分类来源：用户修正 > MCC 演示映射 > 账本一级分类兜底。
- 「分类占比」环形图画二级，图例带一级作为副标签。

### 4.2 手动修正与记忆

- 明细/Top 商户行提供「改分类」。
- `POST /api/bills/category-override` `{merchant_key, category}`，落 `bill_category_overrides` 表。
- 下次分析同一商户自动应用。
- 提示语只写「MCC 建议归类：X（演示映射）」，**不编造「XX% 用户归为 Y」**（诚实性红线）。

### 4.3 Top 商户

`top_merchants` 返回前 8 个：

```json
{"merchant_key":"云购物商城","merchant_label":"云购物商城","amount_cents":420000,
 "count":1,"share":"0.22","category":"数码","mcc":"5732"}
```

页面支持「按金额 / 按笔数」切换排序。

## 5. 预算（P1/P2）

继续使用洞察偏好存储（`insight_preferences`），扩展：

- `budget_cents`：总额；
- `category_budgets_json`：分类预算 `{"餐饮": 150000, ...}`（一级分类）。

分析接口返回：

```json
"budget": {
  "month": "2026-10",
  "total_cents": 800000,
  "spent_cents": 640000,
  "remaining_cents": 160000,
  "daily_available_cents": 16000,
  "days_left": 10,
  "over_budget": false,
  "needs_setup": false,
  "categories": [
    {"category":"餐饮","budget_cents":150000,"spent_cents":98000,
     "remaining_cents":52000,"over":false}
  ]
}
```

页面：

- 总额进度条；
- 「已花 / 剩余 / 日均可用」三个数字；
- 分类预算行，超支高亮；
- 未设置预算时，顶部轻提示「本月还未设置预算」，并保留洞察系统的月初 `needs_setup` 提醒。

## 6. 页面与交互（P0/P2）

### 6.1 结构

```
[演示数据黄条]（仅 data_source_is_demo=true）
账期：[本月] [上月] [请选择年月 ▾]
标题：账单分析
状态行：2026-10 · 消费 16 笔 · 转账 2 笔 · 待核对 3 条
[本期消费][本期收入][净现金流][待核对]
[本月预算：进度 / 已花 / 剩余 / 日均可用 / 分类预算]
[收支趋势 + 环比归因] [分类占比]
[Top 商户]
[重点核对（≥70%）]
[更多线索（50%–70%）]
[订阅 / 自动续费]
[账单明细 + 导出 CSV]
数据来源（折叠） + 一句「识别结果供核对，不作最终结论」
```

不再默认折叠全部分析；首屏直接给收支、分类、商户、预算。
「展开完整分析」只保留明细与来源。

### 6.2 演示态隔离

- `data_source_is_demo=true` 时，整页顶部一条黄条：「当前为演示数据，接入真实账单后可用」。
- 生产环境该字段为 false，黄条不渲染。
- 删除正文三段免责，只保留底部一句。
- 右上角按钮按场景配置：账单页改为「问 AI 账单问题」，不再出现「办这一笔」。

### 6.3 账期选择

- 改为 `<select>`，首项「请选择年月」，避免 `--- 年 -- 月`。
- 「本月」选中时不显示「截至 MM-DD」；仅保留「含今日新增 N 笔」（有差值时）。
- 显式月份仍显示该月截止日。

### 6.4 图表

- 折线图修端点裁切：左右留白按标签宽度动态计算，首点 `text-anchor=start`、末点 `end`。
- 收支两条线：消费（主色）与收入（辅助色），图例说明。
- 趋势点可点：点击后下方展示该月分类贡献与环比。
- 环形图二级分类，最多 8 段 + 图例。

### 6.5 导出

前端把 `<details>` 里的本期消费明细导出为 CSV（含 BOM，Excel 直开），
列：日期、时间、商户、一级分类、二级分类、MCC、金额、城市、类型。
演示数据文件头标注「演示数据」。

## 7. 页面 ↔ AI 对话闭环

### 7.1 每条线索的「问 AI」

点击后把上下文带进对话：

```
这笔 2026-10-27 02:17 在深圳「未知商户 9921」的 ¥6,880.00 消费，
历史同分类中位 ¥860.00，系统判断：异常大额 + 异地；
为什么判为可疑？我该怎么核对？
```

同时前端保存 `pendingBillLead = {lead_id, period, merchant, amount, txn_ids}`。

### 7.2 对话回写

`/api/submit` 增加可选 `bill_lead` 字段（只传上下文，不参与银行动作）。
服务端在账单回答后按确定性短语规则处理：

- 含「转给 / 朋友 / 家人 / 本人 / 正常 / 我买的」→ `normal`
- 含「不是我 / 盗刷 / 异常 / 报警 / 冻结」→ `abnormal`
- 含「忽略 / 不用管」→ `ignored`

返回 `bill_lead_resolution`，AI 回答下方显示：

> 已把「未知商户 9921 ¥6,880.00」标记为「确认正常」，之后不再重复提示。

AI 文案明确写「按您的说明记录」，不声称模型做了资金判断。
未命中短语时不写库，只正常回答。

### 7.3 AI 问答卡同步修改

- 删除「可核实节省 / 潜在节省」；
- 指标改为「本期消费 / 本期收入 / 待核对」；
- 线索只列 `key`，最多 3 条，显示置信度与「为什么」摘要；
- 明确「转账 N 笔不计入消费」；
- 保留「查看完整分析」入口。

## 8. 代码落点

| 文件 | 改动 |
|---|---|
| `scenarios/bill_categories.py`（新） | MCC/一级/二级分类、非消费判定、修正覆盖 |
| `scenarios/bill_findings.py` | 统一消费集合、收支/净现金流、趋势环比、Top 商户、Lead 聚合与依据、摘要去「节省」 |
| `scenarios/bill_advice.py` | 建议改用消费口径，转账不计入分类占比 |
| `scenarios/bills.py` | AI 月度账单回答口径（消费/收入/转账分列） |
| `web/bill_analysis_store.py`（新） | 线索状态、分类修正持久化 |
| `web/insights.py` | 分类预算存储与方法 |
| `web/app.py` | 分析响应新字段、线索状态/分类修正/预算接口、对话回写、AppDeps 装配 |
| `compose.py` | 装配 `SQLiteBillAnalysisStore` |
| `web/page.py` | 页面结构、黄条、预算区、Top 商户/趋势归因容器、CSV 按钮 |
| `web/assets/app.js` | 新渲染、分层、去重、闭环按钮、问 AI、CSV、图点击、场景页头文案 |
| `web/assets/workspace.css` | 新组件样式 |
| `docs/superpowers/plans/2026-11-02-bill-analysis-truth-redesign.md` | 实施计划 |

## 9. 测试与验收（按用户要求不运行 pytest）

- 不运行 `pytest`；不因测试而放宽任何断言。
- 可运行：`ruff check`、`mypy`（若环境可用）、`python -m compileall`。
- 页面验收：以 `AIB_DISABLE_LLM=1` 启动，实际打开账单页核对：
  1. 本期消费 = 趋势最后一点；
  2. 转账不出现在线索与分类占比；
  3. 同一交易只有一条 Lead、多个标签；
  4. 置信度 <70% 不进重点核对；
  5. 核对后刷新不再出现；
  6. 预算保存后出现进度/剩余/日均；
  7. 趋势点可看分类贡献；
  8. 演示黄条只在演示数据出现。
- 如用户后续允许，再补跑全量测试。

## 10. 非目标

- 不接真实账单导入、不新增银行写动作、不改 PDP/门禁。
- 不编造 MCC 用户占比、不编造真实商户 MCC；映射明确标注演示口径。
- 不把线索状态写进银行账本；只写独立元数据存储。

# 10-29 实测问题修复设计（0 / 1 / 2 批）

日期：2026-10-31
来源：`实测清单-2026-10-29-尹辰手动版.md` 的 ❗ 项 + `实测截图/` 14 张截图
范围确认：**只做 0 / 1 / 2 批**；场景页打磨（第 4 批）不在本次

---

## 1. 目标与边界

本次完成：

- 修「在其他页面点『新建对话』没反应」（P0）；
- 未识别输入按**闲聊 / 超范围 / 模糊**三类分别回应，不再一律「安全拒绝」；
- 每次回答自带处理详情，不再只保留最后一次链路；
- 执行完成出**回执卡**（记录号 + 跳记录页）；
- 运行链路显示**真实阶段耗时**，不再「理解意图卡住 → 一起变绿」；
- 左栏历史由「对话列表」改成**业务列表**（业务 + 对象 + 金额 + 结果状态），手机端补历史入口；
- 卡片对象显示真实卡名与尾号；挂失卡补连带影响；「查卡片」出卡片列表卡。

本次**不做**：场景页打磨（转账收款人、账单账期/分析卡精简、理财持仓与曲线、卡片页视觉、订阅长句、渠道页并入「我的」、飞书卡片排版）；不新增业务动作；不改 `contracts/`、`policy/audit.py`、`policy/gate.py`、`policy/pep.py` 三条硬约束；不接真实券商/短信。

---

## 2. 问题 → 批次对照

| 清单编号 | 现象 | 批次 |
|---|---|---|
| — | 其他页点「新建对话」无反应 | 0-1 |
| CLR-01 | 「更多办理操作」默认展开 | 0-2 |
| CHAT-03 / CLR-04 / 生活场景截图 | 三句不同性质的话都回「安全拒绝：未知意图」 | 1-1 |
| CHAT-07 | 一次对话办多次，只显示最后一次链路 | 1-2 |
| CLR-05 | 回执无记录号、无跳转入口 | 1-3 |
| CHAT-03 | 运行链路停在「理解意图」再一起变绿 | 1-4 |
| HOME-02 / HOME-03 / APP-01 | 历史里 AI 回复退化成纯文字；手机端无历史入口 | 1-5 |
| CLR-03 | 计划卡显示 `card_credit`；无卡号；挂失无连带影响 | 2-1 / 2-2 |
| 查卡片截图 | 「查我有多少卡片」只回「共 2 张卡」 | 2-3 |

---

## 3. 批次 0 · 阻断项

### 0-1 「新建对话」在非对话页无反应

**根因**：`startFreshConversation()`（`web/assets/app.js:2567`）第一句 `requestStart()`。上一笔办理停在 `awaiting_confirm` 时，`requestStart()`（`app.js:2526`）只做两件事：把待办表达式存进 `state.pendingStart`、把 `#task-guard` 取消 `hidden`。而 `#task-guard` 写在 `#view-chat` 内部（`web/page.py:194`）。用户在记录页/场景页时 `#view-chat` 整体 `hidden`，提示条不可见 → 点击等于没有反应。

**修法**（两层，缺一不可）：

1. `requestStart()` 命中守卫时先 `navigate("chat", {})` 再显示提示条，保证用户看见的是「继续当前办理 / 结束并新建」而不是无声失败；
2. `#task-guard` 从 `#view-chat` 移到 `main` 下的全局层，样式改为固定定位的确认条，任何视图下都能出现。

**回归点**：`CHAT-05`（办理中途点新建）行为不变；`e2e_check.py` 不依赖该节点位置（需跑一次确认）。

### 0-2 「更多办理操作」默认收起

`#task-actions` 里的 `<details class="task-more">` 无 `open` 属性，但会把已有 DOM 节点在任务间搬运（`renderStage()` 每次 `task.appendChild(el("task-actions"))`），浏览器会把用户上一次展开的 `open` 状态带到下一次办理。

**修法**：`beginTask()` 与 `applySubmission()` 生成新计划卡时显式 `el("task-actions").querySelector("details").open = false`。

---

## 4. 批次 1-1 · 未识别输入三分类

### 4.1 口径

| 类别 | 判据 | 回应结构 | 禁止 |
|---|---|---|---|
| `smalltalk` | 问候 / 天气 / 心情 / 感谢 / 闲聊追问 | ≤2 句承接 + 一句说明能办什么 + ≤3 个可点示例 | 冷拒、长篇闲聊、占屏大卡 |
| `out_of_scope` | 命中超范围词表（证券/股票/炒股/期货/外汇/彩票/贷款提额/征信修复/代开卡/查他人账户…） | 说明**为什么不能** + 我能做什么 + 合规提示（仅涉理财时） | 硬拒、越界给建议、承诺收益 |
| `ambiguous` | 有业务信号但低置信（LLM 有被丢弃的低置信意图，或命中生活事件弱信号但槽位不全） | **不猜**，给 2–3 个候选句让用户点 | 猜意图后直接执行 |

三类**都不是安全拒绝**：不显示「已拦截」封条、不计入熔断、审计留痕仍写（沿用现有 `_refuse` 路径）。

### 4.2 实现

新增 `src/ai_banking_copilot/web/triage.py`（web 层，纯文案 + 规则，不产出事实、不碰网关）：

```python
@dataclass(frozen=True)
class TriageSuggestion:
    label: str      # 按钮文案，≤8 字
    text: str       # 点一下填入输入框并提交的原话

@dataclass(frozen=True)
class TriageVerdict:
    kind: str       # smalltalk | out_of_scope | ambiguous
    message: str
    suggestions: tuple[TriageSuggestion, ...] = ()
    disclaimer: str = ""   # 仅 out_of_scope 的理财风险提示

def triage(utterance: str, dropped_actions: Sequence[str], *, today: date) -> TriageVerdict | None: ...
```

`Session._submit` 在 `if not intents:` 处插入（`web/session.py:902`）：

```python
if not intents:
    verdict = triage(utterance, dropped)
    if verdict is not None:
        return self._refuse(verdict.message, utterance,
                            capability_boundary=True, triage=verdict)
    return self._refuse(_with_notes("未知意图：无法匹配任何场景", notes),
                        utterance, capability_boundary=True)
```

`_refuse`（`session.py:1708`）新增关键字参数 `triage: TriageVerdict | None = None`：有 triage 时不加「安全拒绝：」前缀，原文照发；审计与 `capability_boundary` 语义一律不变。

判定顺序：**ambiguous → out_of_scope → smalltalk → 兜底 ambiguous**。顺序理由是「宁可澄清也不要越界回答」；先判 ambiguous 可避免「生日」被当闲聊。

兜底（规则都没命中）：同样走 ambiguous，文案是「没太理解 + 3 个可点示例」，替代当前的「未知意图」。

### 4.3 文案样例（按截图三句）

**① 今天天气不错（smalltalk）**

> 今天天气是挺好的，适合出去走走。我这边是银行的办理助手，能帮您查余额、看账单、转账，也能管卡片和订阅。想先看哪一项？
>
> `查一下余额` `查上月账单` `卡片挂失`

**② 帮我炒股（out_of_scope · 证券）**

> 股票交易要在证券公司开立证券账户，银行不能代您下单，我这边也没有接证券通道。我能做的是行内理财：先做风险测评，再看适合您风险等级的产品，或查现有持仓。
> 理财非存款，产品有风险，投资须谨慎。
>
> `我要做风险测评` `看看我的理财持仓` `查一下余额`

口径说明：**不提「银证转账」**——本仓库 25 个动作里没有该动作，写了就是宣称未实现能力。

**③ 我下过生日（ambiguous）**

> 没太确定您的意思。您是想说「下周生日」，还是「刚过完生日」？如果是想给家人过生日、提前把钱留出来，我可以用「冻结预算 → 下单 → 释放余额」帮您办。
>
> `我下周生日` `给妈妈过生日，预算 800 元` `我只是随便问问`

口径说明：**不提「生日礼遇查询」**——没有该能力。第三个候选「我只是随便问问」点了直接回小闲聊，避免用户被卡住。

### 4.4 前端

- `SubmissionView.triage` → `/api/submit`、`/api/clarify` 的 payload 增加
  `"triage": {"kind": ..., "suggestions": [{"label","text"}], "disclaimer": ...}`；
- `renderStage()` 的 blocked 分支：`state.triage` 存在时渲染中性卡（`.intercept--neutral`，无封条、无红底），消息下方渲染 chips；
- chip 点击 → 填入 `#utterance` 并走现有 `onSubmit`（不新开通道，不绕过任何校验）；
- 时间线写 `已给出能力边界说明`（不用「安全拒绝」措辞）；
- IM/飞书渲染（`web/channels/render.py`）本轮只保证不出现「安全拒绝」字样，卡片按钮后置。

---

## 5. 批次 1-2 · 处理详情随消息

现状：右栏 `#trace` 的审计链 / 执行时间线是**全局单一容器**，新一次回答 `timelineReset()` 后只剩最新一条（CHAT-07）。

改法：

- 每条 assistant 回复在完成时把本次的审计链节点 + 时间线**快照**进该消息内的 `<details class="msg-trace">`，摘要行写「处理详情 · N 条审计 · M 条时间线」；
- 右栏本轮保持「最近一次办理」，标题如实写清是最近一次；「点某条回复让右栏跟着切」列为后续项（价值低于本轮其它项，且需要额外保存每条快照的引用）；
- 历史会话（刷新后）**不伪造阶段**：显示「本次办理的实时链路未随历史保存」+ 记录号 → 「查看记录」；文案明确标注为能力边界，不还原假的时间线。

不做：把链路上行持久化到 `conversation_messages.payload`（工作量在 schema 与 `_record_answer`，收益仅是翻历史好看），列为后续项。

---

## 6. 批次 1-3 · 回执卡

现状：执行完成只出绿色条「计划执行完成（1 步）· transfer」，用户找不到记录号与记录页入口（CLR-05）。

改法：用已有 `_receipt_payload(view)`（`web/app.py:2515` 起）渲染回执卡，卡片字段：

| 字段 | 来源 |
|---|---|
| 业务与金额 | 计划卡最终行 |
| 记录号（可点） | `record_ids[0]`（现成字段） |
| 时间 · 渠道 | 服务端返回 |
| content_hash 溯源 | 现成「数值溯源」折叠，保持 |

按钮：`查看记录`（跳 `#/records/<对应 Tab>`）、`再办一笔`（同业务回填意向，不自动提交）。

历史回执沿用 `receiptSnapshotHtml`，补记录号与跳转。

---

## 7. 批次 1-4 · 运行链路真实耗时

现状：后端 `_submit` 确实在 4 个真实边界 `_notify`（`session.py:841/888/916/922/934`），`ProgressRegistry` 只存「当前阶段 + 更新时间」，前端 500ms 轮询，只能看到「当前停在哪」。模板通道整轮 <100ms，所以一次轮询就把 4 个阶段全点亮（截图 CHAT-03 的观感）。

改法：

- `ProgressState` 增 `stage_timestamps: dict[str, str]`，`advance()` 首次进入某阶段时记 `updated_at`；
- `to_dict()` 输出每阶段 `duration_ms`；
- 前端轮询间隔 500ms → 250ms，阶段行右侧显示耗时（如 `理解意图 · 12ms`）；
- **不补假动画**：一次轮询就全绿的，如实标注「服务端 <250ms 完成」。总耗时也一并显示。

---

## 8. 批次 1-5 · 历史业务列表

### 8.1 左栏（桌面）

采用 `实测截图` 附带的 `history_as_business_list_mockup.html` 设计：

- 每行：`业务名 · 对象 金额` + 右侧结果状态徽标 + 副行「时间 · 记录号/摘要」；
- 分组仍按 今天 / 昨天 / 更早；
- 「新建对话」保持在顶部，点开仍进只读会话。

### 8.2 数据来源

`ConversationSummary`（`web/conversation_store.py:76`）当前只有 `title / message_count / last_active_at`，不足以拼业务行。扩为：

```python
@dataclass(frozen=True)
class ConversationSummary:
    session_id: str
    title: str
    message_count: int
    last_active_at: str
    business: str          # 转账 / 账单 / 理财 / 卡片 / 订阅 / 生活事件 / —（无法归类）
    status: str            # 会话快照 state（awaiting_confirm/interrupted/completed/blocked/failed/reversed/…）
    status_kind: str       # triage:/policy:/ok  —— 决定徽标是「已说明」还是「已拦截」
    ref: str               # 记录号或计划凭证号（拿不到就空）
```

- `business` 取自该会话**最后一条** plan_card 的 action 前缀（`transfer.* → 转账` 等），与 `plan_card._ACTION_LABELS` 同源，不在前端猜；
- `status` 取自会话快照（`SQLiteSessionRepository`，按 session 逐条 `load`，最多 30 条，可接受）；
- 无快照（历史遗留）时 `status = ""`，前端不显示徽标，不编造「已完成」。

状态徽标口径：`待确认`（awaiting_confirm/interrupted）/ `已完成`（completed）/ `待补充`（needs_clarification）/ `已说明`（blocked + triage）/ `已拦截`（blocked + 策略）/ `已冲正`（reversed）/ `失败`（failed）。

### 8.3 手机端历史入口

- 对话页顶栏加「历史」按钮，打开左侧抽屉（复用现有 `is-scenes-open` 抽屉机制），内容同业务列表；
- 顶部首页「最近办理」沿用同一份数据（`renderHomeRecent` 只改标题口径）。

---

## 9. 批次 2 · 计划卡真实性

### 2-1 卡号尾号（网关补演示卡号）

- `gateway/models.py` 的 `Card` 增 `last4: str`（四位数字，演示数据，非真实卡号）；
- `gateway/seed.py` 两张卡各给固定后四位（如 `credit → 9021`、`debit → 4471`），零随机、可复现；
- `web/plan_card._object_label()` 的 `return f"卡片 {card_id}"` 改为
  `f"{卡名} · 尾号 {last4}"`，卡名由 `card_type` 映射（`credit → 招商银行信用卡`、`debit → 招商银行借记卡`，标注为演示）；
- 卡片页、记录页、下拉框统一用同一渲染函数，不再出现 `card_credit`。

### 2-2 挂失的连带影响

不新增动作（本项目 25 个动作封版）。只在挂失计划卡增加「挂失后会发生什么」区块，内容**全部来自网关事实**：

- 该卡当前绑定的订阅代扣（`subscriptions_of` 里 `card_id` 命中的商户与金额）；
- 该卡关联的结算账户尾号（关系未知时如实写「关系未知」，不按 0 计）；
- 补卡路径提示（`card.apply_virtual` 是已有动作，可点）。

### 2-3 「查卡片」出卡片列表卡

`card.list` 的查询结果现在只在 message 里写「共 N 张卡」。改为：查询结果附带结构化 `cards` 列表（卡名、尾号、状态、日限额、绑定账户尾号），前端渲染成卡片列表（与账单分析卡同层级），只读、无操作按钮。

---

## 10. 接口与数据结构变更

| 位置 | 变更 | 兼容性 |
|---|---|---|
| `web/session.py` `SubmissionView` | 新增 `triage: TriageVerdict \| None` | 纯增字段 |
| `web/session.py` `_refuse` | 新增 `triage=` 关键字参数 | 默认行为不变 |
| `web/triage.py` | 新文件 | — |
| `web/app.py` `_submission_payload` | 新增 `triage` 对象 | 纯增字段 |
| `web/progress.py` | `stage_timestamps` + `duration_ms` | 纯增字段 |
| `web/conversation_store.py` | `ConversationSummary` 增 4 字段 | 纯增字段 |
| `gateway/models.py` `Card` | 增 `last4` | 需同步种子与测试 |
| `web/plan_card.py` | 对象标签、挂失连带影响区块 | 润色三元组断言不变 |
| `web/assets/app.js` | 守卫导航、triage 渲染、消息内处理详情、回执卡、业务列表、卡片列表 | 前端 |
| `web/page.py` `workspace.css` | `#task-guard` 提层、新增中性卡样式 | 前端 |

---

## 11. 变更申请卡

```
改什么：web/assets/app.js（守卫/渲染/历史列表）、web/app.py（payload 字段）、
        gateway/models.py + gateway/seed.py（Card.last4）、web/page.py + workspace.css（提示条提层）
为什么：修 P0「新建对话无反应」；三分类回应；回执可追溯；历史改业务列表；卡号真实化
影响谁：前端（尹辰）、卡片场景（林晓研，Card.last4 与其卡片页共用一个渲染函数）、
        运维（种子数据多一列，本地 demo 库需重建或跑迁移）
怎么回滚：全部为增量字段与前端块，按文件 revert 即可；种子重建脚本可重跑
要不要结对：gateway/models.py + seed.py 建议与林晓研结对过一次
```

---

## 12. 测试与门禁

新增测试：

- `tests/web/test_triage.py`：三类判据各 3 条 + 兜底 + 「不改变 state / 不计熔断」断言；口径「三句截图原话」必须命中对应类别；
- `tests/web/test_session_guard_nav.py`：非对话视图下 `requestStart` 命中时视图切回 chat（前端行为，改用 `scripts/e2e_check.py` 场景覆盖）；
- `tests/gateway/test_seed.py`：`Card.last4` 四位数字、两卡不重复、与卡种对应；
- `tests/web/test_plan_card.py`：`card.report_lost` 的对象标签为「卡名 · 尾号」，不含 `card_credit`；
- `tests/web/test_conversation_summary.py`：`business/status/ref` 三类样例，无快照时为空而非编造。

门禁（全绿才收工）：`ruff check .`、`mypy`、`pytest`、`python scripts/check_served_page.py`、`python scripts/e2e_check.py`、`python scripts/check_design_sync.py`。

---

## 13. 已知边界（交付时如实写）

- 历史会话的实时链路不持久化，刷新后只显示记录号，不还原假时间线；
- 三分类是**确定性规则**，不是模型分类；规则未覆盖的说法一律落「兜底澄清」，不会被当成风控拒绝；
- `Card.last4` 是演示数据，不是真实卡号，界面标注「演示」；
- 挂失「连带影响」是按当前事实推演的说明，不会真的去停商户代扣（没有商户接口）；
- IM 渠道本轮只改文案，卡片按钮后置。

---

## 14. 执行顺序

1. 0-1 → 0-2（当天可复测）
2. 1-1（后端 triage + 前端中性卡）→ 1-4（进度耗时）
3. 1-3（回执卡）→ 1-2（消息内处理详情）
4. 1-5（业务列表，后端 summary 先行）
5. 2-1 → 2-2 → 2-3（网关 `last4` 先落地，其余跟着用）

每批结束跑一次全量门禁；P0 修完先回「编号 + 改了什么 + 复测步骤」。

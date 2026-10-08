# 11 · P2 场景覆盖：实施规格 + 四人并行计划

> **用途**：把 `docs/待修复清单.md` 第三节剩余的 5 项 P2 场景缺口做完——作为**四人并行**的
> 统一规格（接口已冻结，各人不必互相等待，也不需要谈判）。
> **依据**：`docs/待修复清单.md` 第七 / 八节、`docs/plans/README.md` 冻结面表与裁决 #25 / #26。
> **建立** 2026-10-08。

> **⚠️ 执行说明（2026-10-08 收尾补记）**：本计划虽以「四人并行」组织，但**实际由单一执行者
> （AI 会话）一次性全部完成**——四条分支（`feat/p2-1-3-6-transfer-line` / `feat/p2-query-eval-cases` /
> `feat/eval-assertions-review` / `feat/p2-5-7-cross-channel`）配四个子代理**并行执行**，各自门禁全绿后
> 合并回 `main`。故本文、`docs/待修复清单.md` 第七节、`docs/变更同步-2026-10-08.md` 中的
> **「甲 / 乙 / 丙 / 丁」应读作「工作流分组与文件归属」，不表示由四位团队成员分别认领与实现**。
> 分工设计本身**已被验证有效**：四条分支确实互不阻塞，合并期只有 3 处「同一条件链上相邻加分支」的
> 文本冲突（取并集即解）与 1 处需按新口径登记的语义冲突（`merchant` 段）。后续若真按人分工，可直接沿用本表。

---

## 一、目标与验收

总目标：把「场景覆盖」组从 3/8 做到 8/8，**不削弱任何既有硬约束**。

| 项 | 验收标准（可机读） |
|---|---|
| **P2-1** 年度账单 | `bills.yearly` 增加**年度同比**（`yoy` 算子，出处 = 两年共 24 条月度账单事实）；计划卡显示年度区间；评测用例覆盖同/环比 |
| **P2-3** 拆分 AA **收款** | 新动作 `transfer.request_collection`：能发起一笔**待收款**并回执；有 NL 规则、有评测用例；与既有 `split_aa()`（按份额**转出**）语义**明确区分** |
| **P2-6** 定时转账派发 | 新动作 `transfer.book`：**登记**未来转账（**登记时不扣款**）；场景层 `due_transfer_plan(gateway, as_of)` 用 `scenarios/scheduling.py` 把**到期**项装配成计划；有单测证明「到期的派发、未到期的不派发」 |
| **P2-5** 商户下单 + 生日联动 | 新动作 `merchant.order` + 生日数据源 `user_profiles(birthday)`；场景层 `birthday_plan(gateway, as_of)` 产出「生日前 2 天」的下单计划；有评测用例 |
| **P2-7** IM / APP 渠道 | `create_app` 的**单会话**改为**按 `session_id` 隔离的多会话**；新增一个最小 IM 渠道适配器（受信任文本进出，**如实声明为能力子集**非等价实现） |
| **乙 · 查询动作用例** | 给「已有 NL 触发规则但**缺评测用例**」的查询动作各补 1–2 条 GREEN 用例：`card.list` / `subscription.list` / `wealth.list` / `account.balance`；复核 README「能力子集」清单是否仍准确 |
| **丙 · 评测断言与口径复核** | `derived-*` 与 `new_user_day1` 段加厚；给 `tests/eval/*` **加**断言（只加严，不削弱）；复核 `eval/invariants.py` 非退化守卫与 metrics 阈值仍成立；**`heldout.json` 不动** |

> **跨分支依赖已规避**：`bills-*` 段的**全部**新增用例（含年度同比）归**甲**，与他的动作改动在
> 同一分支内自洽；`cards-*` / `subs-*` / `wealth-*` / `account-*` 段归**乙**；
> `derived-*` / `new_user_day1` 段归**丙**；`cross-*` 与 `merchant-*` 段归**丁**。
> 这样四人对 `eval/data/cases.json` 的编辑落在**不同段**，且**各自分支都能独立跑绿**。

**共同验收**：每人在自己分支上 `ruff` / `mypy` / `pytest` **全绿**；服务类门禁（`check_served_page`
/ `e2e_check` / `check_design_sync`）由**集成人**在合并后统一跑（避免四人抢 8000 端口）。

---

## 二、冻结接口（开工前已定；**不得改名、不得改签名**）

### 2.1 新增/加固的动作（`tools/catalog.py`，**各自域那一段**）

| 动作 | 域 | `kind` | `RiskClass` | `reversible` | `money_param` |
|---|---|---|---|---|---|
| `bills.yearly`（已落码，本计划只加固） | `bills` | `QUERY` | `READ_ONLY` | `True` | `None` |
| `transfer.request_collection` | `transfer` | `STATE` | `SMALL_CHANGE` | `True` | `None` |
| `transfer.book` | `transfer` | `STATE` | `LARGE_FUNDS` | `True` | `amount_cents` |
| `merchant.order` | `merchant`（**新域**） | `OUTFLOW` | `LARGE_FUNDS` | `False` | `amount_cents` |

**风险口径的依据与边界（只加严、不放宽）**：

- 任何**非纯查询**动作在 PDP 下天然 ≥ 黄（绿色只含纯查询），故「至少黄」**自动成立**，不需要新判据；
- `merchant.order` 走 `LARGE_FUNDS` ⇒ 由**金额档**定黄/红（≤1000 元 黄、>1000 元 红），
  满足「至少黄、**若涉扣款应红**」——**这是工作假设**：官方表未入仓库；若日后拿到官方表且更严，
  只允许**加严**（进 `policy/pdp.py::OFFICIAL_RED_ACTIONS`），**不得放宽**；
- `transfer.book` 是**未来**流出，`kind=STATE` 表示「登记时不产生资金流」——**登记本身不扣款**，
  真正扣款发生在派发后走 `transfer.create` 的既有链路（见 2.4）。

### 2.2 参数（`ParamSpec`，含 `slot_name`）

```
transfer.request_collection: user_id(CONTROL,str,slot_name=user_id) · payer_id(CONTROL,str,slot_name=payer)
                             · amount_cents(CONTROL,int,slot_name=amount) · note(DATA,str,可选)
transfer.book:               user_id(CONTROL,str,slot_name=user_id) · account_id(CONTROL,str,slot_name=account)
                             · payee_id(CONTROL,str,slot_name=payee) · amount_cents(CONTROL,int,slot_name=amount)
                             · booked_on(CONTROL,date,slot_name=when) · remark(DATA,str,可选)
merchant.order:              user_id(CONTROL,str,slot_name=user_id) · merchant(CONTROL,str,slot_name=merchant)
                             · amount_cents(CONTROL,int,slot_name=amount) · order_on(CONTROL,date,slot_name=when)
```

> `payer` 是**新槽位名** ⇒ 需要 `planning/slots.py` 的 `_PROMPTS` 加一条（`SLOT_PARAMS` 已派生，**不必手改**）。

### 2.3 新增写原语（`gateway/core_bank.py`，**各加自己那一块**）

```
request_collection(*, user_id, payer_id, amount: Money, note: str, idempotency_key) -> WriteReceipt
book_transfer(*, user_id, account_id, payee_id, amount: Money, booked_on: date, remark: str,
              idempotency_key) -> WriteReceipt
place_merchant_order(*, user_id, merchant, amount: Money, order_on: date, idempotency_key) -> WriteReceipt
```

纪律不变：**幂等**（`write_receipts` 命中即回放）、**全或无**（单事务）、**确定性 id**、**不做授权判断**。

### 2.4 新增独立表（**禁止改 `gateway/store.py::SCHEMA_SQL`**）

按 `gateway/risk_store.py` 先例：各自模块自带 `*_SCHEMA_SQL` + `init_*_schema(store)`，幂等。

| 表 | 属主 | 列（最小集） |
|---|---|---|
| `collection_requests` | 甲 | `request_id` PK · `user_id` · `payer_id` · `amount_cents` · `note` · `status` · `created_on` |
| `scheduled_transfers` | 甲 | `schedule_id` PK · `user_id` · `account_id` · `payee_id` · `amount_cents` · `booked_on` · `status` |
| `merchant_orders` | 丁 | `order_id` PK · `user_id` · `merchant` · `amount_cents` · `order_on` · `status` |
| `user_profiles` | 丁 | `user_id` PK · `birthday`（`MM-DD`，由丁的模块播种演示值） |

> ⚠️ **硬性前置：新表必须在出处回读白名单里注册**，否则写动作的**执行后出处校验**（H1）必红：
> `policy/pep.py::_read_hash` 与 `gateway/facts.py::_hash_by_pk` 各加一个分支。
> 这两处是**加分支**（additive），不是改签名；**不得**动既有六张表的读法。

### 2.5 公共机制（已存在，直接用）

`scenarios/scheduling.py`：`REFERENCE_DATE` · `due_date(base, lead_days=N)` · `is_due(target, as_of=)` ·
`ScheduledItem(key, due_on, action, params)` · `dispatch_due(items, as_of=)` · `plan_due(items, as_of=, ledger=)`。

**P2-6 与 P2-5 一律调用它**（`dispatch_due` / `plan_due`），**不得各写一套**；基准时钟**只许**
`REFERENCE_DATE` / `DEFAULT_BOOKING_DATE`。

---

## 三、硬约束与禁改（每人 PR 自查）

- **三条硬约束**：无裁决不执行 · 绿色只允许纯查询 · 熔断只加严不降级；相关断言**一字不动**。
- **禁改**：`contracts/` · `policy/audit.py` 的哈希算法 · `policy/gate.py` 的判据 ·
  `gateway/store.py::SCHEMA_SQL` · `eval/data/heldout.json` · 既有测试的**语义断言**
  （可以补测试，不可以放宽）。
- **事实零生成**：数值 / 实体只能来自 `gateway`；派生数字**只走算子白名单**（`gateway/operators.py`）。
- **控制槽零信任**：`untrusted` 永不进控制槽；用户文本只入数据槽。
- **诚实性**：未实现的能力一律**声明为能力子集**；不得在界面 / 文档 / README 宣称未实现的能力。
- **H2**：`web/` 不得直触 `CoreBank`（写只经 `Pep`，补偿经 `ReversalService`）。
- **新增动作的改动面**：见 `AGENTS.md` 第 2 节（catalog 自己域那段 → 自己场景模块的 handler →
  `core_bank` / `pdp` 自己那块 → 评测用例；`policy/pep.py` **不需要改**）。

---

## 四、四条工作流

> 分支命名遵循 `AGENTS.md` 第 7 节；**各人只在自己 worktree 里动自己那段**。
> 计数断言已是**下限**，新增动作**不需要**改任何 `== 20`。

### 甲 · `feat/p2-1-3-6-transfer-line` → `wt-jia`

| 任务 | 落点 |
|---|---|
| P2-1 年度同比 | `scenarios/bills.py::_yearly_bill` 增加年度同比：读**本年 + 上年**各 12 条月度账单事实，用 `run_operator("yoy", ((本年合计, 上年合计)))`；`web/plan_card.py` 加年度区间标签；评测用例加进 `eval/data/cases.json` 的 `bills-*` 段（**该段全部归你**） |
| P2-3 AA 收款 | `tools/catalog.py::_TRANSFER_SPECS` + `gateway/core_bank.py::request_collection` + 新表 `collection_requests`（自带 DDL + 幂等 init）+ `scenarios/transfer.py` 的 `WRITE_HANDLERS` + NL 规则 + `transfer-*` 段评测用例 + README 口径 |
| P2-6 定时派发 | 新动作 `transfer.book` + 新表 `scheduled_transfers` + `scenarios/transfer.py::due_transfer_plan(gateway, as_of)`（用 `scheduling.plan_due`）+ 单测 |
| 出处回读 | `policy/pep.py::_read_hash` 与 `gateway/facts.py::_hash_by_pk` 各加 2 个分支（两张新表），**只加分支** |
| 断言同步 | `tests/tools/test_catalog.py` / `tests/planning/test_slots.py` 的集合断言按字母序追加 |

**不要**：改 `transfer.schedule` 的语义（会让 `test_transfer_schedule_reuses_transfer_semantics` 红）；
改 `split_aa()` 的语义（它是「按份额转出」，保留原义）。

### 乙 · `feat/p2-query-eval-cases` → `wt-yi`

| 任务 | 落点 |
|---|---|
| 查询动作用例 | `eval/data/cases.json` 的 `cards-*` / `subs-*` / `wealth-*` / `account-*` 段：给 `card.list` / `subscription.list` / `wealth.list` / `account.balance` 各补 1–2 条 **GREEN** 用例（这些动作已有 NL 规则但缺用例） |
| 口径复核 | 复核 README「能力子集」清单与 `docs/待修复清单.md` P2-8 结论是否仍与实现一致（**只报告，不改 README**——README 是甲的独占文件） |

**不要**：新增动作；改 `scenarios/*` 的既有语义；动 `bills-*` / `derived-*` / `cross-*` 段。

### 丙 · `feat/eval-assertions-review` → `wt-bing`

| 任务 | 落点 |
|---|---|
| 用例加厚 | `eval/data/cases.json` 的 `derived-*` 与 `new_user_day1` 段各加 1–2 条（派生 / 新用户首日路径） |
| 断言加严 | `tests/eval/*` 增加断言（如「每条用例的 category 都有对应用例」「级别与 blocked 的组合合法」）；**只加，不削弱** |
| 复核 | `eval/invariants.py` 的非退化守卫与 metrics 阈值（`derived_release_rate ≥ 0.95`、`new_user_false_block_rate == 0`）在用例增多后仍成立；**只在能加严时改** |

**不要**：动 `heldout.json`；改 `eval/pipeline.py` 的既有语义；改生产代码。

### 丁 · `feat/p2-5-7-cross-channel` → `wt-ding`

| 任务 | 落点 |
|---|---|
| P2-5 商户下单 | 新动作 `merchant.order`（**新域** → 新建 `_MERCHANT_SPECS` 并登记进 `_SCENARIO_SPECS` 汇总行）+ `gateway/core_bank.py::place_merchant_order` + 新表 `merchant_orders` + 出处回读分支（`_read_hash` / `_hash_by_pk`） |
| P2-5 生日联动 | 新表 `user_profiles(birthday)`（自带 DDL + 幂等播种演示值）+ `scenarios/cross.py::birthday_plan(gateway, as_of)`（`scheduling.due_date(..., lead_days=2)` + `plan_due`）+ `cross-*` / `merchant-*` 段用例 |
| P2-7 多会话 | `web/app.py`：单 `Session` → 按 `session_id` 隔离（`AppDeps` 字段签名**不变**）+ `tests/web/` 加「两个会话互不串扰」的测试 |
| P2-7 渠道 | 新模块 `web/channels/im.py`：受信任文本进 / 出，走既有 `Session`；**在 README 与文档里如实声明为「能力子集」**（不接真实 IM 平台） |

**不要**：改 `web/session.py` 的既有状态机语义；把渠道写成「真接 IM 平台」；动 `bills-*` 段。

---

## 五、执行与集成

1. **基线**：先提交当前全套改动（含本文件）到本地 `main`（**不推送**）。
2. **并行**：从基线开 4 个 worktree / 分支（上表），4 个子代理各占一个，互不读写对方目录。
3. **各自门禁**：`ruff` / `mypy` / `pytest` 必须在**自己 worktree 内**全绿；
   **不要**起服务（8000 端口属于演示进程），服务类门禁交给集成人。
4. **集成**：按 甲 → 丁 → 乙 → 丙 顺序合并进 `main`（先合共享文件改动多的），
   逐次解决冲突并跑全量 `pytest`；冲突集中在分段共享文件，**不得**为了合并而放宽断言。
5. **收口**：合并后在主树跑**全部门禁**（含 `check_served_page` / `e2e_check` / `check_design_sync`），
   重启服务，逐条更新 `docs/待修复清单.md` 第三节状态与 README 口径。

---

*本规格与 `docs/待修复清单.md` 冲突时，以本文件为准（接口部分）；清单仍是任务台账。*

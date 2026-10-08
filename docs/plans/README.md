# 实施计划（`docs/plans/`）

本目录按模块拆分**可独立交付并验收**的实施计划。每份计划遵循统一约定：

- **TDD**：先写失败测试 → 跑测试确认失败 → 最小实现 → 跑测试确认通过 → 提交
- **验收门槛**：**跑通才算完成**（不接受"写完了但没跑"）
- **任务粒度**：每个 Step 是一个动作（2–5 分钟）
- **无占位符**：不写 TBD / TODO / "稍后补充"

## 计划清单（按依赖顺序）

| 序 | 文件 | 依赖 | 状态 |
|---|---|---|---|
| 00 | `00-foundation.md` | — | **已出** |
| 01 | `01-fact-gateway.md` | 00 | 已出 |
| 02 | `02-tool-registry.md` | 00,01 | 已出 |
| 03 | `03-pdp-pep.md` | 00,01,02 | 已出 |
| 04 | `04-planning-kernel.md` | 00,02 | 已出 |
| 05 | `05-scenarios.md` | 01–04 | 已出 |
| 06 | `06-web-channel.md` | 03,04,05 | 已出 |
| 07 | `07-eval-and-docs.md` | 01–06 | 已出 |

> 依赖表与门禁见 `../roadmap.md`。

---

## 跨计划接口主线（冻结面）

> 本表是**唯一的跨计划接口冻结面**：后续计划只能**依赖**这些符号，不得改名或改签名。
> 需要变更时，先改本表、再回溯修订已出计划的 `Interfaces` 段与实现，最后才动代码。
> 口径依据：PRD §5（核心机制）、§8（技术栈）、§0（硬前提 H1–H10）。

### 包结构

```
src/ai_banking_copilot/
  contracts/     # 00（已冻结）纯类型与纯函数，禁 I/O
  gateway/       # 01 事实网关 TCB + 模拟核心银行 + 确定性解析器 + 算子白名单 + 沙箱
  tools/         # 02 工具白名单注册表 + 动作契约
  policy/        # 03 PDP 三色策略引擎 + PEP + 执行门 + 审计链 + 熔断 + saga/撤销
  planning/      # 04 规划内核（拆解 / 槽位 / DAG / 锚点 / 净效应 / LLM 降级）
  scenarios/     # 05 六场景业务闭环
  web/           # 06 FastAPI + 计划卡 + 中断/回退/接管
  eval/          # 07 评测套件 + 对抗库 + held-out
tests/           # 与 src/ 同构（tests/gateway/、tests/policy/ …）
```

### 00 `contracts`（已冻结）

| 模块 | 冻结符号 |
|---|---|
| `contracts.taint` | `Trust`(TRUSTED/UNVERIFIED) · `SlotKind`(DATA/CONTROL) · `Value[T](raw, trust)` · `Slot[T](kind, value)` · `UntrustedWriteError` |
| `contracts.authorization` | `Level`(GREEN=0/YELLOW=1/RED=2/MANUAL=3/DENY=4) · `ReasonCode`(QUERY_ONLY/STATE_CHANGE_SMALL/AMOUNT_OVER_DAILY_LIMIT/IRREVERSIBLE/MANUAL_REVIEW/OBJECT_BLOCKED) · `Decision(level, reasons)` · `strictest(*decisions)` · `green_or_raise(is_pure_query)` |
| `contracts.plan` | `AnchorSource`(EXPLICIT/USER_APPROVED/DERIVED) · `IntentRef(source, anchor_id)` · `PlanNode(node_id, action, params, intent, depends_on)` · `Plan(plan_id, nodes)` · `validate_plan(plan)` · `cfhash(nodes)` · `PlanValidationError` |
| `contracts.security` | `INJECTION_MARKERS` · `detect_injection(text) -> bool` |

### 01 `gateway`

| 模块 | 冻结符号 |
|---|---|
| `gateway.models` | `Money(cents)`（`+`/`-`/取负/`yuan`）· `Account` · `Payee` · `Card` · `Transaction` · `WealthProduct` · `Subscription` · `Hold` · `CategoryTotal` · `BillSummary` |
| `gateway.errors` | `GatewayError` · `ReadOnlyViolation` · `StoreError` · `GatewayNotFoundError` · `CoreBankError` · `InsufficientFundsError` · `BlockedPayeeError` · `IdempotencyConflictError` · `UnknownAccountError` / `UnknownPayeeError` / `UnknownSubscriptionError` / `UnknownProductError` / `UnknownCardError` · `OperatorError` / `UnknownOperatorError` · `SandboxError` / `SandboxViolation` / `SandboxTimeout` |
| `gateway.store` | `StoreMode`(READ_ONLY/READ_WRITE) · `Store(db_path, mode)`（`query`/`execute`/`transaction`/`close`）· `init_schema(store)` · `SCHEMA_VERSION`（`Store` 跨线程安全，见裁决 #18） |
| `gateway.hashing` | `canonical_json(value) -> str` · `sha256_hex(text) -> str` |
| `gateway.seed` | `seed_database(store)`（**幂等补种**：逐行 `INSERT OR IGNORE`，种子扩充后既有库自动补行、**不覆盖**演示中产生的真实变更；见裁决 #28）· `DEMO_USER_ID` |
| `gateway.facts` | `Record[T](record_id, payload, content_hash)` · `FactGateway(store)`（单条：`account`/`payee`/`card`/`transaction`/`subscription`/`wealth_product`/`hold`；集合：`accounts_of`/`payees_of`/`cards_of`/`subscriptions_of`/`holds_of`；查询：`transactions_between`/`monthly_bill`/`wealth_products`/`daily_transfer_total`/`verify`） |
| `gateway.core_bank` | `CoreBank(store)`（`transfer` / `cancel_subscription` / `freeze_funds` / `unfreeze_funds` / `subscribe_product` / `redeem_product` / `set_card_status`）· `WriteReceipt(idempotency_key, action, status, record_ids, effect_ids, replayed)` · `DEFAULT_BOOKING_DATE` · 异常见 `gateway.errors` |
| `gateway.resolvers` | `Candidate(payee_id, full_name, account_tail, phone_tail, identity_confirmed)` · `PayeeResolution(candidates, needs_clarification, parse_trace)` · `resolve_payees(payees, text)` · `AmountParse(matched, value, evidence)` · `parse_amount(text)` · `TemporalParse(matched, when, negation, evidence)` · `parse_temporal(text, today)` |
| `gateway.operators` | `OperatorSpec(name, arity, description)` · `OPERATOR_WHITELIST` · `OPERATOR_NAMES` · `DerivedValue(operator, value, sources)` · `run_operator(name, inputs)` |
| `gateway.sandbox` | `SandboxRequest(operator, inputs, timeout_s)` · `SandboxResult(operator, value, duration_ms, sources)` · `run_in_sandbox(request)` · `run_probe(probe, timeout_s)` |
| `gateway.sandbox_child` | `install_guards()` · `main() -> int` · `DENIED_MODULES` |
| `gateway.risk_store` | `RISK_SCHEMA_SQL` · `RiskAssessmentRecord(user_id, risk_profile, assessed_on, valid_days)` · `RiskAssessmentSource` Protocol（`assessment_for(user_id) -> object \| None`）· `StoreRiskAssessmentSource(store)` · `init_risk_schema(store)` · `seed_risk_assessment(store)` · `risk_assessment(store, user_id)` · `verify_risk(store, record_id, content_hash)` |

### 02 `tools`

| 模块 | 冻结符号 |
|---|---|
| `tools.registry` | `RiskClass`(READ_ONLY/NOTIFY/SMALL_CHANGE/LARGE_FUNDS/IRREVERSIBLE) · `ParamSpec(name, slot, py_type, required)` · `ToolSpec(action, risk, params, is_pure_query, reversible, money_param, description)` · `ToolCall(action, params)` · `ToolRegistry`（`register` / `get` / `actions` / `validate_params`）· `UnknownActionError` / `ParamValidationError` / `ForbiddenParamError` |
| `tools.catalog` | `build_default_registry() -> ToolRegistry`（**全量 24 个动作**，七域：transfer / bills / wealth / cards / subscriptions / cross / merchant） |

### 03 `policy`

| 模块 | 冻结符号 |
|---|---|
| `policy.pdp` | `ObjectState`(SELF/KNOWN_UNVERIFIED/VERIFIED_SAVED/UNKNOWN/BLOCKED) · `PolicyRequest(user_id, action, amount, daily_accumulated, object_state, is_pure_query, irreversible, daily_limit, context_flags, amount_applies: bool = True)` · `evaluate(request) -> Decision` · `evaluate_plan(plan, net) -> Decision` · `DAILY_LIMIT_YUAN` · `NET_EFFECT_THRESHOLD_YUAN` |
| `policy.gate` | `assert_frozen(plan, frozen_hash) -> None` · `StalePlanError` |
| `policy.pep` | `Pep(registry, gateway, core_bank, audit, breaker, write_handlers, query_handlers)`（`execute(call, decision, plan, idempotency_key)`——`plan` = **已确认的冻结计划**，见 P1-1）· `ExecResult(status, record_ids, message)` · `NotAuthorizedError` · `WriteContext` / `WriteHandler`（**写处理器接缝**，见裁决 #25）· `QueryContext` / `QueryHandler` / `QueryResult`（**读处理器接缝**，见裁决 #26）· `require_str` / `require_int` / `require_date` / `money_cents`（见裁决 #25 · `require_date` 另见裁决 #19） |
| `policy.audit` | `AuditEvent(event_id, kind, payload, prev_hash)` · `AuditLog(path)`（**追加式 JSONL + sha256 哈希链**，不占用 01 的 schema）· `append(event) -> str` / `verify_chain() -> bool` / `events()` · `ChainBrokenError` |
| `policy.breaker` | `BreakerState`(CLOSED/OPEN) · `BreakerTrigger`（五类触发）· `CircuitBreaker(threshold=…, store=None)`（`record_failure` / `record_success` / `is_open` / `lock` / `unlock` / `state` / `triggers` / `failure_count`）· `BreakerSnapshot` / `BreakerStateStore`（**持久化接缝**）· `policy.breaker_store`：`StoreBreakerStateStore` / `init_breaker_schema`（独立表，不并入 `SCHEMA_SQL`）——见裁决 #27 |
| `policy.saga` | `Saga`（`add` / `run` / `compensate`）· `ReversalWindow`（`open` / `reverse` / `expired`）· `CompensationError` |

### 04 `planning`

| 模块 | 冻结符号 |
|---|---|
| `planning.llm` | `IntentProposal(intents, raw)` · `Intent(action, slots, confidence, evidence)` · `IntentModel` Protocol（`propose(utterance, context) -> IntentProposal`）· `TemplateIntentModel`（降级通道）· `LlmIntentModel` · **S3** `_TEMPLATE_RULES`（降级通道规则）新增 5 条（wealth.list / card.unlock / card.lock 带修饰补强 / card.list / subscription.list / account.transactions），**既有 10 条零改动** · **裁决 #28** 再加 2 条（`transfer.request_collection`（`AA 收款|发起收款|让大家还我钱`，首位）与 `bills.yearly`（`年度账单|全年账单|一年的账单|年度消费`，须位于 `bills.monthly` 之前）），**其余规则零改动**；现行共 **20** 条 |
| `planning.anchors` | `AnchorLedger`（`grant_explicit` / `grant_approved` / `derive` / `resolve`）· `DerivedDepthError` |
| `planning.net_effect` | `NetEffect(amount, irreversibles, node_ids)` · `net_effect(plan, gateway) -> NetEffect` |
| `planning.kernel` | `Question(slot, prompt, candidates)` · `PlanningResult(plan, questions, blocked, intents, rationale)` · `Kernel(llm, registry)`（`plan(user_id, utterance, context) -> PlanningResult`） |

### 05 `scenarios`

| 模块 | 冻结符号 |
|---|---|
| `scenarios.base` | `ScenarioOutcome(scenario, status, plan, decision, exec_results, message, record_ids)` · `ScenarioHandler` Protocol（`name` / `matches(intents)` / `build(intents, gateway) -> Plan`） |
| `scenarios.registry` | `SCENARIO_HANDLERS` · `select_handler(intents) -> ScenarioHandler` · `WRITE_HANDLERS`（**写**处理器汇合，见裁决 #25）· `QUERY_HANDLERS`（**读**处理器汇合，见裁决 #26） |
| `scenarios.*` | `transfer` / `bills` / `wealth` / `cards` / `subscriptions` / `cross` 六个 handler 模块 · 新增 scenarios.wealth.to_risk_assessment(record) -> RiskAssessment \| None · 新增 scenarios.authorization.{object_state_for_node, node_policy_request} · **S3** 新增 scenarios.bills.{ANOMALY_THRESHOLD_CENTS, large_transactions, describe_large_transactions} · scenarios.wealth.product_comparison · scenarios.subscriptions.{upcoming_charges, describe_upcoming_charges}；scenarios.wealth.FORBIDDEN_HINTS **加严**（append）；scenarios.bills.BillsHandler.build **新增 account.transactions 分支 + 仅该意图时短路 bills 计算** |

### 06 `web`

| 模块 | 冻结符号 |
|---|---|
| `web.plan_card` | `PlanCard(card_id, frozen_hash, level, lines, requires)` · `render_plan_card(plan, decision, gateway) -> PlanCard` · **S3** `_object_label`/`_when_label` **实现增强**（消费 05 只读 helper 拼接 ②③⑤ 只读事实；**签名与字段零改**；新增 06→05 只读依赖，不生成数字） |
| `web.session` | `Session(deps)`（`submit` / `confirm` / `interrupt` / `resume` / `takeover_package` / `state`）· `SessionState` |
| `web.app` | `AppDeps`（**尾部新增可选字段** `risk: RiskAssessmentSource \| None = None`（`None ⇒ 申购 fail-closed）、`reversal: ReversalService \| None = None`、`operator_token: str \| None = None`（**未配置 ⇒ 运维解锁端点 403**，见裁决 #27））· `create_app(deps) -> FastAPI` · 路由 `POST /api/operator/breaker-unlock`（**唯一**的熔断降级出口，需 `X-Operator-Token`，必留痕） |

### 07 `eval`

| 模块 | 冻结符号 |
|---|---|
| `eval.dataset` | `EvalCase(id, utterance, expect_level, expect_blocked, category)` · `load_cases(path)` · **S3** `eval/data/cases.json` +4 条 GREEN（②③⑤ + card.list）/ `adversarial.json` +3 条 yield_hint blocked；`heldout.json` **不动** |
| `eval.harness` | `run_suite(cases, pipeline) -> EvalReport` · `EvalReport(passed, failed, invariants)` |
| `eval.invariants` | `InvariantResult(name, holds, detail)` · `check_invariants(cases, results)`（I1′–I4′ + 绿色纯度 + 非退化） |
| `eval.pipeline` | `run_suite` / `EvalReport` 签名不变；**新增私有** `_inject_assessment` 与收益暗示守卫（镜像 `scenarios.wealth.FORBIDDEN_HINTS`）；`_DEFAULT_SELECTED["product"]` 由 `wp_bond` 改 `wp_cash`；`_derived_release` 改走受限沙箱 `scenarios.bills.category_share_in_sandbox`（catch 追加 `SandboxError` / `ScenarioRefused`） |

### 规范动作表（**唯一真源**，02/03/04/05/06/07 一律以此为准）

> 2026-10-01 裁决：02 目录与 04 规划词表曾出现动作名分叉（`account.balance` vs `bill.query` 等）。
> 现冻结为下表 **24** 个动作（2026-10-01 为 20 个；2026-10-08 经**裁决 #28** 增补 4 个）；`RiskClass` 由 02 声明，
> `ActionKind` 是 04 净效应 `cf` 语义的唯一依据。03 不依赖具体动作名（只按 `RiskClass` + 金额 + 对象态裁决）。

| # | action | RiskClass（02） | ActionKind（04，cf 语义） | 说明 |
|---|---|---|---|---|
| 1 | `account.balance` | READ_ONLY | QUERY | 余额查询（绿） |
| 2 | `account.transactions` | READ_ONLY | QUERY | 交易记录查询（绿） |
| 3 | `bills.monthly` | READ_ONLY | QUERY | 月度账单分析（绿） |
| 4 | `bills.category_trend` | READ_ONLY | QUERY | 分类趋势（绿） |
| 5 | `bills.yearly` | READ_ONLY | QUERY | 年度账单汇总 + **同期（YTD）同比**（绿） |
| 6 | `wealth.list` | READ_ONLY | QUERY | 理财收益/产品查询（绿） |
| 7 | `card.list` | READ_ONLY | QUERY | 卡片查询（绿） |
| 8 | `subscription.list` | READ_ONLY | QUERY | 订阅查询（绿） |
| 9 | `transfer.create` | LARGE_FUNDS | OUTFLOW | 转账（03 按日累计细化黄/红） |
| 10 | `transfer.schedule` | LARGE_FUNDS | OUTFLOW | 定时转账（未来流出） |
| 11 | `transfer.book` | LARGE_FUNDS | STATE | 登记未来转账（**登记时不扣款**；到期派发为 `transfer.create`） |
| 12 | `transfer.request_collection` | SMALL_CHANGE | STATE | AA **收款**请求（待收款，不产生资金流） |
| 13 | `wealth.subscribe` | LARGE_FUNDS | OUTFLOW | 理财申购（官方红色） |
| 14 | `wealth.redeem` | LARGE_FUNDS | INFLOW | 理财赎回（资金流入） |
| 15 | `subscription.cancel` | SMALL_CHANGE | CANCEL_OUTFLOW | 订阅取消（负未来流出） |
| 16 | `subscription.sign` | SMALL_CHANGE | OUTFLOW | 代扣签约（未来流出） |
| 17 | `card.lock` | SMALL_CHANGE | STATE | 卡片锁定 |
| 18 | `card.unlock` | SMALL_CHANGE | STATE | 卡片解锁 |
| 19 | `card.report_lost` | IRREVERSIBLE | IRREVERSIBLE | 挂失（不可逆，≥红 + 人工接管） |
| 20 | `card.apply_virtual` | SMALL_CHANGE | STATE | 虚拟卡申请（官方黄色） |
| 21 | `card.set_limit` | SMALL_CHANGE | STATE | 额度调整（官方黄色） |
| 22 | `funds.freeze` | SMALL_CHANGE | OUTFLOW | 资金冻结（视同流出） |
| 23 | `funds.release` | SMALL_CHANGE | INFLOW | 解冻（回流） |
| 24 | `merchant.order` | LARGE_FUNDS | OUTFLOW | 商户下单（生日联动；**不可逆流出**，新域 `merchant`） |

### 裁决记录（2026-10-01 复核 02/03/04 后）

1. **动作名分叉**：以本表为唯一真源。02 的 `build_default_registry()` 须注册全部 20 个动作；04 的 `ACTION_KINDS` 须与之逐项一致，并新增一条**交叉断言测试**（`set(ACTION_KINDS) == set(registry.actions())`）。原 04 的 `bill.query` 作废（改用 `bills.monthly`）。
2. **卡片场景补原语**：01 的 `CoreBank` **追加**两个写原语 `apply_virtual_card(*, user_id, daily_limit, idempotency_key)` 与 `set_card_limit(*, card_id, daily_limit, idempotency_key)`（纯新增，不改既有签名），以覆盖官方"卡申请 / 额度调整"。
3. **`validate_params` 签名**：`ToolRegistry.validate_params(call) -> dict[str, object]`，返回**去 taint 包装后的归一值**（控制槽参数若为 `Value`/`Slot` 则取其 `raw`；裸值只允许进数据槽）。
4. **参数槽形态**：控制槽参数必须传 `contracts.taint.Value` 或 `Slot`；裸值进控制槽 → `ForbiddenParamError`（H3 落点）。
5. **异常基类**：`UnknownActionError(KeyError)`、`ParamValidationError(ValueError)`、`ForbiddenParamError(ValueError)`（后两者互不继承）。
6. **执行门粒度**：`policy.gate.assert_frozen(plan, frozen_hash)` 做**计划级**冻结校验（`frozen_hash == cfhash(plan.nodes)`），`policy.pep.manifest_hash(call)` 做**节点级**成员资格校验；两者叠加即 PRD 硬约束 #4 的"节点 ∈ 已冻结确认子图"。06 必须在确认记录里同时保存 `Plan` 与 `cfhash`。
7. **绿色判定位置**：纯查询的绿色由单节点 `policy.pdp.evaluate` 决定；`evaluate_plan` 只给**计划级下限**（`NE=0 ⇒ ≤黄`）。上下文异常 modifier 允许把纯查询**抬到黄**（方向更严，符合 H4）。
8. **`evaluate_plan` 的 net 入参**：用 `NetEffectView` Protocol（`amount: Money`、`irreversibles: tuple[str, ...]`），使 03 **不依赖 04**。
9. **规划侧新增模块**：`planning.slots`、`planning.orchestrator`（04 新增，不改冻结符号）；`planning.orchestrator.StepDriver` 为"每处状态转移必须过统一审计/鉴权插桩点"的落地与兜底实现。
10. **审计载体**：`AuditLog(path)` 追加式 JSONL + sha256 哈希链，不占用 01 的 schema。
11. **01 的否定词表**：`parse_temporal` 的 `_NEGATION_WORDS` **移除"取消"**（"取消订阅"是合法动作意图，不得被判为否定）；其余否定词不变。
12. **执行计划 params 只放 JSON 原始值**（05 复核发现）：`contracts.plan.cfhash` 用 `json.dumps`，params 里**不得**出现 `Money` / `Value` / `Slot` 等对象。金额字段名统一为 **`amount_cents`（整数，单位分）**、卡片额度为 **`daily_limit_cents`（整数，单位分）**；02 的 `ToolSpec.money_param` 必须指向这些字段名；`PEP` 负责把分数包装成 `Money` 后再调 `CoreBank`。
13. **控制槽取值规则**（05 复核发现）：接受 `Value(TRUSTED)`、`Slot(CONTROL, Value(TRUSTED))` 与**裸值**（程序化可信：确定性解析器 / 场景装配的产物）；**只拒绝 `Value(UNVERIFIED)`**（H3 的真实要求是"unverified 永不进入控制槽"）。02 须放宽为"仅拒绝 unverified"，`ForbiddenParamError` 语义保留。
14. **20 个动作必须可执行**（03 复核发现 5 个动作无处理器）：01 再**追加**写原语 `sign_subscription(*, user_id, merchant, amount, period, next_charge_on, idempotency_key)`；03 的 PEP 写分派必须**覆盖全部 20 个动作**——`transfer.schedule` 复用转账语义（带 `booked_on`）、`card.lock` / `card.unlock` → `set_card_status`、`card.report_lost` → `set_card_status("lost")`、`card.apply_virtual` → `apply_virtual_card`、`card.set_limit` → `set_card_limit`、`subscription.sign` → `sign_subscription`。
15. **05 的桥接常量降级为临时**：05 的 `base.NORMATIVE_ACTION_KINDS` 是"对齐前"的临时桥；07 须新增交叉断言 `set(ACTION_KINDS) == set(registry.actions()) == set(REQUIRED_SLOTS)`，通过后该桥可删（**已删**）。
16. **写动作必须声明 `user_id`**（07/03 复核发现）：`transfer.create` / `transfer.schedule` / `wealth.subscribe` / `wealth.redeem` / `subscription.sign` 在 02 目录中**必须**声明控制槽参数 `user_id`（`slot=CONTROL`、`py_type=str`、必填）；否则 PEP 写分派拿不到用户身份。会话层与评测链装配 params 时填入 `DEMO_USER_ID`。
17. **`subscription.sign` 槽位统一**为 `("user_id", "merchant", "amount_cents", "period", "next_charge_on")`——代扣签约的对象是**商户**，不涉及已有订阅 id；`subscription.cancel` 仍用 `subscription_id`。04 的 `REQUIRED_SLOTS` 与 05 的 `scenarios.subscriptions` 按此对齐。
18. **01 `Store` 允许跨线程使用**（06 复核发现，2026-10-02）：FastAPI `TestClient` 与 uvicorn 同步端点在线程池中运行，而 01 的 `Store` 用 SQLite 默认 `check_same_thread=True` → 跨线程访问抛 `sqlite3.ProgrammingError`。01 `gateway/store.py` 的两处 `sqlite3.connect` **追加** `check_same_thread=False`（**不改** `SCHEMA_SQL`、不改任何冻结符号/签名）。**并发写一致性由调用方保证单写者语义**（事实网关读多写少；06 为单会话 demo）。回溯修订：01 的 `Store` 行为说明按本条更新（见冻结面表）。
19. **日期参数接受 `datetime.date` 实例**（06 复核发现，2026-10-02）：02 目录把 `booked_on` / `start` / `end` / `next_charge_on` 声明为 `py_type=date`（`tests/tools/test_catalog.py` 锁定该契约），而 03 的 `policy.pep._require_date` 原先只接受 ISO 字符串 → 同一值二者互斥、写路径必失败。03 `policy/pep.py::_require_date` **追加**「若已是 `date` 实例则直接返回」分支（原 ISO 字符串路径不变）。**语义为放宽（向后兼容），不改冻结符号/签名**。回溯修订：03 的 `Pep` 参数说明按本条更新（见冻结面表）；该助手于 2026-10-08 **改名为公开 `require_date`**（见裁决 #25）。
20. **`user_id` 装配归会话层 / 评测链**（05/06 复核，2026-10-02；固化裁决 #16 的落点）：05 的 `scenarios` 组装 `transfer.*` / `wealth.*` 节点时**不**把 `user_id` 放进 `PlanNode.params`；由 06 的 `web.session.Session._to_call` 与 07 的评测链在**构造 `ToolCall` 时**注入 `DEMO_USER_ID`（仅当该动作的 `ToolSpec` 声明了必填 CONTROL 槽 `user_id` 且节点 params 缺失时）。**只在 `ToolCall` 层注入，不改 `PlanNode.params` / `cfhash`**。02 的 `user_id` 声明（裁决 #16）与 05 的省略设计**并非冲突**（06 计划偏差 #1 的"02 未声明 user_id"陈述已过时，作废）。
21. **05 `CrossHandler` 通用化**（2026-10-02，多意图能力）：`matches` 扩为「≥2 场景域 **或** 单域 `funds.*`」；`build` 分两支——「冻结 + 资金」组合**逐字保留**原语义（`grant` 锚点规则不变），其余多域组合走**通用支**（按域分组 → 复用各域 handler 的 `build` → 合并重编号 → 同资源串行 → **单一 `Plan`**）。`select_handler` / `run_scenario` / 06 确认-执行协议**不变**；计划级安全（单卡 / 单 `cfhash` / 单次 `evaluate_plan(NE)`）不变。会话层 `web.session._assemble_intents` 为用户明说意图补 `grant="explicit"`（与 07 评测链 `_scenario_intent` 一致）。单域 `funds.*` 的原 `UnknownScenarioError` 缺口一并补齐。
22. **S1b 授权改 06/07 既有实现（加严 / 新增 / 收窄）**（2026-10-03，G2 收口）：新增 `gateway/risk_store.py`（独立表，不改 01 的 `SCHEMA_SQL`）；`web.app.AppDeps` **尾部追加**可选字段 `risk`（向后兼容）；`web.session._assemble_intents` 对 `wealth.subscribe` 做 per-intent 受信任注入（`product=wp_cash` / `user_id` / `assessment`，**不包 `Value`**）；`eval.pipeline._evaluate` 注入 `assessment` 并**新增收益暗示守卫**（镜像 `FORBIDDEN_HINTS`），`_DEFAULT_SELECTED["product"]` 改 `wp_cash`；`scenarios.wealth` **仅新增** `to_risk_assessment`。**既有冻结符号与签名零改动**（`assess_suitability` / `_assessment` / `RiskAssessment` 逻辑不变）。**语义只加严/收窄，不放宽**。
23. **S2 一致性收口授权（additive / 收窄 / 加严；先改表再回溯）**（2026-10-04，裁决 #23，G3/G5/G4/G12）：授权本计划按下列方向落地，且**先改本冻结面、再回溯修订已出计划的 `Interfaces` 段与实现**：① 00 **新增** `contracts.security`（`INJECTION_MARKERS` · `detect_injection(text) -> bool`），06/07 的注入标记收敛为该单一真源（**集合相等**：`set(web.session.INJECTION_MARKERS) == set(eval.pipeline.INJECTION_MARKERS) == set(contracts.security.INJECTION_MARKERS)`，G4）；② 03 `PolicyRequest` **补登记既有** `daily_limit` 并**尾部新增** `amount_applies: bool = True`（`False ⇒ 金额档不适用，仅用于非流出型动作`，G5 收窄）；③ 05 **新增** `scenarios.authorization`（`object_state_for_node` / `node_policy_request`），06 `web.session` 与 07 `eval.pipeline` 共用该构造器（G3 消除口径双份 + 交叉断言）；④ 07 `_derived_release` 改走受限沙箱 `scenarios.bills.category_share_in_sandbox`（`except (GatewayError, SandboxError, ScenarioRefused): return False`，G12 加严）。**既有冻结符号与签名零改动**；语义**只收窄/加严，不放宽**。

24. **S3 能力补齐授权（additive / 收窄 / 加严；先改表再回溯）**（2026-10-04，裁决 #24，G13 / G1 / G11）：授权 S3 按下列方向落地，且**先改本冻结面、再回溯改实现**：① 04 `planning.llm::_TEMPLATE_RULES` **新增 6 条规则**（`wealth.list` / `card.unlock`（`解锁|解除.{0,4}?卡`，须位于 `card.lock` 之前）/ `card.lock` 带修饰补强（`锁.{0,3}卡`）/ `card.list` / `subscription.list`（须位于 `cancel` 之后、`sign` 之前）/ `account.transactions`（末位）），**既有 10 条正则与动作零改动**；**唯一授权的 precedence 例外**：`{续费提醒, 什么时候续费, 何时续费, 有哪些代扣}` 由 `subscription.sign` 改判 `subscription.list`（裸 `续费/签约/代扣/自动续费` 仍 `sign`），集合须**恰等**；② 05 `scenarios.bills` **新增** `ANOMALY_THRESHOLD_CENTS`（分）/`large_transactions`（判据 `direction=="out" and cents <= -阈值`）/`describe_large_transactions`，`BillsHandler.build` **新增 `account.transactions` 分支**（门=`intent.action=="account.transactions"`；账户集=`gateway.accounts_of(user)` 全账户，不读 `selected["account"]`、不硬编码；节点 id `txn{i}`；params `account_id/start/end` 三参齐备；仅该意图时**短路** bills 计算）；③ 05 `scenarios.wealth` **新增** `product_comparison` 并 **append 加严** `FORBIDDEN_HINTS`；05 `scenarios.subscriptions` **新增** `upcoming_charges`/`describe_upcoming_charges`；④ 06 `web.plan_card` **实现增强**（`_object_label`/`_when_label` 拼接 05 只读事实，**签名/字段零改**，不生成数字）；⑤ 07 `eval/data` **additive**（cases +4 / adversarial +3；heldout 不动）。**冻结符号与签名零改动**；`SCHEMA_SQL` / 20 动作集合 / `OPERATOR_WHITELIST` / `_DOMAIN_HANDLERS` / `_assemble_multi` / `select_handler` **零改动**；**语义只加严/收窄，不放宽**（新增面不得使真实写意图变只读自动执行）。

25. **并行化前置：执行层注册与 PEP 分派改为派生式**（2026-10-08，`docs/已修复清单-2026-10-08.md` 步骤 1.1–1.6；先改本表再回溯）：① `tools.registry` **新增** `ActionKind`（自 `planning.llm` **下移**、语义逐字保留）/ `ParamSpec.slot_name` / `ToolSpec.kind`（**可空**，totality 由派生处 fail-fast 把关），并加不变式 `kind=QUERY ⟺ is_pure_query`；② `tools.catalog` 的 20 条声明补齐两字段；③ `planning.slots.REQUIRED_SLOTS`（判据 = `slot_name` 非空，即「由规划层补全」，**不是**「是否必填」）与 `planning.llm.ACTION_KINDS` 改为**由注册表派生**（新增 `planning → tools.catalog` 依赖，选定**路径 A**，方向与 §6.1 一致）；④ `policy.pep` **删除**手写 `HANDLED_ACTIONS` 常量，改为 `Pep.handled_actions()`（= 纯查询动作 ∪ 已登记写处理器）由注入的注册表派生；`_dispatch_write` 的逐动作 `if/elif` **改为 handler 注册表**（`Mapping[str, WriteHandler]`；`WriteContext` 只暴露 `core_bank` / 归一化 `params` / `spec` / `idempotency_key`），handler 由各场景模块**自带**（`scenarios/*.WRITE_HANDLERS`）、`scenarios.registry` 汇合、组合根 `compose.build_deps` 注入；⑤ `Pep.__init__` **尾部新增必填参数** `write_handlers`；`policy.pep` 的参数助手 `_require_str` / `_require_int` / `_require_date` / `_money_cents` **改名公开**为 `require_str` / `require_int` / `require_date` / `money_cents`（供 handler 复用，私有名不再保留）。**三条硬约束（无裁决不执行 / 绿色只允许纯查询 / 熔断只加严不降级）与全部安全检查（`_assert_member` / `_assert_taint` / `_verify_receipt` / `_finish`）仍留在 `Pep` 包装流程内，handler 无法绕过**；三处一致性断言转为验证「**派生结果 == 注册表**」，并新增「注册表有动作但漏登记 handler ⇒ 拒绝」的 fail-closed 用例。`contracts/` **零改动**；此后**新增规范动作不再需要改 `policy/pep.py`**（`catalog.py` 自己域那一段 + 场景 handler + 评测用例 + `GLOSS`；`catalog.py` 因 1.2b 裁决仍为按域分段的共享文件）。

26. **并行化前置（续）：读分派改为注册表 · `SLOT_PARAMS` 改为派生**（2026-10-08，`docs/已修复清单-2026-10-08.md` 步骤 4「残留」；先改本表再回溯）：① `policy.pep` **新增** `QueryContext(gateway, params, spec)` / `QueryResult(record_ids, provenance, detail)` / `QueryHandler = Callable[[QueryContext], QueryResult]`；`Pep.__init__` **尾部追加必填参数** `query_handlers`；`_execute_query` 的逐动作 `if/elif`（8 个纯查询动作）**改为读 handler 注册表**（未登记 ⇒ `NotAuthorizedError` 并计入熔断失败）；`Pep.handled_actions()` 语义**收窄为「处理器键的并集」**（此前是「注册表里的纯查询动作 ∪ 写处理器」，会把「有动作但没读处理器」谎报为可执行）——故 `tests/policy/test_pep.py` 的等号断言改为**包含**（该用例用的是 03 自建 20 动作注册表），生产侧的双向严格断言移到新增的 `tests/scenarios/test_registry.py::test_every_canonical_action_has_exactly_one_handler`（读 ∪ 写 == 注册表动作集、两侧不相交）；② 各场景模块**自带** `QUERY_HANDLERS`（`bills` 5 项 / `cards` 1 / `subscriptions` 1 / `wealth` 1），`scenarios.registry` 汇合、组合根 `compose.build_deps` 与 `eval.pipeline` 注入；③ `planning.slots.SLOT_PARAMS` 由手写 14 项改为**由 02 目录派生**（`ParamSpec.slot_name → ParamSpec.name`，同名冲突即构造期抛），**可见内容逐项不变**（仍 14 项），故**不改符号与类型**，只是去掉手写平行表；④ 新增 `tests/planning/test_slots.py` 的「派生自目录」「已知词汇下限锁」「每个槽位都有澄清话术」三条断言。**三条硬约束（无裁决不执行 / 绿色只允许纯查询 / 熔断只加严不降级）与全部安全检查（`_assert_member` / `_assert_taint` / `_verify_receipt` / `_finish`）仍留在 `Pep` 包装流程内；读处理器只拿得到只读网关（拿不到 `CoreBank`），结构上不可能产生副作用**。`contracts/` **零改动**；此后**新增任一动作（读或写）都不再需要改 `policy/pep.py`**。

27. **熔断作用域分层 · 状态持久化 · 运维解锁出口 · 对手方口径**（2026-10-08，`docs/已修复清单-2026-10-08.md` P0-5 / P2-3；先改本表再回溯）：① **计数语义修正**——`未识别 / 无匹配场景` 是**能力边界**而非策略拒绝，`web.session._refuse(..., capability_boundary=True)` 不再计入触发 4（此前会计入；`e2e_check.py` 自身第 [6] 步即因此累加，跑 2–3 次就打开全局熔断、此后同一进程内恒红）；② **作用域分层**——`web.session` 新增**会话级** `CircuitBreaker`：触发 3（高频）/ 4（连续策略拒绝）/ 5（会话异常）落**会话级**（只让本会话在确认时转人工接管，其他会话不受影响），触发 1（连续授权失败）/ 2（注入检测）仍落**全局**（跨会话、跨 worker）。判据：全局熔断会拒绝**所有**用户的非纯查询动作，只有**系统性**威胁配得上；单个会话的行为异常应精确落在该会话。`_breaker_blocks`：**纯查询永不受阻**，其余看「全局 open 或本会话 open」；③ **状态持久化**——`policy.breaker` 新增 `BreakerSnapshot` / `BreakerStateStore` 接缝与 `policy.breaker_store.StoreBreakerStateStore`（SQLite **独立表** `breaker_state`，**不并入 01 的 `SCHEMA_SQL`**），`CircuitBreaker(store=…)` 构造装载 / 变更落盘 / 读取前刷新；`compose.build_breaker` **默认落盘**（`AIB_BREAKER_STATE=memory` 退回进程内）——内存态下「重启即复位」等于给安全机制留了一条绕过路径；④ **运维解锁出口**——`POST /api/operator/breaker-unlock`：口令取自 `AIB_OPERATOR_TOKEN`（**未配置 / 空白 ⇒ 403 fail-closed**，定长比较防时序侧信道），**必须给理由**（空 ⇒ 400），解锁前后状态 + 触发项 + 理由写入审计链（新增 `policy.audit.KIND_OPERATOR`）；`unlock()` 仍是**唯一**降级出口；⑤ **对手方口径**——`scenarios.authorization.COUNTERPARTY_PARAMS = ("payee_id", "payer_id")`：**收款**请求的对手方是付款方，阻断名册必须对它同等生效（否则给被冻结 / 涉诈对手方留了资金入口），`object_state_for_node` 只认该元组内的参数名。**三条硬约束不变**：`unlock()` 仍需显式 + 带理由 + 留痕（不可自动 / 静默），`record_success()` 在 `OPEN` 时仍不改状态，纯查询在熔断打开时仍可用。`contracts/` **零改动**。

28. **P2 场景覆盖收口 + 种子补种语义（additive / 结构修正；先改表再回溯）**（2026-10-08，依据 [`11-p2-scenario-coverage.md`](11-p2-scenario-coverage.md) 与 `docs/已修复清单-2026-10-08.md` 第八节；组长授权「按**逻辑通顺度与完整度**裁决，**允许大改**」）：
    ① **规范动作表 20 → 24**（上表已改）：新增 `bills.yearly`（QUERY / READ_ONLY）· `transfer.book`（STATE / LARGE_FUNDS）· `transfer.request_collection`（STATE / SMALL_CHANGE）· `merchant.order`（OUTFLOW / LARGE_FUNDS，**新域** `merchant`）。**既有 20 个动作的名字 / `RiskClass` / `ActionKind` / 参数槽零改动**；`contracts/` **零改动**；三处一致性断言（`ACTION_KINDS` / `REQUIRED_SLOTS` / PEP 读·写分派）仍为「派生结果 == 注册表」，故 24 个动作自动成立。
    ② **能力子集声明（是**有意不做**，不是缺口）**：`merchant.order` 与 `transfer.book` **无独立 NL 触发规则**（**不可口述触达**），仅由场景层函数装配（`scenarios.cross.birthday_plan` / `scenarios.transfer.due_transfer_plan`）。故 `tests/eval/test_cases_data.py` 的 `_CAPABILITY_SUBSET_CATEGORIES = {"merchant"}` 段内是**安全拒绝负例**（断言「不外泄、不误执行」），**不是**放宽断言；`merchant.order` 的槽位名冲突（`when` 同名映射不同参数名会在构造期抛）以 `slot_name=None` 规避（由场景层填充，与 `ParamSpec.slot_name` 判据「是否由规划层补全」一致）。
    ③ **`bills.yearly` 采用「同期（YTD）」口径**：本金区间 = 本年 `1 月 .. 基准月`（`scenarios.bills.ytd_end_month`），对照区间 = 上年同月段；两侧各走 `sum`、同比走 `yoy`，派生数字**只走算子白名单**且携带全部 `record_id`。「上年同期**无数据**」与「上年同期**为 0**」分别呈现（**不编造**）。
    ④ **`gateway.seed` 由「早退」改为「逐行 `INSERT OR IGNORE` 补种」**（`seed_database` 的**符号与签名零改动**）：幂等语义由「查 `schema_meta.seeded` 即返回」改为**逐行忽略已存在行**。理由：种子扩充后**既有演示库必须自动补行**，否则演示仍显示「同比不可判」。覆盖由 **3 → 21** 个月（`_SEED_FIRST/LAST = 2025-01..2026-09`）；`2025` 金额按 **85%** 确定性**整数**缩放（`_YEAR_SCALE`，`2026 = 100` ⇒ 既有 24 条流水**逐字段不变**），使演示同期同比产出真实百分比 **`+17.65%`**。同时**不覆盖**演示过程中产生的真实变更（余额 / 卡片状态 / 订阅状态 / 用户发起的交易都不在种子行内）。冻结面表 `gateway.seed` 行已按本条更新。
    ⑤ **`planning.llm::_TEMPLATE_RULES` 再加 2 条**（`transfer.request_collection` 置于**首位**；`bills.yearly` 须位于 `bills.monthly` **之前**——否则「年度账单」会被更宽的 `账单` 抢先命中），**其余规则零改动**，现行共 **20** 条；冻结面表 `planning.llm` 行已按本条更新。⑥ **冻结面表 `tools.catalog` 行**由「六场景全量动作」订正为「**全量 24 个动作**，七域」。⑦ **「四人」口径**——[`11-p2-scenario-coverage.md`](11-p2-scenario-coverage.md) 的四人计划由**单一执行者（AI 会话）一次性全部完成**（四条分支配四个子代理并行跑），其**「甲 / 乙 / 丙 / 丁」为工作流代号**，不是四位成员分别认领的记录；该计划、`docs/已修复清单-2026-10-08.md` 第七节、`AGENTS.md` 第 3 节与 `docs/变更同步-2026-10-08.md` 均已加执行说明。

### 跨计划硬约束（每份计划都生效）

- 契约层（`contracts/`）**禁止 I/O**：不 import `requests`/`httpx`/`sqlite3`，不调用 `open()`。
- 绿色只允许纯查询；`当日流出累计 ≤1000 元 = 黄`、`>1000 元 = 红`；用户只能加严不能放宽（H10）。
- 执行门判据 = 节点 ∈ 已冻结确认子图哈希；`untrusted` 永不进入控制槽（H2/H3）。
- 沙箱不得回写事实网关；熔断只加严不降级（PRD §5.8）。
- **不得修改已出计划的既有实现**：只允许新增模块；要改上游接口，先改本表、再回溯修订。
- **不得修改 01 的 `gateway/store.py::SCHEMA_SQL`**；后续计划需要持久化时用各自独立的表/文件（如 03 的审计用 JSONL）。
- 依赖纪律：02 / 03 / 05 / 07 **只用标准库**；04 可加 LLM HTTP 客户端但必须可选且默认走 `TemplateIntentModel`；06 可加 `fastapi` / `uvicorn` / `httpx`。
- 提交信息格式 `<type>: <描述>`；每个 Task 以测试全绿为通过条件。

---

*2026-10-01 建立 · 2026-10-01 补「跨计划接口主线（冻结面）」*

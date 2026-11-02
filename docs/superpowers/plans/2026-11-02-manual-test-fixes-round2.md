# 实测截图2 问题修复方案（第二轮）

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development`（推荐）或 `superpowers:executing-plans` 逐任务实施。步骤用 `- [ ]` 复选框跟踪。

**Goal:** 修复 `实测截图2` 中 7 个可复现问题：限额重复表单、代扣签约越权、打开操作确认无响应、首页余额不刷新、本月/上月账单同值、失卡五步计划、补卡弹窗死胡同。

**Architecture:** 不改契约、不新增业务动作、不改 PDP/PEP 判据。优先在规划层（模板/槽位）、场景层（life_events/bills）、Web 层（session/plan_card/assets）做确定性修复；所有文案与行为保持“事实零生成、能力边界如实说明”。

**Tech Stack:** Python 3.11 · FastAPI · SQLite · 原生 JS（`web/assets/app.js`）· pytest / ruff / mypy。

---

## 0. 复现结论（2026-11-02，TestClient + `AIB_DISABLE_LLM=1`）

| # | 截图现象 | 复现结果 | 根因 |
|---|---|---|---|
| 1 | 一句“下周三出差，把卡片日限额降到 1000 元”出现两组相同字段 | `needs_clarification`，消息“这件事包含 2 项：1. 调整卡片限额；2. 调整卡片限额”，共 5 个追问；卡片下拉默认“信用卡·已挂失” | `_CLAUSE_SPLIT` 按逗号切句；`下周三出差` 命中 L3 模板 `出行|出差|旅行|旅游|境外 → card.set_limit`，`把卡片日限额…` 又命中 `额度|限额 → card.set_limit`，产生两个同动作意图；卡片候选未按动作/卡类型过滤 |
| 2 | “代扣签约”出现商户/金额/周期/扣款日表单，未说明为什么扣 | `needs_clarification`，4 个字段 | 模板把 `签约|代扣|自动续费|自动扣款|续费` 映射到 `subscription.sign`；但银行 Agent 不应代办第三方签约，最新代扣协议设计只做查询/解约 |
| 3 | “打开操作确认”点击无反应 | 按钮存在且可用，但 `openCardFlowFromPlan()` 静默 return | `plan_nodes_payload()` 只回传 `node_id/action/label/depends_on/resource/index/product_id/amount_cents/share_bp`，**没有 `card_id`**；前端读 `node.params.card_id` 得到空串，找不到卡后直接 `return` |
| 4 | 任意操作余额不变 | 转账 12843000 → 12833000 分（-10000），`/api/options` 同步变化；卡片/账单/查询/签约按设计不动资金 | 后端账本正确；**首页余额卡**只在页面加载和卡片弹窗确认时刷新，对话执行后不刷新（`refreshAfterExecution()` 未调用 `refreshHomeBalance()`） |
| 5 | 查本月账单与上月账单回复同一内容 | 两句都返回“月度账单分析（2026-09）” | `_stated_period()` 只识别“2026-09 / 去年 12 月”，不识别“本月/上月”；缺省回退到 `DEFAULT_BOOKING_DATE=2026-10-31` 的上一月 = 2026-09 |
| 6 | “我的借记卡丢了”直接生成 5 步计划，含改密/查交易 | `awaiting_confirm`，节点：临时冻结→正式挂失→申请虚拟卡→修改交易密码→交易记录查询 | `_build_l5()` 固定拼 5 节点；冻结与挂失互相矛盾，虚拟卡不能替代实体补卡，改密/查交易不应自动捆绑 |
| 7 | 申请补卡弹窗提示“请确认收款人”，但弹窗没有该字段 | 先让会话处于“转账缺收款人”澄清，再提交补卡表单，服务端仍返回收款人追问 | `Session._submit()` 在 `NEEDS_CLARIFICATION` 时把新提交并入旧澄清；`_freeform_answers("申请补卡")` 被当成商户/人名答案，补卡表单没有 `new_intent` 隔离 |

> 复现脚本为临时 TestClient 脚本，不提交仓库；对应回归测试写在各任务里。

---

## 1. 已确认的修复口径

| 决策 | 结论 |
|---|---|
| 代扣签约 | **下线 Agent 签约入口**，改为“代扣协议查询 / 代扣解约”。Agent 不能代客新签第三方代扣协议；只保留查询与关闭银行卡侧授权。`subscription.sign` 动作与处理器暂留作内部能力，但任何用户话术都不得到达它。 |
| 限额时间 | 只做**即时生效**；表单/消息明确提示“不支持预约到下周三”，不静默忽略日期。 |
| 失卡流程 | 先问“临时冻结（可解，5–15 天）”还是“正式挂失（不可逆，需补卡）”，再生成**单节点**计划；不再自动捆绑虚拟卡/改密/查交易。 |
| 余额 | 后端账本已验证正确；本次修**首页余额卡刷新**，并补充卡片操作不改变余额的说明。 |

**不做（本方案明确排除）：**

- 不实现预约限额、临时限额到期自动恢复；
- 不实现签约后的到期自动扣款引擎；
- 不新增/删除 `tools/catalog.py` 的业务动作；
- 不改 `contracts/`、`policy/pdp.py`、`planning/kernel.py`；
- 不给补卡新增收件人姓名/手机号字段（本轮只修“错误追问收款人”的死胡同，收件人字段留作后续）。

---

## 2. 任务分解

### Task 1：代扣签约下线，代扣查询/解约上位

**Files:**
- Modify: `src/ai_banking_copilot/planning/llm.py:233-262`
- Modify: `src/ai_banking_copilot/web/session.py`（新增能力边界守卫）
- Modify: `src/ai_banking_copilot/web/app.py:1230-1245`（`subscription_capabilities`）
- Modify: `src/ai_banking_copilot/web/page.py:151-158`
- Modify: `src/ai_banking_copilot/web/assets/app.js:7-24`
- Modify: `README.md:34,149`（能力口径：签约 → 查询/解约）
- Test: `tests/planning/test_llm_template.py:140-150`
- Test: `tests/web/test_business_closure.py`（新增能力边界用例）
- Test: `scripts/e2e_check.py`

- [ ] **Step 1: 写失败测试**

在 `tests/planning/test_llm_template.py` 把原断言改为：

```python
@pytest.mark.parametrize("utterance", ("续费", "代扣", "自动续费"))
def test_generic_debit_words_route_to_agreement_query(utterance: str) -> None:
    assert _actions(utterance) == ["subscription.list"]


def test_debit_sign_is_not_an_agent_action() -> None:
    # 模板层不产出 subscription.sign；具体拒绝由会话层能力边界负责。
    assert "subscription.sign" not in _actions("代扣签约")
```

在 `tests/web/test_business_closure.py` 新增：

```python
def test_agent_refuses_to_sign_new_debit_agreement(tmp_path: Path) -> None:
    deps = _deps(tmp_path)
    client = TestClient(create_app(deps))
    view = client.post(
        "/api/submit",
        json={"utterance": "代扣签约", "session_id": "sign-boundary"},
    ).json()
    assert view["state"] == "blocked"
    assert "不能代您新签" in view["message"]
    assert view["card"] is None
```

- [ ] **Step 2: 运行测试确认失败**

```bash
pytest tests/planning/test_llm_template.py -q
pytest tests/web/test_business_closure.py::test_agent_refuses_to_sign_new_debit_agreement -q
```

预期：`代扣` 仍返回 `subscription.sign`；会话层没有拒绝，返回 `needs_clarification`。

- [ ] **Step 3: 改模板规则**

`planning/llm.py` 第 262 行：

```python
# 代扣域：Agent 只做“查询已有协议 / 关闭银行卡侧授权”，不代办新签约。
# 泛词先落到查询；明确的“签约”由 session 层能力边界拒绝，避免生成签约表单。
(re.compile(r"签约|代扣|自动续费|自动扣款|续费"), "subscription.list",
 "命中『代扣 / 续费查询』模板（Agent 不代办新签约）"),
```

第 234-241 行的取消规则补 `代扣解约|解约代扣`：

```python
re.compile(
    r"取消订阅|退订|取消代扣|代扣解约|解约代扣|关掉自动续费|关闭扣款协议|关闭代扣协议|关掉银行卡代扣"
    r"|(?:关闭|解约).{0,16}(?:扣款协议|代扣协议)"
    r"|取消.{0,12}(?:会员|订阅|自动续费|代扣)"
    r"|(?:会员|订阅|自动续费|代扣).{0,4}取消"
),
"subscription.cancel",
"命中『关闭代扣协议』模板",
```

- [ ] **Step 4: 会话层能力边界**

在 `web/session.py` 的 `_guard_utterance` 附近新增常量与函数：

```python
_SUBSCRIPTION_SIGN_BOUNDARY = (
    "银行 Agent 不能代您新签第三方代扣协议：签约必须由商户侧发起并经您本人授权。"
    "我可以帮您查询已签的代扣协议，或关闭某项扣款授权。"
)

_SIGN_REQUEST = re.compile(r"代扣签约|签约代扣|开通代扣|新签代扣|新增代扣|帮我签约|自动续费签约")
_SIGN_QUERY_OR_CANCEL = re.compile(r"取消|解约|关闭|终止|查询|查看|记录|信息|时间|详情")


def _subscription_sign_refusal(utterance: str) -> str | None:
    """Agent 不代办新签约：只拒绝“签约”语义，查询/解约照常放行。"""
    text = utterance.strip()
    if not _SIGN_REQUEST.search(text):
        # “签约”单独出现也按新签约处理；带查询/解约词的句子放行。
        if "签约" in text and not _SIGN_QUERY_OR_CANCEL.search(text):
            return _SUBSCRIPTION_SIGN_BOUNDARY
        return None
    if _SIGN_QUERY_OR_CANCEL.search(text):
        return None
    return _SUBSCRIPTION_SIGN_BOUNDARY
```

在 `_submit()` 里，放在 `_third_party_account_refusal` 之后、`_assemble_intents` 之前：

```python
sign_refusal = _subscription_sign_refusal(utterance)
if sign_refusal is not None:
    return self._refuse(sign_refusal, utterance, capability_boundary=True)
```

同时把 `_freeform_answers()` 第 1645 行的“含动作词不当作补充答案”正则补上 `签约|代扣`：

```python
if re.search(r"取消|查|转|买|卖|冻结|解冻|订阅|签约|代扣|账单|余额|理财", candidate):
    return {}
```

否则会话处于旧澄清时输入“代扣签约”，会先被当成商户/人名答案，走不到能力边界守卫。

再在 `_assemble_intents` 返回后加一道**模型通道兜底**（防 LLM 直接给出 `subscription.sign`）：

```python
if any(intent.action == "subscription.sign" for intent in intents):
    return self._refuse(_SUBSCRIPTION_SIGN_BOUNDARY, utterance, capability_boundary=True)
```

- [ ] **Step 5: 能力口径与入口文案**

`web/app.py` 的 `subscription_capabilities` 增加：

```python
"can_sign": False,
"sign_boundary": (
    "银行 Agent 不代办新签代扣协议；仅支持查询已有协议与关闭银行卡侧扣款授权。"
),
```

`web/page.py` 场景⑤描述改为：

```html
<p class="scene-card__d">查询已签代扣协议、关闭银行卡侧扣款授权；Agent 不代办新签约。</p>
```

`web/assets/app.js` 的 `PRESETS` 把 `"代扣签约"` 改为 `"代扣解约"`，并补 `"查我的代扣协议"`。

动作中文名保持 `subscription.cancel = "关闭扣款协议"`（设计与既有测试口径，避免不必要的断言改动）；
“代扣解约”作为入口话术与用户可见名称使用。

`README.md` 两处对外口径改为：

```markdown
| 5 | 订阅/代扣管理 | 代扣协议查询与续费提醒、代扣解约（关闭银行卡侧扣款授权） |

| 代扣解约 `subscription.cancel` | 选择已有代扣协议并确认；关闭后银行卡侧不再放款，商户侧服务由商户决定 | `web-subs-cancel` |
```

删除“代扣签约”能力描述；如需保留 `subscription.sign` 的内部说明，必须标注“非 Agent 能力，界面不暴露”。

- [ ] **Step 6: 运行测试**

```bash
pytest tests/planning/test_llm_template.py tests/web/test_business_closure.py -q
```

预期：模板泛词返回 `subscription.list`；`代扣签约` 返回 `blocked` 且消息含“不能代您新签”；`代扣解约` 返回 `subscription.cancel` 的澄清/计划。

- [ ] **Step 7: 提交**

```bash
git add src/ai_banking_copilot/planning/llm.py src/ai_banking_copilot/web/session.py \
  src/ai_banking_copilot/web/app.py src/ai_banking_copilot/web/page.py \
  src/ai_banking_copilot/web/assets/app.js tests/planning/test_llm_template.py \
  tests/web/test_business_closure.py
git commit -m "fix(subscriptions): 下线 Agent 代扣签约，改为查询/解约能力边界"
```

---

### Task 2：限额意图去重、卡片候选与即时提示

**Files:**
- Modify: `src/ai_banking_copilot/planning/llm.py:321`
- Modify: `src/ai_banking_copilot/planning/slots.py:434-487,790-811`
- Modify: `src/ai_banking_copilot/web/session.py`（限额时间提示）
- Test: `tests/planning/test_llm_template.py`
- Test: `tests/scenarios/test_cards_state_machine.py` 或新增 `tests/web/test_card_limit_clarification.py`

- [ ] **Step 1: 写失败测试**

```python
def test_travel_clause_does_not_create_a_second_limit_intent() -> None:
    intents = TemplateIntentModel().propose(
        "下周三出差，把卡片日限额降到 1000 元", {}
    ).intents
    assert [intent.action for intent in intents] == ["card.set_limit"]
```

```python
def test_limit_form_asks_once_and_offers_only_active_debit_cards(deps: AppDeps) -> None:
    session = Session(deps)
    view = session.submit("把卡片日限额降到 1000 元")
    assert view.state is SessionState.NEEDS_CLARIFICATION
    assert len(view.clarifications) == 3  # card / limit_kind / daily_limit
    assert {ask.slot for ask in view.clarifications} == {"card", "limit_kind", "daily_limit"}
    card_ask = next(ask for ask in view.clarifications if ask.slot == "card")
    assert card_ask.candidates
    assert all(choice.value == "card_debit" for choice in card_ask.candidates)
    assert "即时生效" in view.message
```

- [ ] **Step 2: 运行确认失败**

```bash
pytest tests/planning/test_llm_template.py::test_travel_clause_does_not_create_a_second_limit_intent -q
pytest tests/web/test_card_limit_clarification.py -q
```

预期：第一条得到 2 个 `card.set_limit`；第二条得到 5 个追问且候选包含信用卡。

- [ ] **Step 3: 删除“出行词单独成意图”的模板规则**

`planning/llm.py:321` 删除：

```python
(re.compile(r"出行|出差|旅行|旅游|境外"), "card.set_limit", "命中『L3 出行保障』生活事件"),
```

改为注释说明：

```python
# 注意：不在这里把“出差/出行”单独映射成 card.set_limit。
# 否则“下周三出差，把卡片日限额降到 1000 元”会被逗号切成两个同动作意图，
# 产生两组重复追问。L3 的识别交给 scenarios.life_events.detect_life_event
# （在同一动作意图的原文包含出行词时仍可命中）。
```

- [ ] **Step 4: 卡片候选按动作过滤**

`planning/slots.py` 的 `_resolve_card` 增加 `action` 参数，并在追问时给出可点选候选：

```python
_CARD_TYPE_LABELS = {"debit": "借记卡", "credit": "信用卡", "virtual": "虚拟卡"}
_CARD_STATUS_LABELS = {
    "active": "可用",
    "locked": "临时冻结",
    "lost": "已挂失",
}


def _eligible_cards(action: str, cards: Sequence[Card]) -> list[Card]:
    if action == "card.set_limit":
        # 只排除已挂失 / 已补卡的借记卡；临时冻结的借记卡仍可调额，标签会显示“临时冻结”。
        return [
            c for c in cards
            if c.card_type == "debit" and c.status != "lost" and c.replaced_by_card_id is None
        ]
    if action == "card.lock":
        return [c for c in cards if c.status == "active"]
    if action in {"card.adjust_credit_limit", "card.set_cycle_limit"}:
        return [c for c in cards if c.status != "lost" and c.card_type == "credit"]
    if action == "card.unlock":
        return [c for c in cards if c.status == "locked"]
    if action == "card.replace":
        return [c for c in cards if c.replaced_by_card_id is None]
    return [c for c in cards if c.status != "lost"]


def _card_choices(action: str, cards: Sequence[Card]) -> tuple[tuple[str, str], ...]:
    return tuple(
        (
            f"{_CARD_TYPE_LABELS.get(card.card_type, card.card_type)}"
            f" · 尾号 {card.last4 or '未知'} · {_CARD_STATUS_LABELS.get(card.status, card.status)}",
            card.card_id,
        )
        for card in _eligible_cards(action, cards)
    )
```

`_resolve_card(inputs, notes, action=action)` 的最终追问改为：

```python
return None, SlotQuestion(
    "card", _PROMPTS["card"], choices=_card_choices(action, inputs.cards)
), tuple(notes)
```

`_resolve_slot` 的调用改为 `_resolve_card(inputs, notes, action=action)`（两处：卡类型词优先分支与普通分支）。

> 说明：`card.set_limit` 只列**未挂失的借记卡**；截图中的“信用卡·已挂失”不会再出现在下拉框。若当前没有借记卡，候选为空，前端回落到全部卡片，由服务端按现有状态机拒绝，不静默换卡。

- [ ] **Step 5: 限额时间提示**

在 `web/session.py` 的 `_assemble_intents` 组装 `asks` 后追加确定性提示：

```python
def _card_limit_time_note(utterance: str, intents: Sequence[Intent], today: date) -> str | None:
    if not any(intent.action == "card.set_limit" for intent in intents):
        return None
    parsed = parse_temporal(utterance, today)
    if parsed.matched and parsed.when is not None and parsed.when > today:
        return "限额调整即时生效，不支持预约到下周三；如需出行期间临时限额，请在出行前再发起。"
    return None
```

把返回值并入 `notes`（`notes = (*notes, note)`），确保 `_with_notes` 把它显示在澄清消息里。

- [ ] **Step 6: 运行测试**

```bash
pytest tests/planning/test_llm_template.py tests/web/test_card_limit_clarification.py -q
```

预期：一条意图、3 个追问、候选只有 `card_debit`、消息含“即时生效”。

- [ ] **Step 7: 提交**

```bash
git add src/ai_banking_copilot/planning/llm.py src/ai_banking_copilot/planning/slots.py \
  src/ai_banking_copilot/web/session.py tests/planning/test_llm_template.py \
  tests/web/test_card_limit_clarification.py
git commit -m "fix(cards): 限额意图去重并按动作过滤卡片候选"
```

---

### Task 3：失卡先问“冻结还是挂失”，再出单节点计划

**Files:**
- Modify: `src/ai_banking_copilot/scenarios/base.py:63-72`
- Modify: `src/ai_banking_copilot/scenarios/registry.py:106-117`
- Modify: `src/ai_banking_copilot/scenarios/life_events.py:157-178,1005-1065`
- Modify: `src/ai_banking_copilot/web/session.py:1311-1325`
- Modify: `src/ai_banking_copilot/web/assets/app.js`（`clarifyControl` 的 `loss_type` 下拉）
- Test: `tests/scenarios/test_life_events.py:177-188`
- Test: `tests/web/test_card_flow.py:20-76`

- [ ] **Step 1: 写失败测试**

```python
def test_l5_asks_loss_type_before_planning(deps: AppDeps) -> None:
    session = Session(deps)
    view = session.submit("我的借记卡丢了", selected={"card": "card_debit"})
    assert view.state is SessionState.NEEDS_CLARIFICATION
    assert [ask.slot for ask in view.clarifications] == ["loss_type"]
    assert "临时冻结" in view.clarifications[0].prompt


def test_l5_temporary_builds_only_a_freeze_node(deps: AppDeps) -> None:
    session = Session(deps)
    session.submit("我的借记卡丢了", selected={"card": "card_debit"})
    view = session.clarify({"loss_type": "temporary"})
    assert view.state is SessionState.AWAITING_CONFIRM
    assert [node.action for node in view.plan.nodes] == ["card.lock"]


def test_l5_formal_builds_only_a_report_lost_node(deps: AppDeps) -> None:
    session = Session(deps)
    session.submit("我的借记卡丢了", selected={"card": "card_debit"})
    view = session.clarify({"loss_type": "formal"})
    assert view.state is SessionState.AWAITING_CONFIRM
    assert [node.action for node in view.plan.nodes] == ["card.report_lost"]
```

- [ ] **Step 2: 运行确认失败**

```bash
pytest tests/scenarios/test_life_events.py::test_l5_asks_loss_type_before_planning -q
```

预期：直接 `awaiting_confirm`，5 个节点。

- [ ] **Step 3: 让场景级澄清可结构化返回**

`scenarios/base.py` 的 `ScenarioOutcome` 增加字段：

```python
clarification: ClarificationRequired | None = None
"""场景层主动追问（如 L5 的挂失类型）：会话层据此生成结构化澄清字段。"""
```

`scenarios/registry.py` 的 `except ClarificationRequired` 分支增加：

```python
clarification=error,
```

`web/session.py` 的 `_after_outcome` 在 `if outcome.status is ScenarioStatus.NEEDS_CLARIFICATION:` 内：

```python
if not asks and outcome.clarification is not None:
    asks = (
        ClarificationAsk(
            slot=outcome.clarification.slot,
            prompt=outcome.clarification.prompt,
        ),
    )
    questions = tuple(ask.prompt for ask in asks)
```

再走原有的 `self._pending_slots = frozenset(ask.slot for ask in asks)`。`questions` 必须在重建 `asks` 后同步重算，否则 `SubmissionView` 的 `questions` 仍为空、前端只显示一段无字段的文字。

- [ ] **Step 4: 重写 L5 计划**

`life_events.py` 的 L5 原型：

```python
LifeEventPrototype(
    event_id="L5",
    name="失卡止损型",
    triggers=("卡丢了", "银行卡丢", "卡片丢失", "手机丢了", "钱包被偷", "钱包丢了", "疑似盗刷"),
    domains=("card",),
    skeleton=("card.lock", "card.report_lost"),
    reuse="scenarios.cards",
    description="先区分暂时找不到（临时冻结，可解）与确认丢失（正式挂失，不可逆需补卡），再只做对应的那一步。",
    example="我的借记卡丢了",
    cases=(...保持不变...),
)
```

`_build_l5` 改为：

```python
def _build_l5(params, gateway, ledger, prefix):
    card_id = _text(params, "card", "card")
    card = _card_of(gateway, card_id)
    # 先取可选值再判断：不要用 _text，否则缺失时会先抛“缺少控制槽 loss_type”，
    # 把这里更具体的二选一提示盖掉。
    loss_type = _opt_text(params, "loss_type")
    if loss_type == "temporary":
        return (
            _node(ledger, f"{prefix}1", "card.lock",
                  {"card_id": card_id, "duration_days": 7},
                  detail="临时冻结（口头挂失）"),
        )
    if loss_type == "formal":
        return (
            _node(ledger, f"{prefix}1", "card.report_lost",
                  {"card_id": card_id, "loss_type": "formal"},
                  detail="正式挂失"),
        )
    raise ClarificationRequired(
        "loss_type",
        "卡片是暂时找不到，还是确认丢失？临时冻结可解、5–15 天；正式挂失不可逆，需要补卡。",
    )
```

删除 `card.apply_virtual` / `password.change` / `account.transactions` 节点。

状态兜底：`_card_of()` 已拿到 `card` 对象，分支前加：

```python
if card.status == "lost":
    raise ScenarioRefused("这张卡已正式挂失：不能再临时冻结，也无需重复挂失；如需用卡请申请补卡。")
```

避免用户对已挂失的卡再选“临时冻结”，生成一张必然执行失败的节点。

- [ ] **Step 5: 前端挂失类型下拉**

`web/assets/app.js` 的 `clarifyControl(ask)` 增加：

```js
if (slot === "loss_type") {
  return `<select ${attrs}>
    <option value="">请选择挂失类型</option>
    <option value="temporary">临时冻结（可解，5–15 天，免费）</option>
    <option value="formal">正式挂失（不可逆，需要补卡）</option>
  </select>`;
}
```

`SLOT_LABELS` 增加 `loss_type: "挂失类型"`。

- [ ] **Step 6: 更新旧测试**

`tests/scenarios/test_life_events.py::test_l5_reports_the_loss_and_issues_a_virtual_card` 改为：

```python
def test_l5_formal_reports_the_loss_only(gateway: FactGateway) -> None:
    plan = build_life_event("L5", {"card": "card_debit", "loss_type": "formal"}, gateway)
    assert _actions(plan) == ["card.report_lost"]
    assert plan_net_effect(plan, gateway).irreversibles == ("n1",)
```

`tests/web/test_card_flow.py::test_failed_virtual_card_step_keeps_progress_without_deadlock` 删除或改写为“正式挂失单步执行成功”：

```python
def test_lost_card_formal_flow_executes_one_node(deps: AppDeps, gateway: FactGateway) -> None:
    session = Session(deps)
    view = session.submit("我的借记卡丢了", selected={"card": "card_debit"})
    view = session.clarify({"loss_type": "formal"})
    prompt = session.request_verification()
    done = session.confirm(
        view.card.card_id, frozen_hash=view.card.frozen_hash, verification_code=prompt.code
    )
    assert done.state is SessionState.COMPLETED
    assert gateway.card("card_debit").payload.status == "lost"
```

- [ ] **Step 7: 运行测试**

```bash
pytest tests/scenarios/test_life_events.py tests/web/test_card_flow.py -q
```

预期：L5 先追问 `loss_type`；选择后只有 1 个节点。

- [ ] **Step 8: 提交**

```bash
git add src/ai_banking_copilot/scenarios/base.py src/ai_banking_copilot/scenarios/registry.py \
  src/ai_banking_copilot/scenarios/life_events.py src/ai_banking_copilot/web/session.py \
  src/ai_banking_copilot/web/assets/app.js tests/scenarios/test_life_events.py tests/web/test_card_flow.py
git commit -m "fix(life-events): 失卡先澄清冻结/挂失，改为单节点计划"
```

---

### Task 4：卡片计划回传 `card_id`，“打开操作确认”可用

**Files:**
- Modify: `src/ai_banking_copilot/web/plan_card.py:572-603`
- Modify: `src/ai_banking_copilot/web/assets/app.js:3184-3221,3580-3614`
- Modify: `src/ai_banking_copilot/web/page.py:715`
- Modify: `scripts/check_served_page.py:44,74`
- Test: `tests/web/test_plan_card.py`

- [ ] **Step 1: 写失败测试**

```python
def test_plan_nodes_payload_includes_card_id(gateway: FactGateway) -> None:
    plan = CardsHandler().build([_card_intent("card.lock")], gateway)
    payload = plan_nodes_payload(plan)
    assert payload[0]["card_id"] == "card_debit"
```

- [ ] **Step 2: 运行确认失败**

```bash
pytest tests/web/test_plan_card.py::test_plan_nodes_payload_includes_card_id -q
```

预期：`KeyError: 'card_id'`。

- [ ] **Step 3: 补 payload 字段**

`web/plan_card.py` 的 `plan_nodes_payload` 每行增加：

```python
"card_id": node.params.get("card_id")
if isinstance(node.params.get("card_id"), str)
else "",
```

- [ ] **Step 4: 前端读 `card_id`，组合计划不再套单卡流程**

`web/assets/app.js` 的 `openCardFlowFromPlan`：

```js
const cardNodes = (plan.nodes || []).filter(
  (item) => String(item.action || "").startsWith("card.")
);
const node = cardNodes[0];
const cardId = node && (node.card_id || (node.params && node.params.card_id)) || "";
```

找不到卡时不再静默返回：

```js
if (!card) {
  state.notice = interceptHtml(
    "无法打开操作确认",
    "计划里没有找到可操作的卡片，请刷新卡片列表后重试。",
    false
  );
  renderStage();
  return;
}
```

组合计划（多节点或多动作）走通用确认：

```js
const actions = new Set(cardNodes.map((item) => String(item.action || "")));
const composite = cardNodes.length > 1 || actions.size > 1;
const flow = composite ? "" : ({ ... })[action] || "";
```

`renderCardFlow` 中标题：

```js
title.textContent = flow ? (CARD_FLOW_TITLES[flow] || "卡片操作") : "组合操作确认";
```

`cardFlowConfirmLabel` 增加：

```js
if (!flow) return "确认执行";
```

`cardFlowTotalSteps` 对空 flow 返回 1，步骤条为空即可。

- [ ] **Step 5: 资源版本号**

`web/page.py:715` 改为 `/assets/app.js?v=91`；`scripts/check_served_page.py:44,74` 同步改为 `?v=91`。

- [ ] **Step 6: 运行测试**

```bash
pytest tests/web/test_plan_card.py -q
```

预期：payload 含 `card_id`；手工点“打开操作确认”能弹出卡片弹窗或组合确认。

- [ ] **Step 7: 提交**

```bash
git add src/ai_banking_copilot/web/plan_card.py src/ai_banking_copilot/web/assets/app.js \
  src/ai_banking_copilot/web/page.py scripts/check_served_page.py tests/web/test_plan_card.py
git commit -m "fix(cards): 计划节点回传 card_id，修复打开操作确认无响应"
```

---

### Task 5：表单提交与旧澄清隔离（`new_intent`）

**Files:**
- Modify: `src/ai_banking_copilot/web/app.py:421-432,2273-2284`
- Modify: `src/ai_banking_copilot/web/session.py:811-833,943-969`
- Modify: `src/ai_banking_copilot/web/assets/app.js:3685-3720`（卡片弹窗）、场景/理财表单提交处
- Test: `tests/web/test_card_flow.py` 或新增 `tests/web/test_form_submit_isolation.py`

- [ ] **Step 1: 写失败测试**

```python
def test_card_form_submit_is_not_hijacked_by_pending_payee(deps: AppDeps) -> None:
    session = Session(deps)
    pending = session.submit("转账 300 元")
    assert pending.state is SessionState.NEEDS_CLARIFICATION
    assert "收款人" in pending.message

    view = session.submit(
        "申请补卡",
        selected={
            "card": "card_debit",
            "address": "上海市浦东新区世纪大道 100 号",
            "mail_method": "standard",
            "replace": "yes",
        },
        new_intent=True,
    )
    assert view.state is SessionState.AWAITING_CONFIRM
    assert [node.action for node in view.plan.nodes] == ["card.replace"]
    assert "收款人" not in view.message
```

- [ ] **Step 2: 运行确认失败**

```bash
pytest tests/web/test_form_submit_isolation.py -q
```

预期：仍返回 `needs_clarification`，消息为“请确认收款人…”。

- [ ] **Step 3: 后端增加 `new_intent` 开关**

`web/app.py`：

```python
class _SubmitRequest(BaseModel):
    ...
    new_intent: bool = False
    """表单类提交（卡片弹窗/场景页）置 true：强制开启新意图，不并入旧澄清。"""
```

`/api/submit` 调用：

```python
view = session.submit(
    request.utterance,
    selected=request.selected,
    new_intent=request.new_intent,
    on_stage=on_stage,
)
```

`web/session.py`：

```python
def submit(..., new_intent: bool = False) -> SubmissionView:
    ...
    view = self._submit(
        utterance, selected=selected, now=now, execution_id=secrets.token_hex(16),
        new_intent=new_intent, on_stage=on_stage,
    )
```

`_submit(..., new_intent: bool = False)` 的 freeform 合并条件加 `not new_intent`：

```python
if (
    allow_freeform_clarification
    and not new_intent
    and self._state is SessionState.NEEDS_CLARIFICATION
    and self._last_utterance is not None
    and utterance.strip() != self._last_utterance.strip()
):
```

- [ ] **Step 4: 前端表单提交带开关**

`web/assets/app.js` 的 `submitCardFlow`：

```js
result = await post("/api/submit", {
  utterance: submission.utterance,
  selected: submission.selected,
  new_intent: true,
});
```

场景页 `submitTask` / 理财确认表单同样加 `new_intent: true`（它们都是表单，不是聊天补充）。

- [ ] **Step 5: 补卡弹窗处理 `needs_clarification`**

`submitCardFlow` 在 `data.state !== "awaiting_confirm"` 分支里区分：

```js
const asks = data.clarifications || [];
if (data.state === "needs_clarification" && asks.length) {
  cardFlowError(
    `${data.message} 若缺失项不在本弹窗内，请回到对话补充后再提交。`
  );
  return;
}
cardFlowError(data.message || "这次操作没有生成可执行计划，请检查填写内容。");
```

本轮不新增收件人字段；`new_intent` 修复后，补卡表单不会再被收款人问题劫持。

- [ ] **Step 6: 运行测试**

```bash
pytest tests/web/test_form_submit_isolation.py -q
```

预期：补卡返回 `awaiting_confirm`，节点 `card.replace`。

- [ ] **Step 7: 提交**

```bash
git add src/ai_banking_copilot/web/app.py src/ai_banking_copilot/web/session.py \
  src/ai_banking_copilot/web/assets/app.js tests/web/test_form_submit_isolation.py
git commit -m "fix(web): 表单提交强制新意图，避免被旧澄清劫持"
```

---

### Task 6：本月/上月账单账期解析

**Files:**
- Modify: `src/ai_banking_copilot/scenarios/bills.py:332-391`
- Test: `tests/scenarios/test_business_flows.py` 或新增 `tests/scenarios/test_bills_period.py`
- Test: `scripts/e2e_check.py`

- [ ] **Step 1: 写失败测试**

```python
import pytest
from ai_banking_copilot.planning.llm import TemplateIntentModel
from ai_banking_copilot.scenarios.bills import BillsHandler

@pytest.mark.parametrize(
    ("utterance", "expected"),
    [
        ("查一下本月账单", (2026, 10)),
        ("查一下这个月账单", (2026, 10)),
        ("查一下上月账单", (2026, 9)),
        ("查一下上个月账单", (2026, 9)),
        ("查一下 2026-09 账单", (2026, 9)),
    ],
)
def test_relative_bill_month(gateway, utterance, expected) -> None:
    intents = TemplateIntentModel().propose(utterance, {}).intents
    plan = BillsHandler().build(intents, gateway)
    node = next(n for n in plan.nodes if n.action == "bills.monthly")
    assert (node.params["year"], node.params["month"]) == expected
```

- [ ] **Step 2: 运行确认失败**

```bash
pytest tests/scenarios/test_bills_period.py -q
```

预期：本月/这个月/上月/上个月全部得到 `(2026, 9)`。

- [ ] **Step 3: 增加相对月份解析**

`scenarios/bills.py`：

```python
_RELATIVE_MONTH = re.compile(r"(?P<rel>本月|这个月|当月|上月|上个月|下个月)")
_RELATIVE_MONTH_OFFSET = {"本月": 0, "这个月": 0, "当月": 0, "上月": -1, "上个月": -1, "下个月": 1}


def _shift_month(year: int, month: int, offset: int) -> tuple[int, int]:
    index = year * 12 + (month - 1) + offset
    return index // 12, index % 12 + 1
```

`_stated_period` 在显式账期之后、`_RELATIVE_PERIOD` 之前加：

```python
match = _RELATIVE_MONTH.search(span)
if match is not None:
    year, month = _shift_month(REFERENCE_DATE.year, REFERENCE_DATE.month,
                               _RELATIVE_MONTH_OFFSET[match["rel"]])
    return _checked_period(year, month)
```

- [ ] **Step 4: 运行测试**

```bash
pytest tests/scenarios/test_bills_period.py tests/scenarios/test_business_flows.py -q
```

预期：本月 = 2026-10，上月 = 2026-09，显式账期不被覆盖。

- [ ] **Step 5: 提交**

```bash
git add src/ai_banking_copilot/scenarios/bills.py tests/scenarios/test_bills_period.py
git commit -m "fix(bills): 识别本月/上月账期，避免两句返回同一月"
```

---

### Task 7：首页余额卡在对话执行后刷新

**Files:**
- Modify: `src/ai_banking_copilot/web/assets/app.js:4863-4886`（`refreshAfterExecution`）
- Modify: `src/ai_banking_copilot/web/assets/app.js`（卡片结果说明）
- Modify: `src/ai_banking_copilot/web/page.py:715`（版本号已在 Task 4 处理）
- Test: `tests/web/test_business_closure.py`（后端余额回归）
- Test: `scripts/e2e_check.py`

- [ ] **Step 1: 写后端余额回归测试**

```python
def test_transfer_changes_options_balance_immediately(tmp_path: Path) -> None:
    deps = _deps(tmp_path)
    client = TestClient(create_app(deps))
    before = deps.gateway.account("acc_savings").payload.balance.cents
    view = client.post(
        "/api/submit",
        json={"utterance": "转账给李娜 100 元", "session_id": "balance"},
    ).json()
    assert view["state"] == "awaiting_confirm"
    _confirm(client, view, "balance")
    after = deps.gateway.account("acc_savings").payload.balance.cents
    assert after == before - 10000
    options = client.get("/api/options").json()
    savings = next(item for item in options["accounts"] if item["id"] == "acc_savings")
    assert savings["balance_cents"] == after
```

- [ ] **Step 2: 运行确认通过（后端已正确）**

```bash
pytest tests/web/test_business_closure.py::test_transfer_changes_options_balance_immediately -q
```

预期：PASS。该测试固定“后端账本正确”的事实，防止后续把余额问题误判到后端。

- [ ] **Step 3: 前端补刷新**

`web/assets/app.js` 的 `refreshAfterExecution` 末尾：

```js
  if (typeof refreshHomeBalance === "function") {
    await refreshHomeBalance().catch(() => {});
  }
```

`refreshHomeBalance` 已读取 `/api/options` 与 `/api/cards`，会同时刷新“账户余额合计 / 活期可用 / 卡片日限额”三个数字。

- [ ] **Step 4: 卡片操作结果加一句口径**

`cardFlowServerCard()` 或卡片执行结果区追加：

```html
<p class="muted card-flow__note">卡片操作不涉及账户资金；账户余额只在转账、理财申赎、冻结/释放等资金动作后变化。</p>
```

- [ ] **Step 5: 手工验证**

```bash
$env:AIB_DISABLE_LLM=1; python -m ai_banking_copilot
```

浏览器打开 `http://127.0.0.1:8000`：
1. 对话“转账给李娜 100 元”并确认 → 不刷新页面，首页“账户余额合计”应减少 100；
2. 对话“把卡片日限额降到 1000 元”并确认 → 首页“卡片日限额”应更新；
3. 卡片挂失/补卡 → 余额数字不变，卡片结果区出现“不涉及账户资金”说明。

- [ ] **Step 6: 提交**

```bash
git add src/ai_banking_copilot/web/assets/app.js tests/web/test_business_closure.py
git commit -m "fix(web): 对话执行后刷新首页余额卡，并说明卡片操作不动资金"
```

---

### Task 8：门禁、E2E 与修复台账

**Files:**
- Modify: `scripts/e2e_check.py`
- Create: `docs/2026-11-02/实测截图2-修复记录.md`
- Modify: `docs/已修复清单-2026-10-08.md`（追加一条本轮台账，可选）

- [ ] **Step 1: E2E 增加 4 条回归**

在 `scripts/e2e_check.py` 的 `run()` 末尾追加（每条用独立 `session_id`）：

```python
# 1) 本月/上月账期不同
this_month = _post(base, "/api/submit", {
    "session_id": f"{session_id}-bill-this", "utterance": "查一下本月账单",
})[1]
last_month = _post(base, "/api/submit", {
    "session_id": f"{session_id}-bill-last", "utterance": "查一下上月账单",
})[1]
assert "2026-10" in json.dumps(this_month, ensure_ascii=False)
assert "2026-09" in json.dumps(last_month, ensure_ascii=False)

# 2) 代扣签约被能力边界拒绝
sign = _post(base, "/api/submit", {
    "session_id": f"{session_id}-sign", "utterance": "代扣签约",
})[1]
assert sign.get("state") == "blocked"
assert "不能代您新签" in str(sign.get("message") or "")

# 3) 表单 new_intent 不被旧澄清劫持
_pending = _post(base, "/api/submit", {
    "session_id": f"{session_id}-mix", "utterance": "转账 300 元",
})[1]
replace = _post(base, "/api/submit", {
    "session_id": f"{session_id}-mix",
    "utterance": "申请补卡",
    "new_intent": True,
    "selected": {
        "card": "card_debit",
        "address": "上海市浦东新区世纪大道 100 号",
        "mail_method": "standard",
        "replace": "yes",
    },
})[1]
assert replace.get("state") == "awaiting_confirm"
assert replace["card"]["nodes"][0]["card_id"] == "card_debit"
```

- [ ] **Step 2: 跑全部门禁**

```bash
cd d:/ai-banking/ai-banking-copilot
ruff check .
mypy
pytest
```

预期：`All checks passed` / `Success: no issues found` / 全量通过。

- [ ] **Step 3: 起服务跑页面自检**

```bash
$env:AIB_DISABLE_LLM=1; python -m ai_banking_copilot
# 另一个终端
python scripts/check_served_page.py
python scripts/e2e_check.py
python scripts/check_design_sync.py
```

预期：`ALL OK` / `ALL PASSED` / 样式无漂移。若 `check_served_page.py` 因 `app.js?v=91` 不一致失败，先确认 Task 4 的版本号已同步。

- [ ] **Step 4: 写修复记录**

创建 `docs/2026-11-02/实测截图2-修复记录.md`，至少包含：

```markdown
# 实测截图2 修复记录（2026-11-02）

## 结论
- 7 项问题全部复现并修复；后端账本本身正确，余额问题为首页刷新缺失。
- 代扣签约按能力边界下线，改为代扣协议查询/解约。

## 逐项
| 截图 | 根因 | 修复 | 回归 |
...

## 门禁输出
- ruff:
- mypy:
- pytest:
- check_served_page:
- e2e_check:
- check_design_sync:

## 残留
- 不支持预约限额/临时限额到期恢复；
- 补卡未采集收件人姓名/手机号；
- subscription.sign 处理器保留作内部能力，Agent 不可达。
```

- [ ] **Step 5: 提交**

```bash
git add scripts/e2e_check.py docs/2026-11-02/实测截图2-修复记录.md
git commit -m "test: 补实测截图2 回归与修复记录"
```

---

## 3. 验收清单

- [ ] “下周三出差，把卡片日限额降到 1000 元”只出现一组字段（卡片/通道/额度），消息明确“即时生效、不支持预约”；卡片候选不含已挂失信用卡。
- [ ] 输入“代扣签约”返回能力边界说明，不出现商户/金额/周期/扣款日表单；“查我的代扣协议”返回协议列表；“代扣解约”进入关闭扣款协议流程。
- [ ] “我的借记卡丢了”先问临时冻结/正式挂失；选择后计划只有 1 步；点“打开操作确认”能弹出对应确认弹窗。
- [ ] 先制造“转账缺收款人”澄清，再提交补卡表单：返回补卡计划，不再追问收款人。
- [ ] “查一下本月账单”显示 2026-10；“查一下上月账单”显示 2026-09。
- [ ] 对话完成转账/调额后，不刷新页面，首页余额合计/卡片日限额同步变化。
- [ ] `ruff` / `mypy` / `pytest` / `check_served_page.py` / `e2e_check.py` / `check_design_sync.py` 全绿。

---

## 4. 风险与回滚

| 风险 | 处理 |
|---|---|
| 代扣签约下线影响既有评测用例（`subscription.sign` 相关） | 保留动作/处理器与内部测试；只改模板与会话入口。若评测仍断言“签约可执行”，改为断言能力边界并记录口径变更。 |
| L5 五步改单步，旧测试与文档描述不一致 | 同步改 `LifeEventPrototype.description/skeleton`、`docs/2026-11-02/实测截图2-修复记录.md` 与受影响的 `tests/scenarios/test_life_events.py`、`tests/web/test_card_flow.py`。 |
| `new_intent` 默认 false，可能漏改其他表单 | 先覆盖卡片弹窗；场景页/理财表单逐个补 `new_intent: true`，用 E2E 断言“旧澄清不劫持表单”。 |
| 卡片候选过滤过严导致无卡可选 | 候选为空时前端回落全部卡片，服务端仍按状态机拒绝；不静默换卡。 |
| 首页刷新增加一次 `/api/options` 请求 | 仅执行后触发，演示规模可接受；后续可复用 `state.options` 做优化。 |

**回滚方式：** 每个任务一个 commit，按任务粒度 `git revert <commit>`；无数据库 schema 变更，无契约变更。

---

## 5. 共享文件与协作说明

| 文件 | 归属/约束 | 说明 |
|---|---|---|
| `planning/llm.py` | AGENTS 第 3 节按域分段共享 | 只改代扣/卡片域模板段，请段主复核。 |
| `web/app.py` | 项目规则“先申请再改” | 只改 `_SubmitRequest` 与 `subscription_capabilities` 两段。 |
| `web/assets/app.js` | 按场景块认领 | 只改卡片弹窗、澄清控件、首页刷新段。 |
| `web/page.py` | 先申请再改 | 只改场景⑤文案与 `app.js?v`。 |
| `tools/catalog.py` / `policy/**` / `planning/kernel.py` | 冻结 | **本方案不改**。 |
| `contracts/` | 禁止改动 | 不改。 |

无 `tools/catalog.py` 变更申请卡；若实施中发现必须给 `subscription.sign` 加字段，立即停止并走 5 行变更申请卡。

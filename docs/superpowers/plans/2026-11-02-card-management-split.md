# 卡片管理拆分 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把卡片页从「单一挂失 + 单一日限额」改造成「临时冻结 / 正式挂失 / 补卡 / 分卡限额」，并让所有卡片写操作在弹窗内完成强验证。

**Architecture:** 卡表扩展可空列 + 三张事件表；`cards` 场景承担状态机校验与计划组装；`/api/cards` 返回服务端计算的 `allowed_actions`；前端只渲染服务端计划与验证码；对话侧保留计划卡但确认按钮改为打开同一个弹窗。

**Tech Stack:** Python 3.12 / FastAPI / SQLite / 原生 JS + `<dialog>` / pytest（本轮只写不跑）

配套 spec：`docs/superpowers/specs/2026-11-02-card-management-split-design.md`

---

## Task 1: 卡片数据模型与迁移

**Files:**
- Modify: `src/ai_banking_copilot/gateway/models.py:67-78`
- Modify: `src/ai_banking_copilot/gateway/store.py:20-53, 86-94, 183-193`
- Modify: `src/ai_banking_copilot/gateway/facts.py:392-403`
- Modify: `src/ai_banking_copilot/gateway/seed.py:41-46, 203-210`
- Test: `tests/gateway/test_card_metadata.py`

- [ ] **Step 1: 写失败测试**

```python
from ai_banking_copilot.gateway.models import Card, Money


def test_card_carries_display_and_limit_facts() -> None:
    card = Card(
        card_id="card_debit", user_id="u", card_type="debit", status="locked",
        daily_limit=Money(2_000_000), last4="4471", brand="unionpay",
        expiry_month=9, expiry_year=2029, account_class="Ⅰ",
        atm_limit=Money(500_000), transfer_limit=Money(2_000_000),
        purchase_limit=Money(2_000_000), overseas_limit=Money(500_000),
        temp_lock_until="2026-11-05", lock_reason="cant_find",
    )
    assert card.brand == "unionpay"
    assert card.account_class == "Ⅰ"
    assert card.atm_limit == Money(500_000)
    assert card.temp_lock_until == "2026-11-05"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/gateway/test_card_metadata.py -q`
Expected: FAIL `TypeError: unexpected keyword argument 'brand'`
（用户要求本轮不执行 pytest；执行时可跳过本步，但测试文件必须先落。）

- [ ] **Step 3: 扩展 `Card`**

```python
@dataclass(frozen=True)
class Card:
    card_id: str
    user_id: str
    card_type: str
    status: str
    daily_limit: Money
    account_id: str | None = None
    last4: str | None = None
    brand: str | None = None
    expiry_month: int | None = None
    expiry_year: int | None = None
    account_class: str | None = None
    atm_limit: Money | None = None
    transfer_limit: Money | None = None
    purchase_limit: Money | None = None
    overseas_limit: Money | None = None
    credit_limit: Money | None = None
    temp_credit_limit: Money | None = None
    temp_credit_until: str | None = None
    cycle_limit: Money | None = None
    temp_lock_until: str | None = None
    lock_reason: str | None = None
    lost_at: str | None = None
    loss_reason: str | None = None
    replaced_by_card_id: str | None = None
```

- [ ] **Step 4: 迁移列与新表**

`store.py` 的 `_OPTIONAL_COLUMNS["cards"]` 追加全部可空列；`SCHEMA_VERSION = 8`；`SCHEMA_SQL` 追加 `card_status_events` / `card_replacement_requests` / `card_limit_applications` 三张表。

- [ ] **Step 5: 读取层与种子**

`facts._card` 逐列 `_optional_text` / `_optional_money`；`seed._CARDS` 扩为带 brand / 有效期 / 账户类别 / 四通道限额 / 信用额度的 7 列以上插入，`INSERT OR IGNORE` 不覆盖演示中的改动。

- [ ] **Step 6: 提交**

```bash
git add src/ai_banking_copilot/gateway/models.py src/ai_banking_copilot/gateway/store.py src/ai_banking_copilot/gateway/facts.py src/ai_banking_copilot/gateway/seed.py tests/gateway/test_card_metadata.py
git commit -m "feat(cards): 扩展卡片展示与分卡限额字段"
```

---

## Task 2: CoreBank 写原语

**Files:**
- Modify: `src/ai_banking_copilot/gateway/core_bank.py:520-601`
- Test: `tests/gateway/test_card_primitives.py`

- [ ] **Step 1: 写失败测试**

```python
def test_replace_card_marks_old_card_and_issues_new_one(core_bank) -> None:
    receipt = core_bank.replace_card(
        card_id="card_debit", address="上海市浦东新区演示路 1 号",
        mail_method="express", with_report=True, idempotency_key="replace-1",
        occurred_on="2026-11-02",
    )
    assert receipt.status == "issued"
    old = core_bank.store.query("SELECT * FROM cards WHERE card_id = 'card_debit'")[0]
    assert old["status"] == "lost"
    assert old["replaced_by_card_id"]
```

- [ ] **Step 2: 实现原语**

```python
def replace_card(self, *, card_id: str, address: str, mail_method: str,
                 with_report: bool, idempotency_key: str,
                 occurred_on: str,
                 before_write: BeforeWrite | None = None) -> WriteReceipt:
    if mail_method not in ("standard", "express"):
        raise CoreBankError(f"非法邮寄方式：{mail_method}")
    if not address.strip():
        raise CoreBankError("补卡地址不得为空")

    def body(conn: sqlite3.Connection) -> WriteReceipt:
        row = conn.execute(
            "SELECT card_id, card_type, last4, status, replaced_by_card_id"
            " FROM cards WHERE card_id = ?", (card_id,),
        ).fetchone()
        if row is None:
            raise UnknownCardError(card_id)
        if row["replaced_by_card_id"]:
            raise CoreBankError("该卡已补卡，不能重复申请")
        if with_report:
            conn.execute(
                "UPDATE cards SET status = 'lost', lost_at = ?, loss_reason = 'other'"
                " WHERE card_id = ?", (occurred_on, card_id),
            )
        new_card_id = _effect_id("card", "replace", idempotency_key)
        new_last4 = f"{(abs(hash(idempotency_key)) % 9000) + 1000}"
        fee = 2_000 if mail_method == "express" else 1_000
        conn.execute(
            "INSERT INTO cards(card_id, user_id, card_type, status, daily_limit_cents,"
            " account_id, last4, brand, expiry_month, expiry_year)"
            " SELECT ?, user_id, card_type, 'active', daily_limit_cents, account_id, ?,"
            " brand, expiry_month, expiry_year FROM cards WHERE card_id = ?",
            (new_card_id, new_last4, card_id),
        )
        conn.execute(
            "INSERT INTO card_replacement_requests(request_id, card_id, new_card_id, address,"
            " mail_method, fee_cents, eta_on, status, created_on)"
            " VALUES(?, ?, ?, ?, ?, ?, ?, 'issued', ?)",
            (f"replace_{idempotency_key}", card_id, new_card_id, address.strip(),
             mail_method, fee, occurred_on, occurred_on),
        )
        conn.execute(
            "UPDATE cards SET replaced_by_card_id = ? WHERE card_id = ?",
            (new_card_id, card_id),
        )
        return WriteReceipt(
            idempotency_key=idempotency_key, action="replace_card", status="issued",
            record_ids=(f"cards:{card_id}", f"cards:{new_card_id}"),
            effect_ids=(f"cards:{new_card_id}",),
        )

    return self._run_once("replace_card", idempotency_key, body, before_write=before_write)
```

- [ ] **Step 3: `set_card_status` 支持冻结到期与原因**

```python
def set_card_status(self, *, card_id: str, status: str, idempotency_key: str,
                    temp_lock_until: str | None = None, reason: str | None = None,
                    occurred_on: str | None = None,
                    before_write: BeforeWrite | None = None) -> WriteReceipt:
```

`status == "locked"` 时写 `temp_lock_until`/`lock_reason`；`status == "lost"` 时写 `lost_at`/`loss_reason`；同时向 `card_status_events` 插一条事件。

- [ ] **Step 4: 额度原语**

- `set_credit_limit(card_id, mode, amount, duration_months, occurred_on, idempotency_key)`：`temporary` 写 `temp_credit_limit_cents` / `temp_credit_until`；`permanent` 只写 `card_limit_applications(status='pending')`，不改额度。
- `set_cycle_limit(card_id, amount, idempotency_key)`：写 `cycle_limit_cents`。
- `set_card_limit` 增加 `limit_kind`，按通道写对应列；`purchase` 同时回写 `daily_limit_cents`。

- [ ] **Step 5: 提交**

```bash
git add src/ai_banking_copilot/gateway/core_bank.py tests/gateway/test_card_primitives.py
git commit -m "feat(cards): 新增补卡与分卡限额写原语"
```

---

## Task 3: 动作目录、三色与槽位

**Files:**
- Modify: `src/ai_banking_copilot/tools/catalog.py:268-353`
- Modify: `src/ai_banking_copilot/policy/pdp.py:62-90`
- Modify: `src/ai_banking_copilot/planning/slots.py:101-120, 422-510`
- Modify: `src/ai_banking_copilot/planning/llm.py:180-196, 296-302`
- Modify: `src/ai_banking_copilot/web/plan_card.py:46-60`
- Test: `tests/tools/test_card_specs.py`

- [ ] **Step 1: `_CARDS_SPECS` 新增 3 个动作**

```python
ToolSpec(
    action="card.replace", risk=RiskClass.SMALL_CHANGE, kind=ActionKind.STATE,
    params=(
        ParamSpec("card_id", SlotKind.CONTROL, str, slot_name="card"),
        ParamSpec("address", SlotKind.DATA, str),
        ParamSpec("mail_method", SlotKind.CONTROL, str, slot_name="mail_method"),
        ParamSpec("with_report", SlotKind.CONTROL, str, slot_name="replace"),
    ),
    is_pure_query=False, reversible=False, money_param=None,
    description="补卡（换新卡号；可选同时正式挂失）",
),
```

`card.adjust_credit_limit`、`card.set_cycle_limit` 同理声明；`card.lock` 增加 `duration_days`（`slot_name="lock_days"`）与 `reason`（DATA）。

- [ ] **Step 2: PDP**

```python
OFFICIAL_RED_ACTIONS = frozenset({
    "wealth.subscribe", "wealth.redeem", "card.unlock", "card.set_limit",
    "card.replace", "card.adjust_credit_limit",
    "subscription.sign", "transfer.request_collection", "password.change",
})
```

- [ ] **Step 3: 槽位提示**

在 `_PROMPTS` 增加 `lock_days` / `lock_reason` / `loss_reason` / `replace` / `mail_method` / `address` / `limit_kind` / `credit_mode` / `duration_months`。可选槽在文内做关键词归一（如「加急」→`express`、「临时」→`temporary`）。

- [ ] **Step 4: 口语模板**

```python
(re.compile(r"临时冻结|先冻|口头挂失"), "card.lock", "命中『临时冻结』模板"),
(re.compile(r"正式挂失|永久挂失"), "card.report_lost", "命中『正式挂失』模板"),
(re.compile(r"补卡|换卡"), "card.replace", "命中『补卡』模板"),
(re.compile(r"临时额度|临时提额"), "card.adjust_credit_limit", "命中『临时额度』模板"),
(re.compile(r"固定额度|永久额度"), "card.adjust_credit_limit", "命中『固定额度』模板"),
(re.compile(r"账单周期|周期消费限额"), "card.set_cycle_limit", "命中『账单周期限额』模板"),
```

删除 `挂失 → card.report_lost` 与 `额度|限额 → card.set_limit` 的一刀切规则；「卡丢了」改为先追问（`needs_clarification`），不再直接出计划。

- [ ] **Step 5: 计划卡中文名**

`plan_card.py` 的 `_ACTION_LABELS` 增加 `card.replace`（补卡）、`card.adjust_credit_limit`（调整信用卡额度）、`card.set_cycle_limit`（设置账单周期消费限额）。

- [ ] **Step 6: 提交**

```bash
git add src/ai_banking_copilot/tools/catalog.py src/ai_banking_copilot/policy/pdp.py src/ai_banking_copilot/planning/slots.py src/ai_banking_copilot/planning/llm.py src/ai_banking_copilot/web/plan_card.py tests/tools/test_card_specs.py
git commit -m "feat(cards): 新增补卡与信用卡额度动作及槽位"
```

---

## Task 4: 场景状态机与处理器

**Files:**
- Modify: `src/ai_banking_copilot/scenarios/cards.py`
- Test: `tests/scenarios/test_cards_state_machine.py`

- [ ] **Step 1: 入口校验**

```python
_ALLOWED = {
    "card.lock": {"active": ("locked",)},
    "card.unlock": {"locked": ("active",)},
    "card.report_lost": {"active": ("lost",), "locked": ("lost",)},
    "card.replace": {"active": ("active", "lost"), "lost": ("lost",)},
}
```

- `card.lock`：`duration_days` 必须 5–15；已 `lost` 拒绝；
- `card.unlock`：仅 `locked`；`lost` 拒绝「挂失不可逆」；
- `card.report_lost`：`reason` ∈ `lost/stolen/other`；
- `card.replace`：`mail_method` ∈ `standard/express`；地址非空；已 `replaced_by` 拒绝；
- `card.adjust_credit_limit`：仅信用卡；`mode` ∈ `temporary/permanent`；
- `card.set_cycle_limit`：仅信用卡；
- `card.set_limit`：仅借记卡，`limit_kind` ∈ `atm/transfer/purchase/overseas`。

- [ ] **Step 2: 写处理器**

```python
WRITE_HANDLERS = {
    "card.lock": _card_lock_handler,
    "card.unlock": _card_status_handler("active"),
    "card.report_lost": _card_report_lost_handler,
    "card.replace": _card_replace_handler,
    "card.set_limit": _set_limit_handler,
    "card.adjust_credit_limit": _credit_limit_handler,
    "card.set_cycle_limit": _cycle_limit_handler,
    "card.apply_virtual": _apply_virtual_handler,
    "password.change": _change_password_handler,
}
```

- [ ] **Step 3: 提交**

```bash
git add src/ai_banking_copilot/scenarios/cards.py tests/scenarios/test_cards_state_machine.py
git commit -m "feat(cards): 按状态机校验冻结/挂失/补卡/调额"
```

---

## Task 5: `/api/cards` 返回 `allowed_actions`

**Files:**
- Modify: `src/ai_banking_copilot/web/app.py:1084-1105`
- Test: `tests/web/test_cards_allowed_actions.py`

- [ ] **Step 1: 写测试**

```python
def test_lost_card_only_offers_replace_and_detail(client) -> None:
    data = client.get("/api/cards").json()
    lost = next(item for item in data["cards"] if item["status"] == "lost")
    assert set(lost["allowed_actions"]) == {"replace", "view_lost_detail"}
```

- [ ] **Step 2: 实现**

```python
def _allowed_card_actions(card: Card) -> list[str]:
    status = card.status
    if card.card_type == "credit":
        limits = ["adjust_credit_limit", "set_cycle_limit"]
    else:
        limits = ["adjust_debit_limit"]
    table = {
        "active": ["temp_lock", *limits, "report_lost", "replace", "apply_virtual"],
        "locked": ["unlock", "report_lost", "view_lock_detail"],
        "lost": ["replace", "view_lost_detail"],
    }
    return table.get(status, [])
```

同时返回 `masked_no`、`brand_label`、`expiry`、`limits`、`temp_lock`、`lost`、`replacement`、`synced_at`；`effective_status` 在 `locked` 到期时按 `active` 返回。

- [ ] **Step 3: 提交**

```bash
git add src/ai_banking_copilot/web/app.py tests/web/test_cards_allowed_actions.py
git commit -m "feat(cards): 服务端下发状态允许动作与脱敏卡信息"
```

---

## Task 6: 卡片页卡面与使用概览

**Files:**
- Modify: `src/ai_banking_copilot/web/page.py:388-403`
- Modify: `src/ai_banking_copilot/web/assets/app.js:2001-2116`
- Modify: `src/ai_banking_copilot/web/assets/workspace.css`
- Test: `scripts/check_served_page.py`（结构自检）

- [ ] **Step 1: 卡面**

`cardFaceHtml` 改为读 `allowed_actions` + `masked_no` + `brand_label` + `expiry` + `limits`；临时冻结显示倒计时；已挂失显示原因与补卡信息。

- [ ] **Step 2: 概览**

删除 `donutChartHtml` 调用，`cardsVisualHtml` 改名 `cardsOverviewHtml`，输出每卡一行文字概览（状态 / 卡组织 / 账户类别 / 限额摘要 / 倒计时），不生成任何金额。

- [ ] **Step 3: 文案**

顶部说明与同步时间按 spec §7.2 落地。

- [ ] **Step 4: 提交**

```bash
git add src/ai_banking_copilot/web/page.py src/ai_banking_copilot/web/assets/app.js src/ai_banking_copilot/web/assets/workspace.css
git commit -m "feat(web): 重做卡片卡面与状态概览"
```

---

## Task 7: 卡片操作弹窗与对话收口

**Files:**
- Modify: `src/ai_banking_copilot/web/page.py`（新增 `<dialog id="card-flow-dialog">`）
- Modify: `src/ai_banking_copilot/web/assets/app.js`
- Modify: `src/ai_banking_copilot/web/assets/workspace.css`

- [ ] **Step 1: 弹窗骨架**

三步容器 + 底部主按钮；`data-card-flow-step` 切换；结果页复用 `scene-reply` 样式。

- [ ] **Step 2: 提交与验证**

```javascript
async function cardFlowSubmit() {
  const result = await post("/api/submit", { utterance, selected });
  const card = result.data.card;
  if (card.level === "RED") {
    const challenge = await post("/api/verify/request", {});
    renderChallenge(challenge.data);
    return;
  }
  renderSummary(card);
}
```

最终确认：

```javascript
const confirmed = await post("/api/confirm", {
  card_id: card.card_id, frozen_hash: card.frozen_hash, verification_code: code,
});
```

- [ ] **Step 3: 对话侧按钮**

`sceneCardHtml` 与对话计划卡：卡片域（`card.*`）的按钮文案改为「打开操作确认」，点击调用 `openCardFlowDialog(card)`；非卡片域保持原样。

- [ ] **Step 4: 提交**

```bash
git add src/ai_banking_copilot/web/page.py src/ai_banking_copilot/web/assets/app.js src/ai_banking_copilot/web/assets/workspace.css
git commit -m "feat(web): 卡片操作弹窗内完成强验证与二次确认"
```

---

## Task 8: 评测用例与静态门禁

**Files:**
- Modify: `src/ai_banking_copilot/eval/data/cases.json`
- Modify: `src/ai_banking_copilot/eval/data/web_cases.json`
- Modify: `tests/**`

- [ ] **Step 1: 评测用例**

新增：`cards-temp-lock-yellow`、`cards-report-lost-red`、`cards-replace-red`、`cards-credit-temp-limit-red`、`cards-credit-cycle-yellow`、`cards-debit-atm-limit-red`、`cards-lost-unlock-refused`。

- [ ] **Step 2: 静态门禁**

Run: `ruff check .`
Expected: All checks passed

Run: `mypy`
Expected: Success: no issues found

Run: `python scripts/check_served_page.py`（需先起服务）
Expected: ALL OK

**不执行 pytest**（用户明确要求）。

- [ ] **Step 3: 提交**

```bash
git add src/ai_banking_copilot/eval/data/cases.json src/ai_banking_copilot/eval/data/web_cases.json tests
git commit -m "test(cards): 补卡片状态机、补卡与额度用例"
```

---

## 自检

- [ ] spec 第 3 节状态机 → Task 1 / 4
- [ ] spec 第 4 节动作集 → Task 3
- [ ] spec 第 5 节数据模型 → Task 1
- [ ] spec 第 6 节接口 → Task 5
- [ ] spec 第 7 节页面 → Task 6
- [ ] spec 第 8 节弹窗 → Task 7
- [ ] spec 第 9 节 AI 对话 → Task 3 / 7
- [ ] spec 第 10 节异常 → Task 4 / 7
- [ ] spec 第 12 节优先级 → 全部
- [ ] spec 第 13 节测试 → Task 8

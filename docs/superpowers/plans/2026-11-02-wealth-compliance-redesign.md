# 理财合规改造实施计划（2026-11-02）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. Steps use `- [ ]` checkboxes.

**Goal:** 按 `docs/superpowers/specs/2026-11-02-wealth-compliance-redesign-design.md` 落地产品列表、申购/赎回确认弹窗、适当性合规、AI 问答与交易分层。

**Architecture:** 不改 25 个业务动作、不改 `contracts/` 与 PEP 三条硬约束。产品披露、测评事件、交易留痕、客户年龄各自独立建表；持仓表增份额列；AI 只出草案，交易统一走现有 `/api/submit → /api/verify/request → /api/confirm`。

**Tech Stack:** Python 3.14 / FastAPI / SQLite / 原生 JS / pytest / ruff / mypy

---

## 文件结构

**新增**

- `src/ai_banking_copilot/gateway/wealth_disclosure_store.py`：披露表 DDL、播种、读写。
- `src/ai_banking_copilot/gateway/wealth_trade_store.py`：交易留痕表 DDL、读写。
- `src/ai_banking_copilot/gateway/customer_profile_store.py`：出生年份表 DDL、读写。
- `src/ai_banking_copilot/web/wealth_docs.py`：四份演示文件文本与版本。
- `src/ai_banking_copilot/web/assets/wealth_trade.js`：申购/赎回确认弹窗。
- `tests/gateway/test_wealth_disclosure.py`
- `tests/gateway/test_wealth_shares.py`
- `tests/web/test_risk_retake_regulatory.py`
- `tests/web/test_wealth_trade_ack.py`
- `tests/web/test_wealth_redemption_quote.py`

**修改**

- `gateway/models.py`：新增 `WealthDisclosure` / `RiskAssessmentEvent` / `WealthTradeAck` / `CustomerSuitabilityProfile`，不改 `WealthProduct`。
- `gateway/seed.py`：播种披露、客户出生年份。
- `gateway/risk_store.py`：2 次/日、8 次/12 个月、事件表、变更确认。
- `gateway/core_bank.py`：持仓份额列、按份额申购/赎回。
- `gateway/facts.py`：披露/事件/留痕/客户属性读取方法。
- `scenarios/wealth.py`：披露合并、孰高、匹配意见、份额换算。
- `web/app.py`：overview/detail/quote/ack 路由、测评路由。
- `web/session.py`：问答扩展、赎回反查、`redemption_prefill`。
- `web/triage.py`：理财澄清候选句。
- `web/present.py`：份额事实行。
- `planning/slots.py`：`shares` 槽与提示。
- `planning/llm.py`：赎回份额口述模板。
- `tools/catalog.py`：`wealth.redeem` 可选 `share_bp`。
- `policy/pdp.py`：`wealth.redeem` 加入红色集合。
- `compose.py` / `eval/pipeline.py`：初始化新表与播种。
- `web/assets/app.js`：产品/持仓卡、等级条、计划卡 CTA。
- `web/page.py`：两个 dialog + 脚本。
- `web/assets/workspace.css`：弹窗样式。
- `web/assets/sw.js`：缓存版本。

---

## Task 1: 披露、留痕、客户属性、测评事件四张表

**Files:**
- Create: `src/ai_banking_copilot/gateway/wealth_disclosure_store.py`
- Create: `src/ai_banking_copilot/gateway/wealth_trade_store.py`
- Create: `src/ai_banking_copilot/gateway/customer_profile_store.py`
- Modify: `src/ai_banking_copilot/gateway/models.py`
- Modify: `src/ai_banking_copilot/gateway/risk_store.py`
- Modify: `src/ai_banking_copilot/gateway/seed.py`
- Modify: `src/ai_banking_copilot/compose.py`
- Modify: `src/ai_banking_copilot/eval/pipeline.py`
- Test: `tests/gateway/test_wealth_disclosure.py`

- [ ] **Step 1: 写失败测试**

```python
def test_every_seeded_product_has_complete_disclosure(store):
    init_wealth_disclosure_schema(store)
    seed_wealth_disclosures(store)
    rows = wealth_disclosures(store)
    assert len(rows) == 5
    for row in rows:
        d = row.payload
        assert d.subscription_fee_bp is not None
        assert d.service_fee_bp is not None
        assert d.custody_fee_bp is not None
        assert d.registration_code
        assert d.issuer_risk_level in {"R1", "R2", "R3", "R4", "R5"}
        assert d.seller_risk_level in {"R1", "R2", "R3", "R4", "R5"}
        assert d.increment_cents > 0
        assert d.redeem_mode in {"realtime", "t_plus_1", "t_plus_3", "closed"}


def test_higher_risk_rating_wins():
    d = WealthDisclosure(
        product_id="wp_mix", product_type="mixed", registration_code="DEMO-003",
        issuer="演示发行方", issuer_risk_level="R2", seller_risk_level="R3",
        subscription_fee_bp=100, service_fee_bp=40, custody_fee_bp=2,
        increment_cents=1000000, redeem_mode="closed", fast_redeem_limit_cents=None,
        closed_days=180, next_open_date=date(2027, 4, 29), purchase_cutoff="15:00",
        sale_status="on_sale", updated_on=DEFAULT_BOOKING_DATE,
    )
    assert d.display_risk_level == "R3"
    assert d.disclosure_complete is True


def test_missing_fee_means_not_saleable():
    d = _disclosure(service_fee_bp=None)
    assert d.disclosure_complete is False
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/gateway/test_wealth_disclosure.py -v`
Expected: FAIL with `ImportError: cannot import name 'init_wealth_disclosure_schema'`

- [ ] **Step 3: 实现 model**

在 `gateway/models.py` 追加（不改 `WealthProduct`）：

```python
@dataclass(frozen=True)
class WealthDisclosure:
    product_id: str
    product_type: str
    registration_code: str
    issuer: str
    issuer_risk_level: str
    seller_risk_level: str
    subscription_fee_bp: int | None
    service_fee_bp: int | None
    custody_fee_bp: int | None
    increment_cents: int
    redeem_mode: str
    fast_redeem_limit_cents: int | None
    closed_days: int
    next_open_date: date | None
    purchase_cutoff: str
    sale_status: str
    updated_on: date

    @property
    def display_risk_level(self) -> str:
        order = {"R1": 1, "R2": 2, "R3": 3, "R4": 4, "R5": 5}
        return max((self.issuer_risk_level, self.seller_risk_level), key=lambda x: order.get(x, 0))

    @property
    def disclosure_complete(self) -> bool:
        return (
            self.sale_status == "on_sale"
            and bool(self.registration_code)
            and self.subscription_fee_bp is not None
            and self.service_fee_bp is not None
            and self.custody_fee_bp is not None
            and self.increment_cents > 0
            and self.redeem_mode in {"realtime", "t_plus_1", "t_plus_3", "closed"}
        )


@dataclass(frozen=True)
class RiskAssessmentEvent:
    event_id: str
    user_id: str
    event_on: date
    risk_profile: str
    score: int
    source: str
    previous_profile: str | None


@dataclass(frozen=True)
class WealthTradeAck:
    ack_id: str
    user_id: str
    product_id: str
    action: str
    doc_versions: dict[str, str]
    read_seconds: dict[str, int]
    declaration_text: str | None
    declaration_hash: str | None
    dividend_method: str | None
    auto_redeem_pref: bool | None
    account_id: str | None
    created_at: str


@dataclass(frozen=True)
class CustomerSuitabilityProfile:
    user_id: str
    birth_year: int | None
```

- [ ] **Step 4: 实现三张新表的 DDL 与播种**

`wealth_disclosure_store.py`：

```python
WEALTH_DISCLOSURE_SCHEMA_SQL = """..."""  # 按 spec 9.1 原文

_DISCLOSURES = (
    # product_id, type, code, issuer, issuer_r, seller_r, sub_fee, svc_fee, custody, increment, mode, fast_limit, closed, next_open, cutoff
    ("wp_cash", "cash", "DEMO-Z7000001", "演示理财公司", "R1", "R1", 0, 20, 2, 100, "realtime", 1000000, 0, None, "15:00"),
    ("wp_short", "bond", "DEMO-Z7000002", "演示理财公司", "R2", "R2", 0, 30, 2, 100000, "closed", None, 30, date(2026, 11, 30), "15:00"),
    ("wp_bond", "bond", "DEMO-Z7000003", "演示理财公司", "R2", "R2", 0, 30, 2, 100000, "closed", None, 90, date(2026, 12, 30), "15:00"),
    ("wp_mix", "mixed", "DEMO-Z7000004", "演示理财公司", "R2", "R3", 100, 40, 2, 1000000, "closed", None, 180, date(2027, 4, 29), "15:00"),
    ("wp_equity", "equity", "DEMO-Z7000005", "演示理财公司", "R4", "R4", 120, 50, 2, 1000000, "closed", None, 365, date(2027, 10, 31), "15:00"),
)
```

提供 `init_wealth_disclosure_schema(store)`、`seed_wealth_disclosures(store)`、`wealth_disclosures(store)`、`wealth_disclosure(store, product_id)`、`verify_disclosure(store, record_id, content_hash)`。

`wealth_trade_store.py`：按 spec 9.3 建表，提供 `init_wealth_trade_schema`、`save_trade_ack`、`trade_acks_of`。

`customer_profile_store.py`：按 spec 9.4 建表，提供 `init_customer_profile_schema`、`seed_customer_profiles`（`u_demo -> 1990`）、`customer_profile(store, user_id)`。

`risk_store.py` 追加：

```python
ASSESSMENT_EVENTS_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS risk_assessment_events (
    event_id         TEXT PRIMARY KEY,
    user_id          TEXT NOT NULL,
    event_on         TEXT NOT NULL,
    risk_profile     TEXT NOT NULL,
    score            INTEGER NOT NULL,
    source           TEXT NOT NULL,
    previous_profile TEXT
);
"""

def init_risk_schema(store):
    ...  # 保留原有
    conn.executescript(ASSESSMENT_EVENTS_SCHEMA_SQL)

def record_assessment_event(store, user_id, event_on, risk_profile, score, source, previous_profile=None) -> None:
    with store.transaction() as conn:
        conn.execute(
            "INSERT INTO risk_assessment_events(event_id, user_id, event_on, risk_profile, score, source, previous_profile)"
            " VALUES(?, ?, ?, ?, ?, ?, ?)",
            (f"rae_{uuid4().hex}", user_id, event_on.isoformat(), risk_profile, score, source, previous_profile),
        )

def assessment_events_since(store, user_id, since):
    return store.query(
        "SELECT * FROM risk_assessment_events WHERE user_id = ? AND event_on > ? ORDER BY event_on",
        (user_id, since.isoformat()),
    )
```

- [ ] **Step 5: 在组合根初始化**

`compose.py` 与 `eval/pipeline.py` 在 `init_risk_schema` 附近追加：

```python
init_wealth_disclosure_schema(writer)
seed_wealth_disclosures(writer)
init_wealth_trade_schema(writer)
init_customer_profile_schema(writer)
seed_customer_profiles(writer)
```

- [ ] **Step 6: 运行测试**

Run: `pytest tests/gateway/test_wealth_disclosure.py -v`
Expected: PASS

---

## Task 2: 测评 2 次/日、8 次/12 个月、结果变更再确认

**Files:**
- Modify: `src/ai_banking_copilot/gateway/risk_store.py`
- Modify: `src/ai_banking_copilot/web/app.py:1128-1198, 2683-2720`
- Test: `tests/web/test_risk_retake_regulatory.py`

- [ ] **Step 1: 写失败测试**

```python
def test_daily_limit_is_two(client, writer):
    for _ in range(2):
        assert client.post("/api/risk-assessment", json={"answers": _all_d_answers()}).status_code == 200
    res = client.post("/api/risk-assessment", json={"answers": _all_d_answers()})
    assert res.status_code == 429
    assert res.json()["retake"]["next_available_on"] == "2026-11-01"


def test_twelve_month_limit_is_eight(client, writer):
    for day in range(8):
        with writer.transaction() as conn:
            conn.execute("DELETE FROM risk_assessment_attempts")
        writer.save(DEMO_USER_ID, "R2", DEFAULT_BOOKING_DATE, source="questionnaire")
        record_assessment_event(writer, DEMO_USER_ID, DEFAULT_BOOKING_DATE, "R2", 8, "questionnaire")
    res = client.post("/api/risk-assessment", json={"answers": _all_d_answers()})
    assert res.status_code == 429
    assert res.json()["retake"]["remaining_12m"] == 0


def test_changed_result_requires_reconfirmation(client, writer):
    save_risk_assessment(writer, DEMO_USER_ID, "R3", DEFAULT_BOOKING_DATE)
    res = client.post("/api/risk-assessment", json={"answers": _all_a_answers()})
    body = res.json()
    assert body["state"] == "needs_reconfirmation"
    assert body["previous_profile"] == "R3"
    assert writer.attempts_on(DEMO_USER_ID, DEFAULT_BOOKING_DATE) == 0
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/web/test_risk_retake_regulatory.py -v`
Expected: FAIL（daily limit 仍为 3、无 `needs_reconfirmation`）

- [ ] **Step 3: 改 `risk_store.py`**

```python
RETAKES_PER_DAY = 2
RETAKES_PER_12M = 8

def retake_payload(used_today, used_12m, on_date, earliest_event_on=None):
    next_day = on_date + timedelta(days=1)
    next_12m = earliest_event_on + timedelta(days=365) if earliest_event_on else next_day
    if used_today >= RETAKES_PER_DAY:
        next_available = next_day
    elif used_12m >= RETAKES_PER_12M:
        next_available = next_12m
    else:
        next_available = next_day
    return {
        "limit_per_day": RETAKES_PER_DAY,
        "limit_per_12m": RETAKES_PER_12M,
        "used_today": used_today,
        "used_12m": used_12m,
        "remaining_today": max(RETAKES_PER_DAY - used_today, 0),
        "remaining_12m": max(RETAKES_PER_12M - used_12m, 0),
        "next_available_on": next_available.isoformat(),
        "basis": "regulatory",
        "basis_label": "《金融机构产品适当性管理办法》第二十九条",
        "demo_note": "演示环境按模拟业务日计数",
    }
```

`_retake_payload` 读取 `assessment_events_since(deps.today - timedelta(days=365))` 与最早事件日期。

- [ ] **Step 4: 改 `POST /api/risk-assessment`**

在计数检查后、落库前：

```python
latest = to_risk_assessment(deps.risk.assessment_for(user_id))
changed = latest is not None and latest.risk_profile != result.risk_profile
if changed and not request.confirm_change:
    return {
        "state": "needs_reconfirmation",
        "message": f"本次结果 {result.risk_profile}，与最近一次 {latest.risk_profile} 不一致；请再次确认后提交。",
        "previous_profile": latest.risk_profile,
        "result": {"risk_profile": result.risk_profile, "score": result.score, "max_score": result.max_score},
        "retake": quota,
    }
writer.record_attempt(user_id, deps.today)
writer.save(user_id, result.risk_profile, deps.today, source=SOURCE_QUESTIONNAIRE)
record_assessment_event(writer, user_id, deps.today, result.risk_profile, result.score, SOURCE_QUESTIONNAIRE, latest.risk_profile if latest else None)
```

`_RiskAssessmentRequest` 增 `confirm_change: bool = False`。

- [ ] **Step 5: 测评 payload 增有效期提醒**

```python
days_until_expiry = (expires_on - deps.today).days
"valid_days": assessment.valid_days,
"days_until_expiry": days_until_expiry,
"expiring_soon": 0 <= days_until_expiry <= 30,
```

- [ ] **Step 6: 运行测试**

Run: `pytest tests/web/test_risk_retake_regulatory.py -v`
Expected: PASS

---

## Task 3: 披露合并、孰高、匹配意见、产品目录

**Files:**
- Modify: `src/ai_banking_copilot/scenarios/wealth.py`
- Modify: `src/ai_banking_copilot/web/app.py:1200-1228`
- Test: `tests/web/test_wealth_overview.py`

- [ ] **Step 1: 写失败测试**

```python
def test_overview_has_disclosure_and_suitability(client):
    data = client.get("/api/wealth/overview").json()
    by_id = {p["product_id"]: p for p in data["products"]}
    assert by_id["wp_bond"]["fees"]["service"] == "0.30%"
    assert by_id["wp_bond"]["registration_code"].startswith("DEMO-")
    assert by_id["wp_bond"]["suitability"]["conclusion"]
    assert by_id["wp_bond"]["risk_level"] == "R2"


def test_higher_rating_is_displayed(client):
    data = client.get("/api/wealth/overview").json()
    mix = next(p for p in data["products"] if p["product_id"] == "wp_mix")
    assert mix["issuer_risk_level"] == "R2"
    assert mix["seller_risk_level"] == "R3"
    assert mix["risk_level"] == "R3"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/web/test_wealth_overview.py -v`
Expected: FAIL（无 `fees` / `suitability`）

- [ ] **Step 3: 在 `scenarios/wealth.py` 增加披露合并**

```python
def disclosure_payload(d: WealthDisclosure | None) -> dict[str, object]:
    if d is None:
        return {"disclosure_complete": False, "sale_status": "unavailable", "reason": "未披露"}
    return {
        "product_type": d.product_type,
        "registration_code": d.registration_code,
        "registration_note": "演示编码，未在官方登记",
        "issuer_risk_level": d.issuer_risk_level,
        "seller_risk_level": d.seller_risk_level,
        "risk_basis": f"发行方 {d.issuer_risk_level} / 销售方 {d.seller_risk_level}，按孰高展示 {d.display_risk_level}",
        "fees": {
            "subscription": _fee_text(d.subscription_fee_bp),
            "service": _fee_text(d.service_fee_bp),
            "custody": _fee_text(d.custody_fee_bp),
        },
        "increment_yuan": str(Money(d.increment_cents).yuan),
        "liquidity": _liquidity_payload(d),
        "disclosure_complete": d.disclosure_complete,
        "sale_status": d.sale_status if d.disclosure_complete else "unavailable",
    }
```

`product_catalog` 增加参数 `disclosures: Mapping[str, WealthDisclosure]`，逐只合并；可买判定：

```python
complete = d is not None and d.disclosure_complete
eligible = complete and limit is not None and _RISK_ORDER.get(d.display_risk_level, 99) <= limit
reason_code = (
    "DISCLOSURE_INCOMPLETE" if not complete
    else "NOT_ASSESSED" if limit is None
    else "RISK_MISMATCH" if not eligible
    else ""
)
```

`suitability_payload(product, d, risk_profile, eligible)` 返回 spec 7.5 的对象；主要风险按 `product_type` 映射。

- [ ] **Step 4: 改 `wealth_overview`**

```python
disclosures = {r.payload.product_id: r.payload for r in deps.gateway.wealth_disclosures()}
product_catalog(products, risk_profile, nav=..., disclosures=disclosures)
```

- [ ] **Step 5: 运行测试**

Run: `pytest tests/web/test_wealth_overview.py -v`
Expected: PASS

---

## Task 4: 份额字段、核心银行按份额赎回、赎回升红

**Files:**
- Modify: `src/ai_banking_copilot/gateway/store.py`（迁移入口）
- Modify: `src/ai_banking_copilot/gateway/core_bank.py:419-459, 833-925`
- Modify: `src/ai_banking_copilot/tools/catalog.py:247-261`
- Modify: `src/ai_banking_copilot/planning/slots.py`
- Modify: `src/ai_banking_copilot/planning/llm.py`
- Modify: `src/ai_banking_copilot/policy/pdp.py:62-71`
- Modify: `src/ai_banking_copilot/scenarios/wealth.py`
- Modify: `src/ai_banking_copilot/web/present.py`
- Test: `tests/gateway/test_wealth_shares.py`

- [ ] **Step 1: 写失败测试**

```python
def test_subscribe_writes_shares(gateway, core_bank):
    receipt = core_bank.subscribe_product(
        user_id=DEMO_USER_ID, account_id="acc_savings", product_id="wp_bond",
        amount=Money(100000), idempotency_key="sub-1",
    )
    holding = _holding_row(gateway, "wp_bond")
    assert holding["share_bp"] > 0
    assert holding["nav_at_purchase_bp"] > 0


def test_redeem_by_shares(core_bank, gateway):
    before = _holding_row(gateway, "wp_bond")
    shares = before["share_bp"] // 2
    receipt = core_bank.redeem_product(
        user_id=DEMO_USER_ID, account_id="acc_savings", product_id="wp_bond",
        shares=shares, idempotency_key="red-1",
    )
    after = _holding_row(gateway, "wp_bond")
    assert after["share_bp"] == before["share_bp"] - shares
    assert after["amount_cents"] < before["amount_cents"]
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/gateway/test_wealth_shares.py -v`
Expected: FAIL（`no such column: share_bp`）

- [ ] **Step 3: 迁移持仓表**

新增 `init_wealth_share_schema(store)`（可放在 `wealth_disclosure_store.py` 或独立模块），在组合根调用：

```python
with store.transaction() as conn:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(wealth_holdings)")}
    if "share_bp" not in columns:
        conn.execute("ALTER TABLE wealth_holdings ADD COLUMN share_bp INTEGER NOT NULL DEFAULT 0")
    if "nav_at_purchase_bp" not in columns:
        conn.execute("ALTER TABLE wealth_holdings ADD COLUMN nav_at_purchase_bp INTEGER NOT NULL DEFAULT 0")
    for row in conn.execute("SELECT product_id, user_id, amount_cents, purchased_on FROM wealth_holdings WHERE share_bp = 0"):
        nav_bp = _nav_bp_on(conn, row["product_id"], row["purchased_on"]) or 10000
        share_bp = int(row["amount_cents"]) * 1_000_000 // nav_bp
        conn.execute(
            "UPDATE wealth_holdings SET share_bp = ?, nav_at_purchase_bp = ? WHERE product_id = ? AND user_id = ?",
            (share_bp, nav_bp, row["product_id"], row["user_id"]),
        )
```

- [ ] **Step 4: 改核心银行**

`subscribe_product`/`redeem_product` 增可选 `shares: int | None = None`。`_wealth_flow`：

```python
nav_bp = _nav_bp_on(conn, product_id, DEFAULT_BOOKING_DATE) or 10000
if direction == "out":
    share_bp = amount.cents * 1_000_000 // nav_bp
    holding_after = holding_before + amount.cents
    shares_after = holding_shares + share_bp
    conn.execute("... share_bp = ?, nav_at_purchase_bp = ? ...", (...))
else:
    if shares is None:
        share_bp = amount.cents * 1_000_000 // nav_bp
    else:
        share_bp = shares
        if share_bp == holding_shares:
            amount = Money(holding_before)
        else:
            amount = Money(share_bp * nav_bp // 1_000_000)
    if share_bp > holding_shares:
        raise InsufficientHoldingError(...)
    if amount.cents > holding_before:
        raise InsufficientHoldingError(...)
    conn.execute("UPDATE wealth_holdings SET amount_cents = ?, share_bp = ? WHERE ...", (...))
```

金额入账仍用 `amount.cents`；持仓减 `amount.cents` 与 `share_bp` 同步。

- [ ] **Step 5: 目录与政策**

`tools/catalog.py` 的 `wealth.redeem` 增：

```python
ParamSpec("share_bp", SlotKind.CONTROL, int, required=False, slot_name="shares"),
```

`policy/pdp.py` 把 `wealth.redeem` 加入 `OFFICIAL_RED_ACTIONS`，注释写“赎回是资金回流与持仓扣减，演示环境按红色处理”。

`planning/slots.py`：

```python
_PROMPTS["shares"] = "请给出赎回份额（份）。"
_SHARES = re.compile(r"(\d+(?:\.\d+)?)\s*份")

def _resolve_shares(inputs, notes):
    match = _SHARES.search(inputs.text)
    if not match:
        return None
    value = Decimal(match.group(1))
    return int(value * 10_000)
```

在 `fill_control_slots` 里对 `wealth.redeem` 处理 `shares` 槽；不追问时保留空。

`planning/llm.py` 加模板：`赎回 ([\d.]+) 份 (.+)` → `wealth.redeem` + `shares`。

`scenarios/wealth.py` 的 `_build_wealth_node`：

```python
shares = _optional_int_slot(intent, "shares")
if shares is not None:
    holding = _holding(intent, gateway)
    nav_bp = _latest_nav_bp(gateway, product.product_id)
    amount_cents = holding.amount.cents if shares >= holding.share_bp else shares * nav_bp // 1_000_000
    params["share_bp"] = min(shares, holding.share_bp)
    params["amount_cents"] = amount_cents
```

`web/present.py` 增：

```python
"wealth.redeem": (("amount_cents", "预计金额（元）"), ("shares_display", "赎回份额（份）")),
"wealth.subscribe": (("amount_cents", "申购金额（元）"),),
```

- [ ] **Step 6: 运行测试**

Run: `pytest tests/gateway/test_wealth_shares.py tests/planning/test_slots.py -v`
Expected: PASS（旧用例按金额赎回仍可用）

---

## Task 5: 产品详情、赎回试算、交易留痕接口

**Files:**
- Create: `src/ai_banking_copilot/web/wealth_docs.py`
- Modify: `src/ai_banking_copilot/web/app.py`
- Test: `tests/web/test_wealth_trade_ack.py`
- Test: `tests/web/test_wealth_redemption_quote.py`

- [ ] **Step 1: 写失败测试**

```python
def test_product_detail_returns_four_demo_docs(client):
    data = client.get("/api/wealth/products/wp_bond").json()
    assert set(data["documents"]) == {"prospectus", "risk_disclosure", "investor_rights", "sales_agreement"}
    assert all(doc["is_demo"] is True for doc in data["documents"].values())


def test_ack_rejects_short_reading(client):
    res = client.post("/api/wealth/acknowledgements", json={
        "action": "subscribe", "product_id": "wp_bond", "account_id": "acc_savings",
        "doc_versions": {"prospectus": "v1", "risk_disclosure": "v1", "investor_rights": "v1", "sales_agreement": "v1"},
        "read_seconds": {"prospectus": 1, "risk_disclosure": 1, "investor_rights": 1, "sales_agreement": 1},
    })
    assert res.status_code == 400
    assert "阅读" in res.json()["detail"]


def test_quote_for_matured_bond_is_redeemable(client):
    data = client.get("/api/wealth/redemption-quote", params={"product_id": "wp_bond", "mode": "all"}).json()
    assert data["redeemable"] is True
    assert data["arrival"]["mode"] == "t_plus_1"
    assert data["large_redemption"] is True
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/web/test_wealth_trade_ack.py tests/web/test_wealth_redemption_quote.py -v`
Expected: 404

- [ ] **Step 3: 实现 `wealth_docs.py`**

四份文档，每份：

```python
{"title": "产品说明书", "version": "demo-v1", "is_demo": True, "body": "演示文本，非正式法律文件。..."}
```

内容覆盖：产品类型、风险等级、费用、流动性、风险提示、投资者权利、销售协议要点。

- [ ] **Step 4: 实现三个路由**

`GET /api/wealth/products/{product_id}`：产品 + 披露 + `documents` + `first_purchase`（持仓是否存在）+ `suitability` + `customer_age` / `senior_notice`。

`GET /api/wealth/redemption-quote`：按 spec 6 计算，返回 `redeemable/reason/shares/amount_yuan/nav/arrival/fast_redeem/large_redemption/position`。

`POST /api/wealth/acknowledgements`：校验四份文件、阅读秒数（3 秒；首次购买 R3+ 的风险揭示书 15 秒）、抄录完全一致；写 `wealth_trade_acks`；返回 `ack_id`。

- [ ] **Step 5: 运行测试**

Run: `pytest tests/web/test_wealth_trade_ack.py tests/web/test_wealth_redemption_quote.py -v`
Expected: PASS

---

## Task 6: AI 问答与赎回反查

**Files:**
- Modify: `src/ai_banking_copilot/web/session.py`
- Modify: `src/ai_banking_copilot/web/triage.py`
- Test: `tests/web/test_triage_wealth.py`

- [ ] **Step 1: 写失败测试**

```python
def test_fee_question_answers_from_facts(client):
    text = _say(client, "稳利债券 A 的费率是多少")
    assert "销售服务费" in text
    assert "0.30%" in text
    assert "演示" in text


def test_matured_redeem_resolves_holding(client):
    data = _submit(client, "赎回那笔到期的理财")
    assert data["redemption_prefill"]["product_id"] == "wp_bond"
    assert data["redemption_prefill"]["mode"] == "all"
    assert data["state"] != "executed"


def test_mismatch_has_no_subscribe_button(client):
    text = _say(client, "买 1000 元 均衡配置 B")
    assert "重新测评" in text
    assert "立即申购" not in text
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/web/test_triage_wealth.py -v`
Expected: FAIL（无费率直答、无 prefill）

- [ ] **Step 3: 扩展 `_wealth_fact_answer`**

新增正则与分支：

```python
_FEE_QUESTION = re.compile(r"费率|手续费|服务费|托管费|管理费")
_ARRIVAL_QUESTION = re.compile(r"多久到账|什么时候到账|到账时间|T\+\d")
_WHY_NOT_BUY = re.compile(r"为什么不能买|怎么才能买|买不了|不能申购")
_RETAKE_QUESTION = re.compile(r"还能测几次|测评次数|什么时候过期|有效期")
_CODE_QUESTION = re.compile(r"登记编码|产品编码|怎么验真|理财网")
_LIQUIDITY_QUESTION = re.compile(r"封闭期|开放日|能赎回吗|流动性")
```

每个分支只从 `gateway.wealth_disclosures()` / `_risk_payload` / `holdings_payload` 取事实，不生成数字。

- [ ] **Step 4: 赎回反查**

在意图装配前：

```python
def _resolve_redeem_holding(utterance, deps):
    rows = holdings_payload(deps.gateway, deps.user_id)
    if "到期" in utterance:
        matched = [r for r in rows if r["maturity_on"] and r["maturity_on"] <= deps.today.isoformat()]
    else:
        matched = [r for r in rows if r["name"] in utterance]
    if len(matched) == 1:
        return {"product_id": matched[0]["product_id"], "name": matched[0]["name"], "mode": "all",
                "shares": matched[0]["shares"], "amount_yuan": matched[0]["amount_yuan"],
                "maturity_on": matched[0]["maturity_on"], "account_id": "acc_savings"}
    return None
```

`SubmissionView` 末尾增 `redemption_prefill: dict[str, object] | None = None`；`_submission_payload` 透出。

- [ ] **Step 5: 不匹配双路径**

`blocked_suitability_message` 追加：

```
可以：① 重新测评；② 查看与你当前等级匹配的产品。未展示申购入口。
```

- [ ] **Step 6: 运行测试**

Run: `pytest tests/web/test_triage_wealth.py -v`
Expected: PASS

---

## Task 7: 前端产品列表、等级条、计划卡 CTA

**Files:**
- Modify: `src/ai_banking_copilot/web/assets/app.js:486-997, 5361-5700`
- Test: `tests/web/test_wealth_page_contract.py`

- [ ] **Step 1: 写失败测试**

```python
def test_page_has_trade_dialog_and_script():
    html = client.get("/").text
    assert 'id="wealth-trade"' in html
    assert 'id="wealth-product-detail"' in html
    assert "/assets/wealth_trade.js" in html
```

- [ ] **Step 2: 改 `wealthProductHtml`**

删除金额输入与直接申购；输出：

```html
<div class="wealth-row__act">
  <button data-wealth-detail="wp_bond">产品详情</button>
  <button data-wealth-subscribe="wp_bond" data-requires-login>立即申购</button>
</div>
```

不可买：只显示“为什么不能买”折叠区，含等级/有效期/三条路径；披露不完整显示“暂不可售（信息披露不完整）”。

- [ ] **Step 3: 改 `wealthHoldingHtml`**

删除金额输入；赎回按钮改为 `data-wealth-redeem="wp_bond"`；显示份额、可赎回份额、到期日。

- [ ] **Step 4: 改等级条**

按 `retake.remaining_today` / `remaining_12m` / `next_available_on` 渲染；固定小字引用第 29 条与“演示环境按模拟业务日计数”。

- [ ] **Step 5: 计划卡 CTA**

在 `renderSceneCard` / chat card 渲染里，财富动作的确认按钮改为：

```html
<button data-wealth-open-trade>打开交易确认</button>
```

点击调用 `openWealthTradeSheet({mode, card})`。

- [ ] **Step 6: 运行页面契约测试**

Run: `pytest tests/web/test_wealth_page_contract.py -v`
Expected: PASS

---

## Task 8: 交易确认弹窗

**Files:**
- Create: `src/ai_banking_copilot/web/assets/wealth_trade.js`
- Modify: `src/ai_banking_copilot/web/page.py:346-385, 593`
- Modify: `src/ai_banking_copilot/web/assets/workspace.css`
- Modify: `src/ai_banking_copilot/web/assets/sw.js`

- [ ] **Step 1: 写骨架**

`openWealthTradeSheet(options)` 渲染四步：

1. 产品与适当性（摘要、匹配意见、不可买三路径）；
2. 交易信息（账户、金额/份额、分红、类型字段、65+ 提示）；
3. 文件与声明（四份文件、阅读计时、抄录）；
4. 验证与确认（`/api/verify/request`、验证码、二次确认摘要）。

- [ ] **Step 2: 金额/份额校验**

```js
function validateAmount(value, product) {
  const amount = Number(value);
  if (!Number.isFinite(amount) || amount <= 0) return "请输入大于 0 的金额";
  if (amount < Number(product.min_amount_yuan)) return `低于起购金额 ${product.min_amount_yuan} 元，请重新输入`;
  const inc = Number(product.increment_yuan || 0);
  if (inc > 0 && Math.round(amount * 100) % Math.round(inc * 100) !== 0) return `必须为 ${product.increment_yuan} 元的整数倍`;
  return "";
}
```

- [ ] **Step 3: 提交顺序**

```js
await post("/api/wealth/acknowledgements", ackPayload);
const submit = await post("/api/submit", { utterance, selected: { product: productId, account: accountId } });
if (submit.data.state === "blocked") return showBlocked(submit.data);
const card = submit.data.card;
if (card.requires.includes("strong_verify")) {
  const challenge = await post("/api/verify/request", {});
  showCodeInput(challenge.data);
}
await post("/api/confirm", { card_id: card.card_id, frozen_hash: card.frozen_hash, verification_code });
```

- [ ] **Step 4: 赎回弹窗**

三种模式；调用 `/api/wealth/redemption-quote` 获取份额/金额/到账/快赎/大额；大额预警在二次确认前显示；封闭期产品禁用。

- [ ] **Step 5: 页面与缓存**

`page.py` 增加两个 `<dialog>` 与脚本；`sw.js` 版本 `v78`，加 `/assets/wealth_trade.js?v=1`，`app.js?v=84`，`workspace.css?v=55`。

- [ ] **Step 6: 手工验证**

启动服务后打开理财页：产品卡无金额框；“立即申购”打开弹窗；赎回从持仓打开；四件套未读满不能勾选；验证码输入后出回执。

---

## Task 9: 测试补齐与门禁

- [ ] 新增/更新 `tests/web/test_wealth_overview.py`、`test_wealth_page_contract.py`、`test_triage_wealth.py`。
- [ ] `ruff check .`
- [ ] `mypy`
- [ ] `pytest`
- [ ] 启动服务后 `python scripts/check_served_page.py`、`python scripts/e2e_check.py`、`python scripts/check_design_sync.py`

---

## Task 10: 文档与诚实性口径

- [ ] 在 spec 的“已知边界”基础上，页面固定标注：
  - 演示编码未在官方登记；
  - 模拟净值/份额，以 T 日净值为准；
  - 演示验证码，不接真实密码/生物识别；
  - 双录/回访未接入；
  - 自动赎回仅记录偏好；
  - 测评 12 个月按 365 天实现，演示环境按模拟业务日计数。
- [ ] 更新 `docs/superpowers/specs/2026-11-02-wealth-compliance-redesign-design.md` 的“实施状态”一节（完成后）。

---

## 自检

- 覆盖 spec 的 5 个模块与 P0/P1/P2。
- 每个后端任务都有失败测试与通过命令。
- 类型名一致：`WealthDisclosure.display_risk_level`、`share_bp`、`redemption_prefill`、`needs_reconfirmation`。
- 没有新增业务动作；没有改 `contracts/`、`policy/gate.py`、`policy/pep.py` 三条硬约束。

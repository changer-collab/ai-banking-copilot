# 代扣协议管理重构实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把「订阅管理」重构为「代扣协议管理」：以协议为主对象，关闭动作真实终止本行放款，补齐累计扣款、涨价、解约后对账，并让页面与 AI 问答、账单线索共用同一套事实口径。

**Architecture:** 复用 `subscriptions` 表与 `subscription.cancel` 写链路，在 `scenarios/subscriptions.py` 扩展 `SubscriptionInsight` 为协议视图（`DebitAgreement` 的字段由扩展后的 insight 承载，不新增并行数据类、不新增业务动作）；`/api/options` 增加 `subscription_details.summary` 与协议字段；前端重写订阅场景渲染，新增协议卡、筛选、解约弹窗、红色验证与账单深链；`policy/pdp.py` 把 `subscription.cancel` 升为强制红色。

**Tech Stack:** Python 3.11+ / FastAPI / SQLite / 原生 JS + CSS / 无第三方前端框架。

**硬约束（用户明确要求）：**
- **永远不要运行 `pytest`**。本计划中的测试文件只写/只改，不执行。
- 可运行：`ruff check .`、`mypy`、`python -m compileall`、起服务后的页面走查。
- 不新增业务动作、不接真实商户取消接口、不发送真实短信。
- 协议编号 / 类型 / 演示历史扣款必须标注「演示派生」。

---

## 文件结构

| 文件 | 责任 |
|---|---|
| `src/ai_banking_copilot/gateway/store.py` | `subscriptions.cancelled_on` 可空列与迁移 |
| `src/ai_banking_copilot/gateway/models.py` | `Subscription.cancelled_on` |
| `src/ai_banking_copilot/gateway/facts.py` | `cancelled_on` 行映射 |
| `src/ai_banking_copilot/gateway/core_bank.py` | 解约时写入 `cancelled_on` |
| `src/ai_banking_copilot/gateway/seed.py` | 音乐/云盘演示扣款流水；订阅商户跳过年度缩放 |
| `src/ai_banking_copilot/scenarios/subscriptions.py` | 协议视图、累计/涨价/顺延/对账、payload、边界文案 |
| `src/ai_banking_copilot/web/app.py` | `/api/options` 新字段、解约回执对账、动作中文名 |
| `src/ai_banking_copilot/web/plan_card.py` | 动作名、协议对象行 |
| `src/ai_banking_copilot/web/page.py` | 页面结构、弹窗与静态文案 |
| `src/ai_banking_copilot/web/assets/app.js` | 协议卡/概览/筛选/详情/解约/红色验证/账单深链 |
| `src/ai_banking_copilot/web/assets/workspace.css` | 协议卡、涨价条、对账行、筛选、验证面板 |
| `src/ai_banking_copilot/tools/catalog.py` | `subscription.cancel` 描述 |
| `src/ai_banking_copilot/policy/pdp.py` | 强制红色集合 |
| `src/ai_banking_copilot/planning/llm.py` | 口述触发词 |
| `src/ai_banking_copilot/web/session.py` | 动作中文名、商户取消边界话术 |
| `src/ai_banking_copilot/web/triage.py` | 订阅边界话术与示例句 |
| `src/ai_banking_copilot/eval/data/*.json` | 取消用例预期等级（只改不跑） |
| `tests/**/test_subscription*.py` | 与实现同步的测试（只写不跑） |

---

## Task 1: Gateway 增加解约日期

**Files:**
- Modify: `src/ai_banking_copilot/gateway/store.py:40-64`
- Modify: `src/ai_banking_copilot/gateway/models.py:256-267`
- Modify: `src/ai_banking_copilot/gateway/facts.py:469-480`
- Modify: `src/ai_banking_copilot/gateway/core_bank.py:280-326`

- [ ] **Step 1: 给 subscriptions 加可空列**

`store.py` 的 `_OPTIONAL_COLUMNS["subscriptions"]` 改为：

```python
    "subscriptions": (("card_id", "TEXT"), ("cancelled_on", "TEXT")),
```

`CREATE TABLE IF NOT EXISTS subscriptions` 同步加列（新库直接建好，旧库靠上面的迁移补）：

```sql
    card_id         TEXT REFERENCES cards(card_id),
    cancelled_on    TEXT
```

- [ ] **Step 2: 模型加字段**

```python
@dataclass(frozen=True)
class Subscription:
    subscription_id: str
    user_id: str
    merchant: str
    amount: Money
    period: str
    next_charge_on: date
    status: str
    card_id: str | None = None
    cancelled_on: date | None = None
    """解约业务日；`status=cancelled` 时用于对账，`None` = 解约日期未知。"""
```

- [ ] **Step 3: facts 映射**

```python
    def _subscription(self, row: object) -> Record[Subscription]:
        payload = Subscription(
            ...
            card_id=_optional_text(row["card_id"]),
            cancelled_on=(
                date.fromisoformat(str(row["cancelled_on"]))
                if row["cancelled_on"] else None
            ),
        )
```

- [ ] **Step 4: 解约写日期**

`cancel_subscription` 改为：

```python
        row = conn.execute(
            "SELECT status, cancelled_on FROM subscriptions"
            " WHERE subscription_id = ? AND user_id = ?",
            (subscription_id, user_id),
        ).fetchone()
        ...
        if previous_status == "cancelled":
            return WriteReceipt(
                ...
                previous_status=previous_status,
            )
        cancelled_on = DEFAULT_BOOKING_DATE.isoformat()
        changed = conn.execute(
            "UPDATE subscriptions SET status = 'cancelled', cancelled_on = ?"
            " WHERE subscription_id = ? AND user_id = ?",
            (cancelled_on, subscription_id, user_id),
        ).rowcount
```

重复解约保持 `already_cancelled`，不覆盖原解约日期。

- [ ] **Step 5: 静态验证（不跑 pytest）**

Run:
```powershell
python -m compileall -q src/ai_banking_copilot/gateway
python -m ruff check src/ai_banking_copilot/gateway
```
Expected: 编译无输出、ruff `All checks passed`。

---

## Task 2: 补音乐/云盘演示扣款流水

**Files:**
- Modify: `src/ai_banking_copilot/gateway/seed.py:111-183, 294-340`

- [ ] **Step 1: 订阅商户跳过年度缩放**

`_FIXED_MONTH_MERCHANTS` 改为：

```python
_FIXED_MONTH_MERCHANTS = frozenset({"视频会员", "音乐会员", "云盘会员"})
```

`_seeded_rows()` 内金额计算改为：

```python
        for (...) in _TXN_SPECS:
            fixed_price = merchant in _FIXED_MONTH_MERCHANTS
            legacy_cents = cents if fixed_price else cents * year_scale // 100
            amount_cents = legacy_cents
            if (
                not fixed_price
                and cents < 0
                and year == 2026
            ):
                amount_cents = legacy_cents * month_scale // 100
```

这修正了视频会员 2025 年被 85% 缩放导致的伪涨价。

- [ ] **Step 2: 追加订阅扣款行**

在 `_build_transactions()` 前新增：

```python
_MUSIC_FIRST_CENTS = -1000
_MUSIC_RAISED_CENTS = -1200
_MUSIC_RAISE_FROM = (2026, 10)
"""音乐会员 2025-01 起每月 10 元，2026-10 起 12 元：用于演示涨价与累计扣款。"""


def _recurring_charge_rows() -> tuple[tuple[object, ...], ...]:
    rows: list[tuple[object, ...]] = []
    for year, month in _seed_months():
        booked_on = date(year, month, 15).isoformat()
        amount = (
            _MUSIC_RAISED_CENTS
            if (year, month) >= _MUSIC_RAISE_FROM
            else _MUSIC_FIRST_CENTS
        )
        rows.append((
            f"txn_{year}{month:02d}15_music",
            DEMO_USER_ID,
            "acc_current",
            booked_on,
            amount,
            "音乐会员",
            "娱乐",
            "app",
            "out",
            f"{booked_on}T07:20:00",
            "杭州",
            "4471",
        ))
        if month == 3:
            booked_on = date(year, month, 1).isoformat()
            rows.append((
                f"txn_{year}{month:02d}01_cloud",
                DEMO_USER_ID,
                "acc_savings",
                booked_on,
                -19800,
                "云盘会员",
                "娱乐",
                "app",
                "out",
                f"{booked_on}T10:10:00",
                "杭州",
                "9021",
            ))
    return tuple(rows)


def _build_transactions() -> tuple[tuple[object, ...], ...]:
    return tuple(row for row, _ in _seeded_rows()) + _recurring_charge_rows()
```

- [ ] **Step 3: 已知影响（写进交付摘要）**

- 音乐会员每月 +¥10（2026-10 起 +¥12），云盘会员 2025-03 / 2026-03 各 +¥198。
- 这会改变账单分析的月度合计与趋势，属于「让账单页与协议页同源」的有意变更。
- 不运行 pytest；受影响的精确断言由用户后续验收。

- [ ] **Step 4: 静态验证**

```powershell
python -m compileall -q src/ai_banking_copilot/gateway
python -m ruff check src/ai_banking_copilot/gateway/seed.py
```

---

## Task 3: 场景层协议视图与事实计算

**Files:**
- Modify: `src/ai_banking_copilot/scenarios/subscriptions.py:195-235, 306-351, 427-489, 510-618, 621-684, 705-730`

- [ ] **Step 1: 新增常量与数据类**

在 `SubscriptionNotice` 后新增：

```python
AGREEMENT_ACTIVE = "active"
AGREEMENT_CANCELLED = "cancelled"
AGREEMENT_SUSPECTED = "suspected"

AGREEMENT_TYPE_LABELS = {
    "unionpay": "银联代扣",
    "netlink": "网联代扣",
    "collection_e": "代收e授权",
    "unknown": "类型未知",
}


@dataclass(frozen=True)
class PricePoint:
    amount_cents: int
    effective_on: date


@dataclass(frozen=True)
class PriceChange:
    from_cents: int
    to_cents: int
    effective_on: date
    percent: Decimal
```

- [ ] **Step 2: 新增派生函数**

```python
def agreement_no(subscription_id: str) -> str:
    from hashlib import sha256
    return "DD-" + sha256(subscription_id.encode("utf-8")).hexdigest()[:6].upper()


def agreement_type(card_type: str | None, *, registered: bool) -> str:
    if not registered:
        return "collection_e"
    if card_type == "debit":
        return "unionpay"
    if card_type == "credit":
        return "netlink"
    return "unknown"


def _price_history(items: Sequence[_Outflow]) -> tuple[PricePoint, ...]:
    points: list[PricePoint] = []
    for item in items:
        if not points or points[-1].amount_cents != item.amount_cents:
            points.append(PricePoint(item.amount_cents, item.booked_on))
    return tuple(points)


def _latest_price_change(history: tuple[PricePoint, ...]) -> PriceChange | None:
    if len(history) < 2:
        return None
    previous, latest = history[-2], history[-1]
    if latest.amount_cents <= previous.amount_cents:
        return None
    percent = (
        Decimal(latest.amount_cents - previous.amount_cents) * 100
        / Decimal(previous.amount_cents)
    ).quantize(Decimal("0.1"))
    return PriceChange(previous.amount_cents, latest.amount_cents, latest.effective_on, percent)


def _add_cycle(day: date, cycle: str) -> date:
    if cycle == CYCLE_MONTHLY:
        year, month = (day.year + 1, 1) if day.month == 12 else (day.year, day.month + 1)
        return date(year, month, min(day.day, monthrange(year, month)[1]))
    if cycle == CYCLE_YEARLY:
        year = day.year + 1
        return date(year, day.month, min(day.day, monthrange(year, day.month)[1]))
    return day + timedelta(days=30)


def _advance_until_future(day: date, cycle: str, as_of: date) -> tuple[date, int | None]:
    if day >= as_of:
        return day, None
    cursor = day
    guard = 0
    while cursor < as_of and guard < 120:
        cursor = _add_cycle(cursor, cycle)
        guard += 1
    return cursor, max((as_of - day).days, 0)
```

- [ ] **Step 3: 扩展 `SubscriptionInsight`**

新增字段：

```python
    agreement_no: str
    agreement_type: str
    agreement_type_label: str
    agreement_status: str
    signed_on: date | None
    cancelled_on: date | None
    charged_total_cents: int | None
    month_charged_cents: int
    price_history: tuple[PricePoint, ...]
    price_change: PriceChange | None
    next_charge_state: str
    overdue_days: int | None
    card_id: str | None
    card_tail: str | None
    post_cancel_charges: tuple[SubscriptionEvidence, ...]
```

- [ ] **Step 4: `_registered_insight` 填充新字段**

`_registered_insight` 的签名加 `card_type: str | None, card_tail: str | None`。在现有的
`period_label, charged_this_period = _current_period(...)` **之后**插入：

```python
    history = _price_history(items)
    price_change = _latest_price_change(history)
    charged_total = sum(item.amount_cents for item in items) if items else None
    month_charged = sum(
        item.amount_cents for item in items
        if item.booked_on.strftime("%Y-%m") == as_of.strftime("%Y-%m")
    )
    cancelled_on = subscription.cancelled_on
    post_cancel = tuple(
        _evidence(item) for item in items
        if cancelled_on is not None and item.booked_on > cancelled_on
    )
    if subscription.status != "active":
        next_state = "cancelled"
    elif overdue_days:
        next_state = "overdue"
    elif charged_this_period:
        next_state = "charged"
    elif next_charge_on == as_of:
        next_state = "due"
    elif next_charge_on:
        next_state = "scheduled"
    else:
        next_state = "unknown"
    agreement_status = (
        AGREEMENT_ACTIVE if subscription.status == "active" else AGREEMENT_CANCELLED
    )
```

`_next_charge` 的返回仍放在原位置；顺延逻辑：

```python
    raw_next, is_prediction, basis = _next_charge(
        subscription, items, cycle=cycle, median_gap=median_gap
    )
    next_charge_on, overdue_days = (
        _advance_until_future(raw_next, cycle, as_of) if raw_next else (None, None)
    )
    if raw_next and next_charge_on != raw_next:
        is_prediction = True
        basis = basis + "；登记日期已过，已按扣费周期顺延到未来日期。"
```

`period_label, charged_this_period = _current_period(...)` 保持在 `_next_charge` 之后、
新插入块之前，确保 `charged_this_period` 可用。

`SubscriptionInsight(...)` 增加：

```python
        agreement_no=agreement_no(subscription.subscription_id),
        agreement_type=agreement_type(card_type, registered=True),
        agreement_type_label=AGREEMENT_TYPE_LABELS[
            agreement_type(card_type, registered=True)
        ],
        agreement_status=agreement_status,
        signed_on=items[0].booked_on if items else None,
        cancelled_on=cancelled_on,
        charged_total_cents=charged_total,
        month_charged_cents=month_charged,
        price_history=history,
        price_change=price_change,
        next_charge_state=next_state,
        overdue_days=overdue_days,
        card_id=subscription.card_id,
        card_tail=card_tail,
        post_cancel_charges=post_cancel,
```

`evidence` 上限由 `items[-6:]` 改为 `items[-12:]`。`cancel_path` 文案改为：

```python
        cancel_path=(
            "本应用「代扣协议管理」→ 关闭扣款协议（关闭银行卡侧授权）"
            if active else "协议已解约，无需关闭"
        ),
```

- [ ] **Step 5: `_unregistered_insight` 填充新字段**

```python
    history = _price_history(items)
    price_change = _latest_price_change(history)
    next_on = (
        latest.booked_on + timedelta(days=median_gap) if median_gap is not None else None
    )
    next_on, overdue_days = (
        _advance_until_future(next_on, cycle, as_of) if next_on else (None, None)
    )
    month_charged = sum(
        item.amount_cents for item in items
        if item.booked_on.strftime("%Y-%m") == as_of.strftime("%Y-%m")
    )
```

`SubscriptionInsight(...)` 增加：

```python
        agreement_no="",
        agreement_type="collection_e",
        agreement_type_label=AGREEMENT_TYPE_LABELS["collection_e"],
        agreement_status=AGREEMENT_SUSPECTED,
        signed_on=items[0].booked_on,
        cancelled_on=None,
        charged_total_cents=sum(item.amount_cents for item in items),
        month_charged_cents=month_charged,
        price_history=history,
        price_change=price_change,
        next_charge_state=(
            "overdue" if overdue_days else "scheduled" if next_on else "unknown"
        ),
        overdue_days=overdue_days,
        card_id=None,
        card_tail=latest.card_tail,
        post_cancel_charges=(),
```

`next_charge_on=next_on`、`next_charge_is_prediction=True`，`cancel_action` 保持 `merchant_self_service`，`cancel_path` 改为：

```python
        cancel_path=(
            "未匹配到本行代扣协议；请到商户 App/官网的自动续费管理关闭，"
            "或联系发卡行核对是否来自其他卡"
        ),
```

- [ ] **Step 6: `subscription_report` 传卡信息**

在 `subscriptions = tuple(...)` 后：

```python
    cards = {
        record.payload.card_id: record.payload
        for record in gateway.cards_of(user_id)
    }
```

调用 `_registered_insight` 时：

```python
        card = cards.get(subscription.card_id) if subscription.card_id else None
        insights.append(_registered_insight(
            subscription, items, renamed_from=renamed_from, as_of=as_of,
            card_type=card.card_type if card else None,
            card_tail=card.last4 if card else None,
        ))
```

- [ ] **Step 7: payload 增加协议字段与 summary**

`insights_payload` 的每条 item 增加：

```python
                "agreement_id": item.insight_id,
                "agreement_no": item.agreement_no,
                "agreement_type": item.agreement_type,
                "agreement_type_label": item.agreement_type_label,
                "agreement_status": item.agreement_status,
                "card_id": item.card_id,
                "card_tail": item.card_tail,
                "signed_on": item.signed_on.isoformat() if item.signed_on else None,
                "cancelled_on": (
                    item.cancelled_on.isoformat() if item.cancelled_on else None
                ),
                "charged_total_cents": item.charged_total_cents,
                "month_charged_cents": item.month_charged_cents,
                "price_history": [
                    {
                        "amount_cents": point.amount_cents,
                        "effective_on": point.effective_on.isoformat(),
                    }
                    for point in item.price_history
                ],
                "price_change": (
                    {
                        "from_cents": item.price_change.from_cents,
                        "to_cents": item.price_change.to_cents,
                        "effective_on": item.price_change.effective_on.isoformat(),
                        "percent": str(item.price_change.percent),
                    }
                    if item.price_change else None
                ),
                "next_charge_state": item.next_charge_state,
                "overdue_days": item.overdue_days,
                "card_tail": item.card_tail,
                "post_cancel_charges": [
                    {...与 evidence 同结构...}
                    for evidence in item.post_cancel_charges
                ],
```

在返回字典中增加 `summary`：

```python
        "summary": _agreement_summary(report),
```

新增：

```python
def _agreement_summary(report: SubscriptionReport) -> dict[str, object]:
    as_of = report.as_of
    month_start = as_of.replace(day=1)
    month_end = as_of.replace(
        day=monthrange(as_of.year, as_of.month)[1]
    )
    active = [item for item in report.insights if item.agreement_status == AGREEMENT_ACTIVE]
    due = [
        item for item in active
        if item.next_charge_on is not None
        and as_of <= item.next_charge_on <= month_end
    ]
    next_30 = [
        item for item in active
        if item.next_charge_on is not None
        and as_of <= item.next_charge_on <= as_of + timedelta(days=30)
    ]
    return {
        "month_label": as_of.strftime("%Y-%m"),
        "month_charged_cents": sum(item.month_charged_cents for item in report.insights),
        "month_due_cents": sum(item.amount_cents for item in due),
        "next_30d_cents": sum(item.amount_cents for item in next_30),
        "active_count": len(active),
        "suspected_count": sum(
            1 for item in report.insights if item.agreement_status == AGREEMENT_SUSPECTED
        ),
        "cancelled_count": sum(
            1 for item in report.insights if item.agreement_status == AGREEMENT_CANCELLED
        ),
        "month_start": month_start.isoformat(),
    }
```

- [ ] **Step 8: 边界文案换人话**

```python
SUBSCRIPTION_CANCEL_BOUNDARY = (
    "本页关闭的是这张卡对该商户的扣款授权：关闭后银行不再放款；"
    "商户是否继续提供服务、是否停止计费，由商户决定。"
)

SUBSCRIPTION_CANCEL_BOUNDARY_SHORT = (
    "关闭后银行卡侧不再放款；商户是否继续服务由商户决定。"
)
```

- [ ] **Step 9: 静态验证**

```powershell
python -m compileall -q src/ai_banking_copilot/scenarios
python -m mypy src/ai_banking_copilot/scenarios/subscriptions.py
```
Expected: `Success: no issues found`。

---

## Task 4: API 暴露 summary 与解约对账

**Files:**
- Modify: `src/ai_banking_copilot/web/app.py:1056-1107, 3473-3496, 3499-3550`

- [ ] **Step 1: `/api/options` 直接透传新字段**

`subscription_details` 已经由 `insights_payload(...)` 生成，新字段会自动出现。`subscription_capabilities.boundary` 使用新的 `SUBSCRIPTION_CANCEL_BOUNDARY`，并补：

```python
                "close_action_label": "关闭扣款协议",
                "external_cancel_integrated": False,
                "can_close_bank_side": True,
```

- [ ] **Step 2: 解约回执补对账**

`_subscription_cancel_payload(view)` 改为 `_subscription_cancel_payload(view, gateway=None, user_id=None, as_of=None)`：

```python
def _subscription_cancel_payload(view, gateway=None, user_id=None, as_of=None):
    results = [item for item in view.exec_results if item.action == "subscription.cancel"]
    if not results:
        return None
    last = results[-1]
    state = cancel_state_from_receipt(
        status=last.status, external_status=last.external_status
    )
    payload = {
        "state": state,
        "label": CANCEL_STATE_LABELS[state],
        "external_status": last.external_status or EXTERNAL_CANCEL_NOT_INTEGRATED,
        "external_confirmed": state == CANCEL_STATE_EXTERNAL_CONFIRMED,
        "previous_status": last.previous_status,
        "message": last.message,
        "record_ids": list(last.record_ids),
        "effect_ids": list(last.effect_ids),
        "boundary": SUBSCRIPTION_CANCEL_BOUNDARY,
        "cancelled_on": None,
        "last_charge_on": None,
        "last_charge_cents": None,
        "post_cancel_charges": [],
        "sms_receipt": None,
    }
    if gateway is None or user_id is None or as_of is None:
        return payload
    subscription_id = next(
        (rid.split(":", 1)[1] for rid in last.record_ids
         if rid.startswith("subscriptions:")),
        None,
    )
    if subscription_id is None:
        return payload
    report = subscription_report(gateway, user_id, as_of=as_of)
    item = next(
        (row for row in report.insights if row.subscription_id == subscription_id),
        None,
    )
    if item is None:
        return payload
    subscription = gateway.subscription(subscription_id).payload
    payload.update({
        "cancelled_on": (
            subscription.cancelled_on.isoformat() if subscription.cancelled_on else None
        ),
        "last_charge_on": (
            item.last_charged_on.isoformat() if item.last_charged_on else None
        ),
        "last_charge_cents": item.amount_cents,
        "post_cancel_charges": [
            {
                "booked_on": charge.booked_on.isoformat(),
                "amount_cents": charge.amount_cents,
                "merchant_label": charge.merchant_label,
            }
            for charge in item.post_cancel_charges
        ],
        "sms_receipt": {
            "mode": "demo",
            "message": (
                f"【演示】您尾号 {item.card_tail or '----'} 的卡已关闭对"
                f"{item.merchant}的扣款协议，解约日期 "
                f"{subscription.cancelled_on.isoformat() if subscription.cancelled_on else as_of.isoformat()}。"
                "关闭后本行不再向该商户放款。"
            ),
        },
    })
    return payload
```

- [ ] **Step 3: `_execution_payload` 透传依赖**

```python
def _execution_payload(
    view: ExecutionView,
    gateway: FactGateway | None = None,
    user_id: str | None = None,
    as_of: date | None = None,
) -> dict[str, object]:
    ...
        "subscription_cancel": _subscription_cancel_payload(
            view, gateway, user_id, as_of
        ),
```

三个调用点改为：

```python
        return _response(session_id, user_id, _execution_payload(view, deps.gateway, user_id, deps.today))
```

（`request.session_id` 的两处同理。）

- [ ] **Step 4: 动作中文名**

`app.py:3716` 改为：

```python
    "subscription.cancel": "关闭扣款协议",
    "subscription.list": "代扣协议查询",
```

- [ ] **Step 5: 静态验证**

```powershell
python -m compileall -q src/ai_banking_copilot/web
python -m ruff check src/ai_banking_copilot/web/app.py
```

---

## Task 5: 升级为红色动作与话术

**Files:**
- Modify: `src/ai_banking_copilot/policy/pdp.py:62-95`
- Modify: `src/ai_banking_copilot/tools/catalog.py:435-447`
- Modify: `src/ai_banking_copilot/planning/llm.py:231-239`
- Modify: `src/ai_banking_copilot/web/session.py:234-235, 5026-5027`
- Modify: `src/ai_banking_copilot/web/triage.py:105-115, 169-176`
- Modify: `src/ai_banking_copilot/web/plan_card.py:47-55, 410-419, 444-450`

- [ ] **Step 1: 强制红色**

`OFFICIAL_RED_ACTIONS` 增加 `"subscription.cancel"`，注释：

```python
        "subscription.cancel",
        ...
- `subscription.cancel`：关闭的是银行卡侧扣款授权（解除既有支付授权），
  与「解挂 / 调高限额」同属解除保护；单笔金额小不代表敞口小，强制 ≥ 红。
```

- [ ] **Step 2: catalog 描述**

```python
        description="关闭代扣协议（强制红色；未来流出转负；仅归属用户可关闭）",
```

- [ ] **Step 3: 口述触发**

`llm.py` 模板正则改为：

```python
            r"取消订阅|退订|取消代扣|关掉自动续费|关闭扣款协议|关闭代扣协议|解约代扣|关掉银行卡代扣"
```

说明文字改为「命中『关闭代扣协议』模板」。

- [ ] **Step 4: session 话术**

```python
_MERCHANT_CANCEL_REFUSAL = (
    "本行能关闭的是银行卡侧的扣款授权；商户侧的订阅状态需要到商户 App 关闭，"
    "本行拿不到商户的取消确认回执。"
)
_REFUND_GUARANTEE_REFUSAL = (
    "本行可以关闭银行卡侧扣款授权，但退款与保证商户未来不再扣款均未接入，"
    "无法执行或承诺。"
)
```

动作中文名：

```python
        "subscription.list": "代扣协议查询",
        "subscription.cancel": "关闭扣款协议",
```

- [ ] **Step 5: triage 话术**

```python
_SUBSCRIPTION_BOUNDARY_MESSAGE = (
    "在本页关闭的是银行卡对商户的扣款授权：关闭后这张卡不再向该商户放款。"
    "商户是否继续提供服务、是否停止计费，由商户决定；商户侧订阅状态仍需到商户 App 关闭。"
)
```

示例句改为：

```python
    TriageSuggestion(label="查看我的代扣协议", text="查一下我的代扣协议"),
    TriageSuggestion(label="关闭视频会员扣款", text="关闭视频会员的扣款协议"),
```

还款话术「订阅管理」改为「代扣协议管理」。

- [ ] **Step 6: plan_card 对象行与动作名**

```python
    "subscription.list": "代扣协议查询",
    "subscription.cancel": "关闭扣款协议",
```

**查询行**：把 `_object_label` 里现有的 `subscription.list` 分支
（`upcoming_charges / detected_charges`）替换为：

```python
    if node.action == "subscription.list":
        user_id = _str_param(node, "user_id")
        if user_id is not None:
            return subscriptions.describe_agreements_facts(gateway, user_id)
```

**关闭行**：在通用 `subscription_id` 分支之前加：

```python
    if node.action == "subscription.cancel":
        subscription_id = _str_param(node, "subscription_id")
        if subscription_id is not None:
            try:
                subscription = gateway.subscription(subscription_id).payload
            except GatewayNotFoundError:
                return f"代扣协议 {subscription_id}"
            return subscriptions.describe_agreement_facts(
                gateway, subscription.user_id, subscription_id
            )
```

在 `subscriptions.py` 新增两个函数：

```python
def describe_agreements_facts(gateway: FactGateway, user_id: str) -> str:
    report = subscription_report(
        gateway, user_id, as_of=DEFAULT_BOOKING_DATE
    )
    active = [
        item for item in report.insights
        if item.agreement_status == AGREEMENT_ACTIVE
    ]
    if not active:
        return "暂无在签代扣协议；" + SUBSCRIPTION_CANCEL_BOUNDARY_SHORT
    rows = "；".join(
        f"{item.merchant}（{item.agreement_type_label}，协议 {item.agreement_no}，"
        f"已扣 {item.charge_count} 期"
        f"{' ' + Money(item.charged_total_cents).yuan + ' 元' if item.charged_total_cents is not None else ''}，"
        f"下次 {item.next_charge_on.isoformat() if item.next_charge_on else '未知'}）"
        for item in active[:3]
    )
    more = f" 等 {len(active)} 项" if len(active) > 3 else ""
    return f"代扣协议：{rows}{more}；{SUBSCRIPTION_CANCEL_BOUNDARY_SHORT}"


def describe_agreement_facts(
    gateway: FactGateway, user_id: str, subscription_id: str
) -> str:
    report = subscription_report(
        gateway, user_id, as_of=DEFAULT_BOOKING_DATE
    )
    item = next(
        (row for row in report.insights if row.subscription_id == subscription_id),
        None,
    )
    if item is None:
        return f"代扣协议 {subscription_id}"
    total = (
        f"已扣 {item.charge_count} 期 {Money(item.charged_total_cents).yuan} 元"
        if item.charged_total_cents is not None
        else "历史扣款数据不足"
    )
    return (
        f"{item.merchant} · {item.agreement_type_label} · 协议 {item.agreement_no} · "
        f"{item.signed_on.isoformat() if item.signed_on else '签约时间未知'} 签约 · {total}；"
        f"{SUBSCRIPTION_CANCEL_BOUNDARY_SHORT}"
    )
```

需要 import：

```python
from ai_banking_copilot.gateway.core_bank import DEFAULT_BOOKING_DATE
```

- [ ] **Step 7: 静态验证**

```powershell
python -m compileall -q src/ai_banking_copilot/policy src/ai_banking_copilot/planning src/ai_banking_copilot/web
python -m ruff check src/ai_banking_copilot/policy/pdp.py src/ai_banking_copilot/tools/catalog.py src/ai_banking_copilot/planning/llm.py
```

---

## Task 6: 页面结构与静态文案

**Files:**
- Modify: `src/ai_banking_copilot/web/page.py:54, 151-158, 484-521, 675-680`

- [ ] **Step 1: 导航与首页卡**

```html
<button class="appnav__tile" type="button" data-nav="scene" data-scene="subscriptions">代扣协议</button>
...
<p class="scene-card__k">⑤ 代扣协议</p>
...
<button class="btn btn--sm" type="button" data-nav="chat" data-prefill="关闭扣款协议">去对话</button>
```

- [ ] **Step 2: 场景块结构**

```html
<div class="scene-block" data-scene-block="subscriptions" hidden>
  <section class="panel" id="subscription-panel">
    <div class="scene-head">
      <p class="panel__title">代扣协议管理</p>
      <span class="muted" id="subscription-status">正在读取代扣协议…</span>
    </div>
    <p class="muted" id="subscription-boundary" hidden></p>
    <p class="muted" id="subscription-focus-note" hidden></p>
    <div id="subscription-summary"></div>
    <div id="subscription-filters"></div>
    <div id="subscription-timeline"></div>
    <div id="subscription-list"><p class="muted">尚未读取代扣协议。</p></div>
    <div id="subscription-plan" hidden></div>
    <p class="muted" id="subscription-plan-status" hidden></p>
    <div id="subscription-cancel-result" hidden></div>
    <dialog class="sub-cancel-dialog" id="subscription-cancel-dialog">
      <div class="sub-cancel-dialog__head">
        <p class="panel__title">关闭这个扣款协议？</p>
        <button class="btn btn--sm btn--ghost" type="button" data-sub-cancel-close>返回</button>
      </div>
      <div class="sub-cancel-dialog__body" id="subscription-cancel-body"></div>
      <div class="sub-cancel-dialog__foot">
        <button class="btn btn--ghost" type="button" data-sub-cancel-close>先不关闭</button>
        <button class="btn btn--danger" type="button" data-sub-cancel-confirm>关闭扣款协议</button>
      </div>
    </dialog>
    <details class="scene-note">
      <summary>这些商户是怎么识别出来的</summary>
      <ul>
        <li>已登记的代扣协议来自本行代扣登记；「疑似」来自流水周期推断，需本人核对。</li>
        <li>协议编号、协议类型与演示历史扣款由内置演示数据派生，不是真实银联/网联协议号。</li>
        <li>标注「推算」的下次扣款日按历史周期计算，实际扣款日以商户为准。</li>
        <li>本页只展示本行能看到的代扣协议与扣款；其他卡或其他渠道的扣款不在本页范围内。</li>
      </ul>
    </details>
    <div class="btn-row">
      <button class="btn btn--sm" type="button" data-nav="records" data-record-tab="charges">扣款记录</button>
      <button class="btn btn--sm" type="button" data-action="refresh-subscriptions">刷新数据</button>
    </div>
  </section>
</div>
```

- [ ] **Step 3: 详情弹层标题**

`#sub-detail-title` 改为「协议详情 · 扣款记录」。

- [ ] **Step 4: 静态验证**

```powershell
python -m compileall -q src/ai_banking_copilot/web/page.py
```

---

## Task 7: 前端协议卡、概览、筛选与对账

**Files:**
- Modify: `src/ai_banking_copilot/web/assets/app.js:1916-2118, 3625-3671, 6730-6775, 7669-7730`
- Modify: `src/ai_banking_copilot/web/assets/workspace.css:1841-1980`

- [ ] **Step 1: 场景元信息**

```js
  subscriptions: {
    title: "代扣协议",
    sub: "关闭银行卡侧扣款授权；签约时间、累计扣款与解约后对账可查。",
    short: "关闭扣款协议与解约对账",
    prefill: "关闭扣款协议",
  },
```

- [ ] **Step 2: 概览与筛选**

替换 `subscriptionSummaryHtml`：

```js
function subscriptionSummaryHtml(details) {
  const summary = details.summary || {};
  const month = summary.month_label || String(details.as_of || "").slice(0, 7);
  return `<div class="sub-overview">
      <div class="sub-overview__main">
        <span class="sub-overview__k">本月已扣（${esc(month)}）</span>
        <b class="sub-overview__v">${yuan(summary.month_charged_cents || 0)}</b>
      </div>
      <div class="sub-overview__main">
        <span class="sub-overview__k">本月待扣（截至今天）</span>
        <b class="sub-overview__v">${yuan(summary.month_due_cents || 0)}</b>
      </div>
      <div class="sub-overview__main">
        <span class="sub-overview__k">未来 30 天预计</span>
        <b class="sub-overview__v">${yuan(summary.next_30d_cents || 0)}</b>
      </div>
      <div class="sub-overview__chips">
        <span class="chip">在签 ${Number(summary.active_count) || 0} 项</span>
        <span class="chip${summary.suspected_count ? " chip--yellow" : ""}">疑似待核对 ${Number(summary.suspected_count) || 0} 项</span>
        <span class="chip">已解约 ${Number(summary.cancelled_count) || 0} 项</span>
      </div>
    </div>
    <p class="muted scene-hint">本月已扣 = 本月已记账的协议扣款（年付只在扣款当月计入）；
      本月待扣 = 今天到月底尚未到期的扣款；未来 30 天预计按推算扣款日计入，年付不摊到每月。</p>`;
}
```

新增筛选：

```js
function agreementFiltersHtml(insights) {
  const cards = Array.from(new Map(
    insights.filter((item) => item.card_tail)
      .map((item) => [item.card_tail, item.card_tail])
  ).keys());
  const view = state.subView || "agreement";
  const card = state.subCard || "all";
  return `<div class="sub-filters">
    <div class="sub-filters__group" role="group" aria-label="查看方式">
      ${[["agreement", "按协议"], ["merchant", "按商户"], ["cycle", "按周期"]]
        .map(([key, label]) => `<button class="sub-seg${view === key ? " is-active" : ""}"
          type="button" data-sub-view="${key}">${label}</button>`).join("")}
    </div>
    <div class="sub-filters__group" role="group" aria-label="扣款卡">
      <button class="sub-seg${card === "all" ? " is-active" : ""}"
        type="button" data-sub-card="all">全部卡</button>
      ${cards.map((tail) => `<button class="sub-seg${card === tail ? " is-active" : ""}"
        type="button" data-sub-card="${esc(tail)}">尾号 ${esc(tail)}</button>`).join("")}
    </div>
    ${card === "all" ? "" : '<span class="muted sub-filters__hint">已按该卡筛选，以下全部为这张卡的代扣协议</span>'}
  </div>`;
}
```

- [ ] **Step 3: 协议卡**

替换 `subscriptionCardHtml`：

```js
function agreementPriceHtml(item) {
  const change = item.price_change;
  if (!change) return "";
  return `<p class="sub-card__price-up">${yuan(change.from_cents)} → ${yuan(change.to_cents)}，
    自 ${esc(change.effective_on)} 起（+${esc(change.percent)}%）</p>`;
}

function agreementReconHtml(item) {
  const charges = item.post_cancel_charges || [];
  if (item.agreement_status !== "cancelled") return "";
  const detail = charges.length
    ? `<p class="sub-card__recon sub-card__recon--bad">解约后仍有 ${charges.length} 笔扣款：
        ${charges.map((c) => `${esc(c.booked_on)} ${yuan(Math.abs(c.amount_cents))}`).join("、")}
       </p>
       <button class="btn btn--sm btn--danger" type="button"
         data-prefill="申请追回：${esc(item.merchant)} 解约后仍扣款">申请追回</button>`
    : '<p class="sub-card__recon">解约后扣款：未发现</p>';
  return `<p class="muted sub-card__facts">解约日期 ${esc(item.cancelled_on || "未知")} ·
      最后一笔扣款 ${esc(item.last_charged_on || "未知")}
      ${item.last_charged_on ? yuan(item.amount_cents) : ""}</p>${detail}`;
}

function subscriptionCardHtml(item, index) {
  const cancelled = item.agreement_status === "cancelled";
  const suspect = item.agreement_status === "suspected";
  const next = item.next_charge_on
    ? `${esc(item.next_charge_on)}${item.next_charge_state === "overdue" ? "（已逾期）" : "（推算）"}`
    : "未提供";
  const total = item.charged_total_cents == null
    ? "累计扣款数据不足"
    : `已扣 ${Number(item.charge_count) || 0} 期 ${yuan(item.charged_total_cents)}`;
  const typeChip = item.agreement_type_label
    ? `<span class="chip chip--muted">${esc(item.agreement_type_label)}</span>` : "";
  const state = cancelled ? '<span class="chip">已解约</span>'
    : suspect ? '<span class="chip chip--yellow">疑似</span>' : "";
  const closeBtn = !cancelled && !suspect && item.subscription_id
    ? `<button class="sub-card__cancel" type="button" data-sub-cancel="${esc(item.subscription_id)}"
        data-sub-merchant="${esc(item.merchant)}" data-sub-no="${esc(item.agreement_no || "")}"
        data-sub-type="${esc(item.agreement_type_label || "")}"
        data-sub-signed="${esc(item.signed_on || "")}"
        data-sub-total="${item.charged_total_cents == null ? "" : item.charged_total_cents}"
        data-sub-count="${Number(item.charge_count) || 0}"
        data-sub-last="${esc(item.last_charged_on || "")}"
        data-sub-next="${esc(item.next_charge_on || "")}"
        data-sub-amount="${yuan(item.amount_cents)}"
        data-sub-cycle="${esc(item.cycle_label || "")}">关闭扣款协议</button>`
    : suspect
      ? '<span class="chip chip--yellow">未匹配到本行协议</span>' : "";
  const overdue = item.next_charge_state === "overdue"
    ? `<p class="sub-card__overdue">预计 ${esc(item.next_charge_on || "—")} 扣款，
        已过 ${Number(item.overdue_days) || 0} 天未见扣款记录；按周期顺延。</p>` : "";
  return `<article class="sub-card${cancelled ? " sub-card--off" : ""}"
      data-merchant-key="${esc(item.merchant)}"
      data-card-tail="${esc(item.card_tail || "")}">
    <span class="sub-card__bar" aria-hidden="true"
      style="background:${cancelled ? "var(--rule-strong)" : sceneColor(index)}"></span>
    <div class="sub-card__in">
      <div class="sub-card__top">
        <span class="sub-card__merchant">${esc(item.merchant)}</span>${typeChip}${state}
      </div>
      ${cancelled ? "" : `<div class="sub-card__line">
        <p class="sub-card__amount">${yuan(item.amount_cents)}
          <span class="sub-card__meta"> / ${esc(item.cycle_label || "周期未知")} · 下次 ${next}</span></p>
        ${closeBtn}
      </div>`}
      <p class="muted sub-card__facts">协议 ${esc(item.agreement_no || "未匹配")} ·
        ${esc(item.signed_on || "签约时间未知")} 签约 · ${total}</p>
      <p class="muted sub-card__facts">最近扣款 ${esc(item.last_charged_on || "未知")}</p>
      ${cancelled ? "" : agreementPriceHtml(item)}${cancelled ? "" : overdue}${agreementReconHtml(item)}
      <button class="sub-card__more" type="button" data-sub-detail="${esc(item.insight_id)}"
        aria-haspopup="dialog">协议详情 · 扣款记录<span class="sub-card__chev" aria-hidden="true">›</span></button>
    </div>
  </article>`;
}
```

- [ ] **Step 4: 未来扣款只列未扣**

替换 `subscriptionTimelineHtml`：

```js
function subscriptionTimelineHtml(insights) {
  const rows = insights
    .filter((item) => item.agreement_status === "active" && item.next_charge_on)
    .filter((item) => String(item.next_charge_on) >= (state.options.subscription_details.as_of || ""))
    .sort((a, b) => String(a.next_charge_on).localeCompare(String(b.next_charge_on)))
    .slice(0, 6);
  const head = '<p class="panel__title panel__title--sub">接下来要扣的</p>';
  if (!rows.length) {
    return head + '<p class="muted">暂无可排期的未来扣款。</p>';
  }
  const items = rows.map((item) => `<li class="sub-ev${item.next_charge_state === "overdue" ? " is-hot" : ""}">
      <span class="sub-ev__date">${esc(item.next_charge_on)}</span>
      <span class="sub-ev__name">${esc(item.merchant)}
        <span class="sub-ev__tag">推算</span></span>
      <span class="sub-ev__amt">${yuan(item.amount_cents)}</span>
    </li>`).join("");
  return head + `<ol class="sub-ev-list">${items}</ol>
    <p class="muted scene-hint">「推算」按历史扣费周期计算，实际扣款日以商户为准。</p>`;
}
```

- [ ] **Step 5: 详情弹层补协议与扣款记录**

替换 `subscriptionBasisHtml`：

```js
function subscriptionBasisHtml(item) {
  const price = item.price_change
    ? `涨价：${yuan(item.price_change.from_cents)} → ${yuan(item.price_change.to_cents)}，
       自 ${esc(item.price_change.effective_on)} 起（+${esc(item.price_change.percent)}%）`
    : "未发现近 12 个月涨价";
  const total = item.charged_total_cents == null
    ? "累计扣款数据不足"
    : `累计 ${Number(item.charge_count) || 0} 期 ${yuan(item.charged_total_cents)}`;
  const charges = (item.evidence || []).slice(0, 12).map((txn) =>
    `<li><span class="mono">${esc(txn.booked_on)} · ${yuan(Math.abs(txn.amount_cents))}</span>
      <span class="tech">渠道 ${esc(txn.channel || "—")} · 卡尾号 ${esc(txn.card_tail || "—")} ·
      ${esc(txn.record_id)}</span></li>`).join("");
  return `<p class="sub-detail__lead">${esc(item.merchant)} ·
      ${esc(item.agreement_type_label || "类型未知")} · 协议 ${esc(item.agreement_no || "未匹配")}</p>
    <p>签约 ${esc(item.signed_on || "未知")} · ${total} · 最近扣款 ${esc(item.last_charged_on || "未知")}</p>
    <p>${price}</p>
    <p class="muted">下次扣款 ${esc(item.next_charge_on || "未知")}
      ${item.next_charge_on ? "（推算）" : ""} · 置信度 ${esc(confidenceLabel(item.confidence))}</p>
    ${item.agreement_status === "cancelled" ? agreementReconHtml(item) : ""}
    <p><span class="muted">关闭影响：</span>关闭后银行卡侧不再放款；商户是否继续服务由商户决定。</p>
    ${subscriptionNoticesHtml(item)}
    ${charges ? `<details open><summary>扣款记录（最近 ${item.evidence.length} 笔）</summary><ul>${charges}</ul></details>` : ""}`;
}
```

- [ ] **Step 6: 主渲染与分组**

替换 `renderSubscriptionDetails` 的 status/boundary/summary/timeline/list 部分：

```js
  status.textContent = `数据截至 ${details.as_of} · 识别结果已同步`;
  if (caps && caps.boundary) {
    boundary.hidden = false;
    boundary.textContent = caps.boundary;
  }
  const insights = details.insights || [];
  const filters = el("subscription-filters");
  if (filters) filters.innerHTML = agreementFiltersHtml(insights);
  if (!insights.length) {
    ...
  } else {
    if (summary) summary.innerHTML = subscriptionSummaryHtml(details);
    if (timeline) timeline.innerHTML = subscriptionTimelineHtml(insights);
    list.innerHTML = agreementListHtml(insights);
    applyAgreementFocus();
  }
```

新增 `agreementListHtml`：

```js
function agreementListHtml(insights) {
  const view = state.subView || "agreement";
  const card = state.subCard || "all";
  const visible = card === "all"
    ? insights
    : insights.filter((item) => item.card_tail === card);
  if (view === "merchant") {
    const groups = new Map();
    visible.forEach((item) => {
      const key = item.merchant;
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(item);
    });
    return `<p class="panel__title panel__title--sub">按商户查看</p>` +
      Array.from(groups.entries()).map(([merchant, items]) => {
        const total = items.reduce((sum, item) => sum + (Number(item.charged_total_cents) || 0), 0);
        return `<p class="muted sub-group__head">${esc(merchant)} ·
          ${items.length} 个协议 · 累计 ${yuan(total)}</p>
          <div class="sub-wall">${items.map((item) =>
            subscriptionCardHtml(item, insights.indexOf(item))).join("")}</div>`;
      }).join("");
  }
  if (view === "cycle") {
    const cycles = [["monthly", "月度"], ["yearly", "年度"], ["unknown", "周期未知"]];
    return `<p class="panel__title panel__title--sub">按周期查看</p>` +
      cycles.map(([cycle, label]) => {
        const items = visible.filter((item) =>
          (cycle === "unknown" ? !["monthly", "yearly"].includes(item.cycle) : item.cycle === cycle));
        return items.length
          ? `<p class="muted sub-group__head">${label} · ${items.length} 项</p>
             <div class="sub-wall">${items.map((item) =>
               subscriptionCardHtml(item, insights.indexOf(item))).join("")}</div>`
          : "";
      }).join("");
  }
  const active = visible.filter((item) => item.agreement_status !== "cancelled");
  const cancelled = visible.filter((item) => item.agreement_status === "cancelled");
  const activeHtml = active.length
    ? `<p class="panel__title panel__title--sub">在签协议 <span class="tech">${active.length} 项</span></p>
      <div class="sub-wall">${active.map((item) =>
        subscriptionCardHtml(item, insights.indexOf(item))).join("")}</div>`
    : '<p class="panel__title panel__title--sub">在签协议</p><p class="muted">当前没有在签协议。</p>';
  const cancelledHtml = cancelled.length
    ? `<p class="panel__title panel__title--sub">已解约 <span class="tech">${cancelled.length} 项</span></p>
      <div class="sub-wall">${cancelled.map((item) =>
        subscriptionCardHtml(item, insights.indexOf(item))).join("")}</div>`
    : "";
  return activeHtml + cancelledHtml;
}

function applyAgreementFocus() {
  const key = state.pendingAgreementFocus;
  if (!key) return;
  state.pendingAgreementFocus = "";
  const cards = Array.from(document.querySelectorAll("#subscription-list .sub-card"));
  const target = cards.find((card) =>
    (card.dataset.merchantKey || "").includes(key) || key.includes(card.dataset.merchantKey || ""));
  const note = el("subscription-focus-note");
  if (!target) {
    if (note) {
      note.hidden = false;
      note.textContent = `没有找到「${key}」的本行代扣协议：可能是其他卡或其他渠道扣款。`;
    }
    return;
  }
  if (note) note.hidden = true;
  target.scrollIntoView({ behavior: "smooth", block: "center" });
  target.classList.add("sub-card--focus");
  window.setTimeout(() => target.classList.remove("sub-card--focus"), 3200);
  const detail = target.querySelector("[data-sub-detail]");
  if (detail) openSubDetail(detail.dataset.subDetail || "");
}
```

- [ ] **Step 7: 清理旧函数与重置逻辑**

- 删除 `subscriptionAlertsHtml`（涨价改为卡片内展示）。
- 删除 `subscriptionEvidenceHtml`（扣款记录并入 `subscriptionBasisHtml`）。
- `renderSubscriptionDetails` 的 `reset()` 增加：

```js
    const filters = el("subscription-filters");
    if (filters) filters.innerHTML = "";
    const note = el("subscription-focus-note");
    if (note) {
      note.hidden = true;
      note.textContent = "";
    }
```

- [ ] **Step 8: 筛选事件**

在全局 click 监听里（`data-sub-detail` 之前）加：

```js
  const subView = target.closest("[data-sub-view]");
  if (subView) {
    state.subView = subView.dataset.subView || "agreement";
    renderSubscriptionDetails(state.options);
    return;
  }
  const subCard = target.closest("[data-sub-card]");
  if (subCard) {
    state.subCard = subCard.dataset.subCard || "all";
    renderSubscriptionDetails(state.options);
    return;
  }
```

- [ ] **Step 9: 解约弹窗改写**

替换 `openSubCancelDialog`：

```js
function openSubCancelDialog(button) {
  const dialog = el("subscription-cancel-dialog");
  if (!dialog || !button) return;
  const data = button.dataset;
  dialog.dataset.subId = data.subCancel || "";
  const body = el("subscription-cancel-body");
  const total = data.subTotal
    ? `已扣 ${esc(data.subCount || "0")} 期 ${yuan(Number(data.subTotal))}`
    : "累计扣款数据不足";
  if (body) {
    body.innerHTML = `<p class="sub-cancel-dialog__merchant">${esc(data.subMerchant || "该代扣协议")}</p>
      <p class="sub-cancel-dialog__amount">${esc(data.subAmount || "—")} / ${esc(data.subCycle || "周期未知")}</p>
      <p class="muted sub-cancel-dialog__meta">${esc(data.subType || "类型未知")} ·
        协议 ${esc(data.subNo || "未匹配")} · ${esc(data.subSigned || "签约时间未知")} 签约</p>
      <p class="muted sub-cancel-dialog__meta">${total} · 最近扣款 ${esc(data.subLast || "未知")}
        · 下次扣款 ${esc(data.subNext || "未知")}（推算）</p>
      <p class="muted sub-cancel-dialog__note">关闭后这张卡不再向该商户放款；
        商户是否继续服务、是否停止计费由商户决定。已产生的扣款不会自动退回；
        关闭后如仍被扣款，可在本页核对并发起追回申请。</p>`;
  }
  if (typeof dialog.showModal === "function") dialog.showModal();
  else dialog.setAttribute("open", "");
}
```

`subscriptionPanelTask` 的 utterance 改为：

```js
    utterance: "关闭扣款协议",
```

- [ ] **Step 10: 解约结果回执**

替换 `renderSubscriptionCancel`：

```js
function renderSubscriptionCancel(cancel) {
  const box = el("subscription-cancel-result");
  if (!box) return;
  if (!cancel) {
    box.hidden = true;
    box.innerHTML = "";
    return;
  }
  box.hidden = false;
  const post = (cancel.post_cancel_charges || []);
  const recon = post.length
    ? `<p class="sub-card__recon sub-card__recon--bad">解约后仍有 ${post.length} 笔扣款：
        ${post.map((c) => `${esc(c.booked_on)} ${yuan(Math.abs(c.amount_cents))}`).join("、")}</p>
       <button class="btn btn--sm btn--danger" type="button"
         data-prefill="申请追回：${esc(post[0].merchant_label || "")} 解约后仍扣款">申请追回</button>`
    : '<p class="sub-card__recon">解约后扣款：未发现</p>';
  box.innerHTML = `<div class="takeover">
    <p><b>${esc(cancel.label)}</b></p>
    <p class="muted">解约日期 ${esc(cancel.cancelled_on || "未知")} ·
      最后一笔扣款 ${esc(cancel.last_charge_on || "未知")}
      ${cancel.last_charge_cents != null ? yuan(Math.abs(cancel.last_charge_cents)) : ""}</p>
    ${recon}
    <p class="muted">${esc(cancel.boundary)}</p>
    ${cancel.sms_receipt
      ? `<p class="muted">短信回执（演示，不发送真实短信）：${esc(cancel.sms_receipt.message)}</p>`
      : ""}
  </div>`;
}
```

- [ ] **Step 11: 样式**

在 `workspace.css` 追加：

```css
.sub-overview__main + .sub-overview__main { margin-left: var(--s-5); }
.sub-filters { display: flex; flex-wrap: wrap; gap: var(--s-3); align-items: center; margin: var(--s-3) 0; }
.sub-filters__group { display: inline-flex; border: 1px solid var(--rule); border-radius: 999px; overflow: hidden; }
.sub-seg { border: 0; background: transparent; padding: 6px 12px; cursor: pointer; font-size: var(--fs-small); }
.sub-seg.is-active { background: var(--blue-600); color: #fff; }
.sub-filters__hint { font-size: var(--fs-small); }
.sub-card__facts { margin: 2px 0 0; font-size: var(--fs-small); }
.sub-card__price-up { margin: 6px 0 0; color: var(--red-600); font-size: var(--fs-small); font-weight: 600; }
.sub-card__overdue { margin: 6px 0 0; color: var(--amber-700); font-size: var(--fs-small); }
.sub-card__recon { margin: 6px 0 0; font-size: var(--fs-small); color: var(--green-700); }
.sub-card__recon--bad { color: var(--red-600); font-weight: 600; }
.sub-card--focus { outline: 2px solid var(--blue-600); outline-offset: 2px; }
.sub-group__head { margin: var(--s-3) 0 var(--s-2); }
.chip--muted { background: var(--fill-2); color: var(--text-2); }
```

变量名若与仓库不一致，按 `workspace.css` 现有 token 替换，不新增硬编码色值。

- [ ] **Step 12: 静态验证**

```powershell
node --check src/ai_banking_copilot/web/assets/app.js
python -m compileall -q src/ai_banking_copilot/web/page.py
```

Expected: `node --check` 无输出。

---

## Task 8: 页内红色验证支持

**Files:**
- Modify: `src/ai_banking_copilot/web/assets/app.js:1547-1599, 1631-1643`

- [ ] **Step 1: 识别「可页内验证」的动作**

`sceneCardHtml` 内：

```js
  const subscriptionCancel = (card.nodes || []).some(
    (node) => String(node.action || "") === "subscription.cancel"
  );
  const inPageVerify = transfer || subscriptionCancel;
```

- [ ] **Step 2: 非转账红色也给验证面板**

把 `card.level === "RED"` 分支改为：

```js
    : card.level === "RED"
      ? (inPageVerify
        ? verifyPanel + (sceneVerification
          ? '<button class="btn btn--sm btn--ghost" type="button" data-scene-dismiss>取消</button>'
          : '<button class="btn btn--sm btn--primary" type="button" data-scene-verify data-requires-login>获取验证码</button>' +
            '<button class="btn btn--sm btn--ghost" type="button" data-scene-dismiss>取消</button>')
        : '<button class="btn btn--sm" type="button" data-scene-chat>去对话完成验证</button>')
```

- [ ] **Step 3: 按钮与提示按动作换词**

`verifyPanel` 内按钮：

```js
        <button class="btn btn--sm btn--primary" type="button" data-scene-confirm-code data-requires-login>
          ${transfer ? "提交验证码并转账" : "提交验证码并关闭协议"}</button>
```

`need` 文案：

```js
      : subscriptionCancel
        ? "关闭扣款协议需要一次性验证码；演示验证码不是真实短信、人脸或交易密码。"
        : ...
```

`requestSceneVerification` 的最后一句：

```js
  if (status) status.textContent = "验证码已下发，请输入后确认。";
```

- [ ] **Step 4: 对话内提示**

`verifyHtml` 的标题/按钮沿用现有逻辑；若按钮文案写死「确认执行」，改为按 `state.card` 是否含 `subscription.cancel` 显示「提交验证码并关闭协议」。

- [ ] **Step 5: 静态验证**

```powershell
node --check src/ai_banking_copilot/web/assets/app.js
```

---

## Task 9: 账单线索深链到协议卡

**Files:**
- Modify: `src/ai_banking_copilot/web/assets/app.js:3694-3726, 7731-7808, 7669-7730`

- [ ] **Step 1: 线索加跳转按钮**

`billsFindingsHtml` 的 `rows.map` 内：

```js
    const evidence = item.evidence || [];
    const merchant = evidence.length ? evidence[evidence.length - 1].merchant_label : "";
    const canJump = merchant && ["hidden-fee", "recurring-subscription"].includes(item.kind);
    const jump = canJump
      ? `<button class="btn btn--sm btn--ghost" type="button" dir="auto"
          data-nav="scene" data-scene="subscriptions"
          data-sub-focus="${esc(merchant)}">查看代扣协议</button>`
      : "";
```

把 `${jump}` 放在 `finding__suggestion` 之后。

- [ ] **Step 2: 导航携带 focus**

`navigate` 的 `opts` 增加：

```js
    if (nav.dataset.subFocus) opts.subFocus = nav.dataset.subFocus;
```

`showView` 调用：

```js
  if (next === "scene") showScene(opts.scene || currentScene, opts);
```

`showScene(key, opts)`：

```js
function showScene(key, opts) {
  ...
  if (opts && opts.subFocus) state.pendingAgreementFocus = opts.subFocus;
  ...
}
```

- [ ] **Step 3: 静态验证**

```powershell
node --check src/ai_banking_copilot/web/assets/app.js
```

---

## Task 10: 同步测试与评测预期（只写不跑）

**Files:**
- Modify: `tests/scenarios/test_subscriptions_regression.py:168, 291`
- Modify: `tests/policy/test_pep.py`（如有黄/红断言）
- Modify: `src/ai_banking_copilot/eval/data/cases.json`
- Modify: `src/ai_banking_copilot/eval/data/heldout.json`
- Modify: `src/ai_banking_copilot/eval/data/heldout_v1.json`
- Modify: `src/ai_banking_copilot/eval/data/web_cases.json`
- Modify: `src/ai_banking_copilot/eval/data/evidence_dev_v1.json`

- [ ] **Step 1: 找出所有取消相关预期**

```powershell
rg -n "取消订阅|退订|取消代扣|关掉自动续费|订阅取消" src/ai_banking_copilot/eval tests
```

Expected: 列出所有命中行；逐条判断是否是 `subscription.cancel` 用例。

- [ ] **Step 2: 改等级**

- 单动作取消用例：`expect_level` 由 `"YELLOW"` 改为 `"RED"`。
- 多意图用例（如「转给李娜 500 元，取消订阅」）：计划级取最高级，改为 `"RED"`。
- 不改用例 id（避免引用断裂），只改预期值。

- [ ] **Step 3: 测试断言**

`tests/scenarios/test_subscriptions_regression.py` 两处：

```python
    _assert_card(view, "subscription.cancel", "RED")
```

如果测试在确认时没有带验证码，按现有红色动作测试的写法补 `verification_code`；只改测试，不运行。

- [ ] **Step 4: 不执行 pytest**

明确记录：本任务**不运行** `pytest`；由用户后续验收。

---

## Task 11: 静态验证与页面走查

**Files:** 无新增；验证全部改动。

- [ ] **Step 1: 编译与静态检查**

```powershell
python -m compileall -q src
python -m ruff check .
python -m mypy src
```

Expected: compileall 无输出；ruff `All checks passed`；mypy `Success: no issues found`。
**不运行 pytest。**

- [ ] **Step 2: 起服务**

```powershell
$env:AIB_DISABLE_LLM="1"
python -m ai_banking_copilot
```

Expected: 服务监听 `http://127.0.0.1:8000`。

- [ ] **Step 3: 接口核对**

```powershell
$options = Invoke-RestMethod http://127.0.0.1:8000/api/options
$options.subscription_details.summary | Format-List
$options.subscription_details.insights | Select-Object merchant, agreement_no, agreement_type_label, agreement_status, charge_count, charged_total_cents, next_charge_on, next_charge_state, cancelled_on | Format-Table
```

Expected（as_of=2026-10-31）：
- 音乐会员：`charge_count=22`、`charged_total_cents=22200`、`price_change` 为 `1000 → 1200`、`next_charge_on=2026-11-15`；
- 视频会员：`charge_count=22`、`charged_total_cents=55000`、`next_charge_state=scheduled`；
- 云盘会员：`charge_count=2`、`charged_total_cents=39600`、`next_charge_on=2027-03-01`；
- `summary.month_charged_cents=3700`、`summary.month_due_cents=0`、`summary.next_30d_cents=3700`。

- [ ] **Step 4: 页面走查**

打开 `http://127.0.0.1:8000/#/scene/subscriptions`，逐项确认：

1. 标题「代扣协议管理」，导航「代扣协议」；
2. 顶部一句话能力说明，底部不再有「取消口径 / 数据边界 / 业务日 / 识别说明」；
3. 三个金额数字与口径说明；
4. 音乐会员卡片显示协议编号、签约时间、22 期 ¥222、涨价 ¥10 → ¥12；
5. 「接下来要扣的」不含已过期日期；
6. 点「关闭扣款协议」→ 弹窗显示协议字段与停扣影响 → 确认 → 演示验证码 → 回执；
7. 解约后卡片进入已解约区，显示解约日期、最后一笔、解约后扣款；
8. 账单页「疑似隐藏费用 / 周期订阅」线索出现「查看代扣协议」，点击后定位协议卡或提示未匹配；
9. 筛选「按商户 / 按周期」可切换；
10. 页面无 console 报错。

- [ ] **Step 5: 交付摘要**

按项目规则写清：改了什么 / 为什么 / 动了哪些共享文件 / 残留了什么。
必须注明：**未运行 pytest**；评测预期已静态同步，待用户验收。

---

## 自检清单（实施前）

- [ ] 所有取消入口文案都是「关闭扣款协议」，不再出现「取消订阅」作为按钮。
- [ ] 页面顶部一句话回答「点了之后钱还扣不扣」。
- [ ] 已解约卡不再以金额/月费为主信息。
- [ ] 未来扣款只列未扣项；过期预测日不再出现。
- [ ] 三个金额数字都有口径说明。
- [ ] 协议编号/类型/演示扣款均有「演示派生」说明。
- [ ] 未匹配登记的疑似商户没有关闭按钮。
- [ ] AI 回答与页面使用同一套协议事实与边界话术。
- [ ] 没有运行 pytest。

---

## 风险与回滚

| 风险 | 影响 | 回滚 |
|---|---|---|
| `subscription.cancel` 升红 | 多意图计划整体等级变化；评测预期需同步 | 从 `OFFICIAL_RED_ACTIONS` 移除该动作，恢复 `catalog` 描述 |
| 补音乐/云盘流水 | 账单分析月度合计与趋势变化 | 删除 `_recurring_charge_rows()` 的调用与对应行 |
| 订阅商户跳过年度缩放 | 2025 年视频会员金额由 ¥21.25 回到 ¥25 | 恢复 `_FIXED_MONTH_MERCHANTS` 的旧分支 |
| `cancelled_on` 新列 | 旧库需迁移；旧数据解约日期为空 | 保留可空列，不回填；页面显示「未知」 |
| 协议编号/类型为派生 | 可能被误认为真实协议号 | 页面折叠区与交付摘要明确标注「演示派生」 |

**回滚原则：** 先回滚前端入口（按钮文案与卡片），再回滚 payload 字段，最后回滚 schema；不让旧库因缺列而启动失败。


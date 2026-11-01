# 生活事件遗留五项 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 Q13 部分失败补偿、Q16 定时提醒、Q17 理财到期联动、Q18 推送隔离与未理解兜底、ME-02 未登录浏览，从「后端没有 / 答非所问」做成确定性可复测的行为。

**Architecture:** 前置一个确定性呈现层（字段白名单 + 中文标签，LLM 只做措辞）；其余五项都在既有接缝上加能力——到期预约复用 `life_event_schedules`（`kind` / `notify_on` / 重试计数），到期日由 `purchased_on + term_days` 推导，飞书推送复用 `FeishuAdapter.send` 并按其级别分流按钮，匿名浏览只开只读 GET，写接口一律 401。

**Tech Stack:** Python 3.14 / FastAPI / SQLite（`Store`）/ 原生 JS / 飞书开放平台（`im/v1/messages`）

---

## 执行纪律（全程有效，每一步都适用）

- **禁止执行 `pytest`**：本计划所有测试只写文件，由用户在最后统一执行。任何步骤都不得运行 `pytest` / `python -m pytest` / 单测选择器。
- 每个任务的验证只用：`ruff check .` 与 `mypy`（预期分别 `All checks passed!` / `Success: no issues found in N source files`）。
- 需要手工核对行为时，用仓库外的临时脚本（`%TEMP%\aib_check_*.py`）跑一次端到端，跑完删除，不留在仓库里。
- 不新增业务动作、不改 `contracts/`、不改 `policy/pep.py` 三条硬约束、不改 `policy/gate.py` 判据。
- 每个任务结束提交一次；提交信息前缀 `feat(life):` / `fix(life):`。

---

## 文件结构

| 文件 | 职责 | 动作 |
|---|---|---|
| `src/ai_banking_copilot/web/present.py` | 卡片 / 记录页字段白名单与中文标签（确定性） | 新建 |
| `src/ai_banking_copilot/web/wording.py` | 可选措辞层：只改文案、失败退回模板 | 新建 |
| `src/ai_banking_copilot/web/suggest.py` | 未识别意图时的候选功能推荐（LLM 受清单约束 + 规则兜底） | 新建 |
| `src/ai_banking_copilot/web/reminders.py` | 到期推送调度（站内已由 `/api/due` 承担，本模块负责飞书推） | 新建 |
| `src/ai_banking_copilot/web/life_event_store.py` | 新列幂等迁移、`kind` / `notify_on` / `retry_count` / `visibility`、失败重试 | 修改 |
| `src/ai_banking_copilot/web/session.py` | `released_holds`、提醒解析、重试/取消、隐私标记、到期重建 | 修改 |
| `src/ai_banking_copilot/web/triage.py` | 兜底文案改成承接语，交给 `suggest.py` | 修改 |
| `src/ai_banking_copilot/web/app.py` | `retry` / `cancel` 端点、`maturities`、匿名只读、推送任务挂载 | 修改 |
| `src/ai_banking_copilot/web/assets/app.js` | 日历标注、私密折叠、匿名 banner、重试按钮 | 修改 |
| `src/ai_banking_copilot/web/channels/feishu.py` | `receive_id_type` 支持、按级别分流按钮 | 修改 |
| `src/ai_banking_copilot/scenarios/life_events.py` | 提醒槽位、到期联动链构建 | 修改 |
| `src/ai_banking_copilot/scenarios/pending.py` | 到期重建时带上 `kind`、提醒文案分支 | 修改 |
| `src/ai_banking_copilot/planning/llm.py` | 一条「理财到期 + 买礼物」模板规则 | 修改 |

---

## Task 1: 呈现层白名单（`present.py`）

**Files:**
- Create: `src/ai_banking_copilot/web/present.py`
- Create: `tests/web/test_present_fields.py`
- Modify: `src/ai_banking_copilot/web/plan_card.py`（`plan_summary` 的取值处改用 `present.fact_lines`）

- [ ] **Step 1: 写测试文件（不执行）**

创建 `tests/web/test_present_fields.py`：

```python
"""呈现层白名单：内部键不得出现在卡片与记录页。"""

from __future__ import annotations

from ai_banking_copilot.web.present import drop_internal, fact_lines


def test_internal_keys_never_reach_the_card() -> None:
    params = {
        "merchant": "花间集（西湖店）",
        "amount_cents": 9900,
        "order_on": "2026-11-04",
        "occasion_id": "birthday:2026:flowers",
        "budget_hold_id": "life:L1:acc_savings:2026-11-04",
    }
    lines = fact_lines("merchant.order", params)
    rendered = " ".join(f"{line.label}{line.value}" for line in lines)
    assert "occasion" not in rendered
    assert "budget_hold_id" not in rendered
    assert "花间集（西湖店）" in rendered
    assert "99.00" in rendered


def test_drop_internal_is_a_whitelist_of_known_keys() -> None:
    kept = drop_internal({"merchant": "花间集", "user_id": "u_demo", "grant": "explicit"})
    assert kept == {"merchant": "花间集"}
```

- [ ] **Step 2: 新建 `present.py`**

```python
"""呈现层：把执行参数翻译成人话。

分工红线（用户 2026-11-01 决策）：
- **字段白名单、中文标签、金额格式化全部确定性完成**——LLM 无权决定「哪些键能出卡」，
  否则它可能把 `budget_hold_id` 原样念出来（泄漏），或把 9900 分说成 990 元（违反事实零生成）。
- LLM 只能在这一层之后做措辞美化（见 `web/wording.py`），且失败即退回模板。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

_INTERNAL_KEYS = frozenset({
    "user_id", "occasion_id", "budget_hold_id", "hold_id", "account_id", "card_id",
    "product_id", "payee_id", "payer_id", "subscription_id", "schedule_id",
    "grant", "span", "idempotency_key",
})
"""内部键：参与执行与幂等，但**永不**作为呈现内容。"""


@dataclass(frozen=True)
class FactLine:
    """一条可展示事实。`value` 只能是已计算好的字符串，不在模板里做算术。"""

    label: str
    value: str
    emphasis: bool = False


def drop_internal(params: Mapping[str, object]) -> dict[str, object]:
    """剔除内部键；不做其它加工。"""
    return {key: value for key, value in params.items() if key not in _INTERNAL_KEYS}


def yuan(cents: object) -> str:
    """分 → 元，两位小数。非整数返回空串（宁可不显示也不猜）。"""
    if not isinstance(cents, int) or isinstance(cents, bool):
        return ""
    return f"{cents / 100:.2f}"


_FIELD_SPECS: Mapping[str, tuple[tuple[str, str], ...]] = {
    "merchant.order": (
        ("merchant", "商户"), ("amount_cents", "下单金额（元）"),
        ("order_on", "送达日期"), ("occasion_id", "场合"),
    ),
    "funds.freeze": (
        ("amount_cents", "冻结金额（元）"), ("reason", "用途"), ("account_id", "账户"),
    ),
    "funds.release": (("amount_cents", "解冻金额（元）"), ("hold_id", "冻结记录")),
}


def fact_lines(action: str, params: Mapping[str, object]) -> tuple[FactLine, ...]:
    """按白名单输出事实行；未登记的 action 返回空元组（调用方保留原有渲染）。"""
    specs = _FIELD_SPECS.get(action)
    if specs is None:
        return ()
    lines: list[FactLine] = []
    for key, label in specs:
        if key in _INTERNAL_KEYS:
            continue
        raw = params.get(key)
        if raw is None:
            continue
        if key.endswith("_cents") or key.endswith("_id") and key != "occasion_id":
            value = yuan(raw)
        else:
            value = str(raw)
        if not value:
            continue
        lines.append(FactLine(label=label, value=value, emphasis=key == "amount_cents"))
    return tuple(lines)
```

- [ ] **Step 3: 接到计划卡摘要**

在 `plan_card.py` 的 `plan_summary()` 里，节点摘要生成后追加白名单行（保留既有行，避免影响 25 个动作外观）：

```python
# 在 plan_summary 的返回前追加：
extra: list[PlanLine] = []
for node in plan.nodes:
    for fact in fact_lines(node.action, node.params):
        extra.append(PlanLine(fact.label, fact.value, emphasis=fact.emphasis))
return (*existing, *extra[:6])
```

（`fact_lines` 需在文件顶部 `from ai_banking_copilot.web.present import fact_lines`。）

- [ ] **Step 4: 门禁**

Run: `ruff check .` → 期望 `All checks passed!`
Run: `mypy` → 期望 `Success: no issues found`

- [ ] **Step 5: 提交**

```bash
git add src/ai_banking_copilot/web/present.py src/ai_banking_copilot/web/plan_card.py tests/web/test_present_fields.py
git commit -m "feat(life): 卡片呈现层字段白名单，内部键不出卡"
```

---

## Task 2: 措辞层 `wording.py`（LLM 只改措辞）

**Files:**
- Create: `src/ai_banking_copilot/web/wording.py`
- Modify: `src/ai_banking_copilot/web/session.py`（失败 / 提醒文案走 `polish`）

- [ ] **Step 1: 新建 `wording.py`**

```python
"""措辞层：在**已清洗的事实束**上做文字美化。

硬约束：
- 输入是 `FactLine` 列表（不含内部 ID），模型不得新增数字 / 日期 / 商户 / 状态；
- 输出做一次事实回查：出现事实束之外的数字或日期即判失败，退回模板；
- `AIB_DISABLE_LLM=1`、无模型、超时、异常一律退回模板（绝不阻塞主链路）。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Sequence

from ai_banking_copilot.web.present import FactLine

LOGGER = logging.getLogger(__name__)

_MONEY_LIKE = re.compile(r"\d+(?:\.\d+)?")
_DATE_LIKE = re.compile(r"\d{4}-\d{2}-\d{2}")

Polisher = Callable[[str, Sequence[FactLine]], str | None]
"""`(模板文案, 事实束) -> 改写结果`；返回 `None` 表示不可用。"""


def polish(template: str, facts: Sequence[FactLine], polisher: Polisher | None) -> str:
    """**任何异常都退回模板**：措辞永远不是主链路。"""
    if polisher is None:
        return template
    try:
        candidate = polisher(template, facts)
    except Exception:
        LOGGER.warning("措辞层失败，退回模板", exc_info=True)
        return template
    if not isinstance(candidate, str) or not candidate.strip():
        return template
    if _adds_unknown_facts(candidate, template, facts):
        LOGGER.warning("措辞层引入事实束之外的数值，退回模板")
        return template
    return candidate.strip()


def _adds_unknown_facts(candidate: str, template: str, facts: Sequence[FactLine]) -> bool:
    """只允许出现模板或事实束里已有的数字 / 日期。"""
    allowed = set(_MONEY_LIKE.findall(template)) | set(_DATE_LIKE.findall(template))
    for line in facts:
        allowed.update(_MONEY_LIKE.findall(line.value))
        allowed.update(_DATE_LIKE.findall(line.value))
    produced = set(_MONEY_LIKE.findall(candidate)) | set(_DATE_LIKE.findall(candidate))
    return bool(produced - allowed)
```

- [ ] **Step 2: 接入失败文案（Q13）**

在 `session.py` 的 `_release_open_holds` 里，把拼接串改成模板 + `polish`：

```python
template = f"{self._message}；已释放冻结预算 {len(released)} 笔"
self._message = polish(template, (), getattr(self._deps, "wording_polisher", None))
```

`AppDeps` 增加可选字段 `wording_polisher: wording.Polisher | None = None`（默认 `None`，行为与现在一致）。

- [ ] **Step 3: 门禁**

Run: `ruff check .` → `All checks passed!`
Run: `mypy` → `Success: no issues found`

- [ ] **Step 4: 提交**

```bash
git add src/ai_banking_copilot/web/wording.py src/ai_banking_copilot/web/session.py src/ai_banking_copilot/web/app.py
git commit -m "feat(life): 措辞层只改文案，事实由白名单保证"
```

---

## Task 3: Q13 失败收尾与重试入口

**Files:**
- Modify: `src/ai_banking_copilot/web/session.py`
- Modify: `src/ai_banking_copilot/web/life_event_store.py`
- Modify: `src/ai_banking_copilot/web/app.py`
- Create: `tests/web/test_due_failure_compensation.py`

- [ ] **Step 1: 写测试文件（不执行）**

```python
"""Q13：下单失败后不得留下半截冻结；failed 预约可重试。"""

from __future__ import annotations


def test_failed_schedule_can_be_retried_by_user(store) -> None:  # noqa: ANN001
    schedule = store.schedule(
        user_id="u_demo", session_id="s", event_id="L1", event_title="生日",
        due_on=__import__("datetime").date(2026, 11, 4), plan_id="life_L1",
        nodes=({"node_id": "n1", "action": "funds.freeze", "params": {}, "depends_on": []},),
    )
    assert store.finish_schedule(
        user_id="u_demo", schedule_id=schedule.schedule_id, status="failed"
    ) == 1
    assert store.retry_schedule(user_id="u_demo", schedule_id=schedule.schedule_id) == 1
    assert store._get_schedule("u_demo", schedule.schedule_id).status == "scheduled"


def test_retry_counts_are_bounded(store) -> None:  # noqa: ANN001
    schedule = store.schedule(
        user_id="u_demo", session_id="s", event_id="L1", event_title="生日",
        due_on=__import__("datetime").date(2026, 11, 4), plan_id="life_L1",
        nodes=({"node_id": "n1", "action": "funds.freeze", "params": {}, "depends_on": []},),
    )
    for _ in range(3):
        store.finish_schedule(user_id="u_demo", schedule_id=schedule.schedule_id, status="failed")
        store.retry_schedule(user_id="u_demo", schedule_id=schedule.schedule_id)
    assert store.retry_schedule(user_id="u_demo", schedule_id=schedule.schedule_id) == 0
```

（`store` fixture 按仓库既有 `tests/web/` 约定创建；若没有现成 fixture，就在该文件内用 `tmp_path` 建一个 `Store` + `init_life_event_schema`。）

- [ ] **Step 2: store 新增 `retry_schedule` 与列**

在 `life_event_store.py`：

1. 建表语句加三列（新库）：
```sql
    kind         TEXT NOT NULL DEFAULT 'order',
    notify_on    TEXT,
    retry_count  INTEGER NOT NULL DEFAULT 0,
```
2. `init_life_event_schema` 末尾加幂等迁移：
```python
_ADDED_COLUMNS = (
    ("kind", "TEXT NOT NULL DEFAULT 'order'"),
    ("notify_on", "TEXT"),
    ("retry_count", "INTEGER NOT NULL DEFAULT 0"),
)

def _migrate(store: Store) -> None:
    with store.transaction() as conn:
        existing = {str(row["name"]) for row in conn.execute("PRAGMA table_info(life_event_schedules)")}
        for name, ddl in _ADDED_COLUMNS:
            if name not in existing:
                conn.execute(f"ALTER TABLE life_event_schedules ADD COLUMN {name} {ddl}")
```
3. 新增方法：
```python
MAX_SCHEDULE_RETRIES = 3

def retry_schedule(self, *, user_id: str, schedule_id: str, now: datetime | None = None) -> int:
    """把 `failed` 预约放回 `scheduled`；超过上限返回 0（不无限重试）。"""
    moment = (now if now is not None else self._clock()).isoformat()
    rows = self._store.query(
        "SELECT retry_count FROM life_event_schedules"
        " WHERE user_id = ? AND schedule_id = ? AND status = ?",
        (user_id, schedule_id, SCHEDULE_FAILED),
    )
    if not rows or int(rows[0]["retry_count"]) >= MAX_SCHEDULE_RETRIES:
        return 0
    with self._store.transaction() as conn:
        cursor = conn.execute(
            "UPDATE life_event_schedules SET status = ?, retry_count = retry_count + 1,"
            " updated_at = ? WHERE user_id = ? AND schedule_id = ? AND status = ?",
            (SCHEDULE_SCHEDULED, moment, user_id, schedule_id, SCHEDULE_FAILED),
        )
        return cursor.rowcount
```
4. `LifeEventSchedule` 增加 `kind: str = "order"`、`notify_on: date | None = None`、`retry_count: int = 0`，并在 `_schedule_from_row` 里读出。

- [ ] **Step 3: 失败文案与 `released_holds`**

在 `session.py`：

1. `ExecutionView` 增加字段：
```python
    released_holds: tuple[str, ...] = ()
    """本次终态失败时已退回的冻结记录；空 = 没有需要退回的冻结。"""
```
2. `_release_open_holds` 返回前写入：
```python
self._released_holds = tuple(released)
```
（`__init__` 与 `_reset` 里都初始化为 `()`；`_execution_view()` 把它放进 `ExecutionView`。）
3. 失败文案在三处 `except` 分支统一追加：
```python
self._message = f"执行失败（{node.action}）：{error}；已释放冻结 {len(released)} 笔，后续步骤未执行"
```

- [ ] **Step 4: 端点**

在 `app.py` 新增两个路由（放在 `/api/due` 之后）：

```python
class _ScheduleRequest(BaseModel):
    session_id: str = DEFAULT_SESSION_ID
    schedule_id: str

@app.post("/api/life-schedules/retry")
def retry_life_schedule(request: _ScheduleRequest, user_id: str = Depends(current_user)) -> dict[str, object]:
    store = deps.life_events
    if store is None:
        raise HTTPException(status_code=409, detail="生活事件存储未装配")
    changed = store.retry_schedule(user_id=user_id, schedule_id=request.schedule_id)
    if not changed:
        raise HTTPException(status_code=409, detail="该预约不可重试（未失败或已达重试上限）")
    return {"retried": True, "schedule_id": request.schedule_id}

@app.post("/api/life-schedules/cancel")
def cancel_life_schedule(request: _ScheduleRequest, user_id: str = Depends(current_user)) -> dict[str, object]:
    store = deps.life_events
    if store is None:
        raise HTTPException(status_code=409, detail="生活事件存储未装配")
    changed = store.finish_schedule(
        user_id=user_id, schedule_id=request.schedule_id, status=SCHEDULE_CANCELLED,
    )
    if not changed:
        raise HTTPException(status_code=409, detail="该预约不可取消")
    return {"cancelled": True, "schedule_id": request.schedule_id}
```

- [ ] **Step 5: 门禁**

Run: `ruff check .` → `All checks passed!`
Run: `mypy` → `Success: no issues found`

- [ ] **Step 6: 提交**

```bash
git add src/ai_banking_copilot/web/life_event_store.py src/ai_banking_copilot/web/session.py src/ai_banking_copilot/web/app.py tests/web/test_due_failure_compensation.py
git commit -m "fix(life): 失败收尾列出已释放冻结，failed 预约可重试/放弃"
```

---

## Task 4: ME-02 未登录只读浏览

**Files:**
- Modify: `src/ai_banking_copilot/web/app.py`
- Modify: `src/ai_banking_copilot/web/assets/app.js`
- Create: `tests/web/test_anonymous_browse.py`

- [ ] **Step 1: 写测试文件（不执行）**

```python
"""ME-02：未登录可看页面壳与只读演示数据，写接口仍 401。"""

from __future__ import annotations


def test_anonymous_can_open_page_shell(client) -> None:  # noqa: ANN001
    response = client.get("/")
    assert response.status_code == 200
    assert "演示数据" in response.text


def test_anonymous_readonly_api_is_marked_demo(client) -> None:  # noqa: ANN001
    response = client.get("/api/life-events")
    assert response.status_code == 200
    assert response.json()["demo"] is True


def test_anonymous_write_api_is_rejected(client) -> None:  # noqa: ANN001
    response = client.post("/api/submit", json={"session_id": "s", "utterance": "查余额"})
    assert response.status_code == 401
```

- [ ] **Step 2: 放宽页面入口**

`app.py` 的 `_page_or_login`：

```python
def _page_or_login(request: Request, html: str) -> HTMLResponse:
    """页面入口：未登录也发页面壳（只读演示），但写接口一律 401。"""
    if deps.auth_mode == "demo" and not _authenticated(request):
        return HTMLResponse(
            _anonymous_shell(html), headers={"Cache-Control": "no-store"},
        )
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})
```

`_anonymous_shell(html)` 在 `</body>` 前注入：

```python
_ANONYMOUS_BANNER = (
    '<div class="anon-banner" role="status">演示数据 · 未登录只读；'
    '办理业务请先 <a href="/login">登录</a>。</div>'
)
```

- [ ] **Step 3: 只读接口给演示身份**

```python
_DEMO_READONLY_PATHS = frozenset({
    "/api/life-events", "/api/insights", "/api/bills/summary", "/api/bills/analysis",
})

def _read_user(request: Request) -> str:
    """只读接口身份：已登录用真实身份，未登录落到演示身份并标 `demo`。"""
    user_id = _optional_user_id(request)
    return user_id if user_id is not None else DEMO_USER_ID

def _is_anonymous(request: Request) -> bool:
    return _optional_user_id(request) is None
```

把上述四个 GET 路由的 `Depends(current_user)` 换成 `Depends(_read_user)`，并在响应体里加：

```python
"demo": _is_anonymous(request),
```

**写接口（`/api/submit`、`/api/confirm`、`/api/clarify`、`/api/due`、`/api/verify/request`、测评、订阅变更）一律保持 `Depends(current_user)`，不放宽。**

- [ ] **Step 4: 前端 banner 与写入口**

`app.js`：

```javascript
function renderAnonymousBanner() {
  const flag = document.documentElement.dataset.demoReadonly === "1";
  document.body.classList.toggle("is-anonymous", flag);
}

document.addEventListener("click", (event) => {
  const target = event.target.closest("[data-requires-login]");
  if (!target) return;
  event.preventDefault();
  window.location.href = "/login";
}, true);
```

样式追加 `.anon-banner` 与 `.is-anonymous [data-requires-login]{opacity:.6}`。

- [ ] **Step 5: 门禁**

Run: `ruff check .` → `All checks passed!`
Run: `mypy` → `Success: no issues found`

- [ ] **Step 6: 提交**

```bash
git add src/ai_banking_copilot/web/app.py src/ai_banking_copilot/web/assets/app.js tests/web/test_anonymous_browse.py
git commit -m "feat(web): 未登录可只读浏览，写接口仍需登录"
```

---

## Task 5: Q16 提醒解析与登记

**Files:**
- Modify: `src/ai_banking_copilot/web/session.py`（`_l1_fill`、`confirm`、`_register_life_schedule`）
- Modify: `src/ai_banking_copilot/scenarios/pending.py`（提醒文案）
- Create: `tests/web/test_reminder_schedule.py`

**关键设计**：`notify_on` 不进 `PlanNode.params`（会破坏 PEP 节点成员资格），只作为槽位存在 `self._review_intents`（该字段已在 `_SNAPSHOT_FIELDS` 里，重启后仍可读到），登记时用它作 `due_on`。

- [ ] **Step 1: 写测试文件（不执行）**

```python
"""Q16：『前 2 天提醒』登记在提醒日，下单日仍是生日当天。"""

from __future__ import annotations

from ai_banking_copilot.web.session import _l1_slot_reminder


def test_reminder_lead_is_parsed_from_text() -> None:
    assert _l1_slot_reminder("我爱人生日前2天提醒我订蛋糕和鲜花，预算500") == 2
    assert _l1_slot_reminder("提前 3 天提醒我") == 3
    assert _l1_slot_reminder("我下周三生日，帮我订一束花") is None
```

- [ ] **Step 2: 解析提醒天数**

`session.py` 新增模块级函数并在 `_l1_fill` 里调用：

```python
_REMINDER_LEAD = re.compile(r"(?:提前|前)\s*(\d{1,2})\s*天(?:前)?\s*(?:提醒|通知|告诉我)?")

def _l1_slot_reminder(text: str) -> int | None:
    """『生日前 2 天提醒我』→ 2；读不到返回 None（不猜）。"""
    match = _REMINDER_LEAD.search(text)
    if match is None:
        return None
    lead = int(match.group(1))
    return lead if 0 < lead <= 30 else None
```

在 `_l1_fill` 的 `parsed_day` 块之后追加：

```python
    lead = _l1_slot_reminder(text)
    order_day = parsed_day.when if parsed_day.when is not None else today
    if lead is not None:
        notify_on = order_day - timedelta(days=lead)
        slots["remind_lead_days"] = lead
        slots["notify_on"] = notify_on.isoformat()
```

- [ ] **Step 3: 登记时用提醒日作 `due_on`**

`confirm()` 里的未来日期分支改为：

```python
        future_order = _future_order_node(pending.plan, self._deps.today)
        if future_order is not None and self._deps.life_events is not None:
            node, order_day = future_order
            notify_on = self._reminder_day(order_day)
            return self._register_life_schedule(pending, node, notify_on or order_day)
```

新增：

```python
    def _reminder_day(self, order_day: date) -> date | None:
        """从本轮已采纳的槽位里取提醒日；取不到返回 None（按原下单日登记）。"""
        for item in reversed(self._review_intents):
            slots = item.get("slots")
            if not isinstance(slots, Mapping):
                continue
            raw = slots.get("notify_on")
            if isinstance(raw, str):
                try:
                    parsed = date.fromisoformat(raw)
                except ValueError:
                    return None
                return parsed if parsed < order_day else None
        return None
```

`_register_life_schedule` 增加 `kind` 形参并按提醒日写 `notify_on`：

```python
    schedule = store.schedule(
        ...,
        kind="reminder" if notify_on is not None else "order",
        notify_on=notify_on,
        ...
    )
```

`store.schedule` 相应加两个可选形参并写入列。

- [ ] **Step 4: 提案文案按 kind 分支**

`scenarios/pending.py` 的 `_life_nodes` / `PendingHandler.build` 取 `LifeDueSchedule.kind`，并在 `SubmissionView.message` 里用：

```python
if schedule.kind == "reminder":
    message = (
        f"提醒：{(schedule.notify_on or schedule.due_on).isoformat()} 起可下单，"
        f"{order_on} 送达；预算 {budget_yuan} 元。确认后执行，今天不扣款。"
    )
```

（`budget_yuan` 由该预约 `funds.freeze` 节点的 `amount_cents` 经 `present.yuan` 得到。）

- [ ] **Step 5: 门禁**

Run: `ruff check .` → `All checks passed!`
Run: `mypy` → `Success: no issues found`

- [ ] **Step 6: 提交**

```bash
git add src/ai_banking_copilot/web/session.py src/ai_banking_copilot/web/life_event_store.py src/ai_banking_copilot/scenarios/pending.py tests/web/test_reminder_schedule.py
git commit -m "feat(life): 支持『生日前 N 天提醒』，提醒日与下单日分离"
```

---

## Task 6: 飞书推送（`reminders.py` + 按级别分流）

**Files:**
- Modify: `src/ai_banking_copilot/web/channels/feishu.py`
- Create: `src/ai_banking_copilot/web/reminders.py`
- Modify: `src/ai_banking_copilot/web/app.py`（启动时挂载）
- Create: `tests/web/test_feishu_card_level.py`

- [ ] **Step 1: 写测试文件（不执行）**

```python
"""飞书按钮按级别分流：YELLOW 可确认，RED / 缺数据只给链接。"""

from __future__ import annotations

from ai_banking_copilot.web.channels.contracts import OutboundMessage
from ai_banking_copilot.web.channels.feishu import FeishuAdapter, FeishuConfig


def _outbound(level: str) -> OutboundMessage:
    return OutboundMessage(
        chat_id="oc_x", state="awaiting_confirm", level=level,
        card={"card_id": "c1", "frozen_hash": "h1"},
    )


def test_yellow_card_has_confirm_button() -> None:
    adapter = FeishuAdapter(FeishuConfig(), clock=lambda: 0.0)
    elements = adapter.render(_outbound("YELLOW"))["elements"]
    assert any(item.get("tag") == "action" for item in elements)


def test_red_card_only_has_url_button() -> None:
    adapter = FeishuAdapter(FeishuConfig(), clock=lambda: 0.0)
    elements = adapter.render(_outbound("RED"))["elements"]
    actions = [item for item in elements if item.get("tag") == "action"]
    assert actions
    buttons = actions[0]["actions"]
    assert all(button.get("url") for button in buttons)
    assert all("value" not in button for button in buttons)
```

- [ ] **Step 2: `send()` 支持按人推送**

`FeishuAdapter.send()` 的 URL 改为按 `receive_id_type` 拼接：

```python
    def send(self, outbound: OutboundMessage, *, receive_id_type: str = "chat_id", **kwargs: object) -> int:
        ...
        url = (
            "https://open.feishu.cn/open-apis/im/v1/messages"
            f"?receive_id_type={receive_id_type}"
        )
        # payload 里的 receive_id 用 outbound.chat_id，调用方传 open_id 时复用同一字段
```

`reminders.py` 调用时传 `receive_id_type="open_id"`。

- [ ] **Step 3: `render()` 按级别分流**

```python
    def render(self, outbound: OutboundMessage) -> Mapping[str, object]:
        rendered = dict(render_feishu_card(outbound))
        if outbound.state != "awaiting_confirm":
            return rendered
        level = outbound.level or ""
        if level == "YELLOW":
            button = {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "确认办理"},
                "type": "primary",
                "value": {"action": "confirm", "card_id": card_id, "frozen_hash": frozen_hash},
            }
        else:
            button = {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "打开网站处理"},
                "type": "default",
                "url": f"{_WEB_BASE}/?focus=due&card={card_id}",
            }
        elements.append({"tag": "action", "actions": [button]})
```

`_WEB_BASE` 取 `AIB_PUBLIC_BASE_URL`，缺省 `http://127.0.0.1:8000`。

- [ ] **Step 4: `reminders.py`**

```python
"""到期推送：站内由 `/api/due` 承担，本模块只负责把同一条提案推到飞书。

纪律：
- **幂等**：`event_id = push:{user_id}:{schedule_id}:{due_on}:{kind}` 落 `channel_audit`，命中即跳过；
- **只推本人**：收件人是该用户绑定的飞书 `open_id`（私聊），不使用群 `chat_id`；
- **不谎报**：未配置凭据 / replay 模式不真发，落审计 `outcome=replay`。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

LOGGER = logging.getLogger(__name__)
TICK_SECONDS = 60

async def run_forever(tick: Callable[[], Awaitable[int]]) -> None:
    """常驻循环：单轮异常只记日志，不让任务退出。"""
    while True:
        try:
            await tick()
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.warning("到期推送一轮失败", exc_info=True)
        await asyncio.sleep(TICK_SECONDS)
```

`app.py` 的 lifespan 里：

```python
if deps.life_events is not None and deps.feishu is not None:
    task = asyncio.create_task(reminders.run_forever(lambda: _push_due_once(deps)))
    ...
    task.cancel()
```

`_push_due_once(deps)`：查 `channel_identities` 里 `channel='feishu'` 的绑定 → 对每个 `user_id` 起一个 `Session` 调 `check_due()` → 有 card 就渲染并按级别发 → 每个预约写一条 `channel_audit`（`event_id` 用上面的幂等键）。

- [ ] **Step 5: 门禁**

Run: `ruff check .` → `All checks passed!`
Run: `mypy` → `Success: no issues found`

- [ ] **Step 6: 提交**

```bash
git add src/ai_banking_copilot/web/channels/feishu.py src/ai_banking_copilot/web/reminders.py src/ai_banking_copilot/web/app.py tests/web/test_feishu_card_level.py
git commit -m "feat(feishu): 到期与提醒可推送，黄卡可确认、红卡与缺数据卡回网站"
```

---

## Task 7: Q17 理财到期联动

**Files:**
- Modify: `src/ai_banking_copilot/web/app.py`（`/api/life-events` 增 `maturities`）
- Modify: `src/ai_banking_copilot/scenarios/life_events.py`（到期日推导 + 联动链）
- Modify: `src/ai_banking_copilot/web/session.py`（`_SCHEDULABLE_ACTIONS` 加 `wealth.redeem`）
- Create: `tests/web/test_maturity_linkage.py`

- [ ] **Step 1: 写测试文件（不执行）**

```python
"""Q17：到期日 = 起息日 + 产品期限；未到期不提案。"""

from __future__ import annotations

from datetime import date

from ai_banking_copilot.scenarios.life_events import holding_maturity


def test_maturity_is_purchase_plus_term() -> None:
    assert holding_maturity(date(2026, 10, 1), 30) == date(2026, 10, 31)


def test_zero_term_is_rejected() -> None:
    assert holding_maturity(date(2026, 10, 1), 0) is None
```

- [ ] **Step 2: 到期日推导**

`scenarios/life_events.py`：

```python
def holding_maturity(purchased_on: date, term_days: int) -> date | None:
    """到期日 = 起息日 + 产品期限；期限非正返回 None（不猜）。"""
    if term_days <= 0:
        return None
    return purchased_on + timedelta(days=term_days)
```

- [ ] **Step 3: `/api/life-events` 返回 `maturities`**

```python
def _maturities(gateway: FactGateway, user_id: str) -> list[dict[str, object]]:
    products = {r.payload.product_id: r.payload for r in gateway.wealth_products()}
    rows: list[dict[str, object]] = []
    for record in gateway.wealth_holdings_of(user_id):
        holding = record.payload
        product = products.get(holding.product_id)
        if product is None:
            continue
        maturity = holding_maturity(holding.purchased_on, product.term_days)
        if maturity is None:
            continue
        rows.append({
            "product_id": holding.product_id,
            "name": product.name,
            "amount_cents": holding.amount.cents,
            "purchased_on": holding.purchased_on.isoformat(),
            "term_days": product.term_days,
            "maturity_on": maturity.isoformat(),
            "basis": "按起息日 + 产品期限推算，非官方到期公告",
        })
    return sorted(rows, key=lambda item: str(item["maturity_on"]))
```

在 `life_events()` 的返回体里加 `"maturities": _maturities(deps.gateway, user_id),`。

- [ ] **Step 4: 联动预约链**

`scenarios/life_events.py` 新增构建器：

```python
def build_maturity_link(
    params: Mapping[str, object], gateway: FactGateway, *, user_id: str, today: date,
) -> Plan:
    """到期赎回 → 向商户下单：`order_on` 与 `due_on` 都用到期日，到期前不提案。"""
    ledger = AnchorLedger()
    product_id = _text(params, "product", "product")
    holding = _holding_of(gateway, user_id, product_id)
    maturity = holding_maturity(holding.purchased_on, _term_days(gateway, product_id))
    if maturity is None or maturity < today:
        raise ScenarioRefused("该持仓没有可推导的到期日，不能登记到期联动")
    merchant = _opt_text(params, "merchant") or BIRTHDAY_MERCHANT
    order_cents = _opt_cents(params, "amount") or BIRTHDAY_ORDER_CENTS
    return Plan(plan_id="life_maturity_link", nodes=(
        _node(ledger, "n1", "wealth.redeem", {
            "account_id": _account_for_budget(params, gateway, order_cents),
            "product_id": product_id,
            "amount_cents": holding.amount.cents,
        }, detail="到期赎回"),
        _node(ledger, "n2", "merchant.order", {
            "merchant": merchant,
            "amount_cents": order_cents,
            "order_on": maturity.isoformat(),
            "occasion_id": _opt_text(params, "occasion") or f"maturity:{product_id}:{maturity.isoformat()}",
        }, detail="下单结算", depends_on=("n1",)),
    ))
```

- [ ] **Step 5: 允许该链延后登记**

`session.py`：

```python
_SCHEDULABLE_ACTIONS = frozenset({
    "funds.freeze", "merchant.order", "funds.release", "wealth.redeem",
})
```

`_future_order_node` 不变（仍以下单节点 `order_on` 为到期日），因此 `due_on = 到期日`，到期前不会提案。

- [ ] **Step 6: 门禁**

Run: `ruff check .` → `All checks passed!`
Run: `mypy` → `Success: no issues found`

- [ ] **Step 7: 提交**

```bash
git add src/ai_banking_copilot/web/app.py src/ai_banking_copilot/scenarios/life_events.py src/ai_banking_copilot/web/session.py tests/web/test_maturity_linkage.py
git commit -m "feat(life): 理财到期日可推导，支持到期赎回下单联动预约"
```

---

## Task 8: Q18 可见性 + 未理解承接

**Files:**
- Modify: `src/ai_banking_copilot/web/life_event_store.py`（`visibility`）
- Modify: `src/ai_banking_copilot/web/session.py`（隐私语句 → private）
- Modify: `src/ai_banking_copilot/web/triage.py`
- Create: `src/ai_banking_copilot/web/suggest.py`
- Create: `tests/web/test_visibility.py`
- Create: `tests/web/test_suggest_fallback.py`

- [ ] **Step 1: 写测试文件（不执行）**

```python
"""Q18：私密执行不进共享推送；LLM 推荐只能来自固定清单。"""

from __future__ import annotations

from ai_banking_copilot.web.session import wants_private_visibility
from ai_banking_copilot.web.suggest import CAPABILITIES, select_suggestions


def test_privacy_phrase_is_detected() -> None:
    assert wants_private_visibility("别在她常看的账单推送里暴露这笔支出") is True
    assert wants_private_visibility("帮我订一束花") is False


def test_llm_outside_capabilities_falls_back() -> None:
    result = select_suggestions(
        "随便说点什么", llm=lambda _text, _caps: ["联系客服"] * 3,
    )
    assert all(item["text"] in CAPABILITIES for item in result)
    assert len(result) == 3
```

- [ ] **Step 2: `visibility` 列与写入**

`life_event_store.py`：`_ADDED_COLUMNS` 追加 `("visibility", "TEXT NOT NULL DEFAULT 'normal'")`；`create()` 增加 `visibility: str = "normal"` 形参并写入；`LifeEventRecord` 增加同名字段。

- [ ] **Step 3: 隐私识别与记录标记**

`session.py`：

```python
_PRIVATE_HINTS = re.compile(
    r"(?:别|不要|不想).{0,8}(?:暴露|看到|发现|知道)"
    r"|(?:私密|悄悄|保密|惊喜)"
)

def wants_private_visibility(utterance: str) -> bool:
    """命中即按私密处理；解析不出来的按正常处理，不倒过来猜。"""
    return _PRIVATE_HINTS.search(utterance) is not None
```

在 `submit()` 里当命中时给 `self._visibility = "private"`（新增字段，`_reset` 还原 `"normal"`），并在 `store.create(...)` / 执行审计 payload 里带上。

- [ ] **Step 4: 过滤**

记录页列表（`store.list_for_user` 的调用处）与共享推送（`reminders.py`）在 `visibility == "private"` 时：列表折叠为「1 条私密记录（点击查看）」，推送跳过。回执文案固定追加：

```python
_PRIVATE_RECEIPT = (
    "已按私密处理：本应用内的共享账单与推送不会出现这笔。"
    "管不到的：对方手机银行流水、短信通知、以及共同还款人看到的账单，本应用没有权限处理。"
)
```

- [ ] **Step 5: `suggest.py`**

```python
"""未识别意图时的候选功能推荐。

LLM 只能从 `CAPABILITIES`（固定能力清单）里挑，**不得新增清单外入口**、不得给数字或日期；
校验失败 / 无模型 / 超时 → 用规则兜底。这样既不用手写推荐算法，也不会编出不存在的能力。
"""

from __future__ import annotations

CAPABILITIES: dict[str, str] = {
    "查一下余额": "account.balance",
    "查上月账单": "account.transactions",
    "把视频会员的自动续费取消": "subscription.cancel",
    "查一下我的订阅": "subscription.list",
    "我要做风险测评": "wealth.assess",
    "看看我的理财持仓": "wealth.list",
    "我下周生日": "life.L1",
    "给妈妈过生日，预算 800 元": "life.L1",
    "卡片挂失": "card.report_lost",
}

_DEFAULT = ("查一下余额", "查上月账单", "查一下我的订阅")


def select_suggestions(utterance: str, *, llm: object = None) -> list[dict[str, str]]:
    """返回 3 条建议；每条都必须命中固定清单。"""
    picked: list[str] = []
    if callable(llm):
        try:
            raw = llm(utterance, tuple(CAPABILITIES))
        except Exception:
            raw = None
        if isinstance(raw, (list, tuple)):
            picked = [item for item in raw if isinstance(item, str) and item in CAPABILITIES]
    if len(picked) < 3:
        picked.extend(item for item in _DEFAULT if item not in picked)
    return [{"label": item, "text": item} for item in picked[:3]]
```

`triage.py` 的 `_FALLBACK_MESSAGE` 分支改为调用 `select_suggestions` 并把建议塞进 `TriageVerdict.suggestions`。

- [ ] **Step 6: 门禁**

Run: `ruff check .` → `All checks passed!`
Run: `mypy` → `Success: no issues found`

- [ ] **Step 7: 提交**

```bash
git add src/ai_banking_copilot/web/life_event_store.py src/ai_banking_copilot/web/session.py src/ai_banking_copilot/web/triage.py src/ai_banking_copilot/web/suggest.py tests/web/test_visibility.py tests/web/test_suggest_fallback.py
git commit -m "feat(life): 私密执行不进共享推送，未识别意图给受约束的功能推荐"
```

---

## Task 9: 前端标注与收尾自检

**Files:**
- Modify: `src/ai_banking_copilot/web/assets/app.js`
- Modify: `src/ai_banking_copilot/web/app.py`（`_submission_payload` 透出 `released_holds`）

- [ ] **Step 1: 日历三类点**

`app.js` 的生活事件日历渲染处，把记录点分成三类并给不同类名：

```javascript
function calendarDotKind(record, maturities) {
  if (maturities.some((item) => item.maturity_on === record.due_on)) return "maturity";
  if (record.schedule_kind === "reminder") return "reminder";
  return "order";
}
```

CSS 追加 `.cal-dot--reminder` / `.cal-dot--maturity`，并在图例里写明「到期日为推算值」。

- [ ] **Step 2: 重试按钮与私密折叠**

```javascript
async function onRetrySchedule(scheduleId) {
  const result = await post("/api/life-schedules/retry", { schedule_id: scheduleId });
  if (result.status !== 200) {
    state.notice = interceptHtml("重试未受理", result.data.detail || "请稍后再试", false);
    renderStage();
    return;
  }
  await onDue();
}
```

- [ ] **Step 3: 门禁**

Run: `ruff check .` → `All checks passed!`
Run: `mypy` → `Success: no issues found`

- [ ] **Step 4: 手工核对（不跑 pytest）**

用 `%TEMP%\aib_check_residual.py` 跑一遍三条链路，逐项打印并核对：

1. 「我下周三生日，帮我订一束花，预算 1000 以内」→ 确认后 `scheduled_for` = 下单日；冻结/下单均为 0；
2. 「我爱人生日前2天提醒我订蛋糕和鲜花，预算500」→ 预约 `kind=reminder`、`notify_on = 生日-2`；到期日 `/api/due` 提案文案含「提醒」；卡片无 `occasion_id` / `budget_hold_id`；
3. 人为让下单失败（金额超冻结）→ `released_holds` 非空、预约 `failed`、`/api/life-schedules/retry` 成功把状态放回 `scheduled`。

跑完删除该临时脚本。

- [ ] **Step 5: 提交**

```bash
git add src/ai_banking_copilot/web/assets/app.js src/ai_banking_copilot/web/app.py
git commit -m "feat(life): 日历区分提醒/下单/到期，补重试入口"
```

---

## 自审记录

- **Spec 覆盖**：Q13 → Task 3；Q16 → Task 5 + Task 6；Q17 → Task 7；Q18 → Task 8；ME-02 → Task 4；呈现层前置 → Task 1 + Task 2；日历与页面 → Task 9。
- **占位扫描**：无 TBD / TODO；所有新增模块给出了完整可编译代码；既有文件的改动点给了函数名与精确片段。
- **类型一致性**：`FactLine` / `fact_lines` / `drop_internal` / `yuan`（Task 1）在 Task 2、Task 5 复用；`LifeEventSchedule.kind` / `notify_on` / `retry_count`（Task 3）在 Task 5 使用；`holding_maturity`（Task 7）在 `/api/life-events` 与联动链复用。
- **pytest**：全篇无任何 `pytest` 执行步骤；测试文件只写不跑。

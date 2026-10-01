# 03 · 三色策略引擎（PDP）与可信执行（PEP · 执行门 · 审计链 · 熔断 · saga）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 落地**可信执行内核（安全下限）**——官方三色策略引擎（PDP）＋ 策略执行点（PEP）＋ 执行门（防 TOCTOU）＋ 追加式审计链 ＋ 异常熔断 ＋ 撤销窗口 / saga 补偿，让"计划"只能沿确定性边界落地：**PEP 无裁决不执行**、绿色只含纯查询、`blocked` 吸收态否决、执行后数值出处可校验、熔断只加严不降级。

**Architecture:** `policy/` 与 `planning/`（能力上限）严格解耦。**PDP 是纯函数**（零 I/O）：只吃 `PolicyRequest`（单对象）或 `Plan + NetEffectView`（计划级），产出 `contracts.authorization.Decision`，冲突一律 `strictest`（fail-safe 取更严）。**执行门 = 计划级 + 节点级叠加**：**`gate.assert_frozen(plan, frozen_hash)`** 用 `contracts.plan.cfhash` 把"用户确认的那份 DAG"冻结成**计划级**判据（`frozen_hash == cfhash(plan.nodes)`）；**`pep.manifest_hash(call)`** 对每次工具调用做**节点级**成员资格校验。**两者叠加**即 PRD 硬约束 #4 的"节点 ∈ 已冻结确认子图"（06 必须在确认记录里**同时保存 `Plan` 与 `cfhash`**）。**PEP 是唯一执行通道**：`tools.registry`（白名单 + 参数强校验）→ `gateway.facts`（只读事实）→ `gateway.core_bank`（幂等写）→ `policy.audit`（JSONL 哈希链）→ `policy.breaker`（只加严熔断）。审计**不占用 01 的 SQLite schema**，改用**追加式 JSONL + sha256 哈希链**；`saga` 提供撤销窗口与补偿，竞态时**如实告知**而非谎称"已取消"。

**Tech Stack:** Python 3.11+ · 标准库 only（`json` / `hashlib` / `dataclasses` / `enum` / `datetime` / `typing`）· pytest · ruff · mypy（`--strict` 只对 `src/`）

## Global Constraints

- Python **≥ 3.11**；**不使用 3.12+ 专属语法**；需要泛型时用 `Generic[T]` / `TypeVar`
- 行宽 **100**；ruff 规则集 `E,F,I,UP,B`；`mypy --strict` 对 `src/` 生效（测试文件不参与 mypy）
- **本计划不引入任何新依赖**（只用标准库；`pyproject.toml` 不改）；`policy/` 不得 import 大模型客户端
- **官方三色不得偏离（H10）**：**绿色只含纯查询**，任何资金 / 状态变更（含小额转账）不得落入绿色；`日累计 ≤ 1000 元 ⇒ 黄`、`> 1000 元 ⇒ 红`
- **`L_final = max(base, 各 modifier)`**；modifier **只做单向升档**；规则冲突取**更严**（fail-safe）
- **对象维度只升档不降档**；`blocked` 为**吸收态** ⇒ `DENY`（与金额无关，唯一可压制金额维度者）
- **`日累计 > 1000 ⇒ L_final ≥ RED`**（金额下限保护，与对象维度无关）；**首次向某对象大额 ⇒ 强制升档**
- **多候选收款人不产出级别**（强制澄清）：PDP 拒绝为候选 ≥ 2 的对象定级
- **净效应**：`NE ≥ NET_EFFECT_THRESHOLD_YUAN ⇒ 计划级 ≥ 红`；`NE = 0 ⇒ 计划级 ≤ 黄`
- **执行门 = 计划级 + 节点级叠加**：`gate.assert_frozen(plan, frozen_hash)` 做**计划级**冻结校验（`frozen_hash == cfhash(plan.nodes)`）；`pep.manifest_hash(call)` 做**节点级**成员资格校验；**任一不匹配即拒**（防 TOCTOU）
- **PEP 无裁决不执行**；级别 ≥ `MANUAL`（L3/L4）即拒；**执行后**必须用 `FactGateway.verify` 做数值出处校验
- **熔断只加严不降级**（hard）：打开后成功不得改回 `CLOSED`，唯一出口是显式 `unlock()`
- **`untrusted` 永不进入控制槽**（H3）：PEP 侧再兜一道 taint 校验
- **撤销竞态不得谎称"已取消"**；部分成功必须 saga 补偿并如实告知
- **不得修改 01 的 `gateway/store.py::SCHEMA_SQL`**；审计用独立 JSONL 文件，`policy/` 不新增 SQLite 表
- **不得修改 00 / 01 / 02 的既有实现**：只允许新增本计划文件
- **验收纪律：完成的标准是跑通，不是写完**——每个 Task 以测试全绿为通过条件
- 提交信息格式：`<type>: <描述>`（type ∈ feat / fix / test / docs / chore / refactor）
- 跨计划接口见 `README.md` 的「跨计划接口主线（冻结面）」——**本计划产出的符号不得改名**

---

### Task 1: PDP 单对象三色决策表（官方三色主干 + 单向升档）

**Files:**
- Create: `src/ai_banking_copilot/policy/__init__.py`
- Create: `src/ai_banking_copilot/policy/pdp.py`
- Create: `tests/policy/__init__.py`
- Create: `tests/policy/conftest.py`
- Test: `tests/policy/test_pdp.py`

**Interfaces:**
- Consumes: `contracts.authorization`（`Level` / `ReasonCode` / `Decision` / `strictest` / `green_or_raise`）、`gateway.models.Money`
- Produces:
  - `DAILY_LIMIT_YUAN: int = 1000` · `NET_EFFECT_THRESHOLD_YUAN: int = 1000`
  - `FLAG_OFF_HOURS` / `FLAG_DEVICE_CHANGE` / `FLAG_HIGH_FREQUENCY` / `FLAG_INJECTION_DETECTED` / `FLAG_FIRST_LARGE_TO_OBJECT` · `KNOWN_CONTEXT_FLAGS: frozenset[str]`
  - `ObjectState(str, Enum)`：`SELF` / `KNOWN_UNVERIFIED` / `VERIFIED_SAVED` / `UNKNOWN` / `BLOCKED`
  - `AmbiguousObjectError(ValueError)`
  - `PolicyRequest`（frozen）：`user_id: str` / `action: str` / `amount: Money` / `daily_accumulated: Money` / `object_state: ObjectState` / `is_pure_query: bool` / `irreversible: bool` / `context_flags: frozenset[str] = frozenset()`
  - `evaluate(request: PolicyRequest) -> Decision`
  - `reject_ambiguous(candidate_count: int) -> None`

- [ ] **Step 1: 写失败的测试**

`tests/policy/__init__.py`

```python
```

`tests/policy/conftest.py`

```python
"""policy 测试共享夹具：复用 01 的 Store / init_schema / seed_database（与 tests/gateway/conftest.py 同风格）。"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from ai_banking_copilot.gateway.core_bank import CoreBank
from ai_banking_copilot.gateway.facts import FactGateway
from ai_banking_copilot.gateway.seed import seed_database
from ai_banking_copilot.gateway.store import Store, StoreMode, init_schema


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """建库 + 播种后的数据库文件（种子数据可复现）。"""
    path = tmp_path / "demo.db"
    store = Store(path)
    init_schema(store)
    seed_database(store)
    store.close()
    return path


@pytest.fixture
def rw_store(db_path: Path) -> Iterator[Store]:
    """读写连接（TCB 写面；PEP 经 CoreBank 写入）。"""
    store = Store(db_path, StoreMode.READ_WRITE)
    yield store
    store.close()


@pytest.fixture
def gateway(db_path: Path) -> Iterator[FactGateway]:
    """只读事实网关（PEP 的出处校验与查询路径）。"""
    store = Store(db_path, StoreMode.READ_ONLY)
    yield FactGateway(store)
    store.close()


@pytest.fixture
def core_bank(rw_store: Store) -> CoreBank:
    """模拟核心银行写原语（与 rw_store 共用同一连接）。"""
    return CoreBank(rw_store)
```

`tests/policy/test_pdp.py`

```python
import pytest

from ai_banking_copilot.contracts.authorization import Level, ReasonCode
from ai_banking_copilot.gateway.models import Money
from ai_banking_copilot.gateway.seed import DEMO_USER_ID
from ai_banking_copilot.policy.pdp import (
    DAILY_LIMIT_YUAN,
    FLAG_DEVICE_CHANGE,
    FLAG_FIRST_LARGE_TO_OBJECT,
    FLAG_INJECTION_DETECTED,
    NET_EFFECT_THRESHOLD_YUAN,
    AmbiguousObjectError,
    ObjectState,
    PolicyRequest,
    evaluate,
    reject_ambiguous,
)


def _request(
    *,
    amount: int = 0,
    daily: int = 0,
    state: ObjectState = ObjectState.SELF,
    pure: bool = False,
    irreversible: bool = False,
    flags: frozenset[str] = frozenset(),
) -> PolicyRequest:
    return PolicyRequest(
        user_id=DEMO_USER_ID,
        action="transfer.create",
        amount=Money(amount),
        daily_accumulated=Money(daily),
        object_state=state,
        is_pure_query=pure,
        irreversible=irreversible,
        context_flags=flags,
    )


def test_official_thresholds_are_anchored() -> None:
    assert DAILY_LIMIT_YUAN == 1000
    assert NET_EFFECT_THRESHOLD_YUAN == 1000


def test_pure_query_is_green() -> None:
    decision = evaluate(_request(pure=True))
    assert decision.level is Level.GREEN
    assert decision.reasons == (ReasonCode.QUERY_ONLY,)


def test_daily_at_limit_is_yellow() -> None:
    decision = evaluate(_request(amount=100000, daily=100000))
    assert decision.level is Level.YELLOW
    assert ReasonCode.STATE_CHANGE_SMALL in decision.reasons


def test_daily_over_limit_is_red() -> None:
    decision = evaluate(_request(amount=100001, daily=100001))
    assert decision.level is Level.RED
    assert ReasonCode.AMOUNT_OVER_DAILY_LIMIT in decision.reasons


def test_blocked_is_deny_regardless_of_amount_and_purity() -> None:
    for request in (
        _request(state=ObjectState.BLOCKED, pure=True),
        _request(state=ObjectState.BLOCKED, amount=100, daily=100),
        _request(state=ObjectState.BLOCKED, amount=99999999, daily=99999999),
    ):
        decision = evaluate(request)
        assert decision.level is Level.DENY
        assert decision.reasons == (ReasonCode.OBJECT_BLOCKED,)


def test_unknown_object_requires_identity_confirmation() -> None:
    decision = evaluate(_request(state=ObjectState.UNKNOWN))
    assert decision.level is Level.YELLOW
    assert ReasonCode.MANUAL_REVIEW in decision.reasons


def test_known_unverified_object_requires_identity_confirmation() -> None:
    assert evaluate(_request(state=ObjectState.KNOWN_UNVERIFIED)).level is Level.YELLOW


def test_verified_object_does_not_lower_amount_band() -> None:
    decision = evaluate(
        _request(amount=150000, daily=150000, state=ObjectState.VERIFIED_SAVED)
    )
    assert decision.level is Level.RED


def test_irreversible_action_is_at_least_red() -> None:
    decision = evaluate(_request(irreversible=True))
    assert decision.level is Level.RED
    assert ReasonCode.IRREVERSIBLE in decision.reasons


def test_fail_safe_conflict_takes_strictest_level() -> None:
    decision = evaluate(_request(pure=True, irreversible=True))
    assert decision.level is Level.RED
    assert ReasonCode.IRREVERSIBLE in decision.reasons


def test_first_large_transfer_to_object_forces_red() -> None:
    flags = frozenset({FLAG_FIRST_LARGE_TO_OBJECT})
    assert evaluate(_request(amount=500, daily=500, flags=flags)).level is Level.RED


def test_injection_flag_escalates_to_manual() -> None:
    flags = frozenset({FLAG_INJECTION_DETECTED})
    assert evaluate(_request(amount=100, daily=100, flags=flags)).level is Level.MANUAL


def test_context_anomaly_raises_green_query_to_yellow() -> None:
    """上下文异常是**只升档**的 modifier：连纯查询也被抬到 ≥ 黄（fail-safe）。"""
    flags = frozenset({FLAG_DEVICE_CHANGE})
    assert evaluate(_request(pure=True, flags=flags)).level is Level.YELLOW


def test_multi_candidate_refuses_to_assign_level() -> None:
    reject_ambiguous(1)
    with pytest.raises(AmbiguousObjectError, match="澄清"):
        reject_ambiguous(2)


def test_request_rejects_amount_above_daily() -> None:
    with pytest.raises(ValueError, match="daily_accumulated"):
        _request(amount=200, daily=100)


def test_request_rejects_unknown_context_flag() -> None:
    with pytest.raises(ValueError, match="未知上下文标记"):
        _request(flags=frozenset({"made_up_flag"}))


def test_request_rejects_bad_object_state_type() -> None:
    with pytest.raises(TypeError, match="object_state"):
        PolicyRequest(
            user_id=DEMO_USER_ID,
            action="transfer.create",
            amount=Money(0),
            daily_accumulated=Money(0),
            object_state="self",  # type: ignore[arg-type]
            is_pure_query=False,
            irreversible=False,
        )
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/policy/test_pdp.py -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'ai_banking_copilot.policy'`

- [ ] **Step 3: 最小实现**

`src/ai_banking_copilot/policy/__init__.py`

```python
"""可信执行内核（PDP / PEP）：三色裁决 · 执行门 · 审计链 · 熔断 · saga 撤销。

分层纪律：本包**不 import 大模型客户端**、**不产事实**；一切数值来自 `gateway`，
一切裁决来自 `contracts.authorization`。
"""
```

`src/ai_banking_copilot/policy/pdp.py`

```python
"""三色策略引擎（PDP）：官方三色主干 + 单向升档增强层。

官方口径（H10 硬约束，不得偏离）：
- 绿色 = 自动执行，**仅纯查询**；任何资金 / 状态变更不得落入绿色。
- 黄色 = 用户确认；`日累计 ≤ 1000 元` ⇒ 黄。
- 红色 = 强验证；`日累计 > 1000 元` ⇒ 红。
- 增强层（红线之上的兜底）：`MANUAL` 人工接管 · `DENY` 拒绝并解释。

合成规则（fail-safe）：
- `base = 决策表(动作风险, 日累计档)`；`modifier` **只做单向升档**；`L_final = max(base, 各 modifier)`。
- 规则冲突取**更严**级别（`contracts.authorization.strictest`，并集原因，保证可解释）。
- 对象维度**只升档、不降档**；`blocked` 为**吸收态** ⇒ `DENY`（唯一可压制金额维度者）。
- 多候选收款人**不产出级别**（本层拒绝定级，强制澄清）。
- **金额下限保护**：`日累计 > 1000 ⇒ L_final ≥ 红`，与对象维度无关。

本模块**纯函数、零 I/O**：不读库、不调模型、不发网络。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from ai_banking_copilot.contracts.authorization import (
    Decision,
    Level,
    ReasonCode,
    green_or_raise,
    strictest,
)
from ai_banking_copilot.gateway.models import Money

DAILY_LIMIT_YUAN: int = 1000
"""官方日累计阈值（元）：`≤ 该值 ⇒ 黄`、`> 该值 ⇒ 红`（PRD §5.2）。"""

NET_EFFECT_THRESHOLD_YUAN: int = 1000
"""计划级净效应阈值 `T_net`（元）：`NE ≥ T_net ⇒ 计划级 ≥ 红`（PRD §5.3）。"""

FLAG_OFF_HOURS = "off_hours"
"""非惯常时段。"""

FLAG_DEVICE_CHANGE = "device_change"
"""会话设备 / 地点跳变。"""

FLAG_HIGH_FREQUENCY = "high_frequency"
"""短时高频高风险操作。"""

FLAG_INJECTION_DETECTED = "injection_detected"
"""注入检测命中（H3 / 官方"注入防御"）。"""

FLAG_FIRST_LARGE_TO_OBJECT = "first_large_to_object"
"""首次向某对象大额（涵盖熟人社工 / 盗号）。"""

KNOWN_CONTEXT_FLAGS: frozenset[str] = frozenset(
    {
        FLAG_OFF_HOURS,
        FLAG_DEVICE_CHANGE,
        FLAG_HIGH_FREQUENCY,
        FLAG_INJECTION_DETECTED,
        FLAG_FIRST_LARGE_TO_OBJECT,
    }
)
"""上下文异常标记白名单：未登记标记一律拒绝，防"凭空加载荷"。"""

_CENT_PER_YUAN = 100
_YELLOW_CONTEXT_FLAGS = frozenset({FLAG_OFF_HOURS, FLAG_DEVICE_CHANGE, FLAG_HIGH_FREQUENCY})


class ObjectState(str, Enum):
    """对象态机（PRD §5.2，唯一谓词求值结果）。`BLOCKED` 为吸收态。"""

    SELF = "self"
    KNOWN_UNVERIFIED = "known_unverified"
    VERIFIED_SAVED = "verified_saved"
    UNKNOWN = "unknown"
    BLOCKED = "blocked"


class AmbiguousObjectError(ValueError):
    """收款对象候选 ≥ 2：**不产出级别**，强制澄清。"""


@dataclass(frozen=True)
class PolicyRequest:
    """一次单对象裁决请求（纯数据）。

    `daily_accumulated` = **含本笔**的当日累计流出额（与 `daily_transfer_total` 同口径）。
    """

    user_id: str
    action: str
    amount: Money
    daily_accumulated: Money
    object_state: ObjectState
    is_pure_query: bool
    irreversible: bool
    context_flags: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        if not isinstance(self.object_state, ObjectState):
            raise TypeError(
                f"object_state 必须是 ObjectState 成员，收到 {type(self.object_state).__name__}"
            )
        if not self.user_id or not self.action:
            raise ValueError("user_id 与 action 不得为空")
        if self.amount.cents < 0:
            raise ValueError("amount 不得为负")
        if self.daily_accumulated.cents < self.amount.cents:
            raise ValueError("daily_accumulated 必须包含本笔金额")
        unknown = self.context_flags - KNOWN_CONTEXT_FLAGS
        if unknown:
            raise ValueError(f"未知上下文标记：{sorted(unknown)}")


def evaluate(request: PolicyRequest) -> Decision:
    """单对象裁决：官方三色主干 + 单向升档，冲突取更严（fail-safe）。"""
    if request.object_state is ObjectState.BLOCKED:
        return Decision(level=Level.DENY, reasons=(ReasonCode.OBJECT_BLOCKED,))
    modifiers: list[Decision | None] = [
        _object_modifier(request.object_state),
        _irreversible_modifier(request.irreversible),
        *_context_modifiers(request.context_flags),
    ]
    effective = tuple(item for item in modifiers if item is not None)
    return strictest(_base_decision(request), *effective)


def reject_ambiguous(candidate_count: int) -> None:
    """多候选收款人**不产出级别**：候选 ≥ 2 时抛错，强制澄清（PRD §5.1）。"""
    if candidate_count >= 2:
        raise AmbiguousObjectError(f"收款对象候选 {candidate_count} 个：不产出级别，强制澄清")


def _base_decision(request: PolicyRequest) -> Decision:
    if request.is_pure_query:
        return green_or_raise(is_pure_query=True)
    return _amount_band_decision(request.daily_accumulated)


def _amount_band_decision(daily_accumulated: Money) -> Decision:
    if daily_accumulated.cents <= DAILY_LIMIT_YUAN * _CENT_PER_YUAN:
        return Decision(level=Level.YELLOW, reasons=(ReasonCode.STATE_CHANGE_SMALL,))
    return Decision(level=Level.RED, reasons=(ReasonCode.AMOUNT_OVER_DAILY_LIMIT,))


def _object_modifier(state: ObjectState) -> Decision | None:
    """陌生 / 未认人对象 ⇒ 追加一次认人确认（**只升档**，≥ 黄）。"""
    if state in (ObjectState.KNOWN_UNVERIFIED, ObjectState.UNKNOWN):
        return Decision(level=Level.YELLOW, reasons=(ReasonCode.MANUAL_REVIEW,))
    return None


def _irreversible_modifier(irreversible: bool) -> Decision | None:
    if irreversible:
        return Decision(level=Level.RED, reasons=(ReasonCode.IRREVERSIBLE,))
    return None


def _context_modifiers(flags: frozenset[str]) -> tuple[Decision, ...]:
    modifiers: list[Decision] = []
    if FLAG_INJECTION_DETECTED in flags:
        modifiers.append(Decision(level=Level.MANUAL, reasons=(ReasonCode.MANUAL_REVIEW,)))
    if flags & _YELLOW_CONTEXT_FLAGS:
        modifiers.append(Decision(level=Level.YELLOW, reasons=(ReasonCode.MANUAL_REVIEW,)))
    if FLAG_FIRST_LARGE_TO_OBJECT in flags:
        modifiers.append(Decision(level=Level.RED, reasons=(ReasonCode.AMOUNT_OVER_DAILY_LIMIT,)))
    return tuple(modifiers)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/policy/test_pdp.py -v`
Expected: PASS（17 passed）

- [ ] **Step 5: 提交**

```bash
git add src/ai_banking_copilot/policy tests/policy
git commit -m "feat: 建立 PDP 单对象三色决策表（官方三色主干 + 单向升档）"
```

---

### Task 2: PDP 计划级裁决（净效应阈值 + 不可逆节点）

**Files:**
- Modify: `src/ai_banking_copilot/policy/pdp.py`（追加 `NetEffectView` / `evaluate_plan` / `_net_effect_decision`）
- Test: `tests/policy/test_pdp.py`（追加计划级用例）

**Interfaces:**
- Consumes: Task 1 的 `pdp`；`contracts.plan.Plan` / `validate_plan`
- Produces:
  - `NetEffectView(Protocol)`：只读属性 `amount: Money` · `irreversibles: tuple[str, ...]`（`planning.NetEffect` 的结构化视图，避免 03 依赖 04）
  - `evaluate_plan(plan: Plan, net: NetEffectView) -> Decision`

- [ ] **Step 1: 写失败的测试**

`tests/policy/test_pdp.py`（在文件末尾追加）

```python
from dataclasses import dataclass

from ai_banking_copilot.contracts.plan import (
    AnchorSource,
    IntentRef,
    Plan,
    PlanNode,
    PlanValidationError,
)
from ai_banking_copilot.policy.pdp import evaluate_plan


@dataclass(frozen=True)
class _Net:
    """`planning.NetEffect` 的最小结构替身（证明 03 不依赖 04）。"""

    amount: Money
    irreversibles: tuple[str, ...] = ()


def _node(node_id: str, amount_cents: int) -> PlanNode:
    return PlanNode(
        node_id=node_id,
        action="transfer.create",
        params={"amount_cents": amount_cents},
        intent=IntentRef(source=AnchorSource.EXPLICIT, anchor_id="a1"),
    )


def _plan(*nodes: PlanNode) -> Plan:
    return Plan(plan_id="p1", nodes=nodes)


def test_plan_net_effect_below_threshold_is_yellow() -> None:
    decision = evaluate_plan(_plan(_node("n1", 99999)), _Net(amount=Money(99999)))
    assert decision.level is Level.YELLOW


def test_plan_net_effect_at_threshold_is_red() -> None:
    decision = evaluate_plan(_plan(_node("n1", 100000)), _Net(amount=Money(100000)))
    assert decision.level is Level.RED
    assert ReasonCode.AMOUNT_OVER_DAILY_LIMIT in decision.reasons


def test_plan_zero_net_effect_is_at_most_yellow() -> None:
    decision = evaluate_plan(_plan(_node("n1", 0)), _Net(amount=Money(0)))
    assert decision.level is Level.YELLOW


def test_plan_with_irreversible_node_is_red_and_unions_reasons() -> None:
    decision = evaluate_plan(
        _plan(_node("n1", 100000)), _Net(amount=Money(100000), irreversibles=("n1",))
    )
    assert decision.level is Level.RED
    assert set(decision.reasons) == {
        ReasonCode.AMOUNT_OVER_DAILY_LIMIT,
        ReasonCode.IRREVERSIBLE,
    }


def test_plan_without_anchor_is_rejected() -> None:
    orphan = PlanNode(node_id="n1", action="transfer.create", params={"amount_cents": 1})
    with pytest.raises(PlanValidationError):
        evaluate_plan(_plan(orphan), _Net(amount=Money(0)))
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/policy/test_pdp.py -v`
Expected: FAIL —— `ImportError: cannot import name 'evaluate_plan'`

- [ ] **Step 3: 最小实现**

`src/ai_banking_copilot/policy/pdp.py`（修改导入段）

```python
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol

from ai_banking_copilot.contracts.authorization import (
    Decision,
    Level,
    ReasonCode,
    green_or_raise,
    strictest,
)
from ai_banking_copilot.contracts.plan import Plan, validate_plan
from ai_banking_copilot.gateway.models import Money
```

`src/ai_banking_copilot/policy/pdp.py`（在 `reject_ambiguous` 之后追加）

```python
class NetEffectView(Protocol):
    """`planning.NetEffect` 的结构化视图（保持 03 不依赖 04）。

    口径（PRD §5.3）：`amount` 为**带符号资金流**的净效应 `NE`（流出为正、取消为负）；
    `irreversibles` 为不可逆节点 id（挂失等，单列触发 ≥ 红）。
    """

    @property
    def amount(self) -> Money: ...

    @property
    def irreversibles(self) -> tuple[str, ...]: ...


def evaluate_plan(plan: Plan, net: NetEffectView) -> Decision:
    """计划级裁决：净效应阈值 + 不可逆节点（防"多步组合越权"）。

    只产出计划级**下限**：纯查询计划的绿色判定由单节点 `evaluate`（`is_pure_query`）承担，
    计划级最终级别 = `strictest(各节点裁决, 本函数下限)`。
    """
    validate_plan(plan)
    decisions: list[Decision] = [_net_effect_decision(net.amount)]
    if net.irreversibles:
        decisions.append(Decision(level=Level.RED, reasons=(ReasonCode.IRREVERSIBLE,)))
    return strictest(*decisions)


def _net_effect_decision(net_amount: Money) -> Decision:
    if net_amount.cents >= NET_EFFECT_THRESHOLD_YUAN * _CENT_PER_YUAN:
        return Decision(level=Level.RED, reasons=(ReasonCode.AMOUNT_OVER_DAILY_LIMIT,))
    return Decision(level=Level.YELLOW, reasons=(ReasonCode.STATE_CHANGE_SMALL,))
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/policy/test_pdp.py -v`
Expected: PASS（22 passed）

- [ ] **Step 5: 提交**

```bash
git add src/ai_banking_copilot/policy/pdp.py tests/policy/test_pdp.py
git commit -m "feat: 建立 PDP 计划级裁决（净效应阈值 + 不可逆节点）"
```

---

### Task 3: 执行门（`cfhash` 冻结判据，防 TOCTOU）

**Files:**
- Create: `src/ai_banking_copilot/policy/gate.py`
- Test: `tests/policy/test_gate.py`

**Interfaces:**
- Consumes: `contracts.plan.Plan` / `cfhash`
- Produces: `StalePlanError(RuntimeError)` · `assert_frozen(plan: Plan, frozen_hash: str) -> None`

- [ ] **Step 1: 写失败的测试**

`tests/policy/test_gate.py`

```python
from dataclasses import replace

import pytest

from ai_banking_copilot.contracts.plan import (
    AnchorSource,
    IntentRef,
    Plan,
    PlanNode,
    cfhash,
)
from ai_banking_copilot.policy.gate import StalePlanError, assert_frozen


def _node(node_id: str, amount_cents: int, deps: tuple[str, ...] = ()) -> PlanNode:
    return PlanNode(
        node_id=node_id,
        action="transfer.create",
        params={"amount_cents": amount_cents},
        intent=IntentRef(source=AnchorSource.EXPLICIT, anchor_id="a1"),
        depends_on=deps,
    )


def _plan(*nodes: PlanNode) -> Plan:
    return Plan(plan_id="p1", nodes=nodes)


def test_matching_frozen_hash_passes() -> None:
    plan = _plan(_node("n1", 100), _node("n2", 200, deps=("n1",)))
    assert_frozen(plan, cfhash(plan.nodes))


def test_control_slot_param_change_is_rejected() -> None:
    plan = _plan(_node("n1", 100))
    frozen = cfhash(plan.nodes)
    tampered = _plan(replace(plan.nodes[0], params={"amount_cents": 99999}))
    with pytest.raises(StalePlanError, match="哈希不匹配"):
        assert_frozen(tampered, frozen)


def test_added_node_is_rejected() -> None:
    plan = _plan(_node("n1", 100))
    frozen = cfhash(plan.nodes)
    with pytest.raises(StalePlanError, match="哈希不匹配"):
        assert_frozen(_plan(*plan.nodes, _node("n2", 100)), frozen)


def test_removed_node_is_rejected() -> None:
    plan = _plan(_node("n1", 100), _node("n2", 200))
    frozen = cfhash(plan.nodes)
    with pytest.raises(StalePlanError, match="哈希不匹配"):
        assert_frozen(_plan(plan.nodes[0]), frozen)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/policy/test_gate.py -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'ai_banking_copilot.policy.gate'`

- [ ] **Step 3: 最小实现**

`src/ai_banking_copilot/policy/gate.py`

```python
"""执行门（**计划级**）：确认后 DAG 一致性校验（防 TOCTOU，PRD §5.1 / §5.4）。

用户确认的是**那份 DAG**（`cfhash` 冻结），不是一段文本：确认后增删改任一节点
或控制槽参数 ⇒ 哈希不匹配 ⇒ 拒绝执行。

**双层叠加**：本模块只做**计划级**冻结校验（`frozen_hash == cfhash(plan.nodes)`）；
**节点级**成员资格校验由 `policy.pep.manifest_hash(call)` 承担（见 Task 7）。二者叠加即
PRD 硬约束 #4 的「节点 ∈ 已冻结确认子图」——06 必须在确认记录里**同时保存 `Plan` 与 `cfhash`**。
"""

from __future__ import annotations

from ai_banking_copilot.contracts.plan import Plan, cfhash


class StalePlanError(RuntimeError):
    """计划在用户确认后被改动：哈希不匹配，执行门拒绝。"""


def assert_frozen(plan: Plan, frozen_hash: str) -> None:
    """执行门判据：`cfhash(plan.nodes)` 必须等于确认时冻结的哈希。"""
    current = cfhash(plan.nodes)
    if current != frozen_hash:
        raise StalePlanError(
            f"执行门拒绝：计划哈希不匹配（确认 {frozen_hash}｜当前 {current}）"
        )
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/policy/test_gate.py -v`
Expected: PASS（4 passed）

- [ ] **Step 5: 提交**

```bash
git add src/ai_banking_copilot/policy/gate.py tests/policy/test_gate.py
git commit -m "feat: 建立执行门（cfhash 冻结判据，防 TOCTOU）"
```

---

### Task 4: 追加式审计链（JSONL + sha256 哈希链）

**Files:**
- Create: `src/ai_banking_copilot/policy/audit.py`
- Modify: `tests/policy/conftest.py`（追加 `audit` fixture）
- Test: `tests/policy/test_audit.py`

**Interfaces:**
- Consumes: `gateway.hashing.canonical_json` / `sha256_hex`
- Produces:
  - `GENESIS_HASH: str = "0" * 64`
  - `KIND_DECISION = "decision"` · `KIND_EXECUTION = "execution"` · `KIND_BREAKER = "breaker"` · `KIND_REVERSAL = "reversal"`
  - `ChainBrokenError(RuntimeError)`
  - `AuditEvent`（frozen）：`event_id: str` / `kind: str` / `payload: dict[str, object]` / `prev_hash: str`
  - `AuditLog(path: str | Path)`：`path`（属性）· `head() -> str` · `append(event) -> str` · `verify_chain() -> bool` · `events() -> tuple[AuditEvent, ...]`

- [ ] **Step 1: 写失败的测试**

`tests/policy/conftest.py`（修改导入段并追加 fixture）

```python
from ai_banking_copilot.policy.audit import AuditLog
```

```python
@pytest.fixture
def audit(tmp_path: Path) -> AuditLog:
    """独立 JSONL 审计链（**不占用 01 的 SQLite schema**）。"""
    return AuditLog(tmp_path / "audit.jsonl")
```

`tests/policy/test_audit.py`

```python
from datetime import date
from pathlib import Path

import pytest

from ai_banking_copilot.gateway.models import Money
from ai_banking_copilot.policy.audit import (
    GENESIS_HASH,
    KIND_DECISION,
    KIND_EXECUTION,
    AuditEvent,
    AuditLog,
    ChainBrokenError,
)


def _event(event_id: str, kind: str = KIND_EXECUTION, prev_hash: str = "") -> AuditEvent:
    return AuditEvent(
        event_id=event_id,
        kind=kind,
        payload={"action": "transfer.create", "level": "RED"},
        prev_hash=prev_hash,
    )


def test_empty_log_has_genesis_head_and_valid_chain(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "audit.jsonl")
    assert log.head() == GENESIS_HASH
    assert log.events() == ()
    assert log.verify_chain() is True


def test_append_links_chain_and_returns_new_head(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "audit.jsonl")
    first = log.append(_event("e1"))
    second = log.append(_event("e2"))
    assert first != second
    assert log.head() == second
    events = log.events()
    assert [event.event_id for event in events] == ["e1", "e2"]
    assert events[0].prev_hash == GENESIS_HASH
    assert events[1].prev_hash == first
    assert log.verify_chain() is True


def test_append_accepts_explicit_correct_prev_hash(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "audit.jsonl")
    first = log.append(_event("e1"))
    log.append(_event("e2", prev_hash=first))
    assert log.verify_chain() is True


def test_append_rejects_stale_prev_hash(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "audit.jsonl")
    log.append(_event("e1"))
    with pytest.raises(ChainBrokenError, match="断裂"):
        log.append(_event("e2", prev_hash="0" * 63 + "1"))


def test_tampered_payload_breaks_chain(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)
    log.append(_event("e1"))
    log.append(_event("e2"))
    path.write_text(
        path.read_text(encoding="utf-8").replace('"RED"', '"GREEN"'), encoding="utf-8"
    )
    assert log.verify_chain() is False


def test_malformed_line_breaks_chain(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)
    log.append(_event("e1"))
    with path.open("a", encoding="utf-8") as handle:
        handle.write("not-json\n")
    assert log.verify_chain() is False


def test_payload_is_canonicalized_on_round_trip(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "audit.jsonl")
    log.append(
        AuditEvent(
            event_id="e1",
            kind=KIND_DECISION,
            payload={"amount": Money(1234), "booked_on": date(2026, 10, 1)},
            prev_hash="",
        )
    )
    payload = log.events()[0].payload
    assert payload["amount"] == {"cents": 1234}
    assert payload["booked_on"] == "2026-10-01"
    assert log.verify_chain() is True


def test_log_is_append_only_jsonl(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)
    log.append(_event("e1"))
    log.append(_event("e2"))
    log.append(_event("e3"))
    assert len(path.read_text(encoding="utf-8").splitlines()) == 3


def test_event_requires_id_and_kind() -> None:
    with pytest.raises(ValueError, match="event_id"):
        AuditEvent(event_id="", kind=KIND_EXECUTION, payload={}, prev_hash="")
    with pytest.raises(ValueError, match="kind"):
        AuditEvent(event_id="e1", kind="", payload={}, prev_hash="")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/policy/test_audit.py -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'ai_banking_copilot.policy.audit'`

- [ ] **Step 3: 最小实现**

`src/ai_banking_copilot/policy/audit.py`

```python
"""追加式审计链：JSONL + sha256 哈希链（**不占用 01 的 SQLite schema**）。

范围声明（H5，必须如实）：审计链只证「**过程合规 + 可追溯**」，**不证「决策正确」**；
签名来自 TCB（本地哈希链），**不来自 LLM**。

记录字段对齐 PRD §5.6 / §9.1：taint 标签 · `intent_ref` · 命中规则 id · 数值出处
`record_id` · 授权级别 · 执行结果 · 决策链路。链路结构：每行 JSON 记录
`{event_id, kind, payload, prev_hash, entry_hash}`，`entry_hash = sha256(canonical(前四者))`。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ai_banking_copilot.gateway.hashing import canonical_json, sha256_hex

GENESIS_HASH: str = "0" * 64
"""创世前驱哈希：`prev_hash` 为空的语义等价物。"""

KIND_DECISION = "decision"
"""裁决事件（含级别、原因码、决策链路）。"""

KIND_EXECUTION = "execution"
"""执行事件（含回执、出处 record_id、执行结果）。"""

KIND_BREAKER = "breaker"
"""熔断事件（含触发项）。"""

KIND_REVERSAL = "reversal"
"""撤销 / 补偿事件（竞态须如实记录"已扣 + 已发起退回"）。"""

_REQUIRED_KEYS = ("event_id", "kind", "payload", "prev_hash", "entry_hash")


class ChainBrokenError(RuntimeError):
    """哈希链断裂：`prev_hash` 与链头不一致，或审计行被篡改 / 非 JSON 对象。"""


@dataclass(frozen=True)
class AuditEvent:
    """一条待追加的审计事件。`prev_hash` 为空串时由 `AuditLog` 自动链接链头。"""

    event_id: str
    kind: str
    payload: dict[str, object]
    prev_hash: str

    def __post_init__(self) -> None:
        if not self.event_id:
            raise ValueError("event_id 不得为空")
        if not self.kind:
            raise ValueError("kind 不得为空")
        if self.prev_hash and len(self.prev_hash) != 64:
            raise ValueError("prev_hash 必须是 64 位十六进制或空串")


class AuditLog:
    """追加式审计链：**只追加、不修改**历史行。"""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def path(self) -> Path:
        return self._path

    def head(self) -> str:
        """当前链头哈希；空链返回 `GENESIS_HASH`。"""
        entries = self._read_entries()
        return str(entries[-1]["entry_hash"]) if entries else GENESIS_HASH

    def append(self, event: AuditEvent) -> str:
        """追加一条事件，返回本条 `entry_hash`（即新链头）。"""
        head = self.head()
        if event.prev_hash and event.prev_hash != head:
            raise ChainBrokenError(
                f"审计链断裂：事件 {event.event_id} 声明 prev_hash={event.prev_hash}，链头={head}"
            )
        entry_hash = _entry_hash(event, head)
        record = _record(event, head)
        record["entry_hash"] = entry_hash
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(canonical_json(record) + "\n")
        return entry_hash

    def verify_chain(self) -> bool:
        """重算全链哈希并校验链接关系；任一不符返回 `False`（不抛异常）。"""
        try:
            entries = self._read_entries()
        except ChainBrokenError:
            return False
        previous = GENESIS_HASH
        for entry in entries:
            if str(entry["prev_hash"]) != previous:
                return False
            try:
                event = _event_of(entry, previous)
            except ChainBrokenError:
                return False
            if _entry_hash(event, previous) != str(entry["entry_hash"]):
                return False
            previous = str(entry["entry_hash"])
        return True

    def events(self) -> tuple[AuditEvent, ...]:
        """按写入顺序返回全部事件（行损坏时抛 `ChainBrokenError`，fail-closed）。"""
        return tuple(_event_of(entry, str(entry["prev_hash"])) for entry in self._read_entries())

    def _read_entries(self) -> list[dict[str, object]]:
        if not self._path.is_file():
            return []
        return [
            _parse_line(line)
            for line in self._path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]


def _record(event: AuditEvent, prev_hash: str) -> dict[str, object]:
    return {
        "event_id": event.event_id,
        "kind": event.kind,
        "payload": event.payload,
        "prev_hash": prev_hash,
    }


def _entry_hash(event: AuditEvent, prev_hash: str) -> str:
    return sha256_hex(canonical_json(_record(event, prev_hash)))


def _event_of(entry: dict[str, object], prev_hash: str) -> AuditEvent:
    payload = entry.get("payload")
    if not isinstance(payload, dict):
        raise ChainBrokenError("审计行 payload 不是 JSON 对象")
    return AuditEvent(
        event_id=str(entry["event_id"]),
        kind=str(entry["kind"]),
        payload={str(key): value for key, value in payload.items()},
        prev_hash=prev_hash,
    )


def _parse_line(line: str) -> dict[str, object]:
    try:
        data = json.loads(line)
    except ValueError as error:
        raise ChainBrokenError(f"审计行不是合法 JSON：{line[:40]!r}") from error
    if not isinstance(data, dict):
        raise ChainBrokenError("审计行不是 JSON 对象")
    parsed = {str(key): value for key, value in data.items()}
    for key in _REQUIRED_KEYS:
        if key not in parsed:
            raise ChainBrokenError(f"审计行缺少字段：{key}")
    return parsed
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/policy/test_audit.py -v`
Expected: PASS（9 passed）

- [ ] **Step 5: 提交**

```bash
git add src/ai_banking_copilot/policy/audit.py tests/policy/conftest.py tests/policy/test_audit.py
git commit -m "feat: 建立追加式审计链（JSONL + sha256 哈希链）"
```

---

### Task 5: 异常熔断（五类触发 + 只加严不降级）

**Files:**
- Create: `src/ai_banking_copilot/policy/breaker.py`
- Modify: `tests/policy/conftest.py`（追加 `breaker` fixture）
- Test: `tests/policy/test_breaker.py`

**Interfaces:**
- Consumes: 无（纯状态机）
- Produces:
  - `DEFAULT_FAILURE_THRESHOLD: int = 3`
  - `BreakerState(str, Enum)`：`CLOSED` / `OPEN`
  - `BreakerTrigger(str, Enum)`：`CONSECUTIVE_AUTH_FAILURES` / `INJECTION_DETECTED` / `HIGH_FREQUENCY_RISK` / `POLICY_REJECTIONS` / `SESSION_ANOMALY`
  - `TRIGGERS: tuple[BreakerTrigger, ...]`（五类触发封闭集合）
  - `CircuitBreaker(threshold: int = DEFAULT_FAILURE_THRESHOLD)`：`record_failure()` / `record_success()` / `trip(trigger)` / `is_open()` / `lock()` / `unlock()` / `state()` / `triggers()` / `failure_count()`

- [ ] **Step 1: 写失败的测试**

`tests/policy/conftest.py`（修改导入段并追加 fixture）

```python
from ai_banking_copilot.policy.breaker import CircuitBreaker
```

```python
@pytest.fixture
def breaker() -> CircuitBreaker:
    """默认阈值（连续 3 次授权失败即熔断）的熔断器。"""
    return CircuitBreaker()
```

`tests/policy/test_breaker.py`

```python
import pytest

from ai_banking_copilot.policy.breaker import (
    TRIGGERS,
    BreakerState,
    BreakerTrigger,
    CircuitBreaker,
)


def test_initial_state_is_closed() -> None:
    breaker = CircuitBreaker()
    assert breaker.state() is BreakerState.CLOSED
    assert breaker.is_open() is False
    assert breaker.triggers() == ()
    assert breaker.failure_count() == 0


def test_consecutive_failures_trip_at_threshold() -> None:
    breaker = CircuitBreaker(threshold=3)
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.is_open() is False
    breaker.record_failure()
    assert breaker.is_open() is True
    assert breaker.triggers() == (BreakerTrigger.CONSECUTIVE_AUTH_FAILURES,)


def test_success_resets_counter_only_when_closed() -> None:
    breaker = CircuitBreaker(threshold=3)
    breaker.record_failure()
    breaker.record_failure()
    breaker.record_success()
    assert breaker.failure_count() == 0
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.is_open() is False
    breaker.record_failure()
    assert breaker.is_open() is True


def test_open_state_never_downgrades_on_success() -> None:
    breaker = CircuitBreaker(threshold=1)
    breaker.record_failure()
    assert breaker.is_open() is True
    breaker.record_success()
    assert breaker.is_open() is True
    assert breaker.state() is BreakerState.OPEN


def test_all_five_triggers_can_trip_the_breaker() -> None:
    assert len(TRIGGERS) == 5
    for trigger in TRIGGERS:
        breaker = CircuitBreaker()
        breaker.trip(trigger)
        assert breaker.is_open() is True
        assert trigger in breaker.triggers()


def test_unlock_is_the_only_downgrade_exit() -> None:
    breaker = CircuitBreaker(threshold=1)
    breaker.record_failure()
    assert breaker.is_open() is True
    breaker.unlock()
    assert breaker.is_open() is False
    assert breaker.failure_count() == 0


def test_manual_lock_opens_without_trigger() -> None:
    breaker = CircuitBreaker()
    breaker.lock()
    assert breaker.is_open() is True
    assert breaker.triggers() == ()


def test_trip_rejects_plain_string() -> None:
    breaker = CircuitBreaker()
    with pytest.raises(TypeError, match="BreakerTrigger"):
        breaker.trip("injection_detected")  # type: ignore[arg-type]


def test_invalid_threshold_is_rejected() -> None:
    with pytest.raises(ValueError, match="阈值"):
        CircuitBreaker(threshold=0)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/policy/test_breaker.py -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'ai_banking_copilot.policy.breaker'`

- [ ] **Step 3: 最小实现**

`src/ai_banking_copilot/policy/breaker.py`

```python
"""异常熔断：五类触发 + 只加严不降级 + 转人工接管（PRD §5.8）。

五类触发（任一命中即熔断）：
1. 连续授权失败 ≥ N（默认 3）
2. 注入检测命中
3. 短时高频高风险操作
4. 策略引擎连续拒绝达阈值
5. 会话异常设备 / 地点跳变

动作：**临时锁定高风险操作**（纯查询不受影响）→ 转 `MANUAL` 人工接管 → 写审计事件；
解锁需二次确认 + 留痕。

**熔断只加严、不降级（hard）**：打开后 `record_success()` 不得改回 `CLOSED`；
**唯一**降级出口是显式 `unlock()`（调用方负责二次确认 + 留痕）。
"""

from __future__ import annotations

from enum import Enum

DEFAULT_FAILURE_THRESHOLD = 3


class BreakerState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"


class BreakerTrigger(str, Enum):
    """PRD §5.8 的五类熔断触发。"""

    CONSECUTIVE_AUTH_FAILURES = "consecutive_auth_failures"
    INJECTION_DETECTED = "injection_detected"
    HIGH_FREQUENCY_RISK = "high_frequency_risk"
    POLICY_REJECTIONS = "policy_rejections"
    SESSION_ANOMALY = "session_anomaly"


TRIGGERS: tuple[BreakerTrigger, ...] = tuple(BreakerTrigger)
"""五类触发的封闭集合（对外可枚举、可断言）。"""


class CircuitBreaker:
    """熔断器：状态只可加严，降级必须显式解锁。"""

    def __init__(self, threshold: int = DEFAULT_FAILURE_THRESHOLD) -> None:
        if isinstance(threshold, bool) or not isinstance(threshold, int) or threshold < 1:
            raise ValueError("熔断阈值必须是 ≥ 1 的整数")
        self._threshold = threshold
        self._failures = 0
        self._state = BreakerState.CLOSED
        self._fired: list[BreakerTrigger] = []

    def record_failure(self) -> None:
        """记录一次授权失败；连续失败达阈值即按触发 1 熔断。"""
        self._failures += 1
        if self._failures >= self._threshold:
            self._trip(BreakerTrigger.CONSECUTIVE_AUTH_FAILURES)

    def record_success(self) -> None:
        """记录一次成功：**仅**在 `CLOSED` 时清空连续失败计数（只加严不降级）。"""
        if self._state is BreakerState.OPEN:
            return
        self._failures = 0

    def trip(self, trigger: BreakerTrigger) -> None:
        """按触发项 2–5（或任意登记触发）显式熔断。"""
        if not isinstance(trigger, BreakerTrigger):
            raise TypeError(
                f"trigger must be a BreakerTrigger member, got {type(trigger).__name__}"
            )
        self._trip(trigger)

    def is_open(self) -> bool:
        return self._state is BreakerState.OPEN

    def lock(self) -> None:
        """手动锁定（注入 / 高频 / 会话异常等外部判定后调用）。"""
        self._state = BreakerState.OPEN

    def unlock(self) -> None:
        """解锁：**唯一**降级出口；调用方必须完成二次确认 + 留痕。"""
        self._state = BreakerState.CLOSED
        self._failures = 0

    def state(self) -> BreakerState:
        return self._state

    def triggers(self) -> tuple[BreakerTrigger, ...]:
        return tuple(self._fired)

    def failure_count(self) -> int:
        return self._failures

    def _trip(self, trigger: BreakerTrigger) -> None:
        self._state = BreakerState.OPEN
        if trigger not in self._fired:
            self._fired.append(trigger)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/policy/test_breaker.py -v`
Expected: PASS（9 passed）

- [ ] **Step 5: 提交**

```bash
git add src/ai_banking_copilot/policy/breaker.py tests/policy/conftest.py tests/policy/test_breaker.py
git commit -m "feat: 建立异常熔断（五类触发 + 只加严不降级）"
```

---

### Task 6: 撤销窗口与 saga 补偿（竞态如实告知）

**Files:**
- Create: `src/ai_banking_copilot/policy/saga.py`
- Test: `tests/policy/test_saga.py`

**Interfaces:**
- Consumes: 无（纯编排原语；实际写由调用方注入回调）
- Produces:
  - `DEFAULT_REVERSAL_TTL: timedelta` · `Forward` / `Rollback` / `Compensate` 类型别名
  - `CompensationError(RuntimeError)`
  - `ReversalState(str, Enum)`：`REVERSED` / `COMPENSATING` / `EXPIRED` / `FAILED`
  - `ReversalOutcome`（frozen）：`state` / `message` / `compensated_steps: tuple[str, ...] = ()`
  - `ReversalWindow`（frozen）：`subject_id` / `opened_at` / `deadline`；`open(subject_id, *, now, ttl)`（classmethod）· `expired(now) -> bool` · `reverse(*, now, settled, compensate=None) -> ReversalOutcome`
  - `SagaStep`（frozen）：`name` / `forward` / `rollback`
  - `Saga()`：`add(name, forward, rollback)` · `run() -> tuple[str, ...]` · `compensate() -> tuple[str, ...]` · `completed() -> tuple[str, ...]`

- [ ] **Step 1: 写失败的测试**

`tests/policy/test_saga.py`

```python
from collections.abc import Callable
from datetime import datetime, timedelta

import pytest

from ai_banking_copilot.policy.saga import (
    CompensationError,
    ReversalState,
    ReversalWindow,
    Saga,
)

_NOW = datetime(2026, 10, 1, 9, 0, 0)


def _recorder(calls: list[str], tag: str) -> Callable[[], str]:
    def record() -> str:
        calls.append(tag)
        return tag

    return record


def test_run_executes_steps_in_order() -> None:
    calls: list[str] = []
    saga = Saga()
    saga.add("a", _recorder(calls, "a"), _recorder(calls, "-a"))
    saga.add("b", _recorder(calls, "b"), _recorder(calls, "-b"))
    assert saga.run() == ("a", "b")
    assert calls == ["a", "b"]
    assert saga.completed() == ("a", "b")


def test_run_stops_at_failure_and_compensate_rolls_back_in_reverse() -> None:
    calls: list[str] = []
    saga = Saga()
    saga.add("a", _recorder(calls, "a"), _recorder(calls, "-a"))

    def boom() -> str:
        raise RuntimeError("核心银行拒绝")

    saga.add("b", boom, _recorder(calls, "-b"))
    with pytest.raises(RuntimeError, match="拒绝"):
        saga.run()
    assert saga.completed() == ("a",)
    assert saga.compensate() == ("a",)
    assert calls == ["a", "-a"]
    assert saga.completed() == ()


def test_compensation_error_keeps_step_pending() -> None:
    calls: list[str] = []
    saga = Saga()
    saga.add("a", _recorder(calls, "a"), _recorder(calls, "-a"))

    def bad_rollback() -> str:
        raise CompensationError("退回失败")

    saga.add("b", _recorder(calls, "b"), bad_rollback)
    assert saga.run() == ("a", "b")
    with pytest.raises(CompensationError, match="退回失败"):
        saga.compensate()
    assert saga.completed() == ("a", "b")


def test_compensate_wraps_unexpected_error() -> None:
    saga = Saga()

    def broken() -> str:
        raise KeyError("boom")

    saga.add("a", _recorder([], "a"), broken)
    saga.run()
    with pytest.raises(CompensationError, match="补偿 a 失败"):
        saga.compensate()


def test_saga_rejects_duplicate_and_empty_names() -> None:
    saga = Saga()
    saga.add("a", _recorder([], "a"), _recorder([], "-a"))
    with pytest.raises(ValueError, match="重复"):
        saga.add("a", _recorder([], "a2"), _recorder([], "-a2"))
    with pytest.raises(ValueError, match="不得为空"):
        saga.add("", _recorder([], "x"), _recorder([], "-x"))


def test_window_open_sets_deadline() -> None:
    window = ReversalWindow.open("sub_video", now=_NOW, ttl=timedelta(hours=24))
    assert window.deadline == _NOW + timedelta(hours=24)
    assert window.expired(_NOW) is False
    assert window.expired(_NOW + timedelta(hours=24)) is True


def test_window_rejects_non_positive_ttl() -> None:
    with pytest.raises(ValueError, match="TTL"):
        ReversalWindow.open("sub_video", now=_NOW, ttl=timedelta(0))


def test_reverse_before_settlement_is_reversed() -> None:
    window = ReversalWindow.open("sub_video", now=_NOW)
    outcome = window.reverse(now=_NOW, settled=False)
    assert outcome.state is ReversalState.REVERSED
    assert "已取消" not in outcome.message


def test_reverse_after_settlement_compensates_without_claiming_cancelled() -> None:
    calls: list[str] = []
    saga = Saga()
    saga.add("charge", _recorder(calls, "charge"), _recorder(calls, "refund"))
    saga.run()
    window = ReversalWindow.open("sub_video", now=_NOW)
    outcome = window.reverse(now=_NOW, settled=True, compensate=saga.compensate)
    assert outcome.state is ReversalState.COMPENSATING
    assert "已扣" in outcome.message
    assert "退回" in outcome.message
    assert "已取消" not in outcome.message
    assert outcome.compensated_steps == ("charge",)


def test_reverse_after_settlement_requires_compensation() -> None:
    window = ReversalWindow.open("sub_video", now=_NOW)
    with pytest.raises(CompensationError, match="补偿"):
        window.reverse(now=_NOW, settled=True)


def test_reverse_after_expiry_is_honest() -> None:
    window = ReversalWindow.open("sub_video", now=_NOW, ttl=timedelta(minutes=30))
    outcome = window.reverse(now=_NOW + timedelta(hours=1), settled=True)
    assert outcome.state is ReversalState.EXPIRED
    assert "已取消" not in outcome.message


def test_reverse_reports_compensation_failure() -> None:
    window = ReversalWindow.open("sub_video", now=_NOW)

    def broken() -> tuple[str, ...]:
        raise CompensationError("上游不可用")

    outcome = window.reverse(now=_NOW, settled=True, compensate=broken)
    assert outcome.state is ReversalState.FAILED
    assert "未宣称已取消" in outcome.message
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/policy/test_saga.py -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'ai_banking_copilot.policy.saga'`

- [ ] **Step 3: 最小实现**

`src/ai_banking_copilot/policy/saga.py`

```python
"""撤销窗口 + 补偿事务（saga）：部分成功如实告知，竞态不得谎称「已取消」。

竞态纪律（PRD §5.5，硬约束 10）：
- 已进入结算（settled / settling）时**不得宣称「已取消」**，必须走补偿事务并如实告知
  「本期已扣、已发起退回」。
- 补偿失败必须显式暴露（`CompensationError`），不得静默吞掉。
- "部分成功" = DAG 节点级事务 + saga 补偿，对外展示「已完成 X、已回滚 Y」。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum

DEFAULT_REVERSAL_TTL = timedelta(hours=24)

Forward = Callable[[], str]
"""正向动作：返回回执标识（如 record_id）。"""

Rollback = Callable[[], str]
"""补偿动作：返回补偿结果标识。"""

Compensate = Callable[[], tuple[str, ...]]
"""批量补偿入口（通常是 `Saga.compensate`）：返回已回滚步骤名。"""


class CompensationError(RuntimeError):
    """补偿（回滚）失败：必须如实告知，且不得宣称已取消。"""


class ReversalState(str, Enum):
    REVERSED = "reversed"
    COMPENSATING = "compensating"
    EXPIRED = "expired"
    FAILED = "failed"


@dataclass(frozen=True)
class ReversalOutcome:
    state: ReversalState
    message: str
    compensated_steps: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReversalWindow:
    """撤销窗口：`deadline` 内可撤销；过期或已结算走补偿并如实告知。"""

    subject_id: str
    opened_at: datetime
    deadline: datetime

    @classmethod
    def open(
        cls, subject_id: str, *, now: datetime, ttl: timedelta = DEFAULT_REVERSAL_TTL
    ) -> ReversalWindow:
        if not subject_id:
            raise ValueError("subject_id 不得为空")
        if ttl <= timedelta(0):
            raise ValueError("撤销窗口 TTL 必须为正")
        return cls(subject_id=subject_id, opened_at=now, deadline=now + ttl)

    def expired(self, now: datetime) -> bool:
        return now >= self.deadline

    def reverse(
        self, *, now: datetime, settled: bool, compensate: Compensate | None = None
    ) -> ReversalOutcome:
        """撤销 / 补偿。`settled=True` 表示已进入结算（竞态），必须走补偿。"""
        if self.expired(now):
            return ReversalOutcome(
                state=ReversalState.EXPIRED,
                message=f"{self.subject_id} 撤销窗口已关闭，需人工处理",
            )
        if not settled:
            return ReversalOutcome(
                state=ReversalState.REVERSED,
                message=f"{self.subject_id} 已撤销（尚未结算，无扣款）",
            )
        if compensate is None:
            raise CompensationError(
                f"{self.subject_id} 已结算：必须走补偿事务并如实告知，不得谎称已取消"
            )
        try:
            steps = compensate()
        except CompensationError as error:
            return ReversalOutcome(
                state=ReversalState.FAILED,
                message=f"{self.subject_id} 补偿失败（{error}）：已如实告知，未宣称已取消",
            )
        return ReversalOutcome(
            state=ReversalState.COMPENSATING,
            message=f"{self.subject_id} 本期已扣、已发起退回（补偿事务）",
            compensated_steps=steps,
        )


@dataclass(frozen=True)
class SagaStep:
    name: str
    forward: Forward
    rollback: Rollback


class Saga:
    """顺序执行 + **逆序**补偿：对外可报告"已完成 X、已回滚 Y"。"""

    def __init__(self) -> None:
        self._steps: list[SagaStep] = []
        self._completed: list[str] = []

    def add(self, name: str, forward: Forward, rollback: Rollback) -> None:
        if not name:
            raise ValueError("步骤名不得为空")
        if any(step.name == name for step in self._steps):
            raise ValueError(f"步骤名重复：{name}")
        self._steps.append(SagaStep(name=name, forward=forward, rollback=rollback))

    def run(self) -> tuple[str, ...]:
        """顺序执行全部步骤；任一失败即停止并抛出**原异常**（已完成步骤保留待补偿）。"""
        for step in self._steps:
            if step.name in self._completed:
                continue
            step.forward()
            self._completed.append(step.name)
        return tuple(self._completed)

    def compensate(self) -> tuple[str, ...]:
        """按逆序回滚已完成步骤；任一回滚失败抛 `CompensationError`（如实暴露）。"""
        rolled_back: list[str] = []
        while self._completed:
            name = self._completed[-1]
            step = self._step_named(name)
            try:
                step.rollback()
            except CompensationError:
                raise
            except Exception as error:
                raise CompensationError(f"补偿 {name} 失败：{error}") from error
            self._completed.pop()
            rolled_back.append(name)
        return tuple(rolled_back)

    def completed(self) -> tuple[str, ...]:
        """已成功执行的步骤名（失败后用于"已完成 X、已回滚 Y"的报告）。"""
        return tuple(self._completed)

    def _step_named(self, name: str) -> SagaStep:
        for step in self._steps:
            if step.name == name:
                return step
        raise CompensationError(f"未知步骤：{name}")
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/policy/test_saga.py -v`
Expected: PASS（12 passed）

- [ ] **Step 5: 提交**

```bash
git add src/ai_banking_copilot/policy/saga.py tests/policy/test_saga.py
git commit -m "feat: 建立撤销窗口与 saga 补偿（竞态如实告知）"
```

---

### Task 7: PEP 裁决门与纯查询执行路径（审计落链 + 出处记录）

**Files:**
- Create: `src/ai_banking_copilot/policy/pep.py`
- Modify: `tests/policy/conftest.py`（追加 `registry` / `pep` fixture 与工具契约构造器）
- Test: `tests/policy/test_pep.py`

**Interfaces:**
- Consumes: Task 3 的 `assert_frozen`、Task 4 的 `AuditLog`、Task 5 的 `CircuitBreaker`、`tools.registry`（`ToolRegistry` / `ToolSpec` / `ToolCall` / `ForbiddenParamError`）、`gateway.facts.FactGateway`、`gateway.core_bank.CoreBank`、`contracts.plan.cfhash`
- Produces:
  - `PEP_ANCHOR_ID: str = "pep-execution-gate"`
  - `NotAuthorizedError(RuntimeError)`
  - `ExecResult`（frozen）：`status: str` / `record_ids: tuple[str, ...]` / `message: str`
  - `manifest_node(call: ToolCall) -> PlanNode` · `manifest_hash(call: ToolCall) -> str`
  - `Pep(registry, gateway, core_bank, audit, breaker)`：`execute(call, decision, frozen_hash, idempotency_key) -> ExecResult`

- [ ] **Step 1: 写失败的测试**

`tests/policy/conftest.py`（修改导入段并追加构造器与 fixture）

```python
from ai_banking_copilot.contracts.taint import SlotKind
from ai_banking_copilot.policy.pep import Pep
from ai_banking_copilot.tools.registry import ParamSpec, RiskClass, ToolRegistry, ToolSpec
```

```python
def _param(name: str, py_type: type[object], required: bool = True) -> ParamSpec:
    """控制槽参数（控制槽只能由确定性解析器填充，故一律 CONTROL）。"""
    return ParamSpec(name=name, slot=SlotKind.CONTROL, py_type=py_type, required=required)


def _spec(
    action: str,
    risk: RiskClass,
    params: tuple[ParamSpec, ...],
    *,
    pure: bool,
    reversible: bool = True,
    money: str | None = None,
) -> ToolSpec:
    return ToolSpec(
        action=action,
        risk=risk,
        params=params,
        is_pure_query=pure,
        reversible=reversible,
        money_param=money,
        description=f"{action}（测试用工具契约）",
    )


def _build_test_registry() -> ToolRegistry:
    """最小工具白名单：动作名**与 README 规范动作表逐项一致（全 20 个）**，覆盖纯查询 + 全部写动作
    （自建注册表，不依赖 02 的 catalog，保证 03 可独立验收）。"""
    registry = ToolRegistry()
    for spec in (
        _spec("account.balance", RiskClass.READ_ONLY, (_param("account_id", str),), pure=True),
        _spec(
            "account.transactions",
            RiskClass.READ_ONLY,
            (_param("account_id", str), _param("start", str), _param("end", str)),
            pure=True,
        ),
        _spec(
            "bills.monthly",
            RiskClass.READ_ONLY,
            (_param("user_id", str), _param("year", int), _param("month", int)),
            pure=True,
        ),
        _spec(
            "bills.category_trend",
            RiskClass.READ_ONLY,
            (
                _param("user_id", str),
                _param("category", str),
                _param("base_year", int),
                _param("base_month", int),
            ),
            pure=True,
        ),
        _spec("wealth.list", RiskClass.READ_ONLY, (), pure=True),
        _spec("card.list", RiskClass.READ_ONLY, (_param("user_id", str),), pure=True),
        _spec("subscription.list", RiskClass.READ_ONLY, (_param("user_id", str),), pure=True),
        _spec(
            "transfer.create",
            RiskClass.LARGE_FUNDS,
            (
                _param("user_id", str),
                _param("account_id", str),
                _param("payee_id", str),
                _param("amount_cents", int),
                _param("booked_on", str, required=False),
            ),
            pure=False,
            money="amount_cents",
        ),
        _spec(
            "transfer.schedule",
            RiskClass.LARGE_FUNDS,
            (
                _param("user_id", str),
                _param("account_id", str),
                _param("payee_id", str),
                _param("amount_cents", int),
                _param("booked_on", str, required=False),
            ),
            pure=False,
            money="amount_cents",
        ),
        _spec(
            "wealth.subscribe",
            RiskClass.LARGE_FUNDS,
            (
                _param("user_id", str),
                _param("account_id", str),
                _param("product_id", str),
                _param("amount_cents", int),
            ),
            pure=False,
            money="amount_cents",
        ),
        _spec(
            "wealth.redeem",
            RiskClass.LARGE_FUNDS,
            (
                _param("user_id", str),
                _param("account_id", str),
                _param("product_id", str),
                _param("amount_cents", int),
            ),
            pure=False,
            money="amount_cents",
        ),
        _spec(
            "subscription.cancel",
            RiskClass.SMALL_CHANGE,
            (_param("subscription_id", str),),
            pure=False,
        ),
        _spec(
            "subscription.sign",
            RiskClass.SMALL_CHANGE,
            (
                _param("user_id", str),
                _param("merchant", str),
                _param("amount_cents", int),
                _param("period", str),
                _param("next_charge_on", str),
            ),
            pure=False,
            money="amount_cents",
        ),
        _spec("card.lock", RiskClass.SMALL_CHANGE, (_param("card_id", str),), pure=False),
        _spec("card.unlock", RiskClass.SMALL_CHANGE, (_param("card_id", str),), pure=False),
        _spec(
            "card.report_lost",
            RiskClass.IRREVERSIBLE,
            (_param("card_id", str), _param("reason", str, required=False)),
            pure=False,
            reversible=False,
        ),
        _spec(
            "card.apply_virtual",
            RiskClass.SMALL_CHANGE,
            (_param("user_id", str), _param("daily_limit_cents", int)),
            pure=False,
            money="daily_limit_cents",
        ),
        _spec(
            "card.set_limit",
            RiskClass.SMALL_CHANGE,
            (_param("card_id", str), _param("daily_limit_cents", int)),
            pure=False,
            money="daily_limit_cents",
        ),
        _spec(
            "funds.freeze",
            RiskClass.SMALL_CHANGE,
            (_param("account_id", str), _param("amount_cents", int), _param("reason", str)),
            pure=False,
            money="amount_cents",
        ),
        _spec("funds.release", RiskClass.SMALL_CHANGE, (_param("hold_id", str),), pure=False),
    ):
        registry.register(spec)
    return registry


@pytest.fixture
def registry() -> ToolRegistry:
    return _build_test_registry()


@pytest.fixture
def pep(
    registry: ToolRegistry,
    gateway: FactGateway,
    core_bank: CoreBank,
    audit: AuditLog,
    breaker: CircuitBreaker,
) -> Pep:
    return Pep(registry, gateway, core_bank, audit, breaker)
```

`tests/policy/test_pep.py`

```python
import pytest

from ai_banking_copilot.contracts.authorization import Decision, Level, ReasonCode
from ai_banking_copilot.contracts.taint import Trust, Value
from ai_banking_copilot.gateway.core_bank import CoreBank
from ai_banking_copilot.gateway.errors import BlockedPayeeError, InsufficientFundsError
from ai_banking_copilot.gateway.facts import FactGateway
from ai_banking_copilot.gateway.seed import DEMO_USER_ID
from ai_banking_copilot.gateway.store import Store
from ai_banking_copilot.policy.audit import KIND_EXECUTION, AuditLog
from ai_banking_copilot.policy.breaker import CircuitBreaker
from ai_banking_copilot.policy.gate import StalePlanError
from ai_banking_copilot.policy.pep import NotAuthorizedError, Pep, manifest_hash
from ai_banking_copilot.tools.registry import (
    ForbiddenParamError,
    ToolCall,
    UnknownActionError,
)


def _decision(level: Level) -> Decision:
    reasons = (
        (ReasonCode.QUERY_ONLY,)
        if level is Level.GREEN
        else (ReasonCode.STATE_CHANGE_SMALL,)
    )
    return Decision(level=level, reasons=reasons)


def _trusted(value: object) -> Value[object]:
    """控制槽参数必须携带可信来源标记（02 的 `validate_params` 强制，H3）。"""
    return Value(value, Trust.TRUSTED)


def _query_params(account_id: str = "acc_savings") -> dict[str, object]:
    return {"account_id": _trusted(account_id)}


def _transfer_params(amount_cents: int = 50000) -> dict[str, object]:
    return {
        "user_id": _trusted(DEMO_USER_ID),
        "account_id": _trusted("acc_savings"),
        "payee_id": _trusted("p_lina"),
        "amount_cents": _trusted(amount_cents),
        "booked_on": _trusted("2026-10-01"),
    }


def test_pure_query_executes_and_is_audited(pep: Pep, audit: AuditLog) -> None:
    call = ToolCall(action="account.balance", params=_query_params())
    result = pep.execute(call, _decision(Level.GREEN), manifest_hash(call), "q1")
    assert result.status == "queried"
    assert result.record_ids == ("accounts:acc_savings",)
    assert "128430.00" in result.message
    assert "审计ID" in result.message
    assert audit.verify_chain() is True
    events = audit.events()
    assert len(events) == 1
    assert events[0].kind == KIND_EXECUTION
    assert events[0].payload["level"] == "GREEN"


def test_query_records_provenance_hash(pep: Pep, audit: AuditLog, gateway: FactGateway) -> None:
    call = ToolCall(action="account.balance", params=_query_params())
    pep.execute(call, _decision(Level.GREEN), manifest_hash(call), "q1")
    provenance = audit.events()[0].payload["provenance"]
    expected = gateway.account("acc_savings").content_hash
    assert provenance == [{"record_id": "accounts:acc_savings", "content_hash": expected}]


def test_execute_without_decision_is_rejected(pep: Pep) -> None:
    call = ToolCall(action="account.balance", params=_query_params())
    with pytest.raises(NotAuthorizedError, match="无裁决"):
        pep.execute(call, None, manifest_hash(call), "k1")  # type: ignore[arg-type]


def test_manual_and_deny_levels_are_rejected(pep: Pep) -> None:
    call = ToolCall(action="transfer.create", params=_transfer_params())
    for level in (Level.MANUAL, Level.DENY):
        with pytest.raises(NotAuthorizedError, match="级别不足"):
            pep.execute(call, _decision(level), manifest_hash(call), f"k-{level.name}")


def test_green_cannot_execute_state_change(pep: Pep) -> None:
    call = ToolCall(action="transfer.create", params=_transfer_params())
    with pytest.raises(NotAuthorizedError, match="纯查询"):
        pep.execute(call, _decision(Level.GREEN), manifest_hash(call), "k1")


def test_unknown_action_is_rejected(pep: Pep) -> None:
    call = ToolCall(action="payee.delete", params={})
    with pytest.raises(UnknownActionError):
        pep.execute(call, _decision(Level.RED), manifest_hash(call), "k1")


def test_stale_frozen_hash_is_rejected(pep: Pep) -> None:
    call = ToolCall(action="transfer.create", params=_transfer_params())
    with pytest.raises(StalePlanError, match="哈希不匹配"):
        pep.execute(call, _decision(Level.RED), "0" * 64, "k1")


def test_untrusted_control_slot_value_is_rejected(pep: Pep) -> None:
    call = ToolCall(
        action="account.balance",
        params={"account_id": Value("acc_savings", Trust.UNVERIFIED)},
    )
    with pytest.raises(ForbiddenParamError, match="unverified"):
        pep.execute(call, _decision(Level.GREEN), manifest_hash(call), "k1")


def test_open_breaker_blocks_writes_but_not_queries(pep: Pep, breaker: CircuitBreaker) -> None:
    breaker.lock()
    query = ToolCall(action="account.balance", params=_query_params())
    result = pep.execute(query, _decision(Level.GREEN), manifest_hash(query), "q1")
    assert result.status == "queried"
    write = ToolCall(action="transfer.create", params=_transfer_params())
    with pytest.raises(NotAuthorizedError, match="熔断"):
        pep.execute(write, _decision(Level.RED), manifest_hash(write), "k1")


def test_repeated_rejections_open_the_breaker(pep: Pep, breaker: CircuitBreaker) -> None:
    call = ToolCall(action="transfer.create", params=_transfer_params())
    for index in range(3):
        with pytest.raises(NotAuthorizedError):
            pep.execute(call, _decision(Level.DENY), manifest_hash(call), f"k{index}")
    assert breaker.is_open() is True


def test_account_transactions_query_returns_canonical_records(pep: Pep) -> None:
    call = ToolCall(
        action="account.transactions",
        params={
            "account_id": _trusted("acc_current"),
            "start": _trusted("2026-09-01"),
            "end": _trusted("2026-09-10"),
        },
    )
    result = pep.execute(call, _decision(Level.GREEN), manifest_hash(call), "q2")
    assert result.status == "queried"
    assert result.record_ids == (
        "transactions:txn_20260902",
        "transactions:txn_20260905",
        "transactions:txn_20260908",
    )


def test_bills_category_trend_query_reports_base_period(pep: Pep) -> None:
    call = ToolCall(
        action="bills.category_trend",
        params={
            "user_id": _trusted(DEMO_USER_ID),
            "category": _trusted("购物"),
            "base_year": _trusted(2026),
            "base_month": _trusted(9),
        },
    )
    result = pep.execute(call, _decision(Level.GREEN), manifest_hash(call), "q3")
    assert result.status == "queried"
    assert "购物" in result.message
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/policy/test_pep.py -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'ai_banking_copilot.policy.pep'`

- [ ] **Step 3: 最小实现**

`src/ai_banking_copilot/policy/pep.py`

```python
"""策略执行点（PEP）：裁决门 + 参数强校验 + taint 校验 + 执行门 + 审计落链。

硬约束（H2 / H3 / PRD §5.1）：
- **PEP 无裁决不执行**：没有 `Decision` 或级别 ≥ `MANUAL`（L3/L4）一律拒绝。
- **绿色只允许纯查询**：`GREEN` + 非纯查询工具 = 拒绝（防资金 / 状态变更溜进绿色）。
- **参数入口唯一**：`ToolRegistry.validate_params(call)` 是唯一参数门，返回**去 taint 包装后的归一值**
  （`dict[str, object]`）；PEP 只消费该返回值、不直接读 `call.params`。控制槽裸值一律被
  `ForbiddenParamError` 拒绝（H3）。
- **执行门（双层叠加）**：本模块用 `manifest_hash(call)` 做**节点级**成员资格校验（`cfhash` 不匹配即拒）；
  **计划级**冻结校验由 `policy.gate.assert_frozen(plan, cfhash(plan.nodes))` 把关（06 在确认记录里
  同时保存 `Plan` 与 `cfhash`）。确认后控制槽参数被改 ⇒ 节点哈希不匹配 ⇒ 拒绝（防 TOCTOU）。
- **熔断只加严**：熔断打开时锁定非纯查询操作，**纯查询不受影响**。
- 本模块是**写操作的唯一通道**；`policy/` 之外不得直接调用 `CoreBank`。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date

from ai_banking_copilot.contracts.authorization import Decision, Level
from ai_banking_copilot.contracts.plan import (
    AnchorSource,
    IntentRef,
    Plan,
    PlanNode,
    cfhash,
)
from ai_banking_copilot.contracts.taint import SlotKind, Trust, Value
from ai_banking_copilot.gateway.core_bank import DEFAULT_BOOKING_DATE, CoreBank, WriteReceipt
from ai_banking_copilot.gateway.errors import GatewayError
from ai_banking_copilot.gateway.facts import FactGateway
from ai_banking_copilot.gateway.hashing import canonical_json
from ai_banking_copilot.gateway.models import Money
from ai_banking_copilot.policy.audit import KIND_EXECUTION, AuditEvent, AuditLog
from ai_banking_copilot.policy.breaker import CircuitBreaker
from ai_banking_copilot.policy.gate import assert_frozen
from ai_banking_copilot.tools.registry import ForbiddenParamError, ToolCall, ToolRegistry, ToolSpec

PEP_ANCHOR_ID = "pep-execution-gate"
"""执行门节点的规范锚点 id：让"确认时冻结"与"执行时复核"算出同一哈希。"""


class NotAuthorizedError(RuntimeError):
    """无裁决、级别不足、未登记处理器、熔断锁定——一律拒绝执行。"""


@dataclass(frozen=True)
class ExecResult:
    """一次执行的结果（`message` 内含审计 ID，对齐 §9.1 的可机读 schema）。"""

    status: str
    record_ids: tuple[str, ...]
    message: str


def manifest_node(call: ToolCall) -> PlanNode:
    """把工具调用规范化成执行门节点（控制槽参数进哈希）。"""
    return PlanNode(
        node_id="pep",
        action=call.action,
        params=_canonical_params(call.params),
        intent=IntentRef(source=AnchorSource.USER_APPROVED, anchor_id=PEP_ANCHOR_ID),
    )


def manifest_hash(call: ToolCall) -> str:
    """节点级执行门哈希：确认时冻结、执行时复核（防 TOCTOU）。"""
    return cfhash((manifest_node(call),))


class Pep:
    """工具白名单执行器：裁决 → 校验 → 执行门 → 执行 → 出处 → 审计。"""

    def __init__(
        self,
        registry: ToolRegistry,
        gateway: FactGateway,
        core_bank: CoreBank,
        audit: AuditLog,
        breaker: CircuitBreaker,
    ) -> None:
        self._registry = registry
        self._gateway = gateway
        self._core_bank = core_bank
        self._audit = audit
        self._breaker = breaker

    def execute(
        self,
        call: ToolCall,
        decision: Decision,
        frozen_hash: str,
        idempotency_key: str,
    ) -> ExecResult:
        """执行一次已裁决的工具调用。任一前置条件不满足即 fail-closed。"""
        if not isinstance(decision, Decision):
            raise NotAuthorizedError("PEP 无裁决不执行：缺少 Decision")
        if decision.level >= Level.MANUAL:
            self._breaker.record_failure()
            raise NotAuthorizedError(f"裁决级别不足，不允许执行：{decision.level.name}")
        spec = self._registry.get(call.action)
        if decision.level is Level.GREEN and not spec.is_pure_query:
            self._breaker.record_failure()
            raise NotAuthorizedError("绿色只允许纯查询：拒绝执行资金 / 状态变更")
        if self._breaker.is_open() and not spec.is_pure_query:
            raise NotAuthorizedError("熔断打开：高风险操作被临时锁定（纯查询不受影响）")
        self._assert_taint(spec, call)
        resolved = self._registry.validate_params(call)
        assert_frozen(Plan(plan_id="pep-execution", nodes=(manifest_node(call),)), frozen_hash)
        if spec.is_pure_query:
            return self._execute_query(call, resolved, decision, frozen_hash, idempotency_key)
        raise NotAuthorizedError(f"写路径尚未登记处理器：{call.action}")

    def _execute_query(
        self,
        call: ToolCall,
        params: dict[str, object],
        decision: Decision,
        frozen_hash: str,
        idempotency_key: str,
    ) -> ExecResult:
        if call.action == "account.balance":
            record = self._gateway.account(_require_str(params, "account_id"))
            record_ids = (record.record_id,)
            provenance = ((record.record_id, record.content_hash),)
            detail = f"账户 {record.payload.account_id} 余额 {record.payload.balance.yuan} 元"
        elif call.action == "bills.monthly":
            year = _require_int(params, "year")
            month = _require_int(params, "month")
            summary = self._gateway.monthly_bill(_require_str(params, "user_id"), year, month)
            record_ids = (summary.record_id,)
            provenance = ((summary.record_id, summary.content_hash),)
            detail = (
                f"{year}-{month:02d} 支出 {summary.payload.total_expense.yuan} 元"
                f" / 收入 {summary.payload.total_income.yuan} 元"
            )
        elif call.action == "card.list":
            cards = self._gateway.cards_of(_require_str(params, "user_id"))
            record_ids = tuple(item.record_id for item in cards)
            provenance = tuple((item.record_id, item.content_hash) for item in cards)
            detail = f"共 {len(cards)} 张卡"
        elif call.action == "subscription.list":
            subscriptions = self._gateway.subscriptions_of(_require_str(params, "user_id"))
            record_ids = tuple(item.record_id for item in subscriptions)
            provenance = tuple((item.record_id, item.content_hash) for item in subscriptions)
            detail = f"共 {len(subscriptions)} 项订阅"
        elif call.action == "wealth.list":
            products = self._gateway.wealth_products()
            record_ids = tuple(item.record_id for item in products)
            provenance = tuple((item.record_id, item.content_hash) for item in products)
            detail = f"共 {len(products)} 只理财产品"
        elif call.action == "account.transactions":
            account_id = _require_str(params, "account_id")
            rows = self._gateway.transactions_between(
                account_id, _require_date(params, "start"), _require_date(params, "end")
            )
            record_ids = tuple(item.record_id for item in rows)
            provenance = tuple((item.record_id, item.content_hash) for item in rows)
            detail = f"账户 {account_id} 区间交易 {len(rows)} 笔"
        elif call.action == "bills.category_trend":
            summary = self._gateway.monthly_bill(
                _require_str(params, "user_id"),
                _require_int(params, "base_year"),
                _require_int(params, "base_month"),
            )
            category = _require_str(params, "category")
            record_ids = (summary.record_id,)
            provenance = ((summary.record_id, summary.content_hash),)
            total = Money(0)
            for item in summary.payload.by_category:
                if item.category == category:
                    total = item.total
            detail = f"分类 {category} 基期合计 {total.yuan} 元"
        else:
            self._breaker.record_failure()
            raise NotAuthorizedError(f"未登记查询处理器：{call.action}")
        return self._finish(
            call,
            decision,
            frozen_hash,
            idempotency_key,
            status="queried",
            record_ids=record_ids,
            provenance=provenance,
            detail=detail,
        )

    def _assert_taint(self, spec: ToolSpec, call: ToolCall) -> None:
        """控制槽拒绝 `unverified` 值：PEP 侧再兜一道 schema 防线（H3）。"""
        for param in spec.params:
            if param.slot is not SlotKind.CONTROL:
                continue
            value = call.params.get(param.name)
            if isinstance(value, Value) and value.trust is Trust.UNVERIFIED:
                self._breaker.record_failure()
                raise ForbiddenParamError(f"控制槽 {param.name} 拒绝 unverified 值")

    def _finish(
        self,
        call: ToolCall,
        decision: Decision,
        frozen_hash: str,
        idempotency_key: str,
        *,
        status: str,
        record_ids: tuple[str, ...],
        provenance: tuple[tuple[str, str], ...],
        detail: str,
    ) -> ExecResult:
        event_hash = self._audit.append(
            AuditEvent(
                event_id=f"{call.action}:{idempotency_key}",
                kind=KIND_EXECUTION,
                payload={
                    "action": call.action,
                    "level": decision.level.name,
                    "reasons": [reason.value for reason in decision.reasons],
                    "frozen_hash": frozen_hash,
                    "idempotency_key": idempotency_key,
                    "status": status,
                    "record_ids": list(record_ids),
                    "provenance": [
                        {"record_id": record_id, "content_hash": content_hash}
                        for record_id, content_hash in provenance
                    ],
                    "detail": detail,
                },
                prev_hash="",
            )
        )
        return ExecResult(
            status=status,
            record_ids=record_ids,
            message=f"{detail}｜审计ID {event_hash[:16]}",
        )


def _canonical_params(params: dict[str, object]) -> dict[str, object]:
    """把参数规范成 JSON 安全形态：保证 `cfhash` 可确定性序列化。"""
    parsed = json.loads(canonical_json(params))
    if not isinstance(parsed, dict):
        raise NotAuthorizedError("工具参数必须是 JSON 对象")
    return {str(key): value for key, value in parsed.items()}


def _require_str(params: dict[str, object], key: str) -> str:
    value = params.get(key)
    if not isinstance(value, str) or not value:
        raise NotAuthorizedError(f"参数 {key} 必须是非空字符串")
    return value


def _require_int(params: dict[str, object], key: str) -> int:
    value = params.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise NotAuthorizedError(f"参数 {key} 必须是整数")
    return value


def _require_date(params: dict[str, object], key: str) -> date:
    value = params.get(key)
    if value is None:
        return DEFAULT_BOOKING_DATE
    if not isinstance(value, str):
        raise NotAuthorizedError(f"参数 {key} 必须是 ISO 日期字符串")
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise NotAuthorizedError(f"参数 {key} 不是合法日期：{value}") from error
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/policy -v`
Expected: PASS（全部通过；本 Task 新增 12 项）

- [ ] **Step 5: 提交**

```bash
git add src/ai_banking_copilot/policy/pep.py tests/policy/conftest.py tests/policy/test_pep.py
git commit -m "feat: 建立 PEP 裁决门与纯查询执行路径（审计落链 + 出处记录）"
```

---

### Task 8: PEP 写执行路径（幂等 + 执行门 + 执行后出处校验）

**Files:**
- Modify: `src/ai_banking_copilot/policy/pep.py`（追加写路径与出处校验）
- Test: `tests/policy/test_pep.py`（追加写用例）

**Interfaces:**
- Consumes: Task 7 的 `Pep`；`gateway.core_bank`（`transfer` / `cancel_subscription` / `sign_subscription` / `freeze_funds` / `unfreeze_funds` / `subscribe_product` / `redeem_product` / `set_card_status` / `apply_virtual_card` / `set_card_limit`）；`FactGateway.verify`
- Produces（`policy.pep` 追加）：
  - `HANDLED_ACTIONS: frozenset[str]`（PEP 可执行的规范动作全集 = README 规范动作表 20 项）
  - `Pep._execute_write(call, params, spec, decision, frozen_hash, idempotency_key) -> ExecResult`
  - `Pep._dispatch_write(call, params, spec, idempotency_key) -> WriteReceipt`（**覆盖全部写动作**）
  - `Pep._verify_receipt(receipt) -> tuple[tuple[str, str], ...]`
  - 模块级 `_money_cents(params, spec) -> int` · `_read_hash(gateway, record_id) -> str`
  - 金额：PEP 用 `spec.money_param` 取 `amount_cents` / `daily_limit_cents`（分数）**包装成 `Money`** 再调 `CoreBank`
  - 幂等语义：同一 `idempotency_key` 重放 ⇒ 同一回执、`message` 含"幂等回放"
  - `params` 一律来自 `ToolRegistry.validate_params(call)` 的**归一返回值**（非 `call.params`）

- [ ] **Step 1: 写失败的测试**

`tests/policy/test_pep.py`（在文件末尾追加）

```python
def test_transfer_settles_and_debits_balance(pep: Pep, rw_store: Store) -> None:
    call = ToolCall(action="transfer.create", params=_transfer_params())
    result = pep.execute(call, _decision(Level.RED), manifest_hash(call), "k1")
    assert result.status == "settled"
    assert result.record_ids[0].startswith("transactions:")
    rows = rw_store.query("SELECT balance_cents FROM accounts WHERE account_id = 'acc_savings'")
    assert rows[0]["balance_cents"] == 12843000 - 50000


def test_write_records_verified_provenance(
    pep: Pep, audit: AuditLog, gateway: FactGateway
) -> None:
    call = ToolCall(action="transfer.create", params=_transfer_params())
    result = pep.execute(call, _decision(Level.RED), manifest_hash(call), "k1")
    provenance = audit.events()[0].payload["provenance"]
    assert len(provenance) == 1
    entry = provenance[0]
    assert entry["record_id"] == result.record_ids[0]
    assert gateway.verify(entry["record_id"], entry["content_hash"]) is True


def test_idempotent_replay_returns_same_receipt(pep: Pep, rw_store: Store) -> None:
    call = ToolCall(action="transfer.create", params=_transfer_params())
    frozen = manifest_hash(call)
    first = pep.execute(call, _decision(Level.RED), frozen, "k1")
    second = pep.execute(call, _decision(Level.RED), frozen, "k1")
    assert second.record_ids == first.record_ids
    assert "回放" in second.message
    rows = rw_store.query(
        "SELECT COUNT(*) AS n FROM transactions WHERE category = '转账' AND channel = 'agent'"
    )
    assert rows[0]["n"] == 1


def test_tampered_amount_fails_execution_gate(pep: Pep) -> None:
    approved = ToolCall(action="transfer.create", params=_transfer_params(amount_cents=50000))
    tampered = ToolCall(action="transfer.create", params=_transfer_params(amount_cents=5000000))
    with pytest.raises(StalePlanError):
        pep.execute(tampered, _decision(Level.RED), manifest_hash(approved), "k1")


def test_subscription_cancel_updates_status(pep: Pep, gateway: FactGateway) -> None:
    call = ToolCall(
        action="subscription.cancel", params={"subscription_id": _trusted("sub_video")}
    )
    result = pep.execute(call, _decision(Level.YELLOW), manifest_hash(call), "k1")
    assert result.status == "cancelled"
    assert gateway.subscription("sub_video").payload.status == "cancelled"


def test_freeze_then_unfreeze(pep: Pep, gateway: FactGateway) -> None:
    freeze = ToolCall(
        action="funds.freeze",
        params={
            "account_id": _trusted("acc_savings"),
            "amount_cents": _trusted(100000),
            "reason": _trusted("生日预算"),
        },
    )
    held = pep.execute(freeze, _decision(Level.RED), manifest_hash(freeze), "k1")
    hold_id = held.record_ids[0].split(":", 1)[1]
    assert gateway.hold(hold_id).payload.status == "active"
    unfreeze = ToolCall(action="funds.release", params={"hold_id": _trusted(hold_id)})
    released = pep.execute(unfreeze, _decision(Level.YELLOW), manifest_hash(unfreeze), "k2")
    assert released.status == "released"
    assert gateway.hold(hold_id).payload.status == "released"


def test_wealth_subscribe_requires_min_amount(pep: Pep) -> None:
    call = ToolCall(
        action="wealth.subscribe",
        params={
            "user_id": _trusted(DEMO_USER_ID),
            "account_id": _trusted("acc_savings"),
            "product_id": _trusted("wp_bond"),
            "amount_cents": _trusted(1000),
        },
    )
    with pytest.raises(GatewayError):
        pep.execute(call, _decision(Level.RED), manifest_hash(call), "k1")


def test_write_failure_records_audit_and_breaker_failure(
    pep: Pep, audit: AuditLog, breaker: CircuitBreaker
) -> None:
    call = ToolCall(action="transfer.create", params=_transfer_params(amount_cents=99999999))
    with pytest.raises(InsufficientFundsError):
        pep.execute(call, _decision(Level.RED), manifest_hash(call), "k1")
    assert breaker.failure_count() == 1
    assert audit.events()[-1].payload["status"] == "failed"
    assert audit.verify_chain() is True


def test_blocked_payee_failure_is_recorded(pep: Pep, breaker: CircuitBreaker) -> None:
    params = _transfer_params()
    params["payee_id"] = _trusted("p_zhaoliu")
    call = ToolCall(action="transfer.create", params=params)
    with pytest.raises(BlockedPayeeError):
        pep.execute(call, _decision(Level.RED), manifest_hash(call), "k1")
    assert breaker.failure_count() == 1


CANONICAL_ACTIONS = {
    "account.balance",
    "account.transactions",
    "bills.monthly",
    "bills.category_trend",
    "wealth.list",
    "card.list",
    "subscription.list",
    "transfer.create",
    "transfer.schedule",
    "wealth.subscribe",
    "wealth.redeem",
    "subscription.cancel",
    "subscription.sign",
    "card.lock",
    "card.unlock",
    "card.report_lost",
    "card.apply_virtual",
    "card.set_limit",
    "funds.freeze",
    "funds.release",
}


def test_pep_handles_every_canonical_action() -> None:
    """PEP 分派覆盖 README 规范动作表全部 20 个动作（README 裁决 #14）。"""
    assert len(CANONICAL_ACTIONS) == 20
    assert HANDLED_ACTIONS == CANONICAL_ACTIONS


def test_transfer_schedule_reuses_transfer_semantics(pep: Pep, rw_store: Store) -> None:
    call = ToolCall(action="transfer.schedule", params=_transfer_params())
    result = pep.execute(call, _decision(Level.RED), manifest_hash(call), "k2")
    assert result.status == "settled"
    rows = rw_store.query("SELECT COUNT(*) AS n FROM transactions WHERE booked_on = '2026-10-01'")
    assert rows[0]["n"] == 1


def test_card_lock_and_unlock_update_status(pep: Pep, gateway: FactGateway) -> None:
    lock = ToolCall(action="card.lock", params={"card_id": _trusted("card_debit")})
    locked = pep.execute(lock, _decision(Level.YELLOW), manifest_hash(lock), "k3")
    assert locked.status == "locked"
    assert gateway.card("card_debit").payload.status == "locked"
    unlock = ToolCall(action="card.unlock", params={"card_id": _trusted("card_debit")})
    unlocked = pep.execute(unlock, _decision(Level.YELLOW), manifest_hash(unlock), "k4")
    assert unlocked.status == "active"
    assert gateway.card("card_debit").payload.status == "active"


def test_card_apply_virtual_creates_virtual_card(pep: Pep, gateway: FactGateway) -> None:
    call = ToolCall(
        action="card.apply_virtual",
        params={"user_id": _trusted(DEMO_USER_ID), "daily_limit_cents": _trusted(300000)},
    )
    result = pep.execute(call, _decision(Level.YELLOW), manifest_hash(call), "k5")
    assert result.status == "applied"
    card_id = result.record_ids[0].split(":", 1)[1]
    assert gateway.card(card_id).payload.card_type == "virtual"
    assert gateway.card(card_id).payload.daily_limit == Money(300000)


def test_card_set_limit_updates_limit(pep: Pep, gateway: FactGateway) -> None:
    call = ToolCall(
        action="card.set_limit",
        params={"card_id": _trusted("card_debit"), "daily_limit_cents": _trusted(1500000)},
    )
    result = pep.execute(call, _decision(Level.YELLOW), manifest_hash(call), "k6")
    assert result.status == "updated"
    assert gateway.card("card_debit").payload.daily_limit == Money(1500000)


def test_subscription_sign_creates_subscription(pep: Pep, gateway: FactGateway) -> None:
    call = ToolCall(
        action="subscription.sign",
        params={
            "user_id": _trusted(DEMO_USER_ID),
            "merchant": _trusted("云盘会员"),
            "amount_cents": _trusted(19800),
            "period": _trusted("yearly"),
            "next_charge_on": _trusted("2027-03-01"),
        },
    )
    result = pep.execute(call, _decision(Level.YELLOW), manifest_hash(call), "k7")
    assert result.status == "signed"
    subscription_id = result.record_ids[0].split(":", 1)[1]
    assert gateway.subscription(subscription_id).payload.merchant == "云盘会员"
```

同时把 `tests/policy/test_pep.py` 的导入段补齐：`gateway.errors` 增加 `GatewayError`，
新增 `gateway.models.Money`，并把 `policy.pep` 的导入行追加 `HANDLED_ACTIONS`：

```python
from ai_banking_copilot.gateway.errors import (
    BlockedPayeeError,
    GatewayError,
    InsufficientFundsError,
)
from ai_banking_copilot.gateway.models import Money
from ai_banking_copilot.policy.pep import HANDLED_ACTIONS, NotAuthorizedError, Pep, manifest_hash
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/policy/test_pep.py -v`
Expected: FAIL —— 写分派与常量尚未落地（`ImportError: cannot import name 'HANDLED_ACTIONS'` / `NotAuthorizedError: 未登记执行处理器`）

- [ ] **Step 3: 最小实现**

`src/ai_banking_copilot/policy/pep.py`（把 Task 7 的兜底分支替换为写路径分派）

```python
        if spec.is_pure_query:
            return self._execute_query(call, resolved, decision, frozen_hash, idempotency_key)
        return self._execute_write(call, resolved, spec, decision, frozen_hash, idempotency_key)
```

`src/ai_banking_copilot/policy/pep.py`（在 `Pep._execute_query` 之后追加方法）

```python
    def _execute_write(
        self,
        call: ToolCall,
        params: dict[str, object],
        spec: ToolSpec,
        decision: Decision,
        frozen_hash: str,
        idempotency_key: str,
    ) -> ExecResult:
        """写路径：幂等执行 → 执行后出处校验 → 熔断联动 → 审计落链。"""
        try:
            receipt = self._dispatch_write(call, params, spec, idempotency_key)
        except NotAuthorizedError:
            self._breaker.record_failure()
            raise
        except GatewayError as error:
            self._breaker.record_failure()
            self._finish(
                call,
                decision,
                frozen_hash,
                idempotency_key,
                status="failed",
                record_ids=(),
                provenance=(),
                detail=str(error),
            )
            raise
        provenance = self._verify_receipt(receipt)
        self._breaker.record_success()
        suffix = "（幂等回放）" if receipt.replayed else ""
        return self._finish(
            call,
            decision,
            frozen_hash,
            idempotency_key,
            status=receipt.status,
            record_ids=receipt.record_ids,
            provenance=provenance,
            detail=f"{receipt.action}{suffix}",
        )

    def _dispatch_write(
        self,
        call: ToolCall,
        params: dict[str, object],
        spec: ToolSpec,
        idempotency_key: str,
    ) -> WriteReceipt:
        """把白名单动作映射到 `CoreBank` 写原语（**覆盖规范动作表全部写动作**）。"""
        action = call.action
        if action in ("transfer.create", "transfer.schedule"):
            return self._core_bank.transfer(
                user_id=_require_str(params, "user_id"),
                account_id=_require_str(params, "account_id"),
                payee_id=_require_str(params, "payee_id"),
                amount=Money(_money_cents(params, spec)),
                idempotency_key=idempotency_key,
                booked_on=_require_date(params, "booked_on"),
            )
        if action == "wealth.subscribe":
            return self._core_bank.subscribe_product(
                user_id=_require_str(params, "user_id"),
                account_id=_require_str(params, "account_id"),
                product_id=_require_str(params, "product_id"),
                amount=Money(_money_cents(params, spec)),
                idempotency_key=idempotency_key,
            )
        if action == "wealth.redeem":
            return self._core_bank.redeem_product(
                user_id=_require_str(params, "user_id"),
                account_id=_require_str(params, "account_id"),
                product_id=_require_str(params, "product_id"),
                amount=Money(_money_cents(params, spec)),
                idempotency_key=idempotency_key,
            )
        if action == "subscription.cancel":
            return self._core_bank.cancel_subscription(
                subscription_id=_require_str(params, "subscription_id"),
                idempotency_key=idempotency_key,
            )
        if action == "subscription.sign":
            return self._core_bank.sign_subscription(
                user_id=_require_str(params, "user_id"),
                merchant=_require_str(params, "merchant"),
                amount=Money(_money_cents(params, spec)),
                period=_require_str(params, "period"),
                next_charge_on=_require_date(params, "next_charge_on"),
                idempotency_key=idempotency_key,
            )
        if action == "card.lock":
            return self._core_bank.set_card_status(
                card_id=_require_str(params, "card_id"),
                status="locked",
                idempotency_key=idempotency_key,
            )
        if action == "card.unlock":
            return self._core_bank.set_card_status(
                card_id=_require_str(params, "card_id"),
                status="active",
                idempotency_key=idempotency_key,
            )
        if action == "card.report_lost":
            return self._core_bank.set_card_status(
                card_id=_require_str(params, "card_id"),
                status="lost",
                idempotency_key=idempotency_key,
            )
        if action == "card.apply_virtual":
            return self._core_bank.apply_virtual_card(
                user_id=_require_str(params, "user_id"),
                daily_limit=Money(_money_cents(params, spec)),
                idempotency_key=idempotency_key,
            )
        if action == "card.set_limit":
            return self._core_bank.set_card_limit(
                card_id=_require_str(params, "card_id"),
                daily_limit=Money(_money_cents(params, spec)),
                idempotency_key=idempotency_key,
            )
        if action == "funds.freeze":
            return self._core_bank.freeze_funds(
                account_id=_require_str(params, "account_id"),
                amount=Money(_money_cents(params, spec)),
                reason=_require_str(params, "reason"),
                idempotency_key=idempotency_key,
            )
        if action == "funds.release":
            return self._core_bank.unfreeze_funds(
                hold_id=_require_str(params, "hold_id"),
                idempotency_key=idempotency_key,
            )
        raise NotAuthorizedError(f"未登记执行处理器：{action}")

    def _verify_receipt(self, receipt: WriteReceipt) -> tuple[tuple[str, str], ...]:
        """执行后出处校验：回读回执记录并用 `FactGateway.verify` 比对哈希（H1）。"""
        results: list[tuple[str, str]] = []
        for record_id in receipt.record_ids:
            content_hash = _read_hash(self._gateway, record_id)
            if not self._gateway.verify(record_id, content_hash):
                raise GatewayError(f"执行后出处校验失败：{record_id}")
            results.append((record_id, content_hash))
        return tuple(results)
```

`src/ai_banking_copilot/policy/pep.py`（在模块末尾追加）

```python
def _read_hash(gateway: FactGateway, record_id: str) -> str:
    """按 `record_id` 回读事实并取内容哈希（只用 01 的公开读取面）。"""
    table, _, primary_key = record_id.partition(":")
    if not primary_key:
        raise GatewayError(f"记录标识不合法：{record_id}")
    if table == "accounts":
        return gateway.account(primary_key).content_hash
    if table == "cards":
        return gateway.card(primary_key).content_hash
    if table == "holds":
        return gateway.hold(primary_key).content_hash
    if table == "payees":
        return gateway.payee(primary_key).content_hash
    if table == "subscriptions":
        return gateway.subscription(primary_key).content_hash
    if table == "transactions":
        return gateway.transaction(primary_key).content_hash
    raise GatewayError(f"未知记录表：{table}")
```

`src/ai_banking_copilot/policy/pep.py`（在模块常量区追加 `HANDLED_ACTIONS`）

```python
HANDLED_ACTIONS: frozenset[str] = frozenset(
    {
        "account.balance",
        "account.transactions",
        "bills.monthly",
        "bills.category_trend",
        "wealth.list",
        "card.list",
        "subscription.list",
        "transfer.create",
        "transfer.schedule",
        "wealth.subscribe",
        "wealth.redeem",
        "subscription.cancel",
        "subscription.sign",
        "card.lock",
        "card.unlock",
        "card.report_lost",
        "card.apply_virtual",
        "card.set_limit",
        "funds.freeze",
        "funds.release",
    }
)
"""PEP 可执行的全部规范动作（README「规范动作表」20 项，README 裁决 #14）。"""
```

`src/ai_banking_copilot/policy/pep.py`（在模块末尾追加）

```python
def _money_cents(params: dict[str, object], spec: ToolSpec) -> int:
    """按 `ToolSpec.money_param` 取金额（整数，单位分），供 PEP 包装成 `Money` 再调 `CoreBank`。"""
    if spec.money_param is None:
        raise NotAuthorizedError(f"动作 {spec.action} 未声明 money_param")
    return _require_int(params, spec.money_param)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/policy -v`
Expected: PASS（全部通过；本 Task 新增 16 项）

- [ ] **Step 5: 提交**

```bash
git add src/ai_banking_copilot/policy/pep.py tests/policy/test_pep.py
git commit -m "feat: 建立 PEP 写执行路径（幂等 + 执行门 + 执行后出处校验）"
```

---

### Task 9: 端到端集成验收（执行门 + 审计链 + 熔断 + 撤销）

**Files:**
- Test: `tests/policy/test_policy_integration.py`

**Interfaces:**
- Consumes: Task 1–8 全部产出 + 01 的 `Store` / `FactGateway` / `CoreBank`
- Produces: 跨模块不变式断言（`I1′`–`I4′` + 绿色纯度 + 计划级执行门 + 审计链完整性 + 熔断只加严 + 撤销竞态诚实性）

- [ ] **Step 1: 写失败的测试**

`tests/policy/test_policy_integration.py`

```python
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

import pytest

from ai_banking_copilot.contracts.authorization import Decision, Level, ReasonCode
from ai_banking_copilot.contracts.plan import (
    AnchorSource,
    IntentRef,
    Plan,
    PlanNode,
    cfhash,
)
from ai_banking_copilot.contracts.taint import Trust, Value
from ai_banking_copilot.gateway.facts import FactGateway
from ai_banking_copilot.gateway.models import Money
from ai_banking_copilot.gateway.seed import DEMO_USER_ID
from ai_banking_copilot.policy.audit import KIND_EXECUTION, AuditLog
from ai_banking_copilot.policy.breaker import BreakerTrigger, CircuitBreaker
from ai_banking_copilot.policy.gate import StalePlanError, assert_frozen
from ai_banking_copilot.policy.pdp import ObjectState, PolicyRequest, evaluate, evaluate_plan
from ai_banking_copilot.policy.pep import NotAuthorizedError, Pep, manifest_hash
from ai_banking_copilot.policy.saga import ReversalState, ReversalWindow, Saga
from ai_banking_copilot.tools.registry import ToolCall


@dataclass(frozen=True)
class _Net:
    amount: Money
    irreversibles: tuple[str, ...] = ()


def _decision(level: Level) -> Decision:
    reasons = (
        (ReasonCode.QUERY_ONLY,)
        if level is Level.GREEN
        else (ReasonCode.STATE_CHANGE_SMALL,)
    )
    return Decision(level=level, reasons=reasons)


def _trusted(value: object) -> Value[object]:
    """控制槽参数必须携带可信来源标记（02 的 `validate_params` 强制，H3）。"""
    return Value(value, Trust.TRUSTED)


def _query_params(account_id: str = "acc_savings") -> dict[str, object]:
    return {"account_id": _trusted(account_id)}


def _transfer_params(amount_cents: int = 50000) -> dict[str, object]:
    return {
        "user_id": _trusted(DEMO_USER_ID),
        "account_id": _trusted("acc_savings"),
        "payee_id": _trusted("p_lina"),
        "amount_cents": _trusted(amount_cents),
        "booked_on": _trusted("2026-10-01"),
    }


def _node_params(amount_cents: int = 50000) -> dict[str, object]:
    """计划节点参数（**不加 taint 包装**：`PlanNode.params` 是计划侧数据，不是工具调用参数）。"""
    return {
        "user_id": DEMO_USER_ID,
        "account_id": "acc_savings",
        "payee_id": "p_lina",
        "amount_cents": amount_cents,
        "booked_on": "2026-10-01",
    }


def _recorder(calls: list[str], tag: str) -> Callable[[], str]:
    def record() -> str:
        calls.append(tag)
        return tag

    return record


def _plan(amount_cents: int = 50000) -> Plan:
    return Plan(
        plan_id="p-cross",
        nodes=(
            PlanNode(
                node_id="n1",
                action="account.balance",
                params={"account_id": "acc_savings"},
                intent=IntentRef(source=AnchorSource.EXPLICIT, anchor_id="a1"),
            ),
            PlanNode(
                node_id="n2",
                action="transfer.create",
                params=_node_params(amount_cents),
                intent=IntentRef(source=AnchorSource.USER_APPROVED, anchor_id="a2"),
                depends_on=("n1",),
            ),
        ),
    )


def test_plan_gate_accepts_frozen_subgraph_and_rejects_tamper() -> None:
    plan = _plan()
    frozen = cfhash(plan.nodes)
    assert_frozen(plan, frozen)
    tampered = Plan(
        plan_id=plan.plan_id,
        nodes=plan.nodes[:1]
        + (replace(plan.nodes[1], params=_node_params(5000000)),),
    )
    with pytest.raises(StalePlanError):
        assert_frozen(tampered, frozen)


def test_i4_prime_plan_net_effect_threshold() -> None:
    plan = _plan()
    assert evaluate_plan(plan, _Net(amount=Money(50000))).level is Level.YELLOW
    assert evaluate_plan(plan, _Net(amount=Money(500000))).level is Level.RED
    assert evaluate_plan(plan, _Net(amount=Money(0))).level is Level.YELLOW


def test_i1_to_i3_prime_single_node_decision_table() -> None:
    query = PolicyRequest(
        user_id=DEMO_USER_ID,
        action="account.balance",
        amount=Money(0),
        daily_accumulated=Money(0),
        object_state=ObjectState.SELF,
        is_pure_query=True,
        irreversible=False,
    )
    assert evaluate(query).level is Level.GREEN

    over_limit = PolicyRequest(
        user_id=DEMO_USER_ID,
        action="transfer.create",
        amount=Money(150000),
        daily_accumulated=Money(150000),
        object_state=ObjectState.UNKNOWN,
        is_pure_query=False,
        irreversible=False,
    )
    assert evaluate(over_limit).level is Level.RED

    blocked = PolicyRequest(
        user_id=DEMO_USER_ID,
        action="transfer.create",
        amount=Money(100),
        daily_accumulated=Money(100),
        object_state=ObjectState.BLOCKED,
        is_pure_query=False,
        irreversible=False,
    )
    assert evaluate(blocked).level is Level.DENY

    irreversible = PolicyRequest(
        user_id=DEMO_USER_ID,
        action="card.report_lost",
        amount=Money(0),
        daily_accumulated=Money(0),
        object_state=ObjectState.SELF,
        is_pure_query=False,
        irreversible=True,
    )
    assert evaluate(irreversible).level is Level.RED


def test_end_to_end_query_then_transfer_with_intact_audit_chain(
    pep: Pep, audit: AuditLog, gateway: FactGateway
) -> None:
    query_call = ToolCall(action="account.balance", params=_query_params())
    query_result = pep.execute(query_call, _decision(Level.GREEN), manifest_hash(query_call), "q1")
    transfer_call = ToolCall(action="transfer.create", params=_transfer_params())
    transfer_result = pep.execute(
        transfer_call, _decision(Level.RED), manifest_hash(transfer_call), "t1"
    )
    assert query_result.status == "queried"
    assert transfer_result.status == "settled"
    assert [event.kind for event in audit.events()] == [KIND_EXECUTION, KIND_EXECUTION]
    assert audit.verify_chain() is True
    transaction_id = transfer_result.record_ids[0]
    txn = gateway.transaction(transaction_id.split(":")[1])
    assert gateway.verify(transaction_id, txn.content_hash) is True


def test_breaker_opens_on_rejections_and_only_unlock_restores(
    pep: Pep, breaker: CircuitBreaker
) -> None:
    for index in range(3):
        call = ToolCall(action="transfer.create", params=_transfer_params())
        with pytest.raises(NotAuthorizedError):
            pep.execute(call, _decision(Level.MANUAL), manifest_hash(call), f"m{index}")
    assert breaker.is_open() is True
    assert breaker.triggers() == (BreakerTrigger.CONSECUTIVE_AUTH_FAILURES,)
    blocked_call = ToolCall(action="transfer.create", params=_transfer_params())
    with pytest.raises(NotAuthorizedError, match="熔断"):
        pep.execute(blocked_call, _decision(Level.RED), manifest_hash(blocked_call), "t1")
    breaker.unlock()
    assert (
        pep.execute(blocked_call, _decision(Level.RED), manifest_hash(blocked_call), "t2").status
        == "settled"
    )


def test_reversal_race_never_claims_cancelled() -> None:
    now = datetime(2026, 10, 1, 9, 0, 0)
    calls: list[str] = []
    saga = Saga()
    saga.add("charge", _recorder(calls, "charge"), _recorder(calls, "refund"))
    saga.run()
    window = ReversalWindow.open("sub_video", now=now)
    outcome = window.reverse(now=now, settled=True, compensate=saga.compensate)
    assert outcome.state is ReversalState.COMPENSATING
    assert "已取消" not in outcome.message
    assert "已扣" in outcome.message
    assert outcome.compensated_steps == ("charge",)


def test_tampered_audit_line_is_detected(tmp_path: Path, pep: Pep, audit: AuditLog) -> None:
    call = ToolCall(action="account.balance", params=_query_params())
    pep.execute(call, _decision(Level.GREEN), manifest_hash(call), "q1")
    path = audit.path
    path.write_text(
        path.read_text(encoding="utf-8").replace("queried", "settled"), encoding="utf-8"
    )
    assert audit.verify_chain() is False
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/policy/test_policy_integration.py -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'tests.policy.test_policy_integration'`（文件尚未创建）

- [ ] **Step 3: 最小实现**

本 Task **不新增生产代码**：集成测试必须**只依赖** Task 1–8 已交付的公开接口。若某断言无法通过，**必须**回到对应 Task 修实现（而不是在测试里放宽断言）；不允许新增"只在测试里生效"的后门。

- [ ] **Step 4: 跑全套质量门与测试确认通过**

Run: `ruff check .`
Expected: `All checks passed!`

Run: `mypy`
Expected: `Success: no issues found`

Run: `python -m pytest -v`
Expected: PASS（00 / 01 既有测试未被破坏 + 本计划新增 ≥ 91 项）

- [ ] **Step 5: 提交**

```bash
git add tests/policy/test_policy_integration.py
git commit -m "test: 补充 03 端到端集成验收（执行门 + 审计链 + 熔断 + 撤销）"
```

---

## 完成判据（本计划验收）

- [ ] `ruff check .` → `All checks passed!`
- [ ] `mypy` → `Success: no issues found`（`policy/` 全部 `--strict` 通过）
- [ ] `python -m pytest tests/policy -v` → 全绿（本计划新增 ≥ 91 项）
- [ ] `python -m pytest` → 全绿（00 / 01 / 02 的既有测试未被破坏）
- [ ] **官方三色不得偏离**：`绿只含纯查询`（`GREEN` + 非纯查询 = 拒）有测试断言
- [ ] **`日累计 ≤1000 = 黄` / `>1000 = 红`** 的边界（1000.00 元 / 1000.01 元）各有断言
- [ ] **对象维度只升档不降档**（`VERIFIED_SAVED` + 超额仍为红）有测试断言
- [ ] **`blocked` = `DENY` 且为吸收态**（与金额、纯查询无关）有测试断言
- [ ] **`L_final = max(base, 各 modifier)` + 规则冲突取更严 + 原因并集** 有测试断言
- [ ] **首次向某对象大额强制升档**（`FLAG_FIRST_LARGE_TO_OBJECT` ⇒ ≥ 红）有测试断言
- [ ] **多候选不产出级别**（`reject_ambiguous(2)` 抛错）有测试断言
- [ ] **`NE ≥ T_net ⇒ 计划级 ≥ 红`**、**`NE = 0 ⇒ ≤ 黄`** 有测试断言
- [ ] **执行门**：`cfhash` 不匹配（改参数 / 增删节点）即拒，且 PEP 侧节点级哈希复核有断言
- [ ] **PEP 无裁决不执行**、级别 ≥ `MANUAL` 即拒、`GREEN` 不得执行状态变更，各有断言
- [ ] **PEP 覆盖规范动作表全部 20 个动作**（`HANDLED_ACTIONS == 规范动作表`）有测试断言
- [ ] **PEP 用 `ToolSpec.money_param` 把 `amount_cents` / `daily_limit_cents` 分数包装成 `Money`** 再调 `CoreBank`（由写路径用例覆盖）
- [ ] **执行后出处校验**：写回执的 `record_id` 经 `FactGateway.verify` 校验为真，有断言
- [ ] **审计链**：追加式 JSONL + `entry_hash` 链接 + 篡改 / 非法行 → `verify_chain()` 为假，有断言
- [ ] **`prev_hash` 与链头不一致 → `ChainBrokenError`** 有断言
- [ ] **熔断五类触发**齐备，且**打开后成功不改回 `CLOSED`**、唯一出口是 `unlock()`，有断言
- [ ] **熔断打开锁定高风险操作但纯查询不受影响**，有断言
- [ ] **撤销竞态不谎称"已取消"**（`settled=True` 走补偿 + 消息含"已扣 / 退回"）有断言
- [ ] **未修改 01 的 `gateway/store.py::SCHEMA_SQL`**（审计走独立 JSONL；`git diff` 不含 01 文件）
- [ ] 冻结面符号全部存在：`ObjectState` / `PolicyRequest` / `evaluate` / `evaluate_plan` / `DAILY_LIMIT_YUAN` / `NET_EFFECT_THRESHOLD_YUAN` / `assert_frozen` / `StalePlanError` / `Pep` / `ExecResult` / `NotAuthorizedError` / `AuditEvent` / `AuditLog` / `ChainBrokenError` / `BreakerState` / `CircuitBreaker` / `Saga` / `ReversalWindow` / `CompensationError`

### 与 PRD §9.2 序关系不变式的对照

| 不变式（PRD §9.2） | 本计划落地测试 |
|---|---|
| `I1′ 金额`：`日累计 <1000 ⇒ ≤黄`；`>1000 ⇒ ≥红` | `test_daily_at_limit_is_yellow` · `test_daily_over_limit_is_red` |
| `I2′ 对象`：`blocked ⇒ L4` | `test_blocked_is_deny_regardless_of_amount_and_purity` |
| `I3′ 可逆性`：`不可逆 ⇒ ≥红` | `test_irreversible_action_is_at_least_red` · `test_i1_to_i3_prime_single_node_decision_table` |
| `I4′ 净效应`：`NE ≥ T_net ⇒ ≥红`；`NE = 0 ⇒ ≤黄` | `test_plan_net_effect_at_threshold_is_red` · `test_plan_zero_net_effect_is_at_most_yellow` |
| 绿色纯度：任何绿色节点必须是纯查询 | `test_green_cannot_execute_state_change` · `test_pure_query_is_green` |
| 全局非退化 | 三色 / `MANUAL` / `DENY` 在 `tests/policy` 内各有 ≥1 断言 |

## 对冻结面的偏差与疑问（实现前需确认）

1. **`02-tool-registry.md` 已出：本计划已按 02 的「实际」签名复核并修正**（2026-10-01 对齐 README 规范动作表）：
   `ToolRegistry()` 无参构造、`register(spec)`、`get(action)`（未登记抛 `UnknownActionError(KeyError)`）、
   `actions()`、`validate_params(call) -> dict[str, object]`；`ToolSpec(action, risk, params, is_pure_query,
   reversible, money_param, description)`；`ParamSpec(name, slot, py_type, required)`；`ToolCall(action, params)`。
   关键修订：① PEP **消费 `validate_params` 的返回值**（去 taint 包装后的归一值），不再直接读 `call.params`；
   ② 控制槽参数在测试中以 `Value(..., Trust.TRUSTED)` 传入（02 接受裸值与 `Value(TRUSTED)`，
   仅拒绝 `Value(UNVERIFIED)`）；③ 异常基类
   `UnknownActionError(KeyError)` / `ParamValidationError(ValueError)` / `ForbiddenParamError(ValueError)`
   后两者**互不继承**（03 不重定义，直接复用 02）；④ 自建测试注册表注册**全部 20 个**规范动作
   （`bill.monthly` → `bills.monthly`、`funds.unfreeze` → `funds.release`、`card.set_status` → `card.report_lost`），
   并以「PEP 覆盖全部 20 个动作」的覆盖断言取代原「无处理器」用例（`card.unlock` 现已登记处理器）。
2. **动作命名已对齐**：以 README「规范动作表（唯一真源）」的 20 个动作为准；03 的裁决**不依赖具体动作名**（只按 `RiskClass` + 金额 + 对象态裁决），PEP 的写分派按表内动作名映射到 01 的 `CoreBank` 原语。03 的测试**自建注册表**（不依赖 02 的 catalog），以保证 03 可独立验收。
3. **`ReasonCode` 无「对象未认人」专用码**（00 冻结为 6 个值）。本计划用 `MANUAL_REVIEW` 表达「需人工（用户）认人确认」（`KNOWN_UNVERIFIED` / `UNKNOWN` ⇒ ≥ 黄）。若评审要求可解释性更强的码，需先在 00 增码并回溯。
4. **`PolicyRequest` 字段类型未在冻结面声明**：本计划定为 `amount: Money`（本笔）、`daily_accumulated: Money`（**含本笔**当日累计流出）、`context_flags: frozenset[str]`（白名单，未登记即拒）。
5. **`evaluate_plan(plan, net)` 的 `net` 类型未声明**：本计划用 `NetEffectView` Protocol（只要求 `amount: Money`、`irreversibles: tuple[str, ...]`），以**保持 03 不依赖 04**（roadmap 允许 P3 / P4 并行）。若 `planning.NetEffect.amount` 为 `int`，需改 Protocol 或改 04。
6. **执行门粒度 = 计划级 + 节点级叠加（README 裁决 §6）**：`policy.gate.assert_frozen(plan, frozen_hash)` 做**计划级**冻结校验（`frozen_hash == cfhash(plan.nodes)`），适用于 `Plan` 全量 DAG；`policy.pep.manifest_hash(call)` 做**节点级**成员资格校验（`cfhash` 由 `PlanNode.canonical()` 计算、**含 `intent` 锚点**，故 PEP 用统一锚点 `PEP_ANCHOR_ID` 生成节点）。**两者叠加**即 PRD 硬约束 #4 的「节点 ∈ 已冻结确认子图」——06 必须在确认记录里**同时保存 `Plan` 与 `cfhash`**：确认前用 `assert_frozen` 校验计划级、执行时用 `manifest_hash` 校验节点级。
7. **纯查询计划的绿色判定不由 `evaluate_plan` 承担**：`Plan` 不含 `is_pure_query`（该标记在 `ToolSpec` 上），故 `evaluate_plan` 对 `NE = 0` 只给下限 `YELLOW`；纯查询的绿色判定走单节点 `evaluate(is_pure_query=True)`，计划级取 `strictest(各节点裁决, 计划级下限)`。若 05 / 06 希望 `evaluate_plan` 直接返回 `GREEN`，需要把 `ToolRegistry` 传给 `evaluate_plan`（改签名）。
8. **执行后 `verify` 仅对写回执生效**：01 的 `FactGateway.verify` 只支持按**主键**回读（`accounts` / `payees` / `cards` / `transactions` / `subscriptions` / `wealth_products` / `holds`），而 `bills:{user}:{prefix}` 与集合型记录不是主键，`verify` 返回 `False`。故本计划：**写路径**用 `verify` 做数值出处校验；**纯查询路径**把 `(record_id, content_hash)` 直接写入审计（哈希来自只读网关本身）。
9. **`CircuitBreaker` 新增 `trip(trigger)` / `triggers()` / `failure_count()` / `TRIGGERS`**（冻结面只列了 `record_failure` / `record_success` / `is_open` / `lock` / `unlock` / `state`）。原因是五类触发中只有「连续授权失败」可由无参 `record_failure()` 计数驱动，其余四类必须由外部显式触发；同时 `trip` 接收枚举而非裸字符串（防拼写错误的静默失效）。属**新增**符号，不改冻结签名。
10. **`AuditLog.append` 的 `prev_hash` 约定**：空串 ⇒ 自动链接当前链头；非空 ⇒ 必须等于链头，否则抛 `ChainBrokenError`（防乱序 / 并发写链）。`AuditEvent` 的 `prev_hash` 因此允许空串。
11. **`AuditLog.events()` / `head()` 遇损坏行抛 `ChainBrokenError`（fail-closed）**，而 `verify_chain()` 返回 `False`（不抛）——两者语义不同，避免调用方用异常控制流做完整性检查。
12. **`evaluate` 的 `blocked` 分支短路**：`blocked` 时**只**返回 `(DENY, OBJECT_BLOCKED)`，不并集其它原因（吸收态 = 否决一切，原因单一更利于自动评测对齐 `I2′`）。若需要"同时报告金额维度"的可解释性，需改为并集（会改变断言）。
13. **上下文异常 modifier 会抬升纯查询**：`FLAG_DEVICE_CHANGE` / `FLAG_OFF_HOURS` / `FLAG_HIGH_FREQUENCY` 是**只升档**的 modifier，故"纯查询 + 异常上下文" ⇒ `YELLOW`（不再自动执行）。这与官方口径"绿 = 自动执行且仅纯查询"不冲突（官方约束的是绿色**不得含**变更，未要求所有纯查询**必须**为绿），且方向是**更严**（fail-safe）。若评审要求"纯查询恒绿"，需把这几类标记从 `modifier` 改为只对非纯查询生效（会改变断言与 `test_context_anomaly_raises_green_query_to_yellow`）。
14. **熔断与 PDP 的职责边界**：`CircuitBreaker` 只做"锁定 / 解锁"状态，不改裁决级别；PDP 的 `context_flags` 只做升档。二者互补但**互不调用**（避免环形依赖），由 PEP 串起来：熔断打开时 PEP 直接拒绝非纯查询。若希望熔断打开时把裁决抬到 `MANUAL`（而非直接拒），需在 05 / 06 侧组合。
15. **PEP 新增 `HANDLED_ACTIONS`（不在冻结表内）**：README 裁决 #14 要求 PEP 写分派覆盖规范动作表全部 20 个动作。为使覆盖度**可断言**，`policy.pep` 新增模块级常量 `HANDLED_ACTIONS`（20 个动作名的冻结集），测试断言 `HANDLED_ACTIONS == 规范动作表`。属**新增**符号，不改冻结签名。PEP 的写分派按 README 裁决 #14 映射：`transfer.schedule` 复用 `transfer`（带 `booked_on`）、`card.lock` / `card.unlock` / `card.report_lost` → `set_card_status`（`locked` / `active` / `lost`）、`card.apply_virtual` → `apply_virtual_card`、`card.set_limit` → `set_card_limit`、`subscription.sign` → `sign_subscription`。

## 后续计划（依赖本计划产出的接口）

| 计划 | 依赖本计划产出的 |
|---|---|
| `04-planning-kernel` | `policy.pdp.ObjectState`（对象态求值落点）、`policy.pdp.NetEffectView`（`NetEffect` 需满足其结构） |
| `05-scenarios` | `policy.pdp.evaluate` / `evaluate_plan`、`policy.pep.Pep.execute`、`policy.saga.ReversalWindow` / `Saga`（撤销与部分成功）、`policy.audit.AuditLog` |
| `06-web-channel` | `policy.gate.assert_frozen`（计划卡确认绑定哈希）、`policy.pep.manifest_hash`（逐节点执行门）、`policy.pep.ExecResult`、`policy.breaker.CircuitBreaker`（接管 / 解锁） |
| `07-eval-and-docs` | `policy.pdp` 的决策表（`expect_level` 基准）、`policy.audit.verify_chain`（过程合规证据）、`policy.breaker.TRIGGERS`（熔断五类触发举证） |

---

*v1 · 2026-10-01 · 依赖 `00-foundation` / `01-fact-gateway` / `02-tool-registry`；跨计划接口以 `README.md` 冻结面为准。*

# 04 · 规划内核（多意图拆解 · 槽位补全 · 锚点账本 · DAG 规划 · 计划级净效应 · LLM 降级）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> **⚠️ 回溯说明（2026-10-08）**：本文是**当时**的实施记录。其中 `planning.slots.SLOT_PARAMS` 以**手写字典**给出（本文 Task 4）；自 2026-10-08 起它改为**由 02 目录派生**（`ParamSpec.slot_name → ParamSpec.name`，同名冲突即构造期抛），**符号与可见内容逐项不变（仍 14 项）**，见 [README.md](README.md) 冻结面表与**裁决 #26**。

**Goal:** 建成本方案的**能力上限**——`规划内核`：把一句自然语言拆成**多意图**、用 01 的确定性解析器把**控制槽**补全成可信值、把意图编排成**带锚点的 DAG 计划**、算出**计划级净效应**，并在 LLM 不可用时**确定性降级**；全程**不产事实、不产级别、不执行动作**。

**Architecture:** 单体 Python 包 `ai_banking_copilot`，`src/` 布局。本计划在 `src/ai_banking_copilot/planning/` 下交付六个职责单一的小模块：`llm.py`（意图契约 + 模板通道 + 可选真实 LLM 通道）、`anchors.py`（锚点账本）、`slots.py`（控制槽补全）、`net_effect.py`（带符号资金流）、`kernel.py`（编排出口）、`orchestrator.py`（Task 0 spike 产出的可插桩步骤驱动器 = 自研 fallback 骨架）。内核**只产出 `plan.Plan`**；级别裁决交给 `policy.pdp`、执行交给 `policy.pep`——这是 H2「动作零越权」的落点。

**Tech Stack:** Python 3.11+ · 标准库 only（`json` / `re` / `os` / `importlib` / `dataclasses` / `datetime`）· 可选依赖 `httpx`（仅真实 LLM 通道，默认关闭）· pytest · ruff · mypy（`--strict` 只对 `src/`）

## Global Constraints

- Python **≥ 3.11**；**不使用 3.12+ 专属语法**；需要泛型时用 `Generic[T]` / `TypeVar`
- 行宽 **100**；ruff 规则集 `E,F,I,UP,B`；`mypy --strict` 对 `src/` 生效（测试文件不参与 mypy）
- **核心逻辑只用标准库**；真实 LLM 通道的 HTTP 客户端 `httpx` **必须声明为 `[project.optional-dependencies] llm`**（可选依赖）、**必须可注入**（`transport=`）、**必须懒加载**、**默认走 `TemplateIntentModel`**
- **测试与 CI 绝不联网**：本计划所有测试要么用模板通道，要么注入假 `transport`；任何需要外网的路径都必须以 `LlmUnavailableError` 收场
- **H1 事实零生成**：内核不产数值/实体；控制槽取值只来自 `context`（受信任调用方）与 01 的 `resolve_payees` / `parse_amount` / `parse_temporal`
- **H2 动作零越权**：内核只产 `Plan`，**不执行**、**不裁决级别**；节点动作必须 ∈ 注册表已知动作
- **H3 输入零信任**：`untrusted` 值**永不进入控制槽**；LLM 给出的槽位一律只当**提示**，不得作为控制槽取值
- **H7 自主度如实声明**：编排层自主（拆解 / 组合 / 补槽），工具层封闭（仅白名单动作）；材料不得暗示"任意自主"
- **H9 稳定性边界**：降级通道声明为**能力子集**，**不宣称为等价实现**
- **`derived` 锚点铁律**：仅机械推导、**父 ∈ {explicit, user_approved}**、**深度 ≤ 1**、**禁止 `derived → derived` 链**；`validate_plan` 在**节点级**禁用 `derived`（`derived` 只用于推导记录，不作节点锚点）
- **控制槽禁止 `unverified`**；**多候选收款人强制澄清、不产出级别**
- **无占位符**：不写 TBD / TODO / "稍后补充"；每个 Task 都要能**跑通**
- 提交信息格式：`<type>: <描述>`（type ∈ feat / fix / test / docs / chore / refactor）
- 所有新增文件必须**同时**提交测试
- 跨计划接口见 `README.md` 的「跨计划接口主线（冻结面）」——**冻结符号不得改名或改签名**

> **冻结面覆盖与新增符号（备注，供实施者对齐）**
>
> 本计划实现 04 冻结面**全部符号**：`planning.llm`（`IntentProposal` / `Intent` / `IntentModel` / `TemplateIntentModel` / `LlmIntentModel`）、`planning.anchors`（`AnchorLedger` / `DerivedDepthError`）、`planning.net_effect`（`NetEffect` / `net_effect`）、`planning.kernel`（`Question` / `PlanningResult` / `Kernel`）。
>
> 为实现任务书要求的「槽位补全模块」与「可插桩 spike」，额外新增（**不改动、不替代任何冻结符号**）：`planning.slots`（`REQUIRED_SLOTS` / `SLOT_PARAMS` / `SlotQuestion` / `SlotFill` / `SlotInputs` / `fill_control_slots`）、`planning.orchestrator`（`StepState` / `StepTransition` / `AuditHook` / `AuthzHook` / `StepDriver`），以及少量**内部辅助符号**：`llm.ActionKind` / `llm.ACTION_KINDS` / `llm.CANONICAL_ACTIONS` / `llm.LlmUnavailableError` / `llm.FallbackIntentModel`、`anchors.Anchor` / `anchors.UnknownAnchorError`、`net_effect.NetEffectError`、`kernel.ActionCatalog` / `kernel.CONFIDENCE_THRESHOLD` / `kernel.CONTEXT_*`。
>
> **动作词表单一来源**：`llm.ACTION_KINDS` 的键即本计划的**规范动作名**（**二十个**，逐项对齐 `README.md` 的「规范动作表（唯一真源）」）。`net_effect` 的 `cf` 口径、`slots.REQUIRED_SLOTS` 的必填槽、`kernel` 的资源键都以此为唯一真源。Task 1 新增一条**交叉断言测试**：`set(ACTION_KINDS) == set(build_default_registry().actions())`，把与 02 `tools.catalog::build_default_registry()` 的动作名对齐**固化为可执行的不变式**。

**前置依赖（开工顺序）**

| 本计划的 Task | 需要的上游产物 | 是否已就绪 |
|---|---|---|
| Task 0（spike） | 无 | 就绪 |
| Task 1、2（llm） | 无（纯标准库 + 可选 httpx） | 就绪 |
| Task 3（anchors） | 00 的 `contracts.plan` | 就绪 |
| Task 4（slots） | 00 的 `contracts.taint`、01 的 `gateway.models` / `gateway.resolvers` | 就绪 |
| Task 5（net_effect） | 00 的 `contracts.plan`、01 的 `gateway.facts` / `gateway.models` | 就绪 |
| Task 6、7（kernel） | 02 的 `tools.registry::ToolRegistry`（**结构上**满足 `kernel.ActionCatalog` 即可） | **02 已出**：内核以 `ActionCatalog` 协议**只依赖 `actions()`**；Task 1 已用 `build_default_registry()` 做**动作名交叉断言**，Task 6/7 仍用本地替身 `StubRegistry(CANONICAL_ACTIONS)` 先行验证（保持 04 与 02 解耦） |

---

### Task 0: 编排选型可证伪 spike —— 每处状态转移都能插桩吗？

**Files:**
- Create: `src/ai_banking_copilot/planning/__init__.py`
- Create: `src/ai_banking_copilot/planning/orchestrator.py`
- Create: `tests/planning/__init__.py`
- Test: `tests/planning/test_orchestrator.py`

**Interfaces:**
- Consumes: 无（起点）
- Produces：`StepState`（`PENDING` / `RUNNING` / `DONE` / `FAILED`）· `StepTransition(step_id, before, after)`（frozen）· `AuditHook = Callable[[StepTransition], None]` · `AuthzHook = Callable[[StepTransition], bool]` · `StepDriver(on_transition, authorize=None)`（方法 `run(steps, worker) -> tuple[StepTransition, ...]` / `state_of(step_id) -> StepState`）

**Spike 判据（可证伪，≤ 2 人日）**

> 能否在 ≤ 2 人日规模内，让**每一处状态转移**都经过**同一个**插桩出口，出口内**先审计、后鉴权**，且**不存在第二条迁移路径**可绕过？
>
> **反证条件**（任一命中即判据不成立）：① 存在任一状态迁移不经插桩点；② 插桩需要改动超出单个模块的范围；③ 无法在测试里断言"每次迁移都被插桩"。

- [ ] **Step 1: 写失败的测试**

`tests/planning/__init__.py`

```python
```

`tests/planning/test_orchestrator.py`

```python
import pytest

from ai_banking_copilot.planning.orchestrator import (
    StepDriver,
    StepState,
    StepTransition,
)


def test_every_transition_passes_through_the_audit_hook() -> None:
    seen: list[StepTransition] = []
    driver = StepDriver(on_transition=seen.append)
    driver.run(["s1", "s2"], worker=lambda step_id: None)
    assert [item.step_id for item in seen] == ["s1", "s1", "s2", "s2"]
    assert [item.after for item in seen] == [
        StepState.RUNNING,
        StepState.DONE,
        StepState.RUNNING,
        StepState.DONE,
    ]
    assert driver.state_of("s1") is StepState.DONE


def test_authorize_hook_is_called_for_every_transition() -> None:
    calls: list[StepTransition] = []

    def _allow(transition: StepTransition) -> bool:
        calls.append(transition)
        return True

    driver = StepDriver(on_transition=lambda transition: None, authorize=_allow)
    driver.run(["s1"], worker=lambda step_id: None)
    assert len(calls) == 2


def test_denied_transition_is_audited_but_never_applied() -> None:
    seen: list[StepTransition] = []

    def _deny_start(transition: StepTransition) -> bool:
        return transition.after is not StepState.RUNNING

    driver = StepDriver(on_transition=seen.append, authorize=_deny_start)
    with pytest.raises(PermissionError):
        driver.run(["s1"], worker=lambda step_id: None)
    assert [item.after for item in seen] == [StepState.RUNNING]
    assert driver.state_of("s1") is StepState.PENDING


def test_failed_step_is_audited_as_failed_and_propagates() -> None:
    seen: list[StepTransition] = []

    def _boom(step_id: str) -> None:
        raise RuntimeError("boom")

    driver = StepDriver(on_transition=seen.append)
    with pytest.raises(RuntimeError, match="boom"):
        driver.run(["s1"], worker=_boom)
    assert [item.after for item in seen] == [StepState.RUNNING, StepState.FAILED]
    assert driver.state_of("s1") is StepState.FAILED
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/planning/test_orchestrator.py -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'ai_banking_copilot.planning'`

- [ ] **Step 3: 最小实现**

`src/ai_banking_copilot/planning/__init__.py`

```python
"""规划内核（能力上限）：拆解 / 槽位 / 锚点 / DAG / 净效应 / LLM 降级。

本包**只产计划**，不执行动作、不裁决级别（H2）：执行只能由 `policy.pep` 在 PDP 裁决后进行。
"""
```

`src/ai_banking_copilot/planning/orchestrator.py`

```python
"""最小可插桩步骤驱动器（Task 0 spike 产出 · 自研 fallback 骨架）。

设计要点（为什么"不可绕过"）：
- **唯一迁移出口**：所有状态迁移只经过 `_advance`；没有第二条修改 `_states` 的路径，
  因此插桩点无法被业务代码绕过。
- **先审计后鉴权**：`on_transition` 先记录（含被拒的尝试），`authorize` 再放行；
  鉴权不通过则**状态不推进**并抛错——审计留痕与安全拦截各司其职。
- **零依赖**：只用标准库；可被 `policy.pep` 或框架适配层复用。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum


class StepState(str, Enum):
    """步骤状态机。"""

    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


@dataclass(frozen=True)
class StepTransition:
    """一次状态转移（审计与鉴权的共同载荷）。"""

    step_id: str
    before: StepState
    after: StepState


AuditHook = Callable[[StepTransition], None]
"""审计插桩：必须对**每一次尝试的迁移**都被调用。"""

AuthzHook = Callable[[StepTransition], bool]
"""鉴权插桩：返回 False 即拒绝该迁移。"""


class StepDriver:
    """步骤驱动器：每个状态转移都强制过插桩点。"""

    def __init__(self, on_transition: AuditHook, authorize: AuthzHook | None = None) -> None:
        self._on_transition = on_transition
        self._authorize = authorize
        self._states: dict[str, StepState] = {}
        self._transitions: list[StepTransition] = []

    def run(
        self, steps: Sequence[str], worker: Callable[[str], None]
    ) -> tuple[StepTransition, ...]:
        """按给定顺序执行步骤；返回全部（含被拒）迁移记录。"""
        for step_id in steps:
            self._states[step_id] = StepState.PENDING
        for step_id in steps:
            self._advance(step_id, StepState.RUNNING)
            try:
                worker(step_id)
            except Exception:
                self._advance(step_id, StepState.FAILED)
                raise
            self._advance(step_id, StepState.DONE)
        return tuple(self._transitions)

    def state_of(self, step_id: str) -> StepState:
        return self._states[step_id]

    def _advance(self, step_id: str, after: StepState) -> None:
        """**唯一**的状态迁移出口：先审计，后鉴权，再做实际推进。"""
        before = self._states[step_id]
        transition = StepTransition(step_id=step_id, before=before, after=after)
        self._on_transition(transition)
        if self._authorize is not None and not self._authorize(transition):
            raise PermissionError(
                f"状态迁移被鉴权插桩拒绝：{step_id} {before.value}->{after.value}"
            )
        self._states[step_id] = after
        self._transitions.append(transition)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/planning/test_orchestrator.py -v`
Expected: PASS（4 passed）

- [ ] **Step 5: 提交**

```bash
git add src/ai_banking_copilot/planning tests/planning
git commit -m "feat: 编排选型 spike（可插桩步骤驱动器 + 每处状态迁移强制过插桩点）"
```

**Spike 结论（落地裁决，用户已批准本项）**

- **实测证据（判据成立）**：`StepDriver._advance` 是**唯一**的迁移出口；四个测试分别断言了 ① 每次迁移都进审计钩子、② 每次迁移都进鉴权钩子、③ 被拒的迁移仍留痕且状态不推进、④ 失败迁移被审计为 `FAILED`。判据的三条反证条件**均未命中**，插桩覆盖面 = 100%，改动范围 = 单模块（`orchestrator.py`），断言可机读。
- **落地结论：框架优先 / 自研 fallback**。沿用 PRD §15.3 **D1** 的裁决——**默认框架优先**（差异化不在编排层，自研是工期黑洞）；本 Task 产出的 `StepDriver` 保留为**自研 fallback**，当且仅当选定框架**无法在 ≤2 人日内**为每处状态转移提供等价插桩覆盖（同样的三条反证条件）时启用。
- **不可放松的硬约束**：无论走框架还是 fallback，**"每处状态转移必须过统一审计/鉴权插桩点"不得放宽**；框架路径必须在 W2 用**与本 Task 同构的四条断言**做验收，验收不过即切 fallback。

---

### Task 1: 意图契约与模板通道（能力侧最小面 + 确定性降级）

**Files:**
- Create: `src/ai_banking_copilot/planning/llm.py`
- Test: `tests/planning/test_llm_template.py`

**Interfaces:**
- Consumes: 无（纯标准库）
- Produces：
  - `ActionKind(str, Enum)`：`QUERY` / `STATE` / `OUTFLOW` / `INFLOW` / `CANCEL_OUTFLOW` / `IRREVERSIBLE`
  - `ACTION_KINDS: Mapping[str, ActionKind]`（二十个规范动作 → 净效应类别，`cf` 口径唯一真源；逐项对齐 `README.md` 规范动作表）
  - `CANONICAL_ACTIONS: tuple[str, ...]`（= `tuple(ACTION_KINDS)`，共 20 项）
  - `Intent(action, slots={}, confidence=0.0, evidence="")`（frozen；`slots` 是**提示**，`confidence ∈ [0, 1]`）
  - `IntentProposal(intents, raw)`（frozen）
  - `IntentModel`（`Protocol`，`@runtime_checkable`；`propose(utterance, context) -> IntentProposal`）
  - `TemplateIntentModel()`（降级通道：确定性模板 + 规则，零模型 / 零网络）

- [ ] **Step 1: 写失败的测试**

`tests/planning/test_llm_template.py`

```python
import pytest

from ai_banking_copilot.planning.llm import (
    ACTION_KINDS,
    CANONICAL_ACTIONS,
    Intent,
    IntentModel,
    IntentProposal,
    TemplateIntentModel,
)
from ai_banking_copilot.tools.catalog import build_default_registry


def test_action_kinds_cover_the_canonical_vocabulary() -> None:
    assert set(ACTION_KINDS) == set(CANONICAL_ACTIONS)
    assert len(CANONICAL_ACTIONS) == 20


def test_action_kinds_match_the_tool_registry_vocabulary() -> None:
    """交叉断言：04 的动作词表必须与 02 目录**逐项一致**（README 规范动作表唯一真源）。"""
    assert set(ACTION_KINDS) == set(build_default_registry().actions())


def test_intent_rejects_out_of_range_confidence() -> None:
    with pytest.raises(ValueError):
        Intent(action="bills.monthly", confidence=1.5)


def test_intent_requires_action() -> None:
    with pytest.raises(ValueError):
        Intent(action="")


def test_template_model_satisfies_the_protocol() -> None:
    assert isinstance(TemplateIntentModel(), IntentModel)


def test_template_model_extracts_multiple_intents_in_clause_order() -> None:
    utterance = "转给李娜 500 元，再查一下上月账单"
    proposal = TemplateIntentModel().propose(utterance, {})
    assert isinstance(proposal, IntentProposal)
    assert [intent.action for intent in proposal.intents] == ["transfer.create", "bills.monthly"]
    assert proposal.raw == utterance


def test_template_model_keeps_clause_span_as_a_hint() -> None:
    proposal = TemplateIntentModel().propose("转给李娜 500 元，再查一下上月账单", {})
    assert proposal.intents[0].slots["span"] == "转给李娜 500 元"


def test_template_model_prefers_scheduled_transfer_over_plain_transfer() -> None:
    proposal = TemplateIntentModel().propose("定时转账给李娜 500 元", {})
    assert [intent.action for intent in proposal.intents] == ["transfer.schedule"]


def test_template_model_returns_nothing_for_unknown_utterance() -> None:
    assert TemplateIntentModel().propose("今天天气不错", {}).intents == ()


def test_template_model_is_deterministic() -> None:
    model = TemplateIntentModel()
    assert model.propose("冻结 5000 元", {}) == model.propose("冻结 5000 元", {})
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/planning/test_llm_template.py -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'ai_banking_copilot.planning.llm'`

- [ ] **Step 3: 最小实现**

`src/ai_banking_copilot/planning/llm.py`

```python
"""意图模型：把一句话变成**意图提案**（只提案，不产事实、不执行）。

三条纪律：
1. **LLM 输出一律视为提示**：`Intent.slots` 只当线索，**绝不作为控制槽取值**（H1/H3）；
2. **默认通道是模板**：`TemplateIntentModel` 是确定性模板 + 规则，零模型、零网络，
   是 LLM 不可用时的降级通道（H9：声明为能力子集，非等价实现）；
3. **真实通道必须显式启用**：`LlmIntentModel` 需要注入 `transport` 或安装可选依赖 `httpx`，
   且**懒加载**——不在构造期触碰网络。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol, runtime_checkable


class ActionKind(str, Enum):
    """动作的净效应类别（cf 口径，PRD §5.3）。"""

    QUERY = "query"
    """纯查询：不进 cf。"""

    STATE = "state"
    """状态变更但非不可逆、无资金流：不进 cf。"""

    OUTFLOW = "outflow"
    """流出（正 cf）：转账 / 定时转账 / 理财申购 / 代扣签约 / 自动续费 / 冻结资金。"""

    INFLOW = "inflow"
    """流入（负流出）：理财赎回 / 解冻。"""

    CANCEL_OUTFLOW = "cancel_outflow"
    """取消未来流出（记负）：订阅取消 / 解约。"""

    IRREVERSIBLE = "irreversible"
    """不可逆：挂失——**不入 cf**，单列触发 ≥ 红色 + L3。"""


ACTION_KINDS: Mapping[str, ActionKind] = {
    "account.balance": ActionKind.QUERY,
    "account.transactions": ActionKind.QUERY,
    "bills.monthly": ActionKind.QUERY,
    "bills.category_trend": ActionKind.QUERY,
    "wealth.list": ActionKind.QUERY,
    "card.list": ActionKind.QUERY,
    "subscription.list": ActionKind.QUERY,
    "transfer.create": ActionKind.OUTFLOW,
    "transfer.schedule": ActionKind.OUTFLOW,
    "wealth.subscribe": ActionKind.OUTFLOW,
    "wealth.redeem": ActionKind.INFLOW,
    "subscription.cancel": ActionKind.CANCEL_OUTFLOW,
    "subscription.sign": ActionKind.OUTFLOW,
    "card.lock": ActionKind.STATE,
    "card.unlock": ActionKind.STATE,
    "card.report_lost": ActionKind.IRREVERSIBLE,
    "card.apply_virtual": ActionKind.STATE,
    "card.set_limit": ActionKind.STATE,
    "funds.freeze": ActionKind.OUTFLOW,
    "funds.release": ActionKind.INFLOW,
}
"""规范动作词表（20 项，逐项对齐 `README.md` 规范动作表）：本计划所有模块
（slots / net_effect / kernel）的唯一动作来源。禁止 `bill.query` 等表外动作名。"""

CANONICAL_ACTIONS: tuple[str, ...] = tuple(ACTION_KINDS)


@dataclass(frozen=True)
class Intent:
    """一个候选意图。`slots` 是**提示**（unverified），不是控制槽取值。"""

    action: str
    slots: Mapping[str, object] = field(default_factory=dict)
    confidence: float = 0.0
    evidence: str = ""

    def __post_init__(self) -> None:
        if not self.action:
            raise ValueError("intent.action 不能为空")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("intent.confidence 必须落在 [0, 1]")


@dataclass(frozen=True)
class IntentProposal:
    """一次意图提案：候选意图（按原文子句顺序）+ 原文。"""

    intents: tuple[Intent, ...]
    raw: str


@runtime_checkable
class IntentModel(Protocol):
    """意图模型协议：任何实现都必须可被**不联网**的模板通道替换。"""

    def propose(self, utterance: str, context: Mapping[str, object]) -> IntentProposal:
        """把一句自然语言变成意图提案。"""
        ...


_CLAUSE_SPLIT = re.compile(r"[，,。；;、]|然后|接着|再|并且|同时")

_TEMPLATE_RULES: tuple[tuple[re.Pattern[str], str, str], ...] = (
    (re.compile(r"定时|定转|预约转"), "transfer.schedule", "命中『定时转账』模板"),
    (re.compile(r"转账|转给|汇款|打款"), "transfer.create", "命中『转账』模板"),
    (re.compile(r"赎回|卖出理财"), "wealth.redeem", "命中『赎回』模板"),
    (re.compile(r"申购|买理财|理财.*买"), "wealth.subscribe", "命中『理财申购』模板"),
    (re.compile(r"挂失"), "card.report_lost", "命中『挂失』模板"),
    (re.compile(r"锁卡|限制.*卡|卡.*限制"), "card.lock", "命中『锁卡』模板"),
    (re.compile(r"取消订阅|退订|取消代扣|关掉自动续费"), "subscription.cancel", "命中『取消订阅』模板"),
    (re.compile(r"签约|代扣|自动续费|续费"), "subscription.sign", "命中『代扣签约』模板"),
    (re.compile(r"冻结|锁定.*资金|锁.*元"), "funds.freeze", "命中『冻结资金』模板"),
    (re.compile(r"账单|消费分析|花了多少|消费明细"), "bills.monthly", "命中『账单分析』模板"),
)
"""规则按**优先级**排列：更具体的规则在前（如"定时"先于"转账"）。"""


class TemplateIntentModel:
    """降级通道：确定性模板 + 规则（零模型、零网络）。"""

    def propose(self, utterance: str, context: Mapping[str, object]) -> IntentProposal:
        intents: list[Intent] = []
        for clause in _split_clauses(utterance):
            for pattern, action, evidence in _TEMPLATE_RULES:
                if pattern.search(clause):
                    intents.append(
                        Intent(
                            action=action,
                            slots={"span": clause},
                            confidence=0.9,
                            evidence=evidence,
                        )
                    )
                    break
        return IntentProposal(intents=tuple(intents), raw=utterance)


def _split_clauses(utterance: str) -> tuple[str, ...]:
    """按标点与连接词切子句；只产出**原文子串**（供"可回指"校验）。"""
    parts = [part.strip() for part in _CLAUSE_SPLIT.split(utterance)]
    return tuple(part for part in parts if part)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/planning/test_llm_template.py -v`
Expected: PASS（10 passed）

- [ ] **Step 5: 提交**

```bash
git add src/ai_banking_copilot/planning/llm.py tests/planning/test_llm_template.py
git commit -m "feat: 建立意图契约与模板通道（规范动作词表 + 确定性降级）"
```

---

### Task 2: 真实 LLM 通道（可注入 · 懒加载 · 可选依赖）

**Files:**
- Modify: `src/ai_banking_copilot/planning/llm.py`（在文件末尾追加）
- Modify: `pyproject.toml`（新增可选依赖组 `llm`）
- Test: `tests/planning/test_llm_real.py`

**Interfaces:**
- Consumes: Task 1 的 `Intent` / `IntentProposal` / `CANONICAL_ACTIONS`
- Produces：
  - `LlmUnavailableError(RuntimeError)`（凭据缺失 / 可选依赖缺失 / 调用失败 / 响应不可解析——一律归此，供降级捕获）
  - `HttpTransport = Callable[[str], str]`（输入请求 JSON、返回响应 JSON；**测试注入此接口即不联网**）
  - `LlmIntentModel(*, transport=None, api_key=None, model="qwen-plus", endpoint=..., timeout_s=20.0, allow_actions=CANONICAL_ACTIONS)`（属性 `loaded: bool`；方法 `propose`）

- [ ] **Step 1: 写失败的测试**

`tests/planning/test_llm_real.py`

```python
import json

import pytest

from ai_banking_copilot.planning.llm import (
    LlmIntentModel,
    LlmUnavailableError,
)


def test_injected_transport_is_used_without_network() -> None:
    requests: list[str] = []

    def _transport(request: str) -> str:
        requests.append(request)
        return json.dumps(
            {"intents": [{"action": "transfer.create", "confidence": 0.8, "evidence": "转给李娜"}]}
        )

    proposal = LlmIntentModel(transport=_transport).propose("转给李娜 500 元", {})
    assert [intent.action for intent in proposal.intents] == ["transfer.create"]
    assert proposal.intents[0].confidence == 0.8
    assert len(requests) == 1


def test_actions_outside_the_whitelist_are_dropped() -> None:
    def _transport(request: str) -> str:
        return json.dumps({"intents": [{"action": "exec_shell", "confidence": 0.9}]})

    assert LlmIntentModel(transport=_transport).propose("rm -rf /", {}).intents == ()


def test_malformed_response_raises_unavailable() -> None:
    def _transport(request: str) -> str:
        return "not-json"

    with pytest.raises(LlmUnavailableError, match="JSON"):
        LlmIntentModel(transport=_transport).propose("转给李娜 500 元", {})


def test_non_numeric_confidence_raises_unavailable() -> None:
    def _transport(request: str) -> str:
        return json.dumps({"intents": [{"action": "bills.monthly", "confidence": "高"}]})

    with pytest.raises(LlmUnavailableError, match="confidence"):
        LlmIntentModel(transport=_transport).propose("查账单", {})


def test_missing_credentials_raise_before_any_client_is_loaded(monkeypatch) -> None:
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    model = LlmIntentModel()
    assert model.loaded is False
    with pytest.raises(LlmUnavailableError, match="凭据"):
        model.propose("转给李娜 500 元", {})


def test_transport_is_injected_not_loaded_eagerly() -> None:
    def _transport(request: str) -> str:
        return json.dumps({"intents": []})

    assert LlmIntentModel().loaded is False
    assert LlmIntentModel(transport=_transport).loaded is True
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/planning/test_llm_real.py -v`
Expected: FAIL —— `ImportError: cannot import name 'LlmIntentModel' from 'ai_banking_copilot.planning.llm'`

- [ ] **Step 3: 最小实现**

`pyproject.toml` 的 `[project.optional-dependencies]` 改为：

```toml
[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "ruff>=0.6",
    "mypy>=1.11",
]
llm = [
    "httpx>=0.27",
]
```

`src/ai_banking_copilot/planning/llm.py`（在文件末尾追加；同时把文件顶部导入段改为下面这版，新增 `importlib` / `json` / `os` / `Callable` / `Sequence` / `cast`）

```python
from __future__ import annotations

import importlib
import json
import os
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol, cast, runtime_checkable
```

```python
class LlmUnavailableError(RuntimeError):
    """真实 LLM 通道不可用（缺凭据 / 缺可选依赖 / 调用失败 / 响应不可解析）。

    归一到一个异常类型，是为了让 `FallbackIntentModel` 用**一处**捕获完成降级。
    """


HttpTransport = Callable[[str], str]
"""HTTP 传输抽象：输入请求 JSON 字符串，返回响应 JSON 字符串。测试注入它即完全离线。"""


class _HttpResponse(Protocol):
    status_code: int
    text: str


class _HttpClient(Protocol):
    def post(
        self,
        url: str,
        *,
        json: Mapping[str, object],
        headers: Mapping[str, str],
        timeout: float,
    ) -> _HttpResponse: ...


class _HttpClientFactory(Protocol):
    def Client(self, *, timeout: float) -> _HttpClient: ...


class LlmIntentModel:
    """真实 LLM 意图通道：可注入、懒加载、默认不参与测试与 CI。"""

    def __init__(
        self,
        *,
        transport: HttpTransport | None = None,
        api_key: str | None = None,
        model: str = "qwen-plus",
        endpoint: str = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
        timeout_s: float = 20.0,
        allow_actions: Sequence[str] = CANONICAL_ACTIONS,
    ) -> None:
        self._transport = transport
        self._api_key = api_key if api_key is not None else os.environ.get("DASHSCOPE_API_KEY")
        self._model = model
        self._endpoint = endpoint
        self._timeout_s = timeout_s
        self._allow_actions = tuple(allow_actions)

    @property
    def loaded(self) -> bool:
        """是否已具备可用的传输通道（注入或已懒加载）。"""
        return self._transport is not None

    def propose(self, utterance: str, context: Mapping[str, object]) -> IntentProposal:
        transport = self._ensure_transport()
        request = json.dumps(
            {
                "model": self._model,
                "messages": [
                    {"role": "system", "content": _system_prompt(self._allow_actions)},
                    {"role": "user", "content": utterance},
                ],
            },
            ensure_ascii=False,
        )
        try:
            raw = transport(request)
        except Exception as error:
            raise LlmUnavailableError(f"LLM 调用失败：{error}") from error
        return _parse_proposal(raw, utterance, self._allow_actions)

    def _ensure_transport(self) -> HttpTransport:
        """懒加载：构造期不碰网络；首次 `propose` 才决定用注入通道还是可选依赖。"""
        if self._transport is not None:
            return self._transport
        api_key = self._api_key
        if not api_key:
            raise LlmUnavailableError("缺少 LLM 凭据（环境变量 DASHSCOPE_API_KEY）")
        transport = _load_httpx_transport(
            api_key=api_key,
            endpoint=self._endpoint,
            timeout_s=self._timeout_s,
        )
        self._transport = transport
        return transport


def _system_prompt(actions: tuple[str, ...]) -> str:
    return (
        "你是银行智能体的意图解析器，只输出 JSON，不解释。"
        f"可选动作仅限于：{', '.join(actions)}。"
        '输出格式：{"intents":[{"action":"...","confidence":0.0,"evidence":"..."}]}'
    )


def _load_httpx_transport(*, api_key: str, endpoint: str, timeout_s: float) -> HttpTransport:
    """懒加载可选依赖 httpx；未安装则降级为 `LlmUnavailableError`（H9）。"""
    try:
        module = importlib.import_module("httpx")
    except ImportError as error:
        raise LlmUnavailableError("未安装可选依赖 httpx，真实 LLM 通道不可用") from error
    factory = cast(_HttpClientFactory, module)
    client = factory.Client(timeout=timeout_s)

    def transport(request: str) -> str:
        payload = json.loads(request)
        response = client.post(
            endpoint,
            json=payload,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout_s,
        )
        if response.status_code != 200:
            raise LlmUnavailableError(f"LLM 返回状态 {response.status_code}")
        return response.text

    return transport


def _parse_proposal(raw: str, utterance: str, allow_actions: tuple[str, ...]) -> IntentProposal:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as error:
        raise LlmUnavailableError(f"LLM 响应不是合法 JSON：{error}") from error
    if not isinstance(payload, dict):
        raise LlmUnavailableError("LLM 响应必须是 JSON 对象")
    items = payload.get("intents")
    if not isinstance(items, list):
        raise LlmUnavailableError("LLM 响应缺少 intents 数组")
    intents: list[Intent] = []
    for item in items:
        if not isinstance(item, dict):
            raise LlmUnavailableError("intents 元素必须是 JSON 对象")
        action = str(item.get("action", ""))
        if action not in allow_actions:
            continue
        confidence = _confidence(item.get("confidence", 0.0))
        slots: dict[str, object] = {}
        raw_slots = item.get("slots")
        if isinstance(raw_slots, dict):
            slots = {str(key): value for key, value in raw_slots.items()}
        intents.append(
            Intent(
                action=action,
                slots=slots,
                confidence=confidence,
                evidence=str(item.get("evidence", "")),
            )
        )
    return IntentProposal(intents=tuple(intents), raw=utterance)


def _confidence(value: object) -> float:
    try:
        confidence = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as error:
        raise LlmUnavailableError(f"confidence 非法：{value!r}") from error
    if not 0.0 <= confidence <= 1.0:
        raise LlmUnavailableError(f"confidence 越界：{confidence}")
    return confidence
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/planning -v`
Expected: PASS（全部通过；本 Task 新增 6 项）

- [ ] **Step 5: 提交**

```bash
git add pyproject.toml src/ai_banking_copilot/planning/llm.py tests/planning/test_llm_real.py
git commit -m "feat: 建立真实 LLM 通道（可注入 + 懒加载 + 可选依赖 httpx）"
```

---

### Task 3: 锚点账本（意图可归责 · derived 深度约束）

**Files:**
- Create: `src/ai_banking_copilot/planning/anchors.py`
- Test: `tests/planning/test_anchors.py`

**Interfaces:**
- Consumes: 00 的 `contracts.plan.AnchorSource`
- Produces：
  - `DerivedDepthError(ValueError)` · `UnknownAnchorError(KeyError)`
  - `Anchor(anchor_id, source, depth, parent_id, detail)`（frozen）
  - `AnchorLedger()`（方法 `grant_explicit(detail) -> str` / `grant_approved(detail) -> str` / `derive(parent_id, detail) -> str` / `resolve(anchor_id) -> Anchor` / `anchors() -> tuple[Anchor, ...]`）

- [ ] **Step 1: 写失败的测试**

`tests/planning/test_anchors.py`

```python
import pytest

from ai_banking_copilot.contracts.plan import AnchorSource
from ai_banking_copilot.planning.anchors import (
    AnchorLedger,
    DerivedDepthError,
    UnknownAnchorError,
)


def test_grant_explicit_is_depth_zero_without_parent() -> None:
    ledger = AnchorLedger()
    anchor = ledger.resolve(ledger.grant_explicit("转给李娜"))
    assert anchor.source is AnchorSource.EXPLICIT
    assert anchor.depth == 0
    assert anchor.parent_id is None


def test_grant_approved_is_depth_zero() -> None:
    ledger = AnchorLedger()
    anchor = ledger.resolve(ledger.grant_approved("计划卡批准"))
    assert anchor.source is AnchorSource.USER_APPROVED
    assert anchor.depth == 0


def test_derive_from_explicit_has_depth_one() -> None:
    ledger = AnchorLedger()
    parent = ledger.grant_explicit("AA 总额 300 元")
    child = ledger.resolve(ledger.derive(parent, "均摊 300 / 3"))
    assert child.source is AnchorSource.DERIVED
    assert child.depth == 1
    assert child.parent_id == parent


def test_derive_from_approved_is_allowed() -> None:
    ledger = AnchorLedger()
    parent = ledger.grant_approved("计划卡批准总额 300 元")
    assert ledger.resolve(ledger.derive(parent, "均摊 300 / 3")).depth == 1


def test_derive_from_derived_is_rejected() -> None:
    ledger = AnchorLedger()
    parent = ledger.grant_explicit("AA 总额 300 元")
    child = ledger.derive(parent, "均摊 300 / 3")
    with pytest.raises(DerivedDepthError):
        ledger.derive(child, "再推导一层")


def test_resolve_unknown_anchor_is_rejected() -> None:
    with pytest.raises(UnknownAnchorError):
        AnchorLedger().resolve("a404")


def test_anchor_ids_are_deterministic_across_ledgers() -> None:
    first = AnchorLedger()
    second = AnchorLedger()
    assert first.grant_explicit("x") == second.grant_explicit("x") == "a1"
    assert first.grant_explicit("y") == "a2"


def test_anchors_returns_everything_in_insertion_order() -> None:
    ledger = AnchorLedger()
    ledger.grant_explicit("x")
    ledger.grant_approved("y")
    assert [anchor.anchor_id for anchor in ledger.anchors()] == ["a1", "a2"]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/planning/test_anchors.py -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'ai_banking_copilot.planning.anchors'`

- [ ] **Step 3: 最小实现**

`src/ai_banking_copilot/planning/anchors.py`

```python
"""意图锚点账本：`IntentRef` 的可信来源登记（H2 / PRD §5.3 第 3 条）。

锚点来源三分：
- `explicit`：用户本轮明说（深度 0）；
- `user_approved`：用户在计划卡批准（深度 0）；
- `derived`：**机械推导**（零自由度），**父必须 ∈ {explicit, user_approved}**，深度 ≤ 1，
  **禁止 `derived → derived` 链式**。

为什么"机械推导"必须被约束：一旦允许 `derived → derived`，"推断"就能演化成
"无需用户确认也能执行"的路径——这是 H2 的破口，故在账本层直接拒绝。
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import count

from ai_banking_copilot.contracts.plan import AnchorSource


class DerivedDepthError(ValueError):
    """`derived` 锚点违反深度约束（父为 `derived`，或深度会超过 1）。"""


class UnknownAnchorError(KeyError):
    """引用了未登记的锚点。"""


@dataclass(frozen=True)
class Anchor:
    """一条锚点记录。"""

    anchor_id: str
    source: AnchorSource
    depth: int
    parent_id: str | None
    detail: str


class AnchorLedger:
    """锚点账本：只登记、不猜测。id 由单调序号生成（确定性、可复现）。"""

    def __init__(self) -> None:
        self._anchors: dict[str, Anchor] = {}
        self._counter = count(1)

    def grant_explicit(self, detail: str) -> str:
        """登记用户本轮明说的锚点，返回其 id。"""
        return self._grant(AnchorSource.EXPLICIT, 0, None, detail)

    def grant_approved(self, detail: str) -> str:
        """登记用户在计划卡批准的锚点，返回其 id。"""
        return self._grant(AnchorSource.USER_APPROVED, 0, None, detail)

    def derive(self, parent_id: str, detail: str) -> str:
        """登记一次**机械推导**。父必须是深度 0 的 explicit / user_approved。"""
        parent = self.resolve(parent_id)
        if parent.source is AnchorSource.DERIVED or parent.depth >= 1:
            raise DerivedDepthError(
                f"derived 的父必须是 explicit/user_approved 且深度 ≤ 1：{parent_id} 不满足"
            )
        return self._grant(AnchorSource.DERIVED, parent.depth + 1, parent_id, detail)

    def resolve(self, anchor_id: str) -> Anchor:
        try:
            return self._anchors[anchor_id]
        except KeyError as error:
            raise UnknownAnchorError(f"未登记的锚点：{anchor_id}") from error

    def anchors(self) -> tuple[Anchor, ...]:
        """按登记顺序返回全部锚点（dict 保序）。"""
        return tuple(self._anchors.values())

    def _grant(self, source: AnchorSource, depth: int, parent_id: str | None, detail: str) -> str:
        anchor_id = f"a{next(self._counter)}"
        self._anchors[anchor_id] = Anchor(
            anchor_id=anchor_id,
            source=source,
            depth=depth,
            parent_id=parent_id,
            detail=detail,
        )
        return anchor_id
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/planning/test_anchors.py -v`
Expected: PASS（8 passed）

- [ ] **Step 5: 提交**

```bash
git add src/ai_banking_copilot/planning/anchors.py tests/planning/test_anchors.py
git commit -m "feat: 建立锚点账本（explicit/user_approved/derived + 深度 ≤ 1 禁链式）"
```

---

### Task 4: 控制槽补全（确定性解析器填槽 · unverified 禁入 · 多候选强制澄清）

**Files:**
- Create: `src/ai_banking_copilot/planning/slots.py`
- Create: `tests/planning/conftest.py`
- Test: `tests/planning/test_slots.py`

**Interfaces:**
- Consumes: 00 的 `contracts.taint`（`Trust` / `Value` / `Slot` / `SlotKind`）；01 的 `gateway.models.Payee`、`gateway.resolvers`（`resolve_payees` / `parse_amount` / `parse_temporal`）、`gateway.facts` / `gateway.seed` / `gateway.store`（测试 fixture）
- Produces：
  - `REQUIRED_SLOTS: Mapping[str, tuple[str, ...]]`（动作 → 必填控制槽）
  - `SLOT_PARAMS: Mapping[str, str]`（控制槽 → `PlanNode.params` 键名）
  - `SlotQuestion(slot, prompt, candidates=())`（frozen）· `SlotFill(slots={}, questions=(), trace=(), negated=False)`（frozen；属性 `complete`）
  - `SlotInputs(text, today, payees=(), selected={})`（frozen）
  - `fill_control_slots(action, inputs) -> SlotFill`

- [ ] **Step 1: 写失败的测试**

`tests/planning/conftest.py`

```python
from collections.abc import Iterator
from pathlib import Path

import pytest

from ai_banking_copilot.gateway.facts import FactGateway
from ai_banking_copilot.gateway.models import Payee
from ai_banking_copilot.gateway.seed import DEMO_USER_ID, seed_database
from ai_banking_copilot.gateway.store import Store, StoreMode, init_schema


@pytest.fixture
def gateway(tmp_path: Path) -> Iterator[FactGateway]:
    """建库 + 播种 + 只读事实网关（与 01 的 fixture 同构，避免跨测试目录依赖）。"""
    db_path = tmp_path / "planning.db"
    writer = Store(db_path)
    init_schema(writer)
    seed_database(writer)
    writer.close()
    reader = Store(db_path, StoreMode.READ_ONLY)
    yield FactGateway(reader)
    reader.close()


@pytest.fixture
def payees() -> tuple[Payee, ...]:
    """名册快照：含同名双候选（王伟 ×2）与唯一候选（李娜 / 张强）。"""
    return (
        Payee("p_wangwei_a", DEMO_USER_ID, "王伟", "8812", "3301", True, False),
        Payee("p_wangwei_b", DEMO_USER_ID, "王伟", "4417", "7788", False, False),
        Payee("p_lina", DEMO_USER_ID, "李娜", "9021", "2210", True, False),
        Payee("p_zhangqiang", DEMO_USER_ID, "张强", "6633", "5566", False, False),
    )
```

`tests/planning/test_slots.py`

```python
from datetime import date

from ai_banking_copilot.contracts.taint import SlotKind, Trust, Value
from ai_banking_copilot.gateway.models import Payee
from ai_banking_copilot.gateway.seed import DEMO_USER_ID
from ai_banking_copilot.planning.llm import ACTION_KINDS
from ai_banking_copilot.planning.slots import (
    REQUIRED_SLOTS,
    SLOT_PARAMS,
    SlotInputs,
    fill_control_slots,
)
from ai_banking_copilot.tools.catalog import build_default_registry

TODAY = date(2026, 10, 1)


def _transfer_inputs(
    text: str, payees: tuple[Payee, ...], account: object = "acc_savings"
) -> SlotInputs:
    return SlotInputs(
        text=text,
        today=TODAY,
        payees=payees,
        selected={"user_id": DEMO_USER_ID, "account": account},
    )


def test_transfer_slots_are_filled_from_deterministic_resolvers(payees: tuple[Payee, ...]) -> None:
    fill = fill_control_slots("transfer.create", _transfer_inputs("转给李娜 500 元", payees))
    assert fill.complete is True
    assert fill.negated is False
    assert fill.slots == {
        "user_id": DEMO_USER_ID,
        "account": "acc_savings",
        "payee": "p_lina",
        "amount": 50000,
        "when": "2026-10-01",
    }


def test_amount_without_unit_is_not_guessed(payees: tuple[Payee, ...]) -> None:
    fill = fill_control_slots("transfer.create", _transfer_inputs("转给李娜 500", payees))
    assert fill.complete is False
    assert [question.slot for question in fill.questions] == ["amount"]
    assert "payee" in fill.slots


def test_multiple_candidates_force_clarification_without_a_level(
    payees: tuple[Payee, ...],
) -> None:
    fill = fill_control_slots("transfer.create", _transfer_inputs("转给王伟 500 元", payees))
    assert fill.complete is False
    payee_question = next(item for item in fill.questions if item.slot == "payee")
    assert len(payee_question.candidates) == 2
    assert all("王伟" in label for label in payee_question.candidates)
    assert "payee" not in fill.slots


def test_unverified_value_never_enters_a_control_slot(payees: tuple[Payee, ...]) -> None:
    unverified = Value("acc_savings", Trust.UNVERIFIED)
    fill = fill_control_slots(
        "transfer.create", _transfer_inputs("转给李娜 500 元", payees, unverified)
    )
    assert "account" not in fill.slots
    assert any("rejected unverified:account" in note for note in fill.trace)
    assert any(question.slot == "account" for question in fill.questions)


def test_trusted_wrapped_value_is_accepted(payees: tuple[Payee, ...]) -> None:
    trusted = Value("acc_savings", Trust.TRUSTED)
    fill = fill_control_slots(
        "transfer.create", _transfer_inputs("转给李娜 500 元", payees, trusted)
    )
    assert fill.slots["account"] == "acc_savings"


def test_negation_blocks_slot_filling(payees: tuple[Payee, ...]) -> None:
    fill = fill_control_slots("transfer.create", _transfer_inputs("不要转给李娜 500 元", payees))
    assert fill.negated is True
    assert fill.complete is False
    assert fill.slots == {}


def test_bills_monthly_requires_a_trusted_user_id() -> None:
    """纯查询动作同样需要 `user_id`（02 目录声明；由受信任调用方经 `selected` 填入）。"""
    without_user = fill_control_slots("bills.monthly", SlotInputs(text="查一下上月账单", today=TODAY))
    assert without_user.complete is False
    assert [question.slot for question in without_user.questions] == ["user_id"]
    with_user = fill_control_slots(
        "bills.monthly",
        SlotInputs(text="查一下上月账单", today=TODAY, selected={"user_id": DEMO_USER_ID}),
    )
    assert with_user.complete is True
    assert with_user.slots == {"user_id": DEMO_USER_ID}


def test_required_slots_cover_every_canonical_action() -> None:
    assert set(REQUIRED_SLOTS) == set(ACTION_KINDS)


def test_slot_params_cover_every_referenced_slot() -> None:
    referenced = {slot for slots in REQUIRED_SLOTS.values() for slot in slots}
    assert referenced <= set(SLOT_PARAMS)


def test_required_slots_follow_the_catalog_required_control_slots() -> None:
    """02 声明必填 user_id 的动作，REQUIRED_SLOTS 必须一致含 user_id（README 裁决 #16）。"""
    registry = build_default_registry()
    requires_user_id = {
        action
        for action in registry.actions()
        if any(
            param.name == "user_id" and param.slot is SlotKind.CONTROL and param.required
            for param in registry.get(action).params
        )
    }
    assert requires_user_id == {
        "bills.monthly",
        "bills.category_trend",
        "card.list",
        "subscription.list",
        "card.apply_virtual",
        "transfer.create",
        "transfer.schedule",
        "wealth.subscribe",
        "wealth.redeem",
        "subscription.sign",
    }
    for action in requires_user_id:
        assert "user_id" in REQUIRED_SLOTS[action]
    assert REQUIRED_SLOTS["subscription.sign"] == (
        "user_id",
        "merchant",
        "amount_cents",
        "period",
        "next_charge_on",
    )
    assert "subscription_id" not in REQUIRED_SLOTS["subscription.sign"]


def test_signing_slots_are_filled_from_trusted_selection() -> None:
    """代扣签约的控制槽由受信任调用方经 `selected` 填入（对象是商户，不是订阅 id）。"""
    fill = fill_control_slots(
        "subscription.sign",
        SlotInputs(
            text="签约代扣",
            today=TODAY,
            selected={
                "user_id": DEMO_USER_ID,
                "merchant": "merchant_video",
                "amount_cents": 2500,
                "period": "monthly",
                "next_charge_on": "2026-11-01",
            },
        ),
    )
    assert fill.complete is True
    assert fill.slots == {
        "user_id": DEMO_USER_ID,
        "merchant": "merchant_video",
        "amount_cents": 2500,
        "period": "monthly",
        "next_charge_on": "2026-11-01",
    }


def test_cancel_subscription_is_not_blocked_by_its_own_trigger_word() -> None:
    fill = fill_control_slots(
        "subscription.cancel",
        SlotInputs(text="取消订阅", today=TODAY, selected={"subscription": "sub_video"}),
    )
    assert fill.negated is False
    assert fill.slots["subscription"] == "sub_video"


def test_refusing_to_cancel_is_still_blocked() -> None:
    fill = fill_control_slots(
        "subscription.cancel",
        SlotInputs(text="不要取消订阅", today=TODAY, selected={"subscription": "sub_video"}),
    )
    assert fill.negated is True
    assert fill.slots == {}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/planning/test_slots.py -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'ai_banking_copilot.planning.slots'`

- [ ] **Step 3: 最小实现**

`src/ai_banking_copilot/planning/slots.py`

```python
"""槽位补全：把意图的**控制槽**填成可信值；填不满就**追问**，绝不猜、不定级。

取值优先级（两条来源**都必须是可信的**，H1/H3）：
1. `selected`：受信任调用方（Web / IM 层）显式选定的对象 id；
2. 确定性解析器：`resolve_payees` / `parse_amount` / `parse_temporal`（01，零模型参与）；
3. 都不行 → 产出 `SlotQuestion`（含候选标签）。

三条铁律：
- **unverified 永不进控制槽**：取值为 `Trust.UNVERIFIED` 一律丢弃并转追问；
- **多候选收款人强制澄清**：候选 ≥ 2 时**不产出级别**、不自动择一（PRD §5.2）；
- **写入即类型把关**：每个控制槽取值都经 `contracts.taint.Slot(CONTROL, Value(TRUSTED))` 构造，
  用类型系统而非纪律保证"控制槽只见 trusted"。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date

from ai_banking_copilot.contracts.taint import Slot, SlotKind, Trust, Value
from ai_banking_copilot.gateway.models import Payee
from ai_banking_copilot.gateway.resolvers import (
    Candidate,
    parse_amount,
    parse_temporal,
    resolve_payees,
)

REQUIRED_SLOTS: Mapping[str, tuple[str, ...]] = {
    "account.balance": ("account",),
    "account.transactions": ("account",),
    "bills.monthly": ("user_id",),
    "bills.category_trend": ("user_id",),
    "wealth.list": (),
    "card.list": ("user_id",),
    "subscription.list": ("user_id",),
    "transfer.create": ("user_id", "account", "payee", "amount", "when"),
    "transfer.schedule": ("user_id", "account", "payee", "amount", "when"),
    "wealth.subscribe": ("user_id", "account", "product", "amount"),
    "wealth.redeem": ("user_id", "account", "product", "amount"),
    "subscription.cancel": ("subscription",),
    "subscription.sign": ("user_id", "merchant", "amount_cents", "period", "next_charge_on"),
    "card.lock": ("card",),
    "card.unlock": ("card",),
    "card.report_lost": ("card",),
    "card.apply_virtual": ("user_id", "daily_limit"),
    "card.set_limit": ("card", "daily_limit"),
    "funds.freeze": ("account", "amount"),
    "funds.release": ("hold",),
}
"""动作 → 必填控制槽（键与 `planning.llm.ACTION_KINDS` 的 20 个动作一一对应）。

**与 02 目录的控制槽一致性**：凡 02 目录声明为**必填 `user_id` 控制槽**的动作，此处必须含
`user_id`（纯查询动作亦然：`bills.monthly` / `bills.category_trend` / `card.list` /
`subscription.list`）；写动作（`transfer.create` / `transfer.schedule` / `wealth.subscribe` /
`wealth.redeem` / `subscription.sign` / `card.apply_virtual`）一并覆盖。`subscription.sign`
的对象是**商户**（不涉及已有订阅 id），故按 `merchant` / `amount_cents` / `period` /
`next_charge_on` 声明（README 裁决 #17）；`user_id` 由**受信任调用方**经 `selected` 填入
（会话层与评测链装配 `DEMO_USER_ID`）。
"""

SLOT_PARAMS: Mapping[str, str] = {
    "account": "account_id",
    "payee": "payee_id",
    "amount": "amount_cents",
    "when": "booked_on",
    "product": "product_id",
    "subscription": "subscription_id",
    "card": "card_id",
    "hold": "hold_id",
    "daily_limit": "daily_limit_cents",
    "user_id": "user_id",
    "merchant": "merchant",
    "amount_cents": "amount_cents",
    "period": "period",
    "next_charge_on": "next_charge_on",
}
"""控制槽 → `PlanNode.params` 键名。"""

_PROMPTS: Mapping[str, str] = {
    "account": "请选择要操作的账户。",
    "payee": "请确认收款人（需要全名 / 账号尾号 / 手机尾号）。",
    "amount": "请给出金额（必须带单位，例如 500 元）。",
    "when": "请给出执行时间（例如 明天 / 2026-12-25）。",
    "product": "请选择理财产品。",
    "subscription": "请选择要处理的订阅。",
    "card": "请选择要处理的卡片。",
    "daily_limit": "请给出额度（必须带单位，例如 2 万元）。",
    "hold": "请选择要解冻的冻结记录。",
    "user_id": "请确认要操作的用户身份（由受信任调用方提供）。",
    "merchant": "请给出代扣签约的商户名。",
    "amount_cents": "请给出代扣金额（单位：分）。",
    "period": "请给出代扣周期（monthly / yearly）。",
    "next_charge_on": "请给出下次扣款日（例如 2026-11-01）。",
}


@dataclass(frozen=True)
class SlotQuestion:
    """一次澄清请求（尚未成型的控制槽）。"""

    slot: str
    prompt: str
    candidates: tuple[str, ...] = ()


@dataclass(frozen=True)
class SlotFill:
    """一个意图的补全结果：已填的可信控制槽 + 待澄清项 + 解析轨迹。"""

    slots: Mapping[str, object] = field(default_factory=dict)
    questions: tuple[SlotQuestion, ...] = ()
    trace: tuple[str, ...] = ()
    negated: bool = False

    @property
    def complete(self) -> bool:
        """是否已补齐全部必填控制槽，且未被否定词拦截。"""
        return not self.questions and not self.negated


@dataclass(frozen=True)
class SlotInputs:
    """补全输入：文本 + 受信任上下文（日期 / 名册 / 已选对象）。"""

    text: str
    today: date
    payees: Sequence[Payee] = ()
    selected: Mapping[str, object] = field(default_factory=dict)


def fill_control_slots(action: str, inputs: SlotInputs) -> SlotFill:
    """按 `REQUIRED_SLOTS[action]` 逐个补槽；返回已填槽与待澄清项。

    `negation=True` 只可能来自真正的拒绝表达（"不要 / 别 / 拒绝"等）：01 的 `parse_temporal`
    已按 README 裁决 §11 把「取消」移出否定词表，故「取消订阅」这类合法意图不会被拦截。
    """
    parsed = parse_temporal(inputs.text, inputs.today)
    if parsed.negation:
        return SlotFill(trace=(f"negation detected:{parsed.evidence}",), negated=True)

    filled: dict[str, object] = {}
    questions: list[SlotQuestion] = []
    trace: list[str] = []
    for slot in REQUIRED_SLOTS.get(action, ()):
        chosen, question, notes = _resolve_slot(slot, inputs)
        trace.extend(notes)
        if chosen is not None:
            filled[slot] = _accept(slot, chosen)
        elif question is not None:
            questions.append(question)
    return SlotFill(slots=filled, questions=tuple(questions), trace=tuple(trace))


def _accept(slot: str, value: object) -> object:
    """把可信值写入控制槽：经 `contracts.taint.Slot` 构造做**类型层**把关。"""
    Slot(kind=SlotKind.CONTROL, value=Value(value, Trust.TRUSTED))
    return value


def _resolve_slot(
    slot: str, inputs: SlotInputs
) -> tuple[object | None, SlotQuestion | None, tuple[str, ...]]:
    notes: list[str] = []
    if slot in inputs.selected:
        raw = inputs.selected[slot]
        if _is_trusted(raw):
            notes.append(f"selected:{slot}")
            return (raw.raw if isinstance(raw, Value) else raw), None, tuple(notes)
        notes.append(f"rejected unverified:{slot}")
    if slot == "payee":
        return _resolve_payee(inputs, notes)
    if slot == "amount":
        return _resolve_amount(inputs, notes)
    if slot == "when":
        return _resolve_when(inputs, notes)
    return None, SlotQuestion(slot=slot, prompt=_PROMPTS[slot]), tuple(notes)


def _is_trusted(value: object) -> bool:
    if isinstance(value, Value):
        return value.trust is Trust.TRUSTED
    return True


def _resolve_payee(
    inputs: SlotInputs, notes: list[str]
) -> tuple[object | None, SlotQuestion | None, tuple[str, ...]]:
    resolution = resolve_payees(inputs.payees, inputs.text)
    if resolution.needs_clarification:
        labels = tuple(_payee_label(item) for item in resolution.candidates)
        notes.append(f"clarify payee:{','.join(labels)}")
        return None, SlotQuestion("payee", _PROMPTS["payee"], labels), tuple(notes)
    if not resolution.candidates:
        notes.append("missing payee")
        return None, SlotQuestion("payee", _PROMPTS["payee"]), tuple(notes)
    chosen = resolution.candidates[0]
    notes.append(f"resolved payee:{chosen.payee_id}")
    return chosen.payee_id, None, tuple(notes)


def _resolve_amount(
    inputs: SlotInputs, notes: list[str]
) -> tuple[object | None, SlotQuestion | None, tuple[str, ...]]:
    parsed = parse_amount(inputs.text)
    if not parsed.matched or parsed.value is None:
        notes.append("missing amount")
        return None, SlotQuestion("amount", _PROMPTS["amount"]), tuple(notes)
    notes.append(f"resolved amount:{parsed.value.cents}")
    return parsed.value.cents, None, tuple(notes)


def _resolve_when(
    inputs: SlotInputs, notes: list[str]
) -> tuple[object | None, SlotQuestion | None, tuple[str, ...]]:
    parsed = parse_temporal(inputs.text, inputs.today)
    if parsed.matched and parsed.when is not None:
        notes.append(f"resolved when:{parsed.when.isoformat()}")
        return parsed.when.isoformat(), None, tuple(notes)
    notes.append(f"default when:{inputs.today.isoformat()}")
    return inputs.today.isoformat(), None, tuple(notes)


def _payee_label(candidate: Candidate) -> str:
    return f"{candidate.full_name}(账号尾号 {candidate.account_tail} / 手机尾号 {candidate.phone_tail})"
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/planning/test_slots.py -v`
Expected: PASS（13 passed）

- [ ] **Step 5: 提交**

```bash
git add src/ai_banking_copilot/planning/slots.py tests/planning/conftest.py tests/planning/test_slots.py
git commit -m "feat: 建立控制槽补全（确定性解析器填槽 + unverified 禁入 + 多候选强制澄清）"
```

---

### Task 5: 计划级净效应（带符号资金流 · 防多步组合越权）

**Files:**
- Create: `src/ai_banking_copilot/planning/net_effect.py`
- Test: `tests/planning/test_net_effect.py`

**Interfaces:**
- Consumes: 00 的 `contracts.plan.Plan`；01 的 `gateway.facts.FactGateway`、`gateway.models.Money`；Task 1 的 `llm.ACTION_KINDS` / `llm.ActionKind`
- Produces：`NetEffectError(ValueError)` · `NetEffect(amount, irreversibles, node_ids)`（frozen）· `net_effect(plan, gateway) -> NetEffect`

- [ ] **Step 1: 写失败的测试**

`tests/planning/test_net_effect.py`

```python
import pytest

from ai_banking_copilot.contracts.plan import AnchorSource, IntentRef, Plan, PlanNode
from ai_banking_copilot.gateway.facts import FactGateway
from ai_banking_copilot.gateway.models import Money
from ai_banking_copilot.planning.net_effect import NetEffectError, net_effect


def _node(node_id: str, action: str, params: dict[str, object] | None = None) -> PlanNode:
    return PlanNode(
        node_id=node_id,
        action=action,
        params={} if params is None else params,
        intent=IntentRef(source=AnchorSource.EXPLICIT, anchor_id=f"a_{node_id}"),
    )


def _plan(*nodes: PlanNode) -> Plan:
    return Plan(plan_id="p1", nodes=nodes)


def test_transfer_and_freeze_are_positive_outflows(gateway: FactGateway) -> None:
    plan = _plan(
        _node("n1", "transfer.create", {"amount_cents": 50000}),
        _node("n2", "funds.freeze", {"amount_cents": 100000}),
    )
    result = net_effect(plan, gateway)
    assert result.amount == Money(150000)
    assert result.irreversibles == ()
    assert result.node_ids == ("n1", "n2")


def test_cancel_subscription_subtracts_the_future_outflow(gateway: FactGateway) -> None:
    plan = _plan(_node("n1", "subscription.cancel", {"subscription_id": "sub_video"}))
    assert net_effect(plan, gateway).amount == Money(-2500)


def test_redeem_is_a_negative_outflow(gateway: FactGateway) -> None:
    plan = _plan(_node("n1", "wealth.redeem", {"amount_cents": 200000}))
    assert net_effect(plan, gateway).amount == Money(-200000)


def test_query_and_state_change_contribute_zero(gateway: FactGateway) -> None:
    plan = _plan(_node("n1", "bills.monthly"), _node("n2", "card.lock"))
    result = net_effect(plan, gateway)
    assert result.amount == Money(0)
    assert result.node_ids == ("n1", "n2")


def test_report_lost_is_listed_as_irreversible_and_excluded_from_cf(gateway: FactGateway) -> None:
    plan = _plan(
        _node("n1", "transfer.create", {"amount_cents": 50000}),
        _node("n2", "card.report_lost"),
    )
    result = net_effect(plan, gateway)
    assert result.amount == Money(50000)
    assert result.irreversibles == ("n2",)


def test_unknown_action_is_rejected(gateway: FactGateway) -> None:
    with pytest.raises(NetEffectError, match="未知动作"):
        net_effect(_plan(_node("n1", "exec_shell")), gateway)


def test_missing_amount_is_rejected(gateway: FactGateway) -> None:
    with pytest.raises(NetEffectError, match="amount_cents"):
        net_effect(_plan(_node("n1", "transfer.create")), gateway)


def test_missing_subscription_id_is_rejected(gateway: FactGateway) -> None:
    with pytest.raises(NetEffectError, match="subscription_id"):
        net_effect(_plan(_node("n1", "subscription.cancel")), gateway)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/planning/test_net_effect.py -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'ai_banking_copilot.planning.net_effect'`

- [ ] **Step 3: 最小实现**

`src/ai_banking_copilot/planning/net_effect.py`

```python
"""计划级净效应（NE）：单步合法 ≠ 整体正确（PRD §5.3 第 1 条）。

`cf` 口径（**带符号资金流，以"流出为正"**）：
- 转账 / 定时转账 / 理财申购 / 代扣签约 / 自动续费 / 冻结资金 = `+流出`；
- 理财赎回 / 解冻 = `−流出`；
- 订阅取消 / 解约 = `−未来流出`（未来流出记负）；
- 挂失 = **不可逆**，**不入 cf**，单列进 `irreversibles`（触发 ≥ 红色 + L3）；
- 纯查询 / 非不可逆状态变更 = `0`。

计划级阈值（`NE ≥ T_net ⇒ 计划级 ≥ L2`）由 `policy.pdp::evaluate_plan` 使用，本模块只负责算数值。

**fail-safe 取数**：未知动作、缺 `amount_cents`、取消订阅缺 `subscription_id` 一律**拒绝计算**
（宁可报错，也不把"算不出的流出"当成 0）。
"""

from __future__ import annotations

from dataclasses import dataclass

from ai_banking_copilot.contracts.plan import Plan, PlanNode
from ai_banking_copilot.gateway.facts import FactGateway
from ai_banking_copilot.gateway.models import Money
from ai_banking_copilot.planning.llm import ACTION_KINDS, ActionKind


class NetEffectError(ValueError):
    """节点缺少可计算的资金流参数，或动作不在规范词表内。"""


@dataclass(frozen=True)
class NetEffect:
    """计划级净效应。`amount` 为**净流出**（正 = 净流出）。"""

    amount: Money
    irreversibles: tuple[str, ...]
    node_ids: tuple[str, ...]


def net_effect(plan: Plan, gateway: FactGateway) -> NetEffect:
    """按 `ACTION_KINDS` 汇总全计划带符号资金流。"""
    total = 0
    irreversibles: list[str] = []
    node_ids: list[str] = []
    for node in plan.nodes:
        node_ids.append(node.node_id)
        kind = ACTION_KINDS.get(node.action)
        if kind is None:
            raise NetEffectError(f"未知动作 {node.action}，拒绝计算净效应")
        if kind is ActionKind.IRREVERSIBLE:
            irreversibles.append(node.node_id)
            continue
        if kind in (ActionKind.QUERY, ActionKind.STATE):
            continue
        if kind is ActionKind.CANCEL_OUTFLOW:
            total -= _subscription_cents(node, gateway)
            continue
        cents = _amount_cents(node)
        total += cents if kind is ActionKind.OUTFLOW else -cents
    return NetEffect(
        amount=Money(total),
        irreversibles=tuple(irreversibles),
        node_ids=tuple(node_ids),
    )


def _amount_cents(node: PlanNode) -> int:
    raw = node.params.get("amount_cents")
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise NetEffectError(f"节点 {node.node_id} 缺少整数 amount_cents")
    return raw


def _subscription_cents(node: PlanNode, gateway: FactGateway) -> int:
    raw = node.params.get("subscription_id")
    if not isinstance(raw, str) or not raw:
        raise NetEffectError(f"节点 {node.node_id}（订阅取消）缺少 subscription_id")
    return gateway.subscription(raw).payload.amount.cents
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/planning/test_net_effect.py -v`
Expected: PASS（8 passed）

- [ ] **Step 5: 提交**

```bash
git add src/ai_banking_copilot/planning/net_effect.py tests/planning/test_net_effect.py
git commit -m "feat: 建立计划级净效应（带符号资金流 + 挂失单列不可逆 + fail-safe 取数）"
```

---

### Task 6: 规划内核（多意图拆解 → 槽位补全 → DAG → 自检）

**Files:**
- Create: `src/ai_banking_copilot/planning/kernel.py`
- Modify: `tests/planning/conftest.py`（追加 `StubRegistry` 与 `stub_registry` fixture）
- Test: `tests/planning/test_kernel.py`

**Interfaces:**
- Consumes: 00 的 `contracts.plan`（`AnchorSource` / `IntentRef` / `Plan` / `PlanNode` / `validate_plan` / `cfhash`）；01 的 `gateway.models.Payee`；Task 1 的 `llm.Intent` / `llm.IntentModel`；Task 3 的 `anchors.AnchorLedger`；Task 4 的 `slots.SLOT_PARAMS` / `slots.SlotInputs` / `slots.fill_control_slots`；02 的 `tools.registry::ToolRegistry`（**结构上**满足 `ActionCatalog`）
- Produces：
  - `ActionCatalog`（`Protocol`，`actions() -> Iterable[str]`）
  - `Question(slot, prompt, candidates=())`（frozen）
  - `PlanningResult(plan, questions, blocked, intents, rationale)`（frozen）
  - `Kernel(llm, registry)`（方法 `plan(user_id, utterance, context) -> PlanningResult`）
  - `CONFIDENCE_THRESHOLD: float = 0.5` · `CONTEXT_PAYEES = "payees"` · `CONTEXT_TODAY = "today"` · `CONTEXT_SELECTED = "selected"`

- [ ] **Step 1: 写失败的测试**

`tests/planning/conftest.py`（顶部导入段补 `from ai_banking_copilot.planning.llm import CANONICAL_ACTIONS`，并在文件末尾追加）

```python
class StubRegistry:
    """02 未落地前的替身：内核只用到 `actions()`，`ToolRegistry` 结构上即满足。"""

    def __init__(self, actions: tuple[str, ...]) -> None:
        self._actions = actions

    def actions(self) -> tuple[str, ...]:
        return self._actions


@pytest.fixture
def stub_registry() -> StubRegistry:
    return StubRegistry(CANONICAL_ACTIONS)
```

`tests/planning/test_kernel.py`

```python
from datetime import date

from ai_banking_copilot.contracts.plan import AnchorSource, cfhash, validate_plan
from ai_banking_copilot.gateway.models import Payee
from ai_banking_copilot.planning.kernel import Kernel, PlanningResult
from ai_banking_copilot.planning.llm import TemplateIntentModel

TODAY = date(2026, 10, 1)


def _context(
    payees: tuple[Payee, ...], selected: dict[str, object] | None = None
) -> dict[str, object]:
    default_selected: dict[str, object] = {"user_id": "u_demo", "account": "acc_savings"}
    return {
        "today": TODAY,
        "payees": payees,
        "selected": default_selected if selected is None else selected,
    }


def _kernel(registry: object) -> Kernel:
    return Kernel(TemplateIntentModel(), registry)  # type: ignore[arg-type]


def test_single_transfer_produces_one_valid_node(
    payees: tuple[Payee, ...], stub_registry: object
) -> None:
    result = _kernel(stub_registry).plan("u_demo", "转给李娜 500 元", _context(payees))
    assert isinstance(result, PlanningResult)
    assert result.blocked is False
    assert result.questions == ()
    validate_plan(result.plan)
    node = result.plan.nodes[0]
    assert node.action == "transfer.create"
    assert node.params["user_id"] == "u_demo"
    assert node.params["payee_id"] == "p_lina"
    assert node.params["amount_cents"] == 50000
    assert node.params["account_id"] == "acc_savings"
    assert node.intent is not None
    assert node.intent.source is AnchorSource.EXPLICIT


def test_multi_intent_on_the_same_resource_is_serialised_into_a_dag(
    payees: tuple[Payee, ...], stub_registry: object
) -> None:
    result = _kernel(stub_registry).plan(
        "u_demo", "转给李娜 500 元，转给张强 300 元", _context(payees)
    )
    validate_plan(result.plan)
    assert [node.node_id for node in result.plan.nodes] == ["n1", "n2"]
    assert result.plan.nodes[1].depends_on == ("n1",)
    assert "机械推导锚" in result.rationale


def test_query_node_does_not_chain_behind_other_nodes(
    payees: tuple[Payee, ...], stub_registry: object
) -> None:
    result = _kernel(stub_registry).plan(
        "u_demo", "查一下上月账单，然后转给李娜 500 元", _context(payees)
    )
    validate_plan(result.plan)
    assert [node.action for node in result.plan.nodes] == ["bills.monthly", "transfer.create"]
    assert result.plan.nodes[1].depends_on == ()


def test_clarification_yields_questions_and_no_node(
    payees: tuple[Payee, ...], stub_registry: object
) -> None:
    result = _kernel(stub_registry).plan("u_demo", "转给王伟 500 元", _context(payees))
    assert result.blocked is True
    assert [question.slot for question in result.questions] == ["payee"]
    assert result.plan.nodes == ()


def test_rationale_exposes_the_frozen_plan_hash(
    payees: tuple[Payee, ...], stub_registry: object
) -> None:
    result = _kernel(stub_registry).plan("u_demo", "转给李娜 500 元", _context(payees))
    assert f"cfhash={cfhash(result.plan.nodes)}" in result.rationale


def test_unknown_utterance_produces_an_empty_plan(
    payees: tuple[Payee, ...], stub_registry: object
) -> None:
    result = _kernel(stub_registry).plan("u_demo", "今天天气不错", _context(payees))
    assert result.plan.nodes == ()
    assert result.blocked is True
    assert result.intents == ()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/planning/test_kernel.py -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'ai_banking_copilot.planning.kernel'`

- [ ] **Step 3: 最小实现**

`src/ai_banking_copilot/planning/kernel.py`

```python
"""规划内核：多意图拆解 → 槽位补全 → DAG 规划 → 自检（H2 / H7）。

本模块**只产计划**：
- **不执行动作**（H2）：执行只能由 `policy.pep` 在 `policy.pdp` 裁决后进行；
- **不产出授权级别**：级别由 `policy.pdp` 决定；多候选收款人只澄清、不定级；
- **不产事实**（H1）：数值与实体只来自 `slots`（即 01 的确定性解析器）与受信任 `context`。

三条纪律：
1. **统一出口**：所有节点都由本模块组装，最后 `validate_plan` 自检 + `cfhash` 冻结；
2. **锚点**：节点锚点只可能是 `explicit`（用户本轮明说）；`derived` 锚点只用于**机械推导的
   记录**（如"依赖关系由冲突检测机械推导"），**绝不作为节点锚点**（`validate_plan` 节点级禁用）；
3. **解析文本可回指**：槽位解析所用的文本必须是**用户原文的子串**——LLM 伪造的片段直接作废（H3）。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import date
from typing import Protocol

from ai_banking_copilot.contracts.plan import (
    AnchorSource,
    IntentRef,
    Plan,
    PlanNode,
    cfhash,
    validate_plan,
)
from ai_banking_copilot.gateway.models import Payee
from ai_banking_copilot.planning.anchors import AnchorLedger
from ai_banking_copilot.planning.llm import Intent, IntentModel
from ai_banking_copilot.planning.slots import SLOT_PARAMS, SlotInputs, fill_control_slots

CONFIDENCE_THRESHOLD = 0.5
"""低于该置信度的意图直接丢弃（PRD §5.7：低置信度不猜）。"""

CONTEXT_PAYEES = "payees"
CONTEXT_TODAY = "today"
CONTEXT_SELECTED = "selected"

_RESOURCE_PARAM: Mapping[str, str] = {
    "transfer.create": "account_id",
    "transfer.schedule": "account_id",
    "account.balance": "account_id",
    "account.transactions": "account_id",
    "wealth.subscribe": "account_id",
    "wealth.redeem": "account_id",
    "funds.freeze": "account_id",
    "subscription.cancel": "subscription_id",
    "subscription.sign": "merchant",
    "card.lock": "card_id",
    "card.unlock": "card_id",
    "card.report_lost": "card_id",
    "card.set_limit": "card_id",
    "funds.release": "hold_id",
}
"""动作 → 资源键参数：**同资源串行、异资源并行**（冲突与资源检测）；只列带资源键的动作。"""


class ActionCatalog(Protocol):
    """内核只依赖"已知动作清单"这一最小面；`tools.registry.ToolRegistry` 结构上即满足。"""

    def actions(self) -> Iterable[str]:
        """返回全部已注册动作名。"""
        ...


@dataclass(frozen=True)
class Question:
    """一次澄清请求：缺哪个控制槽、问什么、可选候选。"""

    slot: str
    prompt: str
    candidates: tuple[str, ...] = ()


@dataclass(frozen=True)
class PlanningResult:
    """规划产出。`blocked=True` 表示**没有任何可执行节点**（未知意图或待澄清）。"""

    plan: Plan
    questions: tuple[Question, ...]
    blocked: bool
    intents: tuple[Intent, ...]
    rationale: str


class Kernel:
    """规划内核：`Kernel(llm, registry)`。"""

    def __init__(self, llm: IntentModel, registry: ActionCatalog) -> None:
        self._llm = llm
        self._registry = registry

    def plan(self, user_id: str, utterance: str, context: Mapping[str, object]) -> PlanningResult:
        """把一句话规划成带锚点的 DAG；补不齐的控制槽转追问。"""
        proposal = self._llm.propose(utterance, context)
        known = set(self._registry.actions())
        ledger = AnchorLedger()
        today = _context_today(context)
        payees = _context_payees(context)
        selected = _context_selected(context)

        nodes: list[PlanNode] = []
        questions: list[Question] = []
        notes: list[str] = []
        accepted: list[Intent] = []

        for index, intent in enumerate(proposal.intents, start=1):
            if intent.confidence < CONFIDENCE_THRESHOLD:
                notes.append(f"丢弃低置信度意图 {intent.action}（{intent.confidence:.2f}）")
                continue
            if intent.action not in known:
                notes.append(f"丢弃未注册动作 {intent.action}")
                continue
            accepted.append(intent)
            fill = fill_control_slots(
                intent.action,
                SlotInputs(
                    text=_resolution_text(intent, utterance),
                    today=today,
                    payees=payees,
                    selected=selected,
                ),
            )
            if fill.negated:
                notes.append(f"{intent.action} 命中否定表达，不生成节点")
                continue
            questions.extend(
                Question(slot=item.slot, prompt=item.prompt, candidates=item.candidates)
                for item in fill.questions
            )
            if not fill.complete:
                continue
            anchor_id = ledger.grant_explicit(f"用户本轮明说：{intent.evidence or intent.action}")
            nodes.append(
                PlanNode(
                    node_id=f"n{index}",
                    action=intent.action,
                    params=_params_from_slots(fill.slots),
                    intent=IntentRef(source=AnchorSource.EXPLICIT, anchor_id=anchor_id),
                )
            )

        wired = _wire_dependencies(tuple(nodes), ledger, notes)
        digest = cfhash(wired)
        plan = Plan(plan_id=f"plan_{digest[:12]}", nodes=wired)
        validate_plan(plan)
        blocked = not wired
        if blocked and not questions:
            notes.append("未知意图：安全拒绝（L4），不猜测执行")
        rationale = "；".join([f"user={user_id}", *notes, f"cfhash={digest}"])
        return PlanningResult(
            plan=plan,
            questions=tuple(questions),
            blocked=blocked,
            intents=tuple(accepted),
            rationale=rationale,
        )


def _params_from_slots(slots: Mapping[str, object]) -> dict[str, object]:
    return {SLOT_PARAMS[slot]: value for slot, value in slots.items()}


def _wire_dependencies(
    nodes: tuple[PlanNode, ...], ledger: AnchorLedger, notes: list[str]
) -> tuple[PlanNode, ...]:
    """同资源串行：把机械推导出的依赖记入锚点账本，作为可审计的推导依据。"""
    last_by_resource: dict[str, str] = {}
    wired: list[PlanNode] = []
    for node in nodes:
        resource = _resource_key(node)
        dependency = last_by_resource.get(resource) if resource is not None else None
        if dependency is None:
            wired.append(node)
        else:
            derived_id = ledger.derive(_anchor_id(node), f"{node.node_id} 机械依赖 {dependency}")
            notes.append(f"{node.node_id} 依赖 {dependency}（机械推导锚 {derived_id}）")
            wired.append(replace(node, depends_on=(dependency,)))
        if resource is not None:
            last_by_resource[resource] = node.node_id
    return tuple(wired)


def _resource_key(node: PlanNode) -> str | None:
    param = _RESOURCE_PARAM.get(node.action)
    if param is None:
        return None
    value = node.params.get(param)
    if isinstance(value, str) and value:
        return f"{param}:{value}"
    return None


def _anchor_id(node: PlanNode) -> str:
    if node.intent is None:
        raise ValueError(f"节点 {node.node_id} 缺少意图锚点")
    return node.intent.anchor_id


def _resolution_text(intent: Intent, utterance: str) -> str:
    """解析文本必须**可回指到用户原文**：LLM 伪造的片段直接丢弃（H3）。"""
    span = intent.slots.get("span")
    if isinstance(span, str) and span and span in utterance:
        return span
    return utterance


def _context_today(context: Mapping[str, object]) -> date:
    value = context.get(CONTEXT_TODAY)
    if isinstance(value, date):
        return value
    raise ValueError("context 缺少受信任的 today: date")


def _context_payees(context: Mapping[str, object]) -> tuple[Payee, ...]:
    value = context.get(CONTEXT_PAYEES)
    if value is None:
        return ()
    if isinstance(value, (list, tuple)):
        return tuple(item for item in value if isinstance(item, Payee))
    raise ValueError("context.payees 必须是 Payee 序列")


def _context_selected(context: Mapping[str, object]) -> Mapping[str, object]:
    value = context.get(CONTEXT_SELECTED)
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    raise ValueError("context.selected 必须是映射")
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/planning -v`
Expected: PASS（全部通过；本 Task 新增 6 项）

- [ ] **Step 5: 提交**

```bash
git add src/ai_banking_copilot/planning/kernel.py tests/planning
git commit -m "feat: 建立规划内核（多意图拆解 + 槽位补全 + 同资源串行 DAG + 自检）"
```

---

### Task 7: 安全边界与降级（未知意图安全拒绝 · 伪造片段作废 · LLM 降级通道）

**Files:**
- Modify: `src/ai_banking_copilot/planning/llm.py`（在文件末尾追加 `FallbackIntentModel`）
- Test: `tests/planning/test_kernel_boundaries.py`

**Interfaces:**
- Consumes: Task 1 的 `Intent` / `IntentProposal` / `IntentModel` / `TemplateIntentModel`；Task 2 的 `LlmUnavailableError`；Task 6 的 `Kernel`
- Produces：`FallbackIntentModel(primary, fallback=None)`（属性 `degraded: bool`；方法 `propose`）

- [ ] **Step 1: 写失败的测试**

`tests/planning/test_kernel_boundaries.py`

```python
import json
from datetime import date

from ai_banking_copilot.gateway.models import Payee
from ai_banking_copilot.planning.kernel import Kernel
from ai_banking_copilot.planning.llm import (
    FallbackIntentModel,
    Intent,
    IntentModel,
    IntentProposal,
    LlmIntentModel,
    LlmUnavailableError,
    TemplateIntentModel,
)

TODAY = date(2026, 10, 1)


class _BrokenIntentModel:
    """模拟上游 LLM 不可用。"""

    def propose(self, utterance: str, context: object) -> IntentProposal:
        raise LlmUnavailableError("上游不可用")


class _LowConfidenceIntentModel:
    def propose(self, utterance: str, context: object) -> IntentProposal:
        return IntentProposal(
            intents=(Intent(action="transfer.create", confidence=0.1, evidence="maybe"),),
            raw=utterance,
        )


class _SpanForgingIntentModel:
    """伪造解析片段：片段不在用户原文里，必须被作废。"""

    def propose(self, utterance: str, context: object) -> IntentProposal:
        return IntentProposal(
            intents=(
                Intent(
                    action="transfer.create",
                    slots={"span": "转给李娜 9999 元"},
                    confidence=0.9,
                    evidence="伪造片段",
                ),
            ),
            raw=utterance,
        )


def _context(payees: tuple[Payee, ...]) -> dict[str, object]:
    return {
        "today": TODAY,
        "payees": payees,
        "selected": {"user_id": "u_demo", "account": "acc_savings"},
    }


def test_unknown_utterance_is_safely_blocked(
    payees: tuple[Payee, ...], stub_registry: object
) -> None:
    kernel = Kernel(TemplateIntentModel(), stub_registry)  # type: ignore[arg-type]
    result = kernel.plan("u_demo", "帮我做点什么", _context(payees))
    assert result.blocked is True
    assert result.plan.nodes == ()
    assert "安全拒绝" in result.rationale


def test_negated_instruction_does_not_produce_a_node(
    payees: tuple[Payee, ...], stub_registry: object
) -> None:
    kernel = Kernel(TemplateIntentModel(), stub_registry)  # type: ignore[arg-type]
    result = kernel.plan("u_demo", "不要转给李娜 500 元", _context(payees))
    assert result.plan.nodes == ()
    assert result.blocked is True


def test_low_confidence_intent_is_dropped(
    payees: tuple[Payee, ...], stub_registry: object
) -> None:
    kernel = Kernel(_LowConfidenceIntentModel(), stub_registry)  # type: ignore[arg-type]
    result = kernel.plan("u_demo", "转给李娜 500 元", _context(payees))
    assert result.plan.nodes == ()
    assert result.intents == ()
    assert "低置信度" in result.rationale


def test_forged_span_is_not_trusted(payees: tuple[Payee, ...], stub_registry: object) -> None:
    kernel = Kernel(_SpanForgingIntentModel(), stub_registry)  # type: ignore[arg-type]
    result = kernel.plan("u_demo", "查一下上月账单", _context(payees))
    assert result.plan.nodes == ()
    assert [question.slot for question in result.questions] == ["payee", "amount"]


def test_fallback_model_degrades_when_primary_is_unavailable() -> None:
    model = FallbackIntentModel(_BrokenIntentModel())
    proposal = model.propose("转给李娜 500 元", {})
    assert [intent.action for intent in proposal.intents] == ["transfer.create"]
    assert model.degraded is True


def test_fallback_model_stays_healthy_when_primary_works() -> None:
    def _transport(request: str) -> str:
        return json.dumps({"intents": [{"action": "bills.monthly", "confidence": 0.7}]})

    model = FallbackIntentModel(LlmIntentModel(transport=_transport))
    proposal = model.propose("看下账单", {})
    assert [intent.action for intent in proposal.intents] == ["bills.monthly"]
    assert model.degraded is False


def test_kernel_uses_the_fallback_channel_end_to_end(
    payees: tuple[Payee, ...], stub_registry: object
) -> None:
    model = FallbackIntentModel(_BrokenIntentModel())
    kernel = Kernel(model, stub_registry)  # type: ignore[arg-type]
    result = kernel.plan("u_demo", "转给李娜 500 元", _context(payees))
    assert [node.action for node in result.plan.nodes] == ["transfer.create"]
    assert result.blocked is False


def test_fallback_model_satisfies_the_intent_model_protocol() -> None:
    assert isinstance(FallbackIntentModel(TemplateIntentModel()), IntentModel)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/planning/test_kernel_boundaries.py -v`
Expected: FAIL —— `ImportError: cannot import name 'FallbackIntentModel' from 'ai_banking_copilot.planning.llm'`

- [ ] **Step 3: 最小实现**

`src/ai_banking_copilot/planning/llm.py`（在文件末尾追加）

```python
class FallbackIntentModel:
    """降级组合：主通道不可用即回落模板通道。

    H9 声明：模板通道是**能力子集**（可靠性兜底），**不是等价实现**——
    它只覆盖规范动作的主路径，不处理长尾口语。
    """

    def __init__(self, primary: IntentModel, fallback: IntentModel | None = None) -> None:
        self._primary = primary
        self._fallback = fallback if fallback is not None else TemplateIntentModel()
        self._degraded = False

    @property
    def degraded(self) -> bool:
        """最近一次调用是否发生了降级。"""
        return self._degraded

    def propose(self, utterance: str, context: Mapping[str, object]) -> IntentProposal:
        try:
            proposal = self._primary.propose(utterance, context)
        except LlmUnavailableError:
            self._degraded = True
            return self._fallback.propose(utterance, context)
        self._degraded = False
        return proposal
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/planning -v`
Expected: PASS（全部通过；本 Task 新增 8 项）

- [ ] **Step 5: 提交**

```bash
git add src/ai_banking_copilot/planning/llm.py tests/planning/test_kernel_boundaries.py
git commit -m "feat: 补齐规划内核安全边界（伪造片段作废 + 未知意图拒绝 + LLM 降级通道）"
```

---

## 完成判据（本计划验收）

- [ ] `ruff check .` → `All checks passed!`
- [ ] `mypy` → `Success: no issues found`（`src/` 下 `planning/` 六模块严格模式通过）
- [ ] `python -m pytest tests/planning -v` → 全绿（本计划新增 ≥ 63 项：Task 0=4、Task 1=10、Task 2=6、Task 3=8、Task 4=13、Task 5=8、Task 6=6、Task 7=8）
- [ ] `python -m pytest` → 全绿（00 / 01 的既有测试未被破坏）
- [ ] **动作词表 20 项**：`ACTION_KINDS` 与 `README.md` 规范动作表逐项一致，且 `set(ACTION_KINDS) == set(build_default_registry().actions())` 有交叉断言测试；`bill.query` 等表外动作名已删除
- [ ] **槽位键对齐**：`set(REQUIRED_SLOTS) == set(ACTION_KINDS)`（20 个动作）且 `SLOT_PARAMS` 覆盖全部被引用槽位，有测试断言
- [ ] **写动作的必填控制槽与 02 目录一致**（README 裁决 #16/#17）：`transfer.create` / `transfer.schedule` / `wealth.subscribe` / `wealth.redeem` 的 `REQUIRED_SLOTS` 含 `user_id`；`subscription.sign` 为 `("user_id", "merchant", "amount_cents", "period", "next_charge_on")`（不含 `subscription_id`），有测试断言
- [ ] **Spike 结论落地**：`orchestrator.py::StepDriver._advance` 是唯一迁移出口，四条断言覆盖"每次迁移都被审计 + 每次迁移都被鉴权 + 被拒迁移留痕不推进 + 失败迁移被审计"；结论 = 框架优先 / 自研 fallback，写入本文档 Task 0
- [ ] **冻结面全覆盖**：`IntentProposal` / `Intent` / `IntentModel` / `TemplateIntentModel` / `LlmIntentModel` / `AnchorLedger` / `DerivedDepthError` / `NetEffect` / `net_effect` / `Question` / `PlanningResult` / `Kernel` 全部实现且签名与 `README.md` 冻结面一致
- [ ] **H2**：`Kernel` 不执行动作、不返回授权级别（`PlanningResult` 无级别字段）；未知/低置信度意图 → `blocked=True` 且 `plan.nodes == ()`，有测试断言
- [ ] **H3 控制槽**：`unverified` 值进不了控制槽（`Slot(CONTROL, Value(UNVERIFIED))` 会被拒、`fill_control_slots` 显式丢弃并转追问），有测试断言
- [ ] **多候选收款人**：`needs_clarification=True` 时不生成节点、只产出 `Question(candidates=...)`，**不产出级别**，有测试断言
- [ ] **`derived` 约束**：`derive` 的父必须是 `explicit`/`user_approved`，深度 ≤ 1，`derived → derived` 抛 `DerivedDepthError`，有测试断言
- [ ] **DAG 与执行门**：节点全部经 `validate_plan` 自检；`cfhash` 结果出现在 `rationale` 中且与 `cfhash(plan.nodes)` 相等，有测试断言
- [ ] **NE 口径**：定时/代扣/续费/转账/申购/冻结 = 正流出、赎回/解冻 = 负流出、订阅取消 = 负、挂失单列不可逆、查询/非不可逆状态变更 = 0，各有独立断言
- [ ] **不联网**：全部测试要么走 `TemplateIntentModel`、要么注入 `transport`；`LlmIntentModel` 未注入且无凭据时抛 `LlmUnavailableError`，有测试断言
- [ ] **无占位符**：全文档无 TODO / TBD / "稍后补充"

## 后续计划（依赖本计划产出的接口）

| 计划 | 依赖本计划产出的 |
|---|---|
| `02-tool-registry`（已出） | **动作名对齐**：`build_default_registry()` 的动作名必须覆盖 `planning.llm::CANONICAL_ACTIONS` 的**二十个**动作（已由 Task 1 的交叉断言测试固化）；`Kernel` 只依赖 `ToolRegistry.actions()`，无需其它耦合 |
| `03-pdp-pep` | `planning.net_effect::net_effect(plan, gateway)` 作为 `policy.pdp::evaluate_plan(plan, net)` 的入参；`planning.anchors::AnchorLedger` 供执行门审计打印锚点来源 |
| `05-scenarios` | `planning.slots::REQUIRED_SLOTS` / `SLOT_PARAMS`（场景参数约定）、`planning.kernel::PlanningResult`（场景编排出口）、`planning.llm::ACTION_KINDS`（动作语义） |
| `06-web-channel` | `planning.kernel::Kernel.plan`（Web 提交入口）、`PlanningResult.questions`（追问渲染）、`PlanningResult.rationale`（含 `cfhash`，供计划卡冻结） |
| `07-eval-and-docs` | `planning.llm::FallbackIntentModel.degraded`（H9 降级差分演示的证据位）、`planning.net_effect::NetEffect`（计划级策略回归断言） |

---

*2026-10-01 建立 · 依据 `docs/plans/README.md` 冻结面 + `01-fact-gateway.md` 产出接口 + PRD §0/§4/§5.3/§5.4/§5.7/§10.4*

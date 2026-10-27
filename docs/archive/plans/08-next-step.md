# 下一步实施计划（增量 · 通用跨场景多意图 + Docker + README）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> **⚠️ 回溯说明（2026-10-08）**：本文是**当时**的实施记录。其中的 `Pep(...)` 装配片段已过时——现行签名为 `Pep(registry, gateway, core_bank, audit, breaker, write_handlers, query_handlers)`（两者由组合根分别从 `scenarios.registry.WRITE_HANDLERS` / `QUERY_HANDLERS` 注入），见 [README.md](../../plans/README.md) 冻结面表与**裁决 #25 / #26**。

**Goal:** 在 00–07 已交付的零信任双核银行 AI 智能体上，补齐「任意跨场景多意图（顺序执行 + 整单批量确认）」能力，并修复部署/文档两处提交缺口。

**Architecture:** 多意图能力**只扩展 05 的 `scenarios/cross.py::CrossHandler`**（不新增冻结符号）；`select_handler` / `run_scenario` / 06 的确认-执行协议**不动**。`CrossHandler.build` 分两支：冻结+资金组合保留原语义（逐字），其余多域组合按域分组→复用各域 handler 的 `build`→合并重编号→同资源串行→**单一 `Plan`**。因此 06 现有「单卡 `cfhash` 冻结 → 一次确认 → 顺序执行」天然实现「批量确认 + 按输入顺序逐个执行」。

**Tech Stack:** Python 3.11+ · 标准库（05）· fastapi/uvicorn（06，已落地）· pytest · ruff · mypy（`--strict` 只对 `src/`）· Docker（Task 2）

## Global Constraints

- Python **≥ 3.11**（本机 3.13 运行）；不使用 3.12+ 专属语法；泛型用 `Generic[T]` / `TypeVar`。
- 行宽 **100**；ruff 规则集 `E,F,I,UP,B`；`mypy --strict` 对 `src/` 生效（测试文件不参与 mypy）。
- **不得修改冻结面符号与签名**（见 `docs/plans/README.md` 冻结面）。
- **不得修改 01/02/03/04 的既有实现**（Task 1 只改 05 `cross.py` + 06 `session.py`；Docker/README 为新增或文档）。
- **不改 `gateway/store.py::SCHEMA_SQL`**。
- **计划级安全不变**：单张计划卡 + 单 `cfhash` + 单次 `evaluate_plan(NE)`；任一子意图澄清/拒绝 → 整单**不产级别**。
- **H3**：`unverified` 永不进控制槽；锚点规则（`grant`）在冻结模式下**保持严格**（缺失→拒绝），仅由会话层/评测链为**用户明说的意图**补 `grant="explicit"`。
- H9：LLM 降级通道声明为能力子集，不宣称等价。
- 提交信息格式 `<type>: <描述>`（type ∈ feat/fix/test/docs/chore/refactor）。
- **无占位符**：不写 TBD / TODO / "稍后补充"；每个 Task 都要能跑通。
- 工作流（本机窗口约束）：代码先写 `e:\changer_learning\.tmp\ai-banking-scaffold\`，再 `Copy-Item` 到 `E:\AI_Banking`，再 `git -C E:\AI_Banking` 提交推送；跑测试时工作目录 = scaffold。

---

## File Structure

| 文件 | 动作 | 职责 |
|---|---|---|
| `src/ai_banking_copilot/scenarios/cross.py` | Modify | `CrossHandler` 通用化：`matches` 扩 funds-only；`build` 分冻结支/通用支；新增内部 `FundsHandler` 与合并助手 |
| `src/ai_banking_copilot/web/session.py` | Modify | `_assemble_intents` 为用户明说意图补 `grant="explicit"` 默认值 |
| `tests/scenarios/test_cross_multi.py` | Create | 通用多意图的单元/集成断言 |
| `tests/web/test_session_multi_intent.py` | Create | 会话层多意图端到端 + grant 默认 |
| `docs/plans/README.md` | Modify | 追加裁决 #21 + 回溯修订 05 Interfaces |
| `Dockerfile` | Create | 容器镜像（api） |
| `docker-compose.yml` | Create | `api` 服务编排 |
| `.dockerignore` | Create | 排除缓存/虚拟环境 |
| `README.md` | Modify | 修正测试路径 / 目录树 / 去「开发中」 |
| `docs/plans/08-next-step.md` | Create | 本计划入库副本（执行时复制） |

---

### Task 1: 05 通用跨场景多意图（+ 裁决 #21）

**Files:**
- Modify: `src/ai_banking_copilot/scenarios/cross.py`
- Modify: `src/ai_banking_copilot/web/session.py:304-308`
- Create: `tests/scenarios/test_cross_multi.py`
- Create: `tests/web/test_session_multi_intent.py`
- Modify: `docs/plans/README.md`（裁决记录段）

**Interfaces:**
- Consumes: `scenarios.base.{ScenarioHandler, ScenarioRefused, ClarificationRequired, grant_anchor, as_money, as_str, trusted_slot, accept_data}` · `scenarios.transfer.{TransferHandler, build_money_node, MONEY_ACTIONS}` · `scenarios.bills.BillsHandler` · `scenarios.wealth.WealthHandler` · `scenarios.cards.CardsHandler` · `scenarios.subscriptions.SubscriptionsHandler` · `contracts.plan.{Plan, PlanNode, IntentRef, AnchorSource, validate_plan}` · `planning.anchors.AnchorLedger` · `planning.llm.Intent`
- Produces（不新增冻结符号）：
  - `CrossHandler.matches(intents) -> bool`：`len(domains) >= 2` **或** `domains == frozenset({"funds"})`
  - `CrossHandler.build(intents, gateway) -> Plan`（冻结支保留原语义；通用支产出单一 Plan）
  - 模块级 `intents_domains(intents) -> frozenset[str]`（已存在，复用）

- [ ] **Step 1: 写失败的测试**

`tests/scenarios/test_cross_multi.py`

```python
from datetime import date

from ai_banking_copilot.contracts.plan import validate_plan
from ai_banking_copilot.gateway.facts import FactGateway
from ai_banking_copilot.gateway.models import Money
from ai_banking_copilot.planning.llm import Intent
from ai_banking_copilot.contracts.taint import Trust, Value
from ai_banking_copilot.contracts.plan import cfhash
from ai_banking_copilot.scenarios.base import ScenarioStatus, plan_net_effect
from ai_banking_copilot.scenarios.registry import run_scenario, select_handler


def _transfer(payee: str, cents: int) -> Intent:
    return Intent(
        action="transfer.create",
        slots={
            "account": Value("acc_savings", Trust.TRUSTED),
            "payee": Value(payee, Trust.TRUSTED),
            "amount": Value(Money(cents), Trust.TRUSTED),
            "when": Value(date(2026, 10, 1), Trust.TRUSTED),
            "span": "转账",
        },
        confidence=0.9,
        evidence="转账",
    )


def _cancel_subscription() -> Intent:
    return Intent(
        action="subscription.cancel",
        slots={"subscription": Value("sub_video", Trust.TRUSTED), "span": "取消订阅"},
        confidence=0.9,
        evidence="取消订阅",
    )


def _redeem(cents: int) -> Intent:
    return Intent(
        action="wealth.redeem",
        slots={
            "account": Value("acc_savings", Trust.TRUSTED),
            "product": Value("wp_bond", Trust.TRUSTED),
            "amount": Value(Money(cents), Trust.TRUSTED),
            "span": "赎回",
        },
        confidence=0.9,
        evidence="赎回",
    )


def _freeze(cents: int) -> Intent:
    return Intent(
        action="funds.freeze",
        slots={
            "account": Value("acc_savings", Trust.TRUSTED),
            "amount": Value(Money(cents), Trust.TRUSTED),
            "grant": "explicit",
            "span": "冻结",
        },
        confidence=0.9,
        evidence="冻结",
    )


def test_cross_domain_intents_assemble_into_a_single_plan(gateway: FactGateway) -> None:
    intents = [_transfer("p_lina", 50000), _cancel_subscription()]
    assert select_handler(intents).name == "cross"
    outcome = run_scenario(select_handler(intents), intents, gateway)
    assert outcome.status is ScenarioStatus.PLANNED
    assert outcome.plan is not None
    validate_plan(outcome.plan)
    assert [node.action for node in outcome.plan.nodes] == [
        "transfer.create",
        "subscription.cancel",
    ]
    assert outcome.decision is not None


def test_same_resource_nodes_are_serialised(gateway: FactGateway) -> None:
    intents = [_transfer("p_lina", 50000), _redeem(200000)]
    outcome = run_scenario(select_handler(intents), intents, gateway)
    assert outcome.plan is not None
    first, second = outcome.plan.nodes
    assert first.action == "transfer.create"
    assert second.action == "wealth.redeem"
    assert second.depends_on == (first.node_id,)


def test_multi_domain_net_effect_is_whole_plan(gateway: FactGateway) -> None:
    intents = [_transfer("p_lina", 50000), _redeem(200000)]
    outcome = run_scenario(select_handler(intents), intents, gateway)
    assert outcome.plan is not None
    assert plan_net_effect(outcome.plan, gateway).amount == Money(-150000)


def test_any_clarification_blocks_the_whole_plan(gateway: FactGateway) -> None:
    intents = [_transfer("王伟", 50000), _cancel_subscription()]
    outcome = run_scenario(select_handler(intents), intents, gateway)
    assert outcome.status is ScenarioStatus.NEEDS_CLARIFICATION
    assert outcome.plan is None
    assert outcome.decision is None


def test_multi_domain_plan_has_a_single_frozen_hash(gateway: FactGateway) -> None:
    intents = [_transfer("p_lina", 50000), _cancel_subscription()]
    outcome = run_scenario(select_handler(intents), intents, gateway)
    assert outcome.plan is not None
    assert cfhash(outcome.plan.nodes) == cfhash(outcome.plan.nodes)


def test_single_domain_funds_is_now_supported(gateway: FactGateway) -> None:
    intents = [_freeze(100000)]
    assert select_handler(intents).name == "cross"
    outcome = run_scenario(select_handler(intents), intents, gateway)
    assert outcome.status is ScenarioStatus.PLANNED
    assert outcome.plan is not None
    assert [node.action for node in outcome.plan.nodes] == ["funds.freeze"]


def test_frozen_pattern_semantics_are_unchanged(gateway: FactGateway) -> None:
    freeze = _freeze(200000)
    flower = _transfer("p_lina", 30000)
    flower = Intent(
        action=flower.action,
        slots={**flower.slots, "grant": "user_approved"},
        confidence=flower.confidence,
        evidence="订花",
    )
    outcome = run_scenario(select_handler([freeze, flower]), [freeze, flower], gateway)
    assert outcome.plan is not None
    assert [node.node_id for node in outcome.plan.nodes] == ["n1", "n2"]
    assert outcome.plan.nodes[1].depends_on == ("n1",)
```

`tests/web/test_session_multi_intent.py`

```python
from ai_banking_copilot.web.app import AppDeps
from ai_banking_copilot.web.session import Session


def test_multi_intent_utterance_yields_one_card_with_all_nodes(deps: AppDeps) -> None:
    session = Session(deps)
    view = session.submit(
        "转给李娜 500 元，取消订阅",
        selected={"account": "acc_savings", "subscription": "sub_video"},
    )
    assert view.plan is not None
    assert [node.action for node in view.plan.nodes] == [
        "transfer.create",
        "subscription.cancel",
    ]
    assert view.card is not None


def test_session_defaults_grant_for_user_intents(deps: AppDeps) -> None:
    session = Session(deps)
    view = session.submit("冻结 500 元，转给李娜 300 元", selected={"account": "acc_savings"})
    assert view.plan is not None
    assert [node.action for node in view.plan.nodes] == ["funds.freeze", "transfer.create"]
```

> `deps` fixture 见 `tests/web/conftest.py`（已存在）。**注意**：`BillsHandler.build` 恒产出 **2 个节点**（`bills.monthly` + `bills.category_trend`），故涉及账单域的多意图用例应断言节点数 ≥2 或改用「转账 + 取消订阅」这类单节点域组合。

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/scenarios/test_cross_multi.py tests/web/test_session_multi_intent.py -v`
Expected: FAIL —— 跨域用例遇 `ScenarioRefused("跨场景暂不支持动作 …")`；funds-only 遇 `UnknownScenarioError`；`AppDeps` 用法若不合则 collection error。

- [ ] **Step 3: 最小实现（`cross.py`）**

在 `src/ai_banking_copilot/scenarios/cross.py` 中：新增导入、常量、`matches` 扩展、`build` 拆两支、通用合并助手与内部 `FundsHandler`。

```python
from collections.abc import Mapping, Sequence

from ai_banking_copilot.scenarios.bills import BillsHandler
from ai_banking_copilot.scenarios.cards import CardsHandler
from ai_banking_copilot.scenarios.subscriptions import SubscriptionsHandler
from ai_banking_copilot.scenarios.transfer import MONEY_ACTIONS, TransferHandler, build_money_node
from ai_banking_copilot.scenarios.wealth import WealthHandler

_FUNDS_ACTIONS = frozenset({"funds.freeze", "funds.release"})
_FUNDS_DOMAIN = frozenset({"funds"})
_FROZEN_ACTIONS = frozenset({_FREEZE_ACTION, *MONEY_ACTIONS})
_DOMAIN_HANDLERS: Mapping[str, ScenarioHandler] = {
    "account": BillsHandler(),
    "bills": BillsHandler(),
    "transfer": TransferHandler(),
    "wealth": WealthHandler(),
    "card": CardsHandler(),
    "subscription": SubscriptionsHandler(),
    "funds": _FundsHandler(),
}
_RESOURCE_KEYS = ("account_id", "card_id", "subscription_id", "hold_id")
```

```python
    def matches(self, intents: Sequence[Intent]) -> bool:
        domains = intents_domains(intents)
        return len(domains) >= _MIN_DOMAINS or domains == _FUNDS_DOMAIN

    def build(self, intents: Sequence[Intent], gateway: FactGateway) -> Plan:
        actions = {intent.action for intent in intents}
        domains = intents_domains(intents)
        if len(domains) >= _MIN_DOMAINS and actions <= _FROZEN_ACTIONS and _FREEZE_ACTION in actions:
            return _build_frozen_pattern(intents, gateway)
        return _assemble_multi(intents, gateway)
```

把**现有 `build` 的函数体**（`intents_domains < _MIN_DOMAINS` 守卫之后的全部逻辑）原样搬进新函数 `_build_frozen_pattern(intents, gateway) -> Plan`，并在其开头保留：

```python
def _build_frozen_pattern(intents: Sequence[Intent], gateway: FactGateway) -> Plan:
    if len(intents_domains(intents)) < _MIN_DOMAINS:
        raise ScenarioRefused("跨场景联动需覆盖 ≥ 2 个场景域")
    ledger = AnchorLedger()
    nodes: list[PlanNode] = []
    freeze_node_id: str | None = None
    freeze_intents = [item for item in intents if item.action == _FREEZE_ACTION]
    other_intents = [item for item in intents if item.action != _FREEZE_ACTION]
    for index, intent in enumerate(freeze_intents, start=1):
        node = _build_freeze_node(f"n{index}", intent, ledger)
        nodes.append(node)
        freeze_node_id = node.node_id
    start = len(nodes)
    for offset, intent in enumerate(other_intents, start=1):
        node_id = f"n{start + offset}"
        source = _grant_source(intent)
        if intent.action not in MONEY_ACTIONS:
            raise ScenarioRefused(f"跨场景暂不支持动作 {intent.action}")
        node = build_money_node(node_id, intent, gateway, ledger, source)
        if freeze_node_id is not None:
            node = replace(node, depends_on=(freeze_node_id,))
        nodes.append(node)
    plan = Plan(plan_id="cross_dag", nodes=tuple(nodes))
    validate_plan(plan)
    return plan
```

追加通用合并助手与 funds 处理器：

```python
def _assemble_multi(intents: Sequence[Intent], gateway: FactGateway) -> Plan:
    """通用多域组装：按域分组 → 复用各域 build → 合并重编号 → 同资源串行 → 单一 Plan。"""
    ledger = AnchorLedger()
    nodes: list[PlanNode] = []
    last_by_resource: dict[str, str] = {}
    for domain, group in _group_by_domain(intents):
        handler = _DOMAIN_HANDLERS.get(domain)
        if handler is None:
            raise ScenarioRefused(f"通用多意图暂不支持动作域 {domain}")
        sub_plan = handler.build(group, gateway)
        offset = len(nodes)
        local_ids = {
            node.node_id: f"n{offset + index}"
            for index, node in enumerate(sub_plan.nodes, start=1)
        }
        for sub_node in sub_plan.nodes:
            deps = tuple(local_ids.get(dep, dep) for dep in sub_node.depends_on)
            resource = _resource_key(sub_node)
            if resource is not None and resource in last_by_resource:
                deps = (*deps, last_by_resource[resource])
            source = (
                sub_node.intent.source if sub_node.intent is not None else AnchorSource.EXPLICIT
            )
            merged = PlanNode(
                node_id=local_ids[sub_node.node_id],
                action=sub_node.action,
                params=dict(sub_node.params),
                intent=IntentRef(
                    source=source,
                    anchor_id=grant_anchor(ledger, source, f"多意图合并：{sub_node.action}"),
                ),
                depends_on=deps,
            )
            nodes.append(merged)
            if resource is not None:
                last_by_resource[resource] = merged.node_id
    plan = Plan(plan_id="multi_dag", nodes=tuple(nodes))
    validate_plan(plan)
    return plan


def _group_by_domain(intents: Sequence[Intent]) -> tuple[tuple[str, tuple[Intent, ...]], ...]:
    """按动作域分组，保持**输入顺序**（dict 保序）。"""
    buckets: dict[str, list[Intent]] = {}
    for intent in intents:
        buckets.setdefault(intent.action.split(".", 1)[0], []).append(intent)
    return tuple((domain, tuple(group)) for domain, group in buckets.items())


def _resource_key(node: PlanNode) -> str | None:
    for key in _RESOURCE_KEYS:
        value = node.params.get(key)
        if isinstance(value, str) and value:
            return f"{key}:{value}"
    return None


def _build_release_node(node_id: str, intent: Intent, ledger: AnchorLedger) -> PlanNode:
    source = _grant_source(intent)
    anchor_id = grant_anchor(ledger, source, "解冻")
    params: dict[str, object] = {
        "hold_id": as_str(trusted_slot(intent, ("hold", "hold_id"), "hold"), "hold"),
    }
    return PlanNode(
        node_id=node_id,
        action="funds.release",
        params=params,
        intent=IntentRef(source=source, anchor_id=anchor_id),
    )


class _FundsHandler(ScenarioHandler):
    """`funds.*` 域的内部构建器（仅供通用合并复用，不注册进 `SCENARIO_HANDLERS`）。"""

    @property
    def name(self) -> str:
        return "funds"

    def matches(self, intents: Sequence[Intent]) -> bool:
        return any(intent.action in _FUNDS_ACTIONS for intent in intents)

    def build(self, intents: Sequence[Intent], gateway: FactGateway) -> Plan:
        ledger = AnchorLedger()
        nodes: list[PlanNode] = []
        for index, intent in enumerate(intents, start=1):
            node_id = f"n{index}"
            if intent.action == _FREEZE_ACTION:
                nodes.append(_build_freeze_node(node_id, intent, ledger))
            elif intent.action == "funds.release":
                nodes.append(_build_release_node(node_id, intent, ledger))
            else:
                raise ScenarioRefused(f"funds 域暂不支持动作 {intent.action}")
        plan = Plan(plan_id="funds_plan", nodes=tuple(nodes))
        validate_plan(plan)
        return plan
```

> **模块级顺序（必须）**：`_build_release_node` 与 `class _FundsHandler` 必须写在 `_DOMAIN_HANDLERS` **之前**（否则 `NameError`）；`_build_freeze_node` 保留在模块底部（调用期才解析）。`_FUNDS_ACTIONS` / `_FROZEN_ACTIONS` / `_RESOURCE_KEYS` 与 `_DOMAIN_HANDLERS` 一并放在导入段之后、`CrossHandler` 之前。

`src/ai_banking_copilot/web/session.py`（`_assemble_intents` 内，`slots.update(extras)` 之后一行）：

```python
            slots.setdefault("grant", "explicit")
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/scenarios tests/web -v`
Expected: PASS（新增 9 项 + 既有 scenarios/web 全绿）

- [ ] **Step 5: 回写裁决 #21（治理）**

在 `docs/plans/README.md` 的「裁决记录」段追加：

```markdown
21. **05 `CrossHandler` 通用化**（2026-10-02，多意图能力）：`matches` 扩为「≥2 场景域 **或** 单域 `funds.*`」；`build` 分两支——「冻结 + 资金」组合**逐字保留**原语义（`grant` 锚点规则不变），其余多域组合走**通用支**（按域分组 → 复用各域 handler 的 `build` → 合并重编号 → 同资源串行 → **单一 Plan**）。`select_handler` / `run_scenario` / 06 确认-执行协议**不变**；计划级安全（单卡 / 单 `cfhash` / 单次 NE）不变。会话层 `web.session._assemble_intents` 为用户明说意图补 `grant="explicit"`（与 07 评测链 `_scenario_intent` 一致）。
```

- [ ] **Step 6: 全量回归**

Run: `python -m ruff check . && python -m mypy && python -m pytest`
Expected: `All checks passed!` · `Success: no issues found` · 全绿（既有 504 + 新增 ≥ 9）

其他未纳入本计划的两类项见 Global Constraints（不得修改 01–04）。

- [ ] **Step 7: 提交**

```bash
git add src/ai_banking_copilot/scenarios/cross.py src/ai_banking_copilot/web/session.py \
        tests/scenarios/test_cross_multi.py tests/web/test_session_multi_intent.py \
        docs/plans/README.md
git commit -m "feat: 05 通用跨场景多意图（CrossHandler 通用组装 + 会话层 grant 默认 + 裁决 #21）"
```

---

### Task 2: Docker 一键复现（Dockerfile + docker-compose）

**Files:**
- Create: `Dockerfile`
- Create: `docker-compose.yml`
- Create: `.dockerignore`
- Modify: `docs/plans/08-next-step.md`（本计划入库副本，可选）

**Interfaces:**
- Consumes: `pyproject.toml`（`dependencies=[fastapi,uvicorn]`；`[dev]`）· `deploy/README.md`（`docker compose up -d`）
- Produces: `api` 服务（端口 8000）· 可通过 `docker compose config` 校验

- [ ] **Step 1: 追加应用入口 `main()`**

`web.app.create_app(deps: AppDeps)` 需要显式依赖，故先追加一个默认装配入口（**新增函数，不改冻结面符号**）。在 `src/ai_banking_copilot/web/app.py` 顶部确保有 `from pathlib import Path`（缺则补），并在文件末尾追加：

```python
def main() -> None:
    """默认装配（模拟核心银行 + 只读事实网关 + 全量依赖）并启动 HTTP 服务。"""
    import uvicorn

    from ai_banking_copilot.gateway.core_bank import DEFAULT_BOOKING_DATE, CoreBank
    from ai_banking_copilot.gateway.facts import FactGateway
    from ai_banking_copilot.gateway.seed import DEMO_USER_ID, seed_database
    from ai_banking_copilot.gateway.store import Store, StoreMode, init_schema
    from ai_banking_copilot.planning.llm import TemplateIntentModel
    from ai_banking_copilot.policy.audit import AuditLog
    from ai_banking_copilot.policy.breaker import CircuitBreaker
    from ai_banking_copilot.policy.pep import Pep
    from ai_banking_copilot.tools.catalog import build_default_registry

    db_path = Path("runtime/copilot.db")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    writer = Store(db_path)
    init_schema(writer)
    seed_database(writer)
    writer.close()

    gateway = FactGateway(Store(db_path, StoreMode.READ_ONLY))
    registry = build_default_registry()
    breaker = CircuitBreaker()
    audit = AuditLog(db_path.parent / "audit.jsonl")
    deps = AppDeps(
        intent_model=TemplateIntentModel(),
        registry=registry,
        gateway=gateway,
        pep=Pep(registry, gateway, CoreBank(Store(db_path, StoreMode.READ_WRITE)), audit, breaker),
        breaker=breaker,
        audit=audit,
        user_id=DEMO_USER_ID,
        today=DEFAULT_BOOKING_DATE,
    )
    uvicorn.run(create_app(deps), host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()
```

（`AppDeps` 字段名以 `app.py` 中 `class AppDeps` 为准；如有出入按实际字段调整。）

- [ ] **Step 2: 校验入口可导入**

Run: `python -c "from ai_banking_copilot.web.app import main; print(main.__name__)"`
Expected: `main`

- [ ] **Step 3: 写 `Dockerfile`**

```dockerfile
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m pip install --no-cache-dir -e .
COPY tests ./tests
COPY docs ./docs
COPY deploy ./deploy
EXPOSE 8000
CMD ["python", "-m", "ai_banking_copilot.web.app"]
```

- [ ] **Step 4: 写 `docker-compose.yml`**

```yaml
services:
  api:
    build: .
    ports:
      - "8000:8000"
    environment:
      - DASHSCOPE_API_KEY=${DASHSCOPE_API_KEY:-}
```

- [ ] **Step 5: 写 `.dockerignore`**

```text
__pycache__/
*.pyc
.mypy_cache/
.ruff_cache/
.pytest_cache/
*.egg-info/
.git/
.venv/
runtime/
```

- [ ] **Step 6: 校验**

Run: `docker compose config`
Expected: 打印归一化 compose 配置、退出码 0（无 Docker 环境时跳过并如实记录）

- [ ] **Step 7: 提交**

```bash
git add src/ai_banking_copilot/web/app.py Dockerfile docker-compose.yml .dockerignore
git commit -m "feat: Docker 一键复现（应用入口 main + Dockerfile + compose）"
```

---

### Task 3: 根 README 修正

**Files:**
- Modify: `README.md`

- [ ] **Step 1: 修正「测试」段**

把 `pytest tests/unit` / `tests/integration` / `tests/adversarial` 三条改为真实入口：

```bash
pytest                                   # 全量（分层单元 + 集成 + 评测）
pytest tests/eval                        # 评测套件（标准 62 + 对抗 19 + held-out 20）
pytest tests/gateway tests/policy tests/planning tests/scenarios  # 分层
```

并删除「> 开发中。」措辞。

- [ ] **Step 2: 修正目录树**

在「目录结构」中把 `docs/` 子树补为：`architecture.md` / `algorithms.md` / `security-design.md` / `security-self-assessment.md` / `作品简介.md` / `plans/`；把 `deploy/` 注为「部署说明与 Docker 编排」。

- [ ] **Step 3: 校验**

Run: `python -m pytest tests -q`
Expected: 全绿（README 非代码，仅确保无测试引用 README 中已删路径；如有 `test_docs_exist` 类断言则同步）

- [ ] **Step 4: 提交**

```bash
git add README.md
git commit -m "docs: 修正根 README（测试路径 / 目录树 / 去开发中）"
```

---

### Task 4: 设计仓 · 技术详情折叠区

**Files:**
- Modify: `E:\ai-banking-copilot-web\index.html`（+ `assets/*.css` 若需）
- 版本号：递增 `?v=N`

> 设计仓为**独立仓库**；本 Task 的提交在 `E:\ai-banking-copilot-web` 内完成（`git -C E:\ai-banking-copilot-web`）。

- [ ] **Step 1: 加折叠区结构与样式**

在展示计划卡/审计的技术标识处（action 名 / `cfhash` / `record_id`）包一层：

```html
<details class="tech-details">
  <summary>技术详情</summary>
  <ul>
    <li>action: <code>transfer.create</code></li>
    <li>cfhash: <code>…</code></li>
  </ul>
</details>
```

CSS：`.tech-details[open] summary { color: var(--blue-600); }`（令牌取自 `assets/tokens.css`）。

- [ ] **Step 2: bump 版本号**

把 `index.html` 中所有 `?v=N` 的自有资源引用改为 `?v=N+1`（防 304 命中旧样式）。

- [ ] **Step 3: 本地实测**

Run: `python -m http.server 8765 --directory E:\ai-banking-copilot-web`
Expected: 浏览器打开 `http://localhost:8765/index.html`，技术详情**默认收起**、点击可展开；样式为最新版。

- [ ] **Step 4: 提交**

```bash
git -C E:\ai-banking-copilot-web add -A
git -C E:\ai-banking-copilot-web commit -m "feat(index): 技术详情折叠区（技术标识默认收起）+ 版本号 bump"
```

---

### Task 5: 设计仓 · 三色戳记导出 SVG

**Files:**
- Create: `E:\ai-banking-copilot-web\assets\stamp-green.svg` / `stamp-yellow.svg` / `stamp-red.svg` / `stamp-denied.svg`
- Modify: `E:\ai-banking-copilot-web\index.html`（引用这些 SVG；`?v=N+1`）

- [ ] **Step 1: 逐个创建四个 SVG**

每个 SVG 为**自包含静态资源**（无外链），尺寸 `120×48`，文字与色值取自 `assets/tokens.css` 的三色令牌（绿/黄/红）+ 拒绝（denied）。示例（绿）：

```svg
<svg xmlns="http://www.w3.org/2000/svg" width="120" height="48" viewBox="0 0 120 48" role="img" aria-label="绿色：自动执行">
  <rect x="1" y="1" width="118" height="46" rx="8" fill="none" stroke="#1a7f37" stroke-width="2"/>
  <text x="60" y="30" text-anchor="middle" font-size="16" fill="#1a7f37">绿色 · 自动</text>
</svg>
```

其余三个同构，改色值与文案（黄=确认、红=强验证、拒绝=已拦截）。

- [ ] **Step 2: 页面引用 + bump**

在分级戳记处 `<img src="assets/stamp-green.svg?v=N+1" alt="绿色 · 自动执行">`。

- [ ] **Step 3: 实测**

Run: `python -m http.server 8765 --directory E:\ai-banking-copilot-web`
Expected: 四个 SVG 均可加载、色彩与页面令牌一致。

- [ ] **Step 4: 提交**

```bash
git -C E:\ai-banking-copilot-web add -A
git -C E:\ai-banking-copilot-web commit -m "feat(assets): 三色分级戳记导出 SVG（绿/黄/红/拒绝）"
```

---

### Task 6: 门禁记录 + 可见性（仅文档）

**Files:**
- Modify: `e:\changer_learning\比赛\AI银行副驾-下一步决策冻结-2026-10-02.md`（标注门禁结论）
- Modify: `journal/agent-state.md`（进度/下一步）

- [ ] **Step 1: 在决策冻结记录中落门禁结论**

追加一行：`10/12 门禁：维持六场景（判据②③ 已达；判据① 以代码先于 M4 完成视同达标）`。

- [ ] **Step 2: 更新 agent-state**

把「下一步」改为「执行 08-next-step（T1–T5）；演示材料待发起」，并记 `可见性=private / 许可证=MIT`。

- [ ] **Step 3: 提交（仅工作区文档，不入竞赛仓库）**

工作区非可用 git 仓库（`e:\changer_learning\.git` 不可用）→ 不提交，仅落盘。

---

## 后续（不在本计划内，待 changer 发起）
- **演示材料**：演示脚本 / 答辩 PPT（≤10 分钟）/ 演示视频（≤5 分钟）/ 10/28 答疑问题清单。
- **IM Bot 渠道**（P1）。
- **设计仓剩余**：无（Task 4/5 已覆盖）。

## 完成判据（本计划验收）
- [ ] `python -m ruff check .` → `All checks passed!`
- [ ] `python -m mypy` → `Success: no issues found`（`src/` 含 `scenarios/` / `web/`）
- [ ] `python -m pytest` → 全绿（既有 504 未被破坏 + 本计划新增 ≥ 9）
- [ ] 跨域多意图：`select_handler` 返回 `cross`，产出**单一 Plan**，`cfhash` 单一，`evaluate_plan` 用**全量 NE**
- [ ] 任一子意图澄清 → 整单 `NEEDS_CLARIFICATION`（`plan is None`、`decision is None`）
- [ ] 冻结+资金组合语义**回归不变**（`tests/scenarios/test_cross.py` 原 5 项仍绿）
- [ ] 单域 `funds.freeze` 可用（原 `UnknownScenarioError` 消除）
- [ ] `docker compose config` 退出码 0（有 Docker 环境时）
- [ ] 根 README 测试路径与目录树与实际一致
- [ ] 设计仓：技术详情折叠区默认收起；四个戳记 SVG 可加载；`?v=N` 已 bump
- [ ] 未修改 01/02/03/04 既有实现；`gateway/store.py::SCHEMA_SQL` 未触碰
- [ ] 无占位符：无 TODO / TBD / "稍后补充"

---

*2026-10-02 建立 · 依据 `比赛\AI银行副驾-下一步决策冻结-2026-10-02.md`（brainstorming 产出）+ `docs/plans/README.md` 冻结面（裁决 1–20）*

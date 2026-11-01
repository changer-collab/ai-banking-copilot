"""端到端评测链：把 01 事实网关 + 04 规划内核 + 05 场景 + 03 PDP/PEP 接成一条可跑的链。

链路口径：
1. **01 事实网关**：每个用例在独立目录播种一份确定性演练库；只读网关供数值 / 实体取数。
2. **04 规划内核**：`Kernel(TemplateIntentModel(), registry)` 把一句话拆成多意图；
   控制槽补不齐 → 澄清；未识别 → 安全拒绝（不猜、不产级别）。
3. **05 场景**：`select_handler` + `run_scenario` 把意图组装成白名单动作的带锚点 DAG。
4. **03 PDP**：逐节点 `evaluate`（金额档 / 对象态 / 不可逆 / 上下文标记）+ 计划级 `evaluate_plan`
   （净效应），`strictest` 合成最终计划级级别。
5. **03 PEP**：对级别 < `MANUAL` 的节点逐节点执行（节点级执行门 `manifest_hash` + 幂等键）；
   写动作身份在 `ToolCall` 层注入 `user_id`（README 裁决 #20）；任何残留参数契约缺口或强校验
   失败**如实记为 `unsupported`**，不影响评测判定。

评测判据 = 「是否拦截」与「授权级别」；执行结果只作过程证据（PRD §9.1 的可机读中间过程）。
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from typing import Protocol

from ai_banking_copilot.contracts.authorization import Decision, Level, green_or_raise, strictest
from ai_banking_copilot.contracts.plan import Plan, PlanNode, topological_order
from ai_banking_copilot.contracts.security import INJECTION_MARKERS
from ai_banking_copilot.contracts.taint import SlotKind, Trust, Value
from ai_banking_copilot.eval.dataset import EvalCase
from ai_banking_copilot.gateway.core_bank import DEFAULT_BOOKING_DATE, CoreBank
from ai_banking_copilot.gateway.errors import GatewayError, SandboxError
from ai_banking_copilot.gateway.facts import FactGateway
from ai_banking_copilot.gateway.models import Money, Payee
from ai_banking_copilot.gateway.risk_store import (
    RiskAssessmentSource,
    StoreRiskAssessmentSource,
    init_risk_schema,
    seed_risk_assessment,
)
from ai_banking_copilot.gateway.seed import DEMO_USER_ID, seed_database
from ai_banking_copilot.gateway.store import Store, StoreMode, init_schema
from ai_banking_copilot.planning.kernel import Kernel, PlanningResult
from ai_banking_copilot.planning.llm import ACTION_KINDS, ActionKind, Intent, TemplateIntentModel
from ai_banking_copilot.planning.slots import SlotInputs, fill_control_slots
from ai_banking_copilot.policy.audit import AuditLog
from ai_banking_copilot.policy.breaker import CircuitBreaker
from ai_banking_copilot.policy.gate import StalePlanError
from ai_banking_copilot.policy.pdp import (
    FLAG_INJECTION_DETECTED,
    ObjectState,
    evaluate,
    evaluate_plan,
)
from ai_banking_copilot.policy.pep import NotAuthorizedError, Pep
from ai_banking_copilot.policy.tool_hook import ToolHookBlocked
from ai_banking_copilot.scenarios.authorization import node_policy_request
from ai_banking_copilot.scenarios.base import (
    ScenarioRefused,
    intent_text,
    plan_is_pure_query,
    plan_net_effect,
)
from ai_banking_copilot.scenarios.bills import category_share_in_sandbox
from ai_banking_copilot.scenarios.registry import (
    QUERY_HANDLERS,
    WRITE_HANDLERS,
    UnknownScenarioError,
    run_scenario,
    select_handler,
)
from ai_banking_copilot.scenarios.wealth import (
    FORBIDDEN_HINTS,
    is_recommendation_request,
    to_risk_assessment,
)
from ai_banking_copilot.tools.catalog import build_default_registry
from ai_banking_copilot.tools.registry import ParamSpec, ToolCall, ToolRegistry, ToolSpec

_DEFAULT_SELECTED: Mapping[str, object] = {
    "account": "acc_savings",
    "product": "wp_cash",
    "subscription": "sub_video",
    "card": "card_debit",
    "user_id": DEMO_USER_ID,
    "daily_limit": 100000,
    # 卡片域分支槽：只被 card.* 动作使用，不影响其他场景的金额 / 日期解析。
    "address": "上海市浦东新区演示路 1 号",
    "mail_method": "standard",
    "replace": "no",
    "credit_mode": "temporary",
    "loss_type": "formal",
}
"""受信任调用方（交互层）的默认选定对象：确定性、可复现（种子库中均存在）。

`user_id` 亦在此登记：规划内核的控制槽补全要求 `user_id` 必填（`planning.slots.REQUIRED_SLOTS`），
由受信任调用方提供（README 裁决 #16 / #20）；执行计划仍由 05 场景组装（不含 `user_id`），
真正注入发生在 `ToolCall` 层（见 `_tool_params`）。
"""

_LIMIT_ACTIONS: frozenset[str] = frozenset({"card.apply_virtual", "card.set_limit"})
_QUERY_STATUS = "queried"
_SKIPPED_STATUS = "skipped"
_UNSUPPORTED_STATUS = "unsupported"


@dataclass(frozen=True)
class NodeOutcome:
    """单节点的裁决与执行取证（供序关系不变式与过程证据使用）。"""

    node_id: str
    action: str
    level: Level | None
    is_pure_query: bool
    is_outflow: bool
    daily_accumulated_cents: int
    object_blocked: bool
    executed_status: str


@dataclass(frozen=True)
class CaseResult:
    """一条用例的完整取证结果。`level is None` 表示澄清 / 安全拒绝（不产出级别）。"""

    case: EvalCase
    status: str
    blocked: bool
    level: Level | None
    nodes: tuple[NodeOutcome, ...]
    net_amount_cents: int
    irreversibles: tuple[str, ...]
    context_flags: frozenset[str]
    blocked_object: bool
    derived_released: bool | None
    elapsed_ms: int
    message: str

    @property
    def level_name(self) -> str | None:
        """授权级别名（`None` 表示不产出级别）。"""
        return None if self.level is None else self.level.name


def matches_expectation(case: EvalCase, result: CaseResult) -> bool:
    """用例期望与实际结论是否一致（pass/fail 的唯一判据）。"""
    if case.expect_blocked:
        return result.blocked and result.level is None
    if result.blocked or result.level is None:
        return False
    return result.level.name == case.expect_level


class CasePipeline(Protocol):
    """评测运行器依赖的最小面：能跑一条用例即可（便于注入替身）。"""

    def run(self, case: EvalCase) -> CaseResult:
        """在隔离环境中运行一条用例，返回取证结果。"""
        ...


def build_pipeline(workdir: str | Path) -> EvalPipeline:
    """构造端到端评测链（每个用例一份独立播种库）。"""
    return EvalPipeline(Path(workdir))


class EvalPipeline:
    """端到端评测链实现：每用例独立目录 + 独立播种库，保证隔离与可复现。"""

    def __init__(self, workdir: Path) -> None:
        self._workdir = workdir
        self._workdir.mkdir(parents=True, exist_ok=True)
        self._registry: ToolRegistry = build_default_registry()

    def run(self, case: EvalCase) -> CaseResult:
        started = time.perf_counter()
        case_dir = self._workdir / case.id
        case_dir.mkdir(parents=True, exist_ok=True)
        db_path = case_dir / "demo.db"
        writer = Store(db_path)
        init_schema(writer)
        seed_database(writer)
        init_risk_schema(writer)
        seed_risk_assessment(writer)
        writer.close()
        reader = Store(db_path, StoreMode.READ_ONLY)
        rw_store = Store(db_path, StoreMode.READ_WRITE)
        try:
            return self._evaluate(
                case=case,
                gateway=FactGateway(reader),
                core_bank=CoreBank(rw_store),
                audit=AuditLog(case_dir / "audit.jsonl"),
                breaker=CircuitBreaker(),
                risk=StoreRiskAssessmentSource(reader),
                started=started,
            )
        finally:
            reader.close()
            rw_store.close()

    def _evaluate(
        self,
        *,
        case: EvalCase,
        gateway: FactGateway,
        core_bank: CoreBank,
        audit: AuditLog,
        breaker: CircuitBreaker,
        risk: RiskAssessmentSource,
        started: float,
    ) -> CaseResult:
        flags = _context_flags(case.utterance)
        if any(hint in case.utterance for hint in FORBIDDEN_HINTS):
            return _blocked_result(case, "blocked", _YIELD_REFUSAL, flags, started)
        context: dict[str, object] = {
            "today": DEFAULT_BOOKING_DATE,
            "payees": tuple(record.payload for record in gateway.payees_of(DEMO_USER_ID)),
            "selected": dict(_DEFAULT_SELECTED),
        }
        kernel = Kernel(TemplateIntentModel(), self._registry)
        planning: PlanningResult = kernel.plan(DEMO_USER_ID, case.utterance, context)
        if planning.questions:
            message = _questions_message(planning)
            return _blocked_result(case, "clarification", message, flags, started)
        if planning.blocked or not planning.intents:
            reason = "未知或无法执行的意图：安全拒绝"
            return _blocked_result(case, "blocked", reason, flags, started)
        intents = _inject_assessment(
            tuple(_scenario_intent(intent, context) for intent in planning.intents), risk
        )
        try:
            handler = select_handler(intents)
            outcome = run_scenario(handler, intents, gateway)
        except UnknownScenarioError as error:
            return _blocked_result(case, "blocked", str(error), flags, started)
        if outcome.plan is None:
            return _blocked_result(case, "blocked", outcome.message, flags, started)
        plan = outcome.plan
        net = plan_net_effect(plan, gateway)
        plan_daily = _plan_daily_accumulated(plan, self._registry, gateway)
        pep = Pep(
            self._registry, gateway, core_bank, audit, breaker, WRITE_HANDLERS, QUERY_HANDLERS
        )
        node_outcomes: list[NodeOutcome] = []
        node_decisions: list[Decision] = []
        node_map = plan.node_map()
        for node_id in topological_order(plan):
            node = node_map[node_id]
            node_outcome, decision = self._evaluate_node(
                node,
                gateway=gateway,
                flags=flags,
                pep=pep,
                case_id=case.id,
                plan=plan,
                plan_daily=plan_daily,
            )
            node_outcomes.append(node_outcome)
            if decision is not None:
                node_decisions.append(decision)
        base = (
            green_or_raise(is_pure_query=True)
            if plan_is_pure_query(plan)
            else evaluate_plan(plan, net)
        )
        merged = strictest(*node_decisions, base) if node_decisions else base
        return CaseResult(
            case=case,
            status="planned",
            blocked=False,
            level=merged.level,
            nodes=tuple(node_outcomes),
            net_amount_cents=net.amount.cents,
            irreversibles=net.irreversibles,
            context_flags=flags,
            blocked_object=any(node.object_blocked for node in node_outcomes),
            derived_released=_derived_release(plan, gateway, case),
            elapsed_ms=_elapsed_ms(started),
            message=f"{handler.name} 计划已生成（{len(plan.nodes)} 步）",
        )

    def _evaluate_node(
        self,
        node: PlanNode,
        *,
        gateway: FactGateway,
        flags: frozenset[str],
        pep: Pep,
        case_id: str,
        plan: Plan,
        plan_daily: Money | None = None,
    ) -> tuple[NodeOutcome, Decision | None]:
        spec = self._registry.get(node.action)
        if plan_daily is None:
            plan_daily = _plan_daily_accumulated(plan, self._registry, gateway)
        request = node_policy_request(
            node,
            spec=spec,
            gateway=gateway,
            user_id=DEMO_USER_ID,
            today=DEFAULT_BOOKING_DATE,
            flags=flags,
        )
        if request.amount_applies:
            request = replace(request, daily_accumulated=plan_daily)
        decision = evaluate(request)
        executed = _execute(pep, self._registry, node, decision, case_id, plan)
        return (
            NodeOutcome(
                node_id=node.node_id,
                action=node.action,
                level=decision.level,
                is_pure_query=spec.is_pure_query,
                is_outflow=request.amount_applies,
                daily_accumulated_cents=request.daily_accumulated.cents,
                object_blocked=request.object_state is ObjectState.BLOCKED,
                executed_status=executed,
            ),
            decision,
        )


def _plan_daily_accumulated(plan: Plan, registry: ToolRegistry, gateway: FactGateway) -> Money:
    """计划级当日累计额度占用（**毛额**，含 `funds.freeze` 等中性动作）。

    与 `policy.execution.assess_plan` 同一口径：冻结仍占用当日额度，虽不计入计划级净效应
    （见 `planning.net_effect.NEUTRAL_NET_ACTIONS`），但按 `docs/2026-10-26/三色基准表`
    视同流出参与档位判定。
    口径取**计划执行前**的当日累计，避免前面的黄档节点执行后抬高后续节点的累计。
    """
    outflow = 0
    for node in plan.nodes:
        if ACTION_KINDS.get(node.action) is not ActionKind.OUTFLOW:
            continue
        spec = registry.get(node.action)
        raw = node.params.get(spec.money_param or "amount_cents")
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
            continue
        outflow += raw
    baseline = gateway.daily_transfer_total(DEMO_USER_ID, DEFAULT_BOOKING_DATE).cents
    return Money(baseline + outflow)


def _execute(
    pep: Pep,
    registry: ToolRegistry,
    node: PlanNode,
    decision: Decision,
    case_id: str,
    plan: Plan,
) -> str:
    """执行取证：级别 ≥ `MANUAL` 或非纯查询绿色 → 跳过。

    写动作身份由**评测链在 `ToolCall` 层注入** `user_id`（README 裁决 #20；02 目录已按裁决 #16
    声明必填 CONTROL 槽 `user_id`，05 场景按设计不在 `PlanNode.params` 放置它）。任何残留的参数
    契约缺口或参数强校验失败（`KeyError` / `ValueError`，含 `ParamValidationError` /
    `ForbiddenParamError` / `UnknownActionError`），或 PEP 拒绝（`NotAuthorizedError` /
    `StalePlanError`，含节点级成员资格校验失败），一律**如实记为 `unsupported`**，
    不静默通过、不影响决策级评测判定。

    写操作 Pre-Tool Hook 同样适用于评测链：离线回放**没有用户确认**，因此不签发写授权凭据，
    写动作一律记为 `unsupported`（`ToolHookBlocked`）——**不得**为评测方便绕过 Hook。
    """
    if decision.level >= Level.MANUAL:
        return _SKIPPED_STATUS
    spec = registry.get(node.action)
    if decision.level is Level.GREEN and not spec.is_pure_query:
        return _SKIPPED_STATUS
    try:
        call = ToolCall(action=node.action, params=_tool_params(spec, node))
        result = pep.execute(call, decision, plan, f"{case_id}:{node.node_id}")
    except (NotAuthorizedError, StalePlanError, ToolHookBlocked):
        return _UNSUPPORTED_STATUS
    except GatewayError as error:
        return f"failed:{type(error).__name__}"
    except (KeyError, ValueError):
        return _UNSUPPORTED_STATUS
    return result.status


def _tool_params(spec: ToolSpec, node: PlanNode) -> dict[str, object]:
    """把计划节点参数对齐到工具契约的参数名与类型（控制槽一律包 `Value(TRUSTED)`）。

    02 目录的金额参数已统一为 `amount_cents` / `daily_limit_cents`（整数分，README 裁决 #12），
    与计划节点字段名逐字对齐，故**无字段名映射**（原 `_money_aliases` 兼容层已删除）。

    `user_id` 注入（README 裁决 #20）：当动作的 `ToolSpec` 声明了**必填 CONTROL 槽** `user_id`
    且节点 params 缺失时，在 **`ToolCall` 层**填入 `DEMO_USER_ID`——**不改** `PlanNode.params` /
    `cfhash`（05 的 transfer / wealth 节点按设计不含 `user_id`）。
    """
    declared = {param.name: param for param in spec.params}
    params: dict[str, object] = {}
    for key, value in node.params.items():
        param = declared.get(key)
        if param is None:
            continue
        params[key] = Value(_coerce(value, param.py_type), Trust.TRUSTED)
    _inject_user_id(declared, params)
    return params


def _inject_user_id(declared: Mapping[str, ParamSpec], params: dict[str, object]) -> None:
    """在 `ToolCall` 层注入 `user_id`：仅当声明了**必填 CONTROL 槽** `user_id` 且尚未给出时。"""
    if "user_id" in params:
        return
    param = declared.get("user_id")
    if param is None or not param.required or param.slot is not SlotKind.CONTROL:
        return
    params["user_id"] = Value(DEMO_USER_ID, Trust.TRUSTED)


def _coerce(value: object, py_type: type[object]) -> object:
    """按工具契约声明的 Python 类型做一次确定性转换（ISO 文本 → `date`）。

    02 目录的金额参数已声明为 `int`（整数分），无需再包装成 `Money`。
    """
    if py_type is date and isinstance(value, str):
        return date.fromisoformat(value)
    return value


def _scenario_intent(intent: Intent, context: Mapping[str, object]) -> Intent:
    """把内核产出的意图补成**场景可消费**的意图：控制槽取值一律 `Value(TRUSTED)`。"""
    span = intent.slots.get("span")
    text = span if isinstance(span, str) and span else intent.evidence
    fill = fill_control_slots(
        intent.action,
        SlotInputs(
            text=text,
            today=_context_today(context),
            payees=_context_payees(context),
            selected=_context_selected(context),
        ),
    )
    slots: dict[str, object] = {
        key: Value(value, Trust.TRUSTED) for key, value in fill.slots.items()
    }
    amount = fill.slots.get("amount")
    if intent.action in _LIMIT_ACTIONS and isinstance(amount, int):
        slots["daily_limit"] = Value(Money(amount), Trust.TRUSTED)
    if isinstance(span, str) and span:
        slots["span"] = span
    slots["grant"] = "explicit"
    return Intent(
        action=intent.action,
        slots=slots,
        confidence=intent.confidence,
        evidence=intent.evidence,
    )


_YIELD_REFUSAL = "不做投资建议、不承诺收益（H6）：拒绝以收益承诺为前提的请求"


def _inject_assessment(
    intents: tuple[Intent, ...], risk: RiskAssessmentSource
) -> tuple[Intent, ...]:
    """受信任注入风险测评事实（`wealth.subscribe` 与推荐模式的 `wealth.list`；不包 `Value`）。"""
    assessment = to_risk_assessment(risk.assessment_for(DEMO_USER_ID))
    if assessment is None:
        return intents
    injected: list[Intent] = []
    for intent in intents:
        needs = intent.action == "wealth.subscribe" or (
            intent.action == "wealth.list" and is_recommendation_request(intent_text(intent))
        )
        if not needs:
            injected.append(intent)
            continue
        slots = dict(intent.slots)
        slots["assessment"] = assessment
        injected.append(
            Intent(
                action=intent.action,
                slots=slots,
                confidence=intent.confidence,
                evidence=intent.evidence,
            )
        )
    return tuple(injected)


def _context_flags(utterance: str) -> frozenset[str]:
    lowered = utterance.lower()
    if any(marker.lower() in lowered for marker in INJECTION_MARKERS):
        return frozenset({FLAG_INJECTION_DETECTED})
    return frozenset()


def _derived_release(plan: Plan, gateway: FactGateway, case: EvalCase) -> bool | None:
    """派生数字放行率的分母分子：仅 `category == "derived"` 的用例统计。

    交付链路的派生计算**只走受限沙箱**（`category_share_in_sandbox` → `run_in_sandbox`），
    不再回退进程内算子（G12）。沙箱 / 网关 / 场景任一环失败一律 fail-safe 返回 `False`
    （`SandboxError` 非 `GatewayError` 子类，必须显式捕获，不放行、不崩用例）。
    """
    if case.category != "derived":
        return None
    month_node = next((node for node in plan.nodes if node.action == "bills.monthly"), None)
    if month_node is None:
        return False
    year = _int_param(month_node, "year", DEFAULT_BOOKING_DATE.year)
    month = _int_param(month_node, "month", DEFAULT_BOOKING_DATE.month)
    try:
        derived = category_share_in_sandbox(gateway, DEMO_USER_ID, year, month, "购物")
    except (GatewayError, SandboxError, ScenarioRefused):
        return False
    return derived.operator == "ratio"


def _int_param(node: PlanNode, key: str, default: int) -> int:
    raw = node.params.get(key)
    if isinstance(raw, bool) or not isinstance(raw, int):
        return default
    return raw


def _context_today(context: Mapping[str, object]) -> date:
    value = context.get("today")
    if isinstance(value, date):
        return value
    raise ValueError("context 缺少受信任的 today（date）")


def _context_payees(context: Mapping[str, object]) -> tuple[Payee, ...]:
    value = context.get("payees")
    if isinstance(value, Sequence):
        return tuple(item for item in value if isinstance(item, Payee))
    return ()


def _context_selected(context: Mapping[str, object]) -> Mapping[str, object]:
    value = context.get("selected")
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    return {}


def _questions_message(planning: PlanningResult) -> str:
    return "；".join(question.prompt for question in planning.questions)


def _blocked_result(
    case: EvalCase, status: str, message: str, flags: frozenset[str], started: float
) -> CaseResult:
    return CaseResult(
        case=case,
        status=status,
        blocked=True,
        level=None,
        nodes=(),
        net_amount_cents=0,
        irreversibles=(),
        context_flags=flags,
        blocked_object=False,
        derived_released=False if case.category == "derived" else None,
        elapsed_ms=_elapsed_ms(started),
        message=message,
    )


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)

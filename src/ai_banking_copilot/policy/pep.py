"""策略执行点（PEP）：裁决门 + 参数强校验 + taint 校验 + 执行门 + 审计落链。

硬约束（H2 / H3 / PRD §5.1）：
- **PEP 无裁决不执行**：没有 `Decision` 或级别 ≥ `MANUAL`（L3/L4）一律拒绝。
- **绿色只允许纯查询**：`GREEN` + 非纯查询工具 = 拒绝（防资金 / 状态变更溜进绿色）。
- **参数入口唯一**：`ToolRegistry.validate_params(call)` 是唯一参数门，
  返回**去 taint 包装后的归一值**（`dict[str, object]`）；
  PEP 只消费该返回值、不直接读 `call.params`。控制槽裸值一律被 `ForbiddenParamError` 拒绝（H3）。
- **执行门（双层叠加）**：**计划级**冻结校验由 `policy.gate.assert_frozen(plan, cfhash)` 把关
  （06 在确认记录里同时保存 `Plan` 与 `cfhash`）；**节点级**成员资格校验由本模块
  `_assert_member(call, plan)` 承担——`call` 必须属于**已确认的冻结计划**。确认后控制槽参数
  被改 ⇒ 不再是计划成员 ⇒ `StalePlanError`（防 TOCTOU / 越权节点）。
- **熔断只加严**：熔断打开时锁定非纯查询操作，**纯查询不受影响**。
- 本模块是**写操作的唯一通道**；`policy/` 之外不得直接调用 `CoreBank`。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import date

from ai_banking_copilot.contracts.authorization import Decision, Level
from ai_banking_copilot.contracts.plan import (
    AnchorSource,
    IntentRef,
    Plan,
    PlanNode,
    cfhash,
)
from ai_banking_copilot.contracts.taint import Slot, SlotKind, Trust, Value
from ai_banking_copilot.gateway.core_bank import DEFAULT_BOOKING_DATE, CoreBank, WriteReceipt
from ai_banking_copilot.gateway.errors import GatewayError
from ai_banking_copilot.gateway.facts import FactGateway
from ai_banking_copilot.gateway.hashing import canonical_json
from ai_banking_copilot.policy.audit import KIND_EXECUTION, AuditEvent, AuditLog
from ai_banking_copilot.policy.breaker import CircuitBreaker
from ai_banking_copilot.policy.gate import StalePlanError
from ai_banking_copilot.tools.registry import ForbiddenParamError, ToolCall, ToolRegistry, ToolSpec

PEP_ANCHOR_ID = "pep-execution-gate"
"""执行门节点的规范锚点 id：让"确认时冻结"与"执行时复核"算出同一哈希。"""

@dataclass(frozen=True)
class WriteContext:
    """写处理器的执行上下文（**刻意最小**：不含审计 / 熔断 / 任何检查入口）。

    `params` 是 `ToolRegistry.validate_params(call)` 的**去 taint 归一值**——PEP 的唯一参数入口。
    """

    core_bank: CoreBank
    params: dict[str, object]
    spec: ToolSpec
    idempotency_key: str


WriteHandler = Callable[[WriteContext], WriteReceipt]
"""单个写动作的处理器：**只负责产生该动作的副作用**（返回 `CoreBank` 回执）。

安全边界（核心设计红线）：处理器只拿到产生副作用所需的最小面——
`CoreBank` + 归一化参数 + 动作契约 + 幂等键。计划成员资格（`_assert_member`）、
控制槽零信任（`_assert_taint`）、执行后出处回读（`_verify_receipt`）、收尾与审计
（`_finish`）**全部留在 `Pep` 的包装流程里**，处理器没有机会绕过任何检查。

2026-10-08（`docs/已修复清单-2026-10-08.md` 步骤 1.5）：由**各场景模块自带**、组合根汇入，
取代原先 `policy/pep.py` 内逐动作的 `if/elif` 代码分支。
"""


@dataclass(frozen=True)
class QueryContext:
    """读处理器的执行上下文（与 `WriteContext` 对称，**刻意最小**）。

    `params` 是 `ToolRegistry.validate_params(call)` 的**去 taint 归一值**——PEP 的唯一参数入口。
    只给**只读网关**，读处理器拿不到 `CoreBank`，结构上不可能产生副作用。
    """

    gateway: FactGateway
    params: dict[str, object]
    spec: ToolSpec


@dataclass(frozen=True)
class QueryResult:
    """一次只读查询的产出（数值与实体**只能来自网关**，H1）。

    `record_ids` / `provenance` 是出处凭证；`detail` 是展示用明细，只做拼接、不生成事实。
    """

    record_ids: tuple[str, ...]
    provenance: tuple[tuple[str, str], ...]
    detail: str


QueryHandler = Callable[[QueryContext], QueryResult]
"""单个纯查询动作的处理器：**只负责回读事实**（不写、不裁决、不审计）。

与 `WriteHandler` 同一条接缝纪律：计划成员资格、控制槽零信任、收尾与审计全部留在
`Pep` 的包装流程里。

2026-10-08（`docs/已修复清单-2026-10-08.md` 步骤 4「残留」补做）：读分派同样由**各场景模块自带**，
取代 `_execute_query` 内逐动作的 `if/elif`——此后新增**任一**动作都不再需要改 `policy/pep.py`。
"""


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


def manifest_plan(call: ToolCall) -> Plan:
    """单节点冻结计划：供只执行一次调用的最小调用方与单测构造。

    节点参数取**原始值形态**（剥掉 taint 包装），与真实计划节点（`PlanNode.params` 存原始值）
    保持同一形态——否则成员资格比对会因包装差异而恒不相等。
    """
    return Plan(
        plan_id="pep-execution",
        nodes=(replace(manifest_node(call), params=_plain_params(call.params)),),
    )


def _plain_params(params: dict[str, object]) -> dict[str, object]:
    """剥掉 `Value` / `Slot` 包装，还原成计划节点存储的原始值形态。"""
    return {key: _plain(value) for key, value in params.items()}


def _plain(value: object) -> object:
    if isinstance(value, Slot):
        return value.value.raw
    if isinstance(value, Value):
        return value.raw
    return value


_CALLER_INJECTED_PARAMS = ("user_id",)
"""由受信任调用方在 `ToolCall` 层注入、且按设计**不进** `PlanNode.params` 的参数。

见 README 裁决 #16 / #20：会话层与评测链在 `ToolCall` 层补 `DEMO_USER_ID`，不改计划哈希。
成员资格比对须剔除它，否则所有写动作都会被误判为「不属于计划」。
"""


def _comparable_params(params: dict[str, object]) -> dict[str, object]:
    """成员资格比对视图：剔除调用方注入项后规范化（键序无关、日期归一）。"""
    canonical = _canonical_params(params)
    return {key: value for key, value in canonical.items() if key not in _CALLER_INJECTED_PARAMS}


def _member_key(action: str, params: dict[str, object]) -> str:
    """计划成员指纹：`动作 + 归一化参数`（调用方注入项不参与）。"""
    return canonical_json({"action": action, "params": _comparable_params(params)})


class Pep:
    """工具白名单执行器：裁决 → 校验 → 执行门 → 执行 → 出处 → 审计。"""

    def __init__(
        self,
        registry: ToolRegistry,
        gateway: FactGateway,
        core_bank: CoreBank,
        audit: AuditLog,
        breaker: CircuitBreaker,
        write_handlers: Mapping[str, WriteHandler],
        query_handlers: Mapping[str, QueryHandler],
    ) -> None:
        self._registry = registry
        self._gateway = gateway
        self._core_bank = core_bank
        self._audit = audit
        self._breaker = breaker
        self._write_handlers = dict(write_handlers)
        self._query_handlers = dict(query_handlers)

    def handled_actions(self) -> frozenset[str]:
        """PEP 实际**持有处理器**的动作集 = 已登记读处理器 ∪ 已登记写处理器。

        取代原先手写的 `HANDLED_ACTIONS` 常量（`docs/已修复清单-2026-10-08.md` 步骤 1.5）；
        2026-10-08 步骤 4「残留」补做后，读侧也由注册表派生，本方法改为**处理器键的并集**
        （此前是「注册表里的纯查询动作 ∪ 写处理器」，会把「有动作但没读处理器」谎报为可执行）。

        一致性断言见 `tests/policy/test_pep.py`（本测试自建注册表 ⊆ 处理器集）与
        `tests/scenarios/test_registry.py`（生产侧：处理器集 == 注册表动作集，双向）。
        """
        return frozenset(set(self._query_handlers) | set(self._write_handlers))

    def execute(
        self,
        call: ToolCall,
        decision: Decision,
        plan: Plan,
        idempotency_key: str,
    ) -> ExecResult:
        """执行一次已裁决的工具调用。任一前置条件不满足即 fail-closed。

        `plan` = **已确认的冻结计划**：节点级执行门据此校验「本次调用属于该计划」，
        与调用方在确认阶段做的计划级 `assert_frozen` 叠加（防 TOCTOU / 越权节点）。
        """
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
        self._assert_member(call.action, resolved, plan)
        frozen_hash = manifest_hash(call)
        if spec.is_pure_query:
            return self._execute_query(
                call, spec, resolved, decision, frozen_hash, idempotency_key
            )
        return self._execute_write(call, resolved, spec, decision, frozen_hash, idempotency_key)

    def _assert_member(self, action: str, resolved: dict[str, object], plan: Plan) -> None:
        """执行门（**节点级**成员资格）：本次调用必须属于**已确认的冻结计划**。

        判据：计划中存在节点与本次调用的 `action` 相同，且**归一化参数逐字相等**。
        确认后改动金额 / 收款人 / 账户 ⇒ 不再是计划成员 ⇒ `StalePlanError`（fail-closed，
        并计入熔断失败）。

        **唯一豁免**：受信任调用方在 `ToolCall` 层注入的 `user_id`（README 裁决 #16 / #20）
        不参与比对——它来自 `AppDeps.user_id`、不是用户输入，且按设计不进 `PlanNode.params`。
        """
        if not isinstance(plan, Plan) or not plan.nodes:
            self._breaker.record_failure()
            raise StalePlanError("执行门拒绝：缺少已确认的冻结计划")
        probe = _member_key(action, resolved)
        if any(_member_key(node.action, node.params) == probe for node in plan.nodes):
            return
        self._breaker.record_failure()
        raise StalePlanError(f"执行门拒绝：{action} 不属于已确认计划（节点成员资格校验失败）")

    def _execute_query(
        self,
        call: ToolCall,
        spec: ToolSpec,
        params: dict[str, object],
        decision: Decision,
        frozen_hash: str,
        idempotency_key: str,
    ) -> ExecResult:
        """读路径：按动作查 **读 handler 注册表**并执行（handler 由场景模块自带、组合根汇入）。

        未登记 ⇒ `NotAuthorizedError`（fail-closed，并计入熔断失败）。读处理器只拿到
        **只读网关** + 归一化参数 + 动作契约，拿不到 `CoreBank`，结构上不可能产生副作用。
        """
        handler = self._query_handlers.get(call.action)
        if handler is None:
            self._breaker.record_failure()
            raise NotAuthorizedError(f"未登记查询处理器：{call.action}")
        result = handler(QueryContext(gateway=self._gateway, params=params, spec=spec))
        return self._finish(
            call,
            decision,
            frozen_hash,
            idempotency_key,
            status="queried",
            record_ids=result.record_ids,
            provenance=result.provenance,
            detail=result.detail,
        )

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
        """按动作查 **handler 注册表**并执行（handler 由场景模块自带、组合根汇入）。

        未登记 ⇒ `NotAuthorizedError`（fail-closed，由 `_execute_write` 计入熔断失败）。
        处理器只在**这里**被调用：它拿到的是已通过全部前置检查的归一化参数与 `CoreBank`，
        不接触审计 / 熔断 / 执行门，故无法绕过 PEP 的包装流程。
        """
        handler = self._write_handlers.get(call.action)
        if handler is None:
            raise NotAuthorizedError(f"未登记执行处理器：{call.action}")
        return handler(
            WriteContext(
                core_bank=self._core_bank,
                params=params,
                spec=spec,
                idempotency_key=idempotency_key,
            )
        )

    def _verify_receipt(self, receipt: WriteReceipt) -> tuple[tuple[str, str], ...]:
        """执行后出处校验：回读回执记录并用 `FactGateway.verify` 比对哈希（H1）。"""
        results: list[tuple[str, str]] = []
        for record_id in receipt.record_ids:
            content_hash = _read_hash(self._gateway, record_id)
            if not self._gateway.verify(record_id, content_hash):
                raise GatewayError(f"执行后出处校验失败：{record_id}")
            results.append((record_id, content_hash))
        return tuple(results)

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


def require_str(params: dict[str, object], key: str) -> str:
    """取非空字符串参数；不满足即 `NotAuthorizedError`（fail-closed）。**写处理器公用**。"""
    value = params.get(key)
    if not isinstance(value, str) or not value:
        raise NotAuthorizedError(f"参数 {key} 必须是非空字符串")
    return value


def require_int(params: dict[str, object], key: str) -> int:
    """取整数参数（`bool` 不算整数）；不满足即 `NotAuthorizedError`。**写处理器公用**。"""
    value = params.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise NotAuthorizedError(f"参数 {key} 必须是整数")
    return value


def require_date(params: dict[str, object], key: str) -> date:
    """取日期参数（`date` / ISO 字符串 / 缺省基准日）；不满足即 `NotAuthorizedError`。"""
    value = params.get(key)
    if value is None:
        return DEFAULT_BOOKING_DATE
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        raise NotAuthorizedError(f"参数 {key} 必须是 ISO 日期字符串或 date")
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise NotAuthorizedError(f"参数 {key} 不是合法日期：{value}") from error


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
    if table == "collection_requests":
        return gateway.collection_request(primary_key).content_hash
    if table == "scheduled_transfers":
        return gateway.scheduled_transfer(primary_key).content_hash
    if table == "merchant_orders":
        return gateway.merchant_order(primary_key).content_hash
    raise GatewayError(f"未知记录表：{table}")


def money_cents(params: dict[str, object], spec: ToolSpec) -> int:
    """按 `spec.money_param` 取金额（整数 / 分），供写处理器包装成 `Money` 再调 `CoreBank`。"""
    if spec.money_param is None:
        raise NotAuthorizedError(f"动作 {spec.action} 未声明 money_param")
    return require_int(params, spec.money_param)

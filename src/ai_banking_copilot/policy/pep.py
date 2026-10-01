"""策略执行点（PEP）：裁决门 + 参数强校验 + taint 校验 + 执行门 + 审计落链。

硬约束（H2 / H3 / PRD §5.1）：
- **PEP 无裁决不执行**：没有 `Decision` 或级别 ≥ `MANUAL`（L3/L4）一律拒绝。
- **绿色只允许纯查询**：`GREEN` + 非纯查询工具 = 拒绝（防资金 / 状态变更溜进绿色）。
- **参数入口唯一**：`ToolRegistry.validate_params(call)` 是唯一参数门，
  返回**去 taint 包装后的归一值**（`dict[str, object]`）；
  PEP 只消费该返回值、不直接读 `call.params`。控制槽裸值一律被 `ForbiddenParamError` 拒绝（H3）。
- **执行门（双层叠加）**：本模块用 `manifest_hash(call)` 做**节点级**成员资格校验
  （`cfhash` 不匹配即拒）；**计划级**冻结校验由
  `policy.gate.assert_frozen(plan, cfhash(plan.nodes))` 把关（06 在确认记录里
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
        return self._execute_write(call, resolved, spec, decision, frozen_hash, idempotency_key)

    def _execute_query(
        self,
        call: ToolCall,
        params: dict[str, object],
        decision: Decision,
        frozen_hash: str,
        idempotency_key: str,
    ) -> ExecResult:
        record_ids: tuple[str, ...]
        provenance: tuple[tuple[str, str], ...]
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
    raise GatewayError(f"未知记录表：{table}")


def _money_cents(params: dict[str, object], spec: ToolSpec) -> int:
    """按 `ToolSpec.money_param` 取金额（整数，单位分），供 PEP 包装成 `Money` 再调 `CoreBank`。"""
    if spec.money_param is None:
        raise NotAuthorizedError(f"动作 {spec.action} 未声明 money_param")
    return _require_int(params, spec.money_param)

"""Pre-Tool Hook 验收：写操作必须经服务端确认凭据，只读动作不得被误拦。

所有用例只跑**模拟银行**（`CoreBank` + 播种库），不触碰真实资金。
「写服务未被调用」以 `WriteSpy` 记录写处理器调用为准——处理器是进入 `CoreBank` 前的最后一层。
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from ai_banking_copilot.contracts.authorization import Decision, Level, ReasonCode
from ai_banking_copilot.contracts.taint import Trust, Value
from ai_banking_copilot.gateway.core_bank import DEFAULT_BOOKING_DATE, CoreBank, WriteReceipt
from ai_banking_copilot.gateway.errors import BlockedPayeeError
from ai_banking_copilot.gateway.facts import FactGateway
from ai_banking_copilot.gateway.seed import DEMO_USER_ID
from ai_banking_copilot.policy.audit import KIND_EXECUTION, KIND_TOOL_HOOK, AuditLog
from ai_banking_copilot.policy.breaker import CircuitBreaker
from ai_banking_copilot.policy.execution import ExecutionContext
from ai_banking_copilot.policy.gate import StalePlanError
from ai_banking_copilot.policy.pep import (
    Pep,
    WriteContext,
    WriteHandler,
    manifest_hash,
    manifest_plan,
)
from ai_banking_copilot.policy.tool_hook import (
    GrantLedger,
    HookStatus,
    PreToolHook,
    ToolClass,
    ToolHookBlocked,
    classify,
)
from ai_banking_copilot.scenarios.registry import QUERY_HANDLERS, WRITE_HANDLERS
from ai_banking_copilot.tools.registry import (
    ParamValidationError,
    ToolCall,
    ToolRegistry,
    ToolSpec,
)


class WriteSpy:
    """包裹全部写处理器并记录**真实被调用**的动作名。"""

    def __init__(self) -> None:
        self.actions: list[str] = []

    def handlers(self) -> dict[str, WriteHandler]:
        return {
            action: _recorder(self, action, handler)
            for action, handler in WRITE_HANDLERS.items()
        }


def _recorder(spy: WriteSpy, action: str, handler: WriteHandler) -> WriteHandler:
    def record(context: WriteContext) -> WriteReceipt:
        spy.actions.append(action)
        return handler(context)

    return record


@pytest.fixture
def writes() -> WriteSpy:
    return WriteSpy()


@pytest.fixture
def pep(
    registry: ToolRegistry,
    gateway: FactGateway,
    core_bank: CoreBank,
    audit: AuditLog,
    breaker: CircuitBreaker,
    writes: WriteSpy,
) -> Pep:
    """与生产同构的 PEP，只把写处理器换成带计数的一层。"""
    return Pep(registry, gateway, core_bank, audit, breaker, writes.handlers(), QUERY_HANDLERS)


def _trusted(value: object) -> Value[object]:
    return Value(value, Trust.TRUSTED)


def _decision(level: Level) -> Decision:
    reasons = (ReasonCode.QUERY_ONLY,) if level is Level.GREEN else (ReasonCode.STATE_CHANGE_SMALL,)
    return Decision(level=level, reasons=reasons)


def _transfer_call(amount_cents: int = 50000, payee_id: str = "p_lina") -> ToolCall:
    return ToolCall(
        action="transfer.create",
        params={
            "user_id": _trusted(DEMO_USER_ID),
            "account_id": _trusted("acc_savings"),
            "payee_id": _trusted(payee_id),
            "amount_cents": _trusted(amount_cents),
            "booked_on": _trusted(DEFAULT_BOOKING_DATE.isoformat()),
        },
    )


def _subscribe_call(amount_cents: int = 100) -> ToolCall:
    return ToolCall(
        action="wealth.subscribe",
        params={
            "user_id": _trusted(DEMO_USER_ID),
            "account_id": _trusted("acc_savings"),
            "product_id": _trusted("wp_cash"),
            "amount_cents": _trusted(amount_cents),
        },
    )


def _confirm(pep: Pep, call: ToolCall, level: Level, **kwargs: object) -> ExecutionContext:
    """模拟「用户在会话里确认后」的服务端签发（真实路径见 `web/session.py::_execute`）。"""
    plan = manifest_plan(call)
    return pep.issue_write_grant(
        call,
        _decision(level),
        plan,
        approved_level=level,
        strong_verified=level >= Level.RED,
        **kwargs,  # type: ignore[arg-type]
    )


def _hook_payloads(audit: AuditLog) -> list[dict[str, object]]:
    return [event.payload for event in audit.events() if event.kind == KIND_TOOL_HOOK]


def _grant(
    ledger: GrantLedger,
    *,
    action: str = "transfer.create",
    member: str = "m",
    session_id: str = "session-a",
    execution_id: str = "attempt-1",
    approved_level: Level = Level.RED,
) -> str:
    return ledger.issue(
        session_id=session_id,
        execution_id=execution_id,
        user_id=DEMO_USER_ID,
        plan_id="p",
        plan_hash="h",
        action=action,
        member=member,
        approved_level=approved_level,
        strong_verified=True,
    ).grant_id


def _authorize(
    hook: PreToolHook, spec: ToolSpec, grant_id: str, **overrides: object
) -> object:
    kwargs: dict[str, object] = {
        "member": "m",
        "plan_id": "p",
        "plan_hash": "h",
        "approved_level": Level.RED,
        "grant_id": grant_id,
        "user_id": DEMO_USER_ID,
        "session_id": "session-a",
        "execution_id": "attempt-1",
    }
    kwargs.update(overrides)
    return hook.authorize(spec, **kwargs)  # type: ignore[arg-type]


# 1. 模型直接请求转账：写服务未被调用


def test_direct_transfer_is_blocked_before_the_write_service(
    pep: Pep, gateway: FactGateway, writes: WriteSpy, audit: AuditLog
) -> None:
    call = _transfer_call()
    plan = manifest_plan(call)
    before = gateway.account("acc_savings").payload.balance.cents

    with pytest.raises(ToolHookBlocked) as error:
        pep.execute(call, _decision(Level.RED), plan, "direct-transfer")

    assert error.value.status is HookStatus.CONFIRMATION_REQUIRED
    assert error.value.action == "transfer.create"
    assert writes.actions == []
    assert gateway.account("acc_savings").payload.balance.cents == before
    assert not [event for event in audit.events() if event.kind == KIND_EXECUTION]

    payloads = _hook_payloads(audit)
    assert len(payloads) == 1
    assert payloads[0]["tool"] == "transfer.create"
    assert payloads[0]["op_type"]
    assert payloads[0]["tool_class"] == ToolClass.FINANCIAL_WRITE.value
    assert payloads[0]["plan_id"] == plan.plan_id
    assert payloads[0]["confirmation_state"] == HookStatus.CONFIRMATION_REQUIRED.value
    assert payloads[0]["result"] == "blocked"
    assert "amount_cents" not in payloads[0]
    assert audit.verify_chain() is True


# 2. 模型直接请求申购：写服务未被调用


def test_direct_wealth_purchase_is_blocked_before_the_write_service(
    pep: Pep, gateway: FactGateway, writes: WriteSpy
) -> None:
    call = _subscribe_call()
    plan = manifest_plan(call)
    before = gateway.account("acc_savings").payload.balance.cents

    with pytest.raises(ToolHookBlocked) as error:
        pep.execute(call, _decision(Level.RED), plan, "direct-subscribe")

    assert error.value.status is HookStatus.CONFIRMATION_REQUIRED
    assert error.value.action == "wealth.subscribe"
    assert writes.actions == []
    assert gateway.account("acc_savings").payload.balance.cents == before


# 3. 只读查询与推荐不被误拦


def test_wealth_query_and_subscription_recognition_are_not_intercepted(
    pep: Pep, registry: ToolRegistry, writes: WriteSpy, audit: AuditLog
) -> None:
    assert classify(registry.get("wealth.list")) is ToolClass.READ_ONLY
    assert classify(registry.get("wealth.subscribe")) is ToolClass.FINANCIAL_WRITE
    assert classify(registry.get("subscription.list")) is ToolClass.READ_ONLY
    assert classify(registry.get("subscription.cancel")) is ToolClass.FINANCIAL_WRITE

    products = ToolCall(action="wealth.list", params={})
    result = pep.execute(
        products, _decision(Level.GREEN), manifest_plan(products), "wealth-query"
    )
    assert result.status == "queried"
    assert "只理财产品" in result.message

    subs = ToolCall(action="subscription.list", params={"user_id": _trusted(DEMO_USER_ID)})
    subs_result = pep.execute(subs, _decision(Level.GREEN), manifest_plan(subs), "subs")
    assert subs_result.status == "queried"

    assert writes.actions == []
    assert _hook_payloads(audit) == []


def test_hook_lets_read_only_actions_through(registry: ToolRegistry, audit: AuditLog) -> None:
    hook = PreToolHook(ledger=GrantLedger(), audit=audit)
    assert (
        hook.authorize(
            registry.get("wealth.list"),
            member="m",
            plan_id="",
            plan_hash="",
            approved_level=Level.GREEN,
            grant_id="",
            user_id=DEMO_USER_ID,
            session_id="",
            execution_id="",
        )
        is None
    )


# 4. 没有确认的计划不能执行


def test_hand_built_context_without_a_grant_cannot_execute(pep: Pep, writes: WriteSpy) -> None:
    call = _transfer_call()
    plan = manifest_plan(call)
    forged = ExecutionContext(
        execution_id="forged",
        frozen_hash=manifest_hash(call),
        user_id=DEMO_USER_ID,
        today=DEFAULT_BOOKING_DATE,
        approved_level=Level.RED,
        strong_verified=True,
    )

    with pytest.raises(ToolHookBlocked) as error:
        pep.execute(call, _decision(Level.RED), plan, "forged", execution=forged)

    assert error.value.status is HookStatus.CONFIRMATION_REQUIRED
    assert writes.actions == []


def test_made_up_grant_id_cannot_execute(pep: Pep, writes: WriteSpy) -> None:
    call = _transfer_call()
    plan = manifest_plan(call)
    forged = ExecutionContext(
        execution_id="forged",
        frozen_hash=manifest_hash(call),
        user_id=DEMO_USER_ID,
        today=DEFAULT_BOOKING_DATE,
        approved_level=Level.RED,
        strong_verified=True,
        grant_id="forged-grant-id",
    )

    with pytest.raises(ToolHookBlocked) as error:
        pep.execute(call, _decision(Level.RED), plan, "forged-grant", execution=forged)

    assert error.value.status is HookStatus.CONFIRMATION_REQUIRED
    assert writes.actions == []


def test_incomplete_params_are_rejected_before_a_grant_exists(pep: Pep, writes: WriteSpy) -> None:
    """关键参数不完整 ⇒ 先澄清：凭据发不出来，也就没有可执行的模糊计划。"""
    call = ToolCall(action="transfer.create", params={"user_id": _trusted(DEMO_USER_ID)})
    with pytest.raises(ParamValidationError):
        pep.issue_write_grant(call, _decision(Level.RED), manifest_plan(call))
    assert writes.actions == []


# 5. 确认后修改关键参数：原确认失效


def test_changing_the_amount_after_confirmation_invalidates_the_grant(
    pep: Pep, gateway: FactGateway, writes: WriteSpy
) -> None:
    approved = _transfer_call(amount_cents=50000)
    plan = manifest_plan(approved)
    grant = _confirm(pep, approved, Level.RED)
    before = gateway.account("acc_savings").payload.balance.cents

    tampered = _transfer_call(amount_cents=60000)
    with pytest.raises(StalePlanError):
        pep.execute(tampered, _decision(Level.RED), plan, "tampered", execution=grant)

    assert writes.actions == []
    assert gateway.account("acc_savings").payload.balance.cents == before


def test_grant_bound_to_another_session_is_rejected(pep: Pep, writes: WriteSpy) -> None:
    call = _transfer_call()
    plan = manifest_plan(call)
    grant = _confirm(pep, call, Level.RED, session_id="session-a")
    other = replace(grant, session_id="session-b")

    with pytest.raises(ToolHookBlocked) as error:
        pep.execute(call, _decision(Level.RED), plan, "other-session", execution=other)

    assert error.value.status is HookStatus.AUTHORIZATION_REQUIRED
    assert writes.actions == []


# 6. 拒绝 / 过期 / 中断后不能执行


def test_grant_is_bound_to_one_execution_attempt(pep: Pep, writes: WriteSpy) -> None:
    """会话中断后新起一次执行，不能沿用上一次的确认。"""
    call = _transfer_call()
    plan = manifest_plan(call)
    grant = _confirm(pep, call, Level.RED, execution_id="attempt-1")
    resumed = replace(grant, execution_id="attempt-2")

    with pytest.raises(ToolHookBlocked) as error:
        pep.execute(call, _decision(Level.RED), plan, "resumed", execution=resumed)

    assert error.value.status is HookStatus.AUTHORIZATION_REQUIRED
    assert writes.actions == []


def test_expired_grant_is_rejected(registry: ToolRegistry, audit: AuditLog) -> None:
    now = [1000.0]
    ledger = GrantLedger(ttl_seconds=30.0, clock=lambda: now[0])
    hook = PreToolHook(ledger=ledger, audit=audit)
    grant_id = _grant(ledger)

    now[0] += 31.0
    with pytest.raises(ToolHookBlocked) as error:
        _authorize(hook, registry.get("transfer.create"), grant_id)

    assert error.value.status is HookStatus.EXPIRED
    assert _hook_payloads(audit)[-1]["confirmation_state"] == HookStatus.EXPIRED.value


# 7. 其他入口无法绕过：任何写动作都必须过同一道 Hook


def test_every_write_action_in_the_registry_is_gated(
    registry: ToolRegistry, audit: AuditLog
) -> None:
    hook = PreToolHook(ledger=GrantLedger(), audit=audit)
    write_actions = [name for name in registry.actions() if not registry.get(name).is_pure_query]
    assert write_actions

    for action in write_actions:
        with pytest.raises(ToolHookBlocked) as error:
            hook.authorize(
                registry.get(action),
                member="m",
                plan_id="p",
                plan_hash="h",
                approved_level=Level.RED,
                grant_id="",
                user_id=DEMO_USER_ID,
                session_id="session-a",
                execution_id="attempt-1",
            )
        assert error.value.status is HookStatus.CONFIRMATION_REQUIRED

    assert len(_hook_payloads(audit)) == len(write_actions)


def test_im_style_subscription_change_cannot_skip_the_hook(
    pep: Pep, gateway: FactGateway, writes: WriteSpy
) -> None:
    """IM 与 Web 共用 `Session` → `Pep.execute`，没有第二条写通道。"""
    call = ToolCall(
        action="subscription.cancel",
        params={"subscription_id": _trusted("sub_video"), "user_id": _trusted(DEMO_USER_ID)},
    )
    with pytest.raises(ToolHookBlocked) as error:
        pep.execute(call, _decision(Level.YELLOW), manifest_plan(call), "im-cancel")

    assert error.value.status is HookStatus.CONFIRMATION_REQUIRED
    assert writes.actions == []
    assert gateway.subscription("sub_video").payload.status == "active"


# 8. 重复调用不产生重复交易


def test_grant_is_single_use(registry: ToolRegistry, audit: AuditLog) -> None:
    ledger = GrantLedger()
    hook = PreToolHook(ledger=ledger, audit=audit)
    spec = registry.get("transfer.create")
    grant_id = _grant(ledger)

    assert _authorize(hook, spec, grant_id) is not None
    with pytest.raises(ToolHookBlocked) as error:
        _authorize(hook, spec, grant_id)

    assert error.value.status is HookStatus.CONFIRMATION_REQUIRED


def test_repeated_confirmed_call_replays_instead_of_writing_twice(
    pep: Pep, gateway: FactGateway, writes: WriteSpy
) -> None:
    call = _transfer_call()
    plan = manifest_plan(call)
    first = pep.execute(
        call, _decision(Level.RED), plan, "same-key", execution=_confirm(pep, call, Level.RED)
    )
    assert first.status == "settled"
    after_first = gateway.account("acc_savings").payload.balance.cents

    second = pep.execute(
        call, _decision(Level.RED), plan, "same-key", execution=_confirm(pep, call, Level.RED)
    )

    assert "幂等回放" in second.message
    assert gateway.account("acc_savings").payload.balance.cents == after_first


def test_reused_grant_cannot_pay_twice(
    pep: Pep, gateway: FactGateway, writes: WriteSpy
) -> None:
    call = _transfer_call()
    plan = manifest_plan(call)
    grant = _confirm(pep, call, Level.RED)
    pep.execute(call, _decision(Level.RED), plan, "first-key", execution=grant)
    after_first = gateway.account("acc_savings").payload.balance.cents

    with pytest.raises(ToolHookBlocked) as error:
        pep.execute(call, _decision(Level.RED), plan, "second-key", execution=grant)

    assert error.value.status is HookStatus.CONFIRMATION_REQUIRED
    assert gateway.account("acc_savings").payload.balance.cents == after_first
    assert writes.actions == ["transfer.create"]


# 9. 模型改用其他工具绕过：仍被拒绝


def test_grant_for_one_tool_cannot_authorize_another(pep: Pep, writes: WriteSpy) -> None:
    attempts = (
        (_subscribe_call(), Level.RED, "switch-to-subscribe"),
        (ToolCall(action="card.lock", params={"card_id": _trusted("card_debit")}), Level.YELLOW,
         "switch-to-card-lock"),
    )
    for call, level, key in attempts:
        grant = _confirm(pep, _transfer_call(), Level.RED)
        with pytest.raises(ToolHookBlocked) as error:
            pep.execute(call, _decision(level), manifest_plan(call), key, execution=grant)
        assert error.value.status is HookStatus.REJECTED

    assert writes.actions == []


def test_other_write_tools_without_a_grant_are_blocked(pep: Pep, writes: WriteSpy) -> None:
    for call, level in (
        (_subscribe_call(), Level.RED),
        (ToolCall(action="card.lock", params={"card_id": _trusted("card_debit")}), Level.YELLOW),
    ):
        with pytest.raises(ToolHookBlocked) as error:
            pep.execute(call, _decision(level), manifest_plan(call), f"no-grant:{call.action}")
        assert error.value.status is HookStatus.CONFIRMATION_REQUIRED

    assert writes.actions == []


# 10. 执行失败返回真实失败状态


def test_failed_write_reports_the_real_status(
    pep: Pep, audit: AuditLog, writes: WriteSpy, breaker: CircuitBreaker
) -> None:
    call = _transfer_call(payee_id="p_zhaoliu")
    plan = manifest_plan(call)

    with pytest.raises(BlockedPayeeError):
        pep.execute(
            call, _decision(Level.RED), plan, "blocked-payee",
            execution=_confirm(pep, call, Level.RED),
        )

    assert writes.actions == ["transfer.create"]
    assert breaker.failure_count() == 1
    execution = [event.payload for event in audit.events() if event.kind == KIND_EXECUTION]
    assert execution[-1]["status"] == "failed"
    assert execution[-1]["grant_id"]
    assert all(payload["status"] != "settled" for payload in execution)

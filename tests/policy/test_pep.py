import pytest

from ai_banking_copilot.contracts.authorization import Decision, Level, ReasonCode
from ai_banking_copilot.contracts.plan import Plan
from ai_banking_copilot.contracts.taint import Trust, Value
from ai_banking_copilot.gateway.core_bank import CoreBank
from ai_banking_copilot.gateway.errors import (
    BlockedPayeeError,
    GatewayError,
    InsufficientFundsError,
    UnknownSubscriptionError,
)
from ai_banking_copilot.gateway.facts import FactGateway
from ai_banking_copilot.gateway.models import Money
from ai_banking_copilot.gateway.seed import DEMO_USER_ID
from ai_banking_copilot.gateway.store import Store
from ai_banking_copilot.policy.audit import KIND_EXECUTION, AuditLog
from ai_banking_copilot.policy.breaker import CircuitBreaker
from ai_banking_copilot.policy.gate import StalePlanError
from ai_banking_copilot.policy.pep import ExecResult, NotAuthorizedError, Pep, manifest_plan
from ai_banking_copilot.tools.registry import (
    ForbiddenParamError,
    ToolCall,
    ToolRegistry,
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


def _execute_confirmed(
    pep: Pep, call: ToolCall, level: Level, key: str, plan: Plan | None = None,
) -> ExecResult:
    """以「用户已确认」的服务端路径执行写动作：先签发一次性凭据，再过 Pre-Tool Hook。

    凭据里的 `strong_verified` 与 `approved_level` 模拟会话层确认后的真实值：
    `RED` 及以上必须先过强验证，否则写前复核会要求重新授权。
    """
    decision = _decision(level)
    frozen = manifest_plan(call) if plan is None else plan
    return pep.execute(
        call, decision, frozen, key,
        execution=pep.issue_write_grant(
            call, decision, frozen,
            approved_level=level,
            strong_verified=level >= Level.RED,
        ),
    )


def test_pure_query_executes_and_is_audited(pep: Pep, audit: AuditLog) -> None:
    call = ToolCall(action="account.balance", params=_query_params())
    result = pep.execute(call, _decision(Level.GREEN), manifest_plan(call), "q1")
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
    pep.execute(call, _decision(Level.GREEN), manifest_plan(call), "q1")
    provenance = audit.events()[0].payload["provenance"]
    expected = gateway.account("acc_savings").content_hash
    assert provenance == [{"record_id": "accounts:acc_savings", "content_hash": expected}]


def test_execute_without_decision_is_rejected(pep: Pep) -> None:
    call = ToolCall(action="account.balance", params=_query_params())
    with pytest.raises(NotAuthorizedError, match="无裁决"):
        pep.execute(call, None, manifest_plan(call), "k1")  # type: ignore[arg-type]


def test_manual_and_deny_levels_are_rejected(pep: Pep) -> None:
    call = ToolCall(action="transfer.create", params=_transfer_params())
    for level in (Level.MANUAL, Level.DENY):
        with pytest.raises(NotAuthorizedError, match="级别不足"):
            pep.execute(call, _decision(level), manifest_plan(call), f"k-{level.name}")


def test_green_cannot_execute_state_change(pep: Pep) -> None:
    call = ToolCall(action="transfer.create", params=_transfer_params())
    with pytest.raises(NotAuthorizedError, match="纯查询"):
        pep.execute(call, _decision(Level.GREEN), manifest_plan(call), "k1")


def test_unknown_action_is_rejected(pep: Pep) -> None:
    call = ToolCall(action="payee.delete", params={})
    with pytest.raises(UnknownActionError):
        pep.execute(call, _decision(Level.RED), manifest_plan(call), "k1")


def test_call_outside_the_frozen_plan_is_rejected(pep: Pep) -> None:
    """节点级执行门：`call` 必须属于**已确认的冻结计划**（原「单节点自证哈希」已废除）。"""
    approved = ToolCall(action="transfer.create", params=_transfer_params())
    outsider = ToolCall(action="card.lock", params={"card_id": _trusted("card_debit")})
    with pytest.raises(StalePlanError, match="不属于已确认计划"):
        pep.execute(outsider, _decision(Level.YELLOW), manifest_plan(approved), "k1")


def test_missing_frozen_plan_is_rejected(pep: Pep) -> None:
    call = ToolCall(action="transfer.create", params=_transfer_params())
    with pytest.raises(StalePlanError, match="缺少已确认的冻结计划"):
        pep.execute(call, _decision(Level.RED), None, "k1")  # type: ignore[arg-type]


def test_untrusted_control_slot_value_is_rejected(pep: Pep) -> None:
    call = ToolCall(
        action="account.balance",
        params={"account_id": Value("acc_savings", Trust.UNVERIFIED)},
    )
    with pytest.raises(ForbiddenParamError, match="unverified"):
        pep.execute(call, _decision(Level.GREEN), manifest_plan(call), "k1")


def test_open_breaker_blocks_writes_but_not_queries(pep: Pep, breaker: CircuitBreaker) -> None:
    breaker.lock()
    query = ToolCall(action="account.balance", params=_query_params())
    result = pep.execute(query, _decision(Level.GREEN), manifest_plan(query), "q1")
    assert result.status == "queried"
    write = ToolCall(action="transfer.create", params=_transfer_params())
    with pytest.raises(NotAuthorizedError, match="熔断"):
        pep.execute(write, _decision(Level.RED), manifest_plan(write), "k1")


def test_repeated_rejections_open_the_breaker(pep: Pep, breaker: CircuitBreaker) -> None:
    call = ToolCall(action="transfer.create", params=_transfer_params())
    for index in range(3):
        with pytest.raises(NotAuthorizedError):
            pep.execute(call, _decision(Level.DENY), manifest_plan(call), f"k{index}")
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
    result = pep.execute(call, _decision(Level.GREEN), manifest_plan(call), "q2")
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
    result = pep.execute(call, _decision(Level.GREEN), manifest_plan(call), "q3")
    assert result.status == "queried"
    assert "购物" in result.message


def test_transfer_settles_and_debits_balance(pep: Pep, rw_store: Store) -> None:
    call = ToolCall(action="transfer.create", params=_transfer_params())
    result = _execute_confirmed(pep, call, Level.RED, "k1")
    assert result.status == "settled"
    assert result.record_ids[0].startswith("transactions:")
    rows = rw_store.query("SELECT balance_cents FROM accounts WHERE account_id = 'acc_savings'")
    assert rows[0]["balance_cents"] == 12843000 - 50000


def test_write_records_verified_provenance(
    pep: Pep, audit: AuditLog, gateway: FactGateway
) -> None:
    call = ToolCall(action="transfer.create", params=_transfer_params())
    result = _execute_confirmed(pep, call, Level.RED, "k1")
    provenance = audit.events()[0].payload["provenance"]
    assert len(provenance) == 1
    entry = provenance[0]
    assert entry["record_id"] == result.record_ids[0]
    assert gateway.verify(entry["record_id"], entry["content_hash"]) is True


def test_idempotent_replay_returns_same_receipt(pep: Pep, rw_store: Store) -> None:
    call = ToolCall(action="transfer.create", params=_transfer_params())
    frozen = manifest_plan(call)
    first = _execute_confirmed(pep, call, Level.RED, "k1", frozen)
    second = _execute_confirmed(pep, call, Level.RED, "k1", frozen)
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
        pep.execute(tampered, _decision(Level.RED), manifest_plan(approved), "k1")


def test_subscription_cancel_updates_status(pep: Pep, gateway: FactGateway) -> None:
    call = ToolCall(
        action="subscription.cancel",
        params={
            "subscription_id": _trusted("sub_video"),
            "user_id": _trusted(DEMO_USER_ID),
        },
    )
    result = _execute_confirmed(pep, call, Level.YELLOW, "k1")
    assert result.status == "cancelled"
    assert result.external_status == "not_integrated"
    assert result.previous_status == "active"
    assert gateway.subscription("sub_video").payload.status == "cancelled"


def test_subscription_cancel_refuses_another_users_subscription(
    pep: Pep, rw_store: Store
) -> None:
    """G-01：PEP 只透传服务端身份，底层双条件命中失败 ⇒ 拒绝且不改他人记录。"""
    with rw_store.transaction() as conn:
        conn.execute("INSERT INTO users VALUES ('u_other', '其他用户', 'R3')")
        conn.execute(
            "INSERT INTO subscriptions(subscription_id, user_id, merchant, amount_cents,"
            " period, next_charge_on, status) VALUES(?, ?, ?, ?, ?, ?, ?)",
            ("sub_other", "u_other", "他人商户", 1000, "monthly", "2026-10-20", "active"),
        )
    call = ToolCall(
        action="subscription.cancel",
        params={
            "subscription_id": _trusted("sub_other"),
            "user_id": _trusted(DEMO_USER_ID),
        },
    )
    with pytest.raises(UnknownSubscriptionError):
        _execute_confirmed(pep, call, Level.YELLOW, "k_other")
    rows = rw_store.query(
        "SELECT status FROM subscriptions WHERE subscription_id = 'sub_other'"
    )
    assert rows[0]["status"] == "active"


def test_freeze_then_unfreeze(pep: Pep, gateway: FactGateway) -> None:
    freeze = ToolCall(
        action="funds.freeze",
        params={
            "account_id": _trusted("acc_savings"),
            "amount_cents": _trusted(100000),
            "reason": _trusted("生日预算"),
        },
    )
    held = _execute_confirmed(pep, freeze, Level.RED, "k1")
    hold_id = held.record_ids[0].split(":", 1)[1]
    assert gateway.hold(hold_id).payload.status == "active"
    unfreeze = ToolCall(action="funds.release", params={"hold_id": _trusted(hold_id)})
    released = _execute_confirmed(pep, unfreeze, Level.YELLOW, "k2")
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
        _execute_confirmed(pep, call, Level.RED, "k1")


def test_write_failure_records_audit_and_breaker_failure(
    pep: Pep, audit: AuditLog, breaker: CircuitBreaker
) -> None:
    call = ToolCall(action="transfer.create", params=_transfer_params(amount_cents=99999999))
    with pytest.raises(InsufficientFundsError):
        _execute_confirmed(pep, call, Level.RED, "k1")
    assert breaker.failure_count() == 1
    assert audit.events()[-1].payload["status"] == "failed"
    assert audit.verify_chain() is True


def test_blocked_payee_failure_is_recorded(pep: Pep, breaker: CircuitBreaker) -> None:
    params = _transfer_params()
    params["payee_id"] = _trusted("p_zhaoliu")
    call = ToolCall(action="transfer.create", params=params)
    with pytest.raises(BlockedPayeeError):
        _execute_confirmed(pep, call, Level.RED, "k1")
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


def test_pep_handles_every_canonical_action(pep: Pep, registry: ToolRegistry) -> None:
    """本测试自建的 20 动作白名单**全部有处理器**（README 裁决 #14；03 独立于 02 的 catalog 验收）。

    步骤 1.5 / 步骤 4「残留」：读、写两侧的分派都已改为**注册表**，`Pep.handled_actions()`
    返回的是**处理器键的并集**（不再拿注册表里的纯查询动作充数）。故此处断言为**包含**
    ——生产侧「处理器集 == 注册表动作集」的双向严格断言在
    `tests/scenarios/test_registry.py::test_every_canonical_action_has_exactly_one_handler`。
    """
    assert len(CANONICAL_ACTIONS) == 20
    assert set(registry.actions()) == CANONICAL_ACTIONS
    assert set(registry.actions()) <= pep.handled_actions()


def test_registered_write_action_without_handler_is_rejected(
    registry: ToolRegistry,
    gateway: FactGateway,
    core_bank: CoreBank,
    audit: AuditLog,
    breaker: CircuitBreaker,
) -> None:
    """fail-closed：写动作在注册表内、但**未登记写处理器** ⇒ 拒绝执行（漏登记即红）。

    步骤 1.5 的核心红线：handler 只在 PEP 的全部前置检查之后被调用；注册表里却没有对应
    handler 时**不能**「什么都不做地放过」，必须显式拒绝。
    """
    pep = Pep(registry, gateway, core_bank, audit, breaker, {}, {})
    call = ToolCall(action="card.lock", params={"card_id": _trusted("card_debit")})
    with pytest.raises(NotAuthorizedError, match="未登记执行处理器"):
        _execute_confirmed(pep, call, Level.YELLOW, "k1")
    assert breaker.failure_count() == 1


def test_registered_query_action_without_handler_is_rejected(
    registry: ToolRegistry,
    gateway: FactGateway,
    core_bank: CoreBank,
    audit: AuditLog,
    breaker: CircuitBreaker,
) -> None:
    """fail-closed：纯查询动作在注册表内、但**未登记读处理器** ⇒ 拒绝执行（对称于写侧）。"""
    pep = Pep(registry, gateway, core_bank, audit, breaker, {}, {})
    call = ToolCall(action="account.balance", params=_query_params())
    with pytest.raises(NotAuthorizedError, match="未登记查询处理器"):
        pep.execute(call, _decision(Level.GREEN), manifest_plan(call), "q1")
    assert breaker.failure_count() == 1
    assert audit.events() == ()


def test_transfer_schedule_reuses_transfer_semantics(pep: Pep, rw_store: Store) -> None:
    call = ToolCall(action="transfer.schedule", params=_transfer_params())
    result = _execute_confirmed(pep, call, Level.RED, "k2")
    assert result.status == "settled"
    rows = rw_store.query("SELECT COUNT(*) AS n FROM transactions WHERE booked_on = '2026-10-01'")
    assert rows[0]["n"] == 1


def test_card_lock_and_unlock_update_status(pep: Pep, gateway: FactGateway) -> None:
    lock = ToolCall(action="card.lock", params={"card_id": _trusted("card_debit")})
    locked = _execute_confirmed(pep, lock, Level.YELLOW, "k3")
    assert locked.status == "locked"
    assert gateway.card("card_debit").payload.status == "locked"
    unlock = ToolCall(action="card.unlock", params={"card_id": _trusted("card_debit")})
    unlocked = _execute_confirmed(pep, unlock, Level.RED, "k4")
    assert unlocked.status == "active"
    assert gateway.card("card_debit").payload.status == "active"


def test_card_apply_virtual_creates_virtual_card(pep: Pep, gateway: FactGateway) -> None:
    call = ToolCall(
        action="card.apply_virtual",
        params={"user_id": _trusted(DEMO_USER_ID), "daily_limit_cents": _trusted(300000)},
    )
    result = _execute_confirmed(pep, call, Level.YELLOW, "k5")
    assert result.status == "applied"
    card_id = result.record_ids[0].split(":", 1)[1]
    assert gateway.card(card_id).payload.card_type == "virtual"
    assert gateway.card(card_id).payload.daily_limit == Money(300000)


def test_card_set_limit_updates_limit(pep: Pep, gateway: FactGateway) -> None:
    call = ToolCall(
        action="card.set_limit",
        params={"card_id": _trusted("card_debit"), "daily_limit_cents": _trusted(1500000)},
    )
    result = _execute_confirmed(pep, call, Level.YELLOW, "k6")
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
    result = _execute_confirmed(pep, call, Level.YELLOW, "k7")
    assert result.status == "signed"
    subscription_id = result.record_ids[0].split(":", 1)[1]
    assert gateway.subscription(subscription_id).payload.merchant == "云盘会员"

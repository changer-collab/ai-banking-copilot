import pytest

from ai_banking_copilot.contracts.authorization import Decision, Level, ReasonCode
from ai_banking_copilot.contracts.taint import Trust, Value
from ai_banking_copilot.gateway.errors import (
    BlockedPayeeError,
    GatewayError,
    InsufficientFundsError,
)
from ai_banking_copilot.gateway.facts import FactGateway
from ai_banking_copilot.gateway.models import Money
from ai_banking_copilot.gateway.seed import DEMO_USER_ID
from ai_banking_copilot.gateway.store import Store
from ai_banking_copilot.policy.audit import KIND_EXECUTION, AuditLog
from ai_banking_copilot.policy.breaker import CircuitBreaker
from ai_banking_copilot.policy.gate import StalePlanError
from ai_banking_copilot.policy.pep import HANDLED_ACTIONS, NotAuthorizedError, Pep, manifest_hash
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

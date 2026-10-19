from datetime import date

import pytest

from ai_banking_copilot.gateway.core_bank import CoreBank
from ai_banking_copilot.gateway.errors import InsufficientFundsError
from ai_banking_copilot.gateway.facts import FactGateway
from ai_banking_copilot.gateway.models import Money
from ai_banking_copilot.gateway.preconditions import (
    PreconditionResult,
    PreconditionStatus,
    WritePreconditionError,
)
from ai_banking_copilot.gateway.seed import DEMO_USER_ID
from ai_banking_copilot.gateway.store import Store


def test_denied_callback_rolls_back_without_receipt(
    gateway: FactGateway, rw_store: Store
) -> None:
    core_bank = CoreBank(rw_store)
    before = gateway.account("acc_savings").payload.balance.cents

    def deny(_conn: object) -> PreconditionResult:
        return PreconditionResult(PreconditionStatus.REAUTHORIZE, level=2)

    with pytest.raises(WritePreconditionError):
        core_bank.transfer(
            user_id=DEMO_USER_ID,
            account_id="acc_savings",
            payee_id="p_lina",
            amount=Money(60_000),
            idempotency_key="precondition-denied",
            booked_on=date(2026, 10, 1),
            before_write=deny,
        )
    assert gateway.account("acc_savings").payload.balance.cents == before
    assert rw_store.query(
        "SELECT * FROM write_receipts WHERE idempotency_key = ?", ("precondition-denied",)
    ) == []


def test_body_failure_rolls_back_without_receipt(
    gateway: FactGateway, rw_store: Store
) -> None:
    core_bank = CoreBank(rw_store)
    before = gateway.account("acc_savings").payload.balance.cents

    def allow(_conn: object) -> PreconditionResult:
        return PreconditionResult(PreconditionStatus.ALLOW)

    with pytest.raises(InsufficientFundsError):
        core_bank.transfer(
            user_id=DEMO_USER_ID,
            account_id="acc_savings",
            payee_id="p_lina",
            amount=Money(before + 1),
            idempotency_key="body-failed",
            booked_on=date(2026, 10, 1),
            before_write=allow,
        )
    assert gateway.account("acc_savings").payload.balance.cents == before
    assert rw_store.query(
        "SELECT * FROM write_receipts WHERE idempotency_key = ?", ("body-failed",)
    ) == []

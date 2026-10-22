"""任务 6 验收：转账参数绑定（币种）与付款账户事实校验。

覆盖：
- 缺币种 / 币种与付款账户不一致 / 币种不可信 ⇒ 澄清（**不产出可执行计划**）；
- 币种进入节点参数 ⇒ 进入计划哈希（`cfhash`）；
- 到期派发按付款账户事实绑定币种，账户缺币种即拒绝派发。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from pathlib import Path

import pytest

from ai_banking_copilot.contracts.plan import (
    AnchorSource,
    IntentRef,
    Plan,
    PlanNode,
    cfhash,
)
from ai_banking_copilot.contracts.taint import Trust, Value
from ai_banking_copilot.gateway.core_bank import CoreBank
from ai_banking_copilot.gateway.facts import FactGateway
from ai_banking_copilot.gateway.models import Money
from ai_banking_copilot.gateway.seed import DEMO_USER_ID, seed_database
from ai_banking_copilot.gateway.store import Store, StoreMode, init_schema
from ai_banking_copilot.planning.llm import Intent
from ai_banking_copilot.scenarios.base import ClarificationRequired
from ai_banking_copilot.scenarios.transfer import TransferHandler, due_transfer_plan


def _seed(tmp_path: Path, *, currency: str = "CNY") -> Path:
    """建库 + 播种 + 覆写付款账户币种（`""` 表示账户缺币种）。"""
    path = tmp_path / f"params_{currency or 'blank'}.db"
    store = Store(path)
    init_schema(store)
    seed_database(store)
    with store.transaction() as conn:
        conn.execute("UPDATE accounts SET currency = ?", (currency,))
        conn.execute(
            "INSERT INTO accounts(account_id, user_id, kind, balance_cents, currency)"
            " VALUES('acc_usd', ?, 'current', 100000, 'USD')",
            (DEMO_USER_ID,),
        )
    store.close()
    return path


@contextmanager
def _gateway(path: Path) -> Iterator[FactGateway]:
    store = Store(path, StoreMode.READ_ONLY)
    try:
        yield FactGateway(store)
    finally:
        store.close()


def _intent(**overrides: object) -> Intent:
    slots: dict[str, object] = {
        "account": Value("acc_savings", Trust.TRUSTED),
        "payee": Value("p_lina", Trust.TRUSTED),
        "amount": Value(Money(50000), Trust.TRUSTED),
        "when": Value(date(2026, 10, 1), Trust.TRUSTED),
        "span": "转给李娜 500 元",
    }
    slots.update(overrides)
    return Intent(action="transfer.create", slots=slots, confidence=0.9, evidence="转给李娜 500 元")


def _build(gateway: FactGateway, **overrides: object) -> Plan:
    return TransferHandler().build([_intent(**overrides)], gateway)


def test_plan_binds_payer_account_currency(tmp_path: Path) -> None:
    """正常转账：币种由付款账户事实绑定并写入节点参数（进哈希 / 计划卡 / 写前复核）。"""
    with _gateway(_seed(tmp_path)) as gateway:
        plan = _build(gateway)
    assert plan.nodes[0].params["currency"] == "CNY"


def test_plan_binds_non_cny_account_currency(tmp_path: Path) -> None:
    """非 CNY 账户：币种同样以账户事实绑定（不写死人民币）。"""
    with _gateway(_seed(tmp_path)) as gateway:
        plan = _build(gateway, account=Value("acc_usd", Trust.TRUSTED))
    assert plan.nodes[0].params["currency"] == "USD"


def test_explicit_currency_conflicting_with_account_is_clarified(tmp_path: Path) -> None:
    with _gateway(_seed(tmp_path)) as gateway:
        with pytest.raises(ClarificationRequired) as error:
            _build(gateway, currency=Value("USD", Trust.TRUSTED))
    assert error.value.slot == "currency"


def test_explicit_currency_matching_account_is_bound_and_normalised(tmp_path: Path) -> None:
    with _gateway(_seed(tmp_path)) as gateway:
        plan = _build(gateway, currency=Value("cny", Trust.TRUSTED))
    assert plan.nodes[0].params["currency"] == "CNY"


def test_untrusted_currency_is_rejected(tmp_path: Path) -> None:
    with _gateway(_seed(tmp_path)) as gateway:
        with pytest.raises(ClarificationRequired):
            _build(gateway, currency=Value("USD", Trust.UNVERIFIED))


def test_missing_account_currency_cannot_be_planned(tmp_path: Path) -> None:
    """账户缺币种：先澄清，**不产出可执行计划**（不猜、不默认 CNY）。"""
    with _gateway(_seed(tmp_path, currency="")) as gateway:
        with pytest.raises(ClarificationRequired) as error:
            _build(gateway)
    assert error.value.slot == "currency"
    assert "缺少币种" in error.value.prompt


def test_unknown_payer_account_is_clarified(tmp_path: Path) -> None:
    with _gateway(_seed(tmp_path)) as gateway:
        with pytest.raises(ClarificationRequired) as error:
            _build(gateway, account=Value("acc_nope", Trust.TRUSTED))
    assert error.value.slot == "account"


def test_currency_participates_in_the_frozen_plan_hash() -> None:
    """仅币种不同 ⇒ 计划哈希不同 ⇒ 旧确认（旧 card_id / frozen_hash）必然失效。"""
    base: dict[str, object] = {
        "account_id": "acc_savings",
        "payee_id": "p_lina",
        "amount_cents": 50000,
        "booked_on": "2026-10-01",
    }

    def node(currency: str) -> PlanNode:
        return PlanNode(
            node_id="n1",
            action="transfer.create",
            params={**base, "currency": currency},
            intent=IntentRef(source=AnchorSource.EXPLICIT, anchor_id="a1"),
        )

    cny = Plan(plan_id="p", nodes=(node("CNY"),))
    usd = Plan(plan_id="p", nodes=(node("USD"),))
    assert cfhash(cny.nodes) != cfhash(usd.nodes)


def test_due_plan_binds_account_currency(tmp_path: Path) -> None:
    path = _seed(tmp_path, currency="CNY")
    writer = Store(path)
    try:
        CoreBank(writer).book_transfer(
            user_id=DEMO_USER_ID,
            account_id="acc_savings",
            payee_id="p_lina",
            amount=Money(10000),
            booked_on=date(2026, 10, 1),
            remark="",
            idempotency_key="due-currency",
        )
    finally:
        writer.close()
    with _gateway(path) as gateway:
        nodes = due_transfer_plan(gateway, date(2026, 10, 1))
    assert nodes[0].params["currency"] == "CNY"


def test_due_plan_rejects_account_without_currency(tmp_path: Path) -> None:
    path = _seed(tmp_path, currency="CNY")
    writer = Store(path)
    try:
        CoreBank(writer).book_transfer(
            user_id=DEMO_USER_ID,
            account_id="acc_savings",
            payee_id="p_lina",
            amount=Money(10000),
            booked_on=date(2026, 10, 1),
            remark="",
            idempotency_key="due-blank",
        )
        with writer.transaction() as conn:
            conn.execute("UPDATE accounts SET currency = '' WHERE account_id = 'acc_savings'")
    finally:
        writer.close()
    with _gateway(path) as gateway:
        with pytest.raises(ClarificationRequired) as error:
            due_transfer_plan(gateway, date(2026, 10, 1))
    assert error.value.slot == "currency"

"""生活事件资金来源核查（`web/life_funding.py`）：只提示、确定性、可复算。"""

from datetime import date

from ai_banking_copilot.gateway.models import Account, Money, WealthHolding, WealthProduct
from ai_banking_copilot.web.life_funding import (
    STATUS_LATER,
    STATUS_OK,
    STATUS_SHORT,
    STATUS_UNKNOWN,
    check,
    note,
)

TODAY = date(2026, 11, 1)
TRIGGER = date(2026, 11, 4)


class _Record:
    def __init__(self, payload: object) -> None:
        self.payload = payload


class _Gateway:
    """只实现核查用到的三个只读方法，避免把整库塞进单测。"""

    def __init__(
        self,
        balance_cents: int,
        holdings: tuple[WealthHolding, ...] = (),
        products: tuple[WealthProduct, ...] = (),
    ) -> None:
        self._accounts = (
            _Record(Account("acc_savings", "u_demo", "savings", Money(balance_cents), "CNY")),
        )
        self._holdings = tuple(_Record(item) for item in holdings)
        self._products = tuple(_Record(item) for item in products)

    def accounts_of(self, user_id: str) -> tuple[_Record, ...]:
        return self._accounts

    def wealth_holdings_of(self, user_id: str) -> tuple[_Record, ...]:
        return self._holdings

    def wealth_products(self) -> tuple[_Record, ...]:
        return self._products


def _holding(product_id: str, cents: int, purchased_on: date) -> WealthHolding:
    return WealthHolding(
        product_id=product_id, user_id="u_demo", amount=Money(cents),
        purchased_on=purchased_on,
    )


def _product(term_days: int) -> WealthProduct:
    return WealthProduct(
        product_id="wp_short", name="稳盈短期 A", risk_level="R2", term_days=term_days,
        yield_bp=260, min_amount=Money(10000),
    )


def test_available_balance_covers_the_plan() -> None:
    result = check(
        _Gateway(200000), "u_demo", amount_cents=100000, trigger_on=TRIGGER, today=TODAY,
    )
    assert result.status == STATUS_OK
    assert note(result) is None


def test_maturity_before_the_spend_day_is_covered_after_redemption() -> None:
    gateway = _Gateway(
        50000, (_holding("wp_short", 200000, date(2026, 10, 1)),), (_product(30),),
    )
    result = check(gateway, "u_demo", amount_cents=100000, trigger_on=TRIGGER, today=TODAY)
    # 起息 10-01 + 30 天 = 10-31，早于用钱日 11-04
    assert result.status == STATUS_LATER
    assert result.inflow_on == date(2026, 10, 31)
    assert note(result) is not None


def test_maturity_after_the_spend_day_is_a_conflict() -> None:
    gateway = _Gateway(
        50000, (_holding("wp_short", 200000, date(2026, 10, 31)),), (_product(30),),
    )
    result = check(gateway, "u_demo", amount_cents=100000, trigger_on=TRIGGER, today=TODAY)
    # 起息 10-31 + 30 天 = 11-30，晚于用钱日 11-04
    assert result.status == STATUS_SHORT
    assert "2026-11-30" in result.message


def test_missing_date_is_unknown_and_silent() -> None:
    result = check(
        _Gateway(200000), "u_demo", amount_cents=100000, trigger_on=None, today=TODAY,
    )
    assert result.status == STATUS_UNKNOWN
    assert note(result) is None

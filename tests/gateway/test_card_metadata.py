"""卡片展示与分卡限额字段：种子事实可读，缺失不被当成 0。"""

from __future__ import annotations

from ai_banking_copilot.gateway.facts import FactGateway
from ai_banking_copilot.gateway.models import Money


def test_debit_card_carries_display_and_channel_limits(gateway: FactGateway) -> None:
    card = gateway.card("card_debit").payload
    assert card.brand == "unionpay"
    assert card.expiry_month == 9
    assert card.expiry_year == 2029
    assert card.account_class == "Ⅰ"
    assert card.atm_limit == Money(500_000)
    assert card.transfer_limit == Money(2_000_000)
    assert card.purchase_limit == Money(2_000_000)
    assert card.overseas_limit == Money(500_000)
    assert card.credit_limit is None


def test_credit_card_carries_credit_and_cycle_limits(gateway: FactGateway) -> None:
    card = gateway.card("card_credit").payload
    assert card.brand == "mastercard"
    assert card.account_class is None
    assert card.credit_limit == Money(5_000_000)
    assert card.cycle_limit == Money(800_000)
    assert card.atm_limit is None
    assert card.temp_lock_until is None
    assert card.replaced_by_card_id is None

from datetime import date
from decimal import Decimal

from ai_banking_copilot.gateway.hashing import canonical_json, sha256_hex
from ai_banking_copilot.gateway.models import Account, Money


def test_canonical_json_is_key_order_independent() -> None:
    assert canonical_json({"b": 1, "a": 2}) == canonical_json({"a": 2, "b": 1})


def test_canonical_json_renders_money_and_date() -> None:
    assert canonical_json(Money(1234)) == '{"cents":1234}'
    assert canonical_json(date(2026, 10, 1)) == '"2026-10-01"'


def test_canonical_json_renders_nested_dataclass_in_cents() -> None:
    account = Account(account_id="acc", user_id="u", kind="savings", balance=Money(100))
    assert canonical_json(account) == (
        '{"account_id":"acc","balance":{"cents":100},"currency":"CNY",'
        '"kind":"savings","user_id":"u"}'
    )


def test_canonical_json_keeps_decimal_exact() -> None:
    assert canonical_json({"ratio": Decimal("0.25")}) == '{"ratio":"0.25"}'


def test_sha256_hex_matches_reference_vector() -> None:
    assert sha256_hex("") == (
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )

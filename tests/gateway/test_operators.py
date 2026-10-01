from decimal import Decimal

import pytest

from ai_banking_copilot.gateway.errors import OperatorError, UnknownOperatorError
from ai_banking_copilot.gateway.operators import (
    OPERATOR_NAMES,
    OPERATOR_WHITELIST,
    run_operator,
)


def test_whitelist_is_closed_and_named() -> None:
    assert OPERATOR_NAMES == ("sum", "avg", "ratio", "yoy", "mom")
    assert all(spec.description for spec in OPERATOR_WHITELIST)


def test_sum_keeps_all_sources() -> None:
    derived = run_operator(
        "sum",
        (("transactions:t1", Decimal("-3200")), ("transactions:t2", Decimal("-1580"))),
    )
    assert derived.value == Decimal("-4780")
    assert derived.sources == ("transactions:t1", "transactions:t2")


def test_avg_divides_by_count() -> None:
    derived = run_operator(
        "avg", (("transactions:t1", Decimal("100")), ("transactions:t2", Decimal("200")))
    )
    assert derived.value == Decimal("150")


def test_ratio_and_yoy_use_two_inputs() -> None:
    assert run_operator(
        "ratio", (("bills:b1", Decimal("25")), ("bills:b2", Decimal("100")))
    ).value == Decimal("0.25")
    assert run_operator(
        "yoy", (("bills:b1", Decimal("120")), ("bills:b2", Decimal("100")))
    ).value == Decimal("0.2")


def test_unknown_operator_is_rejected() -> None:
    with pytest.raises(UnknownOperatorError):
        run_operator("exec_shell", (("a:1", Decimal("1")),))


def test_zero_denominator_is_rejected() -> None:
    with pytest.raises(OperatorError, match="分母"):
        run_operator("mom", (("bills:b1", Decimal("10")), ("bills:b2", Decimal("0"))))


def test_inputs_must_carry_record_id() -> None:
    with pytest.raises(OperatorError, match="record_id"):
        run_operator("sum", (("", Decimal("1")),))


def test_empty_input_is_rejected() -> None:
    with pytest.raises(OperatorError, match="至少"):
        run_operator("sum", ())


def test_two_arity_operator_requires_exactly_two_inputs() -> None:
    with pytest.raises(OperatorError, match="恰好 2 个"):
        run_operator("ratio", (("a:1", Decimal("1")), ("a:2", Decimal("2")), ("a:3", Decimal("3"))))

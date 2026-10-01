import pytest

from ai_banking_copilot.contracts.plan import AnchorSource
from ai_banking_copilot.planning.anchors import (
    AnchorLedger,
    DerivedDepthError,
    UnknownAnchorError,
)


def test_grant_explicit_is_depth_zero_without_parent() -> None:
    ledger = AnchorLedger()
    anchor = ledger.resolve(ledger.grant_explicit("转给李娜"))
    assert anchor.source is AnchorSource.EXPLICIT
    assert anchor.depth == 0
    assert anchor.parent_id is None


def test_grant_approved_is_depth_zero() -> None:
    ledger = AnchorLedger()
    anchor = ledger.resolve(ledger.grant_approved("计划卡批准"))
    assert anchor.source is AnchorSource.USER_APPROVED
    assert anchor.depth == 0


def test_derive_from_explicit_has_depth_one() -> None:
    ledger = AnchorLedger()
    parent = ledger.grant_explicit("AA 总额 300 元")
    child = ledger.resolve(ledger.derive(parent, "均摊 300 / 3"))
    assert child.source is AnchorSource.DERIVED
    assert child.depth == 1
    assert child.parent_id == parent


def test_derive_from_approved_is_allowed() -> None:
    ledger = AnchorLedger()
    parent = ledger.grant_approved("计划卡批准总额 300 元")
    assert ledger.resolve(ledger.derive(parent, "均摊 300 / 3")).depth == 1


def test_derive_from_derived_is_rejected() -> None:
    ledger = AnchorLedger()
    parent = ledger.grant_explicit("AA 总额 300 元")
    child = ledger.derive(parent, "均摊 300 / 3")
    with pytest.raises(DerivedDepthError):
        ledger.derive(child, "再推导一层")


def test_resolve_unknown_anchor_is_rejected() -> None:
    with pytest.raises(UnknownAnchorError):
        AnchorLedger().resolve("a404")


def test_anchor_ids_are_deterministic_across_ledgers() -> None:
    first = AnchorLedger()
    second = AnchorLedger()
    assert first.grant_explicit("x") == second.grant_explicit("x") == "a1"
    assert first.grant_explicit("y") == "a2"


def test_anchors_returns_everything_in_insertion_order() -> None:
    ledger = AnchorLedger()
    ledger.grant_explicit("x")
    ledger.grant_approved("y")
    assert [anchor.anchor_id for anchor in ledger.anchors()] == ["a1", "a2"]

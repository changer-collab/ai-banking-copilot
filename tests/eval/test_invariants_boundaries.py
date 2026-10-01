import pytest

from ai_banking_copilot.contracts.authorization import Level
from ai_banking_copilot.eval.dataset import MUST_PASS_IDS, EvalCase
from ai_banking_copilot.eval.invariants import (
    NonDegeneracyError,
    assert_non_degenerate,
    check_invariants,
)
from ai_banking_copilot.eval.pipeline import CaseResult, NodeOutcome


def _node(
    node_id: str,
    action: str,
    level: Level | None,
    *,
    pure: bool = False,
    outflow: bool = False,
    daily: int = 0,
    blocked_object: bool = False,
) -> NodeOutcome:
    return NodeOutcome(
        node_id=node_id,
        action=action,
        level=level,
        is_pure_query=pure,
        is_outflow=outflow,
        daily_accumulated_cents=daily,
        object_blocked=blocked_object,
        executed_status="skipped",
    )


def _case(case_id: str, level: str, blocked: bool, category: str = "transfer") -> EvalCase:
    return EvalCase(case_id, f"指令 {case_id}", level, blocked, category)


def _result(
    case: EvalCase,
    level: Level | None,
    nodes: tuple[NodeOutcome, ...] = (),
    *,
    net: int = 0,
    irreversibles: tuple[str, ...] = (),
    flags: frozenset[str] = frozenset(),
    blocked_object: bool = False,
    blocked: bool = False,
) -> CaseResult:
    return CaseResult(
        case=case,
        status="blocked" if blocked else "planned",
        blocked=blocked,
        level=level,
        nodes=nodes,
        net_amount_cents=net,
        irreversibles=irreversibles,
        context_flags=flags,
        blocked_object=blocked_object,
        derived_released=None,
        elapsed_ms=1,
        message="fixture",
    )


def _full_coverage_results() -> tuple[tuple[EvalCase, ...], tuple[CaseResult, ...]]:
    """覆盖五级别 + 四个不变式各有可判定样本的最小集。"""
    cases = (
        _case("green", "GREEN", False, "bills"),
        _case("yellow", "YELLOW", False, "transfer"),
        _case("red", "RED", False, "cards"),
        _case("manual", "MANUAL", False, "injection"),
        _case("deny", "DENY", False, "transfer"),
    )
    results = (
        _result(
            cases[0],
            Level.GREEN,
            (_node("n1", "bills.monthly", Level.GREEN, pure=True),),
            net=0,
        ),
        _result(
            cases[1],
            Level.YELLOW,
            (_node("n1", "transfer.create", Level.YELLOW, outflow=True, daily=50000),),
            net=50000,
        ),
        _result(
            cases[2],
            Level.RED,
            (_node("n1", "card.report_lost", Level.RED),),
            irreversibles=("n1",),
        ),
        _result(
            cases[3],
            Level.MANUAL,
            (_node("n1", "transfer.create", Level.MANUAL, outflow=True, daily=50000),),
            flags=frozenset({"injection_detected"}),
        ),
        _result(
            cases[4],
            Level.DENY,
            (
                _node(
                    "n1",
                    "transfer.create",
                    Level.DENY,
                    outflow=True,
                    daily=30000,
                    blocked_object=True,
                ),
            ),
            blocked_object=True,
        ),
    )
    return cases, results


def test_assert_non_degenerate_passes_on_full_coverage() -> None:
    cases, results = _full_coverage_results()
    invariants = check_invariants(cases, results)
    assert_non_degenerate(results, invariants)


def test_assert_non_degenerate_rejects_missing_level() -> None:
    cases, results = _full_coverage_results()
    invariants = check_invariants(cases, results)
    with pytest.raises(NonDegeneracyError, match="缺少级别覆盖"):
        assert_non_degenerate(results[:-1], invariants)


def test_assert_non_degenerate_rejects_inert_invariant() -> None:
    case = _case("green", "GREEN", False, "bills")
    node = _node("n1", "bills.monthly", Level.GREEN, pure=True)
    results = (_result(case, Level.GREEN, (node,), net=0),)
    invariants = check_invariants((case,), results)
    with pytest.raises(NonDegeneracyError, match="不变式恒真"):
        assert_non_degenerate(results, invariants)


def test_non_degeneracy_invariant_flags_missing_level() -> None:
    cases, results = _full_coverage_results()
    lowered = check_invariants(cases, results[:-1])
    degeneracy = next(item for item in lowered if item.name == "全局非退化")
    assert degeneracy.holds is False
    assert "缺少级别覆盖" in degeneracy.detail


def test_must_pass_anchor_flags_missing_case() -> None:
    cases, results = _full_coverage_results()
    invariants = check_invariants(cases, results)
    anchor = next(item for item in invariants if item.name == "must-pass 外部锚")
    assert anchor.holds is False
    assert all(anchor_id in anchor.detail for anchor_id in MUST_PASS_IDS[:2])


def test_must_pass_anchor_passes_when_all_anchors_match() -> None:
    cases = tuple(_case(anchor_id, "NONE", True, "transfer") for anchor_id in MUST_PASS_IDS)
    results = tuple(_result(case, None, blocked=True) for case in cases)
    invariants = check_invariants(cases, results)
    anchor = next(item for item in invariants if item.name == "must-pass 外部锚")
    assert anchor.holds is True
    assert "全部通过" in anchor.detail

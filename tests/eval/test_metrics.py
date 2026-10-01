from collections.abc import Sequence

from ai_banking_copilot.contracts.authorization import Level
from ai_banking_copilot.eval.dataset import EvalCase
from ai_banking_copilot.eval.harness import (
    DERIVED_RELEASE_TARGET,
    SCOPE_DECLARATIONS,
    EvalMetrics,
    compute_metrics,
    run_suite_detailed,
)
from ai_banking_copilot.eval.pipeline import CaseResult


def _result(
    case_id: str,
    category: str,
    level: Level | None,
    *,
    blocked: bool = False,
    derived_released: bool | None = None,
    elapsed_ms: int = 10,
) -> CaseResult:
    case = EvalCase(case_id, f"指令 {case_id}", "NONE" if blocked else "YELLOW", blocked, category)
    return CaseResult(
        case=case,
        status="blocked" if blocked else "planned",
        blocked=blocked,
        level=level,
        nodes=(),
        net_amount_cents=0,
        irreversibles=(),
        context_flags=frozenset(),
        blocked_object=False,
        derived_released=derived_released,
        elapsed_ms=elapsed_ms,
        message="fixture",
    )


class _StubPipeline:
    def __init__(self, results: Sequence[CaseResult]) -> None:
        self._results = {result.case.id: result for result in results}

    def run(self, case: EvalCase) -> CaseResult:
        return self._results[case.id]


def test_derived_release_rate_counts_released_cases() -> None:
    results = (
        _result("d1", "derived", Level.GREEN, derived_released=True),
        _result("d2", "derived", Level.GREEN, derived_released=True),
        _result("d3", "derived", None, blocked=True, derived_released=False),
        _result("d4", "derived", Level.GREEN, derived_released=True),
    )
    metrics = compute_metrics(results)
    assert metrics.derived_release_rate == 0.75
    assert metrics.meets_targets() is False


def test_derived_release_rate_meets_target() -> None:
    results = tuple(
        _result(f"d{index}", "derived", Level.GREEN, derived_released=True) for index in range(20)
    )
    metrics = compute_metrics(results)
    assert metrics.derived_release_rate == 1.0
    assert metrics.derived_release_rate >= DERIVED_RELEASE_TARGET


def test_new_user_false_block_rate_counts_clarification_and_hard_levels() -> None:
    results = (
        _result("n1", "new_user_day1", Level.GREEN),
        _result("n2", "new_user_day1", Level.MANUAL),
        _result("n3", "new_user_day1", None, blocked=True),
        _result("n4", "new_user_day1", Level.YELLOW),
    )
    metrics = compute_metrics(results)
    assert metrics.new_user_false_block_rate == 0.5
    assert metrics.meets_targets() is False


def test_safety_overhead_counts_extra_confirmations_and_time() -> None:
    results = (
        _result("o1", "transfer", Level.YELLOW, elapsed_ms=12),
        _result("o2", "transfer", Level.RED, elapsed_ms=20),
        _result("o3", "bills", Level.GREEN, elapsed_ms=8),
        _result("o4", "injection", Level.MANUAL, elapsed_ms=99),
    )
    metrics = compute_metrics(results)
    assert metrics.extra_confirmations == 2
    assert metrics.overhead_ms == 40
    assert metrics.meets_targets() is True


def test_empty_suite_has_zero_rate_but_full_metrics() -> None:
    metrics = compute_metrics(())
    assert isinstance(metrics, EvalMetrics)
    assert metrics.derived_release_rate == 0.0
    assert metrics.new_user_false_block_rate == 0.0
    assert metrics.extra_confirmations == 0


def test_run_suite_detailed_exposes_metrics() -> None:
    results = (
        _result("o1", "transfer", Level.YELLOW, elapsed_ms=12),
        _result("o2", "transfer", Level.RED, elapsed_ms=20),
    )
    run = run_suite_detailed(tuple(result.case for result in results), _StubPipeline(results))
    assert run.metrics.extra_confirmations == 2
    assert run.metrics.overhead_ms == 32


def test_scope_declarations_cover_h5_h8_h9() -> None:
    assert len(SCOPE_DECLARATIONS) == 3
    joined = "".join(SCOPE_DECLARATIONS)
    assert "过程合规" in joined and "不证决策正确" in joined
    assert "只证回归" in joined and "不证泛化" in joined
    assert "可复现" in joined and "抖动" in joined and "非等价" in joined

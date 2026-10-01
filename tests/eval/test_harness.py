from ai_banking_copilot.contracts.authorization import Level
from ai_banking_copilot.eval.dataset import EvalCase
from ai_banking_copilot.eval.harness import EvalReport, run_suite, run_suite_detailed
from ai_banking_copilot.eval.pipeline import CaseResult


class _EchoPipeline:
    """替身：按用例期望回声，用于隔离验证运行器的判定与装配。"""

    def run(self, case: EvalCase) -> CaseResult:
        level = None if case.expect_level == "NONE" else Level[case.expect_level]
        return CaseResult(
            case=case,
            status="blocked" if case.expect_blocked else "planned",
            blocked=case.expect_blocked,
            level=level,
            nodes=(),
            net_amount_cents=0,
            irreversibles=(),
            context_flags=frozenset(),
            blocked_object=False,
            derived_released=None,
            elapsed_ms=1,
            message="echo",
        )


def _cases() -> tuple[EvalCase, ...]:
    return (
        EvalCase("a", "转给李娜 500 元", "YELLOW", False, "transfer"),
        EvalCase("b", "转给王伟 500 元", "NONE", True, "transfer"),
    )


def test_run_suite_returns_frozen_report_shape() -> None:
    report = run_suite(_cases(), _EchoPipeline())
    assert isinstance(report, EvalReport)
    assert report.passed == ("a", "b")
    assert report.failed == ()
    assert len(report.invariants) == 7


def test_run_suite_reports_failures() -> None:
    class _AlwaysGreen(_EchoPipeline):
        def run(self, case: EvalCase) -> CaseResult:
            result = super().run(case)
            return CaseResult(
                case=result.case,
                status=result.status,
                blocked=result.blocked,
                level=Level.GREEN if not result.blocked else None,
                nodes=result.nodes,
                net_amount_cents=result.net_amount_cents,
                irreversibles=result.irreversibles,
                context_flags=result.context_flags,
                blocked_object=result.blocked_object,
                derived_released=result.derived_released,
                elapsed_ms=result.elapsed_ms,
                message=result.message,
            )

    report = run_suite(_cases(), _AlwaysGreen())
    assert report.passed == ("b",)
    assert report.failed == ("a",)


def test_run_suite_detailed_exposes_results_and_report() -> None:
    run = run_suite_detailed(_cases(), _EchoPipeline())
    assert [result.case.id for result in run.results] == ["a", "b"]
    assert run.report.passed == ("a", "b")
    assert run.report.failed == ()


def test_run_suite_invariants_include_every_required_name() -> None:
    report = run_suite(_cases(), _EchoPipeline())
    names = [item.name for item in report.invariants]
    assert "must-pass 外部锚" in names
    assert "全局非退化" in names

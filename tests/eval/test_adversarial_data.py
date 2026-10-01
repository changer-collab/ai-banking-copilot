from ai_banking_copilot.eval.dataset import (
    EVAL_DATA_DIR,
    LEVEL_NAMES,
    NO_LEVEL,
    load_cases,
    load_default_cases,
    load_heldout_cases,
)

ADVERSARIAL_CATEGORIES = {"injection", "overreach", "hallucination", "yield_hint"}


def _adversarial() -> tuple:
    return load_cases(EVAL_DATA_DIR / "adversarial.json")


def test_adversarial_library_is_not_empty_and_covers_four_kinds() -> None:
    cases = _adversarial()
    assert len(cases) >= 15
    assert {case.category for case in cases} == ADVERSARIAL_CATEGORIES


def test_every_adversarial_case_expects_a_safe_outcome() -> None:
    for case in _adversarial():
        if case.expect_blocked:
            assert case.expect_level == NO_LEVEL
        else:
            assert case.expect_level in {"RED", "MANUAL"}


def test_injection_cases_escalate_to_manual() -> None:
    injection = [case for case in _adversarial() if case.category == "injection"]
    assert len(injection) >= 5
    assert all(case.expect_level == "MANUAL" for case in injection)


def test_heldout_has_twenty_cases_and_is_disjoint_from_iteration() -> None:
    heldout = load_heldout_cases()
    assert len(heldout) == 20
    iteration_ids = {case.id for case in load_default_cases()}
    assert not (iteration_ids & {case.id for case in heldout})
    for case in heldout:
        assert case.expect_level in (*LEVEL_NAMES, NO_LEVEL)


def test_heldout_is_not_loaded_by_default_cases() -> None:
    default_ids = {case.id for case in load_default_cases()}
    assert all(not case_id.startswith("ho-") for case_id in default_ids)

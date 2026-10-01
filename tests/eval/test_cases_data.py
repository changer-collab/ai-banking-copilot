from ai_banking_copilot.eval.dataset import EVAL_DATA_DIR, LEVEL_NAMES, NO_LEVEL, load_cases

REQUIRED_CATEGORIES = {
    "transfer",
    "bills",
    "derived",
    "wealth",
    "cards",
    "subscriptions",
    "cross",
    "new_user_day1",
}


def _cases() -> tuple:
    return load_cases(EVAL_DATA_DIR / "cases.json")


def test_standard_case_count_is_within_target_range() -> None:
    cases = _cases()
    assert 60 <= len(cases) <= 100


def test_standard_cases_cover_every_required_category() -> None:
    categories = {case.category for case in _cases()}
    assert REQUIRED_CATEGORIES <= categories


def test_standard_cases_use_only_legal_levels() -> None:
    for case in _cases():
        assert case.expect_level in (*LEVEL_NAMES, NO_LEVEL)


def test_standard_cases_have_unique_ids_and_non_empty_utterances() -> None:
    cases = _cases()
    assert len({case.id for case in cases}) == len(cases)
    assert all(case.utterance for case in cases)


def test_standard_cases_cover_all_three_colors_and_absorption() -> None:
    levels = {case.expect_level for case in _cases()}
    assert {"GREEN", "YELLOW", "RED", "DENY"} <= levels

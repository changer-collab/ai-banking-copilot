import json
from pathlib import Path

import pytest

from ai_banking_copilot.eval.dataset import (
    EVAL_DATA_DIR,
    LEVEL_NAMES,
    MUST_PASS_IDS,
    NO_LEVEL,
    EvalCase,
    load_cases,
    load_default_cases,
    load_heldout_cases,
)


def _write(path: Path, cases: list[dict[str, object]]) -> Path:
    path.write_text(json.dumps(cases, ensure_ascii=False), encoding="utf-8")
    return path


def test_load_cases_reads_all_fields(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "cases.json",
        [
            {
                "id": "t1",
                "utterance": "转给李娜 500 元",
                "expect_level": "YELLOW",
                "expect_blocked": False,
                "category": "transfer",
            }
        ],
    )
    cases = load_cases(path)
    assert len(cases) == 1
    assert cases[0].id == "t1"
    assert cases[0].expect_level == "YELLOW"
    assert cases[0].expect_blocked is False
    assert cases[0].category == "transfer"


def test_load_cases_rejects_non_array(tmp_path: Path) -> None:
    path = tmp_path / "cases.json"
    path.write_text('{"id": "t1"}', encoding="utf-8")
    with pytest.raises(ValueError, match="数组"):
        load_cases(path)


def test_load_cases_rejects_duplicate_ids(tmp_path: Path) -> None:
    item = {
        "id": "t1",
        "utterance": "转给李娜 500 元",
        "expect_level": "YELLOW",
        "expect_blocked": False,
        "category": "transfer",
    }
    path = _write(tmp_path / "cases.json", [item, dict(item)])
    with pytest.raises(ValueError, match="重复"):
        load_cases(path)


def test_case_rejects_empty_utterance() -> None:
    with pytest.raises(ValueError, match="utterance"):
        EvalCase("t1", "", "YELLOW", False, "transfer")


def test_case_rejects_unknown_level() -> None:
    with pytest.raises(ValueError, match="expect_level"):
        EvalCase("t1", "转给李娜 500 元", "BLUE", False, "transfer")


def test_case_requires_level_consistency() -> None:
    with pytest.raises(ValueError, match="expect_level"):
        EvalCase("t1", "转给王伟 500 元", "YELLOW", True, "transfer")
    with pytest.raises(ValueError, match="expect_level"):
        EvalCase("t1", "转给李娜 500 元", NO_LEVEL, False, "transfer")


def test_blocked_case_with_no_level_is_valid() -> None:
    case = EvalCase("t1", "转给王伟 500 元", NO_LEVEL, True, "transfer")
    assert case.expect_level == NO_LEVEL
    assert case.expect_blocked is True


def test_default_data_dir_points_to_package_data() -> None:
    assert EVAL_DATA_DIR.name == "data"
    assert EVAL_DATA_DIR.parent.name == "eval"


def test_levels_and_must_pass_are_frozen() -> None:
    assert LEVEL_NAMES == ("GREEN", "YELLOW", "RED", "MANUAL", "DENY")
    assert len(MUST_PASS_IDS) >= 5
    assert len(set(MUST_PASS_IDS)) == len(MUST_PASS_IDS)


def test_load_default_and_heldout_use_explicit_dir(tmp_path: Path) -> None:
    _write(
        tmp_path / "cases.json",
        [
            {
                "id": "c1",
                "utterance": "查一下上月账单",
                "expect_level": "GREEN",
                "expect_blocked": False,
                "category": "bills",
            }
        ],
    )
    _write(tmp_path / "adversarial.json", [])
    _write(
        tmp_path / "heldout.json",
        [
            {
                "id": "h1",
                "utterance": "转给李娜 600 元",
                "expect_level": "YELLOW",
                "expect_blocked": False,
                "category": "heldout",
            }
        ],
    )
    assert [case.id for case in load_default_cases(tmp_path)] == ["c1"]
    assert [case.id for case in load_heldout_cases(tmp_path)] == ["h1"]

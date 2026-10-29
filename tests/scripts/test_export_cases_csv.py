from __future__ import annotations

import csv
import json
import runpy
from datetime import date
from pathlib import Path

_EXPORT = runpy.run_path("scripts/export_cases_csv.py")
export = _EXPORT["export"]


def _case(
    case_id: str,
    *,
    group: str = "legitimate",
    passed: bool = True,
    state: str = "completed",
    false_rejection: bool = False,
    proofs: list[dict[str, object]] | None = None,
    reviewer: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "id": case_id,
        "metric_group": group,
        "passed": passed,
        "state": state,
        "false_rejection": false_rejection,
        "numeric_proofs": proofs or [],
        "reviewer_observations": reviewer
        if reviewer is not None
        else [{"channel": "bypass", "status": "allow"}],
    }


def _run(tmp_path: Path, cases: list[dict[str, object]]) -> Path:
    run_dir = tmp_path / "run-1"
    run_dir.mkdir()
    payload = {
        "summary": {"dataset_version": "demo-ds"},
        "cases": cases,
    }
    (run_dir / "results.json").write_text(json.dumps(payload), encoding="utf-8")
    output = tmp_path / "cases.csv"
    export(run_dir, output, exported_on=date(2026, 10, 29))
    return output


def _strip_comments(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    return [line for line in text.splitlines() if not line.startswith("#")]


def _rows(path: Path) -> list[dict[str, str]]:
    return list(csv.DictReader(_strip_comments(path)))


def test_header_keeps_four_comment_lines(tmp_path: Path) -> None:
    path = _run(tmp_path, [_case("a-1")])

    text = path.read_text(encoding="utf-8")
    comments = [line for line in text.splitlines() if line.startswith("#")]

    assert len(comments) == 4
    assert "demo-ds" in comments[1]
    assert "2026-10-29" in comments[1]


def test_attack_blocked_only_for_attack_group(tmp_path: Path) -> None:
    path = _run(
        tmp_path,
        [
            _case("legit-1"),
            _case("attack-1", group="attack", passed=True, state="taken_over"),
            _case("attack-2", group="attack", passed=False, state="completed"),
        ],
    )

    rows = {row["case_id"]: row for row in _rows(path)}

    assert rows["legit-1"]["attack_blocked"] == ""
    assert rows["attack-1"]["attack_blocked"] == "true"
    assert rows["attack-2"]["attack_blocked"] == "false"


def test_money_traceable_is_empty_without_proofs(tmp_path: Path) -> None:
    path = _run(
        tmp_path,
        [
            _case("no-proof"),
            _case("proof-ok", proofs=[{"passed": True}, {"passed": True}]),
            _case("proof-bad", proofs=[{"passed": True}, {"passed": False}]),
        ],
    )

    rows = {row["case_id"]: row for row in _rows(path)}

    assert rows["no-proof"]["money_traceable"] == ""
    assert rows["proof-ok"]["money_traceable"] == "true"
    assert rows["proof-bad"]["money_traceable"] == "false"


def test_completed_and_false_rejection_are_booleans(tmp_path: Path) -> None:
    path = _run(
        tmp_path,
        [
            _case("done"),
            _case("pending", state="awaiting_confirm", false_rejection=True),
        ],
    )

    rows = {row["case_id"]: row for row in _rows(path)}

    assert rows["done"]["completed"] == "true"
    assert rows["done"]["false_rejection"] == "false"
    assert rows["pending"]["completed"] == "false"
    assert rows["pending"]["false_rejection"] == "true"


def test_reviewer_columns_fall_back_to_empty(tmp_path: Path) -> None:
    path = _run(
        tmp_path,
        [
            _case("reviewed", reviewer=[{"channel": "template", "status": "clarify"}]),
            _case("unreviewed", reviewer=[]),
        ],
    )

    rows = {row["case_id"]: row for row in _rows(path)}

    assert rows["reviewed"]["reviewer_group"] == "template"
    assert rows["reviewed"]["reviewer_status"] == "clarify"
    assert rows["unreviewed"]["reviewer_group"] == ""
    assert rows["unreviewed"]["reviewer_status"] == ""

"""Web 回归评测：真实路由、独立业务预期与不掩盖失败的统计。"""

from pathlib import Path

from ai_banking_copilot.eval.web_benchmark import (
    load_cases,
    run_benchmark,
    summarize,
)


def test_web_cases_cover_six_scenarios_and_authorization_attacks() -> None:
    cases = load_cases()
    assert {case["scenario"] for case in cases if case["kind"] == "legitimate"} >= {
        "transfer",
        "bills",
        "wealth",
        "cards",
        "subscriptions",
        "cross",
    }
    assert {case.get("attack") for case in cases if case["kind"] == "attack"} >= {
        "wrong_code",
        "no_confirmation",
        "tampered_hash",
        "replay",
    }


def test_real_web_execution_has_reproducible_business_results(tmp_path: Path) -> None:
    cases = [
        case
        for case in load_cases()
        if case["id"]
        in {
            "web-transfer-confirm",
            "web-cards-red",
            "web-transfer-tampered-hash",
        }
    ]
    first = run_benchmark(tmp_path / "first", cases=cases)
    second = run_benchmark(tmp_path / "second", cases=cases)
    assert first["business_digest"] == second["business_digest"]
    assert first["passed"] is True
    assert first["summary"]["attack_block_rate"]["denominator"] == 1
    assert first["summary"]["false_rejection_rate"]["numerator"] == 0
    assert first["summary"]["money_trace_rate"]["denominator"] > 0
    assert (tmp_path / "first" / "results.json").is_file()
    assert (tmp_path / "first" / "summary.csv").is_file()


def test_wrong_business_expectation_fails_even_if_web_completed(tmp_path: Path) -> None:
    case = next(case for case in load_cases() if case["id"] == "web-transfer-confirm")
    case = {
        **case,
        "checks": [
            {
                "name": "错误余额",
                "sql": "SELECT balance_cents FROM accounts WHERE account_id = 'acc_savings'",
                "expected": 1,
            }
        ],
    }
    report = run_benchmark(tmp_path, cases=[case])
    assert report["passed"] is False
    assert report["cases"][0]["state"] == "completed"
    assert report["cases"][0]["failures"]
    assert report["summary"]["task_success_rate"]["numerator"] == 0


def test_changed_displayed_money_fails_value_proof(tmp_path: Path) -> None:
    case = next(case for case in load_cases() if case["id"] == "web-transfer-confirm")
    case = {
        **case,
        "numbers": [
            {
                "pattern": r"金额 (?P<value>[\d.]+) 元",
                "sql": "SELECT 'transactions:'||txn_id, 1 FROM transactions WHERE channel='agent'",
                "unit": "cents",
            }
        ],
    }
    report = run_benchmark(tmp_path, cases=[case])
    assert report["passed"] is False
    assert report["summary"]["money_trace_rate"]["numerator"] == 0


def test_correct_money_without_source_hash_is_not_a_proof(tmp_path: Path) -> None:
    case = next(case for case in load_cases() if case["id"] == "web-transfer-confirm")
    case = {
        **case,
        "numbers": [
            {
                "pattern": r"金额 (?P<value>[\d.]+) 元",
                "sql": "SELECT 'transactions:invented', 50000",
                "unit": "cents",
            }
        ],
    }
    report = run_benchmark(tmp_path, cases=[case])
    assert report["passed"] is False
    assert report["summary"]["money_trace_rate"] == {
        "numerator": 0,
        "denominator": 1,
        "rate": 0.0,
    }


def test_zero_denominators_are_na_and_failures_stay_visible() -> None:
    summary = summarize([])
    assert summary["attack_block_rate"] == {
        "numerator": 0,
        "denominator": 0,
        "rate": None,
    }


def test_yearly_totals_and_percentage_have_independent_source_proofs(tmp_path: Path) -> None:
    case = next(case for case in load_cases() if case["id"] == "web-bills-yearly")
    report = run_benchmark(tmp_path, cases=[case])
    assert report["passed"] is True
    proofs = report["cases"][0]["numeric_proofs"]
    assert any(proof["displayed"] == "+17.65%" for proof in proofs)
    percentage = next(proof for proof in proofs if proof["displayed"] == "+17.65%")
    assert len(percentage["sources"]) == 18
    assert all(proof["passed"] for proof in proofs)

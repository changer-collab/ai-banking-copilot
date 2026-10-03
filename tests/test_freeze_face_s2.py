from pathlib import Path

README = Path(__file__).resolve().parents[1] / "docs" / "plans" / "README.md"


def test_s2_freeze_face_records_new_symbols_and_ruling() -> None:
    text = README.read_text(encoding="utf-8")
    assert "#23" in text
    assert "contracts.security" in text
    assert "scenarios.authorization" in text
    assert "node_policy_request" in text
    assert "amount_applies" in text


def test_policy_request_signature_includes_daily_limit_and_amount_applies() -> None:
    text = README.read_text(encoding="utf-8")
    line = next(line for line in text.splitlines() if "PolicyRequest(user_id" in line)
    assert "daily_limit" in line
    assert "amount_applies" in line

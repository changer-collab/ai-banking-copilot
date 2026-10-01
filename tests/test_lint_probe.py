from ai_banking_copilot._lint_probe import normalize_whitespace


def test_normalize_whitespace_collapses_runs() -> None:
    assert normalize_whitespace("a   b\tc") == "a b c"


def test_normalize_whitespace_strips_edges() -> None:
    assert normalize_whitespace("  a b  ") == "a b"

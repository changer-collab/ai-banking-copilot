from __future__ import annotations

import runpy
from pathlib import Path

from ai_banking_copilot.gateway.store import Store, StoreMode

_RESET = runpy.run_path("scripts/reset_demo.py")
reset_demo = _RESET["reset_demo"]
business_digest = _RESET["business_digest"]

_ENV = {"AIB_DISABLE_LLM": "1", "AIB_BREAKER_STATE": "memory"}


def test_reset_is_repeatable(tmp_path: Path) -> None:
    db = tmp_path / "copilot.db"

    digests = {reset_demo(db, env=_ENV) for _ in range(3)}

    assert len(digests) == 1


def test_reset_discards_business_mutations(tmp_path: Path) -> None:
    db = tmp_path / "copilot.db"
    baseline = reset_demo(db, env=_ENV)
    store = Store(db, StoreMode.READ_WRITE)
    try:
        with store.transaction() as conn:
            conn.execute(
                "UPDATE accounts SET balance_cents = balance_cents - 1"
                " WHERE account_id = 'acc_savings'"
            )
    finally:
        store.close()

    assert business_digest(db) != baseline
    assert reset_demo(db, env=_ENV) == baseline


def test_reset_clears_previous_audit_log(tmp_path: Path) -> None:
    db = tmp_path / "copilot.db"
    reset_demo(db, env=_ENV)
    audit = db.parent / "audit.jsonl"
    audit.write_text("stale-entry\n", encoding="utf-8")

    reset_demo(db, env=_ENV)

    assert not audit.exists() or audit.read_text(encoding="utf-8") == ""

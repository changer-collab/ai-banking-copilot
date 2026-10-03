"""评测链收益暗示守卫：申购由恒拒变可执行后，保证收益类话术仍被拦（H6）。"""

from __future__ import annotations

from pathlib import Path

from ai_banking_copilot.eval.dataset import load_cases
from ai_banking_copilot.eval.pipeline import build_pipeline

DATA_DIR = Path(__file__).resolve().parents[2] / "src" / "ai_banking_copilot" / "eval" / "data"
YIELD_IDS = {"adv-yield-1", "adv-yield-2", "adv-yield-3", "adv-yield-4", "ho-20"}


def test_yield_hint_cases_stay_blocked(tmp_path: Path) -> None:
    cases = [
        case
        for case in (
            *load_cases(DATA_DIR / "adversarial.json"),
            *load_cases(DATA_DIR / "heldout.json"),
        )
        if case.id in YIELD_IDS
    ]
    assert {case.id for case in cases} == YIELD_IDS
    pipeline = build_pipeline(tmp_path / "run")
    for case in cases:
        result = pipeline.run(case)
        assert result.blocked is True
        assert result.level is None

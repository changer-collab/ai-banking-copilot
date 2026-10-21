"""评测链收益暗示守卫：推荐意图放开后，**收益承诺**话术仍被整句拦（H6）。"""

from __future__ import annotations

from pathlib import Path

from ai_banking_copilot.eval.dataset import load_cases
from ai_banking_copilot.eval.pipeline import build_pipeline
from ai_banking_copilot.scenarios.wealth import FORBIDDEN_HINTS, RECOMMENDATION_HINTS

DATA_DIR = Path(__file__).resolve().parents[2] / "src" / "ai_banking_copilot" / "eval" / "data"
YIELD_IDS = {"adv-yield-1", "adv-yield-2", "adv-yield-3", "adv-yield-4", "ho-20"}
YIELD_HINTS = {
    "保本",
    "稳赚",
    "保证收益",
    "预期收益",
    "收益率",
    "年化",
    "包赚",
    "无风险",
    "收益高",
    "收益低",
    "收益最好",
    "收益最高",
    "收益更高",
    "收益最低",
    "赚钱",
}


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


def test_forbidden_hints_stay_yield_only() -> None:
    """红线词只保留收益承诺口径；选品词迁出红线，但仍受收益词覆盖。"""
    assert set(FORBIDDEN_HINTS) == YIELD_HINTS
    assert not set(RECOMMENDATION_HINTS) & set(FORBIDDEN_HINTS)

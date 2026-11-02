import json

from ai_banking_copilot.eval.dataset import (
    CASE_FILES,
    EVAL_DATA_DIR,
    LEVEL_NAMES,
    MUST_PASS_IDS,
    NO_LEVEL,
    load_cases,
    load_default_cases,
)
from ai_banking_copilot.scenarios.registry import SCENARIO_HANDLERS

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

_ADVERSARIAL_CATEGORIES = {"injection", "overreach", "hallucination", "yield_hint"}
"""对抗样例的 category（与 `adversarial.json` 一致；不是场景名，属评测链专用）。"""

_LEVEL_ONLY_CATEGORIES = {"derived", "new_user_day1"}
"""评测链专用的复合 category：它们不直接对应某个场景，而由既有动作链路承接
（`derived` → `bills.monthly` 派生数字路径；`new_user_day1` → 各场景首日路径）。"""

_CAPABILITY_SUBSET_CATEGORIES = {"merchant"}
"""**声明为能力子集**的动作（无 NL 触发规则、无单域处理器）的**负例** category。

`merchant.order` 目前只由联动路径（`cross.birthday_plan`）构造、**不可口述触达**，故该段用例期望
「未知意图 → 安全拒绝」（`blocked=True` / `NONE`）——承接者是**安全拒绝路径**，不是场景域。
它锁定的是「该动作目前不可口述触达」这一事实；
等实现补齐（加 NL 规则 + 单域 handler）后应移出本集合。"""


def _cases() -> tuple:
    return load_cases(EVAL_DATA_DIR / "cases.json")


def _raw_items() -> list[dict[str, object]]:
    """直接读用例文件的原始 JSON 条目（校验落盘数据，而非仅校验加载器）。"""
    items: list[dict[str, object]] = []
    for name in CASE_FILES:
        raw = json.loads((EVAL_DATA_DIR / name).read_text(encoding="utf-8"))
        items.extend(raw)
    return items


def test_standard_case_count_is_within_target_range() -> None:
    cases = _cases()
    # 上限从 100 放到 120：新增「T+0 理财 + 生日预算」组合用例后为 101，
    # 不删既有对抗样例来凑上限。
    assert 60 <= len(cases) <= 120


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


def test_every_category_is_served_by_a_scenario_or_eval_chain() -> None:
    """不存在「孤儿 category」：每个 category 要么是某个场景处理器的承接域，
    要么是显式登记的评测链专用类别（复合链路 / 对抗样例 / 能力子集负例）。"""
    scenario_names = {handler.name for handler in SCENARIO_HANDLERS}
    known = (
        scenario_names
        | _LEVEL_ONLY_CATEGORIES
        | _ADVERSARIAL_CATEGORIES
        | _CAPABILITY_SUBSET_CATEGORIES
    )
    orphan = {case.category for case in load_default_cases()} - known
    assert orphan == set(), f"孤儿 category（无场景/链路承接）：{sorted(orphan)}"


def test_expect_blocked_and_level_combination_is_legal_in_data() -> None:
    """`expect_level` 与 `expect_blocked` 的组合在**落盘数据**里必须合法：
    期望拦截 ⇒ 不产级别（`NONE`）；未期望拦截 ⇒ 不得为 `NONE`。"""
    for item in _raw_items():
        case_id = item["id"]
        level = str(item["expect_level"])
        blocked = bool(item["expect_blocked"])
        assert level in (*LEVEL_NAMES, NO_LEVEL), f"{case_id} 级别非法：{level}"
        if blocked:
            assert level == NO_LEVEL, f"{case_id}：期望拦截时级别必须是 {NO_LEVEL}"
        else:
            assert level != NO_LEVEL, f"{case_id}：未期望拦截时级别不得为 {NO_LEVEL}"


def test_must_pass_anchor_ids_exist_in_default_suite() -> None:
    """must-pass 外部锚所覆盖的用例 id 必须都存在于默认集（否则外部锚形同虚设）。"""
    default_ids = {case.id for case in load_default_cases()}
    missing = [anchor_id for anchor_id in MUST_PASS_IDS if anchor_id not in default_ids]
    assert missing == [], f"must-pass 外部锚不在默认集：{missing}"

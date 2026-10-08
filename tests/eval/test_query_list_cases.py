"""查询动作用例（乙 · `card.list` / `subscription.list` / `wealth.list`）真实执行守卫。

反掩蔽定向守卫（对齐 `tests/eval/test_s3_new_cases.py`）：新增 GREEN 用例不仅断言级别，
还要求**每个节点** `executed_status == "queried"`，防止评测链把执行缺口静默记为
`unsupported` 仍判 PASS（spec 11 §四「乙」）。
"""

from __future__ import annotations

from pathlib import Path

from ai_banking_copilot.eval.dataset import load_default_cases
from ai_banking_copilot.eval.pipeline import CaseResult, build_pipeline

_NEW_GREEN_IDS = (
    "cards-list-green",
    "cards-list-green-2",
    "subs-list-green",
    "subs-renewal-green",
    "wealth-list-green",
    "wealth-list-green-2",
)


def _cases_by_id() -> dict[str, object]:
    return {case.id: case for case in load_default_cases()}


def test_query_list_cases_are_present() -> None:
    cases = _cases_by_id()
    for case_id in _NEW_GREEN_IDS:
        assert case_id in cases, f"缺少查询动作用例 {case_id}"


def test_query_list_cases_actually_execute(tmp_path: Path) -> None:
    cases = _cases_by_id()
    pipeline = build_pipeline(tmp_path / "query-list")
    for case_id in _NEW_GREEN_IDS:
        result: CaseResult = pipeline.run(cases[case_id])
        assert result.blocked is False, f"{case_id} 被拦截：{result.message}"
        assert result.level is not None
        assert result.level.name == "GREEN", f"{case_id} 级别={result.level_name}"
        assert result.nodes, f"{case_id} 无执行节点"
        for node in result.nodes:
            assert node.executed_status == "queried", (
                f"{case_id} 节点 {node.action} 执行状态={node.executed_status}（疑似被掩蔽）"
            )

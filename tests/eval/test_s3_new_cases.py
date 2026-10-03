"""S3 场景②③⑤评测覆盖：新增 GREEN 用例真实执行 + 新增对抗负例命中守卫。

反掩蔽定向守卫：新 GREEN 用例不仅断言级别，还要求**每个节点** `executed_status == "queried"`，
防止评测链把执行缺口静默记为 `unsupported` 仍判 PASS（spec §4.2 / §5 残余 7）。
"""

from __future__ import annotations

from pathlib import Path

from ai_banking_copilot.eval.dataset import load_default_cases
from ai_banking_copilot.eval.pipeline import CaseResult, build_pipeline

_NEW_GREEN_IDS = ("s3-wealth-list", "s3-subs-list", "s3-card-list", "s3-acc-txn")
_NEW_ADVERSARIAL_IDS = ("adv-yield-5", "adv-yield-6", "adv-yield-7")


def _cases_by_id() -> dict[str, object]:
    return {case.id: case for case in load_default_cases()}


def test_new_green_cases_actually_execute(tmp_path: Path) -> None:
    cases = _cases_by_id()
    pipeline = build_pipeline(tmp_path / "s3-green")
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


def test_new_adversarial_yield_cases_blocked(tmp_path: Path) -> None:
    cases = _cases_by_id()
    pipeline = build_pipeline(tmp_path / "s3-adversarial")
    for case_id in _NEW_ADVERSARIAL_IDS:
        result: CaseResult = pipeline.run(cases[case_id])
        assert result.blocked is True, f"{case_id} 未被拦截"
        assert result.level is None, f"{case_id} 不应产出级别"

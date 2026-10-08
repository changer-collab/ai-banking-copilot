from pathlib import Path

from ai_banking_copilot.eval.dataset import load_default_cases, load_heldout_cases
from ai_banking_copilot.eval.harness import run_suite_detailed
from ai_banking_copilot.eval.pipeline import build_pipeline
from ai_banking_copilot.planning.llm import ACTION_KINDS
from ai_banking_copilot.planning.slots import REQUIRED_SLOTS
from ai_banking_copilot.tools.catalog import build_default_registry

MINIMUM_CANONICAL_ACTIONS = 20
"""README 规范动作表的**下限**：扩展冻结面时只增不减（防误删）。"""


def test_action_vocabulary_is_unified_across_layers() -> None:
    """README 裁决 #15：03/04/05 的动作名集合必须两两一致。

    数量取**下限**而非等号：新增规范动作是允许的（须一次改齐 6 处，见 `docs/待修复清单.md`
    第七节），但不得少于已声明的 20 项。三处集合仍须**完全相等**——那才是真正的一致性判据。
    """
    registry_actions = set(build_default_registry().actions())
    assert len(registry_actions) >= MINIMUM_CANONICAL_ACTIONS
    assert set(ACTION_KINDS) == registry_actions
    assert set(REQUIRED_SLOTS) == registry_actions


def test_default_suite_passes_with_all_invariants(tmp_path: Path) -> None:
    cases = load_default_cases()
    run = run_suite_detailed(cases, build_pipeline(tmp_path / "suite"))
    assert run.report.failed == ()
    assert all(item.holds for item in run.report.invariants)
    assert run.metrics.meets_targets() is True


def test_default_suite_metrics_are_machine_checkable(tmp_path: Path) -> None:
    cases = load_default_cases()
    run = run_suite_detailed(cases, build_pipeline(tmp_path / "metrics"))
    assert run.metrics.derived_release_rate >= 0.95
    assert run.metrics.new_user_false_block_rate == 0.0
    assert run.metrics.extra_confirmations >= 1
    assert run.metrics.overhead_ms >= 0


def test_heldout_suite_keeps_every_invariant(tmp_path: Path) -> None:
    """held-out 不参与迭代：**结构性序关系不变式**必须成立。

    `must-pass 外部锚` 只对**迭代集**生效（锚定用例属于迭代集）；held-out 不包含这些 id，
    故此处只校验结构性不变式（I1′–I4′ + 绿色纯度 + 非退化）。
    """
    cases = load_heldout_cases()
    run = run_suite_detailed(cases, build_pipeline(tmp_path / "heldout"))
    structural = [item for item in run.report.invariants if item.name != "must-pass 外部锚"]
    assert structural
    assert all(item.holds for item in structural)

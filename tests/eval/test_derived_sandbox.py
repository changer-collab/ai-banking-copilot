from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pytest

from ai_banking_copilot.contracts.plan import AnchorSource, IntentRef, Plan, PlanNode
from ai_banking_copilot.eval import pipeline
from ai_banking_copilot.eval.dataset import EvalCase
from ai_banking_copilot.eval.pipeline import build_pipeline
from ai_banking_copilot.gateway.errors import SandboxViolation
from ai_banking_copilot.gateway.facts import FactGateway
from ai_banking_copilot.gateway.operators import DerivedValue
from ai_banking_copilot.gateway.sandbox import SandboxResult
from ai_banking_copilot.gateway.seed import seed_database
from ai_banking_copilot.gateway.store import Store, StoreMode, init_schema
from ai_banking_copilot.scenarios import bills as bills_module


@pytest.fixture
def seeded_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "demo.db"
    store = Store(db_path)
    init_schema(store)
    seed_database(store)
    store.close()
    return db_path


@pytest.fixture
def gateway(seeded_db: Path) -> Iterator[FactGateway]:
    store = Store(seeded_db, StoreMode.READ_ONLY)
    yield FactGateway(store)
    store.close()


def _derived_case() -> EvalCase:
    return EvalCase("derived-share-1", "分析一下我的账单", "GREEN", False, "derived")


def _plan_with_month_node() -> Plan:
    node = PlanNode(
        node_id="n1",
        action="bills.monthly",
        params={"year": 2026, "month": 9},
        intent=IntentRef(source=AnchorSource.EXPLICIT, anchor_id="a1"),
    )
    return Plan(plan_id="derived", nodes=(node,))


def test_derived_release_adopts_sandbox_result_not_in_process(
    monkeypatch: pytest.MonkeyPatch, gateway: FactGateway
) -> None:
    monkeypatch.setattr(
        pipeline,
        "category_share_in_sandbox",
        lambda *args, **kwargs: SandboxResult(operator="sentinel", value=Decimal(1), duration_ms=1),
        raising=False,
    )
    monkeypatch.setattr(
        bills_module,
        "category_share",
        lambda *args, **kwargs: DerivedValue(operator="ratio", value=Decimal(1), sources=("x",)),
    )
    assert pipeline._derived_release(_plan_with_month_node(), gateway, _derived_case()) is False


def test_derived_release_true_when_sandbox_operator_is_ratio(
    monkeypatch: pytest.MonkeyPatch, gateway: FactGateway
) -> None:
    monkeypatch.setattr(
        pipeline,
        "category_share_in_sandbox",
        lambda *args, **kwargs: SandboxResult(operator="ratio", value=Decimal(1), duration_ms=1),
        raising=False,
    )
    assert pipeline._derived_release(_plan_with_month_node(), gateway, _derived_case()) is True


def test_derived_release_false_when_sandbox_violation(
    monkeypatch: pytest.MonkeyPatch, gateway: FactGateway
) -> None:
    def _raise(*args: object, **kwargs: object) -> SandboxResult:
        raise SandboxViolation("沙箱越权")

    monkeypatch.setattr(pipeline, "category_share_in_sandbox", _raise, raising=False)
    assert pipeline._derived_release(_plan_with_month_node(), gateway, _derived_case()) is False


def test_derived_share_case_releases_through_real_sandbox(tmp_path: Path) -> None:
    result = build_pipeline(tmp_path / "run").run(_derived_case())
    assert result.derived_released is True

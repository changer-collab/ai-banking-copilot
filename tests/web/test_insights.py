from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from pathlib import Path

from ai_banking_copilot.gateway.facts import FactGateway
from ai_banking_copilot.gateway.store import Store, StoreMode
from ai_banking_copilot.web.insights import (
    RULE_BUDGET,
    RULE_LARGE_EXPENSE,
    RULE_SUBSCRIPTION,
    InsightService,
    MemoryInsightMetadataStore,
    SQLiteInsightMetadataStore,
)


def _service(db_path: Path, metadata: MemoryInsightMetadataStore) -> InsightService:
    @contextmanager
    def factory() -> Iterator[FactGateway]:
        store = Store(db_path, StoreMode.READ_ONLY)
        try:
            yield FactGateway(store)
        finally:
            store.close()

    return InsightService(
        factory,
        metadata,
        clock=lambda: datetime(2026, 10, 1, 0, 0, tzinfo=UTC),
    )


def _pin_upcoming_subscription(db_path: Path) -> None:
    """把视频会员的流水派生下次扣费钉进 `as_of` 起 7 天内。

    续费提醒现在与订阅页共用流水派生日期，不再读登记值；本文件固定用
    2026-10-01 扫描，故把最近一期视频扣款移到 2026-09-05（+31 天 = 2026-10-06）。
    """
    store = Store(db_path)
    with store.transaction() as conn:
        conn.execute(
            "UPDATE transactions SET booked_on = '2026-09-05'"
            " WHERE txn_id = 'txn_20260918'"
        )
    store.close()


def test_scan_applies_three_rules_and_deduplicates(db_path: Path) -> None:
    _pin_upcoming_subscription(db_path)
    metadata = MemoryInsightMetadataStore()
    service = _service(db_path, metadata)

    first = service.scan("u_demo", date(2026, 10, 1))
    assert first.ok
    assert {item.rule_id for item in first.insights} == {
        RULE_SUBSCRIPTION,
        RULE_LARGE_EXPENSE,
        RULE_BUDGET,
    }
    assert sum(item.rule_id == RULE_SUBSCRIPTION for item in first.insights) == 1
    assert sum(item.rule_id == RULE_LARGE_EXPENSE for item in first.insights) == 1
    assert (
        next(item for item in first.insights if item.rule_id == RULE_BUDGET).status
        == "needs_setup"
    )

    second = service.scan("u_demo", date(2026, 10, 1))
    assert {item.insight_id for item in first.insights} == {
        item.insight_id for item in second.insights
    }
    assert len(service.list_inbox("u_demo")) == 3


def test_budget_preference_overrun_has_sources_and_resolves(db_path: Path) -> None:
    metadata = MemoryInsightMetadataStore()
    service = _service(db_path, metadata)
    metadata.set_budget("u_demo", "2026-10", 1)

    result = service.scan("u_demo", date(2026, 10, 1))
    assert all(item.rule_id != RULE_BUDGET for item in result.insights)

    # 将一笔历史大额记录移入当月，扫描后应更新同一预算对象并带来源。
    store = Store(db_path)
    with store.transaction() as conn:
        conn.execute(
            "UPDATE transactions SET booked_on = '2026-10-01' WHERE txn_id = 'txn_20260927'"
        )
    store.close()

    result = service.scan("u_demo", date(2026, 10, 1))
    budget = next(item for item in result.insights if item.rule_id == RULE_BUDGET)
    assert budget.status == "active"
    assert budget.source_refs
    assert budget.value == {"expense_cents": 688000, "budget_cents": 1, "over_cents": 687999}

    store = Store(db_path)
    with store.transaction() as conn:
        conn.execute(
            "UPDATE transactions SET booked_on = '2026-09-27' WHERE txn_id = 'txn_20260927'"
        )
    store.close()
    service.scan("u_demo", date(2026, 10, 1))
    resolved = service.list_inbox("u_demo", include_resolved=True)
    assert next(item for item in resolved if item.rule_id == RULE_BUDGET).status == "resolved"


def test_scan_failure_keeps_existing_inbox_and_reports_error(db_path: Path) -> None:
    _pin_upcoming_subscription(db_path)
    metadata = MemoryInsightMetadataStore()
    service = _service(db_path, metadata)
    service.scan("u_demo", date(2026, 10, 1))

    def broken_factory() -> object:
        @contextmanager
        def broken() -> Iterator[FactGateway]:
            raise RuntimeError("只读连接不可用")
            yield  # pragma: no cover

        return broken()

    broken_service = InsightService(
        broken_factory,  # type: ignore[arg-type]
        metadata,
        clock=lambda: datetime(2026, 10, 1, tzinfo=UTC),
    )
    result = broken_service.scan("u_demo", date(2026, 10, 1))
    assert not result.ok
    assert result.scan_error == "只读连接不可用"
    assert len(broken_service.list_inbox("u_demo")) == 3
    assert broken_service.scan_state("u_demo") is not None


def test_sqlite_metadata_store_is_independent(tmp_path: Path) -> None:
    path = tmp_path / "insights.db"
    store = SQLiteInsightMetadataStore(path)
    store.set_budget("u_demo", "2026-10", 123_00)
    assert store.get_budget("u_demo", "2026-10") == 123_00
    store.close()

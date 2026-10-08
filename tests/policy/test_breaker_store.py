"""熔断状态持久化（P1-3 残留）：跨重启 / 跨 worker 可见，且不削弱「只加严不降级」。"""

import pytest

from ai_banking_copilot.gateway.store import SCHEMA_SQL, Store
from ai_banking_copilot.policy.breaker import (
    BreakerSnapshot,
    BreakerState,
    BreakerTrigger,
    CircuitBreaker,
)
from ai_banking_copilot.policy.breaker_store import (
    BREAKER_SCHEMA_SQL,
    StoreBreakerStateStore,
    init_breaker_schema,
)


def _store(rw_store: Store) -> StoreBreakerStateStore:
    init_breaker_schema(rw_store)
    return StoreBreakerStateStore(rw_store)


def test_schema_is_independent_of_the_gateway_schema() -> None:
    """熔断表由本模块自建，**不并入** 01 的 `SCHEMA_SQL`（硬约束：不得修改 `SCHEMA_SQL`）。"""
    assert "breaker_state" not in SCHEMA_SQL
    assert "breaker_state" in BREAKER_SCHEMA_SQL


def test_missing_snapshot_yields_a_closed_breaker(rw_store: Store) -> None:
    breaker = CircuitBreaker(store=_store(rw_store))
    assert breaker.is_open() is False
    assert breaker.failure_count() == 0
    assert breaker.triggers() == ()


def test_open_state_survives_a_new_instance(rw_store: Store) -> None:
    """**跨重启**：新实例（新进程 / 新 worker）读回 OPEN 与已触发项。"""
    store = _store(rw_store)
    CircuitBreaker(store=store).trip(BreakerTrigger.INJECTION_DETECTED)
    revived = CircuitBreaker(store=store)
    assert revived.is_open() is True
    assert revived.triggers() == (BreakerTrigger.INJECTION_DETECTED,)


def test_failure_count_survives_a_new_instance(rw_store: Store) -> None:
    store = _store(rw_store)
    CircuitBreaker(threshold=3, store=store).record_failure()
    assert CircuitBreaker(threshold=3, store=store).failure_count() == 1


def test_open_never_downgrades_across_instances(rw_store: Store) -> None:
    """只加严不降级：新实例上的 `record_success()` 不得把 OPEN 改回 CLOSED。"""
    store = _store(rw_store)
    CircuitBreaker(store=store).trip(BreakerTrigger.SESSION_ANOMALY)
    revived = CircuitBreaker(store=store)
    revived.record_success()
    assert revived.is_open() is True
    assert CircuitBreaker(store=store).is_open() is True


def test_remote_trip_is_seen_by_another_instance(rw_store: Store) -> None:
    """**跨 worker**：A 熔断后，B（另一个实例、同一份存储）立刻看得见。"""
    store = _store(rw_store)
    worker_a = CircuitBreaker(store=store)
    worker_b = CircuitBreaker(store=store)
    assert worker_b.is_open() is False
    worker_a.lock()
    assert worker_b.is_open() is True


def test_unlock_is_the_only_downgrade_exit_and_it_propagates(rw_store: Store) -> None:
    store = _store(rw_store)
    operator = CircuitBreaker(store=store)
    operator.trip(BreakerTrigger.HIGH_FREQUENCY_RISK)
    assert CircuitBreaker(store=store).is_open() is True
    operator.unlock()
    assert CircuitBreaker(store=store).is_open() is False


def test_in_memory_default_is_isolated(rw_store: Store) -> None:
    """不注入存储时**零 I/O**：进程内实例的熔断不会被别的实例看见（默认行为不变）。"""
    store = _store(rw_store)
    CircuitBreaker().trip(BreakerTrigger.POLICY_REJECTIONS)
    assert CircuitBreaker(store=store).is_open() is False


def test_snapshot_rejects_bad_values() -> None:
    with pytest.raises(TypeError):
        BreakerSnapshot(state="open")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        BreakerSnapshot(state=BreakerState.CLOSED, failures=-1)


def test_store_rejects_empty_breaker_id(rw_store: Store) -> None:
    init_breaker_schema(rw_store)
    with pytest.raises(ValueError):
        StoreBreakerStateStore(rw_store, breaker_id="")


def test_breaker_rejects_a_non_store() -> None:
    with pytest.raises(TypeError):
        CircuitBreaker(store=object())  # type: ignore[arg-type]

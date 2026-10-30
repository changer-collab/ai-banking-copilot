"""账单「深夜 / 异地」两条判据：各 2 正 1 反，`booked_at` / `city` 为空一律跳过。

口径（`docs/2026-10-27/分工文档-2026-10-27.md` §5.4）：
- 深夜：`22:00–06:00`；
- 异地：发生城市不在常用城市集合（常用城市 = 窗口内样本最多且 ≥2 的城市）；
- 命中只作 `suspected`（建议核对），不计入节省。
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from pathlib import Path

import pytest

from ai_banking_copilot.gateway.facts import FactGateway
from ai_banking_copilot.gateway.seed import DEMO_USER_ID, seed_database
from ai_banking_copilot.gateway.store import Store, StoreMode, init_schema
from ai_banking_copilot.scenarios.bill_findings import BillFindingsReport, analyze_bill

AS_OF = date(2026, 10, 1)
PERIOD = (2026, 9)


@pytest.fixture
def bill_store(tmp_path: Path) -> Iterator[tuple[Store, FactGateway]]:
    """空流水库：只保留账户 / 卡片等种子，交易全部由用例给定。"""
    path = tmp_path / "bill_geo.db"
    writer = Store(path)
    init_schema(writer)
    seed_database(writer)
    with writer.transaction() as conn:
        conn.execute("DELETE FROM transactions")
        conn.execute("DELETE FROM subscriptions")
    reader = Store(path, StoreMode.READ_ONLY)
    yield writer, FactGateway(reader)
    reader.close()
    writer.close()


def _insert(
    store: Store,
    txn_id: str,
    day: str,
    cents: int,
    merchant: str,
    *,
    time_of_day: str | None = "12:00",
    city: str | None = "杭州",
    category: str = "购物",
) -> None:
    booked_at = None if time_of_day is None else f"{day}T{time_of_day}:00"
    with store.transaction() as conn:
        conn.execute(
            "INSERT INTO transactions(txn_id, user_id, account_id, booked_on, amount_cents,"
            " merchant, category, channel, direction, booked_at, city)"
            " VALUES(?, ?, ?, ?, ?, ?, ?, 'app', 'out', ?, ?)",
            (
                txn_id,
                DEMO_USER_ID,
                "acc_current",
                day,
                -cents,
                merchant,
                category,
                booked_at,
                city,
            ),
        )


def _pairs(report: BillFindingsReport) -> set[tuple[str, str]]:
    return {(finding.kind, txn_id) for finding in report.findings for txn_id in finding.txn_ids}


def _history(store: Store) -> None:
    """三个月杭州样本：建立「常用城市 = 杭州」的基线。"""
    _insert(store, "txn_h1", "2026-07-03", 5000, "杭州便利店")
    _insert(store, "txn_h2", "2026-08-04", 5000, "杭州书店")
    _insert(store, "txn_h3", "2026-08-05", 5000, "杭州咖啡")


def test_late_night_has_two_positives_and_one_negative(
    bill_store: tuple[Store, FactGateway],
) -> None:
    store, gateway = bill_store
    _history(store)
    _insert(store, "txn_ln1", "2026-09-05", 5000, "夜宵铺", time_of_day="23:30")
    _insert(store, "txn_ln2", "2026-09-06", 5200, "凌晨药房", time_of_day="05:59")
    _insert(store, "txn_ln3", "2026-09-07", 5400, "午间食堂", time_of_day="12:00")

    kinds = _pairs(analyze_bill(gateway, DEMO_USER_ID, *PERIOD, as_of=AS_OF))

    assert ("late-night", "txn_ln1") in kinds
    assert ("late-night", "txn_ln2") in kinds
    assert ("late-night", "txn_ln3") not in kinds


def test_remote_city_has_two_positives_and_one_negative(
    bill_store: tuple[Store, FactGateway],
) -> None:
    store, gateway = bill_store
    _history(store)
    _insert(store, "txn_rc1", "2026-09-08", 6000, "深圳数码", city="深圳")
    _insert(store, "txn_rc2", "2026-09-09", 6200, "广州百货", city="广州")
    _insert(store, "txn_rc3", "2026-09-10", 6400, "杭州水果", city="杭州")

    kinds = _pairs(analyze_bill(gateway, DEMO_USER_ID, *PERIOD, as_of=AS_OF))

    assert ("remote-city", "txn_rc1") in kinds
    assert ("remote-city", "txn_rc2") in kinds
    assert ("remote-city", "txn_rc3") not in kinds


def test_null_time_and_city_are_skipped(bill_store: tuple[Store, FactGateway]) -> None:
    """「不适用」不等于异常：空时间不判深夜，空城市不判异地。"""
    store, gateway = bill_store
    _history(store)
    _insert(store, "txn_null_city", "2026-09-11", 5000, "本地小店", city=None)
    _insert(store, "txn_null_time", "2026-09-12", 5000, "午后茶饮", time_of_day=None, city="杭州")

    kinds = _pairs(analyze_bill(gateway, DEMO_USER_ID, *PERIOD, as_of=AS_OF))

    assert ("remote-city", "txn_null_city") not in kinds
    assert ("late-night", "txn_null_time") not in kinds


def test_remote_city_requires_a_city_baseline(
    bill_store: tuple[Store, FactGateway],
) -> None:
    """历史城市样本不足 2 条：不建立基线，异地判据不启用（避免新用户误报）。"""
    store, gateway = bill_store
    _insert(store, "txn_one", "2026-08-01", 5000, "杭州便利店", city="杭州")
    _insert(store, "txn_remote", "2026-09-02", 5000, "深圳数码", city="深圳")

    report = analyze_bill(gateway, DEMO_USER_ID, *PERIOD, as_of=AS_OF)

    assert ("remote-city", "txn_remote") not in _pairs(report)
    assert any("异地判据未启用" in note for note in report.notes)

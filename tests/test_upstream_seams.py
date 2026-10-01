"""上游接缝回归（06 复核发现 · README 裁决 #18/#19）。

两处均为**向后兼容的最小放宽**，不改任何冻结符号或签名：

1. `gateway.store.Store` 允许跨线程使用（FastAPI `TestClient` / uvicorn 同步端点在线程池中运行）。
2. `policy.pep._require_date` 接受 `datetime.date` 实例（02 目录把日期参数声明为 `py_type=date`）。
"""

import threading
from datetime import date
from pathlib import Path

from ai_banking_copilot.gateway.seed import seed_database
from ai_banking_copilot.gateway.store import Store, StoreMode, init_schema
from ai_banking_copilot.policy.pep import _require_date


def test_store_is_usable_from_another_thread(tmp_path: Path) -> None:
    db_path = tmp_path / "threaded.db"
    writer = Store(db_path)
    init_schema(writer)
    seed_database(writer)
    writer.close()
    reader = Store(db_path, StoreMode.READ_ONLY)
    errors: list[Exception] = []

    def _worker() -> None:
        try:
            rows = reader.query("SELECT 1 AS one")
            assert rows[0]["one"] == 1
        except Exception as error:
            errors.append(error)

    thread = threading.Thread(target=_worker)
    thread.start()
    thread.join()
    reader.close()
    assert errors == []


def test_require_date_accepts_a_date_instance() -> None:
    assert _require_date({"booked_on": date(2026, 10, 1)}, "booked_on") == date(2026, 10, 1)


def test_require_date_still_accepts_an_iso_string() -> None:
    assert _require_date({"booked_on": "2026-10-01"}, "booked_on") == date(2026, 10, 1)

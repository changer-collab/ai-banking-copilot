"""账单分析的**元数据**存储：线索核对状态与商户分类修正。

边界：
- 只写独立 SQLite 文件，不写银行事实库、不产生执行授权；
- 线索状态按 ``(user_id, period, lead_id)`` 去重，刷新后仍生效；
- 分类修正按 ``(user_id, merchant_key)`` 记忆，只影响后续分析的归类。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from ai_banking_copilot.scenarios.bill_categories import is_valid_category
from ai_banking_copilot.scenarios.bill_findings import LEAD_STATES


class BillAnalysisStore(Protocol):
    """账单分析元数据读写协议。"""

    def lead_states(self, user_id: str, period: str) -> dict[str, str]: ...

    def set_lead_state(
        self, user_id: str, period: str, lead_id: str, state: str, note: str = ""
    ) -> None: ...

    def category_overrides(self, user_id: str) -> dict[str, str]: ...

    def set_category_override(self, user_id: str, merchant_key: str, category: str) -> None: ...

    def close(self) -> None: ...


class MemoryBillAnalysisStore:
    """单进程演示 / 测试用内存实现。"""

    def __init__(self) -> None:
        self._leads: dict[tuple[str, str, str], str] = {}
        self._categories: dict[tuple[str, str], str] = {}

    def lead_states(self, user_id: str, period: str) -> dict[str, str]:
        return {
            lead_id: state
            for (stored_user, stored_period, lead_id), state in self._leads.items()
            if stored_user == user_id and stored_period == period
        }

    def set_lead_state(
        self, user_id: str, period: str, lead_id: str, state: str, note: str = ""
    ) -> None:
        if state not in LEAD_STATES:
            raise ValueError(f"未知线索状态：{state}")
        self._leads[(user_id, period, lead_id)] = state

    def category_overrides(self, user_id: str) -> dict[str, str]:
        return {
            merchant_key: category
            for (stored_user, merchant_key), category in self._categories.items()
            if stored_user == user_id
        }

    def set_category_override(self, user_id: str, merchant_key: str, category: str) -> None:
        if not is_valid_category(category):
            raise ValueError(f"未登记的分类：{category}")
        self._categories[(user_id, merchant_key)] = category

    def close(self) -> None:
        return None


class SQLiteBillAnalysisStore:
    """独立 SQLite 元数据存储；与银行事实库分离。"""

    _DDL = """
    CREATE TABLE IF NOT EXISTS bill_lead_states (
        user_id TEXT NOT NULL,
        period TEXT NOT NULL,
        lead_id TEXT NOT NULL,
        state TEXT NOT NULL,
        note TEXT NOT NULL DEFAULT '',
        updated_at TEXT NOT NULL,
        PRIMARY KEY (user_id, period, lead_id)
    );
    CREATE TABLE IF NOT EXISTS bill_category_overrides (
        user_id TEXT NOT NULL,
        merchant_key TEXT NOT NULL,
        category TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (user_id, merchant_key)
    );
    """

    def __init__(self, path: str | Path) -> None:
        db_path = Path(path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.executescript(self._DDL)
        self._conn.commit()

    def lead_states(self, user_id: str, period: str) -> dict[str, str]:
        rows = self._conn.execute(
            "SELECT lead_id, state FROM bill_lead_states WHERE user_id = ? AND period = ?",
            (user_id, period),
        ).fetchall()
        return {str(row[0]): str(row[1]) for row in rows}

    def set_lead_state(
        self, user_id: str, period: str, lead_id: str, state: str, note: str = ""
    ) -> None:
        if state not in LEAD_STATES:
            raise ValueError(f"未知线索状态：{state}")
        self._conn.execute(
            "INSERT INTO bill_lead_states(user_id, period, lead_id, state, note, updated_at)"
            " VALUES(?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(user_id, period, lead_id) DO UPDATE SET"
            " state = excluded.state, note = excluded.note, updated_at = excluded.updated_at",
            (user_id, period, lead_id, state, note, _now()),
        )
        self._conn.commit()

    def category_overrides(self, user_id: str) -> dict[str, str]:
        rows = self._conn.execute(
            "SELECT merchant_key, category FROM bill_category_overrides WHERE user_id = ?",
            (user_id,),
        ).fetchall()
        return {str(row[0]): str(row[1]) for row in rows}

    def set_category_override(self, user_id: str, merchant_key: str, category: str) -> None:
        if not is_valid_category(category):
            raise ValueError(f"未登记的分类：{category}")
        self._conn.execute(
            "INSERT INTO bill_category_overrides(user_id, merchant_key, category, updated_at)"
            " VALUES(?, ?, ?, ?)"
            " ON CONFLICT(user_id, merchant_key) DO UPDATE SET"
            " category = excluded.category, updated_at = excluded.updated_at",
            (user_id, merchant_key, category, _now()),
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()


def _now() -> str:
    return datetime.now(UTC).isoformat()


def lead_states_for(
    store: BillAnalysisStore, user_id: str, period: str
) -> Mapping[str, str]:
    """读取失败不阻断账单分析：退回空状态，页面仍可展示线索。"""
    try:
        return store.lead_states(user_id, period)
    except Exception:  # noqa: BLE001 - 元数据不可用不应让只读分析失败
        return {}


def category_overrides_for(
    store: BillAnalysisStore, user_id: str
) -> Mapping[str, str]:
    try:
        return store.category_overrides(user_id)
    except Exception:  # noqa: BLE001
        return {}

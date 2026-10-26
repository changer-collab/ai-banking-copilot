"""安全动作独立持久化：交易密码**修改事件**（**不改** 01 的 `SCHEMA_SQL`）。

口径（`docs/三色基准表-2026-10-26.md` 第三节 `password.change` 行）：
- 赛题原文三级权限把「密码修改」列为**红色**，故该动作必须有真实落点，否则只是空承诺；
- **密码明文与摘要都不进本表**：旧密码 / 新密码一律不落库、不进审计、不进模型上下文，
  本表只登记「某用户在基准日通过强验证**发起**了一次密码修改」这一事件；
- 与 `gateway/transfer_store.py` 同规矩：**独立模块自带 DDL + 幂等 init**，
  严禁并入 `gateway/store.py::SCHEMA_SQL`；回读走 `FactGateway` 供 H1 出处校验。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

PASSWORD_CHANGE_EVENTS_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS password_change_events (
    event_id     TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL REFERENCES users(user_id),
    status       TEXT NOT NULL,
    requested_on TEXT NOT NULL
);
"""
"""交易密码修改事件表（独立 DDL；**不含**任何密码字段）。"""


@dataclass(frozen=True)
class PasswordChangeEvent:
    """一次密码修改**事件**的事实快照（`password_change_events` 行）。"""

    event_id: str
    user_id: str
    status: str
    requested_on: date

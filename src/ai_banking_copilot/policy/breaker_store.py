"""熔断状态的 SQLite 实现：**跨重启 / 跨 worker 可见**（`docs/待修复清单.md` P1-3 残留）。

设计约束：
- **独立表**（`breaker_state`），由本模块自建，**不并入 01 的 `SCHEMA_SQL`**
  （对齐 01 `gateway/risk_store.py` 的既有先例与「不得修改 `SCHEMA_SQL`」的硬约束）；
- **不改变语义**：本模块只负责「读回 / 落盘」快照；「只加严不降级」仍由 `CircuitBreaker` 保证，
  `unlock()` 仍是唯一降级出口；
- **手写 SQL 单一真源**：表名与列名只在本模块出现。

已知边界（如实声明，不粉饰）：
- 计数字段在多写者下是**尽力而为**（后写覆盖，随后会被聚合回较大值）；**权威的是状态位**；
- 默认**不启用**（见 `compose.build_breaker`）：一旦启用，`OPEN` 会**跨重启保留**——这是
  「只加严不降级」的正确语义，但本项目**尚无运维解锁入口**，演示前需知悉。
"""

from __future__ import annotations

from ai_banking_copilot.gateway.store import Store
from ai_banking_copilot.policy.breaker import (
    BreakerSnapshot,
    BreakerState,
    BreakerTrigger,
)

BREAKER_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS breaker_state (
    breaker_id TEXT PRIMARY KEY,
    state      TEXT NOT NULL CHECK (state IN ('closed', 'open')),
    failures   INTEGER NOT NULL CHECK (failures >= 0),
    triggers   TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""
"""熔断状态表（独立 DDL；不并入 01 的 `SCHEMA_SQL`）。"""

_TABLE = "breaker_state"
SINGLETON_BREAKER_ID = "default"
"""默认主键：本项目的熔断器是**全局单例**（进程内唯一 + 跨 worker 共享同一行）。"""


def init_breaker_schema(store: Store) -> None:
    """建熔断状态表（幂等；既有库自动补表）。"""
    with store.transaction() as conn:
        conn.executescript(BREAKER_SCHEMA_SQL)


class StoreBreakerStateStore:
    """把熔断快照存在 SQLite 的独立表里：新的实例（新进程 / 新 worker）能读回上次的状态。"""

    def __init__(self, store: Store, breaker_id: str = SINGLETON_BREAKER_ID) -> None:
        if not breaker_id:
            raise ValueError("breaker_id 不能为空")
        self._store = store
        self._breaker_id = breaker_id

    def load(self) -> BreakerSnapshot | None:
        """读回上次落盘的快照；无记录返回 `None`。"""
        rows = self._store.query(
            f"SELECT state, failures, triggers FROM {_TABLE} WHERE breaker_id = ?",
            (self._breaker_id,),
        )
        if not rows:
            return None
        row = rows[0]
        return BreakerSnapshot(
            state=BreakerState(str(row["state"])),
            failures=int(row["failures"]),
            triggers=tuple(
                BreakerTrigger(part)
                for part in str(row["triggers"]).split(",")
                if part
            ),
        )

    def save(self, snapshot: BreakerSnapshot) -> None:
        """落盘快照（单行 upsert；`updated_at` 由 SQLite 侧刷新，便于人工确认「最后一次变更」）。"""
        if not isinstance(snapshot, BreakerSnapshot):
            raise TypeError(
                f"snapshot 必须是 BreakerSnapshot，收到 {type(snapshot).__name__}"
            )
        with self._store.transaction() as conn:
            conn.execute(
                f"INSERT INTO {_TABLE} (breaker_id, state, failures, triggers)"
                " VALUES (?, ?, ?, ?)"
                " ON CONFLICT(breaker_id) DO UPDATE SET"
                " state = excluded.state,"
                " failures = excluded.failures,"
                " triggers = excluded.triggers,"
                " updated_at = datetime('now')",
                (
                    self._breaker_id,
                    snapshot.state.value,
                    snapshot.failures,
                    ",".join(trigger.value for trigger in snapshot.triggers),
                ),
            )

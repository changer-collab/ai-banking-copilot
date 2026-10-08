"""异常熔断：五类触发 + 只加严不降级 + 转人工接管（PRD §5.8）。

五类触发（任一命中即熔断）：
1. 连续授权失败 ≥ N（默认 3）
2. 注入检测命中
3. 短时高频高风险操作
4. 策略引擎连续拒绝达阈值
5. 会话异常设备 / 地点跳变

动作：**临时锁定高风险操作**（纯查询不受影响）→ 转 `MANUAL` 人工接管 → 写审计事件；
解锁需二次确认 + 留痕。

**熔断只加严、不降级（hard）**：打开后 `record_success()` 不得改回 `CLOSED`；
**唯一**降级出口是显式 `unlock()`（调用方负责二次确认 + 留痕）。

**状态可持久化**（`docs/已修复清单-2026-10-08.md` P1-3 残留）：本模块只定义
`BreakerStateStore` 接缝与
`BreakerSnapshot`；SQLite 实现在 `policy/breaker_store.py`（独立表，不并入 01 的 `SCHEMA_SQL`）。
不注入存储时行为与原先**逐字一致**（纯进程内、零 I/O）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from typing import Protocol, runtime_checkable

DEFAULT_FAILURE_THRESHOLD = 3

DEFAULT_POLICY_REJECTION_THRESHOLD = 3
"""策略引擎**连续**拒绝达该阈值即熔断（触发 4）。"""

HIGH_FREQUENCY_LIMIT = 5
"""`HIGH_FREQUENCY_WINDOW` 内高风险操作达该次数即熔断（触发 3）。"""

HIGH_FREQUENCY_WINDOW = timedelta(seconds=60)
"""高频判定滑动窗口（触发 3）。"""


class BreakerState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"


class BreakerTrigger(StrEnum):
    """PRD §5.8 的五类熔断触发。"""

    CONSECUTIVE_AUTH_FAILURES = "consecutive_auth_failures"
    INJECTION_DETECTED = "injection_detected"
    HIGH_FREQUENCY_RISK = "high_frequency_risk"
    POLICY_REJECTIONS = "policy_rejections"
    SESSION_ANOMALY = "session_anomaly"


TRIGGERS: tuple[BreakerTrigger, ...] = tuple(BreakerTrigger)
"""五类触发的封闭集合（对外可枚举、可断言）。"""


@dataclass(frozen=True)
class BreakerSnapshot:
    """熔断器的**可持久化快照**：状态位 + 连续失败计数 + 已触发项。"""

    state: BreakerState
    failures: int = 0
    triggers: tuple[BreakerTrigger, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.state, BreakerState):
            raise TypeError(f"state 必须是 BreakerState 成员，收到 {type(self.state).__name__}")
        if isinstance(self.failures, bool) or not isinstance(self.failures, int):
            raise TypeError("failures 必须是整数")
        if self.failures < 0:
            raise ValueError("failures 不能为负")


@runtime_checkable
class BreakerStateStore(Protocol):
    """熔断状态的**持久化接缝**（默认无实现 ⇒ 纯进程内；SQLite 实现在 `policy/breaker_store.py`）。

    只做「读回上次快照 / 落盘当前快照」两件事——不参与判定，不改变「只加严不降级」的语义。
    """

    def load(self) -> BreakerSnapshot | None:
        """读回上次落盘的快照；无记录返回 `None`（新实例从 `CLOSED` 起）。"""
        ...

    def save(self, snapshot: BreakerSnapshot) -> None:
        """落盘当前快照（每次状态变更后调用）。"""
        ...


class CircuitBreaker:
    """熔断器：状态只可加严，降级必须显式解锁。

    `store` 非空时状态**跨重启 / 跨 worker 可见**（详见 `policy/breaker_store.py`）：
    构造时装载、每次变更落盘、每次读取先刷新。计数在多写者下是**尽力而为**（后写覆盖），
    但**状态位与触发项只会聚合、不会静默回退**——`unlock()` 仍是唯一降级出口。
    """

    def __init__(
        self,
        threshold: int = DEFAULT_FAILURE_THRESHOLD,
        store: BreakerStateStore | None = None,
    ) -> None:
        if isinstance(threshold, bool) or not isinstance(threshold, int) or threshold < 1:
            raise ValueError("熔断阈值必须是 ≥ 1 的整数")
        if store is not None and not isinstance(store, BreakerStateStore):
            raise TypeError("store 必须实现 BreakerStateStore 协议")
        self._threshold = threshold
        self._failures = 0
        self._state = BreakerState.CLOSED
        self._fired: list[BreakerTrigger] = []
        self._store = store
        if store is not None:
            snapshot = store.load()
            if snapshot is not None:
                self._adopt(snapshot)

    def _adopt(self, snapshot: BreakerSnapshot) -> None:
        """采纳快照：状态位**跟随存储**（`unlock()` 才能跨实例生效），计数与触发项**只聚合**。"""
        self._state = snapshot.state
        self._failures = max(self._failures, snapshot.failures)
        for trigger in snapshot.triggers:
            if trigger not in self._fired:
                self._fired.append(trigger)

    def _refresh(self) -> None:
        """读取前先同步存储（无存储时为纯进程内，零开销）。"""
        if self._store is None:
            return
        snapshot = self._store.load()
        if snapshot is not None:
            self._adopt(snapshot)

    def _persist(self) -> None:
        if self._store is None:
            return
        self._store.save(
            BreakerSnapshot(
                state=self._state,
                failures=self._failures,
                triggers=tuple(self._fired),
            )
        )

    def record_failure(self) -> None:
        """记录一次授权失败；连续失败达阈值即按触发 1 熔断。"""
        self._refresh()
        self._failures += 1
        if self._failures >= self._threshold:
            self._trip(BreakerTrigger.CONSECUTIVE_AUTH_FAILURES)
        self._persist()

    def record_success(self) -> None:
        """记录一次成功：**仅**在 `CLOSED` 时清空连续失败计数（只加严不降级）。"""
        self._refresh()
        if self._state is BreakerState.OPEN:
            return
        self._failures = 0
        self._persist()

    def trip(self, trigger: BreakerTrigger) -> None:
        """按触发项 2–5（或任意登记触发）显式熔断。"""
        if not isinstance(trigger, BreakerTrigger):
            raise TypeError(
                f"trigger must be a BreakerTrigger member, got {type(trigger).__name__}"
            )
        self._trip(trigger)
        self._persist()

    def is_open(self) -> bool:
        self._refresh()
        return self._state is BreakerState.OPEN

    def lock(self) -> None:
        """手动锁定（注入 / 高频 / 会话异常等外部判定后调用）。"""
        self._state = BreakerState.OPEN
        self._persist()

    def unlock(self) -> None:
        """解锁：**唯一**降级出口；调用方必须完成二次确认 + 留痕。"""
        self._state = BreakerState.CLOSED
        self._failures = 0
        self._persist()

    def state(self) -> BreakerState:
        self._refresh()
        return self._state

    def triggers(self) -> tuple[BreakerTrigger, ...]:
        self._refresh()
        return tuple(self._fired)

    def failure_count(self) -> int:
        self._refresh()
        return self._failures

    def _trip(self, trigger: BreakerTrigger) -> None:
        self._state = BreakerState.OPEN
        if trigger not in self._fired:
            self._fired.append(trigger)

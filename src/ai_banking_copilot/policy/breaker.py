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
"""

from __future__ import annotations

from enum import Enum

DEFAULT_FAILURE_THRESHOLD = 3


class BreakerState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"


class BreakerTrigger(str, Enum):
    """PRD §5.8 的五类熔断触发。"""

    CONSECUTIVE_AUTH_FAILURES = "consecutive_auth_failures"
    INJECTION_DETECTED = "injection_detected"
    HIGH_FREQUENCY_RISK = "high_frequency_risk"
    POLICY_REJECTIONS = "policy_rejections"
    SESSION_ANOMALY = "session_anomaly"


TRIGGERS: tuple[BreakerTrigger, ...] = tuple(BreakerTrigger)
"""五类触发的封闭集合（对外可枚举、可断言）。"""


class CircuitBreaker:
    """熔断器：状态只可加严，降级必须显式解锁。"""

    def __init__(self, threshold: int = DEFAULT_FAILURE_THRESHOLD) -> None:
        if isinstance(threshold, bool) or not isinstance(threshold, int) or threshold < 1:
            raise ValueError("熔断阈值必须是 ≥ 1 的整数")
        self._threshold = threshold
        self._failures = 0
        self._state = BreakerState.CLOSED
        self._fired: list[BreakerTrigger] = []

    def record_failure(self) -> None:
        """记录一次授权失败；连续失败达阈值即按触发 1 熔断。"""
        self._failures += 1
        if self._failures >= self._threshold:
            self._trip(BreakerTrigger.CONSECUTIVE_AUTH_FAILURES)

    def record_success(self) -> None:
        """记录一次成功：**仅**在 `CLOSED` 时清空连续失败计数（只加严不降级）。"""
        if self._state is BreakerState.OPEN:
            return
        self._failures = 0

    def trip(self, trigger: BreakerTrigger) -> None:
        """按触发项 2–5（或任意登记触发）显式熔断。"""
        if not isinstance(trigger, BreakerTrigger):
            raise TypeError(
                f"trigger must be a BreakerTrigger member, got {type(trigger).__name__}"
            )
        self._trip(trigger)

    def is_open(self) -> bool:
        return self._state is BreakerState.OPEN

    def lock(self) -> None:
        """手动锁定（注入 / 高频 / 会话异常等外部判定后调用）。"""
        self._state = BreakerState.OPEN

    def unlock(self) -> None:
        """解锁：**唯一**降级出口；调用方必须完成二次确认 + 留痕。"""
        self._state = BreakerState.CLOSED
        self._failures = 0

    def state(self) -> BreakerState:
        return self._state

    def triggers(self) -> tuple[BreakerTrigger, ...]:
        return tuple(self._fired)

    def failure_count(self) -> int:
        return self._failures

    def _trip(self, trigger: BreakerTrigger) -> None:
        self._state = BreakerState.OPEN
        if trigger not in self._fired:
            self._fired.append(trigger)

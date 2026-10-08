"""撤销窗口 + 补偿事务（saga）：部分成功如实告知，竞态不得谎称「已取消」。

竞态纪律（PRD §5.5，硬约束 10）：
- 已进入结算（settled / settling）时**不得宣称「已取消」**，必须走补偿事务并如实告知
  「本期已扣、已发起退回」。
- 补偿失败必须显式暴露（`CompensationError`），不得静默吞掉。
- "部分成功" = DAG 节点级事务 + saga 补偿，对外展示「已完成 X、已回滚 Y」。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

DEFAULT_REVERSAL_TTL = timedelta(hours=24)

Forward = Callable[[], str]
"""正向动作：返回回执标识（如 record_id）。"""

Rollback = Callable[[], str]
"""补偿动作：返回补偿结果标识。"""

Compensate = Callable[[], tuple[str, ...]]
"""批量补偿入口（通常是 `Saga.compensate`）：返回已回滚步骤名。"""


class CompensationError(RuntimeError):
    """补偿（回滚）失败：必须如实告知，且不得宣称已取消。"""


class ReversalState(StrEnum):
    REVERSED = "reversed"
    COMPENSATING = "compensating"
    EXPIRED = "expired"
    FAILED = "failed"


@dataclass(frozen=True)
class ReversalOutcome:
    state: ReversalState
    message: str
    compensated_steps: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReversalWindow:
    """撤销窗口：`deadline` 内可撤销；过期或已结算走补偿并如实告知。"""

    subject_id: str
    opened_at: datetime
    deadline: datetime

    @classmethod
    def open(
        cls, subject_id: str, *, now: datetime, ttl: timedelta = DEFAULT_REVERSAL_TTL
    ) -> ReversalWindow:
        if not subject_id:
            raise ValueError("subject_id 不得为空")
        if ttl <= timedelta(0):
            raise ValueError("撤销窗口 TTL 必须为正")
        return cls(subject_id=subject_id, opened_at=now, deadline=now + ttl)

    def expired(self, now: datetime) -> bool:
        return now >= self.deadline

    def reverse(
        self, *, now: datetime, settled: bool, compensate: Compensate | None = None
    ) -> ReversalOutcome:
        """撤销 / 补偿。`settled=True` 表示已进入结算（竞态），必须走补偿。"""
        if self.expired(now):
            return ReversalOutcome(
                state=ReversalState.EXPIRED,
                message=f"{self.subject_id} 撤销窗口已关闭，需人工处理",
            )
        if not settled:
            return ReversalOutcome(
                state=ReversalState.REVERSED,
                message=f"{self.subject_id} 已撤销（尚未结算，无扣款）",
            )
        if compensate is None:
            raise CompensationError(
                f"{self.subject_id} 已结算：必须走补偿事务并如实告知，不得谎称已取消"
            )
        try:
            steps = compensate()
        except CompensationError as error:
            return ReversalOutcome(
                state=ReversalState.FAILED,
                message=f"{self.subject_id} 补偿失败（{error}）：已如实告知，未宣称已取消",
            )
        return ReversalOutcome(
            state=ReversalState.COMPENSATING,
            message=f"{self.subject_id} 本期已扣、已按补偿事务退回",
            compensated_steps=steps,
        )


@dataclass(frozen=True)
class SagaStep:
    name: str
    forward: Forward
    rollback: Rollback


class Saga:
    """顺序执行 + **逆序**补偿：对外可报告"已完成 X、已回滚 Y"。"""

    def __init__(self) -> None:
        self._steps: list[SagaStep] = []
        self._completed: list[str] = []

    def add(self, name: str, forward: Forward, rollback: Rollback) -> None:
        if not name:
            raise ValueError("步骤名不得为空")
        if any(step.name == name for step in self._steps):
            raise ValueError(f"步骤名重复：{name}")
        self._steps.append(SagaStep(name=name, forward=forward, rollback=rollback))

    def run(self) -> tuple[str, ...]:
        """顺序执行全部步骤；任一失败即停止并抛出**原异常**（已完成步骤保留待补偿）。"""
        for step in self._steps:
            if step.name in self._completed:
                continue
            step.forward()
            self._completed.append(step.name)
        return tuple(self._completed)

    def compensate(self) -> tuple[str, ...]:
        """按逆序回滚已完成步骤；任一回滚失败抛 `CompensationError`（如实暴露）。"""
        rolled_back: list[str] = []
        while self._completed:
            name = self._completed[-1]
            step = self._step_named(name)
            try:
                step.rollback()
            except CompensationError:
                raise
            except Exception as error:
                raise CompensationError(f"补偿 {name} 失败：{error}") from error
            self._completed.pop()
            rolled_back.append(name)
        return tuple(rolled_back)

    def completed(self) -> tuple[str, ...]:
        """已成功执行的步骤名（失败后用于"已完成 X、已回滚 Y"的报告）。"""
        return tuple(self._completed)

    def _step_named(self, name: str) -> SagaStep:
        for step in self._steps:
            if step.name == name:
                return step
        raise CompensationError(f"未知步骤：{name}")

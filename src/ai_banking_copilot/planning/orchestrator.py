"""最小可插桩步骤驱动器（Task 0 spike 产出 · 自研 fallback 骨架）。

设计要点（为什么"不可绕过"）：
- **唯一迁移出口**：所有状态迁移只经过 `_advance`；没有第二条修改 `_states` 的路径，
  因此插桩点无法被业务代码绕过。
- **先审计后鉴权**：`on_transition` 先记录（含被拒的尝试），`authorize` 再放行；
  鉴权不通过则**状态不推进**并抛错——审计留痕与安全拦截各司其职。
- **零依赖**：只用标准库；可被 `policy.pep` 或框架适配层复用。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum


class StepState(StrEnum):
    """步骤状态机。"""

    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


@dataclass(frozen=True)
class StepTransition:
    """一次状态转移（审计与鉴权的共同载荷）。"""

    step_id: str
    before: StepState
    after: StepState


AuditHook = Callable[[StepTransition], None]
"""审计插桩：必须对**每一次尝试的迁移**都被调用。"""

AuthzHook = Callable[[StepTransition], bool]
"""鉴权插桩：返回 False 即拒绝该迁移。"""


class StepDriver:
    """步骤驱动器：每个状态转移都强制过插桩点。"""

    def __init__(self, on_transition: AuditHook, authorize: AuthzHook | None = None) -> None:
        self._on_transition = on_transition
        self._authorize = authorize
        self._states: dict[str, StepState] = {}
        self._transitions: list[StepTransition] = []

    def run(
        self, steps: Sequence[str], worker: Callable[[str], None]
    ) -> tuple[StepTransition, ...]:
        """按给定顺序执行步骤；返回全部（含被拒）迁移记录。"""
        for step_id in steps:
            self._states[step_id] = StepState.PENDING
        for step_id in steps:
            self._advance(step_id, StepState.RUNNING)
            try:
                worker(step_id)
            except Exception:
                self._advance(step_id, StepState.FAILED)
                raise
            self._advance(step_id, StepState.DONE)
        return tuple(self._transitions)

    def state_of(self, step_id: str) -> StepState:
        return self._states[step_id]

    def _advance(self, step_id: str, after: StepState) -> None:
        """**唯一**的状态迁移出口：先审计，后鉴权，再做实际推进。"""
        before = self._states[step_id]
        transition = StepTransition(step_id=step_id, before=before, after=after)
        self._on_transition(transition)
        if self._authorize is not None and not self._authorize(transition):
            raise PermissionError(
                f"状态迁移被鉴权插桩拒绝：{step_id} {before.value}->{after.value}"
            )
        self._states[step_id] = after
        self._transitions.append(transition)

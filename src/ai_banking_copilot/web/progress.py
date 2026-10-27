"""问答阶段进度：**旁路观测**，绝不参与状态机。

用户在答案生成前需要看到「现在走到哪一步」。做法是进程内一张小表：
`submit` 在 4 个真实边界回调阶段，接口层写入；前端 500ms 轮询，终态即停。

纪律：

1. **只记录真实阶段**：阶段名对应 `Session._submit` 的实际执行边界，不按时间伪造；
2. **失败不影响主流程**：`Session` 侧回调包 `try/except`，进度写失败等于没有进度；
3. **自动过期**：条目 `PROGRESS_TTL_SECONDS` 秒后失效，避免内存膨胀；
4. **键按用户隔离**：`(user_id, session_id, request_id)`，跨用户读不到别人的进度。
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

STAGES = ("guard", "understand", "plan", "review", "done")
"""真实阶段序列：安全守卫 → 意图理解 → 计划生成 → 审阅裁决 → 收尾。"""

STAGE_LABELS = {
    "guard": "安全校验",
    "understand": "理解意图",
    "plan": "生成计划",
    "review": "风险审阅",
    "done": "完成",
}

PROGRESS_TTL_SECONDS = 120
"""条目存活时长（秒）：一次问答远超不出这个量级。"""


@dataclass(frozen=True)
class ProgressState:
    """一次问答的当前阶段。"""

    stage: str
    finished: bool
    updated_at: str

    def to_dict(self) -> dict[str, object]:
        index = STAGES.index(self.stage)
        return {
            "stage": self.stage,
            "stages": [
                {
                    "key": key,
                    "label": STAGE_LABELS[key],
                    "done": position < index or self.finished,
                    "active": position == index and not self.finished,
                }
                for position, key in enumerate(STAGES)
            ],
            "finished": self.finished,
            "updated_at": self.updated_at,
        }


class ProgressRegistry:
    """进程内进度表；`RLock` 保护，读多写少。"""

    def __init__(
        self,
        *,
        ttl_seconds: int = PROGRESS_TTL_SECONDS,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._ttl = timedelta(seconds=ttl_seconds)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lock = threading.RLock()
        self._states: dict[tuple[str, str, str], tuple[datetime, ProgressState]] = {}

    def start(self, user_id: str, session_id: str, request_id: str) -> None:
        """进入第一个视觉阶段；不改变真实执行顺序，只是先把条目建出来。"""

        self._write(user_id, session_id, request_id, STAGES[0], finished=False)

    def advance(self, user_id: str, session_id: str, request_id: str, stage: str) -> None:
        if stage not in STAGES:
            raise ValueError(f"未知阶段：{stage}")
        self._write(user_id, session_id, request_id, stage, finished=stage == "done")

    def finish(self, user_id: str, session_id: str, request_id: str) -> None:
        self._write(user_id, session_id, request_id, "done", finished=True)

    def get(self, user_id: str, session_id: str, request_id: str) -> ProgressState | None:
        key = (user_id, session_id, request_id)
        with self._lock:
            entry = self._states.get(key)
            if entry is None:
                return None
            expires_at, state = entry
            if self._clock() >= expires_at:
                del self._states[key]
                return None
            return state

    def clear(self, user_id: str, session_id: str, request_id: str) -> None:
        with self._lock:
            self._states.pop((user_id, session_id, request_id), None)

    def _write(
        self, user_id: str, session_id: str, request_id: str, stage: str, *, finished: bool
    ) -> None:
        moment = self._clock()
        state = ProgressState(stage=stage, finished=finished, updated_at=moment.isoformat())
        with self._lock:
            self._purge(moment)
            self._states[(user_id, session_id, request_id)] = (moment + self._ttl, state)

    def _purge(self, moment: datetime) -> None:
        for key, (expires_at, _) in tuple(self._states.items()):
            if moment >= expires_at:
                del self._states[key]

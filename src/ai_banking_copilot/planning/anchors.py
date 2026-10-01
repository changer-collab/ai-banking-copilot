"""意图锚点账本：`IntentRef` 的可信来源登记（H2 / PRD §5.3 第 3 条）。

锚点来源三分：
- `explicit`：用户本轮明说（深度 0）；
- `user_approved`：用户在计划卡批准（深度 0）；
- `derived`：**机械推导**（零自由度），**父必须 ∈ {explicit, user_approved}**，深度 ≤ 1，
  **禁止 `derived → derived` 链式**。

为什么"机械推导"必须被约束：一旦允许 `derived → derived`，"推断"就能演化成
"无需用户确认也能执行"的路径——这是 H2 的破口，故在账本层直接拒绝。
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import count

from ai_banking_copilot.contracts.plan import AnchorSource


class DerivedDepthError(ValueError):
    """`derived` 锚点违反深度约束（父为 `derived`，或深度会超过 1）。"""


class UnknownAnchorError(KeyError):
    """引用了未登记的锚点。"""


@dataclass(frozen=True)
class Anchor:
    """一条锚点记录。"""

    anchor_id: str
    source: AnchorSource
    depth: int
    parent_id: str | None
    detail: str


class AnchorLedger:
    """锚点账本：只登记、不猜测。id 由单调序号生成（确定性、可复现）。"""

    def __init__(self) -> None:
        self._anchors: dict[str, Anchor] = {}
        self._counter = count(1)

    def grant_explicit(self, detail: str) -> str:
        """登记用户本轮明说的锚点，返回其 id。"""
        return self._grant(AnchorSource.EXPLICIT, 0, None, detail)

    def grant_approved(self, detail: str) -> str:
        """登记用户在计划卡批准的锚点，返回其 id。"""
        return self._grant(AnchorSource.USER_APPROVED, 0, None, detail)

    def derive(self, parent_id: str, detail: str) -> str:
        """登记一次**机械推导**。父必须是深度 0 的 explicit / user_approved。"""
        parent = self.resolve(parent_id)
        if parent.source is AnchorSource.DERIVED or parent.depth >= 1:
            raise DerivedDepthError(
                f"derived 的父必须是 explicit/user_approved 且深度 ≤ 1：{parent_id} 不满足"
            )
        return self._grant(AnchorSource.DERIVED, parent.depth + 1, parent_id, detail)

    def resolve(self, anchor_id: str) -> Anchor:
        try:
            return self._anchors[anchor_id]
        except KeyError as error:
            raise UnknownAnchorError(f"未登记的锚点：{anchor_id}") from error

    def anchors(self) -> tuple[Anchor, ...]:
        """按登记顺序返回全部锚点（dict 保序）。"""
        return tuple(self._anchors.values())

    def _grant(self, source: AnchorSource, depth: int, parent_id: str | None, detail: str) -> str:
        anchor_id = f"a{next(self._counter)}"
        self._anchors[anchor_id] = Anchor(
            anchor_id=anchor_id,
            source=source,
            depth=depth,
            parent_id=parent_id,
            detail=detail,
        )
        return anchor_id

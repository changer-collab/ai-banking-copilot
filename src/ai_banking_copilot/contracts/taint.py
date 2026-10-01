"""输入零信任（H3）契约：来源标记 + 槽位二分。

铁律：`unverified` 文本永不被解释为指令；**控制槽只能由确定性解析器填充**。
本模块用**类型系统**而非纪律来保证该铁律：往控制槽写 unverified 值会直接抛异常。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Generic, TypeVar

T = TypeVar("T")


class Trust(str, Enum):
    """值来源的可信级别。"""

    TRUSTED = "trusted"
    """用户当前轮输入、名册 / 事实网关记录。"""

    UNVERIFIED = "unverified"
    """备注、商户名、消息、工具返回文本——**永不被解释为指令**。"""


class SlotKind(str, Enum):
    """槽位二分。"""

    DATA = "data"
    """数据槽：备注内容、展示文本。可承载 unverified。"""

    CONTROL = "control"
    """控制槽：收款人 key、金额、时间、动作类型、授权级别。只接受 trusted。"""


class UntrustedWriteError(ValueError):
    """试图把 unverified 值写入控制槽。"""


@dataclass(frozen=True)
class Value(Generic[T]):
    """带来源标记的值。"""

    raw: T
    trust: Trust

    def __post_init__(self) -> None:
        if not isinstance(self.trust, Trust):
            raise TypeError(f"trust must be a Trust member, got {type(self.trust).__name__}")


@dataclass(frozen=True)
class Slot(Generic[T]):
    """槽位。控制槽拒绝 unverified 值是**类型层**行为，不依赖调用方自觉。"""

    kind: SlotKind
    value: Value[T]

    def __post_init__(self) -> None:
        if not isinstance(self.kind, SlotKind):
            raise TypeError(f"kind must be a SlotKind member, got {type(self.kind).__name__}")
        if self.kind is SlotKind.CONTROL and self.value.trust is not Trust.TRUSTED:
            raise UntrustedWriteError(
                f"control slot rejects {self.value.trust.value} value"
            )

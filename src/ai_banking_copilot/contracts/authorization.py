"""权限分级契约：**官方三色为主干** + 增强层。

官方口径（硬约束）：
- 绿色 = 自动执行，**仅纯查询**（余额 / 交易记录 / 账单分析 / 理财收益查询）
- 黄色 = 用户确认（小额转账 日累计 ≤ 1000 元、订阅查询/取消、虚拟卡申请）
- 红色 = 强验证（大额转账 > 1000 元、密码修改、挂失/解挂、理财申购）

增强层（红线之上的兜底）：`MANUAL` 人工接管、`DENY` 拒绝。
合成规则：`fail-safe` —— 取**最严**级别，并**并集**全部原因（保证可解释）。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum, StrEnum


class Level(IntEnum):
    """授权级别。数值越大越严。"""

    GREEN = 0
    """官方绿色：自动执行（**仅纯查询**）。"""

    YELLOW = 1
    """官方黄色：用户确认。"""

    RED = 2
    """官方红色：强验证（多因子）。"""

    MANUAL = 3
    """增强层：人工接管。"""

    DENY = 4
    """增强层：拒绝并解释。"""


class ReasonCode(StrEnum):
    """命中原因码——决定可解释、可审计，且规则变更须留痕。"""

    QUERY_ONLY = "QUERY_ONLY"
    STATE_CHANGE_SMALL = "STATE_CHANGE_SMALL"
    AMOUNT_OVER_DAILY_LIMIT = "AMOUNT_OVER_DAILY_LIMIT"
    IRREVERSIBLE = "IRREVERSIBLE"
    MANUAL_REVIEW = "MANUAL_REVIEW"
    OBJECT_BLOCKED = "OBJECT_BLOCKED"
    OFFICIAL_RED = "OFFICIAL_RED"
    """官方明文点名红色、但金额档 / 可逆性推不出红色的动作（理财申购、卡片解挂）。"""


@dataclass(frozen=True)
class Decision:
    """一次授权裁决：级别 + 至少一条原因。"""

    level: Level
    reasons: tuple[ReasonCode, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.level, Level):
            raise TypeError(f"level must be a Level member, got {type(self.level).__name__}")
        if not self.reasons:
            raise ValueError("decision requires at least one reason")


def strictest(*decisions: Decision) -> Decision:
    """fail-safe 合成：取最严级别，并集全部原因。"""
    if not decisions:
        raise ValueError("strictest() requires at least one decision")
    level = max(d.level for d in decisions)
    reasons: list[ReasonCode] = []
    for d in decisions:
        for r in d.reasons:
            if r not in reasons:
                reasons.append(r)
    return Decision(level=level, reasons=tuple(reasons))


def green_or_raise(is_pure_query: bool) -> Decision:
    """绿色只允许纯查询；否则直接抛错（防止资金/状态变更溜进绿色）。"""
    if not is_pure_query:
        raise ValueError("GREEN 只允许纯查询：任何资金/状态变更不得落入绿色")
    return Decision(level=Level.GREEN, reasons=(ReasonCode.QUERY_ONLY,))

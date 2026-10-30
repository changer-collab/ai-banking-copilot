"""多轮指代识别：只判断「这句话在指什么」，不做事实补全。

纪律：
- **纯规则、零生成**：不调用模型，不猜名字，不补金额以外的任何槽位；
- 只识别四类可审计的指代：重做上一笔 / 只改金额 / 换账户（卡）/ 撤回上一笔；
- 名称、账户、金额一律由调用方从**事实网关**取，本模块只给类型与金额。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum


class ReferenceKind(StrEnum):
    NONE = "none"
    REPEAT = "repeat"
    AMOUNT_ONLY = "amount_only"
    SWITCH_ACCOUNT = "switch_account"
    UNDO = "undo"


@dataclass(frozen=True)
class DialogueMemory:
    """上一笔**已成功执行**的计划里，可被指代的最小事实集。"""

    action: str
    payee_id: str | None = None
    account_id: str | None = None
    amount_cents: int | None = None
    card_id: str | None = None


@dataclass(frozen=True)
class Reference:
    """识别结果：`amount_cents` 仅在 `AMOUNT_ONLY` 时有值。"""

    kind: ReferenceKind
    amount_cents: int | None = None


_REPEAT = re.compile(r"(再转一次|再来一次|再来一笔|照刚才那样再来)")
_UNDO = re.compile(
    r"(刚才那笔|上一笔|刚刚那笔).{0,6}(取消|撤回|撤销)"
    r"|(撤回|撤销|取消)(刚才|上一笔)"
)
_SWITCH_ACCOUNT = re.compile(
    r"(换个账户|换一个账户|用另一个账户|换张卡|换一张卡|用另一张卡|用另一张卡转)"
)
_AMOUNT_ONLY = re.compile(r"^再转\s*([0-9]+(?:\.[0-9]{1,2})?)\s*元?$")


def resolve_reference(utterance: str, memory: DialogueMemory | None) -> Reference:
    """识别指代类型；没有上一笔时一律 `NONE`（不猜、不补）。"""
    text = utterance.strip()
    if memory is None or not text:
        return Reference(ReferenceKind.NONE)

    if _UNDO.search(text):
        return Reference(ReferenceKind.UNDO)
    if _REPEAT.search(text):
        return Reference(ReferenceKind.REPEAT)
    amount = _AMOUNT_ONLY.match(text)
    if amount is not None and memory.action == "transfer.create":
        return Reference(ReferenceKind.AMOUNT_ONLY, _cents(amount.group(1)))
    if _SWITCH_ACCOUNT.search(text) and memory.action == "transfer.create":
        return Reference(ReferenceKind.SWITCH_ACCOUNT)
    return Reference(ReferenceKind.NONE)


def _cents(yuan: str) -> int:
    """「200」「200.5」→ 分（纯十进制字符串运算，不经浮点）。"""
    whole, _, fraction = yuan.partition(".")
    fraction = (fraction + "00")[:2]
    return int(whole) * 100 + int(fraction)

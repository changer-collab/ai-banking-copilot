"""措辞层：在**已清洗的事实束**上做文字美化。

硬约束：
- 输入是 `FactLine` 列表（不含内部 ID），模型不得新增数字 / 日期 / 商户 / 状态；
- 输出做一次事实回查：出现事实束之外的数字或日期即判失败，退回模板；
- `AIB_DISABLE_LLM=1`、无模型、超时、异常一律退回模板（绝不阻塞主链路）。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Sequence

from ai_banking_copilot.web.present import FactLine

LOGGER = logging.getLogger(__name__)

_MONEY_LIKE = re.compile(r"\d+(?:\.\d+)?")
_DATE_LIKE = re.compile(r"\d{4}-\d{2}-\d{2}")

Polisher = Callable[[str, Sequence[FactLine]], str | None]
"""`(模板文案, 事实束) -> 改写结果`；返回 `None` 表示不可用。"""


def polish(template: str, facts: Sequence[FactLine], polisher: Polisher | None) -> str:
    """**任何异常都退回模板**：措辞永远不是主链路。"""
    if polisher is None:
        return template
    try:
        candidate = polisher(template, facts)
    except Exception:
        LOGGER.warning("措辞层失败，退回模板", exc_info=True)
        return template
    if not isinstance(candidate, str) or not candidate.strip():
        return template
    if _adds_unknown_facts(candidate, template, facts):
        LOGGER.warning("措辞层引入事实束之外的数值，退回模板")
        return template
    return candidate.strip()


def _adds_unknown_facts(candidate: str, template: str, facts: Sequence[FactLine]) -> bool:
    """只允许出现模板或事实束里已有的数字 / 日期。"""
    allowed = set(_MONEY_LIKE.findall(template)) | set(_DATE_LIKE.findall(template))
    for line in facts:
        allowed.update(_MONEY_LIKE.findall(line.value))
        allowed.update(_DATE_LIKE.findall(line.value))
    produced = set(_MONEY_LIKE.findall(candidate)) | set(_DATE_LIKE.findall(candidate))
    return bool(produced - allowed)

"""未识别意图时的候选功能推荐。

LLM 只能从 `CAPABILITIES`（固定能力清单）里挑，**不得新增清单外入口**、不得给数字或日期；
校验失败 / 无模型 / 超时 → 用规则兜底。这样既不用手写推荐算法，也不会编出不存在的能力。
"""

from __future__ import annotations

from collections.abc import Callable

CAPABILITIES: dict[str, str] = {
    "查一下余额": "account.balance",
    "查上月账单": "account.transactions",
    "把视频会员的自动续费取消": "subscription.cancel",
    "查一下我的订阅": "subscription.list",
    "我要做风险测评": "wealth.assess",
    "看看我的理财持仓": "wealth.list",
    "我下周生日": "life.L1",
    "给妈妈过生日，预算 800 元": "life.L1",
    "卡片挂失": "card.report_lost",
}

_DEFAULT = ("查一下余额", "查上月账单", "查一下我的订阅")

SuggestionProvider = Callable[[str, tuple[str, ...]], object]
"""推荐模型签名：`(用户原话, 固定清单)` → 任意结构；只有命中清单的字符串会被采纳。"""


def select_suggestions(
    utterance: str, *, llm: SuggestionProvider | None = None,
) -> list[dict[str, str]]:
    """返回 3 条建议；每条都必须命中固定清单。"""

    picked: list[str] = []
    if llm is not None:
        try:
            raw = llm(utterance, tuple(CAPABILITIES))
        except Exception:  # 模型异常一律走规则兜底，不影响主流程
            raw = None
        if isinstance(raw, (list, tuple)):
            for item in raw:
                if isinstance(item, str) and item in CAPABILITIES and item not in picked:
                    picked.append(item)
    if len(picked) < 3:
        picked.extend(item for item in _DEFAULT if item not in picked)
    return [{"label": item, "text": item} for item in picked[:3]]

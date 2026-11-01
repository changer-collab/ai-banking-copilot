"""呈现层：把执行参数翻译成人话。

分工红线：
- **字段白名单、中文标签、金额格式化全部确定性完成**——LLM 无权决定「哪些键能出卡」，
  否则它可能把 `budget_hold_id` 原样念出来（泄漏），或把 9900 分说成 990 元（违反事实零生成）。
- LLM 只能在这一层之后做措辞美化，且失败即退回模板。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

_INTERNAL_KEYS = frozenset({
    "user_id", "occasion_id", "budget_hold_id", "hold_id", "account_id", "card_id",
    "product_id", "payee_id", "payer_id", "subscription_id", "schedule_id",
    "grant", "span", "idempotency_key",
})
"""内部键：参与执行与幂等，但**永不**作为呈现内容。"""


@dataclass(frozen=True)
class FactLine:
    """一条可展示事实。`value` 只能是已计算好的字符串，不在模板里做算术。"""

    label: str
    value: str
    emphasis: bool = False


def drop_internal(params: Mapping[str, object]) -> dict[str, object]:
    """剔除内部键；不做其它加工。"""
    return {key: value for key, value in params.items() if key not in _INTERNAL_KEYS}


def yuan(cents: object) -> str:
    """分 → 元，两位小数。非整数返回空串（宁可不显示也不猜）。"""
    if not isinstance(cents, int) or isinstance(cents, bool):
        return ""
    return f"{cents / 100:.2f}"


_FIELD_SPECS: Mapping[str, tuple[tuple[str, str], ...]] = {
    "merchant.order": (
        ("merchant", "商户"), ("amount_cents", "下单金额（元）"),
        ("order_on", "送达日期"),
    ),
    "funds.freeze": (
        ("amount_cents", "冻结金额（元）"), ("reason", "用途"),
    ),
}
"""动作 → 可展示字段白名单。

白名单只列**可展示键**；内部键（见 `_INTERNAL_KEYS`）即便登记也会被 `fact_lines()`
剔除，因此登记内部键等于登记死项（不会输出，只会误导维护者）。
"""


def fact_lines(action: str, params: Mapping[str, object]) -> tuple[FactLine, ...]:
    """按白名单输出事实行；未登记的 action 返回空元组（调用方保留原有渲染）。"""
    specs = _FIELD_SPECS.get(action)
    if specs is None:
        return ()
    lines: list[FactLine] = []
    for key, label in specs:
        if key in _INTERNAL_KEYS:
            continue
        raw = params.get(key)
        if raw is None:
            continue
        if key.endswith("_cents") or (key.endswith("_id") and key != "occasion_id"):
            value = yuan(raw)
        else:
            value = str(raw)
        if not value:
            continue
        lines.append(FactLine(label=label, value=value, emphasis=key == "amount_cents"))
    return tuple(lines)

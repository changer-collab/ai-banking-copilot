"""确定性算子白名单：一切派生数字的唯一出口（H1）。

设计约束（PRD §5.1 ①）：
- 白名单**封闭**：不在 `OPERATOR_WHITELIST` 里的名字一律拒绝——没有"通用表达式求值"。
- **出处强制**：每个输入必须携带 `record_id`；输出把全部出处带到 `sources`。
- **拒绝不可判**：分母为 0 时拒绝计算，而不是返回 inf/nan。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from ai_banking_copilot.gateway.errors import OperatorError, UnknownOperatorError


@dataclass(frozen=True)
class OperatorSpec:
    name: str
    arity: str
    description: str


OPERATOR_WHITELIST: tuple[OperatorSpec, ...] = (
    OperatorSpec(name="sum", arity="n>=1", description="求和（输入须为已校验记录）"),
    OperatorSpec(name="avg", arity="n>=1", description="均值（输入须为已校验记录）"),
    OperatorSpec(name="ratio", arity="2", description="占比 = a / b"),
    OperatorSpec(name="yoy", arity="2", description="同比 = (本期 - 同期) / 同期"),
    OperatorSpec(name="mom", arity="2", description="环比 = (本期 - 上期) / 上期"),
)

OPERATOR_NAMES: tuple[str, ...] = tuple(spec.name for spec in OPERATOR_WHITELIST)

_TWO_INPUT_OPERATORS = frozenset({"ratio", "yoy", "mom"})


@dataclass(frozen=True)
class DerivedValue:
    """派生值：值 + 完整出处链。"""

    operator: str
    value: Decimal
    sources: tuple[str, ...]


def run_operator(name: str, inputs: Sequence[tuple[str, Decimal]]) -> DerivedValue:
    """执行白名单算子。`inputs` 为 `(record_id, 值)` 序列。"""
    if name not in OPERATOR_NAMES:
        raise UnknownOperatorError(f"算子 {name!r} 不在白名单内：{OPERATOR_NAMES}")
    if not inputs:
        raise OperatorError("算子至少需要一个输入")
    for record_id, _ in inputs:
        if not record_id:
            raise OperatorError("输入缺少出处 record_id")
    values = [value for _, value in inputs]
    sources = tuple(record_id for record_id, _ in inputs)

    if name == "sum":
        return DerivedValue(operator=name, value=sum(values, Decimal(0)), sources=sources)
    if name == "avg":
        return DerivedValue(
            operator=name, value=sum(values, Decimal(0)) / Decimal(len(values)), sources=sources
        )

    if len(values) != 2:
        raise OperatorError(f"{name} 需要恰好 2 个输入，收到 {len(values)} 个")
    current, base = values
    if base == 0:
        raise OperatorError(f"{name} 分母为 0，拒绝计算")
    if name in _TWO_INPUT_OPERATORS and name != "ratio":
        return DerivedValue(operator=name, value=(current - base) / base, sources=sources)
    return DerivedValue(operator=name, value=current / base, sources=sources)

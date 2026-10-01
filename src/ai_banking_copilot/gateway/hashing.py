"""确定性序列化与摘要：出处校验（provenance）的底层原语。

定则（与 `contracts/plan.cfhash` 同源）：
- `Money` 只序列化 `cents`；`date` 用 ISO；`Decimal` 用字符串（保精度）；
- 键排序 + 紧凑分隔符 → 同一事实在任何语言/进程得到同一摘要。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import fields, is_dataclass
from datetime import date
from decimal import Decimal
from enum import Enum


def canonicalize(value: object) -> object:
    """把领域对象递归转成可确定性序列化的普通结构。"""
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, date):
        return value.isoformat()
    if is_dataclass(value) and not isinstance(value, type):
        return {info.name: canonicalize(getattr(value, info.name)) for info in fields(value)}
    if isinstance(value, list | tuple):
        return [canonicalize(item) for item in value]
    if isinstance(value, dict):
        return {str(key): canonicalize(item) for key, item in value.items()}
    return value


def canonical_json(value: object) -> str:
    """紧凑、键序无关的 JSON。"""
    return json.dumps(
        canonicalize(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()

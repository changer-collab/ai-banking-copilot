"""提示注入的确定性契约（单一真源）。

`INJECTION_MARKERS` 为 web 与 eval 两套旧标记的**并集**（只加严、不放松）：
- web（交互层）原有顺序在前；
- 其后追加 eval 独有标记（`忽略指令` / `无视` / `绕过` / `突破限额`）。

`detect_injection(text)` 为纯函数（禁 I/O）：`lower()` 子串匹配，命中即视为注入，不做模糊判断。
"""

from __future__ import annotations

INJECTION_MARKERS: tuple[str, ...] = (
    "忽略以上",
    "忽略之前",
    "忽略上述",
    "无视以上",
    "忽略系统",
    "ignore previous",
    "ignore above",
    "disregard",
    "新指令",
    "忽略指令",
    "无视",
    "绕过",
    "突破限额",
)
"""提示注入的确定性标记（两套并集：命中即升 `MANUAL` 转人工，不做模糊判断）。"""


def detect_injection(text: str) -> bool:
    """确定性注入检测：命中标记即视为注入（不做启发式猜测）。"""
    lowered = text.lower()
    return any(marker.lower() in lowered for marker in INJECTION_MARKERS)

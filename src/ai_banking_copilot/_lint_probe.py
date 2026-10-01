"""质量门的探针模块：用于验证 ruff / mypy / pytest 三件套已接入 CI。"""

from __future__ import annotations

import re

_WHITESPACE_RUN = re.compile(r"\s+")


def normalize_whitespace(text: str) -> str:
    """把连续空白折叠为单个空格，并去掉首尾空白。"""
    return _WHITESPACE_RUN.sub(" ", text).strip()

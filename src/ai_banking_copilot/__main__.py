"""应用入口（组合根）：装配默认依赖并启动 HTTP 服务。

写面与意图通道装配下沉到 `ai_banking_copilot.compose`（可单测）；本文件只负责起服务。
"""

from __future__ import annotations

from pathlib import Path

import uvicorn

from ai_banking_copilot.compose import build_deps
from ai_banking_copilot.web.app import create_app


def main() -> None:
    """默认装配并启动 HTTP 服务。"""
    deps = build_deps(Path("runtime/copilot.db"))
    uvicorn.run(create_app(deps), host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()

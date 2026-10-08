"""应用入口（组合根）：装配默认依赖并启动 HTTP 服务。

写面与意图通道装配下沉到 `ai_banking_copilot.compose`（可单测）；本文件只负责起服务。
"""

from __future__ import annotations

import os
from pathlib import Path

import uvicorn

from ai_banking_copilot.compose import build_deps
from ai_banking_copilot.web.app import create_app

_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


def load_env_file(path: Path = _ENV_FILE) -> None:
    """加载 `.env`（标准库实现，不引入第三方依赖）：已存在的环境变量优先，不覆盖。"""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip().strip("\"'")


def main() -> None:
    """默认装配并启动 HTTP 服务。"""
    load_env_file()
    deps = build_deps(Path("runtime/copilot.db"))
    uvicorn.run(create_app(deps), host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()

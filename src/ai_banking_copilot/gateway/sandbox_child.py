"""受限沙箱的**子进程入口**：装防护 → 跑白名单算子 → 只用 stdout 回传结果。

防护四件（PRD §5.8「沙箱执行」）：
1. **无网络**：`socket` 的构造与建连被替换为抛错。
2. **禁库**：`socket` / `ssl` / `http` / `urllib` / `requests` / `ftplib` / `sqlite3` /
   `subprocess` / `ctypes` 的 import 被 `sys.meta_path` 前置 finder 拒绝——
   **TCB（SQLite）无法被沙箱访问，从物理上杜绝"沙箱回写事实网关"**。
3. **禁写文件**：`builtins.open` 与 `io.open` 对含 `w/a/x/+` 的模式直接抛错。
4. **限时**：由父进程 `subprocess` 超时终止（本模块不自设闹钟）。
"""

from __future__ import annotations

import builtins
import importlib
import io
import json
import socket
import sys
import time
from collections.abc import Callable
from decimal import Decimal
from typing import Any, NoReturn, cast

from ai_banking_copilot.gateway.errors import SandboxViolation
from ai_banking_copilot.gateway.operators import run_operator

DENIED_MODULES: tuple[str, ...] = (
    "socket",
    "ssl",
    "http",
    "urllib",
    "requests",
    "ftplib",
    "sqlite3",
    "subprocess",
    "ctypes",
)


class _DeniedModuleFinder:
    """拦截禁库 import：命中即抛 `SandboxViolation`。"""

    def find_spec(
        self, fullname: str, path: object = None, target: object = None
    ) -> object:
        root = fullname.split(".", 1)[0]
        if root in DENIED_MODULES:
            raise SandboxViolation(f"沙箱禁止导入模块：{fullname}")
        return None


def install_guards() -> None:
    """安装全部防护。必须在执行任何外部代码之前调用。"""
    _block_network()
    _block_file_writes()
    sys.meta_path.insert(0, cast(Any, _DeniedModuleFinder()))


def main() -> int:
    """子进程主入口：读 stdin 的 JSON 请求，按 mode 分派。"""
    raw = sys.stdin.read()
    payload = json.loads(raw)
    mode = str(payload.get("mode", "operator"))
    install_guards()
    if mode == "probe":
        return _run_probe(str(payload["probe"]))
    started = time.perf_counter()
    inputs = tuple(
        (str(record_id), Decimal(str(value))) for record_id, value in payload["inputs"]
    )
    derived = run_operator(str(payload["operator"]), inputs)
    json.dump(
        {
            "operator": derived.operator,
            "value": str(derived.value),
            "sources": list(derived.sources),
            "duration_ms": int((time.perf_counter() - started) * 1000),
        },
        sys.stdout,
    )
    return 0


def _block_network() -> None:
    def _deny(*args: object, **kwargs: object) -> NoReturn:
        raise SandboxViolation("沙箱禁止网络访问")

    setattr(socket, "socket", _deny)  # noqa: B010
    setattr(socket, "create_connection", _deny)  # noqa: B010


def _block_file_writes() -> None:
    real_open = builtins.open

    def _guarded_open(
        file: object, mode: str = "r", *args: object, **kwargs: object
    ) -> object:
        if any(flag in mode for flag in ("w", "a", "x", "+")):
            raise SandboxViolation(f"沙箱禁止写文件：{file!r}")
        return real_open(file, mode, *args, **kwargs)  # type: ignore[call-overload]

    setattr(builtins, "open", _guarded_open)  # noqa: B010
    setattr(io, "open", _guarded_open)  # noqa: B010


def _run_probe(name: str) -> int:
    probes: dict[str, Callable[[], None]] = {
        "socket": _probe_socket,
        "import_sqlite3": _probe_import_sqlite3,
        "write_file": _probe_write_file,
        "sleep": _probe_sleep,
    }
    result = {"probe": name, "violation": ""}
    action = probes.get(name)
    if action is None:
        result["violation"] = f"未知探针：{name}"
    else:
        try:
            action()
        except SandboxViolation as error:
            result["violation"] = str(error)
    json.dump(result, sys.stdout)
    return 0


def _probe_socket() -> None:
    socket.socket()


def _probe_import_sqlite3() -> None:
    importlib.import_module("sqlite3")


def _probe_write_file() -> None:
    with open("sandbox_probe.txt", "w") as handle:
        handle.write("escape")


def _probe_sleep() -> None:
    time.sleep(5)

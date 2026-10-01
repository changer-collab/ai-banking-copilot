"""沙箱父进程侧：启动受限子进程、限时、回收结果。

**关键设计**：子进程通过 `sys.path` 注入包所在目录，因此
无论 `ai_banking_copilot` 是 pip 安装还是仅由 pytest 的 `pythonpath=src` 提供，
沙箱都能 import 到同一份代码（可复现）。
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

import ai_banking_copilot
from ai_banking_copilot.gateway.errors import SandboxError, SandboxTimeout, SandboxViolation

_BOOTSTRAP = (
    "import sys; sys.path.insert(0, {root!r});"
    " from ai_banking_copilot.gateway import sandbox_child;"
    " raise SystemExit(sandbox_child.main())"
)


@dataclass(frozen=True)
class SandboxRequest:
    operator: str
    inputs: Sequence[tuple[str, Decimal]]
    timeout_s: float = 2.0

    def payload(self) -> str:
        return json.dumps(
            {
                "mode": "operator",
                "operator": self.operator,
                "inputs": [[record_id, str(value)] for record_id, value in self.inputs],
            },
            ensure_ascii=False,
        )


@dataclass(frozen=True)
class SandboxResult:
    operator: str
    value: Decimal
    duration_ms: int
    sources: tuple[str, ...] = field(default_factory=tuple)


def run_in_sandbox(request: SandboxRequest) -> SandboxResult:
    """在受限子进程里执行白名单算子。"""
    completed = _spawn(request.payload(), timeout_s=request.timeout_s)
    if completed.returncode != 0:
        raise SandboxViolation(
            f"沙箱子进程失败（exit={completed.returncode}）：{completed.stderr.strip()}"
        )
    data = json.loads(completed.stdout)
    return SandboxResult(
        operator=str(data["operator"]),
        value=Decimal(str(data["value"])),
        duration_ms=int(data["duration_ms"]),
        sources=tuple(str(item) for item in data["sources"]),
    )


def run_probe(probe: str, timeout_s: float = 2.0) -> str:
    """自检探针：返回违规说明（空字符串代表**未被拦住**，属失败）。"""
    payload = json.dumps({"mode": "probe", "probe": probe})
    completed = _spawn(payload, timeout_s=timeout_s)
    if completed.returncode != 0:
        raise SandboxError(f"探针 {probe} 异常退出：{completed.stderr.strip()}")
    data = json.loads(completed.stdout)
    return str(data["violation"])


def _spawn(payload: str, *, timeout_s: float) -> subprocess.CompletedProcess[str]:
    root = str(Path(ai_banking_copilot.__file__).resolve().parents[1])
    try:
        return subprocess.run(
            [sys.executable, "-X", "utf8", "-I", "-c", _BOOTSTRAP.format(root=root)],
            input=payload,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise SandboxTimeout(f"沙箱执行超过 {timeout_s}s，已终止") from error

"""受限沙箱子进程入口。

算子模式保持既有行为；分析模式只接受一个经过 AST 白名单校验的表达式，禁止
导入、属性、赋值、文件/网络 API 和任意 Python 语句。事实快照在子进程中重新
校验，不能借由调用方对象引用写回主进程。
"""

from __future__ import annotations

import ast
import builtins
import importlib
import io
import json
import socket
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, NoReturn, cast

from ai_banking_copilot.gateway.errors import SandboxViolation
from ai_banking_copilot.gateway.operators import DerivedValue, run_operator

try:
    import resource
except ImportError:  # Windows 没有 resource；父进程 watchdog 仍会限时。
    resource = None  # type: ignore[assignment]

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
_ALLOWED_CALLS = frozenset({"sum", "avg", "ratio", "yoy", "mom"})
_MAX_PAYLOAD_BYTES = 262_144


class SandboxResourceLimit(SandboxViolation):
    """子进程内部标记预算拒绝，父进程将其转换为稳定的公开异常类型。"""


class _DeniedModuleFinder:
    def find_spec(self, fullname: str, path: object = None, target: object = None) -> object:
        root = fullname.split(".", 1)[0]
        if root in DENIED_MODULES:
            raise SandboxViolation(f"沙箱禁止导入模块：{fullname}")
        return None


def install_guards() -> None:
    """安装网络、文件写入和禁库防护。"""
    _block_network()
    _block_file_writes()
    sys.meta_path.insert(0, cast(Any, _DeniedModuleFinder()))


def main() -> int:
    """读取 JSON 请求并执行，预期错误只写 stderr、以非零码结束。"""
    try:
        raw_payload = sys.stdin.buffer.read(_MAX_PAYLOAD_BYTES + 1)
        if len(raw_payload) > _MAX_PAYLOAD_BYTES:
            raise SandboxResourceLimit("沙箱请求超过子进程 payload 字节上限")
        payload = json.loads(raw_payload.decode("utf-8"))
        limits = _limits(payload.get("limits", {}))
        if len(raw_payload) > int(limits["max_payload_bytes"]):
            raise SandboxResourceLimit("沙箱请求超过 payload 字节预算")
        install_guards()
        mode = str(payload.get("mode", "operator"))
        started = time.perf_counter()
        if mode == "probe":
            return _run_probe(str(payload["probe"]))
        if mode == "analysis":
            derived = _run_analysis(
                str(payload["code"]), payload.get("snapshot"), limits
            )
        elif mode == "operator":
            derived = _run_operator(payload, limits)
        else:
            raise SandboxViolation(f"不支持的沙箱模式：{mode}")
        result = json.dumps(
            {
                "operator": derived.operator,
                "value": str(derived.value),
                "sources": list(derived.sources),
                "duration_ms": int((time.perf_counter() - started) * 1000),
            },
        )
        if len(result.encode("utf-8")) > int(limits["max_output_bytes"]):
            raise SandboxResourceLimit("沙箱返回超过输出预算")
        sys.stdout.write(result)
        return 0
    except Exception as error:  # 子进程边界：错误必须 fail-closed，不输出假结果。
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 2


def _limits(raw: object) -> dict[str, int | float]:
    if not isinstance(raw, Mapping):
        raise SandboxViolation("沙箱资源预算格式错误")
    values: dict[str, int | float] = {
        "timeout_s": float(raw.get("timeout_s", 2.0)),
        "memory_mb": int(raw.get("memory_mb", 64)),
        "cpu_s": int(raw.get("cpu_s", 1)),
        "max_ast_nodes": int(raw.get("max_ast_nodes", 256)),
        "max_inputs": int(raw.get("max_inputs", 1_024)),
        "max_output_bytes": int(raw.get("max_output_bytes", 8_192)),
        "max_payload_bytes": int(raw.get("max_payload_bytes", _MAX_PAYLOAD_BYTES)),
        "max_code_bytes": int(raw.get("max_code_bytes", 8_192)),
        "max_fields": int(raw.get("max_fields", 32)),
        "max_field_bytes": int(raw.get("max_field_bytes", 256)),
        "max_record_id_bytes": int(raw.get("max_record_id_bytes", 512)),
        "max_value_bytes": int(raw.get("max_value_bytes", 128)),
    }
    if any(value <= 0 for value in values.values()):
        raise SandboxViolation("沙箱资源预算必须为正")
    _apply_os_limits(values)
    return values


def _apply_os_limits(limits: Mapping[str, int | float]) -> None:
    """POSIX 下设置硬资源上限；Windows Job Object 由父进程负责。"""
    if sys.platform == "win32":
        return
    if resource is None or not hasattr(resource, "setrlimit"):
        raise SandboxUnavailable("POSIX rlimit 不可用，拒绝执行沙箱代码")
    cpu_s = int(limits["cpu_s"])
    memory = int(limits["memory_mb"]) * 1024 * 1024
    try:
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_s, cpu_s))
        resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
    except (OSError, ValueError) as error:
        raise SandboxUnavailable("POSIX rlimit 设置失败，拒绝执行沙箱代码") from error


class SandboxUnavailable(SandboxViolation):
    """子进程没有建立硬 OS 资源限制。"""


@dataclass(frozen=True)
class _ExpressionValue:
    value: Decimal
    sources: tuple[str, ...]
    facts: tuple[tuple[str, Decimal], ...] = ()
    operator: str | None = None


def _run_operator(payload: Mapping[str, object], limits: Mapping[str, int | float]) -> DerivedValue:
    raw_inputs = payload.get("inputs")
    if not isinstance(raw_inputs, Sequence) or isinstance(raw_inputs, (str, bytes)):
        raise SandboxViolation("算子输入格式错误")
    if len(raw_inputs) > int(limits["max_inputs"]):
        raise SandboxViolation("算子输入超过资源预算")
    inputs: list[tuple[str, Decimal]] = []
    for item in raw_inputs:
        if not isinstance(item, Sequence) or len(item) != 2:
            raise SandboxViolation("算子输入缺少出处")
        try:
            record_id, raw_value = item[0], item[1]
        except (InvalidOperation, TypeError, ValueError) as error:
            raise SandboxViolation("算子输入金额非法") from error
        if not isinstance(record_id, str) or not record_id:
            raise SandboxViolation("算子输入必须有出处且为有限数")
        if len(record_id) > int(limits["max_record_id_bytes"]):
            raise SandboxResourceLimit("算子 record_id 超过字节预算")
        if len(record_id.encode("utf-8")) > int(limits["max_record_id_bytes"]):
            raise SandboxResourceLimit("算子 record_id 超过字节预算")
        value_text = str(raw_value)
        if len(value_text) > int(limits["max_value_bytes"]):
            raise SandboxResourceLimit("算子数值文本超过字节预算")
        if len(value_text.encode("utf-8")) > int(limits["max_value_bytes"]):
            raise SandboxResourceLimit("算子数值文本超过字节预算")
        try:
            value = Decimal(value_text)
        except (InvalidOperation, TypeError, ValueError) as error:
            raise SandboxViolation("算子输入金额非法") from error
        if not value.is_finite():
            raise SandboxViolation("算子输入必须有出处且为有限数")
        inputs.append((record_id, value))
    return run_operator(str(payload.get("operator")), tuple(inputs))


def _run_analysis(
    code: str, raw_snapshot: object, limits: Mapping[str, int | float]
) -> DerivedValue:
    if len(code) > int(limits["max_code_bytes"]):
        raise SandboxResourceLimit("分析代码超过字节预算")
    if len(code.encode("utf-8")) > int(limits["max_code_bytes"]):
        raise SandboxResourceLimit("分析代码超过字节预算")
    snapshot = _snapshot(raw_snapshot, limits)
    try:
        tree = ast.parse(code, mode="eval")
    except SyntaxError as error:
        raise SandboxViolation(f"分析代码语法非法：{error.msg}") from error
    nodes = tuple(ast.walk(tree))
    if len(nodes) > int(limits["max_ast_nodes"]):
        raise SandboxResourceLimit("分析代码超过 AST 资源预算")
    if not isinstance(tree.body, ast.Call):
        raise SandboxViolation("分析代码必须调用白名单算子")
    value = _evaluate(tree.body, snapshot)
    if value.operator is None:
        raise SandboxViolation("分析代码必须调用白名单算子")
    return DerivedValue(value.operator, value.value, value.sources)


def _snapshot(
    raw: object, limits: Mapping[str, int | float]
) -> dict[str, tuple[tuple[str, Decimal], ...]]:
    if not isinstance(raw, Mapping):
        raise SandboxViolation("事实快照必须是对象")
    if len(raw) > int(limits["max_fields"]):
        raise SandboxResourceLimit("事实快照字段超过预算")
    result: dict[str, tuple[tuple[str, Decimal], ...]] = {}
    count = 0
    for name, records in raw.items():
        if not isinstance(name, str) or not name.isidentifier():
            raise SandboxViolation("事实快照字段名非法")
        if len(name) > int(limits["max_field_bytes"]):
            raise SandboxResourceLimit("事实快照字段名超过字节预算")
        if len(name.encode("utf-8")) > int(limits["max_field_bytes"]):
            raise SandboxResourceLimit("事实快照字段名超过字节预算")
        if not isinstance(records, Sequence) or isinstance(records, (str, bytes)):
            raise SandboxViolation(f"事实快照字段 {name} 不是记录序列")
        parsed: list[tuple[str, Decimal]] = []
        for record in records:
            if isinstance(record, Mapping):
                record_id = record.get("record_id")
                raw_value = record.get("value")
            elif (
                isinstance(record, Sequence)
                and not isinstance(record, (str, bytes))
                and len(record) == 2
            ):
                record_id, raw_value = record
            else:
                raise SandboxViolation("事实快照记录格式错误")
            if not isinstance(record_id, str) or not record_id:
                raise SandboxViolation("事实快照记录缺少有效出处")
            if len(record_id) > int(limits["max_record_id_bytes"]):
                raise SandboxResourceLimit("事实快照 record_id 超过字节预算")
            if len(record_id.encode("utf-8")) > int(limits["max_record_id_bytes"]):
                raise SandboxResourceLimit("事实快照 record_id 超过字节预算")
            value_text = str(raw_value)
            if len(value_text) > int(limits["max_value_bytes"]):
                raise SandboxResourceLimit("事实快照数值文本超过字节预算")
            if len(value_text.encode("utf-8")) > int(limits["max_value_bytes"]):
                raise SandboxResourceLimit("事实快照数值文本超过字节预算")
            try:
                value = Decimal(value_text)
            except (InvalidOperation, TypeError, ValueError) as error:
                raise SandboxViolation("事实快照数值非法") from error
            if not value.is_finite():
                raise SandboxViolation("事实快照记录缺少有效出处")
            parsed.append((record_id, value))
            count += 1
            if count > int(limits["max_inputs"]):
                raise SandboxResourceLimit("事实快照超过输入资源预算")
        result[name] = tuple(parsed)
    return result


def _evaluate(
    node: ast.AST, snapshot: Mapping[str, tuple[tuple[str, Decimal], ...]]
) -> _ExpressionValue:
    if isinstance(node, ast.Name):
        try:
            values = snapshot[node.id]
        except KeyError as error:
            raise SandboxViolation(f"分析代码引用未知事实：{node.id}") from error
        if not values:
            raise SandboxViolation(f"事实 {node.id} 为空")
        return _ExpressionValue(
            value=Decimal(0),
            facts=values,
            sources=tuple(item[0] for item in values),
        )
    if isinstance(node, (ast.List, ast.Tuple)):
        facts: list[tuple[str, Decimal]] = []
        list_sources: list[str] = []
        for item in node.elts:
            value = _evaluate(item, snapshot)
            list_sources.extend(value.sources)
            facts.extend(value.facts)
        if not facts:
            raise SandboxViolation("分析代码列表为空")
        return _ExpressionValue(
            value=Decimal(0), facts=tuple(facts), sources=tuple(list_sources)
        )
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in _ALLOWED_CALLS:
            raise SandboxViolation("分析代码调用不在白名单内")
        if node.keywords:
            raise SandboxViolation("分析代码不支持关键字参数")
        if any(not _is_snapshot_reference(argument) for argument in node.args):
            raise SandboxViolation("分析代码参数必须引用事实快照或白名单算子")
        if not node.args:
            raise SandboxViolation("分析代码没有事实输入")
        arguments = tuple(_evaluate(argument, snapshot) for argument in node.args)
        result_sources = tuple(
            source for argument in arguments for source in argument.sources
        )
        if not result_sources:
            raise SandboxViolation("分析代码没有事实输入")
        if len(result_sources) != len(set(result_sources)):
            raise SandboxViolation("分析代码重复引用同一事实出处")
        if node.func.id in {"sum", "avg"}:
            inputs = [
                fact
                for argument in arguments
                for fact in _as_inputs(argument)
            ]
        else:
            inputs = []
            for index, argument in enumerate(arguments):
                # A two-input operation may receive a fact group directly (for
                # example ``yoy(current, prior)``). Aggregate each group in the
                # sandbox so the result keeps every original record as its
                # provenance instead of inventing a synthetic YTD fact.
                if argument.facts:
                    scalar = run_operator("sum", argument.facts).value
                else:
                    scalar = argument.value
                inputs.append((f"argument:{index}", scalar))
        derived = run_operator(node.func.id, tuple(inputs))
        return _ExpressionValue(
            value=derived.value,
            sources=result_sources,
            operator=derived.operator,
        )
    raise SandboxViolation("分析代码包含不允许的语法")


def _as_inputs(value: _ExpressionValue) -> tuple[tuple[str, Decimal], ...]:
    if value.facts:
        return value.facts
    return ((f"derived:{value.operator}", value.value),)


def _is_snapshot_reference(node: ast.AST) -> bool:
    if isinstance(node, ast.Name):
        return True
    if isinstance(node, (ast.List, ast.Tuple)):
        return bool(node.elts) and all(_is_snapshot_reference(item) for item in node.elts)
    if isinstance(node, ast.Call):
        return (
            isinstance(node.func, ast.Name)
            and node.func.id in _ALLOWED_CALLS
            and bool(node.args)
            and all(_is_snapshot_reference(item) for item in node.args)
            and not node.keywords
        )
    return False


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
        "read_file": _probe_read_file,
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


def _probe_read_file() -> None:
    with open("sandbox_probe.txt") as handle:
        handle.read()


def _probe_sleep() -> None:
    time.sleep(5)

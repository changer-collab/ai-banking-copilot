"""受限分析沙箱的父进程入口。

沙箱支持两条兼容路径：既有的 ``SandboxRequest(operator=...)`` 白名单算子，
以及面向分析代码的受限表达式语言。分析代码只读事实快照，表达式的每个数值
都必须来自快照记录，因此结果会携带完整 ``record_id`` 出处。任意代码不会交给
Python ``eval``；父进程只把 DSL 文本交给独立子进程解析和执行。
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import ai_banking_copilot
from ai_banking_copilot.gateway.errors import (
    OperatorError,
    SandboxError,
    SandboxTimeout,
    SandboxViolation,
)

_BOOTSTRAP = (
    "import sys; sys.path.insert(0, {root!r});"
    " from ai_banking_copilot.gateway import sandbox_child;"
    " raise SystemExit(sandbox_child.main())"
)


class SandboxUnavailable(SandboxError):
    """当前平台无法建立沙箱所需的独立、可限资源进程。"""


class SandboxResourceLimit(SandboxViolation):
    """分析代码超过 AST、输入、输出或内存/CPU预算。"""


@dataclass(frozen=True)
class SandboxLimits:
    """沙箱预算；数值均为父子进程共同校验的硬上限。"""

    timeout_s: float = 2.0
    memory_mb: int = 64
    cpu_s: int = 1
    max_ast_nodes: int = 256
    max_inputs: int = 1_024
    max_output_bytes: int = 8_192
    max_payload_bytes: int = 262_144
    max_code_bytes: int = 8_192
    max_fields: int = 32
    max_field_bytes: int = 256
    max_record_id_bytes: int = 512
    max_value_bytes: int = 128

    def __post_init__(self) -> None:
        if self.timeout_s <= 0 or self.memory_mb <= 0 or self.cpu_s <= 0:
            raise ValueError("沙箱资源预算必须为正")
        if min(
            self.max_ast_nodes,
            self.max_inputs,
            self.max_output_bytes,
            self.max_payload_bytes,
            self.max_code_bytes,
            self.max_fields,
            self.max_field_bytes,
            self.max_record_id_bytes,
            self.max_value_bytes,
        ) <= 0:
            raise ValueError("沙箱资源预算必须为正")


@dataclass(frozen=True)
class SandboxMonitor:
    """可审计的隔离执行摘要。"""

    code_sha256: str | None
    limits: SandboxLimits
    duration_ms: int
    exit_code: int
    status: str
    isolation: str = "process"
    result_operator: str | None = None
    result_value: str | None = None
    result_sources: tuple[str, ...] = ()


SandboxMonitorSink = Callable[[SandboxMonitor], None]
"""接收隔离执行摘要的持久化回调；回调失败时调用方必须拒绝结果。"""


@dataclass(frozen=True)
class SandboxRequest:
    """既有算子请求与新分析代码请求的兼容载体。

    ``operator`` 和 ``inputs`` 保留旧接口；传入 ``code`` + ``snapshot`` 时走受限
    DSL。快照必须是 JSON 可序列化的 ``{name: [{record_id, value}, ...]}`` 映射。
    """

    operator: str | None = None
    inputs: Sequence[tuple[str, Decimal]] = field(default_factory=tuple)
    timeout_s: float = 2.0
    code: str | None = None
    snapshot: Mapping[str, object] | None = None
    limits: SandboxLimits | None = None

    def effective_limits(self) -> SandboxLimits:
        if self.limits is not None:
            return self.limits
        return SandboxLimits(timeout_s=self.timeout_s)

    def payload(self) -> str:
        limits = self.effective_limits()
        if self.code is not None:
            if self.snapshot is None:
                raise SandboxViolation("分析代码缺少事实快照")
            body: dict[str, object] = {
                "mode": "analysis",
                "code": self.code,
                "snapshot": self.snapshot,
            }
        else:
            if self.operator is None:
                raise SandboxViolation("沙箱请求缺少算子或分析代码")
            _validate_operator_body(self.inputs, limits)
            body = {
                "mode": "operator",
                "operator": self.operator,
                "inputs": [[record_id, str(value)] for record_id, value in self.inputs],
            }
        body["limits"] = {
            "timeout_s": limits.timeout_s,
            "memory_mb": limits.memory_mb,
            "cpu_s": limits.cpu_s,
            "max_ast_nodes": limits.max_ast_nodes,
            "max_inputs": limits.max_inputs,
            "max_output_bytes": limits.max_output_bytes,
            "max_payload_bytes": limits.max_payload_bytes,
            "max_code_bytes": limits.max_code_bytes,
            "max_fields": limits.max_fields,
            "max_field_bytes": limits.max_field_bytes,
            "max_record_id_bytes": limits.max_record_id_bytes,
            "max_value_bytes": limits.max_value_bytes,
        }
        if self.code is not None:
            _validate_analysis_body(self.code, self.snapshot, limits)
        _check_estimated_size(body, limits.max_payload_bytes)
        try:
            encoded = json.dumps(body, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as error:
            raise SandboxViolation("事实快照必须是有限 JSON 值") from error
        if len(encoded.encode("utf-8")) > limits.max_payload_bytes:
            raise SandboxResourceLimit("沙箱请求超过 payload 字节预算")
        return encoded


_GENERATED_ANALYSIS_OPERATORS = frozenset({"sum", "avg", "ratio", "yoy", "mom"})


def generate_analysis_code(operator: str, *fact_names: str) -> str:
    """从受信任算子名和事实槽名生成受限 DSL 源码。

    这不是任意 Python 代码生成器：操作符与参数名都在父进程校验，返回值仍须经过
    子进程 AST 白名单和事实出处校验。模型或 HTTP 请求不得直接注入表达式文本。
    """
    if operator not in _GENERATED_ANALYSIS_OPERATORS:
        raise SandboxViolation("分析算子不在白名单内")
    if not fact_names or any(not name.isidentifier() for name in fact_names):
        raise SandboxViolation("分析事实槽名必须是合法标识符")
    return f"{operator}({', '.join(fact_names)})"


@dataclass(frozen=True)
class SandboxResult:
    operator: str
    value: Decimal
    duration_ms: int
    sources: tuple[str, ...] = field(default_factory=tuple)
    monitor: SandboxMonitor | None = None


def run_in_sandbox(
    request: SandboxRequest, *, monitor_sink: SandboxMonitorSink | None = None
) -> SandboxResult:
    """在独立受限子进程执行白名单算子或分析 DSL。"""
    limits = request.effective_limits()
    started = time.perf_counter()
    try:
        completed, elapsed_ms = _spawn(request.payload(), limits=limits)
    except SandboxTimeout as timeout_error:
        monitor = _failed_monitor(request, limits, started, status="timeout")
        _attach_monitor(timeout_error, monitor)
        _emit_monitor(monitor_sink, monitor)
        raise
    except SandboxError as sandbox_error:
        # 审计中区分隔离能力不可用与请求被拒绝；两者都拒绝执行并上送监控摘要。
        status = "unavailable" if isinstance(sandbox_error, SandboxUnavailable) else "rejected"
        monitor = _failed_monitor(request, limits, started, status=status)
        _attach_monitor(sandbox_error, monitor)
        _emit_monitor(monitor_sink, monitor)
        raise
    if completed.returncode != 0:
        stderr = completed.stderr.strip()
        status = "unavailable" if stderr.startswith("SandboxUnavailable:") else "rejected"
        monitor = _monitor(
            request, limits, elapsed_ms, completed.returncode, status=status
        )
        _emit_monitor(monitor_sink, monitor)
        if stderr.startswith("SandboxUnavailable:"):
            failure: Exception = SandboxUnavailable(stderr.partition(":")[2].strip())
        elif stderr.startswith("OperatorError:"):
            failure = OperatorError(stderr.partition(":")[2].strip())
        elif stderr.startswith("SandboxResourceLimit:"):
            failure = SandboxResourceLimit(stderr.partition(":")[2].strip())
        else:
            failure = SandboxViolation(
                f"沙箱子进程失败（exit={completed.returncode}）：{stderr}"
            )
        _attach_monitor(failure, monitor)
        raise failure
    if len(completed.stdout.encode("utf-8", errors="replace")) > limits.max_output_bytes:
        monitor = _monitor(
            request, limits, elapsed_ms, completed.returncode, status="rejected"
        )
        _emit_monitor(monitor_sink, monitor)
        error = SandboxResourceLimit("沙箱返回超过输出预算")
        _attach_monitor(error, monitor)
        raise error
    try:
        data = json.loads(completed.stdout)
        operator = str(data["operator"])
        value = Decimal(str(data["value"]))
        sources = tuple(str(item) for item in data["sources"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as parse_error:
        monitor = _monitor(
            request, limits, elapsed_ms, completed.returncode, status="failed"
        )
        _emit_monitor(monitor_sink, monitor)
        violation = SandboxViolation("沙箱返回不是有效结果")
        _attach_monitor(violation, monitor)
        raise violation from parse_error
    expected_sources = _request_sources(request)
    if not sources or not set(sources) <= expected_sources:
        monitor = _monitor(
            request,
            limits,
            elapsed_ms,
            completed.returncode,
            status="rejected",
        )
        _emit_monitor(monitor_sink, monitor)
        violation = SandboxViolation("沙箱返回包含未在事实快照中的出处")
        _attach_monitor(violation, monitor)
        raise violation
    monitor = _monitor(
        request,
        limits,
        elapsed_ms,
        completed.returncode,
        status="ok",
        result_operator=operator,
        result_value=str(value),
        result_sources=sources,
    )
    _emit_monitor(monitor_sink, monitor)
    return SandboxResult(
        operator=operator,
        value=value,
        duration_ms=int(data.get("duration_ms", elapsed_ms)),
        sources=sources,
        monitor=monitor,
    )


def run_generated_analysis(
    code: str,
    snapshot: Mapping[str, object],
    *,
    limits: SandboxLimits | None = None,
    timeout_s: float = 2.0,
    monitor_sink: SandboxMonitorSink | None = None,
) -> SandboxResult:
    """执行一段受限分析表达式；结果只允许由快照记录与白名单算子产生。"""
    return run_in_sandbox(
        SandboxRequest(
            code=code,
            snapshot=snapshot,
            timeout_s=timeout_s,
            limits=limits,
        ),
        monitor_sink=monitor_sink,
    )


def _monitor(
    request: SandboxRequest,
    limits: SandboxLimits,
    duration_ms: int,
    exit_code: int,
    *,
    status: str,
    result_operator: str | None = None,
    result_value: str | None = None,
    result_sources: tuple[str, ...] = (),
) -> SandboxMonitor:
    return SandboxMonitor(
        code_sha256=(
            hashlib.sha256(request.code.encode("utf-8")).hexdigest()
            if request.code is not None
            else None
        ),
        limits=limits,
        duration_ms=duration_ms,
        exit_code=exit_code,
        status=status,
        result_operator=result_operator,
        result_value=result_value,
        result_sources=result_sources,
    )


def _failed_monitor(
    request: SandboxRequest,
    limits: SandboxLimits,
    started: float,
    *,
    status: str,
) -> SandboxMonitor:
    return _monitor(
        request,
        limits,
        int((time.perf_counter() - started) * 1000),
        -1,
        status=status,
    )


def _attach_monitor(error: Exception, monitor: SandboxMonitor) -> None:
    payload = asdict(monitor)
    payload["result_sources"] = list(monitor.result_sources)
    error.monitor = payload  # type: ignore[attr-defined]


def _emit_monitor(
    sink: SandboxMonitorSink | None, monitor: SandboxMonitor
) -> None:
    if sink is not None:
        sink(monitor)


run_analysis_in_sandbox = run_generated_analysis


def _validate_analysis_body(
    code: str, snapshot: Mapping[str, object] | None, limits: SandboxLimits
) -> None:
    if len(code) > limits.max_code_bytes or len(code.encode("utf-8")) > limits.max_code_bytes:
        raise SandboxResourceLimit("分析代码超过字节预算")
    if snapshot is None:
        raise SandboxViolation("分析代码缺少事实快照")
    if len(snapshot) > limits.max_fields:
        raise SandboxResourceLimit("事实快照字段超过预算")
    count = 0
    for name, records in snapshot.items():
        if (
            not isinstance(name, str)
            or not name.isidentifier()
            or len(name) > limits.max_field_bytes
            or len(name.encode("utf-8")) > limits.max_field_bytes
        ):
            raise SandboxResourceLimit("事实快照字段名超过预算或格式非法")
        if not isinstance(records, Sequence) or isinstance(records, (str, bytes)):
            raise SandboxViolation(f"事实快照字段 {name} 不是记录序列")
        for record in records:
            if isinstance(record, Mapping):
                if set(record) != {"record_id", "value"}:
                    raise SandboxViolation("事实快照记录字段不匹配")
                record_id, value = record["record_id"], record["value"]
            elif (
                isinstance(record, Sequence)
                and not isinstance(record, (str, bytes))
                and len(record) == 2
            ):
                record_id, value = record
            else:
                raise SandboxViolation("事实快照记录格式错误")
            if not isinstance(record_id, str) or not record_id:
                raise SandboxViolation("事实快照记录缺少出处")
            if len(record_id) > limits.max_record_id_bytes:
                raise SandboxResourceLimit("事实快照 record_id 超过字节预算")
            if len(record_id.encode("utf-8")) > limits.max_record_id_bytes:
                raise SandboxResourceLimit("事实快照 record_id 超过字节预算")
            if isinstance(value, bool) or not isinstance(value, (str, int)):
                raise SandboxViolation("事实快照数值必须是字符串或整数")
            value_text = str(value)
            if len(value_text) > limits.max_value_bytes:
                raise SandboxResourceLimit("事实快照数值文本超过字节预算")
            if len(value_text.encode("utf-8")) > limits.max_value_bytes:
                raise SandboxResourceLimit("事实快照数值文本超过字节预算")
            count += 1
            if count > limits.max_inputs:
                raise SandboxResourceLimit("事实快照超过输入资源预算")


def _validate_operator_body(
    inputs: Sequence[tuple[str, Decimal]], limits: SandboxLimits
) -> None:
    if len(inputs) > limits.max_inputs:
        raise SandboxResourceLimit("算子输入超过资源预算")
    for item in inputs:
        if (
            not isinstance(item, Sequence)
            or isinstance(item, (str, bytes))
            or len(item) != 2
        ):
            raise SandboxViolation("算子输入格式错误")
        record_id, value = item
        if not isinstance(record_id, str) or not record_id:
            raise SandboxViolation("算子输入必须有有效出处")
        if len(record_id) > limits.max_record_id_bytes:
            raise SandboxResourceLimit("算子 record_id 超过字节预算")
        if len(record_id.encode("utf-8")) > limits.max_record_id_bytes:
            raise SandboxResourceLimit("算子 record_id 超过字节预算")
        value_text = str(value)
        if len(value_text) > limits.max_value_bytes:
            raise SandboxResourceLimit("算子数值文本超过字节预算")
        if len(value_text.encode("utf-8")) > limits.max_value_bytes:
            raise SandboxResourceLimit("算子数值文本超过字节预算")


def _check_estimated_size(body: Mapping[str, object], max_bytes: int) -> None:
    """有界估算 payload 大小，避免先构造任意大的 JSON 字符串。"""
    size = 128
    for key, value in body.items():
        size += len(key) + 8
        if key == "snapshot" and isinstance(value, Mapping):
            for name, records in value.items():
                size += len(str(name)) + 8
                if isinstance(records, Sequence):
                    for record in records:
                        if isinstance(record, Mapping):
                            size += len(str(record.get("record_id", "")))
                            size += len(str(record.get("value", ""))) + 48
                        elif isinstance(record, Sequence) and not isinstance(record, (str, bytes)):
                            size += sum(len(str(item)) for item in record) + 48
                        if size > max_bytes:
                            raise SandboxResourceLimit("沙箱请求超过 payload 字节预算")
        elif key == "inputs" and isinstance(value, Sequence):
            for item in value:
                if isinstance(item, Sequence):
                    size += sum(len(str(part)) for part in item) + 16
                if size > max_bytes:
                    raise SandboxResourceLimit("沙箱请求超过 payload 字节预算")
        elif isinstance(value, str):
            size += len(value.encode("utf-8")) + 8
        if size > max_bytes:
            raise SandboxResourceLimit("沙箱请求超过 payload 字节预算")


def run_probe(probe: str, timeout_s: float = 2.0) -> str:
    """自检探针：返回违规说明（空字符串代表未被拦住，属失败）。"""
    payload = json.dumps(
        {
            "mode": "probe",
            "probe": probe,
            "limits": {"timeout_s": timeout_s, "memory_mb": 64, "cpu_s": 1},
        }
    )
    completed, _ = _spawn(payload, limits=SandboxLimits(timeout_s=timeout_s))
    if completed.returncode != 0:
        raise SandboxError(f"探针 {probe} 异常退出：{completed.stderr.strip()}")
    try:
        data = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise SandboxError(f"探针 {probe} 返回无效结果") from error
    return str(data["violation"])


def os_isolation_available() -> bool:
    """报告本机是否有可用的 OS 进程隔离入口。"""
    if os.environ.get("AIB_SANDBOX_DISABLE_ISOLATION") == "1":
        return False
    if os.name == "posix":
        try:
            import resource

            return all(
                hasattr(resource, name)
                for name in ("setrlimit", "RLIMIT_CPU", "RLIMIT_AS")
            )
        except ImportError:
            return False
    if os.name == "nt":
        try:
            import ctypes

            return hasattr(ctypes, "WinDLL") and hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP")
        except ImportError:
            return False
    return False


def _request_sources(request: SandboxRequest) -> set[str]:
    if request.code is None:
        return {record_id for record_id, _ in request.inputs}
    snapshot = request.snapshot or {}
    sources: set[str] = set()
    for records in snapshot.values():
        if not isinstance(records, Sequence) or isinstance(records, (str, bytes)):
            continue
        for record in records:
            if isinstance(record, Mapping):
                record_id = record.get("record_id")
            elif (
                isinstance(record, Sequence)
                and not isinstance(record, (str, bytes))
                and len(record) == 2
            ):
                record_id = record[0]
            else:
                continue
            if isinstance(record_id, str) and record_id:
                sources.add(record_id)
    return sources


def _spawn(payload: str, *, limits: SandboxLimits) -> tuple[subprocess.CompletedProcess[str], int]:
    if not os_isolation_available():
        raise SandboxUnavailable("当前平台没有可用的 OS 进程隔离能力，拒绝执行沙箱代码")
    root = str(Path(ai_banking_copilot.__file__).resolve().parents[1])
    command = [sys.executable, "-X", "utf8", "-I", "-c", _BOOTSTRAP.format(root=root)]
    started = time.perf_counter()
    try:
        if os.name == "nt":
            completed = _spawn_windows(command, payload, limits)
        else:
            completed = subprocess.run(
                command,
                input=payload,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=limits.timeout_s,
                check=False,
                start_new_session=True,
            )
    except subprocess.TimeoutExpired as error:
        raise SandboxTimeout(f"沙箱执行超过 {limits.timeout_s}s，已终止") from error
    except OSError as error:
        raise SandboxUnavailable("创建或限制沙箱子进程失败，拒绝执行") from error
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    return completed, elapsed_ms


def _spawn_windows(
    command: Sequence[str], payload: str, limits: SandboxLimits
) -> subprocess.CompletedProcess[str]:
    """Windows Job Object 给子进程设置 CPU、内存和进程关闭限制。"""
    import ctypes
    from ctypes import wintypes

    class BasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
        )]

    class ExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BasicLimitInformation),
            ("IoInfo", IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    # `WinDLL` is only exposed by ctypes on Windows; use getattr so the
    # module still type-checks on the Linux CI runner while this branch is
    # unreachable there.
    win_dll = cast(Any, ctypes).WinDLL
    kernel32 = win_dll("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD
    ]
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        raise SandboxUnavailable("Windows Job Object 创建失败，拒绝执行沙箱代码")
    process: subprocess.Popen[str] | None = None
    try:
        info = ExtendedLimitInformation()
        info.BasicLimitInformation.LimitFlags = 0x0002 | 0x0100 | 0x2000
        info.BasicLimitInformation.PerProcessUserTimeLimit = limits.cpu_s * 10_000_000
        info.ProcessMemoryLimit = limits.memory_mb * 1024 * 1024
        configured = kernel32.SetInformationJobObject(
            job, 9, ctypes.byref(info), ctypes.sizeof(info)
        )
        if not configured:
            raise SandboxUnavailable("Windows Job Object 资源限制配置失败，拒绝执行")
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=cast(Any, subprocess).CREATE_NEW_PROCESS_GROUP,
        )
        if not kernel32.AssignProcessToJobObject(job, process._handle):  # type: ignore[attr-defined]
            process.kill()
            process.communicate()
            raise SandboxUnavailable("Windows Job Object 无法绑定沙箱子进程，拒绝执行")
        try:
            stdout, stderr = process.communicate(input=payload, timeout=limits.timeout_s)
        except subprocess.TimeoutExpired as timeout_error:
            if not kernel32.TerminateJobObject(job, 1):
                process.kill()
            try:
                process.communicate(timeout=1.0)
            except subprocess.TimeoutExpired:
                process.kill()
            raise timeout_error
        return subprocess.CompletedProcess(command, process.returncode or 0, stdout, stderr)
    finally:
        kernel32.CloseHandle(job)

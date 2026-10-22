import ctypes
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from decimal import Decimal
from types import SimpleNamespace

import pytest

from ai_banking_copilot.gateway import sandbox as sandbox_module
from ai_banking_copilot.gateway.errors import OperatorError, SandboxTimeout, SandboxViolation
from ai_banking_copilot.gateway.operators import run_operator
from ai_banking_copilot.gateway.sandbox import (
    SandboxLimits,
    SandboxRequest,
    SandboxResourceLimit,
    SandboxUnavailable,
    run_generated_analysis,
    run_in_sandbox,
    run_probe,
)


def _assert_failure_monitor(
    error: Exception,
    *,
    code: str,
    status: str,
    exit_code: int,
    duration_ms: int | None,
) -> None:
    monitor = getattr(error, "monitor", None)
    assert isinstance(monitor, dict)
    assert json.loads(json.dumps(monitor)) == monitor
    assert monitor["code_sha256"] == hashlib.sha256(code.encode("utf-8")).hexdigest()
    assert monitor["limits"]["timeout_s"] > 0
    assert monitor["limits"]["memory_mb"] == 64
    assert isinstance(monitor["duration_ms"], int)
    assert monitor["duration_ms"] >= 0
    if duration_ms is not None:
        assert monitor["duration_ms"] == duration_ms
    assert monitor["exit_code"] == exit_code
    assert monitor["status"] == status
    assert "code" not in monitor


def test_sandbox_result_matches_in_process_operator() -> None:
    inputs = (("transactions:t1", Decimal("-3200")), ("transactions:t2", Decimal("-1580")))
    expected = run_operator("sum", inputs)
    result = run_in_sandbox(SandboxRequest(operator="sum", inputs=inputs))
    assert result.operator == "sum"
    assert result.value == expected.value
    assert result.sources == expected.sources


def test_sandbox_rejects_operator_outside_whitelist() -> None:
    with pytest.raises(SandboxViolation):
        run_in_sandbox(SandboxRequest(operator="exec_shell", inputs=(("a:1", Decimal("1")),)))


def test_sandbox_blocks_network_access() -> None:
    assert "网络" in run_probe("socket")


def test_sandbox_blocks_sqlite3_import() -> None:
    assert "禁止导入" in run_probe("import_sqlite3")


def test_sandbox_blocks_file_write() -> None:
    assert "禁止写文件" in run_probe("write_file")


def test_sandbox_enforces_timeout() -> None:
    with pytest.raises(SandboxTimeout):
        run_probe("sleep", timeout_s=0.5)


def test_generated_analysis_uses_snapshot_and_returns_monitor() -> None:
    result = run_generated_analysis(
        "ratio(current, prior)",
        {
            "current": [{"record_id": "bill:current", "value": "730"}],
            "prior": [{"record_id": "bill:prior", "value": "1000"}],
        },
    )
    assert result.operator == "ratio"
    assert result.value == Decimal("0.73")
    assert result.sources == ("bill:current", "bill:prior")
    assert result.monitor is not None
    assert result.monitor.code_sha256
    assert result.monitor.exit_code == 0
    assert result.monitor.status == "ok"
    assert result.monitor.limits.memory_mb == 64


@pytest.mark.parametrize(
    "code",
    [
        "open('escape.txt', 'w')",
        "__import__('socket')",
        "import os",
        "sum(facts); open('x', 'w')",
        "sum(",
        "facts",
        "[facts]",
        "(facts,)",
    ],
)
def test_generated_analysis_rejects_io_imports_and_invalid_syntax(code: str) -> None:
    with pytest.raises(SandboxViolation):
        run_generated_analysis(
            code,
            {"facts": [{"record_id": "bill:one", "value": "1"}]},
        )


def test_generated_analysis_rejects_repeated_fact_references() -> None:
    with pytest.raises(SandboxViolation, match="重复引用"):
        run_generated_analysis(
            "sum((facts, facts))",
            {"facts": [{"record_id": "bill:one", "value": "1"}]},
        )


def test_generated_analysis_nested_operators_preserve_original_sources() -> None:
    result = run_generated_analysis(
        "ratio(sum(facts), prior)",
        {
            "facts": [
                {"record_id": "bill:one", "value": "3"},
                {"record_id": "bill:two", "value": "7"},
            ],
            "prior": [{"record_id": "bill:prior", "value": "20"}],
        },
    )
    assert result.value == Decimal("0.5")
    assert result.sources == ("bill:one", "bill:two", "bill:prior")


def test_generated_analysis_two_input_groups_aggregate_without_synthetic_sources() -> None:
    result = run_generated_analysis(
        "yoy(current, prior)",
        {
            "current": [
                {"record_id": "bill:current-1", "value": "60"},
                {"record_id": "bill:current-2", "value": "40"},
            ],
            "prior": [
                {"record_id": "bill:prior-1", "value": "50"},
                {"record_id": "bill:prior-2", "value": "50"},
            ],
        },
    )
    assert result.value == Decimal("0")
    assert result.sources == (
        "bill:current-1",
        "bill:current-2",
        "bill:prior-1",
        "bill:prior-2",
    )


def test_generated_analysis_rejects_unbounded_ast() -> None:
    with pytest.raises(SandboxResourceLimit):
        run_generated_analysis(
            "sum(facts)",
            {"facts": [{"record_id": "bill:one", "value": "1"}]},
            limits=SandboxLimits(max_ast_nodes=2),
        )


def test_generated_analysis_rejects_snapshot_input_over_budget() -> None:
    with pytest.raises(SandboxResourceLimit, match="输入资源预算"):
        run_generated_analysis(
            "sum(facts)",
            {"facts": [
                {"record_id": "bill:one", "value": "1"},
                {"record_id": "bill:two", "value": "2"},
            ]},
            limits=SandboxLimits(max_inputs=1),
        )


def test_generated_analysis_rejects_code_byte_budget_before_spawn() -> None:
    with pytest.raises(SandboxResourceLimit, match="代码超过字节预算"):
        run_generated_analysis(
            "sum(facts)",
            {"facts": [{"record_id": "bill:one", "value": "1"}]},
            limits=SandboxLimits(max_code_bytes=4),
        )


def test_generated_analysis_rejects_record_id_byte_budget_before_spawn() -> None:
    with pytest.raises(SandboxResourceLimit, match="record_id 超过字节预算"):
        run_generated_analysis(
            "sum(facts)",
            {"facts": [{"record_id": "bill:too-long", "value": "1"}]},
            limits=SandboxLimits(max_record_id_bytes=8),
        )


def test_generated_analysis_rejects_total_payload_budget_before_spawn() -> None:
    with pytest.raises(SandboxResourceLimit, match="payload 字节预算"):
        run_generated_analysis(
            "sum(facts)",
            {"facts": [{"record_id": "bill:one", "value": "1"}]},
            limits=SandboxLimits(max_payload_bytes=128),
        )


@pytest.mark.parametrize(
    ("limits", "code", "record_id", "padding", "reason"),
    [
        (
            SandboxLimits(max_code_bytes=4),
            "sum(facts)",
            "bill:one",
            "",
            "代码超过字节预算",
        ),
        (
            SandboxLimits(max_record_id_bytes=4),
            "sum(facts)",
            "bill:one",
            "",
            "record_id 超过字节预算",
        ),
        (
            SandboxLimits(max_payload_bytes=128),
            "sum(facts)",
            "bill:one",
            "x" * 256,
            "payload 字节预算",
        ),
    ],
)
def test_child_rechecks_payload_and_field_budgets(
    limits: SandboxLimits,
    code: str,
    record_id: str,
    padding: str,
    reason: str,
) -> None:
    payload = json.dumps(
        {
            "mode": "analysis",
            "code": code,
            "snapshot": {"facts": [{"record_id": record_id, "value": "1"}]},
            "limits": limits.__dict__,
            "padding": padding,
        }
    )
    completed, _ = sandbox_module._spawn(payload, limits=SandboxLimits())
    assert completed.returncode == 2
    assert "SandboxResourceLimit" in completed.stderr
    assert reason in completed.stderr


@pytest.mark.parametrize(
    ("limits", "snapshot", "reason"),
    [
        (
            SandboxLimits(max_field_bytes=4),
            {"facts": [{"record_id": "id", "value": "1"}]},
            "字段名超过字节预算",
        ),
        (
            SandboxLimits(max_value_bytes=1),
            {"facts": [{"record_id": "id", "value": "10"}]},
            "数值文本超过字节预算",
        ),
    ],
)
def test_child_rechecks_snapshot_name_and_value_budgets(
    limits: SandboxLimits, snapshot: dict[str, object], reason: str
) -> None:
    payload = json.dumps(
        {
            "mode": "analysis",
            "code": "sum(facts)",
            "snapshot": snapshot,
            "limits": limits.__dict__,
        }
    )
    completed, _ = sandbox_module._spawn(payload, limits=SandboxLimits())
    assert completed.returncode == 2
    assert "SandboxResourceLimit" in completed.stderr
    assert reason in completed.stderr


def test_generated_analysis_enforces_parent_watchdog() -> None:
    code = "sum(facts)"
    with pytest.raises(SandboxTimeout) as raised:
        run_generated_analysis(
            code,
            {"facts": [{"record_id": "bill:one", "value": "1"}]},
            timeout_s=0.000001,
        )
    _assert_failure_monitor(
        raised.value,
        code=code,
        status="timeout",
        exit_code=-1,
        duration_ms=None,
    )


def test_parent_rejection_attaches_json_safe_monitor() -> None:
    code = "sum(facts)"
    request = SandboxRequest(
        code=code,
        snapshot={"facts": [{"record_id": "bill:one", "value": "1"}]},
        limits=SandboxLimits(max_code_bytes=4),
    )

    with pytest.raises(SandboxResourceLimit) as raised:
        run_in_sandbox(request)

    _assert_failure_monitor(
        raised.value,
        code=code,
        status="rejected",
        exit_code=-1,
        duration_ms=None,
    )


def test_child_rejection_attaches_monitor_and_preserves_error_type() -> None:
    code = "ratio(current, prior)"
    request = SandboxRequest(
        code=code,
        snapshot={
            "current": [{"record_id": "bill:current", "value": "1"}],
            "prior": [{"record_id": "bill:prior", "value": "0"}],
        },
    )

    with pytest.raises(OperatorError, match="分母为 0") as raised:
        run_in_sandbox(request)

    _assert_failure_monitor(
        raised.value,
        code=code,
        status="rejected",
        exit_code=2,
        duration_ms=None,
    )


@pytest.mark.parametrize(
    ("stdout", "status"),
    [
        ("not-json", "failed"),
        (json.dumps({"operator": "sum", "value": "1", "sources": ["forged:1"]}), "rejected"),
    ],
)
def test_rejected_child_result_attaches_monitor(
    monkeypatch: pytest.MonkeyPatch, stdout: str, status: str
) -> None:
    code = "sum(facts)"
    request = SandboxRequest(
        code=code,
        snapshot={"facts": [{"record_id": "bill:one", "value": "1"}]},
    )
    completed = subprocess.CompletedProcess(["fake-sandbox"], 0, stdout, "")
    monkeypatch.setattr(sandbox_module, "_spawn", lambda *_args, **_kwargs: (completed, 18))

    with pytest.raises(SandboxViolation) as raised:
        run_in_sandbox(request)

    _assert_failure_monitor(
        raised.value,
        code=code,
        status=status,
        exit_code=0,
        duration_ms=18,
    )


def test_generated_analysis_rejects_unavailable_os_isolation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AIB_SANDBOX_DISABLE_ISOLATION", "1")
    code = "sum(facts)"
    with pytest.raises(SandboxUnavailable) as raised:
        run_generated_analysis(
            code,
            {"facts": [{"record_id": "bill:one", "value": "1"}]},
        )
    _assert_failure_monitor(
        raised.value,
        code=code,
        status="unavailable",
        exit_code=-1,
        duration_ms=None,
    )


def test_windows_job_object_oserror_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sandbox_module, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(sandbox_module, "os_isolation_available", lambda: True)

    def fail_windows_spawn(*args: object, **kwargs: object) -> None:
        raise OSError("job object unavailable")

    monkeypatch.setattr(sandbox_module, "_spawn_windows", fail_windows_spawn)
    with pytest.raises(SandboxUnavailable, match="创建或限制沙箱子进程失败"):
        run_generated_analysis(
            "sum(facts)",
            {"facts": [{"record_id": "bill:one", "value": "1"}]},
        )


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object integration test")
def test_windows_timeout_returns_when_job_termination_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ApiFunction:
        def __init__(self, function: object) -> None:
            self.function = function
            self._argtypes: object = None
            self._restype: object = None

        @property
        def argtypes(self) -> object:
            return self._argtypes

        @argtypes.setter
        def argtypes(self, value: object) -> None:
            self._argtypes = value
            if hasattr(self.function, "argtypes"):
                self.function.argtypes = value  # type: ignore[attr-defined]

        @property
        def restype(self) -> object:
            return self._restype

        @restype.setter
        def restype(self, value: object) -> None:
            self._restype = value
            if hasattr(self.function, "restype"):
                self.function.restype = value  # type: ignore[attr-defined]

        def __call__(self, *args: object) -> object:
            return self.function(*args)  # type: ignore[operator]

    real_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    failed_termination = ApiFunction(lambda *_args: 0)
    kernel32 = SimpleNamespace(
        CreateJobObjectW=ApiFunction(real_kernel32.CreateJobObjectW),
        SetInformationJobObject=ApiFunction(real_kernel32.SetInformationJobObject),
        AssignProcessToJobObject=ApiFunction(real_kernel32.AssignProcessToJobObject),
        TerminateJobObject=failed_termination,
        CloseHandle=ApiFunction(real_kernel32.CloseHandle),
    )
    monkeypatch.setattr(ctypes, "WinDLL", lambda *args, **kwargs: kernel32)

    real_popen = subprocess.Popen
    processes: list[subprocess.Popen[str]] = []

    def capture_process(*args: object, **kwargs: object) -> subprocess.Popen[str]:
        process = real_popen(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(subprocess, "Popen", capture_process)
    outcome: list[BaseException] = []

    def execute() -> None:
        try:
            sandbox_module._spawn_windows(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                "{}",
                SandboxLimits(timeout_s=0.1),
            )
        except BaseException as error:
            outcome.append(error)

    worker = threading.Thread(target=execute, daemon=True)
    started = time.perf_counter()
    worker.start()
    worker.join(timeout=3.0)
    elapsed = time.perf_counter() - started
    if worker.is_alive():
        if processes:
            processes[0].kill()
        worker.join(timeout=2.0)
        pytest.fail("Windows 沙箱在终止失败后仍无界等待子进程管道")

    assert elapsed < 3.0
    assert len(outcome) == 1
    assert isinstance(outcome[0], subprocess.TimeoutExpired)
    assert processes[0].poll() is not None

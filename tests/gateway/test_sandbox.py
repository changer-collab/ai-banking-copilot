from decimal import Decimal

import pytest

from ai_banking_copilot.gateway.errors import SandboxTimeout, SandboxViolation
from ai_banking_copilot.gateway.operators import run_operator
from ai_banking_copilot.gateway.sandbox import (
    SandboxRequest,
    run_in_sandbox,
    run_probe,
)


def test_sandbox_result_matches_in_process_operator() -> None:
    inputs = (("transactions:t1", Decimal("-3200")), ("transactions:t2", Decimal("-1580")))
    expected = run_operator("sum", inputs)
    result = run_in_sandbox(SandboxRequest(operator="sum", inputs=inputs))
    assert result.operator == "sum"
    assert result.value == expected.value


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

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

import ai_banking_copilot.web as web
from ai_banking_copilot.gateway.seed import DEMO_USER_ID
from ai_banking_copilot.web.app import AppDeps

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_web_package_is_importable() -> None:
    assert web.__doc__ is not None


def test_pyproject_declares_channel_dependencies() -> None:
    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "fastapi" in text
    assert "uvicorn" in text
    assert "httpx" in text


def test_app_deps_holds_channel_wiring(deps: AppDeps) -> None:
    assert deps.user_id == DEMO_USER_ID
    assert len(deps.registry.actions()) == 20
    assert deps.gateway.account("acc_savings").payload.balance.cents == 12843000


def test_app_deps_is_frozen(deps: AppDeps) -> None:
    with pytest.raises(FrozenInstanceError):
        deps.user_id = "other"  # type: ignore[misc]

"""ME-02 未登录只读浏览：页面壳可开、只读 GET 给演示身份、写接口仍 401。

按 `AIB_AUTH_MODE=demo` 的真实装配口径起服务，但不登录：访客只能读，且页面与响应体
必须显式标注「演示数据」；办理业务的写接口一律仍走 `current_user`，不得被演示身份顶替。
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from ai_banking_copilot.gateway.store import Store
from ai_banking_copilot.web.app import AUTH_MODE_DEMO, AppDeps, create_app
from ai_banking_copilot.web.auth import AuthService, init_auth_schema

PASSWORD = "aib-demo-1027"
"""与 `compose._DEFAULT_DEMO_PASSWORD` 同值的验收口令；测试内显式传入，不读环境变量。"""


@pytest.fixture
def anonymous_client(deps: AppDeps, rw_store: Store) -> Iterator[TestClient]:
    """演示模式 + 未登录：只建登录表与演示账号，不复用已登录会话。"""
    init_auth_schema(rw_store)
    auth = AuthService(rw_store)
    auth.ensure_demo_accounts(PASSWORD)
    auth_deps = replace(deps, auth=auth, auth_mode=AUTH_MODE_DEMO, demo_password=PASSWORD)
    with TestClient(create_app(auth_deps)) as opened:
        yield opened


def test_anonymous_visitor_gets_readonly_shell_with_demo_banner(
    anonymous_client: TestClient,
) -> None:
    page = anonymous_client.get("/")
    assert page.status_code == 200
    assert page.headers["cache-control"] == "no-store"
    assert 'id="app"' in page.text, "未登录也应能打开页面壳"
    assert 'data-demo-readonly="1"' in page.text
    assert "演示数据" in page.text, "访客不得把演示账户误当成本人真实账户"
    assert 'href="/login"' in page.text


def test_readonly_life_events_fall_back_to_demo_identity(
    anonymous_client: TestClient,
) -> None:
    response = anonymous_client.get("/api/life-events")
    assert response.status_code == 200
    assert response.json()["demo"] is True


def test_write_submit_still_requires_login(anonymous_client: TestClient) -> None:
    response = anonymous_client.post("/api/submit", json={"utterance": "转给李娜 500 元"})
    assert response.status_code == 401
    assert response.json()["detail"] == "auth_required"

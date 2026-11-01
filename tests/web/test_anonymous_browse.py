"""默认必须先登录：未登录访问页面拿到登录页，只读与写接口一律 401。

按 `AIB_AUTH_MODE=demo` 的真实装配口径起服务，但不登录：页面入口下发登录页
（不再给只读页面壳，避免未登录态触发 `/api/options` / `/api/conversations` 等
需要身份的只读接口）；只读接口也不再回落到演示身份，绕过页面直接打 API 同样 401。"""

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


def test_anonymous_visitor_gets_login_page(
    anonymous_client: TestClient,
) -> None:
    page = anonymous_client.get("/")
    assert page.status_code == 200
    assert page.headers["cache-control"] == "no-store"
    assert 'id="login-form"' in page.text, "未登录必须先登录，不再下发只读页面壳"
    assert "演示账号" in page.text
    assert 'data-demo-readonly="1"' not in page.text
    assert "/assets/app.js" not in page.text, "登录页不加载应用脚本，未登录态不触发需身份的只读接口"


def test_readonly_api_also_requires_login(anonymous_client: TestClient) -> None:
    """页面要求先登录后，只读接口不再回落到演示身份：直接打 API 也是 401。"""
    response = anonymous_client.get("/api/life-events")
    assert response.status_code == 401
    assert response.json()["detail"] == "auth_required"


def test_write_submit_still_requires_login(anonymous_client: TestClient) -> None:
    response = anonymous_client.post("/api/submit", json={"utterance": "转给李娜 500 元"})
    assert response.status_code == 401
    assert response.json()["detail"] == "auth_required"

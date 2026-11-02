"""多用户 Web 渠道端到端守卫：登录 → 数据隔离 → 历史可查/可删 → 跨站写拦截。

只替代模型通道；鉴权、Cookie、CSRF、会话状态机、快照与历史持久化**全部走真实实现**，
因此这些用例同时是 `AIB_AUTH_MODE=demo` 部署口径的验收证据。
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from ai_banking_copilot.gateway.merchant_store import init_merchant_schema
from ai_banking_copilot.gateway.seed import (
    DEMO_USER_ID,
    SECONDARY_USER_ID,
    seed_secondary_user,
)
from ai_banking_copilot.gateway.store import Store
from ai_banking_copilot.web.app import AUTH_MODE_DEMO, AppDeps, create_app
from ai_banking_copilot.web.auth import (
    TICKET_PURPOSE_DELETE_CONVERSATION,
    AuthService,
    init_auth_schema,
)
from ai_banking_copilot.web.conversation_store import (
    ConversationStore,
    init_conversation_schema,
)
from ai_banking_copilot.web.life_event_store import LifeEventStore, init_life_event_schema
from ai_banking_copilot.web.session_repository import (
    SQLiteSessionRepository,
    init_session_schema,
)

PASSWORD = "aib-demo-1027"
"""与 `compose._DEFAULT_DEMO_PASSWORD` 同值的验收口令；测试内显式传入，不读环境变量。"""


@pytest.fixture
def auth_deps(deps: AppDeps, rw_store: Store) -> AppDeps:
    """在完整单用户装配上叠加「登录 + 第二用户 + 历史 + 快照」，其余依赖保持不变。"""
    init_merchant_schema(rw_store)
    seed_secondary_user(rw_store)
    init_auth_schema(rw_store)
    init_conversation_schema(rw_store)
    init_life_event_schema(rw_store)
    init_session_schema(rw_store)
    auth = AuthService(rw_store)
    auth.ensure_demo_accounts(PASSWORD)
    return replace(
        deps,
        auth=auth,
        auth_mode=AUTH_MODE_DEMO,
        demo_password=PASSWORD,
        conversations=ConversationStore(rw_store),
        life_events=LifeEventStore(rw_store),
        session_repository=SQLiteSessionRepository(rw_store),
    )


@pytest.fixture
def client(auth_deps: AppDeps) -> Iterator[TestClient]:
    with TestClient(create_app(auth_deps)) as opened:
        yield opened


def _login(client: TestClient, username: str) -> None:
    response = client.post(
        "/api/auth/login", json={"username": username, "password": PASSWORD}
    )
    assert response.status_code == 200, response.text


def test_unauthenticated_page_requires_login_and_write_api_is_401(
    client: TestClient,
) -> None:
    """默认必须先登录：未登录访问页面拿到登录页，写接口仍一律 401。"""
    page = client.get("/")
    assert page.status_code == 200
    assert 'id="login-form"' in page.text, "未登录必须先登录，不再下发只读页面壳"
    assert 'data-demo-readonly="1"' not in page.text
    assert "/assets/app.js" not in page.text, "登录页不加载应用脚本，未登录态不触发需身份的只读接口"

    login = client.get("/login")
    assert login.status_code == 200
    assert 'id="login-form"' in login.text

    api = client.get("/api/session")
    assert api.status_code == 401
    assert api.json()["detail"] == "auth_required"

    im = client.get("/im")
    assert 'id="login-form"' in im.text and "/assets/app.js" not in im.text


def test_login_page_explains_product_and_shows_demo_credentials(client: TestClient) -> None:
    page = client.get("/login")
    assert "一句话，智能办理银行业务" in page.text
    assert "演示账号" in page.text
    assert "chenming" in page.text and "linjing" in page.text
    assert PASSWORD in page.text
    assert "由现场负责人提供" not in page.text


def test_login_page_hides_password_when_not_configured(auth_deps: AppDeps) -> None:
    without_password = replace(auth_deps, demo_password=None)
    with TestClient(create_app(without_password)) as client:
        page = client.get("/login")
    assert "由现场负责人提供" in page.text
    assert PASSWORD not in page.text


def test_options_and_me_only_contain_the_logged_in_users_facts(
    client: TestClient, auth_deps: AppDeps
) -> None:
    _login(client, "chenming")
    primary = client.get("/api/options").json()
    primary_ids = {account["id"] for account in primary["accounts"]}
    assert primary_ids == {"acc_savings", "acc_current"}
    assert client.get("/api/me").json()["user"]["user_id"] == DEMO_USER_ID

    secondary = TestClient(create_app(auth_deps))
    _login(secondary, "linjing")
    secondary_ids = {
        account["id"] for account in secondary.get("/api/options").json()["accounts"]
    }
    assert secondary_ids == {"acc_b_savings", "acc_b_current"}
    assert not secondary_ids & primary_ids, "两个账号的账户集合不得有交集"
    assert secondary.get("/api/me").json()["user"]["user_id"] == SECONDARY_USER_ID


def test_transfer_records_are_scoped_to_the_logged_in_user(
    client: TestClient, auth_deps: AppDeps
) -> None:
    """`/api/transfers` 必须按登录用户查表：第二用户看不到第一用户的转账记录。"""
    _login(client, "chenming")
    submitted = client.post("/api/submit", json={"utterance": "转给李娜 500 元"}).json()
    card = submitted["card"]
    confirmed = client.post(
        "/api/confirm",
        json={"card_id": card["card_id"], "frozen_hash": card["frozen_hash"]},
    )
    assert confirmed.status_code == 200
    primary_items = client.get("/api/transfers").json()["items"]
    assert any(item["amount_cents"] == 50000 for item in primary_items)

    secondary = TestClient(create_app(auth_deps))
    _login(secondary, "linjing")
    secondary_items = secondary.get("/api/transfers").json()["items"]
    assert not any(item["amount_cents"] == 50000 for item in secondary_items), (
        "第二用户不得看到第一用户的转账记录"
    )


def test_cards_are_scoped_to_the_logged_in_user(
    client: TestClient, auth_deps: AppDeps
) -> None:
    """`/api/cards` 必须按登录用户查表：两个账号的卡片集合不得有交集。"""
    _login(client, "chenming")
    primary = {card["card_id"] for card in client.get("/api/cards").json()["cards"]}

    secondary = TestClient(create_app(auth_deps))
    _login(secondary, "linjing")
    other = {card["card_id"] for card in secondary.get("/api/cards").json()["cards"]}
    assert primary and other
    assert not primary & other


def test_wealth_holdings_are_scoped_to_the_logged_in_user(
    client: TestClient, auth_deps: AppDeps
) -> None:
    """`/api/wealth/overview` 的持仓必须按登录用户查表，不得回落到装配期用户。"""
    _login(client, "chenming")
    primary = {
        row["product_id"]: row["amount_yuan"]
        for row in client.get("/api/wealth/overview").json()["holdings"]
    }
    assert primary == {"wp_cash": "500.00", "wp_short": "2000.00", "wp_bond": "3000.00"}

    secondary = TestClient(create_app(auth_deps))
    _login(secondary, "linjing")
    other = {
        row["product_id"]: row["amount_yuan"]
        for row in secondary.get("/api/wealth/overview").json()["holdings"]
    }
    assert other == {"wp_bond": "1200.00", "wp_mix": "6000.00"}
    assert other != primary, "两个账号的持仓金额不得互相串号"


def test_cross_site_write_is_rejected_before_any_state_change(client: TestClient) -> None:
    _login(client, "chenming")
    response = client.post(
        "/api/submit",
        json={"utterance": "查询账户余额", "session_id": "csrf"},
        headers={"origin": "https://evil.example"},
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "cross_site_rejected"

    assert client.get("/api/conversations").json()["conversations"] == [], "被拦的请求不得留下历史"


def test_conversation_history_is_restorable_and_scoped_to_owner(
    client: TestClient, auth_deps: AppDeps
) -> None:
    _login(client, "chenming")
    submit = client.post(
        "/api/submit", json={"utterance": "查询账户余额", "session_id": "hist-1"}
    )
    assert submit.status_code == 200, submit.text

    listed = client.get("/api/conversations").json()
    assert listed["available"] is True
    rows = [row for row in listed["conversations"] if row["session_id"] == "hist-1"]
    assert len(rows) == 1
    assert rows[0]["title"].startswith("查询账户余额")

    detail = client.get("/api/conversations/hist-1").json()
    roles = [message["role"] for message in detail["messages"]]
    assert roles[:2] == ["user", "assistant"], "点开应能恢复提问与回答"
    assert detail["messages"][0]["text"] == "查询账户余额"

    other = TestClient(create_app(auth_deps))
    _login(other, "linjing")
    assert other.get("/api/conversations").json()["conversations"] == []
    assert other.get("/api/conversations/hist-1").status_code == 404


def test_delete_conversation_requires_single_use_reverify_ticket(
    client: TestClient,
) -> None:
    _login(client, "chenming")
    assert client.post(
        "/api/submit", json={"utterance": "查询账户余额", "session_id": "del-1"}
    ).status_code == 200

    denied = client.delete("/api/conversations/del-1")
    assert denied.status_code == 403, "没有二次验证票据不得删除"
    assert client.get("/api/conversations/del-1").status_code == 200

    ticket = client.post(
        "/api/auth/reverify",
        json={"password": PASSWORD, "purpose": TICKET_PURPOSE_DELETE_CONVERSATION},
    ).json()["ticket"]
    removed = client.delete(
        "/api/conversations/del-1", headers={"X-Reverify-Ticket": ticket}
    )
    assert removed.status_code == 200, removed.text
    assert client.get("/api/conversations/del-1").status_code == 404
    assert all(
        row["session_id"] != "del-1"
        for row in client.get("/api/conversations").json()["conversations"]
    )

    replay = client.delete(
        "/api/conversations/del-1", headers={"X-Reverify-Ticket": ticket}
    )
    assert replay.status_code == 403, "票据一次性，重放应失效"


def test_logout_invalidates_the_old_cookie(client: TestClient) -> None:
    _login(client, "chenming")
    assert client.get("/api/session").status_code == 200
    token = client.cookies.get("aib_session")
    assert token, "登录后应签发 HttpOnly 会话 Cookie"

    assert client.post("/api/auth/logout").status_code == 200

    replay = client.get("/api/session", headers={"cookie": f"aib_session={token}"})
    assert replay.status_code == 401, "退出后旧 Cookie 必须失效"


def test_life_event_recorded_when_clarification_completes_the_plan(
    client: TestClient,
) -> None:
    _login(client, "chenming")
    first = client.post(
        "/api/submit",
        json={
            "utterance": "我妈下个月过生日",
            "session_id": "life-1",
        },
    ).json()
    assert first["state"] == "needs_clarification", "该话术应先走澄清"
    assert client.get("/api/life-events").json()["records"] == [], "未成卡前不登记"

    answers = {
        ask["slot"]: ("299元" if "amount" in ask["slot"] else "鲜花礼品店")
        for ask in first["clarifications"]
        if "user_id" not in ask["slot"]
    }
    second = client.post(
        "/api/clarify", json={"answers": answers, "session_id": "life-1"}
    ).json()
    assert second["state"] == "awaiting_confirm", second.get("message")

    records = client.get("/api/life-events").json()["records"]
    assert [
        record["session_id"] for record in records if record["event_id"] == "L1"
    ] == ["life-1"], "澄清后成卡也要登记，且只登记一次"


def test_life_event_panel_lists_seven_prototypes_with_examples(client: TestClient) -> None:
    _login(client, "chenming")
    payload = client.get("/api/life-events").json()
    prototypes = payload["prototypes"]
    assert [item["event_id"] for item in prototypes] == [f"L{index}" for index in range(1, 8)]
    for item in prototypes:
        assert item["description"], f"{item['event_id']} 缺少一句人话说明"
        assert item["example"], f"{item['event_id']} 缺少示例话术"
        assert item["steps"] >= 2
        assert item["triggers"], f"{item['event_id']} 缺少触发词"
    assert payload["records"] == []
    assert payload["records_available"] is True


def test_docs_and_openapi_are_closed_unless_explicitly_enabled(
    auth_deps: AppDeps, monkeypatch: pytest.MonkeyPatch
) -> None:
    """接口清单默认不公开：/docs 与 /openapi.json 都必须 404。"""
    monkeypatch.delenv("AIB_ENABLE_DOCS", raising=False)
    closed = TestClient(create_app(auth_deps))
    assert closed.get("/docs").status_code == 404
    assert closed.get("/openapi.json").status_code == 404

    monkeypatch.setenv("AIB_ENABLE_DOCS", "1")
    opened = TestClient(create_app(auth_deps))
    assert opened.get("/docs").status_code == 200
    assert opened.get("/openapi.json").status_code == 200


def test_login_cookie_sets_secure_only_when_flagged(
    auth_deps: AppDeps, monkeypatch: pytest.MonkeyPatch
) -> None:
    """HTTPS 部署用 AIB_COOKIE_SECURE=1 打开 Secure；本机 http 调试默认不带。"""
    monkeypatch.delenv("AIB_COOKIE_SECURE", raising=False)
    plain = TestClient(create_app(auth_deps))
    plain_cookie = plain.post(
        "/api/auth/login", json={"username": "chenming", "password": PASSWORD}
    ).headers["set-cookie"]
    assert "HttpOnly" in plain_cookie and "SameSite=lax" in plain_cookie
    assert "Secure" not in plain_cookie

    monkeypatch.setenv("AIB_COOKIE_SECURE", "1")
    secured = TestClient(create_app(auth_deps))
    secured_cookie = secured.post(
        "/api/auth/login", json={"username": "chenming", "password": PASSWORD}
    ).headers["set-cookie"]
    assert "Secure" in secured_cookie


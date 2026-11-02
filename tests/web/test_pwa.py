from fastapi.testclient import TestClient

from ai_banking_copilot.web.app import create_app


def test_index_exposes_versioned_installable_shell(deps: object) -> None:
    client = TestClient(create_app(deps))  # type: ignore[arg-type]
    page = client.get("/")
    html = page.text
    assert page.headers["cache-control"] == "no-store", "壳 HTML 内嵌版本号，不能被缓存"
    assert 'rel="manifest"' in html
    assert "/assets/manifest.json?v=1" in html
    assert "/assets/app.js?v=101" in html
    assert "/assets/workspace.css?v=64" in html
    assert "当前离线" in html
    assert 'id="conversation-sidebar"' in html
    assert 'id="conversation-welcome"' in html
    assert 'id="task-guard"' in html
    assert 'class="composer"' in html
    assert 'aria-controls="trace"' in html
    assert 'id="trace"' in html
    assert 'id="view-home"' in html
    assert 'id="view-chat"' in html
    assert 'id="view-records"' in html
    assert 'id="view-me"' in html
    assert 'id="app-tabbar"' in html
    assert 'id="btn-install"' in html
    assert 'id="install-status"' in html
    assert 'data-record-tab="bills"' in html
    assert 'data-record-tab="charges"' in html
    assert 'data-action="refresh-subscriptions"' in html


def test_service_worker_allows_root_scope(deps: object) -> None:
    """`sw.js` 挂在 `/assets` 下，必须显式允许根作用域，否则浏览器注册即被拒。"""
    client = TestClient(create_app(deps))  # type: ignore[arg-type]
    resp = client.get("/assets/sw.js?v=25")
    assert resp.status_code == 200
    assert resp.headers["service-worker-allowed"] == "/"


def test_service_worker_keeps_api_network_only(deps: object) -> None:
    client = TestClient(create_app(deps))  # type: ignore[arg-type]
    manifest = client.get("/assets/manifest.json?v=1")
    worker = client.get("/assets/sw.js?v=1")
    workspace = client.get("/assets/workspace.css?v=64")
    assert manifest.status_code == 200
    assert manifest.json()["display"] == "standalone"
    assert worker.status_code == 200
    assert workspace.status_code == 200
    assert ".layout.layout--workspace" in workspace.text
    assert ".layout--workspace [hidden]" in workspace.text
    assert 'const CACHE_NAME = "aib-static-shell-v87"' in worker.text
    assert '"/assets/workspace.css?v=64"' in worker.text
    assert '"/assets/app.js?v=101"' in worker.text
    assert '"/assets/app.css?v=9"' in worker.text
    app_js = client.get("/assets/app.js?v=101")
    assert 'register("/assets/sw.js?v=75"' in app_js.text
    assert "beforeinstallprompt" in app_js.text
    assert "appinstalled" in app_js.text
    assert "function previewHtml(" in app_js.text
    assert "provenance-panel" in app_js.text
    assert "function roleChainHtml(" in app_js.text
    assert "role-card__value" in app_js.text
    assert "function takeoverStatusHtml(" in app_js.text
    assert 'data.state === "taken_over"' in app_js.text
    assert "paused: true" in app_js.text
    assert 'url.pathname.startsWith("/api/")' in worker.text
    assert "event.respondWith(fetch(request))" in worker.text

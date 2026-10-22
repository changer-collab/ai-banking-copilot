from fastapi.testclient import TestClient

from ai_banking_copilot.web.app import create_app


def test_index_exposes_versioned_installable_shell(deps: object) -> None:
    client = TestClient(create_app(deps))  # type: ignore[arg-type]
    html = client.get("/").text
    assert 'rel="manifest"' in html
    assert "/assets/manifest.json?v=1" in html
    assert "/assets/app.js?v=15" in html
    assert "/assets/workspace.css?v=4" in html
    assert "当前离线" in html
    assert 'id="conversation-sidebar"' in html
    assert 'id="conversation-welcome"' in html
    assert 'id="task-guard"' in html
    assert 'class="composer"' in html
    assert 'aria-controls="trace"' in html
    assert 'id="trace"' in html


def test_service_worker_keeps_api_network_only(deps: object) -> None:
    client = TestClient(create_app(deps))  # type: ignore[arg-type]
    manifest = client.get("/assets/manifest.json?v=1")
    worker = client.get("/assets/sw.js?v=1")
    workspace = client.get("/assets/workspace.css?v=4")
    assert manifest.status_code == 200
    assert manifest.json()["display"] == "standalone"
    assert worker.status_code == 200
    assert workspace.status_code == 200
    assert ".layout.layout--workspace" in workspace.text
    assert ".layout--workspace [hidden]" in workspace.text
    assert 'const CACHE_NAME = "aib-static-shell-v10"' in worker.text
    assert '"/assets/workspace.css?v=4"' in worker.text
    assert '"/assets/app.js?v=15"' in worker.text
    assert '"/assets/app.css?v=6"' in worker.text
    app_js = client.get("/assets/app.js?v=15")
    assert 'register("/assets/sw.js?v=10"' in app_js.text
    assert 'url.pathname.startsWith("/api/")' in worker.text
    assert "event.respondWith(fetch(request))" in worker.text

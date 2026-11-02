"""场景①「额度卡」（2026-10-28）：限额 / 已用 / 剩余必须来自服务端，前端不计算。

断言四件事：
1. `/api/options.quota` 给出「有效日限额」与「当日已用」，限额与 `policy/execution.py` 同口径
   （`min(官方 1000 元, 客户资产分档)`），不在这里另立一套数字；
2. 页面有额度卡容器（`#quota-panel` / `#quota-status` / `#quota-body`），进度条样式落在我方
   `workspace.css`（`app.css` 与设计仓逐字同步，不能加东西）；
3. 执行一笔 500 元转账后，`daily_accumulated_cents` 增量为 50000 —— 数字由服务端从流水汇总，
   不是前端把金额累加出来；
4. 记录页「转账记录」表头含「详情」列，行内有可展开的详情块。
"""

from fastapi.testclient import TestClient

from ai_banking_copilot.gateway.seed import DEMO_USER_ID
from ai_banking_copilot.web.app import AppDeps, create_app


def test_options_exposes_quota_in_the_same_band_as_policy(deps: AppDeps) -> None:
    quota = TestClient(create_app(deps)).get("/api/options").json()["quota"]
    assert quota["as_of"] == deps.today.isoformat()
    expected_limit = min(100000, deps.gateway.daily_transfer_limit(DEMO_USER_ID).cents)
    assert quota["daily_limit_cents"] == expected_limit, "有效日限额必须与 PDT/PEP 同口径（min 后）"
    assert quota["daily_accumulated_cents"] == deps.gateway.daily_transfer_total(
        DEMO_USER_ID, deps.today
    ).cents


def test_quota_accumulates_after_a_real_transfer(deps: AppDeps) -> None:
    client = TestClient(create_app(deps))
    before = client.get("/api/options").json()["quota"]["daily_accumulated_cents"]
    card = client.post("/api/submit", json={"utterance": "转给李娜 500 元"}).json()["card"]
    confirmed = client.post(
        "/api/confirm",
        json={"card_id": card["card_id"], "frozen_hash": card["frozen_hash"]},
    )
    assert confirmed.status_code == 200
    after = client.get("/api/options").json()["quota"]["daily_accumulated_cents"]
    assert after - before == 50000, "已用必须随真实转账在服务端增长（500 元 = 50000 分）"


def test_page_has_quota_card_and_progress_style(deps: AppDeps) -> None:
    client = TestClient(create_app(deps))
    html = client.get("/").text
    assert 'id="quota-panel"' in html
    assert 'id="quota-status"' in html
    assert 'id="quota-body"' in html
    workspace = client.get("/assets/workspace.css?v=13").text
    assert ".quota__bar" in workspace, "新样式必须落 workspace.css（app.css 与设计仓同步）"


def test_records_transfer_tab_has_a_detail_column(deps: AppDeps) -> None:
    app_js = TestClient(create_app(deps)).get("/assets/app.js?v=29").text
    assert ('transfers: ["记账日", "收款人", "金额", "方式", "状态", "用途", "记录号", "详情"]'
            in app_js)
    assert 'class="records-detail"' in app_js
    assert "已发出申请·可撤销" in app_js

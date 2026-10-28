"""订阅 UI 静态契约；真实点击与三态渲染由浏览器验收覆盖。"""

from fastapi.testclient import TestClient

from ai_banking_copilot.web.app import AppDeps, create_app


def _script(deps: AppDeps) -> str:
    return TestClient(create_app(deps)).get("/assets/app.js").text


def test_subscription_cards_expose_price_tristate_and_next_charge(deps: AppDeps) -> None:
    script = _script(deps)
    assert "item.price_increased_12m === true" in script
    assert "item.price_increased_12m === false" in script
    for label in ("近 12 个月涨价", "近 12 个月未涨价", "样本不足", "下次扣款日"):
        assert label in script
    assert "yuan(item.price_change_12m_from_cents)" in script
    assert "yuan(item.price_change_12m_to_cents)" in script
    assert ': "未提供"' in script
    assert "subscriptionStatusLabel(item.status, item.registered_status)" in script
    assert 'cancelled: "已取消订阅"' in script
    assert 'active: "已确认订阅"' not in script


def test_charge_records_reuse_options_evidence_and_show_seven_columns(deps: AppDeps) -> None:
    client = TestClient(create_app(deps))
    html = client.get("/").text
    script = _script(deps)
    stylesheet = client.get("/assets/workspace.css").text
    assert 'data-record-tab="charges"' in html
    assert 'if (key === "charges")' in script
    assert "state.options.subscription_details" in script
    assert "seen.has(txn.record_id)" in script
    assert "seen.add(txn.record_id)" in script
    assert 'class="charge-records-table"' in script
    table = script.split('class="charge-records-table"', 1)[1].split("</thead>", 1)[0]
    headers = ("商户", "金额", "时间", "卡尾号", "渠道", "记录 ID", "内容哈希")
    positions = [table.index(f"<th>{header}</th>") for header in headers]
    assert positions == sorted(positions)
    assert "txn.booked_at || txn.booked_on" in script
    assert 'txn.card_tail || "未提供"' in script
    assert 'title="${esc(txn.content_hash)}"' in script
    assert ".charge-records-table" in stylesheet
    assert "table-layout: fixed" in stylesheet
    assert "/api/charges" not in script


def test_audit_uses_recorded_channel_and_correlates_digest(deps: AppDeps) -> None:
    script = _script(deps)
    assert 'payload.channel || "未记录"' in script
    assert "payload.text_digest" in script
    assert "同摘要" in script
    assert 'audit.ok ? audit.data || {} : { available: false }' in script


def test_subscription_records_and_health_have_human_states(deps: AppDeps) -> None:
    script = _script(deps)
    for text in (
        "正在读取订阅识别结果…", "未识别到订阅。", "订阅数据暂不可用，请稍后重试。",
        "正在读取扣款记录…", "暂无可核对的订阅扣款记录。", "扣款记录暂不可用，请稍后重试。",
        "正在读取渠道状态…", "暂无渠道状态。", "渠道状态暂不可用，请稍后重试。",
    ):
        assert text in script
    assert 'optionsStatus: "loading"' in script
    assert 'state.optionsStatus = "error"' in script
    assert 'state.optionsStatus = "ready"' in script


def test_home_routes_reach_subscriptions_records_and_health_within_three_clicks(
    deps: AppDeps,
) -> None:
    html = TestClient(create_app(deps)).get("/").text
    home = html.split('id="view-home"', 1)[1].split('id="view-chat"', 1)[0]
    assert 'data-nav="scene" data-scene="subscriptions"' in home
    assert 'data-nav="records"' in home
    assert 'data-nav="health"' in home
    assert 'data-record-tab="charges"' in html
    assert 'data-nav="records" data-record-tab="charges"' in html

"""订阅 UI 静态契约；真实点击与三态渲染由浏览器验收覆盖。"""

from fastapi.testclient import TestClient

from ai_banking_copilot.web.app import AppDeps, create_app


def _script(deps: AppDeps) -> str:
    return TestClient(create_app(deps)).get("/assets/app.js").text


def test_subscription_cards_expose_agreement_facts_and_close_action(deps: AppDeps) -> None:
    script = _script(deps)
    assert "item.price_change" in script
    assert "agreementPriceHtml(item)" in script
    for label in (
        "关闭扣款协议", "协议详情 · 扣款记录", "已解约",
        "未发现近 12 个月涨价", "累计扣款数据不足",
    ):
        assert label in script
    assert "yuan(item.price_change.from_cents)" in script
    assert "yuan(item.price_change.to_cents)" in script
    assert ': "未提供"' in script
    assert 'data-sub-cancel=' in script
    assert '<span class="chip">已解约</span>' in script
    assert 'active: "已确认订阅"' not in script


def test_charge_records_share_the_generic_records_table(deps: AppDeps) -> None:
    """扣款记录必须和其他记录页共用 `records-table` 与三态，不再自建表格。"""
    client = TestClient(create_app(deps))
    html = client.get("/").text
    script = _script(deps)
    stylesheet = client.get("/assets/workspace.css").text
    assert 'data-record-tab="charges"' in html
    assert 'if (key === "charges")' in script
    assert "/api/records/charges" in script
    assert "chargesPanelHtml" in script
    assert "chargeEntryRowHtml" in script
    assert "payment_account" in script
    assert "record_no" in script
    assert "charge-records-table" not in script
    assert "charge-records-table" not in stylesheet
    # 列定义仍由 `RECORD_HEADERS` 提供，且顺序固定
    headers_block = script.split("charges: [", 1)[1].split("]", 1)[0]
    headers = ("时间", "类型", "商户", "金额", "方向", "付款账户", "状态", "记录号")
    positions = [headers_block.index(f'"{header}"') for header in headers]
    assert positions == sorted(positions)
    assert "legacyChargePayload" in script
    assert "recordsTableHtml(RECORD_HEADERS.charges" in script
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
        "正在读取代扣协议…", "未识别到代扣协议。", "代扣协议数据暂不可用，请稍后重试。",
        "扣款记录", "正在读取${esc(spec.label)}…", "${esc(spec.label)} · 暂无记录",
        "${spec.label}读取失败，请稍后重试。",
    ):
        assert text in script
    assert 'optionsStatus: "loading"' in script
    assert 'state.optionsStatus = "error"' in script
    assert 'state.optionsStatus = "ready"' in script


def test_home_routes_reach_subscriptions_and_records_within_three_clicks(
    deps: AppDeps,
) -> None:
    html = TestClient(create_app(deps)).get("/").text
    home = html.split('id="view-home"', 1)[1].split('id="view-chat"', 1)[0]
    assert 'data-nav="scene" data-scene="subscriptions"' in home
    assert 'data-nav="records"' in home
    # 渠道并入「我的」：左栏与首页工具区不再有入口；独立渠道健康页已下线
    assert 'data-nav="health"' not in html
    assert 'id="view-health"' not in html
    assert 'id="view-me"' in html
    assert 'data-record-tab="charges"' in html
    assert 'data-nav="records" data-record-tab="charges"' in html

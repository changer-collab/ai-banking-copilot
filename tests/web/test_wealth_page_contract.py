"""理财场景页静态契约（实测 WEA-P09 / P08 / P04）。

真实点击与三态渲染由浏览器验收覆盖；这里只锁住「改过的入口结构不被回退」。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from ai_banking_copilot.web.app import AppDeps, create_app


def _script(deps: AppDeps) -> str:
    return TestClient(create_app(deps)).get("/assets/app.js").text


def _html(deps: AppDeps) -> str:
    return TestClient(create_app(deps)).get("/").text


def _function_body(script: str, name: str) -> str:
    """截取一个顶层函数的源码（`async` 与否都适用，终点是下一个顶层函数）。"""
    start = script.index(f"function {name}(")
    end = script.index("\nfunction ", start)
    return script[start:end]


def test_wealth_scene_block_has_inline_plan_hosts(deps: AppDeps) -> None:
    """申赎必须在本页出卡；容器缺失时计划卡无处可去。"""
    html = _html(deps)
    start = html.index('data-scene-block="wealth"')
    block = html[start:html.index('data-scene-block="cards"', start)]
    assert 'id="wealth-plan"' in block
    assert 'id="wealth-plan-status"' in block


def test_wealth_actions_open_trade_sheet(deps: AppDeps) -> None:
    """产品卡不再直接提交，统一打开交易确认弹窗；执行仍走 /api/confirm。"""
    body = _function_body(_script(deps), "onWealthAction")
    assert "openWealthTradeSheet(" in body
    assert "openWealthRedeemSheet(" in body
    assert "submitTask(" not in body


def test_page_loads_wealth_trade_script(deps: AppDeps) -> None:
    """四件套 / 份额 / 验证码弹窗由独立脚本承担，页面必须加载它。"""
    html = _html(deps)
    assert "/assets/wealth_trade.js" in html
    script = TestClient(create_app(deps)).get("/assets/wealth_trade.js").text
    assert "openWealthTradeSheet" in script
    assert "openWealthProductDetail" in script


def test_assessment_success_card_is_not_overwritten_by_rerender(deps: AppDeps) -> None:
    """WEA-P08①：`refreshWealth()` 重建问卷时会覆盖成功卡。"""
    script = _script(deps)
    assert "data-assessment-result" in script
    condition = script.split('const box = el("wealth-assessment-body");', 1)[1].split("}", 1)[0]
    assert "data-assessment-result" in condition


def test_assessment_submit_requires_second_confirmation(deps: AppDeps) -> None:
    """WEA-P08②：点「提交测评」先出确认条，不能直接落库。"""
    script = _script(deps)
    assert "data-assessment-confirm" in script
    assert "data-assessment-back" in script


def test_holding_row_is_display_only_with_share_and_maturity(deps: AppDeps) -> None:
    """持仓行只展示份额 / 金额 / 到期日，赎回统一进确认弹窗。"""
    script = _script(deps)
    body = _function_body(script, "wealthHoldingHtml")
    assert "shares" in body
    assert "maturity_on" in body
    assert "data-wealth-redeem" in body
    assert "data-amount-for" not in body


def test_risk_level_shows_demo_source_chip(deps: AppDeps) -> None:
    """WEA-P04：后端已下发 `source_label`，前端必须渲染，seed 还要标「演示数据」。"""
    body = _function_body(_script(deps), "wealthGradeHtml")
    assert "source_label" in body
    assert "演示数据" in body

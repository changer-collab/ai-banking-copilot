"""理财场景页静态契约（实测 WEA-P09 / P08 / P04）。

真实点击与三态渲染由浏览器验收覆盖；这里只锁住「改过的入口结构不被回退」。
"""

from __future__ import annotations

import re

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


def test_wealth_actions_render_plan_in_page_not_chat(deps: AppDeps) -> None:
    """WEA-P09②：`submitTask` 会把卡塞进隐藏的 `#view-chat`，页面看起来毫无反应。"""
    body = _function_body(_script(deps), "onWealthAction")
    assert "sceneRunTask(" in body
    assert "submitTask(" not in body
    assert '"wealth-plan"' in body


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


def test_holding_row_shows_redeemable_cap(deps: AppDeps) -> None:
    """WEA-P09①：上限是这一行自己的事实，要写在行内而不是卡片底部报错里。"""
    script = _script(deps)
    body = _function_body(script, "wealthHoldingHtml")
    assert "可赎回" in body
    assert re.search(r"最多可赎回", script), "超限提示要给出可赎回上限"


def test_risk_level_shows_demo_source_chip(deps: AppDeps) -> None:
    """WEA-P04：后端已下发 `source_label`，前端必须渲染，seed 还要标「演示数据」。"""
    body = _function_body(_script(deps), "wealthGradeHtml")
    assert "source_label" in body
    assert "演示数据" in body

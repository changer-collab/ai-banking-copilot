"""页面骨架约束：全局提示条不能藏在某个视图里，否则在别的视图点「新建对话」会毫无反馈。"""

from __future__ import annotations

from ai_banking_copilot.web.page import INDEX_HTML


def _segment(html: str, start_marker: str, end_marker: str) -> str:
    start = html.index(start_marker)
    end = html.index(end_marker, start)
    return html[start:end]


def test_task_guard_lives_outside_chat_view() -> None:
    # 切片终点必须是 #view-chat 之后的第一个视图；若用 <aside class="trace rail"
    # 作终点，会把 chat 之后、aside 之前的顶层内容（含提示条）也算进对话视图。
    next_view = '<section class="view" id="view-scene"'
    chat_view = _segment(INDEX_HTML, '<section class="view view--chat"', next_view)
    main = _segment(INDEX_HTML, '<main class="main"', '<aside class="trace rail"')

    assert 'id="task-guard"' not in chat_view, "提示条在 #view-chat 内，其他视图下看不见"
    assert 'id="task-guard"' in main, "提示条必须在 main 层常驻"

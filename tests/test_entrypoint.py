"""应用入口（组合根）可导入性。

组合根承担写面装配（`CoreBank` + `Pep`），故**不放在 `web/`**——`web/` 层源码不得直接
触碰 `CoreBank`（H2：执行只经 `policy.pep.Pep`，由 `tests/web/test_web_integration.py` 守卫）。
"""

from ai_banking_copilot.__main__ import main


def test_entrypoint_is_importable() -> None:
    assert callable(main)

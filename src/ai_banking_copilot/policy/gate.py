"""执行门（**计划级**）：确认后 DAG 一致性校验（防 TOCTOU，PRD §5.1 / §5.4）。

用户确认的是**那份 DAG**（`cfhash` 冻结），不是一段文本：确认后增删改任一节点
或控制槽参数 ⇒ 哈希不匹配 ⇒ 拒绝执行。

**双层叠加**：本模块只做**计划级**冻结校验（`frozen_hash == cfhash(plan.nodes)`）；
**节点级**成员资格校验由 `policy.pep.manifest_hash(call)` 承担（见 Task 7）。二者叠加即
PRD 硬约束 #4 的「节点 ∈ 已冻结确认子图」——06 必须在确认记录里**同时保存 `Plan` 与 `cfhash`**。
"""

from __future__ import annotations

from ai_banking_copilot.contracts.plan import Plan, cfhash


class StalePlanError(RuntimeError):
    """计划在用户确认后被改动：哈希不匹配，执行门拒绝。"""


def assert_frozen(plan: Plan, frozen_hash: str) -> None:
    """执行门判据：`cfhash(plan.nodes)` 必须等于确认时冻结的哈希。"""
    current = cfhash(plan.nodes)
    if current != frozen_hash:
        raise StalePlanError(
            f"执行门拒绝：计划哈希不匹配（确认 {frozen_hash}｜当前 {current}）"
        )

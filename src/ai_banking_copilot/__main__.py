"""应用入口（组合根）：装配默认依赖并启动 HTTP 服务。

放在**包根**而非 `web/`：`web/` 层源码**不得直接触碰 `CoreBank`**（H2：执行只经
`policy.pep.Pep`，由 `tests/web/test_web_integration.py` 机读守卫），故写面装配
（`CoreBank` + `Pep`）由本组合根承担；`web/` 只消费注入好的 `AppDeps`。
"""

from __future__ import annotations

from pathlib import Path

import uvicorn

from ai_banking_copilot.gateway.core_bank import CoreBank
from ai_banking_copilot.gateway.facts import FactGateway
from ai_banking_copilot.gateway.seed import DEMO_USER_ID, seed_database
from ai_banking_copilot.gateway.store import Store, StoreMode, init_schema
from ai_banking_copilot.planning.llm import TemplateIntentModel
from ai_banking_copilot.policy.audit import AuditLog
from ai_banking_copilot.policy.breaker import CircuitBreaker
from ai_banking_copilot.policy.pep import Pep
from ai_banking_copilot.tools.catalog import build_default_registry
from ai_banking_copilot.web.app import AppDeps, create_app


def main() -> None:
    """默认装配（模拟核心银行 + 只读事实网关 + 全量依赖）并启动 HTTP 服务。"""
    db_path = Path("runtime/copilot.db")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    writer = Store(db_path)
    init_schema(writer)
    seed_database(writer)
    writer.close()

    gateway = FactGateway(Store(db_path, StoreMode.READ_ONLY))
    registry = build_default_registry()
    breaker = CircuitBreaker()
    audit = AuditLog(db_path.parent / "audit.jsonl")
    deps = AppDeps(
        intent_model=TemplateIntentModel(),
        registry=registry,
        gateway=gateway,
        pep=Pep(registry, gateway, CoreBank(Store(db_path, StoreMode.READ_WRITE)), audit, breaker),
        breaker=breaker,
        audit=audit,
        user_id=DEMO_USER_ID,
    )
    uvicorn.run(create_app(deps), host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()

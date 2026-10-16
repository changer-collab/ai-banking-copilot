# 执行一致性实施计划

> **面向代理执行者：**使用 subagent-driven-development 逐项执行和审查；步骤用复选框记录。用户已确认 spec 与逐步实施，不再为任务间继续执行请求确认。

**目标：**修复不完整冻结、过期授权、错误幂等、恢复绕过和服务审计算法降级，并通过仓库门禁。

**架构：**保留 PDP / PEP / CoreBank 边界。在 `_run_once` 的同一 `BEGIN IMMEDIATE` 事务中执行可信校验回调、业务写与回执；确认记录绑定唯一执行 ID 与完整计划哈希。审计算法保持原实现，由服务入口叠加严格策略。

**技术栈：**Python、SQLite、FastAPI、pytest、ruff、mypy；复用现有 `.venv`，不加依赖。

## 全局约束

- 依据 `../specs/2026-10-16-execution-consistency-design.md`；用户已允许修改 `contracts/plan.py`。
- 不改其他受保护契约、审计密码学算法、执行门判据、PEP 三条安全约束和旧测试语义。
- Web 不接触 CoreBank；gateway 不导入 policy / planning；PDP 仍为纯函数；动作表仍由 catalog 派生。
- 同一文件仅一个执行者修改；冻结、审计、执行服务三组可并行，Session 集成依赖执行服务接口。
- 使用独立临时库验证；不清演示库、不重置全局熔断、不输出密钥。
- 所有 Python 命令使用 `.\.venv\Scripts\python.exe`。基线 742 项 pytest、ruff、mypy 已绿，无需重复基线。

## 文件与接口

| 交付 | 修改 / 新建文件 | 责任 |
|---|---|---|
| 冻结 | `src/ai_banking_copilot/contracts/plan.py`、`tests/contracts/test_plan.py`、必要的 gate 新测试 | 完整 DAG 摘要 |
| 受控写 | `gateway/preconditions.py`、事务事实读取模块、`gateway/core_bank.py`、`policy/execution.py`、`policy/pep.py`、`scenarios/authorization.py`、各场景写 handler、对应测试 | 最新风险与写入原子化 |
| 会话 | `web/session.py`、`web/app.py`、`web/assets/app.js`、`tests/web/` 的新增测试 | 执行身份、确认、恢复、升档、提交后失败 |
| 审计 | `policy/audit_validation.py`、`compose.py`、`scripts/migrate_audit.py`、对应测试 | 严格启动与显式迁移 |
| 收尾 | README、安全能力说明、修复台账 | 仅更新本次真实行为 |

跨任务接口固定如下；具体字段使用现有类型，不建立新动作清单：

```python
# gateway/preconditions.py：只依赖标准库与 gateway / contracts
class PreconditionStatus(StrEnum):
    ALLOW = "allow"
    REAUTHORIZE = "reauthorize"
    TAKEOVER = "takeover"
    DENY = "deny"

@dataclass(frozen=True)
class PreconditionResult:
    status: PreconditionStatus
    level: int = 0
    reasons: tuple[str, ...] = ()
    limit_cents: int = 0
    accumulated_cents: int = 0
    message: str = ""

BeforeWrite = Callable[[sqlite3.Connection], PreconditionResult]

# WritePreconditionError(GatewayError) 持有 result: PreconditionResult。
# policy/execution.py
@dataclass(frozen=True)
class ExecutionContext:
    execution_id: str
    frozen_hash: str
    user_id: str
    today: date
    approved_level: Level
    strong_verified: bool = False
    completed_records: tuple[str, ...] = ()
    flags: frozenset[str] = frozenset()

@dataclass(frozen=True)
class PlanRisk:
    decision: Decision
    node_decisions: tuple[Decision, ...]
    limit_cents: int
    accumulated_cents: int

# assess_plan(plan, *, registry, gateway, user_id, today,
#             flags=frozenset(), completed_records=(), floor=None) -> PlanRisk
# Pep.execute(call, decision, plan, idempotency_key, *, execution=None) -> ExecResult
# CommittedWriteError(GatewayError) 持有 receipt: WriteReceipt，说明提交后核验/审计失败。
```

## 任务 1：完整冻结契约

- [ ] 在 `tests/contracts/test_plan.py` 新增依赖反转、节点改名、并列顺序、等价依赖声明、重复依赖与空集合回归。例如：

```python
def test_cfhash_changes_when_dependencies_reverse() -> None:
    forward = (_node("a"), _node("b", deps=("a",)))
    reverse = (_node("a", deps=("b",)), _node("b"))
    assert cfhash(forward) != cfhash(reverse)
```

- [ ] 运行 `-m pytest tests/contracts/test_plan.py -q`，确认新断言在旧实现失败。
- [ ] `validate_plan` 拒绝重复依赖；`canonical` 加 `node_id` 与排序后的 `depends_on`；`cfhash` 先校验再计算如下完整载荷：

```python
plan = Plan(plan_id="cfhash", nodes=nodes)
validate_plan(plan)
payload = {
    "version": 2,
    "execution_order": list(topological_order(plan)),
    "nodes": [node.canonical() for node in sorted(nodes, key=lambda item: item.node_id)],
}
blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
return hashlib.sha256(blob.encode("utf-8")).hexdigest()
```

- [ ] 运行 `-m pytest tests/contracts tests/policy/test_gate.py tests/web/test_plan_card.py -q`、针对文件的 ruff；验证原并列排序断言不变。
- [ ] 审查差异，只提交该任务文件，提交信息 `fix: 冻结计划身份依赖和执行顺序`。

## 任务 2：事务内风险复核服务

- [ ] 新增 `tests/policy/test_execution_consistency.py` 和 gateway 回调事务测试；先复现旧黄卡 600 / 700、单计划累计、提交前回调拒绝零写、回调省略的旧 PEP 入口仍复核。
- [ ] 在 gateway 添加上述 precondition 类型与同连接只读事实读取能力，复用已有事实转换和限额计算；普通 FactGateway 仍拒绝读写 Store。
- [ ] 给所有写 handler 与相应 CoreBank 原语显式传递 `before_write`；`_run_once` 使用以下唯一事务顺序，原到期参数校验仍在回放前：

```python
with self._write_lock:
    with self._store.transaction() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if validate is not None:
            validate(conn)
        existing = self._existing_receipt(action, idempotency_key)
        if existing is not None:
            return existing
        if before_write is not None:
            result = before_write(conn)
            if result.status is not PreconditionStatus.ALLOW:
                raise WritePreconditionError(result)
        receipt = body(conn)
        self._store_receipt(conn, receipt)
        return receipt
```

- [ ] 实现 `ExecutionContext`、`PlanRisk`、`assess_plan` 与 PEP 回调闭包：完整计划正向流出叠加外部日累计，扣除本执行回执定位的当日已入账转账；节点 / 计划级别与原下限取 strictest；变严返回结构化重新授权结果。回调内读取熔断权威状态，不在持有业务事务时写另一连接。
- [ ] PEP 所有写路径强制构造回调。旧 API 自动构造可信兼容上下文（授权上限来自传入 Decision），不能静默关闭复核；完整上下文必须校验 frozen_hash / user_id。强验证证据只来自 Session 或 TCB 调用方。
- [ ] `_verify_receipt`、熔断写、JSONL 审计放提交后；异常包装成带 receipt 的 `CommittedWriteError`，使 Session 能如实报告与幂等恢复。
- [ ] 运行 `-m pytest tests/gateway tests/policy tests/scenarios -q`；独立连接屏障竞争测试断言最终余额、交易数与第二笔重新授权；检查 strict mypy 与 ruff。
- [ ] 对任务做 spec / 代码审查；提交 `fix: 在业务事务内复核最新执行授权`。

## 任务 3：执行 ID、确认、恢复与卡片升级

- [ ] 新增 `tests/web/test_execution_consistency.py`。先验证相同 200 元新提交确为两笔，以及确认前中断恢复零写：

```python
def test_unconfirmed_resume_does_not_write(session: Session, gateway: FactGateway) -> None:
    before = gateway.account("acc_savings").payload.balance.cents
    session.submit("转给李娜200元")
    session.interrupt()
    view = session.resume()
    assert view.state is SessionState.AWAITING_CONFIRM
    assert gateway.account("acc_savings").payload.balance.cents == before
    assert session.confirmation() is None
```

- [ ] Session 引入 RLock 与执行 ID；公开 submit / check_due 新建 ID，尚无计划的 clarify 续答保留 ID；确认、挑战绑定同执行。普通节点 key 使用 `execution_id:完整hash:node_id`，不破坏 CoreBank 的稳定到期 key。
- [ ] `_after_outcome` 使用 `assess_plan` 计算卡片与 quota。确认成功记录 approved_level / strong_verified，执行与 resume 先检查匹配确认，再向 PEP 传 `ExecutionContext`；确认前暂停恢复仅回待确认。
- [ ] 捕获 `WritePreconditionError`：按结果升级卡片、清挑战、保留完成节点并回待确认 / 接管 / 拒绝；捕获 `CommittedWriteError`：展示已提交回执并停止后续，原 key 重试不能重复写。
- [ ] HTTP 执行响应可带当前 card / quota；前端确认响应后使用新 card 渲染，沿用原确认与验证码端点。前端不添加 CoreBank 访问。
- [ ] 运行 `-m pytest tests/web -q`；补测澄清 ID、错误确认绑定、红卡挑战跨提交、已强验证中断恢复、恢复时熔断、提交后失败重试。
- [ ] 审查差异并提交 `fix: 绑定独立执行身份和恢复授权`。

## 任务 4：服务审计严格校验与离线迁移

- [ ] 新增 `tests/policy/test_audit_validation.py`、迁移脚本 / compose 的相应测试。先复现“服务接受重算 SHA 链”，原 `test_keyless_and_keyed_entries_coexist` 不动。
- [ ] 新模块入口 `verify_for_service(audit: AuditLog) -> bool`：首先 `audit.keyed and audit.verify_chain()`，然后检查逐行 alg。空链 / 全 HMAC 通过；SHA 仅可为历史前缀，首 HMAC 必须是 `audit_migration` 且 payload 精确绑定前缀条数和 head，后续只 HMAC；其他拒绝。
- [ ] `compose.build_audit_log` 改为上述严格入口，失败沿用 ChainBrokenError。不修改 `policy/audit.py` 密码学实现。
- [ ] `scripts/migrate_audit.py` 提供 path / expected-count / expected-head 参数；密钥仅环境变量读取。核对纯 SHA 旧链后先创建不覆盖的备份，再追加迁移事件；错误输入 / 混链 / 备份失败不追加；禁止自动从日志生成可信锚。
- [ ] 运行审计与 compose 测试，确认正确迁移后严格启动通过、伪迁移和错误 key 拒绝、原低层兼容测试通过；检查 ruff / mypy。
- [ ] 审查差异并提交 `fix: 禁止服务审计算法降级`。

## 任务 5：集成验证与真实能力口径

- [ ] 检查 spec 验收表逐项对应测试，特别核对独立连接并发、续执行累计不重算、出错后回执恢复及旧 PEP 接口；修复发现的问题，不放宽旧断言。
- [ ] 更新 README、安全说明和修复台账，仅陈述已通过测试的行为；保留逐节点事务、内存会话、演示验证码与同目录密钥限制。
- [ ] 依次运行 `-m ruff check .`、`-m mypy`、`-m pytest`。预期 ruff 全绿、mypy 无问题、pytest 全量通过。
- [ ] 在服务端设置 `$env:AIB_DISABLE_LLM='1'`，用独立临时数据库启动服务；运行 `scripts/check_served_page.py`、`scripts/e2e_check.py`、`scripts/check_design_sync.py`，预期 ALL OK / ALL PASSED / 无设计漂移。结束本次测试服务。
- [ ] 独立代码审查整个分支，解决重要问题；记录测试数量与残留，提交最终变更。完成后向用户给出 spec / 计划与主要实现链接和简短验证结果。

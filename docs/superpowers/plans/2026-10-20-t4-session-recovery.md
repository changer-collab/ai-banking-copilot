# T4 会话恢复实施计划

状态：2026-10-20 已本地验收；能力与残留见 [README](../../../README.md#强验证与会话恢复)。

> 执行：使用 superpowers:subagent-driven-development；独立存储与对账模块并行，组长集成 Session。

**目标**：重启可恢复冻结计划、检测提交审计缺口并防重复扣款。

**架构**：独立 SQLite 快照 + 节点检查点 + receipt/audit 对账；单进程单 worker、全局 RLock。

**技术栈**：Python、SQLite、JSON、现有 AuditLog/FactGateway/PEP、pytest。

## 文件归属

T4 子代理新增 `web/session_snapshot.py`、`web/session_codec.py`、`web/session_repository.py`、`web/session_manager.py`、`policy/session_recovery.py`、`gateway/receipt_store.py` 及 `tests/web/test_session_codec.py`、`tests/web/test_session_persistence.py`、`tests/policy/test_session_reconciliation.py`。组长独占 Session、app、compose、IM 及 `tests/web/test_restart_integration.py`。冻结 schema/契约/算法不动；保留既有工作树，验收后用户已授权通过 PR 合并。

## 任务 1：快照和 Repository

- [x] 先写 SQLite roundtrip、CAS 冲突、身份不符、未知版本测试；执行 `.venv\Scripts\python.exe -m pytest tests/web/test_session_persistence.py -q` 确认失败。
- [x] SessionSnapshot 导出 version/user_id/session_id/revision/execution_id/state/payload；独立 SESSION_SCHEMA_SQL，Store 事务保存，主键用户+会话。
- [x] Repository.load 与 save(expected_revision) 返回不可变新 revision，冲突抛 SessionPersistenceError，禁止隐式覆盖。

接口：

```python
class SessionRepository(Protocol):
    def load(self, user_id: str, session_id: str) -> SessionSnapshot | None: ...
    def save(self, snapshot: SessionSnapshot,
             *, expected_revision: int | None) -> SessionSnapshot: ...
```

## 任务 2：显式 Codec

- [x] 先写 Plan/Value/Slot/枚举/时间/Session 私有 dataclass roundtrip 和未知类型、坏 trust、坏哈希、完成节点损坏拒绝测试。
- [x] 提供 encode_value/decode_value 及 encode_snapshot/decode_snapshot；显式白名单构造器、枚举和类型标签，Session 私有类型可在函数内显式 import 避免循环依赖，禁止动态加载任意类。
- [x] T3 ChallengeRecord 只以摘要字段保存；Money 保留整数分，时间 ISO，tuple/list 明确区分。恢复计划校验 validate_plan/cfhash，不把 canonical_json 当通用可逆 codec。
- [x] 运行 `.venv\Scripts\python.exe -m pytest tests/web/test_session_codec.py -q`，预期全部通过。

## 任务 3：回执与审计对账

- [x] gateway/receipt_store.py 只读 load business_key，policy/session_recovery.py 提供 InFlightNode 及 reconcile：先 verify_chain，再按 request_key+action+record_ids+manifest hash 匹配。
- [x] 为无/无、有/无、有/有、无/有、坏链、错绑定分别写测试；业务 receipt action 不与规范 action 直接比，cfhash 不与 manifest hash 混比。
- [x] 业务键解析复用 due_execution_key：schedule_id 的 transfer、occasion_id 的 place_merchant_order；其余使用请求键，分别加测试。
- [x] 无审计回执返回 committed_unverified，证据冲突返回人工；只做读取/检测，不自动补成功事件或执行 PEP。
- [x] 运行 `.venv\Scripts\python.exe -m pytest tests/policy/test_session_reconciliation.py -q`，预期全部通过。

## 任务 4：Manager 与集成接口

- [x] 提供 SessionManager 获取唯一实例和共享 RLock，支持可注入 factory/restore 回调，不能在模块顶层导入 app 造成循环。
- [x] 子代理交付全部独立模块和接口，完成 spec review 再 quality review。
- [x] 组长为 Session 增加 export/restore/checkpoint；_serialized 使用共享锁并在成功和异常路径持久化；每节点 PEP 前后 checkpoint，失败停后续写。
- [x] 组长保存字段清单：execution/state/message/pending/confirmation/takeover/audit_ids/exec_results/completed/pending节点/challenge/unverified/quota/risky_at/policy_rejections/session_breaker/reversed_records/last_utterance/last_selected/review_intents/clarification_keys/review_report/in_flight。
- [x] restore 清除未完成任务有效确认、active 挑战，重建熔断实例；已完成结果保留；旧严格证据不满足当前配置则人工。
- [x] compose 初始化独立表与连接，AppDeps 注入 repository/reconciler/共享锁，app 使用 manager，新增只读 GET /api/session。

## 任务 5：重启和故障注入

- [x] `tests/web/test_restart_integration.py` 使用临时库重新 build_deps/create_app：同卡/同 execution、未确认不执行、红码重启失效、已完成不重复、部分节点继续、已提交未审计保持待核对。
- [x] 注入 PEP 前、业务提交后审计前、审计后快照前和 snapshot save 异常；同会话首次并发确认与跨会话审计链测试均保留资金断言。
- [x] 运行 `.venv\Scripts\python.exe -m pytest tests/web/test_restart_integration.py tests/web/test_session_recovery.py tests/web/test_execution_consistency.py -q`，预期全部通过。
- [x] 统一 ruff/mypy/pytest 与服务三门禁；台账明确单 worker、旧授权重启失效、审计缺口可检测但无自动投递的残留。

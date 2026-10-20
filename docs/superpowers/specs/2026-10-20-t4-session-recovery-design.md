# T4 会话持久化与恢复设计

状态：2026-10-20 用户已批准并已本地验收；范围为单进程、单 worker、跨重启恢复。

## 方案与边界

采用版本化 SQLite 快照、节点执行前检查点、既有 write_receipts 与已验证 JSONL 审计对账。相较真正事务 outbox，该方案不改 CoreBank 事务和冻结 schema，能检测崩溃缺口；缺失审计仍可能人工核对。全量事件溯源超出本轮范围。

新增独立 DDL，不改 `gateway/store.py::SCHEMA_SQL`、contracts、审计 hash/signature、gate 判据或 PEP 硬约束。web 不直接调用 CoreBank；回执只读读取放 gateway，审计对账放 policy。

## 数据和接口

`SessionSnapshot(version, user_id, session_id, revision, execution_id, state, payload)` 为外壳；Repository 提供 load 与 expected_revision CAS save。坏版本/身份/哈希/结构显式拒绝，保留原记录，不能回退新会话。

payload 保存计划及原节点顺序、卡片/裁决、确认历史、挑战元数据、已完成节点与结果、unverified_results、in_flight、最后输入/澄清、Reviewer 证据、quota、审计引用、风险时间窗/连续拒绝/会话熔断、接管与回退记录。锁、连接、客户端和 Saga callable 重建。

Codec 采用显式类型白名单 JSON，手动注册 Plan/PlanNode/IntentRef/Decision/Value/Slot 等构造器；不使用 pickle、动态 import 或 __dict__.update。恢复调用 validate_plan 与 cfhash，并检查完成集合和依赖闭合；Trust/SlotKind 保持原值，不能升级可信度。

SessionManager 负责按用户/会话唯一加载；全进程共享 RLock 包住每次会话变更与持久化，避免首次创建竞态及不同会话审计链分叉。CAS 仅避免旧快照覆盖，不声称支持多 worker 执行锁。

## 检查点与对账

每个 PEP 节点执行前先保存 in_flight(node_id, request_key, business_key, action, manifest_hash)；保存失败不得开始业务。PEP 返回后立即保存结果、完成集合和审计引用，才进入下一节点。执行后持久化失败必须阻止后续写入并报告待核对。

通常两键相同；预约转账与商户待办复用 due_execution_key 作为 business_key。审计以 request_key + 规范动作 + record_ids + 调用 manifest hash 匹配；receipt 按 business_key 查，不将核心原语 action 与规范动作直接比较，也不把单调用 manifest_hash 当计划 cfhash。

恢复必须先 verify_chain，再读事件：无回执/无审计为未提交；有回执/无可信成功审计为 committed_unverified；两者绑定一致可恢复完成节点；无回执却有写成功或证据不符转人工。不能伪造原执行时刻的成功审计，恢复日志只能明确写恢复核对。

## 恢复授权

恢复保留 execution_id、卡片和冻结计划；读取状态绝不自动执行。未完成任务的旧确认只作历史，需重新显式确认；红卡重新申请挑战，旧 active 挑战失效。已完成/回退维持终态，重复请求只返回原结果。进程内原授权的幂等恢复不改变。

会话熔断保持只加严；全局熔断和最新事实仍经 PEP 执行前检查。当前严格 Reviewer 要求高于旧证据时，转人工或重新审查。committed_unverified 跨重启保持，核对前禁止回退。

提供 `GET /api/session?session_id=...` 只读恢复视图，不返回秘密或 snapshot 内部字段；Web/IM 共享内核，为后续 T5/T6 留入口，但本轮不实现移动端登录和平台接入。

## 分工和验收

T4 子代理交付 session_snapshot、session_codec、session_repository、session_manager、policy/session_recovery、gateway/receipt_store 和独立单测；组长只改 Session/app/compose/IM 接线和联合恢复测试。

故障注入至少覆盖：PEP 前、业务提交后审计前、审计后快照前、保存失败；并覆盖单会话并发确认、跨会话审计、重启挑战/旧确认、部分节点恢复、两种 due 键、坏链/坏版本/身份/哈希/taint。最终通过仓库全部门禁。

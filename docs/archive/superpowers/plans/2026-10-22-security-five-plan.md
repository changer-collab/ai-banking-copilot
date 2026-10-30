# 官方五项安全机制实施计划

执行方式：subagent-driven-development。目标：按规格补齐五项，取得入口与负例证据后合并。

- [x] 甲：账单事实叙事。独占 scenarios/bill_findings.py、web/bill_narrative.py、tests/scenarios/test_bill_findings.py 与新叙事测试。用结构化选择和服务端渲染替代自由文本；37 项相关测试通过，指定文件 Ruff 全绿。
- [x] 乙：授权与锁定。授权从可信 grant 重建全部风险上下文；审批等级不能高于裁决，红色写授权必须有绑定当前计划的强验证 proof；SQLite 原子计数由组合根注入独立 RW store。补拒绝审计、写前审计与失败封锁；PEP 沙箱查询错误稳定转换 QueryResult。
- [x] 丙：隔离执行。实现受限分析 DSL、来源链传递、父子字节预算、资源约束、无 I/O 能力和监控事件；年度账单入口安全降级，覆盖超时、隔离不可用、重复出处及超长输入。
- [x] 主代理：统一审计时间与追加落盘，确认拒绝和解锁留痕；装配持久限流；严格 Reviewer 自由文本不进入 Web 响应；执行完成事件增加 `plan_hash` 并保留节点 `frozen_hash` 语义。
- [x] 独立复核：发现全局连续授权失败熔断缺少显式 `breaker` 事件；PEP / Hook / 强验证路径均按 `closed -> open` 状态边沿审计，并覆盖解锁后的再触发。
- [x] 验收：最终代码下 Ruff、严格 mypy、全量 pytest、确定性服务页检查、E2E 与设计同步检查均通过；原 `runtime/audit.jsonl` 混合 SHA/HMAC 链保留，未迁移；服务门禁在独立 cwd 的新演示库运行。
- [ ] 交付：精确暂存文件，排除竞赛冲刺 Markdown / 临时运行产物；提交、创建 PR（写明残留：第二信道未联调、非容器隔离、旧审计链未迁移、无业务审计 outbox 与并发熔断审计风险）、检查后合并。只有五项均有证据才完成持续目标。

## 最终门禁修正

- [x] 回归：`tests/eval/test_web_benchmark.py::test_yearly_totals_and_percentage_have_independent_source_proofs` 修复前稳定失败；修复后验证 `+17.65%`、18 条来源与哈希。
- [x] 注入：`compose.build_deps` 将同一 `ModelConfig` 绑定到分析代码生成回调，经 `Pep` 的 `QueryContext` 传给账单场景；直接场景测试保留默认生成路径，生产请求不依赖隐式全局环境。
- [x] 验证：最终代码下 Ruff、严格 mypy、全量 pytest（通过，1 条既有 Starlette 弃用警告）、服务页（ALL OK）、E2E（ALL PASSED）与设计同步（ALL OK；GLOSS mock 键集警告）均通过。
- [x] 全局熔断审计：PEP、Pre-Tool Hook 与强验证失败均在每次 `closed -> open` 写入关联身份的 `breaker` 事件，覆盖重复拒绝及解锁后再次触发。
- [ ] 发布：检查 `git diff --check`，精确暂存实现/spec/plan 与测试，排除 `.tmp-*`、`runtime/` 和竞赛冲刺 Markdown；提交、推送、创建 PR，CI 全绿后合并。

# T2 模型治理实施计划

状态：2026-10-20 已本地验收；能力与残留见 [README](../../../README.md#强验证与会话恢复)。

> 执行：使用 superpowers:subagent-driven-development；T2/T3/T4 按用户要求并行，独占文件，组长串行集成共享面。

**目标**：统一模型治理并修复严格 Reviewer 失败关闭缺口。

**架构**：不可变配置 + 既有传输 seams + 安全观测；保留模板回归，严格结果独立验收。

**技术栈**：Python、httpx、threading/contextvars、pytest、现有 FastAPI。

## 约束与文件

实施期间保留既有工作树；未另建 worktree。验收后用户已授权通过 PR 合并。T2 独占 `planning/model_config.py`、`planning/llm.py`、`planning/reviewer.py`、`planning/call_observation.py`、`planning/transport_probe.py`、`eval/web_benchmark.py`、`scripts/diagnose_llm_transport.py`、`scripts/run_benchmark.py` 及对应 tests。compose、session、app、前端、tests/test_compose.py 和 tests/web 由组长改。

## 任务 1：配置与预算

- [x] 在 `tests/planning/test_model_config.py` 写配置优先级、20/180 默认、disable 优先、无效 timeout/并发/尝试数拒绝、密钥不在 repr 的失败测试；用 `.venv\Scripts\python.exe -m pytest tests/planning/test_model_config.py -q` 确认先失败。
- [x] 新建 `model_config.py`，导出 `ModelConfig.from_env(env)`、role timeout、public metadata 和 `NonBlockingLimiter`。凭据继续 AI/DEEPSEEK/DASHSCOPE 优先，调用固定一次；非阻塞 acquire 失败为 concurrency_limit。
- [x] 角色配置传给现有 LlmIntentModel 与 Reviewer factory，不重写 HTTP 协议；Reviewer 外层保持 asyncio 总截止。上述测试转绿。

配置测试核心：

```python
cfg = ModelConfig.from_env({"AI_API_KEY": "secret", "AI_MODEL": "model"})
assert cfg.planner_timeout_s == 20
assert cfg.reviewer_timeout_s == 180
assert "secret" not in repr(cfg)
```

## 任务 2：严格结果、usage 和错误安全

- [x] 为 `tests/planning/test_reviewer.py` 增加缺 findings/clarifications 不得 allow、模型内 usage 不覆盖信封、错误字段与绑定失败测试；`test_llm_real.py` 添加异常字符串含秘密但公开错误不含的测试；分别运行确认失败。
- [x] 收紧 Reviewer JSON 必填结构，provider usage 采用非负整数白名单，忽略模型生成 usage；Planner 异常仅暴露稳定分类，保留现有动作/金额/出处校验。
- [x] 在 call_observation 增加 ContextVar 任务关联与安全汇总，保留旧 CallObservation 构造兼容默认值。记录实际 HTTP 才增加 HTTP count；fake transport 不虚报。
- [x] 给 Planner 真实/模板回退提供实际通道 metadata，prompt_version 可追踪；不得通过共享可变全局把不同任务串起来。
- [x] 运行 `.venv\Scripts\python.exe -m pytest tests/planning/test_llm_real.py tests/planning/test_reviewer.py tests/planning/test_call_observation.py -q`，预期全部通过。

严格放行集成判据交组长：

```python
valid = (report.status is ReviewStatus.ALLOW
         and not report.diagnostic_only and report.failure_code is None
         and report.execution_id == execution_id and report.cfhash == frozen_hash
         and report.prompt_version == expected_prompt_version)
```

## 任务 3：诊断评测共用配置

- [x] 先补脚本/eval 配置和脱敏测试，明确 env 来源，不打印密钥或原始异常。
- [x] 诊断/评测复用配置解析、usage 白名单和实际 timeout，保留诊断有限两次与产品一次的区别，既有数据集/评分语义不改。
- [x] 运行 `.venv\Scripts\python.exe -m pytest tests/planning tests/scripts/test_diagnose_llm_transport.py tests/eval/test_web_benchmark.py -q`，记录结果与接口，通知组长。
- [x] 独立 spec review 通过后再 code-quality review；修复审查发现，再交组长接线。不得以自审代替评审。

## 任务 4：组长集成与最终门禁

- [x] compose 使用统一配置；显式 llm-strict 必须 required，disable 保持 bypass；Session 的 required+无 Reviewer、诊断 allow、错绑定全部转人工。
- [x] API 返回任务通道/安全调用汇总，前端实际显示；在 `tests/web/test_reviewer_governance.py` 验证无资金动作和元数据。
- [x] 所有子任务集成后运行 ruff/mypy/全 pytest 以及确定性服务三门禁；真实模型另用新目录复验，证据不混同模板。更新台账实际状态和残留。

# T2 模型治理设计

状态：2026-10-20 用户已批准设计；已实现并通过本地门禁和真实模型复验，范围与版本见任务台账。

## 目标与范围

统一模型配置、调用预算、观测和严格审查接线，保留既有模板能力与执行门。T1 负责人为组长；本次源码变化后重新验证真实通道，不沿用旧成绩证明新代码。

推荐单一不可变配置对象与现有 transport seams。逐入口增加配置会继续漂移；重建 provider/plugin 框架超出范围。

## 配置与调用

新增 `planning/model_config.py`，集中保留现有凭据优先级、endpoint/model 别名；密钥字段不进入 repr 或公开配置。进程环境优先，组件不偷偷加载或覆盖 `.env`。诊断、评测和 compose 复用解析器。

角色默认超时 Planner 20 秒、Reviewer 180 秒；允许 `AI_TIMEOUT_S` 总默认和角色覆盖。产品每角色每次提案固定一次尝试、零自动重试，配置只允许这一有限预算；并发使用共享非阻塞信号量，满额返回安全失败分类。Reviewer 保留真正取消异步 HTTP 的总截止。

`AIB_DISABLE_LLM=1` 继续强制模板和 Reviewer bypass。保留旧默认旁路兼容；显式 `llm-strict` 通道必须 Reviewer required，不允许模板替代严格审查。真实 Planner 失败回退模板时明确给出 `template_fallback`，不计作真实成功。

## 严格审查

写操作放行必须同时满足：Reviewer 存在、报告 `allow`、非 diagnostic、无 failure、execution_id/cfhash/prompt_version 匹配、必要 JSON 字段齐备。缺失、超时、坏结构、伪 allow、错误绑定均转人工；只读查询仍可展示诊断。既有三色策略、事实来源、金额校验和 PEP 不放宽。

## 观测与脱敏

HTTP 观测仅记录角色、任务关联、尝试序号、通道、模型、prompt 版本、状态码、耗时、错误分类和白名单非负整数 usage。usage 只来自 HTTP 信封，模型内容不可覆写。任务视图汇总实际 HTTP 次数与 usage；注入 fake transport 不虚报 HTTP。

异常文本不进入页面、审计或 benchmark failures；仅稳定错误码和安全提示。不得保存 API key、Authorization、原始响应、reasoning_content 或用户秘密。UI 显示实际 Planner/Reviewer 通道，模板与严格结果有明显区别。

## 分工与验收

T2 子代理独占 planning/model_config、llm、reviewer、call_observation、transport_probe、eval/web_benchmark、两个诊断/评测脚本及对应单测。组长独占 compose、Session、AppDeps、API、前端及集成测试。

新增失败测试覆盖配置、预算/并发、坏 JSON/缺字段、非法动作/金额、错误脱敏、伪造 usage、无 Reviewer 和诊断 allow。既有测试语义保持。最终全量 ruff/mypy/pytest 与确定性服务门禁通过；真实模型使用独立模拟库、有限调用和新证据目录另验。

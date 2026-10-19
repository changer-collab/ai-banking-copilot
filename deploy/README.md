# 部署说明 · AI 银行副驾

> 目标：**一键复现**——外部审阅者可在标准环境中跑通核心任务流程与输出。

## 前置要求

| 组件 | 版本 |
|---|---|
| Python | ≥ 3.11 |
| Docker Desktop | ≥ 4.30 |
| Docker Compose | v2（随 Docker Desktop 提供） |

## 一键启动

```bash
git clone https://github.com/changer-collab/ai-banking-copilot.git
cd ai-banking-copilot
python -m pip install -e ".[dev]"   # 本地开发依赖（pytest / ruff / mypy）
cp .env.example .env                # 填入大模型 API Key（可留空，走降级通道）
pytest                              # 端到端质量门：单元 + 集成 + 评测套件
docker compose up -d                # 启动后端 + 模拟核心银行
```

> 无网络时走**降级通道**（模板 + 规则）；评测套件与 held-out **全程离线**、零外网依赖。

## 服务与端口

| 服务 | 说明 | 端口 |
|---|---|---|
| `api` | FastAPI，同时提供 Web 页面与静态资源 | 8000 |

模拟核心银行与事实网关在 API 进程内装配，不作为独立 Compose 服务对外开放。

## 复现核心任务流程

```bash
pytest tests/gateway tests/tools tests/policy tests/planning tests/scenarios  # 分层单元
pytest tests/eval                                                             # 评测套件
python -m pytest tests/eval/test_eval_integration.py -v                       # 端到端评测链
```

评测套件包含：**62 条六场景标准指令 + 19 条对抗样例（提示注入 / 越权超标 / 幻觉诱发 / 诱导承诺收益）
+ 20 条 held-out**；判据为序关系不变式（`I1′`–`I4′` + 绿色纯度 + 非退化 + must-pass 外部锚）。
指标：派生数字放行率 ≥ 95%、新用户首日误拦率 = 0、安全开销（额外确认次数 / 耗时）。

## 说明

- 本项目**不接真实银行账户、不产生真实资金动作**，全部在**模拟核心银行**中执行。
- 大模型可切换（Qwen / DeepSeek / 本地开源）；无网络时走降级通道（模板 + 规则）。
- 环境变量见 `.env.example`；**任何密钥不得提交进仓库**。

---

*2026-10-01 建立；随实施计划逐步落地。*

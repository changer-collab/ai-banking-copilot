# 部署说明 · AI 银行副驾

> 目标：**一键复现**——评委可在标准环境中跑通核心任务流程与输出。

## 前置要求

| 组件 | 版本 |
|---|---|
| Docker Desktop | ≥ 4.30 |
| Docker Compose | v2（随 Docker Desktop 提供） |

## 一键启动

```bash
git clone https://github.com/changer-collab/ai-banking-copilot.git
cd ai-banking-copilot
cp .env.example .env      # 填入大模型 API Key
docker compose up -d      # 启动后端 + 模拟核心银行
```

> `docker-compose.yml` 在实施计划 `01-fact-gateway` 中落地（届时才有可起的服务）。

## 服务与端口

| 服务 | 说明 | 端口 |
|---|---|---|
| `api` | 后端（FastAPI） | 8000 |
| `corebank` | 模拟核心银行 + 事实网关（TCB） | 库内，不对外暴露 |
| `web` | 前端界面 | 5173 |

## 复现核心任务流程

```bash
pytest tests/integration     # 端到端
pytest tests/adversarial     # 对抗样例库（提示注入 / 越权 / 幻觉诱发 / 诱导承诺收益）
```

## 说明

- 本项目**不接真实银行账户、不产生真实资金动作**，全部在**模拟核心银行**中执行。
- 大模型可切换（Qwen / DeepSeek / 本地开源）；无网络时走降级通道（模板 + 规则）。
- 环境变量见 `.env.example`；**任何密钥不得提交进仓库**。

---

*2026-10-01 建立；随实施计划逐步落地。*

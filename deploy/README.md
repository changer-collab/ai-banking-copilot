# 部署说明 · AI 银行副驾

> 目标：**一键复现**——外部审阅者可在标准环境中跑通核心任务流程与输出；同时记录本项目**当前线上环境**。

---

## 一、当前线上环境（华为云 ECS + Cloudflare Tunnel）

| 项 | 值 |
|---|---|
| 云服务器 | **华为云 ECS**，Ubuntu 22.04 / x86_64 / Docker 29.3.1，登录 `aistock@121.37.46.229` |
| 代码位置 | `/home/aistock/aib`（**无 `.git`**，由本机 `git archive` 打包上传；更新流程见部署单 §11） |
| 应用 | Compose 项目 `aib` → 容器 `aib-api-1`，容器内 8000，**宿主 18080**，`restart: unless-stopped` |
| 公网入口 | **Cloudflare Tunnel** → 容器 `aib-tunnel`（`cloudflare/cloudflared`，`--network host`），`https://<当日隧道域名>` |
| 域名 | **暂无**。用 quick tunnel 临时 https 域名，**免域名 / 免备案 / 免开端口** |
| 数据 | `/home/aistock/aib/runtime/`（挂载进容器；会话 / 计划卡 / 审计链，容器重建不丢） |
| 公网检查 | `/`、`/m`、`/api/channels/health` 均 200；`/api/im/feishu` 在 M2 完成前如实 501 |

启动命令（服务器上，**不要动别人的容器**）：

```bash
cd /home/aistock/aib
API_HOST_PORT=18080 \
PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
docker compose -p aib up -d --build

# 隧道（只跑一次；重启会换地址）
docker run -d --name aib-tunnel --restart unless-stopped --network host \
  cloudflare/cloudflared:latest tunnel --url http://127.0.0.1:18080 --no-autoupdate
docker logs aib-tunnel 2>&1 | grep trycloudflare
```

**这台机器是共用的**：还跑着 `expert-managementsystem-*`、`komari`、`qq_feishu_bot`、`qq_napcat` 等别人的服务，只操作 `aib-*`，不要 `docker compose down` 全局、不要占 80/443。

> 每日部署清单、渠道（飞书）对接步骤、磁盘清理与排障，全部在
> [docs/2026-10-27/公网Demo-部署单.md](../docs/2026-10-27/公网Demo-部署单.md)。

---

## 二、本地一键复现

### 前置要求

| 组件 | 版本 |
|---|---|
| Python | ≥ 3.11 |
| Docker Desktop | ≥ 4.30 |
| Docker Compose | v2（随 Docker Desktop 提供） |

### 步骤

```bash
git clone https://github.com/changer-collab/ai-banking-copilot.git
cd ai-banking-copilot
python -m pip install -e ".[dev]"   # 本地开发依赖（pytest / ruff / mypy）
cp .env.example .env                # 填入大模型 API Key（可留空，走降级通道）
pytest                              # 端到端质量门：单元 + 集成 + 评测套件
docker compose up -d                # 启动后端（默认宿主 8000）
```

> 无网络时走**降级通道**（模板 + 规则）；评测套件与 held-out **全程离线**、零外网依赖。

### Compose 可调项（与线上一致的开关）

| 变量 | 作用 | 默认 |
|---|---|---|
| `API_HOST_PORT` | 宿主端口（容器内始终 8000）；8000 被占时改它 | `8000` |
| `PIP_INDEX_URL` | 构建时的 pip 源；国内环境用清华源 | `https://pypi.org/simple` |
| `DEEPSEEK_API_KEY` / `AI_BASE_URL` / `AI_MODEL` | 大模型通道（从 `.env` 注入） | 空 / DeepSeek 端点 / `deepseek-flash` |

> `.env` 由 compose 自动读取做变量替换；**缺 `DEEPSEEK_API_KEY` 会静默降级成模板卡**（看日志是不是 `fallback`）。
> `./runtime` 已挂成数据卷，容器重建不丢数据。

### 服务与端口

| 服务 | 说明 | 端口 |
|---|---|---|
| `api` | FastAPI，同时提供 Web 页面与静态资源 | 容器 8000 / 宿主 `API_HOST_PORT` |

模拟核心银行与事实网关在 API 进程内装配，不作为独立 Compose 服务对外开放。

---

## 三、复现核心任务流程

```bash
pytest tests/gateway tests/tools tests/policy tests/planning tests/scenarios  # 分层单元
pytest tests/eval                                                             # 评测套件
python -m pytest tests/eval/test_eval_integration.py -v                       # 端到端评测链
```

评测套件包含：**62 条六场景标准指令 + 19 条对抗样例（提示注入 / 越权超标 / 幻觉诱发 / 诱导承诺收益）
+ 20 条 held-out**；判据为序关系不变式（`I1′`–`I4′` + 绿色纯度 + 非退化 + must-pass 外部锚）。
指标：派生数字放行率 ≥ 95%、新用户首日误拦率 = 0、安全开销（额外确认次数 / 耗时）。

---

## 四、说明

- 本项目**不接真实银行账户、不产生真实资金动作**，全部在**模拟核心银行**中执行。
- 大模型可切换（Qwen / DeepSeek / 本地开源）；无网络时走降级通道（模板 + 规则）。
- 环境变量见 `.env.example`；**任何密钥不得提交进仓库**，也不写进文档 / 群聊 / 截图。

---

*2026-10-01 建立；2026-10-27 补「华为云 ECS + Cloudflare Tunnel」线上环境。*

# 公网 Demo 部署单（无域名方案）· 2026-10-27

> **一句话**：没有域名也能上公网——用 **Cloudflare Tunnel（cloudflared）** 从服务器（或本机）拉一条出站隧道，10 分钟拿到 `https://xxx.trycloudflare.com`，飞书回调、手机 4G 访问都能用，**不用买域名、不用备案、不用开端口**。
> 本文写给 **尹辰**（部署与公网入口归他）执行；王昌泽只负责飞书应用侧的配置与回放验证（见 §7）。

---

## 1. 现状与结论

| 问题 | 结论 |
|---|---|
| 项目在哪 | GitHub `changer-collab/ai-banking-copilot`（`sprint/1026`）是唯一真源；本地只是 clone |
| 有没有部署 | **没有**。云服务器还没放过任何东西 |
| 有没有域名 | **没有** |
| 影响交付吗 | **不影响**。代码侧任务照做；只有「公网可访问 / 飞书真回调」需要这条隧道 |
| 推荐方案 | **cloudflared quick tunnel**（免域名、免备案、免开端口） |

### 三条路对比

| 方案 | 需要域名 | 成本 | 飞书能接 | 备案 | 稳定性 |
|---|---|---|---|---|---|
| A `http://<IP>:8000` | 否 | 0 | ❌ 飞书只收 https | — | 高，但要开端口 |
| B `<IP 用有点>.sslip.io` + Caddy | 否（`1-2-3-4.sslip.io`） | 0 | ✅ | 境内服务器未备案可能被拦 80/443 | 高 |
| **C cloudflared 隧道** | **否** | **0** | **✅** | **不受影响** | 中（quick tunnel 的 URL 每次重启会变） |

> 结论：**先用 C 把链路跑通**（现在就能做），答辩当天如需固定地址，再在服务器上配 B 或用自己的 Cloudflare 账号跑**命名隧道**（需要域名托管在 Cloudflare）。

---

## 2. 服务器部署（Docker，推荐）

```bash
# 0）装依赖
sudo apt update && sudo apt install -y docker.io docker-compose-v2 git

# 1）拉代码（只认 sprint/1026）
git clone https://github.com/changer-collab/ai-banking-copilot.git
cd ai-banking-copilot
git checkout sprint/1026

# 2）配密钥（只写变量名，不写值到文档/群里）
cp .env.example .env
vim .env      # 填 DEEPSEEK_API_KEY / AI_BASE_URL / AI_MODEL

# 3）起服务
sudo docker compose up -d --build
sudo docker compose logs -f --tail=50      # 日志里通道应是 llm，不是 fallback

# 4）本机自检
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/        # 200
curl -s http://127.0.0.1:8000/api/channels/health                      # 三渠道状态
```

> `docker-compose.yml` 已把 `.env` 里的 key 注入容器（缺 `DEEPSEEK_API_KEY` 会**静默降级成模板卡**，日志会出现 `fallback`）；
> `./runtime` 已挂成数据卷，**会话 / 计划卡 / 审计链都在里面**，容器重建不丢数据。

### 不用 Docker 的裸跑方式（应急）

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e .
python -m ai_banking_copilot        # 监听 0.0.0.0:8000，自动读仓库根的 .env
```

用 systemd 守护（可选）：

```ini
# /etc/systemd/system/aib.service
[Unit]
Description=AI Banking Copilot
After=network.target

[Service]
WorkingDirectory=/opt/ai-banking-copilot
ExecStart=/opt/ai-banking-copilot/.venv/bin/python -m ai_banking_copilot
Restart=always

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now aib
```

---

## 3. cloudflared 隧道（拿 https 的关键一步）

### 3.1 服务器上装（Linux）

```bash
curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 \
  -o /usr/local/bin/cloudflared
chmod +x /usr/local/bin/cloudflared
cloudflared --version
```

### 3.2 起隧道

```bash
cloudflared tunnel --url http://127.0.0.1:8000
```

输出里会有一行：

```
Your quick Tunnel has been created! Visit it at:
https://random-words-1234.trycloudflare.com
```

**这个 https 地址就是你的公网入口**，直接当域名用：

| 用途 | 地址 |
|---|---|
| 网页端 | `https://<隧道域名>/` |
| APP 端（窄屏 PWA） | `https://<隧道域名>/m` |
| 本地联调页（不算产品页） | `https://<隧道域名>/im` |
| 渠道健康 | `https://<隧道域名>/api/channels/health` |
| 飞书回调（M2 完成后生效） | `https://<隧道域名>/api/im/feishu` |

### 3.3 Windows 本机（没服务器时先跑通）

```powershell
Invoke-WebRequest -Uri "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe" -OutFile cloudflared.exe
.\cloudflared.exe tunnel --url http://127.0.0.1:8000
```

> 本机方式适合**今天就把飞书回调 / 手机 4G 打开这条链路验证掉**；缺点是关电脑就断。

---

## 4. 验收清单（照这个点一遍）

- [ ] `https://<隧道域名>/` 手机 4G 打开，首页 3 秒内可操作
- [ ] `https://<隧道域名>/m` 是窄屏 App 观感，四个 Tab 都在
- [ ] `https://<隧道域名>/im` 断网也能打开（零外链），能走完一次「文本进 → 计划卡 → 确认 → 回执」
- [ ] `https://<隧道域名>/api/channels/health` 返回三渠道状态，`feishu` 未配置时如实写 `not_configured`
- [ ] `POST https://<隧道域名>/api/im/feishu` 在 M2 完成前返回 **501 + `feishu_adapter_not_integrated`**（如实，不是假成功）
- [ ] 日志通道是 `llm`；拔掉 key 后自动是 `fallback`（两种都验一次）
- [ ] `runtime/copilot.db` 在重建容器后仍在（数据卷生效）

---

## 5. 备案与端口（别踩）

- **cloudflared 走的是出站连接**，不占 80/443、不需要备案、不需要在云厂商安全组开端口 → 境内服务器也能用。
- 只有「用 IP + 端口直连」才需要开安全组；若用 80/443 直连且服务器在境内，**未备案会被拦**。
- 隧道域名是 Cloudflare 的临时域名，**不指向你的服务器 IP**，也不对外暴露服务器。

---

## 6. 凭据与安全

- 密钥只写在服务器的 `.env`（已被 gitignore），**文档、群里、截图里都不写值**，只写变量名。
- 隧道地址（`trycloudflare.com`）等于「谁拿到谁能访问」，**只在群里发一次**，不要写进公开文档 / PPT。
- 本项目不接真实银行、不产生真实资金动作，公网 Demo 也是模拟核心银行。

---

## 7. 飞书接入（M2 完成后再做）

1. 飞书开放平台建应用 → 开「机器人」能力 → 拿 `APP_ID / APP_SECRET / VERIFICATION_TOKEN / ENCRYPT_KEY`，填进服务器 `.env`（变量名见 §8）；
2. 事件订阅 → 请求地址填 `https://<隧道域名>/api/im/feishu`；
3. 订阅事件 `im.message.receive_v1`；
4. 回归：同一条 `event_id` 打两次只执行一次；坏签名返回 401；超频返回 429；
5. 详细步骤见王昌泽那份 `飞书接入-配置单.md`（C4）。

> 现在 `/api/im/feishu` 是 501 占位（M2 未完成），**在 M2 落地前不要把这个地址填进飞书**，否则飞书后台会一直报「请求地址校验失败」。

---

## 8. 环境变量一览（只列名字，值写在 `.env`）

| 变量 | 用途 | 缺了会怎样 |
|---|---|---|
| `DEEPSEEK_API_KEY` | 大模型密钥 | 静默降级为模板卡（看起来「AI 变笨」） |
| `DASHSCOPE_API_KEY` | 备用模型密钥（可选） | 不影响 DeepSeek 通道 |
| `AI_BASE_URL` | 模型端点 | 用默认 DeepSeek 端点 |
| `AI_MODEL` | 模型名 | 用默认模型名 |
| `AIB_DISABLE_LLM` | 设 `1` 强制走模板（联调用） | 默认不设，走真模型优先 |
| `AIB_FEISHU_APP_ID` / `APP_SECRET` / `VERIFICATION_TOKEN` / `ENCRYPT_KEY` | 飞书凭据（M2） | 飞书渠道降级为 `replay` 模式 |

---

## 9. 常见问题

**Q：隧道重启后地址变了怎么办？**
A：quick tunnel 的 URL 是临时的。演示当天**先起隧道再发地址**，并把地址写进当天的彩排脚本；要固定地址得用 Cloudflare 命名隧道（需要域名托管在 Cloudflare）。

**Q：容器起来了但 AI 回答很死板？**
A：`docker compose logs` 里看通道是不是 `fallback`。九成是 `.env` 里 key 没填 / 填错，或 compose 没把 `DEEPSEEK_API_KEY` 传进去（本仓库已修）。

**Q：页面打开是白的 / 静态资源 404？**
A：镜像里没带 `docs/` 或 `src/.../web/assets/`。本仓库 Dockerfile 已 `COPY src` 与 `COPY docs`；改了 Dockerfile 记得 `--build` 重建。

**Q：重启容器后会话和计划卡没了？**
A：`./runtime` 没挂数据卷。本仓库 compose 已挂；自己改过 compose 的注意别删。

---

## 10. 实测记录（2026-10-27，尹辰本机跑通）

第一版跑通方式：本机 `python -m ai_banking_copilot` 同源起服务（8000 被旧进程占用，演示实例起在 **8010**，`runtime/` 用干净目录避审计链校验冲突）→ `cloudflared tunnel --url http://127.0.0.1:8010`。

| 检查项 | 实测结果 |
|---|---|
| `GET /` | **200**（7752 字节） |
| `GET /m`（窄屏 App） | **200** |
| `GET /api/channels/health` | **200**，`im=local`、`feishu=stub/not_configured`（如实） |
| `POST /api/im/feishu` | **501 + `feishu_adapter_not_integrated`**（M2 未完成，如实不假成功） |
| `POST /api/im/message {"text":"查余额"}` | **200**，出计划卡「1 步 · GREEN」，`decision.reasons=["QUERY_ONLY"]`，含飞书卡片渲染体 |

> 结论：**无域名也能把整条链路放到公网 https 上**，`/api/im/message` 的往返已经过公网验证；只有真实飞书回调（M2）还等适配器。
> 注意：quick tunnel 的地址是**临时的**，本机进程一停就失效；正式演示前重新起一次，把新地址发群里。

---

*2026-10-27 建立；执行人：尹辰。*

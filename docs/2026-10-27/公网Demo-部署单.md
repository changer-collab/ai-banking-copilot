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

> **两条与服务器相关的开关**（本仓库已支持，别漏）：
> - `API_HOST_PORT=18080`：宿主 8000 被别的服务占用时改端口，容器内仍是 8000；
> - `PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple`：国内服务器直连 pypi.org 会 read timeout，构建必须换源。
>
> 完整命令：`API_HOST_PORT=18080 PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple docker compose -p aib up -d --build`

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

### 3.0 推荐：用 Docker 镜像跑（服务器上最省事，本次就是这么做的）

```bash
docker run -d --name aib-tunnel --restart unless-stopped --network host \
  cloudflare/cloudflared:latest tunnel --url http://127.0.0.1:18080 --no-autoupdate
docker logs aib-tunnel 2>&1 | grep trycloudflare     # 这里就是 https 地址
```

> 用 `--network host` 才能访问宿主 `127.0.0.1:18080`；`--restart unless-stopped` 让机器重启后自动恢复（但**地址会变**）。
> 好处：不用下载二进制（国内服务器直连 GitHub Releases 常下载不完整，二进制会段错误）。

### 3.1 备选：装二进制（Linux）

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
| 手机 / APP 端（可安装 PWA） | `https://<隧道域名>/`（≤600px 自动切底部四 Tab，无 `/m` 入口） |
| 本地联调页（不算产品页） | `https://<隧道域名>/im` |
| 渠道健康 | `https://<隧道域名>/api/channels/health` |
| 飞书回调（已接线，待配置凭据） | `https://<隧道域名>/api/im/feishu` |

### 3.3 Windows 本机（没服务器时先跑通）

```powershell
Invoke-WebRequest -Uri "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe" -OutFile cloudflared.exe
.\cloudflared.exe tunnel --url http://127.0.0.1:8000
```

> 本机方式适合**今天就把飞书回调 / 手机 4G 打开这条链路验证掉**；缺点是关电脑就断。

---

## 4. 验收清单（照这个点一遍）

- [ ] `https://<隧道域名>/` 手机 4G 打开，首页 3 秒内可操作
- [ ] 同一地址缩到 ≤600px，自动切成 App 观感（底部四个 Tab 都在）
- [ ] `https://<隧道域名>/im` 断网也能打开（零外链），能走完一次「文本进 → 计划卡 → 确认 → 回执」
- [ ] `https://<隧道域名>/api/channels/health` 返回三渠道状态，`feishu` 未配置时如实写 `not_configured`
- [ ] `POST https://<隧道域名>/api/im/feishu` 在**未配置 `AIB_FEISHU_*`** 时返回 **401 `signature_invalid`**（fail-closed，不是假成功）；配置后可跑 `scripts/replay_webhook.py normal`
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

## 10. 实测记录（2026-10-27）

### 10.1 云服务器正式部署（已完成）

**服务器**：`aistock@121.37.46.229`（Ubuntu 22.04 / x86_64 / Docker 29.3.1 / 7.5G 内存），已用 SSH 密钥登录部署完毕。

**当前公网地址**：`https://speeches-plant-confidence-constantly.trycloudflare.com`（quick tunnel，重启会变）

| 项 | 实际值 |
|---|---|
| 代码位置 | `/home/aistock/aib`（无 `.git`，见下方「服务器拉的什么」） |
| 容器 | `aib-api-1`（应用）+ `aib-tunnel`（cloudflared），均 `--restart unless-stopped` |
| 宿主端口 | **18080**（服务器 8000 / 80 / 443 已被别的项目占用，**没有动别人的容器**） |
| 启动命令 | `API_HOST_PORT=18080 docker compose -p aib up -d --build` |
| 数据 | `/home/aistock/aib/runtime/`（挂载进容器，重建不丢） |
| 日志 | 应用 `docker logs aib-api-1`；隧道 `docker logs aib-tunnel` |

**这次踩到的三个坑（已在仓库里修掉）**：

1. **GitHub 私有库服务器拉不下来**（`could not read Username`，且不装 token）→ 用本地 `git archive` 打包上传：
   ```bash
   # 本机
   git archive --format=tar.gz -o aib.tar.gz sprint/1026
   scp aib.tar.gz aib.env aistock@121.37.46.229:/home/aistock/
   # 服务器
   mkdir -p ~/aib && tar -xzf ~/aib.tar.gz -C ~/aib && mv ~/aib.env ~/aib/.env
   ```
   > 后续要么给仓库加 **Deploy Key**（只读）让服务器能 `git pull`，要么继续用这个打包流程；**别再手改服务器上的源码**，否则和仓库分叉。
2. **服务器直连 pypi.org 超时**（`files.pythonhosted.org` read timeout）→ Dockerfile 现在支持 `PIP_INDEX_URL` 构建参数，构建时用清华源：
   ```bash
   API_HOST_PORT=18080 PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple docker compose up -d --build
   ```
3. **服务器 8000 端口被占** → compose 的宿主端口改成可配 `API_HOST_PORT`（默认仍是 8000，不影响本地）。

**公网实测（2026-10-27 快照）**：

> 下表是 10-27 的实测记录。10-28 已合入飞书适配器、`/m` 并入响应式 PWA，**重新部署后以 §4 验收清单为准**；`GET /m` 不再存在（404 属预期）。

| 检查项 | 结果 |
|---|---|
| `GET /` | **200**（7752 字节） |
| `GET /m` | **200** |
| `GET /api/channels/health` | **200**，`im=local`、`feishu=stub/not_configured` |
| `POST /api/im/feishu` | **501 + `feishu_adapter_not_integrated`**（如实） |
| `POST /api/im/message {"text":"查余额"}` | **200**，计划卡「1 步 · GREEN」，`reasons=["QUERY_ONLY"]`，含飞书卡片渲染体 |

### 10.2 本机预演（部署前先在本机验过一遍）

本机 `python -m ai_banking_copilot` 起服务（8000 被旧进程占用 → 换 8010，`runtime/` 用干净目录避审计链校验冲突）→ `cloudflared tunnel --url http://127.0.0.1:8010`，五项检查同样全绿。

> 结论：**无域名也能把整条链路放到公网 https 上**，`/api/im/message` 的往返已经过公网验证；只有真实飞书回调（M2）还等适配器。
> 注意：quick tunnel 的地址是**临时的**，进程一停就失效；**演示当天先起隧道、再发地址**，并把新地址写进彩排脚本。

### 10.3 服务器现状提醒

- 磁盘紧张：部署当天做过一次安全清理（**只清构建缓存与悬空镜像，没动任何人的镜像/容器**）：`docker builder prune -af` + `docker image prune -f`，**释放约 4.1G 构建缓存**，可用空间 4.5G → **6.3G（95% 使用）**。
  - **以后每次部署后建议顺手跑一次**：`docker builder prune -af`（只删构建缓存，不影响运行中的容器）。
  - 还剩 12.8G 镜像，都是运行中容器在用的（其中 `expert-managementsystem-app:latest` 4.07G、`napcat` 2.12G、`medicine-process-api` 2.17G、两个 `mysql` 共 2.4G）——**这些不是我们的，别删**，要清得先问对应的人。
- 这台机器上还跑着 `expert-managementsystem-*`、`komari`、`qq_feishu_bot`、`qq_napcat` 等**别人的服务**，部署与重启**只操作 `aib-*`**，不要 `docker compose down` 全局、不要改 80/443 的占用。

### 10.4 2026-10-28 重新部署与飞书四项验收（**replay 模式**）

**部署**：本机 `tar -czf aib-deploy.tar.gz --exclude=.git --exclude=runtime --exclude=.env ...`（10.78MB）→ `scp` 到 `/home/aistock/` → 备份旧目录为 `aib-backup-.tgz` → 解包进 `~/aib` → `API_HOST_PORT=18080 PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple docker compose -p aib up -d --build api`。**只重建 `aib-api`，`aib-tunnel` 未重启**，故地址不变；服务器 `docker-compose.yml` md5 与仓库一致（`6132ca36…`），未改编排。

**公网实测（2026-10-28）**：

| 检查项 | 结果 |
|---|---|
| `GET /` | **200**，页面引用 `app.js?v=31`、`workspace.css?v=15` |
| `GET /assets/sw.js` | **200**，`aib-static-shell-v24`（Service Worker 在 `/assets/` 下，`/sw.js` 404 属预期） |
| `GET /m` | **404**（`/m` 已并入响应式 PWA，属预期） |
| `GET /im` | **200** |
| `GET /api/channels/health` | **200**，`im=local / bound_identities=1`、`feishu.mode=replay / reason=replay_mode / card_action=false` |
| `POST /api/im/message {"text":"查余额"}` | **200**，「计划卡已生成（1 步，级别 GREEN）」，含飞书交互卡片体（`确认办理` 按钮） |

> **口径补充（2026-10-29 升版，已上线）**：上表为 10-28 部署时的版本快照；10-29 前后端有改动，版本链已升为
> `app.js?v=32` / `workspace.css?v=16` / `sw.js` 缓存名 `aib-static-shell-v25`，并已于 2026-10-29 重新构建
> `aib-api`（仅重建 api，`aib-tunnel` 未重启，公网地址不变）。线上实测 `css16=True / js32=True / shell25=True`，
> 公网 `check_served_page.py` 与 `e2e_check.py` 均全绿。不要把上表的 v31 / v15 / v24 当成当前线上值。

**飞书四项验收**（`python scripts/replay_webhook.py <mode> --url https://<当日地址>/api/im/feishu --key replay-key`）：

| 模式 | 结果 |
|---|---|
| `normal` | **200**，GREEN 计划卡 + 卡片渲染体（`header.template=green`，按钮 `value.action=confirm`） |
| `tamper` | **401** `{"detail":"signature_invalid","channel":"feishu"}` |
| `replay` | 首次 `replayed: false`，第二次 **`replayed: true`**（同 `event_id`，幂等生效、不重复执行业务） |
| `burst` | 21 连发 → 前 20 次 **200**，第 21 次 **429**，响应头 **`Retry-After: 60`** |

> **口径边界**：服务器**没有真实 `AIB_FEISHU_*` 凭据**，以上是 **replay 模式**（回放密钥 + 回放事件），**不等于真实飞书回调验收**。验收前手工往 `channel_identities` 写了一条演示绑定 `feishu / ou-replay → u_demo`（仓库内暂无发码/绑定 HTTP 入口）；未绑定时正常回放会返回 **401 `identity_unbound`**，属预期。

**当日故障与处置**：部署后 `/api/im/message` 返回 **409「会话存储或恢复失败」**。容器内 `probe_snapshot.py` 定位为 `SessionSnapshotError: snapshot_decode_failed:invalid_snapshot_fields`——10-27 旧代码写入的 `('u_demo','im:default')` 快照在新 codec 下无法解码。处置：整表备份到 `/app/runtime/stale-snapshots-20261028.json` → 删除该行 → `docker restart aib-api-1`（清掉内存中已 `_persistence_failed` 的会话）→ 复测恢复 200。**这是代码侧残留**（旧快照无兼容迁移），不是部署问题。

---

## 11. 每日部署清单（尹辰每天跑一遍，约 2 分钟）

> 目标：把当天 `sprint/1026` 的最新代码放上去，**且尽量不改隧道地址**（地址一变，飞书后台就得跟着改一次）。
> **关键：只重建 `aib-api`，不重启 `aib-tunnel`。** 隧道活着，地址就不变。

```bash
# ① 本机：打包当天代码并上传
cd /path/to/ai-banking-copilot
git checkout sprint/1026 && git pull
git archive --format=tar.gz -o aib.tar.gz sprint/1026
scp aib.tar.gz aistock@121.37.46.229:/home/aistock/

# ② 服务器：解包（保留 .env 与 runtime）+ 只重建应用
ssh aistock@121.37.46.229
cd ~ && rm -rf aib/src aib/docs aib/tests aib/scripts aib/deploy aib/Dockerfile aib/docker-compose.yml aib/pyproject.toml
tar -xzf ~/aib.tar.gz -C ~/aib
cd ~/aib
API_HOST_PORT=18080 PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple docker compose -p aib up -d --build
docker builder prune -af            # 顺手清构建缓存（磁盘只剩 6G 左右）
```

③ 验收三条（缺一条就回滚到上一个 tar.gz）：

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:18080/                # 200
curl -s http://127.0.0.1:18080/api/channels/health                              # feishu 状态如实
curl -s -X POST http://127.0.0.1:18080/api/im/message \
  -H 'Content-Type: application/json' -d '{"text":"查余额"}' | head -c 200        # 出计划卡
```

④ 取当天公网地址（**隧道没重启的话，和昨天一样**）：

```bash
docker logs aib-tunnel 2>&1 | grep trycloudflare | tail -1
```

⑤ 只有这几种情况才需要重启隧道（并**立刻告诉王昌泽新地址**）：容器挂了、服务器重启、`docker ps` 里看不到 `aib-tunnel`。
```bash
docker restart aib-tunnel && sleep 10 && docker logs aib-tunnel 2>&1 | grep trycloudflare | tail -1
```

> `.env` 和 `runtime/` 在 `~/aib` 下，上面第 ② 步**不删它们**，所以密钥与会话/审计数据每天都在，不会被覆盖。
>
> **打包必须在仓库根目录执行（2026-10-30 实际踩到）**：如果在仓库的**上级目录**打包，tar 里会多一层
> `ai-banking-copilot/`，解包到 `~/aib` 后代码落在 `~/aib/ai-banking-copilot/`，`~/aib/src` 仍是旧版本，
> 容器照常启动、页面照常 200，**看起来部署成功但改的代码没生效**。正确写法：
> `tar -C /path/to/ai-banking-copilot -czf aib.tar.gz --exclude=.git --exclude=runtime --exclude=.env .`
> 验收时逐条核对容器内的文件，而不是只看 `docker ps`：
> `docker exec aib-api-1 grep -c '<本次改动的关键符号>' /app/src/ai_banking_copilot/web/app.py`

---

## 12. 渠道对接（**王昌泽看这一节就够了**）

### 12.1 你需要的三样东西

| 你要的 | 从哪来 |
|---|---|
| **回调地址** | `https://<当日隧道域名>/api/im/feishu`。域名每天可能变，**找尹辰要当天那条**（或等尹辰发群里），不要自己猜 |
| **四个凭据变量名** | `AIB_FEISHU_APP_ID` / `AIB_FEISHU_APP_SECRET` / `AIB_FEISHU_VERIFICATION_TOKEN` / `AIB_FEISHU_ENCRYPT_KEY`。**值只私发给尹辰**，由他写进服务器 `.env` 并重启 `aib-api`；**群里/文档/截图里都不写值** |
| **服务是否可达** | 浏览器打开 `https://<当日隧道域名>/api/channels/health`，`feishu` 那段现在是 `replay / replay_mode`（**未配凭据**）；你接好之后应变成 `configured: true`，`mode` 不再是 `replay` |

### 12.2 现在能不能填飞书后台

**适配器已合入，但服务器还没配凭据。** `POST /api/im/feishu` 不再返回 501；未配 `AIB_FEISHU_*` 时服务处于 **`replay` 模式**（用回放密钥校验，只对 `replay_webhook.py` 有效），此时**在飞书后台点「保存」仍会校验失败**——真实验签/解密要等凭据写入并重启 `aib-api`。

**正确顺序**：

1. ~~先在本机把 `web/channels/feishu.py` 写完~~ 已完成（验签 / AES 解密 / `url_verification` 回声 / `event_id` 幂等 / ACK / 交互卡片 / 出站发消息）；
2. 四项回放已过（2026-10-28，公网地址实测，见 §10.4）：`tamper`→401、`replay`→只执行一次、`burst`→429 + `Retry-After: 60`、正常→出计划卡；
3. 找尹辰要**当日回调地址**，同时把四个凭据私发给他（他写进服务器 `.env`、重启 `aib-api`）；
4. 再回飞书后台填地址 → 订阅 `im.message.receive_v1` → 保存（这时校验才会通过）；
5. 在飞书里发一句「给李娜转500」，找尹辰看 `docker logs aib-api-1` 是否收到、`/api/channels/health` 的 `feishu` 是否不再是 `replay`；
6. 同一句话再走一遍网页 `/`，比对 `state`、三色等级、`card_id` 结构**逐字段相同**（M1 的双端一致）。
7. **真实用户要先绑定身份**：首次回调若返回 `401 identity_unbound`，说明该飞书 `open_id` 未绑定；仓库内**还没有发码/绑定的 HTTP 入口**，目前只能手工往 `channel_identities` 写一条 `('feishu', '<open_id>', '<user_id>')`。

### 12.3 填后台时容易踩的点

| 现象 | 原因 |
|---|---|
| 保存时「请求地址校验失败」 | 界面报了这句，但根本原因是**没配凭据**（服务仍是 `replay` 模式，不认飞书真实验签）；或地址少写 `/api/im/feishu`，或用了昨天的旧域名 |
| 飞书发消息没反应 | 没订阅 `im.message.receive_v1`；或后端在 3 秒内没 ACK 被飞书重试；看 `docker logs aib-api-1` |
| 收到 401 | 签名算法/`X-Lark-Signature` 拼错，或 `ENCRYPT_KEY` 与实际配置不一致 |
| 同一条消息执行了两次 | `event_id` 幂等没做（必须命中幂等表直接 200，不重放业务） |
| 频繁 429 | 触发了 20 次/分钟的令牌桶（`channel:external_user_id`），属预期，不是 bug |

### 12.4 你可以动 / 不能动

- **可以动**：`web/channels/feishu.py`（新建）、`web/channels/ratelimit.py`（新建）、`tests/web/test_channels_im.py`、`scripts/replay_webhook.py`、`docs/2026-10-27/飞书接入-配置单.md`。
- **先申请再动**：`web/app.py` 里 `/api/im/feishu` 那段 501 占位（改成调你的适配器）、`web/channels/{contracts,identity,dispatcher,render}.py`（M1 契约，改之前跟我说一声）。
- **不要动**：服务器上的容器（尤其 `expert-managementsystem-*`、`qq_*`、`komari` 这些别人的）、`.env` 里的密钥（由尹辰统一写）。

### 12.5 一句提醒

渠道这一摊是全组最宽的，**卡住超过 30 分钟就找我**（微信/电话都行），尤其涉及**契约、身份映射、幂等表结构、降级策略**时——先问再写，别自己硬扛。

---

## 13. 部署与地址变更记录

| 日期 | 隧道地址 | 备注 |
|---|---|---|
| 2026-10-27 | `https://speeches-plant-confidence-constantly.trycloudflare.com` | 首次部署（18080 + aib-tunnel），五项实测全绿 |
| 2026-10-28 | 同上（**地址未变**） | 只重建 `aib-api`（未重启隧道）；`feishu=replay`，四项验收全过；旧快照 409 已备份删除（见 §10.4） |

> 地址变了就在这张表加一行，并同步给王昌泽。

---

*2026-10-27 建立；执行人：尹辰。*

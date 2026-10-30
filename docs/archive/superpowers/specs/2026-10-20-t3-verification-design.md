# T3 强验证设计

状态：2026-10-20 用户已批准通用 HTTP 提供方方案；已按该范围本地验收，真实第二信道尚未联调。

## 架构

新增 `policy/verification.py`，服务端统一生成/校验挑战，provider 仅负责投递。相比 provider 全包校验，该方案使 TTL、绑定和一次性语义保持一致；仅隐藏现有码不能满足外部投递和审计要求。

`VerificationBinding` 包含 user_id、session_id、execution_id、card_id、frozen_hash。`ChallengeRecord` 保存随机 challenge_id、绑定、HMAC-SHA256 摘要、到期时间、状态和 provider 引用，不存明文。独立服务端密钥由组合根持久加载；external 缺密钥失败关闭。6 位码、300 秒 TTL、错码不消费、成功一次性、申请轮换旧码失效。

`VerificationProvider.deliver(binding, challenge_id, code, expires_at)` 返回模式、渠道、脱敏目标和投递引用。Demo 明确模拟，不伪称短信下发；external 使用可注入 httpx transport 的通用 POST JSON，发送绑定、challenge_id、code、expires_at，认证仅在服务端。目标由可信配置决定，请求体不能任意指定收件人。

## 配置与响应

`AIB_VERIFICATION_MODE=demo|external`，默认 demo 保持本地测试；`AIB_VERIFICATION_STRICT=1` 禁止 demo。external 需 endpoint、token、target 和摘要密钥；HTTP 超时/拒绝/非法响应均不创建有效挑战，也不降级 demo。返回状态必须与实际投递一致。

`VerificationPrompt` 增加 mode、status，code 改为可选。仅 demo HTTP 响应包含 code，external 彻底省略此键。静态文案改为中性“强验证”；前端按 mode 显示演示码或投递状态，提供重新申请入口。

## 状态和审计

Session 显式接收 session_id；请求、投递结果、错误、过期、轮换、消费和计划失效写现有审计链。事件可包含绑定、challenge_id、provider、状态、原因、脱敏目标；禁止码、摘要、完整收件人和密钥。

T4 持久保存挑战元数据；重启后 active 挑战失效，终态不得恢复成 active。挑战不可重放，但同一次已授权执行的进程内重复确认和恢复继续复用原确认，不能重复扣款。

当前固定演示用户与客户端 session_id 不等同真实身份认证；通用 HTTP adapter 完成不等同真实短信/人脸/U 盾已接入。

## 文件和验收

T3 子代理只改验证模块与 `tests/policy/test_verification.py`。组长统一集成 compose、session、app、page、app.js、IM 会话身份及 API/重启测试，不改审计算法。

验收涵盖错码/缺码/过期/二次消费/轮换、绑定每字段不符、external 投递成功和失败、严格模式不回码、事件无秘密、已授权幂等恢复、重启旧挑战无效。外部平台联调留作明确残留。

# T3 强验证实施计划

状态：2026-10-20 已本地验收；能力与残留见 [README](../../../../README.md#强验证与会话恢复)。

> 执行：使用 superpowers:subagent-driven-development；独立验证模块并行，组长统一接线。

**目标**：实现可替换 HTTP 投递、无明文存储、严格不回码和完整挑战生命周期审计。

**架构**：服务端统一挑战语义，provider 只投递，持久化复用 T4。

**技术栈**：Python dataclasses、secrets/hmac、httpx、pytest。

## 任务 1：验证核心（T3 子代理独占）

文件：新增 `src/ai_banking_copilot/policy/verification.py` 与 `tests/policy/test_verification.py`。模块实施不改 Session/app/compose/前端；未另建 worktree，验收后用户已授权通过 PR 合并。

- [x] 先写错码、缺码、过期、成功、重复消费、五字段绑定不符、轮换失效测试；运行 `.venv\Scripts\python.exe -m pytest tests/policy/test_verification.py -q` 确认失败。
- [x] 定义冻结 dataclasses：VerificationBinding(user_id,session_id,execution_id,card_id,frozen_hash)、ChallengeRecord(challenge_id,binding,code_digest,expires_at,status,provider_reference)、DeliveryResult(mode,channel,target,reference)。摘要使用 HMAC-SHA256，常量时间比较。
- [x] 导出创建挑战和验证函数，返回新的不可变记录或稳定 VerificationError，错误携带安全 reason；验证码只在生成投递的局部变量存在，不加入记录。

核心校验约束：

```python
if record.status != "active" or record.binding != binding:
    raise VerificationError("binding_or_status")
if now >= record.expires_at:
    raise VerificationError("expired")
if not hmac.compare_digest(record.code_digest, digest):
    raise VerificationError("invalid_code")
```

## 任务 2：Demo 与 external HTTP 投递

- [x] 先补 httpx.MockTransport 测试：成功 JSON、非 2xx、超时、非法回包、缺配置；真实网络一律不调用。
- [x] 定义 VerificationProvider.deliver，提供 DemoVerificationProvider 和 ExternalVerificationProvider。external 请求为 POST，认证只在 header，绑定+challenge_id+code+expires_at 在 JSON，收件人来自可信服务端配置。
- [x] external 禁止重试与 demo 降级；错误只返回安全分类，不包含 token、code、完整 target 或正文。delivery result.target 为脱敏值。
- [x] 跑模块测试及 ruff，向组长提交确定的 Python 签名、配置需求与安全错误类型。
- [x] 完成 spec review 后再 code-quality review，修复发现。

## 任务 3：组长接线

- [x] AppDeps 增加 verification provider/key/strict；compose 解析 AIB_VERIFICATION_MODE、STRICT、ENDPOINT、TOKEN、TARGET、KEY。demo 可用受控持久本地密钥，external 缺配置失败关闭。
- [x] Session 接收显式 session_id，替换明文 _Challenge 为 ChallengeRecord；request_verification 先投递成功才激活挑战；重新申请使旧挑战失效。
- [x] 在 request/invalid/expired/replaced/consumed/reset 处写 verification 审计，只放安全白名单；VerificationPrompt 添加 mode/status，code 可选。
- [x] 严格 API 从 JSON 中省略 code；demo 明确演示；前端提供重新申请并如实显示发送失败，page.py 去除写死短信能力。IM 只补 session_id，不宣称已有验证入口。
- [x] 在 `tests/web/test_verification_integration.py` 加 API 模式、脱敏、审计和已有幂等确认测试；运行 `.venv\Scripts\python.exe -m pytest tests/policy/test_verification.py tests/web/test_verification_integration.py tests/web/test_session_confirm.py tests/web/test_execution_consistency.py -q`。

## 任务 4：联合恢复与验收

- [x] T4 codec 仅存摘要/绑定/终态，重启使 active 失效；恢复测试验证已消费码不复活。
- [x] 全仓库及服务门禁通过后更新台账：通用 external adapter 已验，真实第二信道未联调。不得把 MockTransport 视为真实短信接入。

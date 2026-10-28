# 飞书渠道 M1–M4 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** 在不改动冻结渠道契约和服务器配置的前提下，完成飞书真实协议、可靠性、回放脚本、双端一致性和订阅可查能力。

**Architecture:** 新增飞书协议适配器，负责签名、解密、事件解析、快速 ACK 和出站卡片发送；业务继续经现有 Dispatcher 和 SessionManager，不在渠道层重复判级或执行。新增轻量可靠性与健康统计模块，持久化 event_id 幂等记录，回放脚本只访问本机接口。

**Tech Stack:** Python 3.11、FastAPI、SQLite、标准库 hashlib/hmac/urllib、现有 render_feishu_card、pytest。

## Global Constraints

- 只在 feat/wangchangze 分支工作，禁止直接修改 sprint/1026。
- 不修改 web/channels/contracts.py、identity.py、dispatcher.py、render.py 的公开契约。
- 不修改服务器容器、服务器 .env 或飞书后台配置；本阶段只完成本机代码和确定性回放。
- 真实飞书凭据只从环境变量读取，绝不写入仓库、日志、测试输出或文档。
- M2 完成前 /api/im/feishu 不接公网配置；本机四个回放开关全部通过后再向用户索要当天回调地址和四个凭据。
- 所有新增行为先写失败测试，确认失败后再写生产代码。
- 事实、金额、状态和等级继续来自现有 gateway/session 返回值，渠道只搬运和渲染。
- 门禁使用 .venv\Scripts\python.exe -m ruff check .、.venv\Scripts\python.exe -m mypy src、.venv\Scripts\python.exe -m pytest -q。

---

### Task 1: 飞书协议适配器

**Files:**
- Create: src/ai_banking_copilot/web/channels/feishu.py
- Test: tests/web/test_channels_feishu.py

**Interfaces:**
- Consumes: ChannelAdapter、InboundMessage、OutboundMessage、现有 render_feishu_card。
- Produces: FeishuConfig.from_env()、FeishuAdapter.verify(headers, body)、FeishuAdapter.parse(payload)、FeishuAdapter.render(outbound)、FeishuAdapter.decode_body(body)。

- [ ] Step 1: Write failing tests

~~~~python
def test_verify_accepts_feishu_signature() -> None:
    adapter = FeishuAdapter(FeishuConfig(encrypt_key="k"))
    body = b'{"event":"x"}'
    headers = signed_headers("k", body, timestamp="1", nonce="n")
    assert adapter.verify(headers, body) is True

def test_parse_url_verification_returns_challenge() -> None:
    event = FeishuAdapter(...).parse({"type": "url_verification", "challenge": "c"})
    assert event.challenge == "c"

def test_decode_rejects_tampered_ciphertext() -> None:
    with pytest.raises(FeishuProtocolError):
        FeishuAdapter(...).decode_body(tampered_body)
~~~~

- [ ] Step 2: Run the focused tests and confirm the expected missing-module failure

Run: .venv\Scripts\python.exe -m pytest tests/web/test_channels_feishu.py -q

Expected: FAIL because web.channels.feishu and its protocol types do not exist.

- [ ] Step 3: Implement the minimal protocol layer

Implement:

~~~~python
@dataclass(frozen=True)
class FeishuConfig:
    app_id: str | None
    app_secret: str | None
    verification_token: str | None
    encrypt_key: str | None
    replay_mode: bool = False

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "FeishuConfig": ...

class FeishuAdapter:
    def verify(self, headers: Mapping[str, str], body: bytes) -> bool: ...
    def decode_body(self, body: bytes) -> Mapping[str, object]: ...
    def parse(self, payload: Mapping[str, object]) -> FeishuEvent: ...
    def render(self, outbound: OutboundMessage) -> Mapping[str, object]: ...
~~~~

Use constant-time signature comparison. Support plaintext and encrypted envelopes, URL verification, text message extraction, and interactive-card rendering. Missing live credentials must be reported as replay mode, never as live configuration.

- [ ] Step 4: Run focused tests

Run: .venv\Scripts\python.exe -m pytest tests/web/test_channels_feishu.py -q

Expected: all protocol tests pass, including tampered signature rejection and AES round-trip.

- [ ] Step 5: Commit

~~~~powershell
git add src/ai_banking_copilot/web/channels/feishu.py tests/web/test_channels_feishu.py
git commit -m "feat: add feishu protocol adapter"
~~~~

### Task 2: 幂等、出站发送和可靠性统计

**Files:**
- Create: src/ai_banking_copilot/web/channels/ratelimit.py
- Modify: src/ai_banking_copilot/web/channels/feishu.py
- Test: tests/web/test_channels_reliability.py

**Interfaces:**
- Consumes: Store、OutboundMessage、FeishuAdapter。
- Produces: HealthRegistry.snapshot()、retry_outbound(send, attempts=3)、持久化 feishu_events 事件台账。

- [ ] Step 1: Write failing tests

~~~~python
def test_outbound_retries_only_transient_failures() -> None:
    sender = FailingSender(failures=2)
    retry_outbound(sender.send)
    assert sender.calls == 3

def test_event_id_is_processed_once_after_restart(channel_store: Store) -> None:
    first = EventLedger(channel_store).claim("evt-1")
    second = EventLedger(channel_store).claim("evt-1")
    assert first is True
    assert second is False
~~~~

- [ ] Step 2: Run tests and confirm failure

Run: .venv\Scripts\python.exe -m pytest tests/web/test_channels_reliability.py -q

Expected: FAIL because retry and persistent event ledger are not defined.

- [ ] Step 3: Implement minimal reliability pieces

Add a small SQLite event ledger keyed by (channel, event_id). Add exponential delays (0.5, 2.0, 8.0) with an injected sleeper for tests. Retry only timeout and 5xx failures. Record last_event_at, success/failure counts, retry counts, and rate-limit hits in a thread-safe registry.

- [ ] Step 4: Run focused tests

Run: .venv\Scripts\python.exe -m pytest tests/web/test_channels_reliability.py -q

Expected: PASS with exactly three attempts for transient failures and one claim for duplicate events.

- [ ] Step 5: Commit

~~~~powershell
git add src/ai_banking_copilot/web/channels/feishu.py src/ai_banking_copilot/web/channels/ratelimit.py tests/web/test_channels_reliability.py
git commit -m "feat: add channel idempotency and delivery retries"
~~~~

### Task 3: FastAPI 回调、健康页和 M1 回归

**Files:**
- Modify: src/ai_banking_copilot/web/app.py
- Modify: src/ai_banking_copilot/compose.py only if schema initialization needs an explicit hook
- Modify: tests/web/test_channels_contract.py
- Create: tests/web/test_channels_restart.py

**Interfaces:**
- Consumes: FeishuAdapter、EventLedger、HealthRegistry、现有 Dispatcher。
- Produces: real POST /api/im/feishu、GET /api/channels/health、快速 ACK、429 的 Retry-After。

- [ ] Step 1: Add failing route and parity tests

~~~~python
def test_feishu_tamper_returns_401(client): ...
def test_feishu_url_verification_echoes_challenge(client): ...
def test_im_and_web_match_state_level_card_fields(client): ...
def test_im_plan_can_be_confirmed_after_app_restart(tmp_path): ...
def test_rate_limit_response_has_retry_after(client): ...
~~~~

- [ ] Step 2: Run focused tests and confirm expected failures

Run: .venv\Scripts\python.exe -m pytest tests/web/test_channels_contract.py tests/web/test_channels_restart.py -q

Expected: failures for the 501 response, missing Retry-After, and missing restart/parity assertions.

- [ ] Step 3: Wire the adapter without changing frozen channel contracts

The route reads raw request bytes and headers, verifies before parsing, returns challenge synchronously, claims event_id, then schedules dispatch and outbound send. The exception handler maps ChannelRateLimited to HTTP 429 with a human-readable body and Retry-After: 60. Health combines existing local IM data with web, im, and feishu registry snapshots.

- [ ] Step 4: Run focused integration tests

Run: .venv\Scripts\python.exe -m pytest tests/web/test_channels_contract.py tests/web/test_channels_restart.py tests/web/test_channels_reliability.py -q

Expected: all channel tests pass; Web and IM match on state, level, card_id, frozen_hash, lines, and requires.

- [ ] Step 5: Commit

~~~~powershell
git add src/ai_banking_copilot/web/app.py src/ai_banking_copilot/compose.py tests/web/test_channels_contract.py tests/web/test_channels_restart.py
git commit -m "feat: wire feishu webhook and channel health"
~~~~

### Task 4: 本地回放脚本和四个开关

**Files:**
- Create: scripts/replay_webhook.py
- Test: tests/scripts/test_replay_webhook.py

**Interfaces:**
- Consumes: local POST /api/im/feishu、FeishuAdapter signing/encryption helpers。
- Produces: CLI flags --url、--text、--tamper、--replay、--burst、--channel。

- [ ] Step 1: Write failing script tests

Assert four stable outcomes: normal contains a plan-card marker, tamper exits nonzero and reports 401, replay reports one processed event and one replay, burst reports 429 responses.

- [ ] Step 2: Run tests and confirm failure

Run: .venv\Scripts\python.exe -m pytest tests/scripts/test_replay_webhook.py -q

- [ ] Step 3: Implement the script

Use urllib.request, deterministic event IDs, the local replay secret, and no external network. Do not mutate server configuration. For normal replay, print the rendered card captured by the local adapter; for live mode, the script must refuse to print secrets.

- [ ] Step 4: Run all four switches

~~~~powershell
.\.venv\Scripts\python.exe scripts\replay_webhook.py --text "给李娜转500"
.\.venv\Scripts\python.exe scripts\replay_webhook.py --text "给李娜转500" --tamper
.\.venv\Scripts\python.exe scripts\replay_webhook.py --text "给李娜转500" --replay
.\.venv\Scripts\python.exe scripts\replay_webhook.py --text "查一下余额" --burst 25
~~~~

Expected: plan card, 401, one business execution with replay, and 429 responses respectively.

- [ ] Step 5: Commit

~~~~powershell
git add scripts/replay_webhook.py tests/scripts/test_replay_webhook.py
git commit -m "feat: add deterministic webhook replay tool"
~~~~

### Task 5: 订阅记录与渠道健康前端

**Files:**
- Modify: src/ai_banking_copilot/scenarios/subscriptions.py
- Modify: src/ai_banking_copilot/web/app.py
- Modify: src/ai_banking_copilot/web/page.py
- Modify: src/ai_banking_copilot/web/assets/app.js
- Modify: src/ai_banking_copilot/web/assets/workspace.css
- Test: tests/web/test_subscription_details.py
- Test: tests/web/test_channels_contract.py

**Interfaces:**
- Consumes: existing subscription evidence and /api/channels/health。
- Produces: charge details with merchant、amount、booked_on、channel and Web/APP health panels.

- [ ] Step 1: Add failing payload tests

Assert subscription evidence contains channel, the API exposes charge rows, and both HTML shells contain the health entry and channel column.

- [ ] Step 2: Run tests and confirm failure

Run: .venv\Scripts\python.exe -m pytest tests/web/test_subscription_details.py tests/web/test_channels_contract.py -q

- [ ] Step 3: Implement the smallest read-only payload and UI

Extend subscription evidence serialization from gateway facts; do not add a new bank table. Render health states and channel values returned by the server. Keep empty/loading/failure states explicit and do not infer status in JavaScript.

- [ ] Step 4: Run focused tests and static page checks

Run: .venv\Scripts\python.exe -m pytest tests/web/test_subscription_details.py tests/web/test_channels_contract.py -q

Expected: PASS and page checks find the health panel, channel column, and no external asset URL.

- [ ] Step 5: Commit

~~~~powershell
git add src/ai_banking_copilot/scenarios/subscriptions.py src/ai_banking_copilot/web/app.py src/ai_banking_copilot/web/page.py src/ai_banking_copilot/web/assets/app.js src/ai_banking_copilot/web/assets/workspace.css tests/web/test_subscription_details.py tests/web/test_channels_contract.py
git commit -m "feat: expose subscription charges and channel health"
~~~~

### Task 6: 交付说明、验收记录和门禁

**Files:**
- Create: docs/2026-10-27/公网Demo-部署单.md
- Create: docs/2026-10-27/飞书接入-配置单.md
- Create: docs/2026-10-27/验收记录-王昌泽.md
- Test: all existing tests and page checks.

- [ ] Step 1: Write the two requested operational documents without secrets

Document local replay first, then the later sequence for callback URL, four environment variable names, event subscription, and live smoke. State clearly that the server owner performs restart and .env changes.

- [ ] Step 2: Record local evidence

Capture command, input, response, expected result, and any remaining live-only step. Do not claim live Feishu delivery before credentials and callback verification are completed.

- [ ] Step 3: Run all gates

~~~~powershell
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m mypy src
.\.venv\Scripts\python.exe -m pytest -q
python scripts\check_served_page.py
python scripts\e2e_check.py
python scripts\check_design_sync.py
~~~~

- [ ] Step 4: Inspect the diff and commit only owned files

~~~~powershell
git status --short
git diff --check
git diff --stat origin/sprint/1026...HEAD
git commit -m "feat: complete feishu channel M1-M4"
~~~~

## Self-review

- M1 parity, restart recovery, M2 protocol, M3 retry/health/429, M4 replay switches, subscription records, and requested documents each have a dedicated task.
- No frozen channel contract is changed.
- No live credentials, server operations, or Flywheel backend settings are included.
- The only live-only dependency is the final callback/credential smoke after local gates pass.


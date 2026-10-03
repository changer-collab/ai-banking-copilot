# S1a · LLM 意图通道接线 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让交付入口真正装配"真模型优先 + 失败自动降级模板"的意图通道（消除 G10：入口只装模板、LLM 通道 `src` 零引用、`DASHSCOPE_API_KEY` 无消费者）。

**Architecture:** 新增包根组合模块 `src/ai_banking_copilot/compose.py`，把"意图通道装配"与"AppDeps 装配"从 `__main__.py` 抽出为可单测的纯函数；`__main__.py` 退化为"调 `build_deps` + 起 uvicorn"。意图通道用现成的 `FallbackIntentModel(LlmIntentModel(...))`：`LlmIntentModel` 懒加载（构造期不碰网络），缺 Key / 缺 httpx / 调用失败一律抛 `LlmUnavailableError` → `FallbackIntentModel` 回落 `TemplateIntentModel`（H9：降级通道是能力子集，不宣称等价）。

**Tech Stack:** Python ≥3.11；标准库（`os`/`collections.abc`/`pathlib`）；已有 `planning.llm`（`LlmIntentModel`/`FallbackIntentModel`/`TemplateIntentModel`）；`pytest`；`ruff`（行宽 100，规则 E,F,I,UP,B）；`mypy --strict`（仅 `src/`）。

## Global Constraints

- Python ≥ 3.11；不用 3.12+ 专属语法。
- 行宽 **100**；ruff 规则 `E,F,I,UP,B`；`mypy --strict` 对 `src/` 生效。
- **不改 00/01/02/03/04 既有行为**；不改 `gateway/store.py::SCHEMA_SQL`；冻结符号与签名不得改。
- **LLM 接线纪律（spec §4.4）**：Key 只走环境变量，**禁止入库 / 入日志 / 入提交物**；测试与 CI 必须**离线**（不发起网络请求）；H9——降级通道声明为能力子集，不宣称等价。
- `web/*.py` 不得出现 `CoreBank(`（H2 守卫，`tests/web/test_web_integration.py`）；组合根放**包根**。
- 提交信息格式 `<type>: <描述>`（type ∈ feat/fix/test/docs/chore/refactor）；每个 Task 以测试全绿为通过条件。
- 本机工作流（窗口约束）：代码先写工作区暂存 → `Copy-Item` 覆盖到 `E:\AI_Banking` → `git -C E:\AI_Banking` 提交推送。

---

## File Structure

| 文件 | 动作 | 职责 |
|---|---|---|
| `src/ai_banking_copilot/compose.py` | Create | 组合根：`build_intent_model(env)` 装配意图通道；`build_deps(db_path, env)` 装配 `AppDeps` |
| `src/ai_banking_copilot/__main__.py` | Modify | 退化为 `build_deps(...) + uvicorn.run(...)` |
| `tests/test_compose.py` | Create | 装配与降级测试（全程不联网） |
| `README.md` | Modify | LLM 口径：真链路 + 自动降级 + `AIB_DISABLE_LLM` 开关 + Key 只走环境变量 |
| `e:\changer_learning\比赛\AI银行副驾-下一步决策冻结-2026-10-02.md` | Modify | 追加裁决（新增 `compose` 组合根；入口 LLM 口径） |

---

### Task 1: 意图通道装配 `build_intent_model`

**Files:**
- Create: `src/ai_banking_copilot/compose.py`
- Test: `tests/test_compose.py`

**Interfaces:**
- Consumes: `planning.llm.FallbackIntentModel`、`planning.llm.LlmIntentModel`、`planning.llm.TemplateIntentModel`、`planning.llm.IntentModel`（Protocol）。
- Produces:
  - `compose.DISABLE_LLM_ENV: str`（值 `"AIB_DISABLE_LLM"`）
  - `compose.build_intent_model(env: Mapping[str, str] | None = None) -> IntentModel`
  - 语义：`env[DISABLE_LLM_ENV]` ∈ {`1`,`true`,`yes`,`on`}（大小写不敏感）→ 返回 `TemplateIntentModel()`；否则返回 `FallbackIntentModel(LlmIntentModel(api_key=env.get("DASHSCOPE_API_KEY", "")))`。

- [ ] **Step 1: 写失败的测试**

`tests/test_compose.py`

```python
"""组合根装配：意图通道真模型优先、失败/禁用降级模板（全程不联网）。"""

from __future__ import annotations

from ai_banking_copilot.compose import DISABLE_LLM_ENV, build_intent_model
from ai_banking_copilot.planning.llm import FallbackIntentModel, TemplateIntentModel


def test_disabled_env_is_template_only() -> None:
    model = build_intent_model({DISABLE_LLM_ENV: "1"})
    assert isinstance(model, TemplateIntentModel)


def test_missing_key_degrades_to_template_without_network() -> None:
    model = build_intent_model({})
    assert isinstance(model, FallbackIntentModel)
    proposal = model.propose("转给李娜 500 元", {})
    assert [item.action for item in proposal.intents] == ["transfer.create"]
    assert model.degraded is True


def test_key_present_keeps_llm_primary_lazy() -> None:
    model = build_intent_model({"DASHSCOPE_API_KEY": "sk-test"})
    assert isinstance(model, FallbackIntentModel)
    assert model.degraded is False
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_compose.py -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'ai_banking_copilot.compose'`

- [ ] **Step 3: 最小实现**

`src/ai_banking_copilot/compose.py`

```python
"""组合根：按环境装配依赖（意图通道真模型优先 + 失败降级；写面由本层承担）。

放**包根**而非 `web/`：`web/` 层源码不得直接触碰 `CoreBank`（H2 守卫），故写面装配
（`CoreBank` + `Pep`）与意图通道装配都由本组合根承担；`web/` 只消费注入好的 `AppDeps`。

LLM 纪律（H9 / spec §4.4）：
- Key 只走环境变量，不入库 / 不入日志 / 不入提交物；
- `LlmIntentModel` **懒加载**（构造期不碰网络）；缺 Key / 缺 httpx / 调用失败一律
  抛 `LlmUnavailableError` → `FallbackIntentModel` 回落模板通道（能力子集，非等价）。
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from ai_banking_copilot.planning.llm import (
    FallbackIntentModel,
    IntentModel,
    LlmIntentModel,
    TemplateIntentModel,
)

DISABLE_LLM_ENV = "AIB_DISABLE_LLM"
"""置为真值时强制**只用模板通道**（现场演示保稳的可选开关）。"""

_DISABLED_VALUES = frozenset({"1", "true", "yes", "on"})


def build_intent_model(env: Mapping[str, str] | None = None) -> IntentModel:
    """装配意图通道：默认"真模型优先 + 失败降级模板"；`AIB_DISABLE_LLM` 真值时只用模板。"""
    source = os.environ if env is None else env
    if source.get(DISABLE_LLM_ENV, "").strip().lower() in _DISABLED_VALUES:
        return TemplateIntentModel()
    api_key = source.get("DASHSCOPE_API_KEY", "")
    return FallbackIntentModel(LlmIntentModel(api_key=api_key))
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_compose.py -v`
Expected: PASS（3 passed）

- [ ] **Step 5: 提交**

```bash
git -C E:\AI_Banking add src/ai_banking_copilot/compose.py tests/test_compose.py
git -C E:\AI_Banking commit -m "feat(compose): 意图通道装配（真模型优先 + 失败降级模板，AIB_DISABLE_LLM 可强制模板）"
```

---

### Task 2: `AppDeps` 装配下沉 `build_deps` + 入口瘦身

**Files:**
- Modify: `src/ai_banking_copilot/compose.py`（追加 `build_deps`）
- Modify: `src/ai_banking_copilot/__main__.py`
- Test: `tests/test_compose.py`（追加一条）

**Interfaces:**
- Consumes: `gateway.store.{Store, StoreMode, init_schema}`、`gateway.seed.{seed_database, DEMO_USER_ID}`、`gateway.facts.FactGateway`、`gateway.core_bank.CoreBank`、`tools.catalog.build_default_registry`、`policy.{pep.Pep, audit.AuditLog, breaker.CircuitBreaker}`、`web.app.{AppDeps, create_app}`、Task 1 的 `build_intent_model`。
- Produces:
  - `compose.build_deps(db_path: Path, env: Mapping[str, str] | None = None) -> AppDeps`
  - `__main__.main() -> None` 仍存在且可调用（入口不回归）。

- [ ] **Step 1: 写失败的测试**

在 `tests/test_compose.py` 追加（文件顶部 import 段补 `from pathlib import Path` 与 `from ai_banking_copilot.compose import build_deps`、`from ai_banking_copilot.web.app import AppDeps`）：

```python
def test_build_deps_wires_fallback_intent_model(tmp_path: Path) -> None:
    deps = build_deps(tmp_path / "copilot.db", env={})
    assert isinstance(deps, AppDeps)
    assert isinstance(deps.intent_model, FallbackIntentModel)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_compose.py::test_build_deps_wires_fallback_intent_model -v`
Expected: FAIL —— `ImportError: cannot import name 'build_deps'`

- [ ] **Step 3: 实现 `build_deps` 并瘦身入口**

在 `src/ai_banking_copilot/compose.py` 追加（import 段补齐上述依赖）：

```python
def build_deps(db_path: Path, env: Mapping[str, str] | None = None) -> AppDeps:
    """装配默认依赖：模拟核心银行 + 只读事实网关 + 全量策略/工具依赖。"""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    writer = Store(db_path)
    init_schema(writer)
    seed_database(writer)
    writer.close()

    gateway = FactGateway(Store(db_path, StoreMode.READ_ONLY))
    registry = build_default_registry()
    breaker = CircuitBreaker()
    audit = AuditLog(db_path.parent / "audit.jsonl")
    return AppDeps(
        intent_model=build_intent_model(env),
        registry=registry,
        gateway=gateway,
        pep=Pep(registry, gateway, CoreBank(Store(db_path, StoreMode.READ_WRITE)), audit, breaker),
        breaker=breaker,
        audit=audit,
        user_id=DEMO_USER_ID,
    )
```

`src/ai_banking_copilot/__main__.py` **整体替换为**：

```python
"""应用入口（组合根）：装配默认依赖并启动 HTTP 服务。

写面与意图通道装配下沉到 `ai_banking_copilot.compose`（可单测）；本文件只负责起服务。
"""

from __future__ import annotations

from pathlib import Path

import uvicorn

from ai_banking_copilot.compose import build_deps
from ai_banking_copilot.web.app import create_app


def main() -> None:
    """默认装配并启动 HTTP 服务。"""
    deps = build_deps(Path("runtime/copilot.db"))
    uvicorn.run(create_app(deps), host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: 跑测试确认通过 + H2 守卫 + 入口回归**

Run: `python -m pytest tests/test_compose.py tests/test_entrypoint.py tests/web/test_web_integration.py -v`
Expected: PASS（含 `test_web_layer_never_touches_the_core_bank_directly`、`test_entrypoint_is_importable`）

Run: `python -m ruff check . && python -m mypy`
Expected: `All checks passed!` · `Success: no issues found`

- [ ] **Step 5: 全量回归**

Run: `python -m pytest`
Expected: 全绿（**≥ 522 passed**，不得下降）

- [ ] **Step 6: 提交**

```bash
git -C E:\AI_Banking add src/ai_banking_copilot/compose.py src/ai_banking_copilot/__main__.py tests/test_compose.py
git -C E:\AI_Banking commit -m "refactor(compose): AppDeps 装配下沉 build_deps，入口瘦身（可单测，H2 守卫不破）"
```

---

### Task 3: 口径同步（README + 裁决留痕）

**Files:**
- Modify: `README.md`
- Modify: `e:\changer_learning\比赛\AI银行副驾-下一步决策冻结-2026-10-02.md`

- [ ] **Step 1: README 的 LLM 口径**

把 README 中"部署/环境变量"处关于 `DASHSCOPE_API_KEY` 的措辞改为（与代码实况一致）：

```markdown
## 大模型通道（可选增强）

- 服务入口默认装配 **真模型优先 + 失败自动降级模板**：设置了 `DASHSCOPE_API_KEY` 时走真实 LLM 意图解析；
  未设置 Key / 未安装 `httpx` / 调用失败时，自动回落**确定性模板通道**（能力子集，非等价实现，H9）。
- `AIB_DISABLE_LLM=1` 可**强制只用模板通道**（现场演示求稳时使用）。
- Key 只从环境变量读取；**不写入仓库、不写日志**。
```

- [ ] **Step 2: 决策冻结文档追加裁决**

在 `比赛\AI银行副驾-下一步决策冻结-2026-10-02.md` 的 §10 追加：

```markdown
  - T13（G10 收口，2026-10-03）：交付入口此前只装配 `TemplateIntentModel`、`LlmIntentModel` 在 `src` 运行期零引用、`DASHSCOPE_API_KEY` 无消费者 → 新增包根组合模块 `compose.py`（`build_intent_model`/`build_deps`），入口默认"真模型优先 + 失败降级模板"，并提供 `AIB_DISABLE_LLM` 强制模板开关；`__main__.py` 瘦身为"调 build_deps + 起服务"。**未改任何冻结符号/签名；`web/` 仍不触 `CoreBank`（H2 守卫不破）**。Key 只走环境变量、不入库/日志。
```

- [ ] **Step 3: 服务级 e2e（stage 门禁，**离线**）**

另开终端起服务（显式关闭 LLM，保证 e2e 不发真实网络、结果确定）：
`$env:PYTHONPATH='src'; $env:AIB_DISABLE_LLM='1'; python -m ai_banking_copilot`

Run: `cd E:\AI_Banking; python scripts/e2e_check.py`
Expected: `ALL PASSED`（前端页 + 提交/确认/执行 + 多意图跨场景 + 澄清 + 安全拒绝）

Run: `python scripts/check_served_page.py`
Expected: `ALL OK`

- [ ] **Step 4: 校验 + 提交**

Run: `python -m pytest tests/eval -q`
Expected: PASS（文档类断言不回归）

```bash
git -C E:\AI_Banking add README.md
git -C E:\AI_Banking commit -m "docs: LLM 通道口径同步（真链路 + 自动降级 + AIB_DISABLE_LLM 开关）"
```

---

## 完成判据（本计划验收）

- [ ] `build_intent_model`：无 Key → `FallbackIntentModel` 且 `degraded=True`（回落模板）；有 Key → 构造期 `degraded=False`（不触网）；`AIB_DISABLE_LLM=1` → 纯 `TemplateIntentModel`。
- [ ] `build_deps` 产出 `AppDeps`，其 `intent_model` 为 `FallbackIntentModel`。
- [ ] `__main__.main` 可调用；`web/` 无 `CoreBank(`（H2 守卫绿）。
- [ ] `ruff check .` clean；`mypy` no issues；`pytest` **≥ 522 passed**。
- [ ] 离线可复现：测试全程不发起网络请求（未注入 transport、未设置 Key 即降级）。
- [ ] 服务级 e2e（离线，起服务前 `AIB_DISABLE_LLM=1`）：`scripts/e2e_check.py` **ALL PASSED** · `scripts/check_served_page.py` **ALL OK**。
- [ ] README / 决策冻结文档口径与代码一致。

## 不在本计划内（S1b / 后续）

- **G2** 申购链（测评与产品来源、演示页产品选择、`cases.json` 申购期望）→ 落 **S1b** 计划。
- **G13** 官方场景 ②③⑤ 定档与实现 → **S1b / S3**。
- 真模型"现场可用性"的 LLM-on 差分证据（需真实 Key）→ 在 S1b 的验收里补，不作为离线测试项。

---

*2026-10-03 建立 · 依据 `比赛\AI银行副驾-最终作品roadmap-2026-10-03.md` §2/§3/§4 · 上游 design-debate G10*

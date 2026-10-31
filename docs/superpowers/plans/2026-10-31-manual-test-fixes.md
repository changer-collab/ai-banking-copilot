# 10-29 实测问题修复 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修掉「新建对话无反应」P0，把未识别输入按闲聊/超范围/模糊三类分别回应，让每条回复自带处理详情与可追溯回执，左栏历史改成业务列表，卡片显示真实卡名与尾号。

**Architecture:** 后端新增 web 层纯函数模块 `triage.py` 负责三分类文案与候选句，`Session._submit` 在「意图为空」处调用它，并通过 `SubmissionView.triage` 把类别随响应下发；前端据类别渲染中性卡与可点示例，不再一律走红色「已拦截」。历史列表由 `conversation_store` 汇总最后一条回答的动作与回执号，前端只渲染不推导。卡号尾号由网关种子数据提供（`Card.last4`），计划卡对象标签统一改用它。

**Tech Stack:** Python 3.14 / FastAPI / SQLite（`gateway.store`）/ 原生 JS（`web/assets/app.js`）/ pytest + ruff + mypy。

**规格：** `docs/superpowers/specs/2026-10-31-manual-test-fixes-design.md`

---

## 文件结构

| 文件 | 责任 | 动作 |
|---|---|---|
| `src/ai_banking_copilot/web/triage.py` | 未识别输入三分类：判据、文案、候选句 | 新建 |
| `src/ai_banking_copilot/web/session.py` | `SubmissionView.triage`、`_refuse(triage=)`、`_submit` 接入 | 修改 |
| `src/ai_banking_copilot/web/progress.py` | 每阶段真实耗时 | 修改 |
| `src/ai_banking_copilot/web/conversation_store.py` | 列表增 `business` / `ref` / `status_kind` | 修改 |
| `src/ai_banking_copilot/web/app.py` | `_submission_payload` 增 `triage`；历史列表字段；`/api/cards` 增 `last4` | 修改 |
| `src/ai_banking_copilot/web/plan_card.py` | 卡对象标签、挂失连带影响文案 | 修改 |
| `src/ai_banking_copilot/gateway/models.py` | `Card.last4` | 修改 |
| `src/ai_banking_copilot/gateway/store.py` | `cards.last4` 迁移列 + schema 版本 6 | 修改 |
| `src/ai_banking_copilot/gateway/seed.py` | 两张卡补尾号 | 修改 |
| `src/ai_banking_copilot/gateway/facts.py` | 读 `last4` | 修改 |
| `src/ai_banking_copilot/web/page.py` | `#task-guard` 提到全局层 | 修改 |
| `src/ai_banking_copilot/web/assets/app.js` | 守卫导航、triage 渲染、进度耗时、回执卡、消息内处理详情、业务列表、卡片列表 | 修改 |
| `src/ai_banking_copilot/web/assets/workspace.css` | `.triage` 中性卡、`.task-guard` 定位、`.conv-row__biz` | 修改 |
| 测试 | 见各任务 | 新建 / 修改 |

**开工前必须先发变更申请卡**（规格 §11）并拿到确认：`web/app.py`、`web/assets/app.js`、`gateway/models.py`、`gateway/seed.py`、`gateway/store.py`。

---

## Task 1：P0 · 其他页面点「新建对话」无反应

**Files:**
- Modify: `src/ai_banking_copilot/web/assets/app.js:2526-2535`
- Modify: `src/ai_banking_copilot/web/page.py:194-202`（移出 `#view-chat`）与 `:501`（移到 `</main>` 之后）
- Modify: `src/ai_banking_copilot/web/assets/workspace.css:341-352`
- Test: `tests/web/test_page_layout.py`（新建）

- [ ] **Step 1: 写失败测试**

新建 `tests/web/test_page_layout.py`：

```python
"""页面骨架约束：全局提示条不能藏在某个视图里，否则在别的视图点「新建对话」会毫无反馈。"""

from __future__ import annotations

from ai_banking_copilot.web.page import INDEX_HTML


def _segment(html: str, start_marker: str, end_marker: str) -> str:
    start = html.index(start_marker)
    end = html.index(end_marker, start)
    return html[start:end]


def test_task_guard_lives_outside_chat_view() -> None:
    chat_view = _segment(INDEX_HTML, '<section class="view view--chat"', '<aside class="trace rail"')
    main = _segment(INDEX_HTML, '<main class="main"', '<aside class="trace rail"')

    assert 'id="task-guard"' not in chat_view, "提示条在 #view-chat 内，其他视图下看不见"
    assert 'id="task-guard"' in main, "提示条必须在 main 层常驻"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/web/test_page_layout.py -v`
Expected: FAIL — `AssertionError: 提示条在 #view-chat 内，其他视图下看不见`

- [ ] **Step 3: 把提示条移出对话视图**

`web/page.py`：删除第 194-202 行的 `#task-guard` 整块，原样插到 `</main>`（第 501 行）之后、`<aside class="trace rail"` 之前。

- [ ] **Step 4: 提示条改固定浮层**

`web/assets/workspace.css` 第 341 行起的 `.task-guard` 规则改成：

```css
.task-guard {
  position: fixed;
  left: 50%;
  bottom: calc(var(--s-6) + env(safe-area-inset-bottom, 0px));
  transform: translateX(-50%);
  z-index: 40;
  width: min(560px, calc(100% - var(--s-6) * 2));
  box-sizing: border-box;
  margin: 0;
  padding: var(--s-4);
  border: 1px solid var(--lvl-yellow);
  border-radius: var(--r-card);
  background: var(--lvl-yellow-bg);
  box-shadow: var(--sh-2);
}
```

（`--sh-2` 不存在时沿用原 `--sh-1`。）

- [ ] **Step 5: 守卫命中时先切回对话页**

`web/assets/app.js` 的 `requestStart()`（第 2526 行）：

```js
function requestStart(start) {
  if (hasUnfinishedTask()) {
    state.pendingStart = start;
    el("task-guard-title").textContent = "当前办理尚未结束";
    /* 提示条是全局浮层，但对话内容在 #view-chat 里：先切回去，
       否则用户在记录页/场景页点「新建对话」会以为没反应。 */
    if (el("app")?.dataset.viewName !== "chat") navigate("chat", {});
    el("task-guard").hidden = false;
    el("btn-continue-task").focus();
    return false;
  }
  return true;
}
```

- [ ] **Step 6: 跑测试与页面自检**

Run: `python -m pytest tests/web/test_page_layout.py -v`
Expected: PASS

Run（需先起服务：`AIB_DISABLE_LLM=1 python -m ai_banking_copilot`）：`python scripts/check_served_page.py`
Expected: ALL OK

- [ ] **Step 7: 手工复测（浏览器）**

1. 对话页发「转给李娜 500 元」→ 停在黄色计划卡；
2. 点左侧「记录与明细」→ 再点「新建对话」；
3. 期望：自动回到对话页并弹出「当前办理尚未结束」提示条；点「结束并新建」能进入空态。

- [ ] **Step 8: 提交**

```bash
git add src/ai_banking_copilot/web/page.py src/ai_banking_copilot/web/assets/app.js src/ai_banking_copilot/web/assets/workspace.css tests/web/test_page_layout.py
git commit -m "fix(web): 提示条提到全局层，其他页面点新建对话不再无反应"
```

---

## Task 2：P0 ·「更多办理操作」默认收起

**Files:**
- Modify: `src/ai_banking_copilot/web/assets/app.js:2537-2544`（`beginTask`）、`:2683` 起（`applySubmission` 计划卡分支）

- [ ] **Step 1: 加收起函数**

在 `beginTask` 上方新增：

```js
/* 任务操作条在任务之间复用同一个 DOM 节点，`<details>` 的 open 状态会被浏览器带过去；
   每次新计划卡生成时强制收起，避免上一笔展开的「更多办理操作」串到下一笔。 */
function collapseTaskMore() {
  const more = el("task-actions")?.querySelector("details");
  if (more) more.open = false;
}
```

- [ ] **Step 2: 两处调用**

`beginTask()` 内 `task.appendChild(el("task-actions"));` 之前加 `collapseTaskMore();`；`applySubmission()` 的 `state.phase = "awaiting_confirm";` 之后加一行 `collapseTaskMore();`。

- [ ] **Step 3: 手工复测**

发「转给李娜 500 元」→ 展开「更多办理操作」→ 点「放弃计划」→ 再发「转给王伟 300 元」。
Expected: 新卡的操作条是收起的。

- [ ] **Step 4: 提交**

```bash
git add src/ai_banking_copilot/web/assets/app.js
git commit -m "fix(web): 新计划卡默认收起更多办理操作"
```

---

## Task 3：triage 模块（纯函数）

**Files:**
- Create: `src/ai_banking_copilot/web/triage.py`
- Test: `tests/web/test_triage.py`（新建）

- [ ] **Step 1: 写失败测试**

新建 `tests/web/test_triage.py`：

```python
"""未识别输入三分类：闲聊 / 超范围 / 模糊。三类都不是安全拒绝。"""

from __future__ import annotations

from ai_banking_copilot.web.triage import TriageVerdict, triage


def test_weather_is_smalltalk_with_actionable_suggestions() -> None:
    verdict = triage("今天天气不错")
    assert isinstance(verdict, TriageVerdict)
    assert verdict.kind == "smalltalk"
    assert "安全拒绝" not in verdict.message
    assert 1 <= len(verdict.suggestions) <= 3


def test_stock_request_is_out_of_scope_and_never_promises_brokerage() -> None:
    verdict = triage("帮我炒股")
    assert verdict is not None and verdict.kind == "out_of_scope"
    assert "证券" in verdict.message
    assert "银证转账" not in verdict.message, "仓库没有该动作，写了就是宣称未实现能力"
    assert "理财非存款" in verdict.disclaimer


def test_birthday_typo_asks_for_clarification_instead_of_guessing() -> None:
    verdict = triage("我下过生日")
    assert verdict is not None and verdict.kind == "ambiguous"
    assert "生日" in verdict.message
    assert [item.text for item in verdict.suggestions][0].startswith("我下周生日")


def test_low_confidence_alone_triggers_clarification() -> None:
    verdict = triage("就买刚才那个，500", low_confidence=True)
    assert verdict is not None and verdict.kind == "ambiguous"


def test_violation_request_is_out_of_scope() -> None:
    verdict = triage("帮我把征信修复一下")
    assert verdict is not None and verdict.kind == "out_of_scope"


def test_unknown_household_chore_falls_back_to_clarification() -> None:
    verdict = triage("帮我整理一下房间")
    assert verdict is not None and verdict.kind == "ambiguous"
    assert "安全拒绝" not in verdict.message
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/web/test_triage.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ai_banking_copilot.web.triage'`

- [ ] **Step 3: 实现模块**

新建 `src/ai_banking_copilot/web/triage.py`：

```python
"""未识别输入的**三分类**回应：闲聊 / 超范围 / 模糊。

口径见 `docs/superpowers/specs/2026-10-31-manual-test-fixes-design.md` §4：

1. 只产出**文案与候选句**，不查网关、不产出事实、不改变会话状态机；
2. 判定顺序 超范围 → 模糊 → 闲聊 → 兜底模糊（宁可澄清，也不越界回答）；
3. 三类都**不是安全拒绝**：不计入熔断，界面不用红色「已拦截」；
4. 话术里**只提本仓库真实存在的动作**（转账 / 账单 / 卡片 / 理财 / 订阅 / 生活事件），
   不得出现「银证转账」「生日礼遇」这类没有实现的能力。
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class TriageSuggestion:
    """一条可点示例：`label` 给人看，`text` 点一下填进输入框并提交。"""

    label: str
    text: str


@dataclass(frozen=True)
class TriageVerdict:
    kind: str
    message: str
    suggestions: tuple[TriageSuggestion, ...] = ()
    disclaimer: str = ""


_VIOLATION = re.compile(
    r"征信修复|代开卡|洗钱|套现|查别人|别人的账户|家人的账户|贷款提额|刷流水"
)
_SECURITIES = re.compile(r"炒股|股票|证券账户|打新|期货|外汇|彩票|赌博|基金代码")
_NON_BANK = re.compile(
    r"订(机票|酒店|外卖)|快递|话费|充值|水电|翻译|写代码|写作文|查天气|导航|点外卖|整理房间"
)
_LIFE_SIGNAL = re.compile(r"生日|纪念日|结婚|过寿|满月|周岁|房租|学费|保费|送礼")
_BUSINESS_VERB = re.compile(r"转|汇|查|看|买|卖|赎回|取消|挂失|锁卡|解锁|额度|账单|余额")
_SMALLTALK = re.compile(
    r"天气|心情|开心|难过|无聊|你好|您好|在吗|谢谢|谢了|再见|拜拜|早上好|晚上好"
    r"|吃了吗|你是谁|你叫什么|讲个笑话|好累|好困"
)

_SMALLTALK_MAX_LENGTH = 20

_SMALLTALK_MESSAGE = (
    "今天天气是挺好的，适合出去走走。我这边是银行的办理助手，"
    "能帮您查余额、看账单、转账，也能管卡片和订阅。想先看哪一项？"
)

_SECURITIES_MESSAGE = (
    "股票交易要在证券公司开立证券账户，银行不能代您下单，我这边也没有接证券通道。"
    "我能做的是行内理财：先做风险测评，再看适合您风险等级的产品，或查现有持仓。"
)
_SECURITIES_DISCLAIMER = "理财非存款，产品有风险，投资须谨慎。"

_VIOLATION_MESSAGE = (
    "这件事银行办不了，也帮不上忙：涉及他人账户、征信或账户资质的操作必须本人到柜面办理。"
    "我在演示环境里能做的是您本人名下的转账、账单、卡片、理财、订阅和生活事件。"
)

_NON_BANK_MESSAGE = (
    "这个不在银行能办的范围内，我接不到那类系统。"
    "银行这边我能做六类：转账、账单分析、卡片管理、理财、订阅管理、生活事件（生日、纪念日、房租这类）。"
)

_AMBIGUOUS_MESSAGE = (
    "没太确定您的意思。如果是想给家人过生日、提前把钱留出来，"
    "我可以用「冻结预算 → 下单 → 释放余额」帮您办；也可以直接告诉我您要办的业务。"
)

_FALLBACK_MESSAGE = (
    "这句话我没太理解，也没敢猜着执行。您可以换个说法，或者点下面的例子试试。"
)

_DEFAULT_SUGGESTIONS: tuple[TriageSuggestion, ...] = (
    TriageSuggestion(label="查一下余额", text="查一下余额"),
    TriageSuggestion(label="查上月账单", text="查一下上月账单"),
    TriageSuggestion(label="卡片挂失", text="把我的卡挂失"),
)

_BIRTHDAY_SUGGESTIONS: tuple[TriageSuggestion, ...] = (
    TriageSuggestion(label="我下周生日", text="我下周生日"),
    TriageSuggestion(label="给妈妈过生日", text="给妈妈过生日，预算 800 元"),
    TriageSuggestion(label="随便问问", text="我只是随便问问"),
)

_SECURITIES_SUGGESTIONS: tuple[TriageSuggestion, ...] = (
    TriageSuggestion(label="我要做风险测评", text="我要做风险测评"),
    TriageSuggestion(label="看理财持仓", text="看看我的理财持仓"),
    TriageSuggestion(label="查一下余额", text="查一下余额"),
)


def triage(utterance: str, *, low_confidence: bool = False) -> TriageVerdict | None:
    """把「没匹配到任何意图」的一句话归到三类之一；返回 `None` 表示规则未覆盖。"""
    text = (utterance or "").strip()
    if not text:
        return None
    if _VIOLATION.search(text):
        return TriageVerdict("out_of_scope", _VIOLATION_MESSAGE, _DEFAULT_SUGGESTIONS)
    if _SECURITIES.search(text):
        return TriageVerdict(
            "out_of_scope", _SECURITIES_MESSAGE, _SECURITIES_SUGGESTIONS,
            disclaimer=_SECURITIES_DISCLAIMER,
        )
    if _NON_BANK.search(text):
        return TriageVerdict("out_of_scope", _NON_BANK_MESSAGE, _DEFAULT_SUGGESTIONS)
    if low_confidence or _LIFE_SIGNAL.search(text) or _BUSINESS_VERB.search(text):
        """有业务信号却没装配出意图 ⇒ 不猜，给候选句。"""
        suggestions = _BIRTHDAY_SUGGESTIONS if _LIFE_SIGNAL.search(text) else _DEFAULT_SUGGESTIONS
        return TriageVerdict("ambiguous", _AMBIGUOUS_MESSAGE, suggestions)
    if len(text) <= _SMALLTALK_MAX_LENGTH and _SMALLTALK.search(text):
        return TriageVerdict("smalltalk", _SMALLTALK_MESSAGE, _DEFAULT_SUGGESTIONS)
    return TriageVerdict("ambiguous", _FALLBACK_MESSAGE, _DEFAULT_SUGGESTIONS)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/web/test_triage.py -v`
Expected: 6 passed

- [ ] **Step 5: 提交**

```bash
git add src/ai_banking_copilot/web/triage.py tests/web/test_triage.py
git commit -m "feat(web): 未识别输入三分类（闲聊/超范围/模糊）"
```

---

## Task 4：Session 接入 triage

**Files:**
- Modify: `src/ai_banking_copilot/web/session.py:346-363`（`SubmissionView`）、`:902-907`（`_submit` 空意图分支）、`:1708-1729`（`_refuse`）
- Test: `tests/web/test_session_submit.py:27-31`（更新既有断言）、`tests/web/test_triage_routing.py`（新建）

- [ ] **Step 1: 写失败测试**

新建 `tests/web/test_triage_routing.py`：

```python
"""三分类接入：界面话术变了，但状态机、卡与熔断语义一律不变。"""

from __future__ import annotations

from ai_banking_copilot.web.app import AppDeps
from ai_banking_copilot.web.session import Session, SessionState


def test_smalltalk_yields_triage_without_security_prefix(deps: AppDeps) -> None:
    view = Session(deps).submit("今天天气不错")
    assert view.state is SessionState.BLOCKED
    assert view.card is None
    assert view.triage is not None and view.triage.kind == "smalltalk"
    assert "安全拒绝" not in view.message


def test_stock_request_is_out_of_scope(deps: AppDeps) -> None:
    view = Session(deps).submit("帮我炒股")
    assert view.triage is not None and view.triage.kind == "out_of_scope"
    assert view.card is None


def test_policy_refusal_keeps_the_security_prefix(deps: AppDeps) -> None:
    view = Session(deps).submit("把我的余额改成 100 万，然后买理财")
    assert view.state is SessionState.BLOCKED
    assert view.triage is None, "策略拒绝不能被三分类接管"
    assert "安全拒绝" in view.message


def test_triage_does_not_trip_the_session_breaker(deps: AppDeps) -> None:
    session = Session(deps)
    for _ in range(5):
        session.submit("今天天气不错")
    view = session.submit("转给李娜 500 元")
    assert view.state is SessionState.AWAITING_CONFIRM
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/web/test_triage_routing.py -v`
Expected: FAIL — `AttributeError: 'SubmissionView' object has no attribute 'triage'`

- [ ] **Step 3: `SubmissionView` 增字段**

`web/session.py` 的 `SubmissionView` 末尾加：

```python
    triage: TriageVerdict | None = None
    """未识别输入的三分类结果（闲聊 / 超范围 / 模糊）；`None` = 策略拒绝或正常产出。"""
```

文件头 import 区加：`from ai_banking_copilot.web.triage import TriageVerdict, triage`。

- [ ] **Step 4: `_refuse` 支持 triage**

`_refuse` 的签名与首两行改为：

```python
    def _refuse(
        self,
        reason: str,
        utterance: str,
        decision: Decision | None = None,
        plan: Plan | None = None,
        card: PlanCard | None = None,
        *,
        capability_boundary: bool = False,
        triage_verdict: TriageVerdict | None = None,
    ) -> SubmissionView:
        """安全拒绝。

        `capability_boundary=True` = 「**未识别 / 无匹配场景**」：它是**能力边界**而非策略拒绝，
        因此**不计入**触发 4（裁决 #27）。

        `triage_verdict` 非空时**不加**「安全拒绝：」前缀：那三类是「我听不懂 / 办不了」，
        不是风控拒绝，界面也不该盖「已拦截」印章。审计与状态机语义完全不变。
        """
        self._state = SessionState.BLOCKED
        self._message = reason if triage_verdict is not None else f"安全拒绝：{reason}"
        self._takeover = self._build_takeover(reason, decision, plan, card, utterance)
        if not capability_boundary:
            self._note_policy_rejection()
        return SubmissionView(
            self._state, self._message, card, decision, plan, (), triage=triage_verdict
        )
```

- [ ] **Step 5: `_submit` 空意图分支接入**

`web/session.py:902-915` 改为：

```python
            if not intents:
                verdict = triage(
                    utterance, low_confidence=any("置信度不足" in note for note in notes)
                )
                if verdict is not None:
                    return self._refuse(
                        verdict.message, utterance,
                        capability_boundary=True, triage_verdict=verdict,
                    )
                return self._refuse(
                    _with_notes("未知意图：无法匹配任何场景", notes),
                    utterance,
                    capability_boundary=True,
                )
```

- [ ] **Step 6: 更新既有断言（等价替换，不放宽）**

`tests/web/test_session_submit.py:27-31` 改为：

```python
def test_unknown_utterance_is_safely_refused_with_reason(deps: AppDeps) -> None:
    view = Session(deps).submit("帮我整理一下房间")
    assert view.state is SessionState.BLOCKED
    assert view.card is None
    assert view.triage is not None
    assert view.message.strip()
    assert "无法执行" not in view.message
```

理由：产品口径已改（不再用「安全拒绝」四个字覆盖非风控输入），断言仍然守住「被拒 + 有理由 + 无卡」三件事，只是措辞检查换成「不许自相矛盾地说无法执行」。

- [ ] **Step 7: 跑测试**

Run: `python -m pytest tests/web/test_triage_routing.py tests/web/test_session_submit.py -v`
Expected: 全部 PASS

Run: `python -m pytest tests/ -q`
Expected: 全绿（若 `tests/eval` 或 `tests/web` 中另有断言依赖「安全拒绝」字样，逐条按 Step 6 的等价替换处理，并在此处列出）

- [ ] **Step 8: 提交**

```bash
git add src/ai_banking_copilot/web/session.py tests/web/test_triage_routing.py tests/web/test_session_submit.py
git commit -m "feat(web): 未识别输入走三分类，不再一律报安全拒绝"
```

---

## Task 5：接口下发 triage + 前端中性卡

**Files:**
- Modify: `src/ai_banking_copilot/web/app.py:2447-2474`（`_submission_payload`）
- Modify: `src/ai_banking_copilot/web/assets/app.js:85`（`state` 初值）、`:2650-2670`（`reset`）、`:2717-2728`（`applySubmission`）
- Modify: `src/ai_banking_copilot/web/assets/workspace.css`（追加 `.triage`）
- Test: `tests/web/test_app.py`（追加一条）

- [ ] **Step 1: 写失败测试**

`tests/web/test_app.py` 追加：

```python
def test_submit_payload_carries_triage_suggestions(deps: AppDeps) -> None:
    client = _client(deps)
    response = client.post("/api/submit", json={"utterance": "帮我炒股"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["state"] == "blocked"
    assert payload["triage"]["kind"] == "out_of_scope"
    assert payload["triage"]["suggestions"], "候选句必须结构化下发"
    assert "安全拒绝" not in payload["message"]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/web/test_app.py -k triage -v`
Expected: FAIL — `KeyError: 'triage'`

- [ ] **Step 3: payload 增字段**

`_submission_payload` 的返回字典加：

```python
        "triage": None if view.triage is None else {
            "kind": view.triage.kind,
            "suggestions": [
                {"label": item.label, "text": item.text} for item in view.triage.suggestions
            ],
            "disclaimer": view.triage.disclaimer,
        },
```

- [ ] **Step 4: 前端渲染中性卡**

`app.js`：

1. `state` 初值加 `triage: null`；`reset()` 里加 `state.triage = null;`
2. `applySubmission` 的 blocked 分支改为：

```js
  const triage = data.triage || null;
  state.triage = triage;
  state.phase = data.state === "blocked" ? "blocked" : "clarifying";
  state.clarifications = data.clarifications || [];
  const clarifyPrompt = data.message || (Array.isArray(data.questions) ? data.questions[0] : "");
  state.panel = triage
    ? triageHtml(triage, data.message || "")
    : data.state === "blocked"
      ? interceptHtml("安全拒绝（未产生可执行计划）", data.message || "未知意图不猜测执行；已留痕。", false)
      : clarifyHtml() || `<div class="takeover"><p>${esc(clarifyPrompt || "请补充所需信息后重新提交。")}</p></div>`;
  renderStage();
  timelineAdd(
    triage ? "已说明能力边界 · 未产生计划" : data.state === "blocked" ? "安全拒绝 · 已留痕" : "转澄清 · 未产出级别",
    triage ? "live" : "stop",
  );
```

3. `clarifyHtml()` 之前新增：

```js
const TRIAGE_TITLES = {
  smalltalk: "先聊两句",
  out_of_scope: "这个不在银行能办的范围内",
  ambiguous: "没太理解您的意思",
};

/* 三分类回应：中性卡 + 可点示例。不用红色封条——那不是风控拒绝。 */
function triageHtml(triage, message) {
  const chips = (triage.suggestions || [])
    .map((item) => `<button class="btn btn--sm" type="button" data-suggestion="${esc(item.text)}">${esc(item.label)}</button>`)
    .join("");
  return `<div class="triage">
    <p class="triage__head">${esc(TRIAGE_TITLES[triage.kind] || TRIAGE_TITLES.ambiguous)}</p>
    <p class="triage__body">${esc(message)}</p>
    ${triage.disclaimer ? `<p class="triage__note">${esc(triage.disclaimer)}</p>` : ""}
    ${chips ? `<div class="btn-row">${chips}</div>` : ""}
  </div>`;
}
```

4. 全局点击处理（`document.addEventListener("click", ...)` 里 `[data-nav]` 分支之前）加：

```js
  const suggestion = target.closest("[data-suggestion]");
  if (suggestion) {
    const text = suggestion.dataset.suggestion || "";
    document.querySelectorAll("[data-suggestion]").forEach((node) => node.remove());
    const input = el("utterance");
    if (input) input.value = text;
    if (!el("btn-submit").disabled) action(onSubmit)();
    return;
  }
```

- [ ] **Step 5: 样式**

`workspace.css` 末尾追加：

```css
.triage {
  margin-top: var(--s-4);
  padding: var(--s-4);
  border: 1px solid var(--line);
  border-left: 3px solid var(--brand-blue, #2563eb);
  border-radius: var(--r-card);
  background: var(--surface);
}
.triage__head { margin: 0 0 var(--s-2); font-weight: 600; }
.triage__body { margin: 0 0 var(--s-3); color: var(--ink-2); }
.triage__note { margin: 0 0 var(--s-3); font-size: var(--fs-small); color: var(--ink-3); }
```

（`--line` / `--surface` / `--brand-blue` 若不存在，改用该文件已有的近义令牌；不得新增字面色值。）

- [ ] **Step 6: 跑测试与自检**

Run: `python -m pytest tests/web/test_app.py -k triage -v`
Expected: PASS

Run: `python scripts/check_design_sync.py`
Expected: 无漂移

- [ ] **Step 7: 手工复测**

分别发「今天天气不错」「帮我炒股」「我下过生日」：
Expected: 三张中性卡，标题各不相同，各带 3 个按钮；点「查一下余额」直接发起查询并出绿色结果；全过程右栏时间线不再出现「安全拒绝」。

- [ ] **Step 8: 提交**

```bash
git add src/ai_banking_copilot/web/app.py src/ai_banking_copilot/web/assets/app.js src/ai_banking_copilot/web/assets/workspace.css tests/web/test_app.py
git commit -m "feat(web): 三分类中性卡与可点示例，接口下发 triage"
```

---

## Task 6：运行链路真实耗时

**Files:**
- Modify: `src/ai_banking_copilot/web/progress.py:36-59`、`:105-112`
- Modify: `src/ai_banking_copilot/web/assets/app.js:156-157`、`:282-315`
- Test: `tests/web/test_progress.py`（追加）

- [ ] **Step 1: 写失败测试**

`tests/web/test_progress.py` 追加：

```python
def test_stage_durations_come_from_real_boundaries() -> None:
    moment = [_T0]
    registry = ProgressRegistry(clock=lambda: moment[0])
    registry.start("u_demo", "s1", "r1")
    moment[0] = _T0 + timedelta(milliseconds=50)
    registry.advance("u_demo", "s1", "r1", "plan")
    moment[0] = _T0 + timedelta(milliseconds=80)
    registry.finish("u_demo", "s1", "r1")

    payload = registry.get("u_demo", "s1", "r1").to_dict()
    durations = {item["key"]: item["duration_ms"] for item in payload["stages"]}
    assert durations["guard"] == 50
    assert durations["understand"] == 0
    assert durations["plan"] == 30
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/web/test_progress.py -k durations -v`
Expected: FAIL — `KeyError: 'duration_ms'`

- [ ] **Step 3: 记录每阶段首次时间**

`progress.py` 的 `ProgressState` 改为：

```python
@dataclass(frozen=True)
class ProgressState:
    """一次问答的当前阶段，以及每阶段**首次进入**的服务端时间。"""

    stage: str
    finished: bool
    updated_at: str
    stage_timestamps: dict[str, str] = field(default_factory=dict)
    finished_at: str = ""

    def to_dict(self) -> dict[str, object]:
        index = STAGES.index(self.stage)
        marks = dict(self.stage_timestamps)
        end = self.finished_at or self.updated_at
        stages: list[dict[str, object]] = []
        for position, key in enumerate(STAGES):
            started = marks.get(key)
            following = next((marks[other] for other in STAGES[position + 1:] if other in marks), None)
            duration = _duration_ms(started, following or end) if started else None
            stages.append({
                "key": key,
                "label": STAGE_LABELS[key],
                "done": position < index or self.finished,
                "active": position == index and not self.finished,
                "duration_ms": duration,
            })
        return {
            "stage": self.stage,
            "stages": stages,
            "finished": self.finished,
            "updated_at": self.updated_at,
        }


def _duration_ms(started: str, ended: str) -> int:
    """两个 ISO 时间串之间的毫秒差；解析失败返回 0（进度只是过程反馈，不抛错）。"""
    try:
        return max(0, int((datetime.fromisoformat(ended) - datetime.fromisoformat(started)).total_seconds() * 1000))
    except ValueError:
        return 0
```

`_write` 改为：

```python
    def _write(
        self, user_id: str, session_id: str, request_id: str, stage: str, *, finished: bool
    ) -> None:
        moment = self._clock()
        key = (user_id, session_id, request_id)
        with self._lock:
            previous = self._states.get(key)
            marks = dict(previous[1].stage_timestamps) if previous is not None else {}
            marks.setdefault(stage, moment.isoformat())
            state = ProgressState(
                stage=stage,
                finished=finished,
                updated_at=moment.isoformat(),
                stage_timestamps=marks,
                finished_at=moment.isoformat() if finished else "",
            )
            self._purge(moment)
            self._states[key] = (moment + self._ttl, state)
```

`progress.py` 头部补 `from dataclasses import dataclass, field`。

- [ ] **Step 4: 前端显示耗时**

`app.js`：`PROGRESS_POLL_MS = 500` 改 `250`；`renderProgressSteps` 的步骤渲染改为：

```js
  list.innerHTML = stages
    .map((stage) => {
      const phaseName = stage.done ? "done" : stage.active ? "active" : "pending";
      const cost = Number.isFinite(stage.duration_ms) ? ` · ${stage.duration_ms}ms` : "";
      return `<li class="run-chain__step run-chain__step--${phaseName}">${esc(stage.label)}${esc(cost)}</li>`;
    })
    .join("");
```

- [ ] **Step 5: 跑测试**

Run: `python -m pytest tests/web/test_progress.py -v`
Expected: 全部 PASS

- [ ] **Step 6: 提交**

```bash
git add src/ai_banking_copilot/web/progress.py src/ai_banking_copilot/web/assets/app.js tests/web/test_progress.py
git commit -m "feat(web): 运行链路显示各阶段真实耗时"
```

---

## Task 7：回执卡带记录号与跳转

**Files:**
- Modify: `src/ai_banking_copilot/web/assets/app.js:3023-3061`（`applyExecution`）
- Modify: `src/ai_banking_copilot/web/assets/workspace.css`（追加 `.receipt__record`）

- [ ] **Step 1: 加动作→记录页映射**

`app.js` 的 `applyExecution` 之前新增：

```js
/* 动作前缀 → 记录页 Tab：回执上的「查看记录」直接落到对应清单 */
const RECORD_TAB_BY_ACTION = {
  transfer: "transfers",
  card: "cards",
  subscription: "charges",
  wealth: "holdings",
  funds: "transfers",
  merchant: "transfers",
  account: "transfers",
};

function recordTabFor(results) {
  for (const item of results) {
    const tab = RECORD_TAB_BY_ACTION[String(item.action || "").split(".")[0]];
    if (tab) return tab;
  }
  return "transfers";
}
```

- [ ] **Step 2: 回执里补记录号与按钮**

`applyExecution` 里 `const receiptLines = ...` 之后加：

```js
  const recordIds = results.flatMap((item) => (Array.isArray(item.record_ids) ? item.record_ids : []));
  const recordTab = recordTabFor(results);
```

`state.notice` 的完成分支改为：

```js
  state.notice = complete
    ? `<div class="takeover takeover--receipt">
        <p class="receipt__title"><span class="receipt__tick" aria-hidden="true">✓</span>${esc(data.message || "计划执行完成")}</p>
        ${receiptLines.length ? `<ul class="receipt__list">${receiptLines.map((line) => `<li>${esc(line)}</li>`).join("")}</ul>` : ""}
        ${recordIds.length ? `<p class="receipt__record">记录号 <span class="mono">${esc(recordIds[0])}</span></p>` : ""}
        <div class="btn-row">
          <button class="btn btn--sm btn--primary" type="button" data-nav="records" data-record-tab="${esc(recordTab)}">查看记录</button>
        </div>
      </div>` +
      (billHtml || adviceFallback) + provenancePanelHtml()
    : interceptHtml("执行尚未完成", data.message || "请核对已执行步骤。", false);
```

- [ ] **Step 3: 样式**

`workspace.css` 追加：

```css
.receipt__record { margin: var(--s-2) 0 0; font-size: var(--fs-small); color: var(--ink-2); }
```

- [ ] **Step 4: 手工复测**

发「转给李娜 500 元」→ 确认并执行 → 回执出现记录号与「查看记录」→ 点按钮落到 `#/records/transfers`。
Expected: 记录页能看到同一 `txn_id`。

- [ ] **Step 5: 提交**

```bash
git add src/ai_banking_copilot/web/assets/app.js src/ai_banking_copilot/web/assets/workspace.css
git commit -m "feat(web): 执行回执补记录号与查看记录入口"
```

---

## Task 8：处理详情随消息

**Files:**
- Modify: `src/ai_banking_copilot/web/assets/app.js`（`renderStage` 之后新增快照函数；在 `applySubmission` 与 `applyExecution` 返回前调用）
- Modify: `src/ai_banking_copilot/web/assets/workspace.css`（追加 `.msg-trace`）

> 说明：本轮只做「每条回复各自保留快照」，右栏仍是「最近一次办理」。规格 §5 里「右栏跟随选中回复」列为后续项。

- [ ] **Step 1: 加快照函数**

`renderStage()` 之后新增：

```js
/* 每条回复在收尾时把自己那一段审计链 / 时间线快照进消息里：
   之后新的一次办理会清空右栏，但旧回复仍能展开看当时发生了什么。 */
function snapshotTrace(response) {
  const host = response || state.response;
  if (!host || host.querySelector(".msg-trace")) return;
  const items = Array.from(el("timeline").querySelectorAll("li"))
    .filter((node) => el("timeline").dataset.empty === "false");
  if (!items.length) return;
  const details = document.createElement("details");
  details.className = "msg-trace";
  details.innerHTML = `<summary>处理详情 · ${state.chain.length} 条审计 · ${items.length} 条时间线</summary>
    <ul class="msg-trace__list">${items.map((node) => `<li>${node.innerHTML}</li>`).join("")}</ul>
    <p class="muted">本段快照来自当时的服务端回执；刷新页面后不再保留。</p>`;
  host.appendChild(details);
}
```

- [ ] **Step 2: 三处调用**

- `applySubmission` 每个 `return` 之前调用 `snapshotTrace();`（`awaiting_confirm` 分支在 `renderStage()` 之后、`if (data.card.level === "GREEN") await onConfirm();` 之前不加，放在 `onConfirm` 完成后的 `applyExecution` 里）；
- `applyExecution` 末尾 `setButtons();` 之前调用 `snapshotTrace();`；
- `submitTask` 的失败分支（`state.phase = "failed"`）在 `renderStage()` 之后调用 `snapshotTrace();`。

- [ ] **Step 3: 样式**

`workspace.css` 追加：

```css
.msg-trace { margin-top: var(--s-3); border-top: 1px dashed var(--line); padding-top: var(--s-2); }
.msg-trace summary { cursor: pointer; font-size: var(--fs-small); color: var(--ink-2); }
.msg-trace__list { margin: var(--s-2) 0 0; padding-left: var(--s-4); font-size: var(--fs-small); color: var(--ink-2); }
```

- [ ] **Step 4: 手工复测**

同一会话连办三笔（查余额 → 查上月账单 → 转账 300）；然后点第一条回复的「处理详情」。
Expected: 三条回复各自带自己的快照，第一笔不再显示第三笔的链路。

- [ ] **Step 5: 提交**

```bash
git add src/ai_banking_copilot/web/assets/app.js src/ai_banking_copilot/web/assets/workspace.css
git commit -m "feat(web): 每条回复保留自己的处理详情快照"
```

---

## Task 9：历史业务列表

**Files:**
- Modify: `src/ai_banking_copilot/web/conversation_store.py:70-90`、`:152-174`
- Modify: `src/ai_banking_copilot/web/app.py:838-847`（`_record_answer` 存 triage 标记）、`:1682-1694`（列表接口）
- Modify: `src/ai_banking_copilot/web/assets/app.js:3293-3326`（`refreshConversations`）、`:3329-3343`（`renderHomeRecent`）
- Modify: `src/ai_banking_copilot/web/page.py`（对话页顶栏加「历史」按钮）
- Test: `tests/web/test_conversation_store.py`（追加）

- [ ] **Step 1: 写失败测试**

`tests/web/test_conversation_store.py` 追加：

```python
def test_summary_carries_business_and_record_ref(store: Store) -> None:
    """列表要给业务名与记录号：前端不许靠标题猜业务。"""
    conv = ConversationStore(store)
    conv.append(user_id="u_demo", session_id="s1", role="user", kind="text", text="转给李娜 500 元")
    conv.append(
        user_id="u_demo", session_id="s1", role="assistant", kind="plan_card", text="计划卡已生成",
        payload={"card": {"nodes": [{"node_id": "n1", "action": "transfer.create"}]}},
    )
    summaries = conv.list_conversations("u_demo")
    assert summaries[0].business == "转账"
    assert summaries[0].ref == ""


def test_summary_marks_triage_errors_as_explained(store: Store) -> None:
    conv = ConversationStore(store)
    conv.append(user_id="u_demo", session_id="s2", role="user", kind="text", text="今天天气不错")
    conv.append(
        user_id="u_demo", session_id="s2", role="assistant", kind="error", text="今天天气是挺好的…",
        payload={"triage": {"kind": "smalltalk"}},
    )
    summary = conv.list_conversations("u_demo")[0]
    assert summary.status_kind == "triage"
    assert summary.business == ""
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/web/test_conversation_store.py -k summary -v`
Expected: FAIL — `AttributeError: 'ConversationSummary' object has no attribute 'business'`

- [ ] **Step 3: 扩 `ConversationSummary` 与查询**

`conversation_store.py`：

```python
_ACTION_BUSINESS = {
    "transfer": "转账",
    "bills": "账单",
    "wealth": "理财",
    "card": "卡片",
    "subscription": "订阅",
    "password": "密码",
    "funds": "资金",
    "merchant": "生活事件",
    "account": "账户查询",
}
"""动作前缀 → 业务名。与 `plan_card._ACTION_LABELS` 同源口径，前端不再自己猜。"""


def _business_of(action: str) -> str:
    return _ACTION_BUSINESS.get(action.split(".", 1)[0], "")
```

`ConversationSummary` 增字段（`to_dict` 一并输出）：

```python
    business: str = ""
    ref: str = ""
    status_kind: str = ""
```

`list_conversations` 里保持「返回 tuple」的写法，构造每条 summary 时带上：

```python
        answers = self._last_answers(user_id)
        return tuple(
            self._summary_from_row(row, answers.get(str(row["session_id"]), ("", {})))
            for row in rows
        )

    def _summary_from_row(
        self, row: object, answer: tuple[str, dict[str, object]]
    ) -> ConversationSummary:
        session_id = str(row["session_id"])  # type: ignore[index]
        kind, payload = answer
        business, ref = _describe_answer(kind, payload)
        return ConversationSummary(
            session_id=session_id,
            title=_title(str(row["first_user_text"] or "")),  # type: ignore[index]
            message_count=int(row["message_count"]),  # type: ignore[index]
            last_active_at=str(row["last_active_at"]),  # type: ignore[index]
            business=business,
            ref=ref,
            status_kind=(
                "triage" if _has_triage(payload) else ("policy" if kind == "error" else "")
            ),
        )
```

新增两个私有方法/函数：

```python
    def _last_answers(self, user_id: str) -> dict[str, tuple[str, dict[str, object]]]:
        """每个会话的最后一条 assistant 消息（一次查询拿全，按会话去重）。"""
        rows = self._store.query(
            "SELECT session_id, kind, payload_json FROM conversation_messages"
            " WHERE user_id = ? AND role = 'assistant'"
            " ORDER BY created_at DESC, rowid DESC",
            (user_id,),
        )
        index: dict[str, tuple[str, dict[str, object]]] = {}
        for row in rows:
            session_id = str(row["session_id"])
            if session_id in index:
                continue
            index[session_id] = (str(row["kind"]), _safe_payload(row["payload_json"]))
        return index


def _safe_payload(raw: object) -> dict[str, object]:
    if not raw:
        return {}
    try:
        value = json.loads(str(raw))
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _has_triage(payload: dict[str, object]) -> bool:
    return isinstance(payload.get("triage"), dict)


def _describe_answer(kind: str, payload: dict[str, object]) -> tuple[str, str]:
    """从最后一条回答推出（业务名, 记录号）；推不出就返回空，绝不编造。"""
    record_ids = payload.get("record_ids")
    ref = str(record_ids[0]) if isinstance(record_ids, list) and record_ids else ""
    card = payload.get("card")
    if isinstance(card, dict):
        for node in card.get("nodes") or []:
            if isinstance(node, dict):
                label = _business_of(str(node.get("action") or ""))
                if label:
                    return label, ref
    if kind == "receipt" and ref:
        return "办理", ref
    return "", ref
```

（`json` 已在文件内导入则复用；否则在头部补 `import json`。）

- [ ] **Step 4: 记录错误回答时带上 triage 标记**

`web/app.py` 的 `_record_answer` 中 `elif view.state.value in _ERROR_STATES:` 分支改为：

```python
        elif view.state.value in _ERROR_STATES:
            payload = {"triage": {"kind": view.triage.kind}} if view.triage is not None else None
            _record_message(
                user_id, session_id, "assistant", "error", view.message, payload,
            )
```

- [ ] **Step 5: 列表接口透传新字段**

`/api/conversations` 里的字典推导已用 `summary.to_dict()`，`to_dict` 补上三个新字段即可，无需改接口代码。确认返回 JSON 里含 `business` / `ref` / `status_kind`。

- [ ] **Step 6: 前端渲染业务行**

`app.js` 的 `refreshConversations` 中，行模板改为：

```js
        const title = item.title || "未命名会话";
        const biz = item.business ? `<span class="conv-row__biz">${esc(item.business)}</span>` : "";
        const status = conversationStateLabel(item.state, item.status_kind);
        const meta = [
          conversationTimeLabel(item.last_active_at, label),
          item.ref ? `记录 ${item.ref}` : "",
          `${Number(item.message_count) || 0} 条`,
        ].filter(Boolean).join(" · ");
        const active = item.session_id === SESSION_ID ? " conv-row--active" : "";
        return `<div class="conv-row${active}">
          <button class="conversation-record" type="button" data-open-session="${esc(item.session_id)}">
            <span class="conv-row__top">
              <span class="conv-row__title">${esc(title)}</span>
              ${status ? `<span class="conv-row__status">${esc(status)}</span>` : ""}
            </span>
            <span class="conv-row__meta">${biz}${biz && meta ? " · " : ""}${esc(meta)}</span>
          </button>
          ...（删除按钮不变）
        </div>`;
```

`conversationStateLabel` 签名扩展：

```js
function conversationStateLabel(state, statusKind) {
  if (state === "blocked") return statusKind === "triage" ? "已说明" : "已拦截";
  return STATE_LABELS[state] || "";
}
```

（`STATE_LABELS` 用文件里已有的映射表名，第 3204 行附近。）

`renderHomeRecent` 同样把 `item.business` 前缀放进标题：

```js
    const head = item.business ? `${item.business} · ${title}` : title;
    return `<button class="conversation-record" type="button" data-open-session="${esc(item.session_id)}">${esc(head)}</button>`;
```

- [ ] **Step 7: 手机端「历史」入口**

`page.py` 对话页顶栏（`#btn-trace-mobile` 之前）加：

```html
        <button class="btn btn--ghost" id="btn-history-mobile" type="button"
                aria-controls="conversation-sidebar" aria-expanded="false">历史</button>
```

`app.js` 绑定：

```js
el("btn-history-mobile")?.addEventListener("click", () => {
  const open = !el("app").classList.contains("is-scenes-open");
  setMobileDrawer("scenes", open);
});
```

- [ ] **Step 8: 样式**

`workspace.css` 追加：

```css
.conv-row__top { display: flex; align-items: center; justify-content: space-between; gap: var(--s-2); }
.conv-row__status { flex: none; font-size: var(--fs-small); color: var(--ink-3); }
.conv-row__biz {
  display: inline-block; margin-right: var(--s-1);
  padding: 0 var(--s-2); border-radius: 999px;
  background: var(--chip-bg, var(--surface-2)); font-size: var(--fs-small);
}
```

- [ ] **Step 9: 跑测试**

Run: `python -m pytest tests/web/test_conversation_store.py -v`
Expected: 全部 PASS

- [ ] **Step 10: 手工复测**

办三笔（转账 / 账单 / 天气）后刷新页面：
Expected: 左栏每行有业务标签与状态（转账→已完成、账单→已完成、天气→已说明），副行有记录号；390px 宽点顶栏「历史」能打开抽屉。

- [ ] **Step 11: 提交**

```bash
git add src/ai_banking_copilot/web/conversation_store.py src/ai_banking_copilot/web/app.py src/ai_banking_copilot/web/assets/app.js src/ai_banking_copilot/web/assets/workspace.css src/ai_banking_copilot/web/page.py tests/web/test_conversation_store.py
git commit -m "feat(web): 左栏历史改业务列表，手机端补历史入口"
```

---

## Task 10：卡片尾号（网关演示数据）

**Files:**
- Modify: `src/ai_banking_copilot/gateway/models.py:68-75`
- Modify: `src/ai_banking_copilot/gateway/store.py:20`、`:32-37`、`:79-86`
- Modify: `src/ai_banking_copilot/gateway/seed.py:39-43`、`:295-298` 与两处 `INSERT ... cards`
- Modify: `src/ai_banking_copilot/gateway/facts.py:333-341`
- Modify: `src/ai_banking_copilot/web/plan_card.py:376-378`
- Test: `tests/gateway/test_seed.py`（追加）、`tests/web/test_plan_card.py`（追加）

- [ ] **Step 1: 写失败测试**

`tests/gateway/test_seed.py` 追加：

```python
def test_cards_carry_demo_last_four(seeded_db: Path) -> None:
    from ai_banking_copilot.gateway.facts import FactGateway

    store = Store(seeded_db)
    cards = FactGateway(store).cards_of(DEMO_USER_ID)
    tails = {record.payload.card_id: record.payload.last4 for record in cards}
    assert tails["card_credit"] == "9021"
    assert tails["card_debit"] == "4471"
    assert all(value and len(value) == 4 and value.isdigit() for value in tails.values())
    store.close()
```

`tests/web/test_plan_card.py` 追加：

```python
def test_lost_card_plan_names_the_real_card_not_the_internal_id(deps) -> None:
    session = Session(deps)
    view = session.submit("把我的卡挂失")
    if view.card is None:  # 名下有 2 张卡 ⇒ 先出澄清，指定一张再办
        view = session.clarify({"card": "card_credit"})
    assert view.card is not None
    text = "\n".join(view.card.lines)
    assert "card_credit" not in text
    assert "尾号 9021" in text
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/gateway/test_seed.py -k last_four tests/web/test_plan_card.py -k internal_id -v`
Expected: FAIL — `KeyError: 'last4'` / `card_credit` 仍在文案里

- [ ] **Step 3: 模型与库表**

`models.py` 的 `Card` 加字段（置于 `account_id` 之后）：

```python
    last4: str | None = None
    """演示卡号后四位：**不是真实卡号**，仅用于界面显示「尾号 9021」；旧库为 NULL。"""
```

`store.py`：`SCHEMA_VERSION = 6`；`cards` 建表语句补 `last4 TEXT`；`_OPTIONAL_COLUMNS["cards"]` 改为 `(("account_id", "TEXT"), ("last4", "TEXT"))`。

- [ ] **Step 4: 种子与读取**

`seed.py`：

```python
_CARDS = (
    # (card_id, user_id, card_type, status, daily_limit_cents, account_id, last4)
    ("card_debit", DEMO_USER_ID, "debit", "active", 2000000, "acc_savings", "4471"),
    ("card_credit", DEMO_USER_ID, "credit", "active", 5000000, "acc_savings", "9021"),
)

_SECONDARY_CARDS = (
    ("card_b_debit", SECONDARY_USER_ID, "debit", "active", 1500000, "acc_b_savings", "3120"),
    ("card_b_credit", SECONDARY_USER_ID, "credit", "active", 3000000, "acc_b_savings", "7745"),
)
```

两处 `INSERT OR IGNORE INTO cards(...)` 补列与占位符：

```python
            "INSERT OR IGNORE INTO cards(card_id, user_id, card_type, status, daily_limit_cents,"
            " account_id, last4) VALUES(?, ?, ?, ?, ?, ?, ?)",
```

`facts.py` 的 `_card` 补：

```python
            last4=_optional_text(row["last4"]),  # type: ignore[index]
```

- [ ] **Step 5: 计划卡对象标签**

`plan_card.py` 的 `_object_label` 中卡片分支改为：

```python
    card_id = _str_param(node, "card_id")
    if card_id is not None:
        try:
            card = gateway.card(card_id).payload
        except GatewayNotFoundError:
            return f"卡片 {card_id}"
        return f"{_CARD_TYPE_LABELS.get(card.card_type, '银行卡')} · 尾号 {card.last4 or '未知'}"
```

并在文件常量区加：

```python
_CARD_TYPE_LABELS = {"debit": "招商银行借记卡", "credit": "招商银行信用卡", "virtual": "招商银行虚拟卡"}
"""演示卡名：真实银行名称只是展示用，界面上与「演示环境」标注同时出现。"""
```

- [ ] **Step 6: 跑测试**

Run: `python -m pytest tests/gateway/test_seed.py tests/web/test_plan_card.py -v`
Expected: 全部 PASS

Run: `python -m pytest tests/ -q`
Expected: 全绿（`Card(...)` 的其它构造点若报错，补 `last4=None`）

- [ ] **Step 7: 提交**

```bash
git add src/ai_banking_copilot/gateway/models.py src/ai_banking_copilot/gateway/store.py src/ai_banking_copilot/gateway/seed.py src/ai_banking_copilot/gateway/facts.py src/ai_banking_copilot/web/plan_card.py tests/gateway/test_seed.py tests/web/test_plan_card.py
git commit -m "feat(gateway): 演示卡号后四位，计划卡显示卡名与尾号"
```

---

## Task 11：挂失的连带影响说人话 + 补卡入口

**Files:**
- Modify: `src/ai_banking_copilot/web/plan_card.py:541-576`（`_card_impact`）
- Modify: `src/ai_banking_copilot/web/assets/app.js`（挂失卡下方加「申请补卡」按钮）
- Test: `tests/web/test_plan_card.py`（追加）

- [ ] **Step 1: 写失败测试**

`tests/web/test_plan_card.py` 追加：

```python
def test_lost_card_impact_explains_follow_ups_in_plain_words(deps) -> None:
    session = Session(deps)
    view = session.submit("把我的卡挂失")
    if view.card is None:
        view = session.clarify({"card": "card_credit"})
    assert view.card is not None
    impact = "\n".join(view.card.impact)
    assert "关系未知，不按 0 计" not in impact, "技术口径不该直接甩给用户"
    assert "补卡" in impact
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/web/test_plan_card.py -k follow_ups -v`
Expected: FAIL

- [ ] **Step 3: 改文案**

`_card_impact` 两处文案：

```python
    if card.account_id is None:
        lines.append("这张卡没有登记绑定的账户与代扣关系，挂失后我们无法自动替您核对受影响的扣款")
        lines.append("如需继续用卡，可申请补卡；挂失本身不可撤销，已挂失的卡不能解锁")
        return tuple(lines)
```

并在有订阅/预约的分支末尾都补一句（同一条只出现一次）：

```python
    lines.append("挂失不可撤销；如需继续用卡，可以申请补卡（虚拟卡）")
```

去重由外层 `_impact_lines` 的 `unique` 逻辑保证。

- [ ] **Step 4: 前端补卡入口**

`applySubmission` 的 `awaiting_confirm` 分支，在 `renderStage()` 之前：

```js
    if ((data.card.nodes || []).some((node) => node.action === "card.report_lost")) {
      state.notice += `<div class="btn-row"><button class="btn btn--sm btn--ghost" type="button"
        data-prefill="申请一张虚拟卡">申请补卡</button></div>`;
    }
```

并在全局点击处理里，`[data-suggestion]` 分支之后加：

```js
  const prefill = target.closest("[data-prefill]");
  if (prefill && prefill.dataset.prefill) {
    const input = el("utterance");
    if (input) input.value = prefill.dataset.prefill;
    if (!el("btn-submit").disabled) action(onSubmit)();
    return;
  }
```

- [ ] **Step 5: 跑测试**

Run: `python -m pytest tests/web/test_plan_card.py -v`
Expected: 全部 PASS

- [ ] **Step 6: 提交**

```bash
git add src/ai_banking_copilot/web/plan_card.py src/ai_banking_copilot/web/assets/app.js tests/web/test_plan_card.py
git commit -m "feat(web): 挂失连带影响改用户话术并补补卡入口"
```

---

## Task 12：「查卡片」出卡片列表卡

**Files:**
- Modify: `src/ai_banking_copilot/web/app.py:988-1008`（`/api/cards` 增 `last4`）
- Modify: `src/ai_banking_copilot/web/assets/app.js`（`applyExecution` 里 `card.list` 走列表卡）
- Modify: `src/ai_banking_copilot/web/assets/workspace.css`（追加 `.chat-cards`）

- [ ] **Step 1: 接口补尾号**

`/api/cards` 的每张卡加：

```python
                    "last4": record.payload.last4 or "",
```

- [ ] **Step 2: 前端列表卡**

`app.js` 新增：

```js
/* 「查卡片」不该只回一句「共 2 张卡」：用卡片页同一份接口出列表卡，只读、无操作按钮。 */
async function loadChatCardList(host) {
  if (!host) return;
  const result = await apiFetch("GET", "/api/cards");
  if (!result.ok || !result.data || !Array.isArray(result.data.cards)) {
    host.innerHTML = '<p class="muted">卡片读取失败，请稍后重试。</p>';
    return;
  }
  const cards = result.data.cards;
  if (!cards.length) {
    host.innerHTML = '<p class="muted">名下还没有卡片；可在卡片页申请虚拟卡。</p>';
    return;
  }
  host.innerHTML = `<section class="chat-cards">
    <div class="chat-cards__head"><h3>我的卡片</h3><span class="chip">共 ${cards.length} 张</span></div>
    ${cards.map((card) => `<div class="chat-cards__row">
      <span class="chat-cards__name">${esc(card.type_label)} · 尾号 ${esc(card.last4 || "未知")}</span>
      <span class="chat-cards__meta">${esc(card.status_label)} · 日限额 ${yuan(Number(card.daily_limit_cents) || 0)}</span>
    </div>`).join("")}
    <div class="btn-row"><button class="btn btn--sm btn--ghost" type="button" data-nav="scene" data-scene="cards">去卡片页</button></div>
  </section>`;
}
```

`applyExecution` 里，`const recordIds = ...` 那组声明之后补两行：

```js
  const actions = results.map((item) => String(item.action || ""));
  const showsCardList = complete && actions.includes("card.list");
```

`state.notice = complete ? ... : ...` 赋值结束之后、`renderStage()` 之前加：

```js
  if (showsCardList) state.notice += '<div id="chat-card-list" class="muted">正在读取卡片…</div>';
```

`renderStage()` 之后加：

```js
  if (showsCardList) void loadChatCardList(el("chat-card-list"));
```

- [ ] **Step 3: 样式**

```css
.chat-cards { margin-top: var(--s-4); border: 1px solid var(--line); border-radius: var(--r-card); padding: var(--s-4); }
.chat-cards__head { display: flex; align-items: center; justify-content: space-between; gap: var(--s-2); }
.chat-cards__row { display: flex; flex-direction: column; gap: var(--s-1); padding: var(--s-2) 0; border-bottom: 1px dashed var(--line); }
.chat-cards__row:last-of-type { border-bottom: 0; }
.chat-cards__name { font-weight: 600; }
.chat-cards__meta { font-size: var(--fs-small); color: var(--ink-3); }
```

- [ ] **Step 4: 手工复测**

发「查我有多少卡片」。
Expected: 出「我的卡片」列表（信用卡 · 尾号 9021 / 借记卡 · 尾号 4471），带「去卡片页」按钮，不再只有一句「共 2 张卡」。

- [ ] **Step 5: 提交**

```bash
git add src/ai_banking_copilot/web/app.py src/ai_banking_copilot/web/assets/app.js src/ai_banking_copilot/web/assets/workspace.css
git commit -m "feat(web): 查卡片输出卡片列表卡"
```

---

## 收尾门禁（每批结束跑一次，最后一次全跑）

```bash
ruff check .
mypy
python -m pytest tests/ -q
# 另开一个终端：AIB_DISABLE_LLM=1 python -m ai_banking_copilot
python scripts/check_served_page.py
python scripts/e2e_check.py
python scripts/check_design_sync.py
```

Expected: 全绿；`e2e_check.py` 输出 ALL PASSED。

交付回复必须贴：全量测试输出 + 变更摘要（改了什么 / 为什么 / 动了哪些共享文件 / 残留）。残留至少包括：

- 历史回复的实时链路不持久化（Task 8 只做前端快照）；
- 挂失连带影响是说明文案，不会真的停商户代扣；
- `Card.last4` 是演示数据，界面需与「演示环境」标注同时出现；
- IM / 飞书渠道只改了文案，卡片按钮后置。

# AGENTS.md · 改动约束与分工边界

> **给在本仓库动手的所有人（以及你们的 AI 代理）**：动手前先读本文件，再读
> [`docs/待修复清单.md`](docs/待修复清单.md)（任务台账 + 分工 + 逐条状态）。
> 建立 2026-10-07。

## 1. 完成的标准是「跑通」，不是「写完」

任何改动合并前必须全绿：

```bash
ruff check .        # 必须 All checks passed
mypy                # 必须 Success: no issues found（strict，覆盖 src/）
pytest              # 全量通过
```

涉及服务 / 页面的改动，另需（先起服务）：

```bash
# 起服务：演示与自检必须走**确定性模板通道**——仓库 `.env` 里配了 AI_API_KEY，
# 不设该开关时服务端会走真实 LLM，e2e 结果随模型输出漂移。
# 注意：它是**服务端进程**的环境变量；加在下面自检脚本上无效（脚本不 import 应用）。
AIB_DISABLE_LLM=1 python -m ai_banking_copilot
```

```bash
python scripts/check_served_page.py             # 必须 ALL OK
python scripts/e2e_check.py                     # 必须 ALL PASSED
python scripts/check_design_sync.py             # 样式不得与设计仓漂移
```

> ⚠️ **熔断「只加严不降级」，恢复路径见下**（README 裁决 #27）：
> - **会话级**（触发 3 高频 / 4 连续策略拒绝 / 5 会话异常）只让**该会话**在确认时转人工接管；
>   Web 端**每个页面实例一个新会话**，故「新开页面 / 刷新」即可恢复，不再锁死全场。
>   连续跑 `e2e_check.py` **不会**再污染（「未识别 / 无匹配场景」是**能力边界**，不计入策略拒绝）。
> - **全局**（触发 1 连续授权失败 / 2 注入检测）会拒绝**所有**用户的非纯查询动作，且**默认落盘**
>   （跨重启保留）。恢复走**运维解锁**：以 `AIB_OPERATOR_TOKEN=<口令> python -m ai_banking_copilot`
>   起服务，然后 `python scripts/operator_unlock.py --reason "..."`；口令未配置时该端点一律 403。
> - 演示库当日已发生较大额转账时，转账计划会**合法**升为红卡——脚本已处理该分支（申请挑战码），但**不会**重置库。
>
> 详见 `docs/待修复清单.md` **P0-5** 与 `docs/plans/README.md` **裁决 #27**。

> 门禁挂了就是没完成。不要「先合并再修」。

## 2. 冻结面：`catalog.py` 是唯一真源，四处结构**由它派生**（2026-10-08 起）

`planning.llm.ACTION_KINDS` / `planning.slots.REQUIRED_SLOTS` / `planning.slots.SLOT_PARAMS` /
`policy.pep` 的**读、写两套 handler 分派**
**都不再是手写平行表**——它们都是 `tools/catalog.py` 注册表的投影（`docs/待修复清单.md` 步骤 1.1–1.6
与步骤 4「残留」；裁决 #25 / #26）。新增一个规范动作的实际改动面：

| # | 位置 | 内容 | 何时必改 |
|---|---|---|---|
| 1 | `tools/catalog.py` | `ToolSpec`：动作、参数（含 `slot_name`）、`kind`、风险、是否纯查询 | **总是**——**只改自己域那一段**（6 组区段 git 可自动合并） |
| 2 | 自己的场景模块 | 动作的 **handler**（`WRITE_HANDLERS` 或 `QUERY_HANDLERS`）；纯查询动作另需 `matches` / `build` 路径 | **总是** |
| 3 | `gateway/core_bank.py` | 新动作需要的**写原语**（只加自己的方法） | 该动作没有可复用的原语时 |
| 4 | `policy/pdp.py` | 该动作的**三色口径**（只加自己那一行；**只加严不放宽**） | 官方表对该动作有专门定级时 |
| 5 | `planning/llm.py` | 仅 `_TEMPLATE_RULES`（可控口述触发） | 需要口述触发时 |
| 6 | `planning/slots.py` | 仅当出现**新槽位名**：`_PROMPTS` + `_resolve_slot`（`SLOT_PARAMS` 已派生，不必手改） | 需要新槽位时 |
| 7 | `src/ai_banking_copilot/eval/data/*` | 评测用例 | **总是** |
| 8 | `web/assets/app.js`（`GLOSS`）+ `README.md` | 出现新的 reason / require 码；对外能力口径变化 | 出现新码 / 口径变化时 |

**`policy/pep.py` 不再因新增动作而改动**：读、写两侧的分派**都已**是 handler 注册表
（`WRITE_HANDLERS` / `QUERY_HANDLERS` 各场景自带、`scenarios/registry.py` 汇合、组合根注入），
`Pep.handled_actions()` = 两侧**处理器键的并集**（裁决 #25 / #26）。新增**任一**动作（读或写）
都只需改自己名下那几处。

> **按域分段的共享文件**：`tools/catalog.py` / `gateway/core_bank.py` / `policy/pdp.py` /
> `eval/data/cases.json` / `planning/llm.py` / `web/assets/app.js` **不是单人独占**——
> 各人**只改自己那一段**并请段主复核即可（详见 `docs/待修复清单.md` 第七节「冲突热点与规避」）。
> `catalog.py` 自 2026-10-08 起由「甲独占」放开为按域分段：1.2b 当初要求单人汇总的理由
> （6 处派生结构逐字一致）已随 ①–④ 全部派生消失。

另外，「动作名唯一真源」= `tools/catalog.py`；旧文档指向的 **README「规范动作表」并不存在**
（README 只有覆盖场景与能力口径），故第 8 项只需在**对外口径变化**时更新 README。

硬断言（漏改即红）：

- `tests/tools/test_catalog.py` 的「绿色集合 == 纯查询集合」；
- `tests/scenarios/test_registry.py` 的「**读处理器 ∪ 写处理器 == 注册表动作集**（且两侧不相交）」
  ——漏登记任一处理器、或同动作登记进两侧，必红；
- `tests/policy/test_pep.py`（自建注册表 ⊆ 处理器集）与 `tests/eval/test_eval_integration.py`
  的「**派生结果 == 注册表**」——漏声明 `kind`、或动作集合不一致，必红。

计数断言（`test_catalog.py` / `test_llm_template.py` / `test_base.py` / `test_scaffold.py` /
`test_eval_integration.py`）自 2026-10-08 起一律为**下限**（`>= 20`，防误删），不再用等号——
等号会逼每个新增动作的人都去改**同一行**（必然冲突）。集合本身仍逐项精确
（`test_pep.py` 的 20 项是 03 自建测试注册表，不依赖 catalog，保持等号）。

## 3. 文件归属：**同一个文件同一时间只由一个人改**（**分段共享文件除外**）

按 [`docs/待修复清单.md`](docs/待修复清单.md) **第七节「四人分工与文件归属」**，**只在归属自己的
文件里改**；需要别人负责的文件时，**在 PR 描述里写明需求**。

**例外 = 按域分段共享文件**（`tools/catalog.py` / `gateway/core_bank.py` / `policy/pdp.py` /
`eval/data/cases.json` / `planning/llm.py` / `web/assets/app.js`）：这些文件**只改自己那一段**
（各段互不引用、git 可自动合并），并请段主复核——这不算「顺手改别人的文件」。
`policy/pep.py` 则因读写两侧都已注册表化而**不需要任何人**为新动作改动。

## 4. 禁止改动（需 4 人+组长一致同意）

- `src/ai_banking_copilot/contracts/`（计划与授权**契约**，改动会同时影响评测、审计、文档三处口径）
- `policy/audit.py` 的哈希/签名算法与 `policy/gate.py` 的判据（审计链与执行门语义）
- `policy/pep.py` 的「无裁决不执行」「绿色只允许纯查询」「熔断只加严不降级」三条硬约束及其断言
- 既有测试的**语义断言**（可以补测试，不能为了让测试变绿而放宽断言）

## 5. 架构边界（有守卫测试，改了会红）

- **H2**：`web/` 层源码**不得**直接触碰 `CoreBank`（写操作只经 `policy.pep.Pep`；
  补偿经 `policy/reversal.ReversalService`，由组合根注入）。`tests/web/test_web_integration.py` 守卫。
- **事实零生成**：数值 / 实体事实只能来自 `gateway`；LLM 不得生成事实。
- **控制槽零信任**：`untrusted` 值进控制槽由**类型层**拒绝，不要绕过 `Value`/`Slot` 包装。

## 6. 诚实性纪律（本项目的红线）

**界面、文案、对外文档不得宣称未实现的能力。** 已修过的同类问题：

- 红色「强验证」曾在界面上写「人脸 / 短信 / U 盾」而实际只是布尔开关 → 已改为真实的服务端一次性挑战；
- 回退曾宣称「已发起退回」而实际没有资金动作 → 已改为真实冲正；
- 前端的「中断 / 回退」曾无视服务端失败、一律显示成功 → 已改为如实报错。

新增任何能力时，若只是占位，**要么明确标注为演示/模拟，要么如实声明为能力子集**（非等价实现）。

## 7. 分支与提交

- 分支：`feat/p2-1-yearly-bill` 这种「类型 / 清单条目号 - 简短描述」。
- 评测用例 id 用**自己的场景前缀**（`bills-yearly-*` / `cards-*` / `subs-*` / `cross-*`），
  追加在 `cases.json` **自己的段内**，避免与他人交错。
- PR 描述里写清：改了哪几条清单项、跑了哪些门禁、**残留了什么**（残留必须写）。

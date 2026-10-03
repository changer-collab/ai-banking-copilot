# 协作规范（分支模型 · 评审 · 设计先行）

> 适用仓库：`ai-banking-copilot`（主仓）与 `ai-banking-copilot-web`（设计仓）。

## 1. 分支模型（强约束）

- `main` 为**受保护分支**：**禁止任何人直推**（含仓库所有者）。
- 所有改动必须经由**功能分支 + Pull Request**。
- **只有 `@changer-collab` 有合并（merge）权限**；其他成员无 merge 权限。
- 本地开发**不得停留在 `main`**：本地工作 / 集成分支固定为 **`changer`**；
  每位成员亦可另开自己的功能分支（如 `feat/<主题>`），但**禁止以 `main` 作为工作分支**。

## 2. 评审与合并流程

1. 从最新 `main` 切分支（本地 `changer` 或 `feat/<主题>`）。
2. 提交并 `git push` 该分支。
3. 在 GitHub 开 PR，base 选 `main`。
4. **由 `@changer-collab` 审核并合并**；PR 作者不得自行合并。

## 3. 设计先行（强约束）

- **所有界面 / 交互类功能，必须先在 `ai-banking-copilot-web` 完成前端设计**
  （页面 + 样式 + 交互稿，含演示数据），评审确认后**才允许**在 `ai-banking-copilot` 中进行后端 / 集成实现。
- 两仓共用同一套设计令牌（`assets/tokens.css`）；主仓页面须**复用**设计仓的令牌与组件语汇。
- 主仓若需改动页面版式，**先改设计仓**，再同步回主仓（顺序不可颠倒）。

## 4. 提交信息

- 格式：`<type>: <描述>`，type ∈ `feat | fix | test | docs | chore | refactor`。
- 单次提交聚焦一件事；**不提交密钥、构建产物、缓存与本地数据**。

## 5. 质量门

- 主仓：`ruff check .` / `mypy` / `pytest` 必须全绿（`make check` 一键执行）。
- 设计仓：纯静态零构建；改 CSS / JS 后**必须 bump HTML 的 `?v=N`**。

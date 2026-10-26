# 门禁与评测结果

> 口径：所有数字都必须能由本文档列出的命令复现。**不写没跑过的数字。**
> 采集环境：Windows / Python 3.14.3 / `.venv` 内 `pip install -e ".[dev]"`

---

## D0 基线（2026-10-26）

**提交**：`3375d52`（分支 `sprint/1026`）

| 命令 | 结论 | 退出码 |
|---|---|---|
| `ruff check .` | `All checks passed!` | 0 |
| `mypy` | `Success: no issues found in 91 source files` | 0 |
| `pytest -q` | `1562 passed, 1 warning in 229.49s (0:03:49)` | 0 |

**唯一 warning（非阻断）**：

```
StarletteDeprecationWarning: Using httpx with starlette.testclient is deprecated;
install httpx2 instead.
```

**用法**：明天起任何一次回归变红，先对照这一行——D0 是 1562 全绿、0 失败、0 跳过。多出来的失败一定是这几天的改动引入的，不要去怀疑基线。

---

## 待填（后续里程碑）

| 里程碑 | 日期 | 提交 | ruff | mypy | pytest | 备注 |
|---|---|---|---|---|---|---|
| D0 基线 | 2026-10-26 | `3375d52` | 过 | 过 | 1562 passed | — |
| D3 收口后 | 待填 | 待填 | 待填 | 待填 | 待填 | 后端收口 + 25 动作 |

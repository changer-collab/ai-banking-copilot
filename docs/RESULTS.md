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
| D3 收口后 | 2026-10-27 | `25fdccc` | 过 | 过（92 files） | 1564 passed | 25 动作 + ⑥ 改 B 案 + 三动作升红 |

**D3 收口后明细（2026-10-27，`25fdccc`）**

| 命令 | 结论 |
|---|---|
| `ruff check .` | `All checks passed!` |
| `mypy src` | `Success: no issues found in 92 source files` |
| `pytest -q --junitxml=...` | `tests=1564 errors=0 failures=0 skipped=0`，耗时 221.91s |

较 D0 多 2 条用例（新增 `password.change` 与生日链冻结/释放断言），无失败、无跳过。唯一 warning 与 D0 相同（Starlette/httpx 弃用提示）。

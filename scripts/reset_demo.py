"""一键重置演示库：删掉业务库与运行痕迹，再走唯一装配入口重新播种。

用法（在仓库根目录执行）：

```
python scripts/reset_demo.py                # 重置 runtime/copilot.db
python scripts/reset_demo.py --db runtime/copilot.db
```

删除范围只包含本脚本认识的这些文件（含 SQLite 旁路文件）：

- `<db>`、`<db>-wal`、`<db>-shm`：业务库；
- `audit.jsonl`：审计链日志（重置后从空链重新开始）；
- `<stem>-insights.db`（含旁路）：洞察元数据；
- `<stem>-bill-analysis.db`（含旁路）：账单分析结果与线索处理记录。

**保留** `audit.jsonl.key` 与 `<stem>.verification.key`：密钥不属于业务状态，
保留后重置前后的审计链密钥与强验证密钥保持一致。

重建复用 `compose.build_deps`（与 `python -m ai_banking_copilot` 同一入口），
因此重置后的业务表与首次启动逐字节等价；`business_digest` 可用于核对
「连续重置 N 次结果一致」。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Mapping, Sequence
from contextlib import ExitStack
from pathlib import Path

from ai_banking_copilot.compose import build_deps
from ai_banking_copilot.gateway.store import Store, StoreMode

DEFAULT_DB = Path("runtime/copilot.db")

# 与 compose.build_deps 的审计日志/洞察库命名保持一致（见 compose.build_audit_log /
# INSIGHT_DB_ENV 的缺省分支）。
_AUDIT_FILE = "audit.jsonl"


def _artifact_paths(db_path: Path) -> tuple[Path, ...]:
    parent = db_path.parent
    names = (
        db_path.name,
        f"{db_path.name}-wal",
        f"{db_path.name}-shm",
        _AUDIT_FILE,
        f"{db_path.stem}-insights.db",
        f"{db_path.stem}-insights.db-wal",
        f"{db_path.stem}-insights.db-shm",
        f"{db_path.stem}-bill-analysis.db",
        f"{db_path.stem}-bill-analysis.db-wal",
        f"{db_path.stem}-bill-analysis.db-shm",
    )
    return tuple(parent / name for name in names)


def _remove_artifacts(db_path: Path) -> list[str]:
    removed: list[str] = []
    for path in _artifact_paths(db_path):
        if path.is_file():
            path.unlink()
            removed.append(str(path))
    return removed


def business_digest(db_path: Path) -> str:
    """业务表的确定性摘要：逐表全量行、按 repr 排序后取 sha256。"""
    store = Store(db_path, StoreMode.READ_ONLY)
    try:
        names = [
            str(row["name"])
            for row in store.query(
                "SELECT name FROM sqlite_master WHERE type='table'"
                " AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        snapshot = {
            name: sorted(
                [list(row) for row in store.query(f'SELECT * FROM "{name}"')], key=repr
            )
            for name in names
        }
    finally:
        store.close()
    payload = json.dumps(snapshot, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def reset_demo(
    db_path: Path = DEFAULT_DB, env: Mapping[str, str] | None = None
) -> str:
    """删除旧库与运行痕迹并重新播种，返回重置后的业务表摘要。"""
    _remove_artifacts(db_path)
    with ExitStack() as resources:
        build_deps(db_path, env=env, resources=resources)
    return business_digest(db_path)


def main(argv: Sequence[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="一键重置演示库（删库 + 重新播种）")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="业务库路径")
    args = parser.parse_args(argv)
    removed = _remove_artifacts(args.db)
    with ExitStack() as resources:
        build_deps(args.db, resources=resources)
    digest = business_digest(args.db)
    for path in removed:
        print(f"已删除 {path}")
    print(f"已重置 {args.db}")
    print(f"业务表摘要 {digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

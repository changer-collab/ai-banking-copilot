"""把基准评测的 ``results.json`` 逐例导出成 CSV，供人工复核与对标存档。

用法（在仓库根目录执行）：

```
python scripts/export_cases_csv.py \\
    --input runtime/benchmark/evidence-dev-v1-fix2/run-1/results.json \\
    --output docs/results/evidence-dev-v1-cases.csv
```

也可以直接给 run 目录（脚本自动找 ``results.json``）：

```
python scripts/export_cases_csv.py --input runtime/benchmark/web-smoke-v2-fix2/run-1 \\
    --output docs/results/web-smoke-v2-cases.csv
```

列口径与 ``docs/results/web-smoke-v2-cases.csv`` 保持一致，仅做扁平化：

- ``attack_blocked``：仅 ``metric_group == "attack"`` 时取该例 ``passed``，其余留空；
- ``false_rejection``：逐例 ``false_rejection``（汇总分母只算 legitimate）；
- ``completed``：``state == "completed"``；
- ``money_traceable``：无 ``numeric_proofs`` 时留空，否则全部 proof 通过才算 ``true``；
- ``reviewer_group`` / ``reviewer_status``：取首条 ``reviewer_observations`` 的
  ``channel`` / ``status``（无审阅记录时留空）。

脚本只读 ``results.json``，不重跑评测；生成的文件带三行注释头（含耗时最长的
复跑命令与导出日期），因此 ``diff`` 可直接用来核对两次评测是否逐例一致。
"""

from __future__ import annotations

import argparse
import csv
import json
from collections.abc import Iterable, Mapping, Sequence
from datetime import date
from pathlib import Path
from typing import Any

COLUMNS = (
    "case_id",
    "attack_blocked",
    "false_rejection",
    "completed",
    "money_traceable",
    "reviewer_group",
    "reviewer_status",
)


def _bool_text(value: bool) -> str:
    return "true" if value else "false"


def _attack_blocked(case: Mapping[str, Any]) -> str:
    if case.get("metric_group") != "attack":
        return ""
    return _bool_text(bool(case.get("passed")))


def _money_traceable(case: Mapping[str, Any]) -> str:
    proofs = case.get("numeric_proofs") or []
    if not proofs:
        return ""
    return _bool_text(all(bool(proof.get("passed")) for proof in proofs))


def _reviewer(case: Mapping[str, Any]) -> tuple[str, str]:
    observations = case.get("reviewer_observations") or []
    if not observations:
        return "", ""
    first = observations[0]
    return str(first.get("channel") or ""), str(first.get("status") or "")


def case_row(case: Mapping[str, Any]) -> dict[str, str]:
    group, status = _reviewer(case)
    return {
        "case_id": str(case.get("id", "")),
        "attack_blocked": _attack_blocked(case),
        "false_rejection": _bool_text(bool(case.get("false_rejection"))),
        "completed": _bool_text(case.get("state") == "completed"),
        "money_traceable": _money_traceable(case),
        "reviewer_group": group,
        "reviewer_status": status,
    }


def _header_lines(payload: Mapping[str, Any], run_dir: Path, exported_on: date) -> list[str]:
    summary = payload.get("summary") or {}
    dataset = summary.get("dataset_version") or "unknown"
    relative = f"{run_dir.as_posix()}/results.json"
    cases = payload.get("cases", [])
    legit_total = sum(1 for case in cases if case.get("metric_group") == "legitimate")
    return [
        f"# 导出：python scripts/export_cases_csv.py --input {relative}",
        f"# 数据集 {dataset}，run 目录 {run_dir.as_posix()}，导出日 {exported_on.isoformat()}",
        "# attack_blocked 取该例 passed，仅对 metric_group=attack 有意义（其余留空）；"
        f"false_rejection 的汇总分母只算 legitimate（{legit_total} 条）；",
        "# money_traceable 无展示数值时留空；reviewer_group/status 取自逐例 reviewer_observations",
    ]


def _resolve_results(input_path: Path) -> tuple[Path, Path]:
    """返回 (results.json 路径, run 目录)。"""
    if input_path.is_dir():
        return input_path / "results.json", input_path
    return input_path, input_path.parent


def export(input_path: Path, output_path: Path, exported_on: date | None = None) -> int:
    results_path, run_dir = _resolve_results(input_path)
    payload = json.loads(results_path.read_text(encoding="utf-8"))
    cases: Sequence[Mapping[str, Any]] = payload.get("cases") or []
    rows: Iterable[Mapping[str, str]] = (case_row(case) for case in cases)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="") as handle:
        for line in _header_lines(payload, run_dir, exported_on or date.today()):
            handle.write(line + "\n")
        writer = csv.DictWriter(handle, fieldnames=list(COLUMNS), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    return len(cases)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="把基准评测 results.json 逐例导出成 CSV")
    parser.add_argument("--input", type=Path, required=True, help="results.json 或其所在 run 目录")
    parser.add_argument("--output", type=Path, required=True, help="导出的 CSV 路径")
    args = parser.parse_args(argv)

    count = export(args.input, args.output)
    print(f"已导出 {count} 条用例: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

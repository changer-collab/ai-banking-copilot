"""一条命令运行自建 Web 回归：python scripts/run_benchmark.py [--output runtime/benchmark]。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ai_banking_copilot.eval.web_benchmark import run_benchmark


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="模板通道自建 Web 回归评测（非盲测）")
    parser.add_argument("--output", type=Path, default=Path("runtime/benchmark"))
    parser.add_argument("--repeat", type=int, default=2, choices=range(1, 11))
    args = parser.parse_args()
    reports = [run_benchmark(args.output / f"run-{index + 1}") for index in range(args.repeat)]
    consistent = len({report["business_digest"] for report in reports}) == 1
    for index, report in enumerate(reports, start=1):
        print(f"运行 {index}: {'PASS' if report['passed'] else 'FAIL'}")
        for metric, value in report["summary"].items():
            if metric == "scenario_task_success_rates":
                continue
            rate = "N/A" if value["rate"] is None else f"{value['rate']:.2%}"
            print(f"  {metric}: {value['numerator']}/{value['denominator']} ({rate})")
        for case in report["cases"]:
            if not case["passed"]:
                print(f"  FAIL {case['id']}: {'；'.join(case['failures'])}")
    print(f"独立运行的业务摘要一致: {consistent}")
    print(f"逐例 JSON 与汇总 CSV: {args.output.resolve()}")
    return 0 if consistent and all(report["passed"] for report in reports) else 1


if __name__ == "__main__":
    raise SystemExit(main())

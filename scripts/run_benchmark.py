"""运行 Web 回归；严格模型缺凭据时明确报告 unavailable，不使用模板补分。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ai_banking_copilot.eval.web_benchmark import run_benchmark


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Web 回归评测（自建数据，非盲测）")
    parser.add_argument("--output", type=Path, default=Path("runtime/benchmark"))
    parser.add_argument("--repeat", type=int, default=2, choices=range(1, 11))
    parser.add_argument("--channel", choices=("template", "llm-strict"), default="template")
    parser.add_argument("--dataset", default="web-smoke-v2")
    parser.add_argument("--arm", default="template")
    parser.add_argument(
        "--release",
        nargs="?",
        const="release",
        default=False,
        help="要求干净工作树；可选传入发布标识",
    )
    args = parser.parse_args()
    reports = [
        run_benchmark(
            args.output / f"run-{index + 1}",
            channel=args.channel,
            dataset=args.dataset,
            arm=args.arm,
            release=args.release,
        )
        for index in range(args.repeat)
    ]
    if args.channel == "llm-strict" and any(
        report.get("status") == "unavailable" for report in reports
    ):
        for index, report in enumerate(reports, start=1):
            print(f"运行 {index}: UNAVAILABLE ({report['unavailable_reason']})")
        print(f"严格模型证据目录: {args.output.resolve()}")
        return 2
    consistent = len({report["business_digest"] for report in reports}) == 1
    for index, report in enumerate(reports, start=1):
        print(f"运行 {index}: {'PASS' if report['passed'] else 'FAIL'}")
        for metric, value in report["summary"].items():
            if metric in {"scenario_task_success_rates", "failed_case_ids", "dataset_version"}:
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

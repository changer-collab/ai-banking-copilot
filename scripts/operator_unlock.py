"""运维解锁：调用 `POST /api/operator/breaker-unlock`（README 裁决 #27）。

**唯一**的熔断降级出口。只做一件事：带口令 + 理由调那个端点，把结果原样打印出来。

用法：
    # 口令来自环境变量（推荐，不进 shell 历史 / 不被 ps 看到）
    $env:AIB_OPERATOR_TOKEN = "<口令>"
    python scripts/operator_unlock.py --reason "注入误报，已人工核实"

    # 或直接给出（会进 shell 历史，仅限本地演示）
    python scripts/operator_unlock.py --token <口令> --reason "演示复位"

退出码：0 = 解锁成功；1 = 失败（含 403 未授权 / 400 缺理由 / 服务不可达）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

TOKEN_ENV = "AIB_OPERATOR_TOKEN"
UNLOCK_PATH = "/api/operator/breaker-unlock"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="解锁全局熔断器（运维动作，必留痕）")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--token", default=None, help=f"运维口令；缺省读环境变量 {TOKEN_ENV}")
    parser.add_argument("--reason", required=True, help="解锁理由（必填，会写入审计链）")
    args = parser.parse_args(argv)

    token = (args.token or os.environ.get(TOKEN_ENV, "")).strip()
    if not token:
        print(f"缺少运维口令：请设环境变量 {TOKEN_ENV} 或用 --token 传入", file=sys.stderr)
        return 1

    data = json.dumps({"reason": args.reason}).encode("utf-8")
    request = urllib.request.Request(
        args.base_url.rstrip("/") + UNLOCK_PATH,
        data=data,
        headers={"Content-Type": "application/json", "X-Operator-Token": token},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        print(f"解锁失败：HTTP {error.code} {detail}", file=sys.stderr)
        return 1
    except urllib.error.URLError as error:
        print(f"服务不可达：{error.reason}", file=sys.stderr)
        return 1

    print(
        "已解锁：state {before} -> {after}；触发项 {triggers}；理由「{reason}」已写入审计链".format(
            before=payload.get("state_before"),
            after=payload.get("state_after"),
            triggers=payload.get("triggers_before"),
            reason=payload.get("reason"),
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

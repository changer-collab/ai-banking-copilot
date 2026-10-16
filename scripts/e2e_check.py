"""前后端连接自检：对**真实运行的服务**发 HTTP 请求（非 TestClient）。

用法：
    # 终端 A：启动服务
    python -m ai_banking_copilot
    # 终端 B：跑自检（默认 http://127.0.0.1:8000）
    python scripts/e2e_check.py
    python scripts/e2e_check.py --base-url http://127.0.0.1:8000

覆盖：内联页可达（前端）→ `/api/submit`（规划卡）→ `/api/confirm`（批量确认 + 顺序执行）
→ 多意图跨场景 / 澄清 / 安全拒绝。仅用标准库，退出码 0 = 全过。
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
import uuid


def _post(base: str, path: str, body: dict[str, object]) -> tuple[int, dict[str, object]]:
    data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        base + path, data=data, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8"))


def _get(base: str, path: str) -> tuple[int, str]:
    with urllib.request.urlopen(base + path, timeout=15) as response:
        return response.status, response.read().decode("utf-8")


def _norm(state: object) -> str:
    """会话状态归一（服务端返回小写枚举值）。"""
    return str(state).lower()


def run(base: str) -> list[str]:
    failures: list[str] = []
    # 每次运行对应一个新页面会话，避免沿用其他自检的 pending 或高频计数。
    session_id = f"e2e-{uuid.uuid4().hex}"

    status, html = _get(base, "/")
    asset_status, _asset = _get(base, "/assets/app.js")
    page_ok = (
        status == 200
        and asset_status == 200
        and 'id="stage"' in html
        and "/assets/app.js" in html
        and "计划卡" in html
    )
    print(f"[1] GET /                     -> {status}  前端页可达={page_ok}")
    if not page_ok:
        failures.append("GET / 未返回内联页")

    status, submit = _post(
        base,
        "/api/submit",
        {
            "session_id": session_id,
            "utterance": "转给李娜 500 元，取消订阅",
            "selected": {"account": "acc_savings", "subscription": "sub_video"},
        },
    )
    card = submit.get("card") or {}
    print(f"[2] POST /api/submit (多意图)  -> {status}")
    print(f"    state={submit.get('state')} level={card.get('level')}")
    for line in card.get("lines", []):
        print(f"    · {line}")
    if status != 200 or _norm(submit.get("state")) != "awaiting_confirm" or not card:
        failures.append("/api/submit 多意图未产出计划卡")

    if card:
        payload: dict[str, object] = {
            "session_id": session_id,
            "card_id": card.get("card_id"),
            "frozen_hash": card.get("frozen_hash"),
        }
        # 红色卡按官方口径需强验证：向服务端申请挑战（演示环境验证码随响应回显）。
        # 注意：若演示库日累计已超 1000 元，同一计划会合法升为红色。
        if str(card.get("level", "")).upper() == "RED":
            vstatus, prompt = _post(base, "/api/verify/request", {"session_id": session_id})
            payload["verification_code"] = prompt.get("code")
            print(
                f"[2b] POST /api/verify/request  -> {vstatus} "
                f"channel={prompt.get('channel')} target={prompt.get('target')}"
            )
            if vstatus != 200 or not prompt.get("code"):
                failures.append("/api/verify/request 未下发验证码")
        status, executed = _post(base, "/api/confirm", payload)
        results = executed.get("results", [])
        print(f"[3] POST /api/confirm         -> {status} state={executed.get('state')}")
        for item in results:
            print(f"    · {item.get('status')}: {item.get('message')}")
        if status != 200 or not results:
            failures.append("/api/confirm 未执行")

    status, frozen = _post(
        base,
        "/api/submit",
        {"session_id": session_id, "utterance": "冻结 500 元，转给李娜 300 元",
         "selected": {"account": "acc_savings"}},
    )
    fcard = frozen.get("card") or {}
    frozen_ok = status == 200 and _norm(frozen.get("state")) == "awaiting_confirm" and bool(fcard)
    print(
        f"[4] submit 冻结+转账(跨场景)   -> {status} "
        f"state={frozen.get('state')} 通过={frozen_ok}"
    )
    if not frozen_ok:
        failures.append("跨场景(冻结+转账)未产出计划卡")

    status, clarify = _post(
        base,
        "/api/submit",
        {"session_id": session_id, "utterance": "转给王伟 500 元",
         "selected": {"account": "acc_savings"}},
    )
    clarify_ok = status == 200 and _norm(clarify.get("state")) == "needs_clarification"
    print(
        f"[5] submit 多候选收款人        -> {status} "
        f"state={clarify.get('state')} 通过={clarify_ok}"
    )
    if not clarify_ok:
        failures.append("多候选收款人未触发澄清")

    status, blocked = _post(
        base, "/api/submit", {"session_id": session_id, "utterance": "帮我做点什么",
                              "selected": {"account": "acc_savings"}}
    )
    blocked_ok = status == 200 and _norm(blocked.get("state")) in {"blocked", "needs_clarification"}
    print(
        f"[6] submit 未知意图            -> {status} "
        f"state={blocked.get('state')} 通过={blocked_ok}"
    )
    if not blocked_ok:
        failures.append("未知意图未安全拒绝")

    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="前后端连接自检（对真实服务发 HTTP 请求）")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    args = parser.parse_args(argv)
    base = str(args.base_url).rstrip("/")
    print(f"target: {base}\n")
    failures = run(base)
    print()
    if failures:
        print("FAILED:")
        for item in failures:
            print("  -", item)
        return 1
    print("ALL PASSED：前端页可达 + 提交/确认/执行 + 多意图跨场景 + 澄清 + 安全拒绝")
    return 0


if __name__ == "__main__":
    sys.exit(main())

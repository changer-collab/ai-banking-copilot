"""本地回放飞书回调，覆盖正常、验签篡改、重复投递和突发限流。"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from http.client import HTTPResponse
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from ai_banking_copilot.web.channels.feishu import (  # noqa: E402
    encrypt_payload,
    signature_for,
)


@dataclass(frozen=True)
class Result:
    status: int
    body: dict[str, object]
    headers: dict[str, str]


def _positive_int(value: str) -> int:
    count = int(value)
    if count <= 0:
        raise argparse.ArgumentTypeError("必须大于 0")
    return count


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "mode",
        nargs="?",
        choices=("normal", "tamper", "replay", "burst"),
        help="兼容旧接口的位置模式；未指定时执行 normal",
    )
    parser.add_argument("--channel", choices=("feishu",), default="feishu")
    parser.add_argument("--text", default="查一下余额")
    parser.add_argument("--url", default="http://127.0.0.1:8000/api/im/feishu")
    parser.add_argument(
        "--key", default=os.getenv("AIB_FEISHU_ENCRYPT_KEY", "replay-key")
    )
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--tamper", action="store_true", help="发送一次篡改签名")
    modes.add_argument("--replay", action="store_true", help="重复发送同一事件")
    modes.add_argument("--burst", type=_positive_int, metavar="N", help="发送 N 个唯一事件")
    return parser


def _message(event_id: str, text: str) -> dict[str, object]:
    return {
        "schema": "2.0",
        "header": {
            "event_id": event_id,
            "event_type": "im.message.receive_v1",
            "token": os.getenv("AIB_FEISHU_VERIFICATION_TOKEN", "verify-token"),
        },
        "event": {
            "sender": {"sender_id": {"open_id": os.getenv("AIB_FEISHU_REPLAY_USER", "ou-replay")}},
            "message": {
                "chat_id": os.getenv("AIB_FEISHU_REPLAY_CHAT", "oc-replay"),
                "message_type": "text",
                "content": json.dumps({"text": text}, ensure_ascii=False),
            },
        },
    }


def _post(url: str, body: bytes, key: str, *, tamper: bool = False) -> Result:
    timestamp = str(int(time.time()))
    nonce = "replay-nonce"
    signature = signature_for(key, timestamp, nonce, body)
    if tamper:
        signature = "0" * 64
    request = Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Lark-Request-Timestamp": timestamp,
            "X-Lark-Request-Nonce": nonce,
            "X-Lark-Signature": signature,
        },
    )
    try:
        response: HTTPResponse
        with urlopen(request, timeout=5) as response:
            return _result(response.status, response.headers, response.read())
    except HTTPError as error:
        return _result(error.code, error.headers, error.read())
    except URLError as error:
        raise SystemExit(f"回放请求失败：{error.reason}") from error


def _result(status: int, headers: object, raw: bytes) -> Result:
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        body = {"raw": raw.decode("utf-8", errors="replace")}
    header_map = {
        str(key).lower(): str(value) for key, value in headers.items()  # type: ignore[attr-defined]
    }
    result_body = body if isinstance(body, dict) else {"body": body}
    return Result(status=status, body=result_body, headers=header_map)


def _body(event_id: str, key: str, text: str) -> bytes:
    encrypted = encrypt_payload(_message(event_id, text), key)
    return json.dumps({"encrypt": encrypted}, separators=(",", ":")).encode("utf-8")


def _mode(args: argparse.Namespace) -> tuple[str, int]:
    if args.tamper:
        return "tamper", 1
    if args.replay:
        return "replay", 2
    if args.burst is not None:
        return "burst", int(args.burst)
    mode = str(args.mode or "normal")
    return mode, 21 if mode == "burst" else 1


def run(args: argparse.Namespace) -> int:
    mode, burst_count = _mode(args)
    url = str(args.url)
    key = str(args.key)
    text = str(args.text)
    if mode == "normal":
        result = _post(url, _body("replay-normal", key, text), key)
        print(json.dumps(
            {"mode": mode, "status": result.status, "body": result.body},
            ensure_ascii=False,
        ))
        return 0 if result.status == 200 else 1
    if mode == "tamper":
        result = _post(url, _body("replay-tamper", key, text), key, tamper=True)
        print(json.dumps(
            {"mode": mode, "status": result.status, "body": result.body},
            ensure_ascii=False,
        ))
        return 0 if result.status == 401 else 1
    if mode == "replay":
        body = _body("replay-once", key, text)
        first = _post(url, body, key)
        second = _post(url, body, key)
        print(json.dumps(
            {"mode": mode, "first": first.body, "second": second.body},
            ensure_ascii=False,
        ))
        return 0 if (
            first.status == 200
            and second.status == 200
            and second.body.get("replayed") is True
        ) else 1
    statuses = []
    for index in range(burst_count):
        result = _post(url, _body(f"replay-burst-{index}", key, text), key)
        statuses.append(result.status)
    print(json.dumps({"mode": mode, "statuses": statuses}, ensure_ascii=False))
    return 0 if 429 in statuses else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    has_switch = args.tamper or args.replay or args.burst is not None
    if args.mode is not None and has_switch:
        parser.error("位置模式不能与模式开关同时使用")
    return run(args)


if __name__ == "__main__":
    sys.exit(main())

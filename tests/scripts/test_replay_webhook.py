from __future__ import annotations

import argparse
import json
import runpy
from collections.abc import Callable

import pytest

from ai_banking_copilot.web.channels.feishu import FeishuAdapter, FeishuConfig

_REPLAY = runpy.run_path("scripts/replay_webhook.py")
Result = _REPLAY["Result"]
build_parser: Callable[[], argparse.ArgumentParser] = _REPLAY["build_parser"]
run: Callable[[argparse.Namespace], int] = _REPLAY["run"]
main: Callable[[list[str]], int] = _REPLAY["main"]


def _result(status: int, *, replayed: bool = False) -> object:
    return Result(status=status, body={"replayed": replayed}, headers={})


def _args(*argv: str) -> argparse.Namespace:
    return build_parser().parse_args(list(argv))


def test_new_default_mode_sends_text_once(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, bytes, str, bool]] = []

    def post(url: str, body: bytes, key: str, *, tamper: bool = False) -> object:
        calls.append((url, body, key, tamper))
        return _result(200)

    monkeypatch.setitem(run.__globals__, "_post", post)
    args = _args("--channel", "feishu", "--text", "给李娜转500")

    assert run(args) == 0
    assert len(calls) == 1
    url, body, key, tamper = calls[0]
    assert url.endswith("/api/im/feishu")
    assert tamper is False
    payload = FeishuAdapter(FeishuConfig(encrypt_key=key)).decode_body(body)
    content = json.loads(payload["event"]["message"]["content"])
    assert content["text"] == "给李娜转500"


@pytest.mark.parametrize(
    "first,second",
    [("--tamper", "--replay"), ("--tamper", "--burst"), ("--replay", "--burst")],
)
def test_new_mode_switches_are_mutually_exclusive(
    first: str, second: str
) -> None:
    argv = [first]
    if first == "--burst":
        argv.append("2")
    argv.append(second)
    if second == "--burst":
        argv.append("2")

    with pytest.raises(SystemExit) as raised:
        main(argv)

    assert raised.value.code == 2


@pytest.mark.parametrize("value", ["0", "-1"])
def test_burst_rejects_non_positive_counts(value: str) -> None:
    with pytest.raises(SystemExit) as raised:
        _args("--burst", value)

    assert raised.value.code == 2


def test_new_burst_sends_exact_count_with_unique_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bodies: list[bytes] = []

    def post(_url: str, body: bytes, _key: str, *, tamper: bool = False) -> object:
        assert tamper is False
        bodies.append(body)
        return _result(429 if len(bodies) == 4 else 200)

    monkeypatch.setitem(run.__globals__, "_post", post)

    assert run(_args("--burst", "4", "--text", "查最近扣款")) == 0
    assert len(bodies) == 4
    adapter = FeishuAdapter(FeishuConfig(encrypt_key="replay-key"))
    payloads = [adapter.decode_body(body) for body in bodies]
    event_ids = [str(payload["header"]["event_id"]) for payload in payloads]
    assert len(set(event_ids)) == 4
    assert all(
        json.loads(payload["event"]["message"]["content"])["text"]
        == "查最近扣款"
        for payload in payloads
    )


def test_new_replay_reuses_identical_encrypted_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bodies: list[bytes] = []

    def post(_url: str, body: bytes, _key: str, *, tamper: bool = False) -> object:
        assert tamper is False
        bodies.append(body)
        return _result(200, replayed=len(bodies) == 2)

    monkeypatch.setitem(run.__globals__, "_post", post)

    assert run(_args("--replay")) == 0
    assert len(bodies) == 2
    assert bodies[0] == bodies[1]


def test_tamper_sends_one_bad_signature(monkeypatch: pytest.MonkeyPatch) -> None:
    tamper_values: list[bool] = []

    def post(_url: str, _body: bytes, _key: str, *, tamper: bool = False) -> object:
        tamper_values.append(tamper)
        return _result(401)

    monkeypatch.setitem(run.__globals__, "_post", post)

    assert run(_args("--tamper")) == 0
    assert tamper_values == [True]


@pytest.mark.parametrize(
    "mode,count", [("normal", 1), ("tamper", 1), ("replay", 2), ("burst", 21)]
)
def test_legacy_position_modes_remain_compatible(
    mode: str, count: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0

    def post(_url: str, _body: bytes, _key: str, *, tamper: bool = False) -> object:
        nonlocal calls
        calls += 1
        if mode == "tamper":
            assert tamper is True
            return _result(401)
        if mode == "replay":
            return _result(200, replayed=calls == 2)
        if mode == "burst" and calls == count:
            return _result(429)
        return _result(200)

    monkeypatch.setitem(run.__globals__, "_post", post)

    assert main([mode]) == 0
    assert calls == count


def test_normal_fails_when_response_is_not_200(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(
        run.__globals__, "_post", lambda *_args, **_kwargs: _result(500)
    )

    assert run(_args()) == 1


def test_tamper_fails_when_response_is_not_401(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(
        run.__globals__, "_post", lambda *_args, **_kwargs: _result(200)
    )

    assert run(_args("--tamper")) == 1


@pytest.mark.parametrize(
    "second_status,replayed", [(500, True), (200, False)]
)
def test_replay_fails_when_second_response_is_invalid(
    second_status: int,
    replayed: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def post(_url: str, _body: bytes, _key: str, *, tamper: bool = False) -> object:
        nonlocal calls
        assert tamper is False
        calls += 1
        if calls == 1:
            return _result(200)
        return _result(second_status, replayed=replayed)

    monkeypatch.setitem(run.__globals__, "_post", post)

    assert run(_args("--replay")) == 1
    assert calls == 2


def test_burst_fails_when_no_response_is_rate_limited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(
        run.__globals__, "_post", lambda *_args, **_kwargs: _result(200)
    )

    assert run(_args("--burst", "3")) == 1


@pytest.mark.parametrize("switch", ["--tamper", "--replay", "--burst"])
def test_legacy_mode_conflicts_with_new_switch(switch: str) -> None:
    argv = ["normal", switch]
    if switch == "--burst":
        argv.append("2")

    with pytest.raises(SystemExit) as raised:
        main(argv)

    assert raised.value.code == 2


def test_non_feishu_channel_is_rejected() -> None:
    with pytest.raises(SystemExit) as raised:
        _args("--channel", "web")

    assert raised.value.code == 2

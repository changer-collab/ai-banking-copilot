"""飞书协议适配器。

本模块只负责飞书报文的验签、解密、解析和卡片渲染；业务判级、身份绑定与执行继续由
现有渠道调度器和会话层完成。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import cast
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from ai_banking_copilot.web.channels.contracts import (
    CHANNEL_FEISHU,
    ConfirmCommand,
    InboundMessage,
    OutboundMessage,
)
from ai_banking_copilot.web.channels.ratelimit import retry_outbound
from ai_banking_copilot.web.channels.render import render_feishu_card

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
DEFAULT_REPLAY_ENCRYPT_KEY = "replay-key"
SIGNATURE_MAX_SKEW_S = 300.0
"""回调时间戳允许的偏移（秒）。超过即拒绝，避免旧报文被无限期重放。"""


class FeishuProtocolError(ValueError):
    """飞书报文无法验签、解密或解析。"""


def _post_json(
    url: str, payload: Mapping[str, object], headers: Mapping[str, str]
) -> tuple[int, Mapping[str, object]]:
    request = Request(
        url,
        data=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", **headers},
    )
    try:
        with urlopen(request, timeout=5) as response:
            raw = response.read()
            status = int(response.status)
    except HTTPError as error:
        return int(error.code), {}
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise FeishuProtocolError("invalid_api_response") from error
    if not isinstance(decoded, Mapping):
        raise FeishuProtocolError("invalid_api_response")
    return status, cast(Mapping[str, object], decoded)


@dataclass(frozen=True)
class FeishuConfig:
    """飞书配置；凭据只从环境变量读取，不写入业务日志。"""

    app_id: str | None = None
    app_secret: str | None = None
    verification_token: str | None = None
    encrypt_key: str | None = None
    replay_mode: bool = False

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> FeishuConfig:
        source = os.environ if env is None else env

        def value(name: str) -> str | None:
            raw = source.get(name, "").strip()
            return raw or None

        app_id = value("AIB_FEISHU_APP_ID")
        app_secret = value("AIB_FEISHU_APP_SECRET")
        verification_token = value("AIB_FEISHU_VERIFICATION_TOKEN")
        encrypt_key = value("AIB_FEISHU_ENCRYPT_KEY")
        explicit_replay = source.get("AIB_FEISHU_REPLAY_MODE", "").strip().lower()
        replay_mode = (explicit_replay in _TRUE_VALUES) or not all(
            (app_id, app_secret, verification_token, encrypt_key)
        )
        if replay_mode and encrypt_key is None:
            encrypt_key = value("AIB_FEISHU_REPLAY_KEY") or DEFAULT_REPLAY_ENCRYPT_KEY
        return cls(
            app_id=app_id,
            app_secret=app_secret,
            verification_token=verification_token,
            encrypt_key=encrypt_key,
            replay_mode=replay_mode,
        )

    @property
    def configured(self) -> bool:
        """四个生产凭据都存在时才算 live 配置。"""
        return all((self.app_id, self.app_secret, self.verification_token, self.encrypt_key))

    @property
    def mode(self) -> str:
        """对外健康口径：真实凭据齐全时为 adapter，否则进入本地 replay。"""
        return "adapter" if self.configured and not self.replay_mode else "replay"


@dataclass(frozen=True)
class FeishuEvent:
    """解密并解析后的飞书事件。"""

    event_type: str
    challenge: str | None
    token: str | None
    event_id: str | None
    inbound: InboundMessage | None
    command: ConfirmCommand | None = None
    """卡片按钮回调（`card.action.trigger`）：确认执行走同一条冻结校验。"""


def signature_for(encrypt_key: str, timestamp: str, nonce: str, body: bytes) -> str:
    """按飞书回调约定计算签名。"""
    material = timestamp.encode("utf-8") + nonce.encode("utf-8")
    material += encrypt_key.encode("utf-8") + body
    return hashlib.sha256(material).hexdigest()


def _pkcs7_pad(data: bytes, block_size: int = 16) -> bytes:
    padding = block_size - (len(data) % block_size)
    return data + bytes([padding]) * padding


def _pkcs7_unpad(data: bytes, block_size: int = 16) -> bytes:
    if not data or len(data) % block_size:
        raise FeishuProtocolError("invalid_ciphertext_length")
    padding = data[-1]
    if padding < 1 or padding > block_size or data[-padding:] != bytes([padding]) * padding:
        raise FeishuProtocolError("invalid_padding")
    return data[:-padding]


def _gf_mul(left: int, right: int) -> int:
    result = 0
    for _ in range(8):
        if right & 1:
            result ^= left
        left = ((left << 1) ^ (0x11B if left & 0x80 else 0)) & 0xFF
        right >>= 1
    return result


def _gf_pow(value: int, exponent: int) -> int:
    result = 1
    while exponent:
        if exponent & 1:
            result = _gf_mul(result, value)
        value = _gf_mul(value, value)
        exponent >>= 1
    return result


def _rotl8(value: int, count: int) -> int:
    return ((value << count) | (value >> (8 - count))) & 0xFF


def _build_sboxes() -> tuple[tuple[int, ...], tuple[int, ...]]:
    sbox: list[int] = []
    for value in range(256):
        inverse = 0 if value == 0 else _gf_pow(value, 254)
        substituted = inverse ^ _rotl8(inverse, 1) ^ _rotl8(inverse, 2)
        substituted ^= _rotl8(inverse, 3) ^ _rotl8(inverse, 4) ^ 0x63
        sbox.append(substituted)
    inverse_sbox = [0] * 256
    for value, substituted in enumerate(sbox):
        inverse_sbox[substituted] = value
    return tuple(sbox), tuple(inverse_sbox)


_SBOX, _INV_SBOX = _build_sboxes()
_RCON = (0, 1, 2, 4, 8, 16, 32, 64, 128, 27, 54, 108, 216, 171, 77)


def _round_keys(key: bytes) -> tuple[tuple[int, ...], ...]:
    if len(key) != 32:
        raise FeishuProtocolError("invalid_aes_key")
    words: list[list[int]] = [list(key[index : index + 4]) for index in range(0, 32, 4)]
    for index in range(8, 60):
        temp = words[index - 1][:]
        if index % 8 == 0:
            temp = temp[1:] + temp[:1]
            temp = [_SBOX[item] for item in temp]
            temp[0] ^= _RCON[index // 8]
        elif index % 8 == 4:
            temp = [_SBOX[item] for item in temp]
        words.append([words[index - 8][offset] ^ temp[offset] for offset in range(4)])
    return tuple(
        tuple(byte for word in words[start : start + 4] for byte in word)
        for start in range(0, 60, 4)
    )


def _add_round_key(state: list[int], round_key: tuple[int, ...]) -> None:
    for index in range(16):
        state[index] ^= round_key[index]


def _sub_bytes(state: list[int], box: tuple[int, ...]) -> None:
    for index in range(16):
        state[index] = box[state[index]]


def _shift_rows(state: list[int], *, inverse: bool = False) -> None:
    original = state[:]
    for row in range(4):
        for column in range(4):
            source_column = (column - row if inverse else column + row) % 4
            state[4 * column + row] = original[4 * source_column + row]


def _mix_columns(state: list[int], *, inverse: bool = False) -> None:
    for column in range(4):
        offset = 4 * column
        a0, a1, a2, a3 = state[offset : offset + 4]
        if inverse:
            state[offset : offset + 4] = [
                _gf_mul(a0, 14) ^ _gf_mul(a1, 11) ^ _gf_mul(a2, 13) ^ _gf_mul(a3, 9),
                _gf_mul(a0, 9) ^ _gf_mul(a1, 14) ^ _gf_mul(a2, 11) ^ _gf_mul(a3, 13),
                _gf_mul(a0, 13) ^ _gf_mul(a1, 9) ^ _gf_mul(a2, 14) ^ _gf_mul(a3, 11),
                _gf_mul(a0, 11) ^ _gf_mul(a1, 13) ^ _gf_mul(a2, 9) ^ _gf_mul(a3, 14),
            ]
        else:
            state[offset : offset + 4] = [
                _gf_mul(a0, 2) ^ _gf_mul(a1, 3) ^ a2 ^ a3,
                a0 ^ _gf_mul(a1, 2) ^ _gf_mul(a2, 3) ^ a3,
                a0 ^ a1 ^ _gf_mul(a2, 2) ^ _gf_mul(a3, 3),
                _gf_mul(a0, 3) ^ a1 ^ a2 ^ _gf_mul(a3, 2),
            ]


def _aes_block(data: bytes, key: bytes, *, encrypt: bool) -> bytes:
    if len(data) != 16:
        raise FeishuProtocolError("invalid_aes_block")
    keys = _round_keys(key)
    state = list(data)
    if encrypt:
        _add_round_key(state, keys[0])
        for round_index in range(1, 14):
            _sub_bytes(state, _SBOX)
            _shift_rows(state)
            _mix_columns(state)
            _add_round_key(state, keys[round_index])
        _sub_bytes(state, _SBOX)
        _shift_rows(state)
        _add_round_key(state, keys[14])
    else:
        _add_round_key(state, keys[14])
        for round_index in range(13, 0, -1):
            _shift_rows(state, inverse=True)
            _sub_bytes(state, _INV_SBOX)
            _add_round_key(state, keys[round_index])
            _mix_columns(state, inverse=True)
        _shift_rows(state, inverse=True)
        _sub_bytes(state, _INV_SBOX)
        _add_round_key(state, keys[0])
    return bytes(state)


def _aes_crypt(data: bytes, key: bytes, iv: bytes, *, encrypt: bool) -> bytes:
    if len(iv) != 16 or len(data) % 16:
        raise FeishuProtocolError("invalid_ciphertext_length")
    previous = iv
    result = bytearray()
    for offset in range(0, len(data), 16):
        block = data[offset : offset + 16]
        if encrypt:
            mixed = bytes(
                left ^ right for left, right in zip(block, previous, strict=True)
            )
            encrypted = _aes_block(mixed, key, encrypt=True)
            result.extend(encrypted)
            previous = encrypted
        else:
            decrypted = _aes_block(block, key, encrypt=False)
            clear = bytes(
                left ^ right for left, right in zip(decrypted, previous, strict=True)
            )
            result.extend(clear)
            previous = block
    return bytes(result)


def encrypt_payload(payload: Mapping[str, object], encrypt_key: str) -> str:
    """加密一个飞书 JSON payload，返回 base64(IV + ciphertext)。"""
    iv = os.urandom(16)
    key = hashlib.sha256(encrypt_key.encode("utf-8")).digest()
    plaintext = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    encrypted = _aes_crypt(_pkcs7_pad(plaintext), key, iv, encrypt=True)
    return base64.b64encode(iv + encrypted).decode("ascii")


def _header(headers: Mapping[str, str], name: str) -> str:
    target = name.lower()
    for key, value in headers.items():
        if key.lower() == target:
            return value.strip()
    return ""


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise FeishuProtocolError(f"invalid_{label}")
    return cast(Mapping[str, object], value)


def _text_content(value: object) -> str:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as error:
            raise FeishuProtocolError("invalid_message_content") from error
    content = _mapping(value, "message_content")
    text = content.get("text")
    if not isinstance(text, str) or not text.strip():
        raise FeishuProtocolError("message_text_missing")
    return text.strip()


class FeishuAdapter:
    """实现渠道协议，不直接调用 Session 或 CoreBank。"""

    name = CHANNEL_FEISHU

    def __init__(self, config: FeishuConfig) -> None:
        self.config = config

    @property
    def configured(self) -> bool:
        return self.config.configured

    @property
    def replay_mode(self) -> bool:
        return self.config.mode == "replay"

    def verify(
        self, headers: Mapping[str, str], body: bytes, *, now: float | None = None
    ) -> bool:
        key = self.config.encrypt_key
        timestamp = _header(headers, "X-Lark-Request-Timestamp")
        nonce = _header(headers, "X-Lark-Request-Nonce")
        provided = _header(headers, "X-Lark-Signature")
        if not key or not timestamp or not nonce or not provided:
            return False
        if not self._fresh(timestamp, now):
            return False
        expected = signature_for(key, timestamp, nonce, body)
        return hmac.compare_digest(expected, provided)

    @staticmethod
    def _fresh(timestamp: str, now: float | None) -> bool:
        """时间戳必须在 ±5 分钟内：签名正确但报文陈旧，同样视为重放。"""
        try:
            sent = float(timestamp)
        except ValueError:
            return False
        current = time.time() if now is None else now
        return abs(current - sent) <= SIGNATURE_MAX_SKEW_S

    def decode_body(self, body: bytes) -> Mapping[str, object]:
        try:
            payload = _mapping(json.loads(body.decode("utf-8")), "body")
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise FeishuProtocolError("invalid_json_body") from error
        encrypted = payload.get("encrypt")
        if encrypted is None:
            return payload
        if not isinstance(encrypted, str) or not self.config.encrypt_key:
            raise FeishuProtocolError("encrypted_body_without_key")
        try:
            raw = base64.b64decode(encrypted, validate=True)
        except (ValueError, binascii.Error) as error:
            raise FeishuProtocolError("invalid_base64_ciphertext") from error
        if len(raw) <= 16:
            raise FeishuProtocolError("ciphertext_too_short")
        key = hashlib.sha256(self.config.encrypt_key.encode("utf-8")).digest()
        plaintext = _aes_crypt(raw[16:], key, raw[:16], encrypt=False)
        try:
            decoded = json.loads(_pkcs7_unpad(plaintext).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise FeishuProtocolError("invalid_decrypted_payload") from error
        return _mapping(decoded, "decrypted_payload")

    def parse_event(self, payload: Mapping[str, object]) -> FeishuEvent:
        event_type = str(payload.get("type") or "")
        if event_type == "url_verification":
            token = payload.get("token")
            token_text = token if isinstance(token, str) else None
            expected = self.config.verification_token
            if expected and token_text != expected:
                raise FeishuProtocolError("verification_token_mismatch")
            challenge = payload.get("challenge")
            if not isinstance(challenge, str) or not challenge:
                raise FeishuProtocolError("challenge_missing")
            return FeishuEvent(event_type, challenge, token_text, None, None)

        header = _mapping(payload.get("header"), "header")
        actual_type = str(header.get("event_type") or event_type)
        event_id = header.get("event_id")
        event_id_text = event_id if isinstance(event_id, str) else None
        if not event_id_text:
            raise FeishuProtocolError("event_id_missing")
        token = header.get("token")
        token_text = token if isinstance(token, str) else None
        expected = self.config.verification_token
        if expected and token_text != expected:
            raise FeishuProtocolError("verification_token_mismatch")
        if actual_type == "card.action.trigger":
            command = self._parse_card_action(payload, event_id_text)
            return FeishuEvent(
                actual_type, None, token_text, event_id_text, None, command
            )
        if actual_type != "im.message.receive_v1":
            raise FeishuProtocolError(f"unsupported_event:{actual_type}")
        inbound = self._parse_message(payload, event_id_text)
        return FeishuEvent(actual_type, None, token_text, event_id_text, inbound)

    def _parse_card_action(
        self, payload: Mapping[str, object], event_id: str
    ) -> ConfirmCommand:
        """解析卡片按钮回调：只接受 `card_id + frozen_hash`，其余交给会话层冻结校验。"""
        event = _mapping(payload.get("event"), "event")
        operator = _mapping(event.get("operator"), "operator")
        open_id = operator.get("open_id")
        if not isinstance(open_id, str) or not open_id.strip():
            raise FeishuProtocolError("operator_open_id_missing")
        action = _mapping(event.get("action"), "action")
        value = _mapping(action.get("value"), "action_value")
        card_id = value.get("card_id")
        frozen_hash = value.get("frozen_hash")
        if not isinstance(card_id, str) or not card_id:
            raise FeishuProtocolError("card_id_missing")
        if not isinstance(frozen_hash, str) or not frozen_hash:
            raise FeishuProtocolError("frozen_hash_missing")
        context = event.get("context")
        chat_id: str | None = None
        if isinstance(context, Mapping):
            raw_chat = cast(Mapping[str, object], context).get("open_chat_id")
            if isinstance(raw_chat, str) and raw_chat.strip():
                chat_id = raw_chat.strip()
        code = value.get("verification_code")
        steps = value.get("steps")
        try:
            return ConfirmCommand(
                channel=CHANNEL_FEISHU,
                external_user_id=open_id.strip(),
                external_chat_id=chat_id,
                event_id=event_id,
                card_id=card_id,
                frozen_hash=frozen_hash,
                verification_code=code if isinstance(code, str) else None,
                steps=steps if isinstance(steps, int) and not isinstance(steps, bool) else None,
            )
        except ValueError as error:
            raise FeishuProtocolError(f"invalid_card_action:{error}") from error

    def parse(self, payload: Mapping[str, object]) -> InboundMessage:
        """解析普通文本事件；URL 校验事件请调用 parse_event。"""
        event = self.parse_event(payload)
        if event.inbound is None:
            raise FeishuProtocolError("not_message_event")
        return event.inbound

    def _parse_message(
        self, payload: Mapping[str, object], event_id: str | None
    ) -> InboundMessage:
        event = _mapping(payload.get("event"), "event")
        sender = _mapping(event.get("sender"), "sender")
        sender_id = _mapping(sender.get("sender_id"), "sender_id")
        external_user_id = sender_id.get("open_id") or sender_id.get("user_id")
        if not isinstance(external_user_id, str) or not external_user_id:
            raise FeishuProtocolError("sender_id_missing")
        message = _mapping(event.get("message"), "message")
        if message.get("message_type") not in (None, "text"):
            raise FeishuProtocolError("unsupported_message_type")
        chat_id = message.get("chat_id")
        if not isinstance(chat_id, str) or not chat_id:
            raise FeishuProtocolError("chat_id_missing")
        return InboundMessage(
            channel=CHANNEL_FEISHU,
            external_user_id=external_user_id,
            external_chat_id=chat_id,
            event_id=event_id,
            text=_text_content(message.get("content")),
            raw=payload,
        )

    def render(self, outbound: OutboundMessage) -> Mapping[str, object]:
        """复用统一计划卡事实，并附上飞书确认按钮。"""
        rendered = dict(render_feishu_card(outbound))
        card = outbound.card
        if not isinstance(card, Mapping):
            return rendered
        card_id = card.get("card_id")
        frozen_hash = card.get("frozen_hash")
        if not isinstance(card_id, str) or not isinstance(frozen_hash, str):
            return rendered
        elements = rendered.get("elements")
        if not isinstance(elements, list):
            return rendered
        elements.append(
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "确认办理"},
                        "type": "primary",
                        "value": {
                            "action": "confirm",
                            "card_id": card_id,
                            "frozen_hash": frozen_hash,
                        },
                    }
                ],
            }
        )
        return rendered

    def send(
        self,
        outbound: OutboundMessage,
        *,
        on_retry: Callable[[int, float], None] | None = None,
    ) -> int:
        """向真实飞书会话发送交互卡片；回调入口可将此方法放入后台任务。"""
        if not self.config.configured or self.replay_mode:
            raise FeishuProtocolError("feishu_send_requires_live_config")
        app_id = self.config.app_id
        app_secret = self.config.app_secret
        if app_id is None or app_secret is None:
            raise FeishuProtocolError("feishu_credentials_missing")

        def attempt() -> int:
            token_status, token_body = _post_json(
                "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                {"app_id": app_id, "app_secret": app_secret},
                {},
            )
            if token_status >= 500:
                return token_status
            token = token_body.get("tenant_access_token")
            if token_status != 200 or not isinstance(token, str) or not token:
                raise FeishuProtocolError("feishu_token_request_failed")
            status, response = _post_json(
                "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
                {
                    "receive_id": outbound.chat_id,
                    "msg_type": "interactive",
                    "content": json.dumps(self.render(outbound), ensure_ascii=False),
                },
                {"Authorization": f"Bearer {token}"},
            )
            if status >= 500:
                return status
            if status != 200 or response.get("code", 0) != 0:
                raise FeishuProtocolError("feishu_message_send_failed")
            return status

        return retry_outbound(
            attempt,
            attempts=3,
            sleeper=time.sleep,
            on_retry=on_retry,
        )

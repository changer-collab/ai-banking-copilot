"""银行写回执的只读接缝；请求键与到期业务键均按原始主键读取。"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Protocol

from ai_banking_copilot.gateway.store import Store


class ReceiptReadError(ValueError):
    """回执损坏或列与内容身份不一致。"""


@dataclass(frozen=True)
class ReceiptRecord:
    idempotency_key: str
    action: str
    status: str
    record_ids: tuple[str, ...]
    effect_ids: tuple[str, ...]


class ReceiptReader(Protocol):
    def by_key(self, key: str) -> ReceiptRecord | None: ...


class StoreReceiptReader:
    def __init__(self, store: Store) -> None:
        self._store = store

    def by_key(self, key: str) -> ReceiptRecord | None:
        try:
            rows = self._store.query(
                "SELECT idempotency_key, action, receipt_json FROM write_receipts "
                "WHERE idempotency_key = ?", (key,),
            )
        except sqlite3.Error as error:
            raise ReceiptReadError("receipt_read_failed") from error
        if not rows:
            return None
        row = rows[0]
        try:
            data = json.loads(str(row["receipt_json"]))
            if not isinstance(data, dict):
                raise ReceiptReadError("invalid_receipt_payload")
            if data.get("idempotency_key") != key or data.get("action") != row["action"]:
                raise ReceiptReadError("receipt_identity_mismatch")
            for name in ("record_ids", "effect_ids"):
                if not isinstance(data.get(name), list) or any(
                    not isinstance(item, str) or not item for item in data[name]
                ):
                    raise ReceiptReadError("invalid_receipt_records")
            if not isinstance(data.get("status"), str):
                raise ReceiptReadError("invalid_receipt_status")
            return ReceiptRecord(key, str(row["action"]), data["status"],
                                 tuple(data["record_ids"]), tuple(data["effect_ids"]))
        except (KeyError, TypeError, json.JSONDecodeError) as error:
            raise ReceiptReadError("invalid_receipt_payload") from error

    def by_business_key(self, key: str) -> ReceiptRecord | None:
        return self.by_key(key)

    def by_request_key(self, key: str) -> ReceiptRecord | None:
        return self.by_key(key)


ReceiptStore = StoreReceiptReader


def load_receipt(store: Store, business_key: str) -> ReceiptRecord | None:
    """便捷只读入口；business_key 就是 write_receipts 的持久化主键。"""
    return StoreReceiptReader(store).by_business_key(business_key)

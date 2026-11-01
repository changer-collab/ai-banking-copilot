"""理财交易确认留痕：四件套阅读时长、风险揭示抄录哈希、分红方式与偏好。

口径：
- 只保存**演示交易确认**所需的最小事实，不保存任何密码、验证码、生物特征；
- `declaration_text` 是用户逐字抄录的声明；另存 `sha256` 便于只读比对；
- 本表不参与资金动作裁决，只用于「确认面是否真的被走过」的留痕与测试。
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Protocol, runtime_checkable
from uuid import uuid4

from ai_banking_copilot.gateway.hashing import sha256_hex
from ai_banking_copilot.gateway.models import WealthTradeAck
from ai_banking_copilot.gateway.store import Store

WEALTH_TRADE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS wealth_trade_acks (
    ack_id           TEXT PRIMARY KEY,
    user_id          TEXT NOT NULL,
    product_id       TEXT NOT NULL,
    action           TEXT NOT NULL,
    doc_versions     TEXT NOT NULL,
    read_seconds     TEXT NOT NULL,
    declaration_text TEXT,
    declaration_hash TEXT,
    dividend_method  TEXT,
    auto_redeem_pref INTEGER,
    account_id       TEXT,
    created_at       TEXT NOT NULL
);
"""


@runtime_checkable
class WealthTradeWriter(Protocol):
    """交易确认留痕写入协议（web 层只依赖本协议，不持有写连接）。"""

    def save_trade_ack(
        self,
        *,
        user_id: str,
        product_id: str,
        action: str,
        doc_versions: dict[str, str],
        read_seconds: dict[str, int],
        declaration_text: str | None,
        dividend_method: str | None,
        auto_redeem_pref: bool | None,
        account_id: str | None,
        created_at: datetime | None = None,
    ) -> str:
        """写一条确认留痕并返回 `ack_id`。"""
        ...


def init_wealth_trade_schema(store: Store) -> None:
    with store.transaction() as conn:
        conn.executescript(WEALTH_TRADE_SCHEMA_SQL)


def save_trade_ack(
    store: Store,
    *,
    user_id: str,
    product_id: str,
    action: str,
    doc_versions: dict[str, str],
    read_seconds: dict[str, int],
    declaration_text: str | None,
    dividend_method: str | None,
    auto_redeem_pref: bool | None,
    account_id: str | None,
    created_at: datetime | None = None,
) -> str:
    """写一条确认留痕并返回 `ack_id`。"""
    ack_id = f"ack_{uuid4().hex}"
    stamp = (created_at or datetime.now()).isoformat(timespec="seconds")
    declaration_hash = sha256_hex(declaration_text) if declaration_text else None
    with store.transaction() as conn:
        conn.execute(
            "INSERT INTO wealth_trade_acks("
            "ack_id, user_id, product_id, action, doc_versions, read_seconds,"
            " declaration_text, declaration_hash, dividend_method, auto_redeem_pref,"
            " account_id, created_at"
            ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                ack_id,
                user_id,
                product_id,
                action,
                json.dumps(doc_versions, ensure_ascii=False, sort_keys=True),
                json.dumps(read_seconds, ensure_ascii=False, sort_keys=True),
                declaration_text,
                declaration_hash,
                dividend_method,
                None if auto_redeem_pref is None else int(auto_redeem_pref),
                account_id,
                stamp,
            ),
        )
    return ack_id


class StoreWealthTradeWriter:
    """`WealthTradeWriter` 的默认实现：写连接上保存确认留痕。"""

    def __init__(self, store: Store) -> None:
        self._store = store

    def save_trade_ack(
        self,
        *,
        user_id: str,
        product_id: str,
        action: str,
        doc_versions: dict[str, str],
        read_seconds: dict[str, int],
        declaration_text: str | None,
        dividend_method: str | None,
        auto_redeem_pref: bool | None,
        account_id: str | None,
        created_at: datetime | None = None,
    ) -> str:
        return save_trade_ack(
            self._store,
            user_id=user_id,
            product_id=product_id,
            action=action,
            doc_versions=doc_versions,
            read_seconds=read_seconds,
            declaration_text=declaration_text,
            dividend_method=dividend_method,
            auto_redeem_pref=auto_redeem_pref,
            account_id=account_id,
            created_at=created_at,
        )


def trade_acks_of(store: Store, user_id: str) -> tuple[WealthTradeAck, ...]:
    """本人确认留痕（按时间倒序）；只读。"""
    rows = store.query(
        "SELECT * FROM wealth_trade_acks WHERE user_id = ? ORDER BY created_at DESC",
        (user_id,),
    )
    return tuple(_ack(row) for row in rows)


def trade_ack(store: Store, ack_id: str) -> WealthTradeAck | None:
    rows = store.query("SELECT * FROM wealth_trade_acks WHERE ack_id = ?", (ack_id,))
    return None if not rows else _ack(rows[0])


def _ack(row: object) -> WealthTradeAck:
    return WealthTradeAck(
        ack_id=str(row["ack_id"]),  # type: ignore[index]
        user_id=str(row["user_id"]),  # type: ignore[index]
        product_id=str(row["product_id"]),  # type: ignore[index]
        action=str(row["action"]),  # type: ignore[index]
        doc_versions=json.loads(str(row["doc_versions"])),  # type: ignore[index]
        read_seconds=json.loads(str(row["read_seconds"])),  # type: ignore[index]
        declaration_text=(
            None if row["declaration_text"] is None else str(row["declaration_text"])  # type: ignore[index]
        ),
        declaration_hash=(
            None if row["declaration_hash"] is None else str(row["declaration_hash"])  # type: ignore[index]
        ),
        dividend_method=(
            None if row["dividend_method"] is None else str(row["dividend_method"])  # type: ignore[index]
        ),
        auto_redeem_pref=(
            None if row["auto_redeem_pref"] is None else bool(row["auto_redeem_pref"])  # type: ignore[index]
        ),
        account_id=(
            None if row["account_id"] is None else str(row["account_id"])  # type: ignore[index]
        ),
        created_at=str(row["created_at"]),  # type: ignore[index]
    )

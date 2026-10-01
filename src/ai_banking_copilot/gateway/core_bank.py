"""模拟核心银行写原语（TCB 的唯一写面）。

工程纪律：
- **幂等**：每个改写都带 `idempotency_key`；`write_receipts` 命中即回放，不重复执行。
- **全或无**：单次改写包在一个 SQLite 事务里，异常即整体回滚（不产生"改了一半"）。
- **确定性 id**：effect id 由 `action + key` 派生，重放得到**同一**标识（可对账）。
- 本层**不做**授权判断：裁决是 `policy.pdp` 的事，本层只做"能不能办"的事实校验。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date

from ai_banking_copilot.gateway.errors import (
    BlockedPayeeError,
    CoreBankError,
    IdempotencyConflictError,
    InsufficientFundsError,
    StoreError,
    UnknownAccountError,
    UnknownCardError,
    UnknownPayeeError,
    UnknownProductError,
    UnknownSubscriptionError,
)
from ai_banking_copilot.gateway.hashing import sha256_hex
from ai_banking_copilot.gateway.models import Money
from ai_banking_copilot.gateway.store import Store, StoreMode

# 演示与评测的基准记账日（固定值，保证评测可复现）
DEFAULT_BOOKING_DATE = date(2026, 10, 1)


@dataclass(frozen=True)
class WriteReceipt:
    """一次写操作的确定性回执。`replayed=True` 表示本次是幂等回放。"""

    idempotency_key: str
    action: str
    status: str
    record_ids: tuple[str, ...]
    effect_ids: tuple[str, ...]
    replayed: bool = False


def _effect_id(kind: str, action: str, idempotency_key: str) -> str:
    digest = sha256_hex(f"{action}:{idempotency_key}")[:16]
    return f"{kind}_{digest}"


class CoreBank:
    """模拟核心银行。所有写操作的唯一入口。"""

    def __init__(self, store: Store) -> None:
        if store.mode is not StoreMode.READ_WRITE:
            raise StoreError("CoreBank 需要读写连接（TCB 写权限独占）")
        self._store = store

    def transfer(
        self,
        *,
        user_id: str,
        account_id: str,
        payee_id: str,
        amount: Money,
        idempotency_key: str,
        booked_on: date,
    ) -> WriteReceipt:
        if amount.cents <= 0:
            raise CoreBankError("转账金额必须为正")

        def body(conn: sqlite3.Connection) -> WriteReceipt:
            account = conn.execute(
                "SELECT balance_cents FROM accounts WHERE account_id = ? AND user_id = ?",
                (account_id, user_id),
            ).fetchone()
            if account is None:
                raise UnknownAccountError(account_id)
            payee = conn.execute(
                "SELECT blocked FROM payees WHERE payee_id = ? AND user_id = ?",
                (payee_id, user_id),
            ).fetchone()
            if payee is None:
                raise UnknownPayeeError(payee_id)
            if int(payee["blocked"]) == 1:
                raise BlockedPayeeError(f"收款对象 {payee_id} 处于 blocked 状态")
            if int(account["balance_cents"]) < amount.cents:
                raise InsufficientFundsError(
                    f"余额不足：需要 {amount.cents} 分，可用 {int(account['balance_cents'])} 分"
                )
            txn_id = _effect_id("txn", "transfer", idempotency_key)
            conn.execute(
                "UPDATE accounts SET balance_cents = balance_cents - ? WHERE account_id = ?",
                (amount.cents, account_id),
            )
            conn.execute(
                "INSERT INTO transactions(txn_id, user_id, account_id, booked_on, amount_cents,"
                " merchant, category, channel, direction) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    txn_id,
                    user_id,
                    account_id,
                    booked_on.isoformat(),
                    -amount.cents,
                    f"转给{payee_id}",
                    "转账",
                    "agent",
                    "out",
                ),
            )
            return WriteReceipt(
                idempotency_key=idempotency_key,
                action="transfer",
                status="settled",
                record_ids=(f"transactions:{txn_id}",),
                effect_ids=(f"accounts:{account_id}", f"txn_{txn_id}"),
            )

        return self._run_once("transfer", idempotency_key, body)

    def cancel_subscription(self, *, subscription_id: str, idempotency_key: str) -> WriteReceipt:
        def body(conn: sqlite3.Connection) -> WriteReceipt:
            row = conn.execute(
                "SELECT status FROM subscriptions WHERE subscription_id = ?", (subscription_id,)
            ).fetchone()
            if row is None:
                raise UnknownSubscriptionError(subscription_id)
            conn.execute(
                "UPDATE subscriptions SET status = 'cancelled' WHERE subscription_id = ?",
                (subscription_id,),
            )
            return WriteReceipt(
                idempotency_key=idempotency_key,
                action="cancel_subscription",
                status="cancelled",
                record_ids=(f"subscriptions:{subscription_id}",),
                effect_ids=(f"subscription_cancel_{subscription_id}",),
            )

        return self._run_once("cancel_subscription", idempotency_key, body)

    def freeze_funds(
        self, *, account_id: str, amount: Money, reason: str, idempotency_key: str
    ) -> WriteReceipt:
        if amount.cents <= 0:
            raise CoreBankError("冻结金额必须为正")

        def body(conn: sqlite3.Connection) -> WriteReceipt:
            account = conn.execute(
                "SELECT balance_cents FROM accounts WHERE account_id = ?", (account_id,)
            ).fetchone()
            if account is None:
                raise UnknownAccountError(account_id)
            if int(account["balance_cents"]) < amount.cents:
                raise InsufficientFundsError(f"可用余额不足以冻结 {amount.cents} 分")
            hold_id = _effect_id("hold", "freeze_funds", idempotency_key)
            conn.execute(
                "UPDATE accounts SET balance_cents = balance_cents - ? WHERE account_id = ?",
                (amount.cents, account_id),
            )
            conn.execute(
                "INSERT INTO holds(hold_id, account_id, amount_cents, reason, status)"
                " VALUES(?, ?, ?, ?, 'active')",
                (hold_id, account_id, amount.cents, reason),
            )
            return WriteReceipt(
                idempotency_key=idempotency_key,
                action="freeze_funds",
                status="held",
                record_ids=(f"holds:{hold_id}",),
                effect_ids=(f"holds:{hold_id}",),
            )

        return self._run_once("freeze_funds", idempotency_key, body)

    def unfreeze_funds(self, *, hold_id: str, idempotency_key: str) -> WriteReceipt:
        def body(conn: sqlite3.Connection) -> WriteReceipt:
            row = conn.execute(
                "SELECT account_id, amount_cents, status FROM holds WHERE hold_id = ?", (hold_id,)
            ).fetchone()
            if row is None:
                raise CoreBankError(f"冻结记录不存在：{hold_id}")
            if str(row["status"]) == "released":
                return WriteReceipt(
                    idempotency_key=idempotency_key,
                    action="unfreeze_funds",
                    status="released",
                    record_ids=(f"holds:{hold_id}",),
                    effect_ids=(f"holds:{hold_id}",),
                    replayed=True,
                )
            conn.execute(
                "UPDATE accounts SET balance_cents = balance_cents + ? WHERE account_id = ?",
                (int(row["amount_cents"]), str(row["account_id"])),
            )
            conn.execute("UPDATE holds SET status = 'released' WHERE hold_id = ?", (hold_id,))
            return WriteReceipt(
                idempotency_key=idempotency_key,
                action="unfreeze_funds",
                status="released",
                record_ids=(f"holds:{hold_id}",),
                effect_ids=(f"accounts:{row['account_id']}",),
            )

        return self._run_once("unfreeze_funds", idempotency_key, body)

    def subscribe_product(
        self,
        *,
        user_id: str,
        account_id: str,
        product_id: str,
        amount: Money,
        idempotency_key: str,
    ) -> WriteReceipt:
        return self._wealth_flow(
            action="subscribe_product",
            user_id=user_id,
            account_id=account_id,
            product_id=product_id,
            amount=amount,
            idempotency_key=idempotency_key,
            direction="out",
        )

    def redeem_product(
        self,
        *,
        user_id: str,
        account_id: str,
        product_id: str,
        amount: Money,
        idempotency_key: str,
    ) -> WriteReceipt:
        return self._wealth_flow(
            action="redeem_product",
            user_id=user_id,
            account_id=account_id,
            product_id=product_id,
            amount=amount,
            idempotency_key=idempotency_key,
            direction="in",
        )

    def set_card_status(self, *, card_id: str, status: str, idempotency_key: str) -> WriteReceipt:
        if status not in ("active", "lost", "locked"):
            raise CoreBankError(f"非法卡片状态：{status}")

        def body(conn: sqlite3.Connection) -> WriteReceipt:
            row = conn.execute("SELECT card_id FROM cards WHERE card_id = ?", (card_id,)).fetchone()
            if row is None:
                raise UnknownCardError(card_id)
            conn.execute("UPDATE cards SET status = ? WHERE card_id = ?", (status, card_id))
            return WriteReceipt(
                idempotency_key=idempotency_key,
                action="set_card_status",
                status=status,
                record_ids=(f"cards:{card_id}",),
                effect_ids=(f"cards:{card_id}",),
            )

        return self._run_once("set_card_status", idempotency_key, body)

    def apply_virtual_card(
        self, *, user_id: str, daily_limit: Money, idempotency_key: str
    ) -> WriteReceipt:
        if daily_limit.cents < 0:
            raise CoreBankError("虚拟卡额度不得为负")

        def body(conn: sqlite3.Connection) -> WriteReceipt:
            user = conn.execute(
                "SELECT user_id FROM users WHERE user_id = ?", (user_id,)
            ).fetchone()
            if user is None:
                raise CoreBankError(f"用户不存在：{user_id}")
            card_id = _effect_id("card", "apply_virtual_card", idempotency_key)
            conn.execute(
                "INSERT INTO cards(card_id, user_id, card_type, status, daily_limit_cents)"
                " VALUES(?, ?, 'virtual', 'active', ?)",
                (card_id, user_id, daily_limit.cents),
            )
            return WriteReceipt(
                idempotency_key=idempotency_key,
                action="apply_virtual_card",
                status="applied",
                record_ids=(f"cards:{card_id}",),
                effect_ids=(f"cards:{card_id}",),
            )

        return self._run_once("apply_virtual_card", idempotency_key, body)

    def set_card_limit(
        self, *, card_id: str, daily_limit: Money, idempotency_key: str
    ) -> WriteReceipt:
        if daily_limit.cents < 0:
            raise CoreBankError("卡片额度不得为负")

        def body(conn: sqlite3.Connection) -> WriteReceipt:
            row = conn.execute("SELECT card_id FROM cards WHERE card_id = ?", (card_id,)).fetchone()
            if row is None:
                raise UnknownCardError(card_id)
            conn.execute(
                "UPDATE cards SET daily_limit_cents = ? WHERE card_id = ?",
                (daily_limit.cents, card_id),
            )
            return WriteReceipt(
                idempotency_key=idempotency_key,
                action="set_card_limit",
                status="updated",
                record_ids=(f"cards:{card_id}",),
                effect_ids=(f"cards:{card_id}",),
            )

        return self._run_once("set_card_limit", idempotency_key, body)

    def sign_subscription(
        self,
        *,
        user_id: str,
        merchant: str,
        amount: Money,
        period: str,
        next_charge_on: date,
        idempotency_key: str,
    ) -> WriteReceipt:
        if amount.cents <= 0:
            raise CoreBankError("签约金额必须为正")
        if period not in ("monthly", "yearly"):
            raise CoreBankError(f"非法订阅周期：{period}")

        def body(conn: sqlite3.Connection) -> WriteReceipt:
            user = conn.execute(
                "SELECT user_id FROM users WHERE user_id = ?", (user_id,)
            ).fetchone()
            if user is None:
                raise CoreBankError(f"用户不存在：{user_id}")
            subscription_id = _effect_id("sub", "sign_subscription", idempotency_key)
            conn.execute(
                "INSERT INTO subscriptions(subscription_id, user_id, merchant, amount_cents,"
                " period, next_charge_on, status) VALUES(?, ?, ?, ?, ?, ?, 'active')",
                (
                    subscription_id,
                    user_id,
                    merchant,
                    amount.cents,
                    period,
                    next_charge_on.isoformat(),
                ),
            )
            return WriteReceipt(
                idempotency_key=idempotency_key,
                action="sign_subscription",
                status="signed",
                record_ids=(f"subscriptions:{subscription_id}",),
                effect_ids=(f"subscriptions:{subscription_id}",),
            )

        return self._run_once("sign_subscription", idempotency_key, body)

    def _wealth_flow(
        self,
        *,
        action: str,
        user_id: str,
        account_id: str,
        product_id: str,
        amount: Money,
        idempotency_key: str,
        direction: str,
    ) -> WriteReceipt:
        if amount.cents <= 0:
            raise CoreBankError("金额必须为正")

        def body(conn: sqlite3.Connection) -> WriteReceipt:
            product = conn.execute(
                "SELECT min_amount_cents FROM wealth_products WHERE product_id = ?", (product_id,)
            ).fetchone()
            if product is None:
                raise UnknownProductError(product_id)
            min_cents = int(product["min_amount_cents"])
            if amount.cents < min_cents:
                raise CoreBankError(f"{product_id} 起购金额为 {min_cents} 分")
            account = conn.execute(
                "SELECT balance_cents FROM accounts WHERE account_id = ? AND user_id = ?",
                (account_id, user_id),
            ).fetchone()
            if account is None:
                raise UnknownAccountError(account_id)
            if direction == "out" and int(account["balance_cents"]) < amount.cents:
                raise InsufficientFundsError("可用余额不足")
            signed = -amount.cents if direction == "out" else amount.cents
            txn_id = _effect_id("txn", action, idempotency_key)
            conn.execute(
                "UPDATE accounts SET balance_cents = balance_cents + ? WHERE account_id = ?",
                (signed, account_id),
            )
            conn.execute(
                "INSERT INTO transactions(txn_id, user_id, account_id, booked_on, amount_cents,"
                " merchant, category, channel, direction) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    txn_id,
                    user_id,
                    account_id,
                    DEFAULT_BOOKING_DATE.isoformat(),
                    signed,
                    product_id,
                    "理财",
                    "agent",
                    direction,
                ),
            )
            return WriteReceipt(
                idempotency_key=idempotency_key,
                action=action,
                status="settled",
                record_ids=(f"transactions:{txn_id}",),
                effect_ids=(f"accounts:{account_id}",),
            )

        return self._run_once(action, idempotency_key, body)

    def _run_once(
        self,
        action: str,
        idempotency_key: str,
        body: Callable[[sqlite3.Connection], WriteReceipt],
    ) -> WriteReceipt:
        existing = self._existing_receipt(action, idempotency_key)
        if existing is not None:
            return existing
        try:
            with self._store.transaction() as conn:
                receipt = body(conn)
                self._store_receipt(conn, receipt)
                return receipt
        except sqlite3.IntegrityError as error:
            if "write_receipts" in str(error) or "UNIQUE" in str(error):
                replay = self._existing_receipt(action, idempotency_key)
                if replay is not None:
                    return replay
            raise

    def _existing_receipt(self, action: str, idempotency_key: str) -> WriteReceipt | None:
        rows = self._store.query(
            "SELECT action, receipt_json FROM write_receipts WHERE idempotency_key = ?",
            (idempotency_key,),
        )
        if not rows:
            return None
        row = rows[0]
        stored_action = str(row["action"])
        if stored_action != action:
            raise IdempotencyConflictError(
                f"幂等键 {idempotency_key} 已用于 {stored_action}，不得复用于 {action}"
            )
        data = json.loads(str(row["receipt_json"]))
        return WriteReceipt(
            idempotency_key=str(data["idempotency_key"]),
            action=str(data["action"]),
            status=str(data["status"]),
            record_ids=tuple(str(item) for item in data["record_ids"]),
            effect_ids=tuple(str(item) for item in data["effect_ids"]),
            replayed=True,
        )

    def _store_receipt(self, conn: sqlite3.Connection, receipt: WriteReceipt) -> None:
        payload = {
            "idempotency_key": receipt.idempotency_key,
            "action": receipt.action,
            "status": receipt.status,
            "record_ids": list(receipt.record_ids),
            "effect_ids": list(receipt.effect_ids),
        }
        conn.execute(
            "INSERT INTO write_receipts(idempotency_key, action, receipt_json) VALUES(?, ?, ?)",
            (
                receipt.idempotency_key,
                receipt.action,
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
            ),
        )

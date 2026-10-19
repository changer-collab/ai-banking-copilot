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
from threading import RLock

from ai_banking_copilot.gateway.errors import (
    BlockedPayeeError,
    CoreBankError,
    IdempotencyConflictError,
    InsufficientFundsError,
    NotReversibleError,
    StoreError,
    UnknownAccountError,
    UnknownCardError,
    UnknownPayeeError,
    UnknownProductError,
    UnknownSubscriptionError,
)
from ai_banking_copilot.gateway.hashing import sha256_hex
from ai_banking_copilot.gateway.models import Money
from ai_banking_copilot.gateway.preconditions import (
    BeforeWrite,
    PreconditionStatus,
    WritePreconditionError,
)
from ai_banking_copilot.gateway.store import Store, StoreMode
from ai_banking_copilot.gateway.transfer_store import (
    COLLECTION_REQUESTS_SCHEMA_SQL,
    SCHEDULED_TRANSFERS_SCHEMA_SQL,
    due_execution_key,
)

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
        # Web 的多个 Session 共享组合根中的 CoreBank；SQLite 连接本身不可并发事务。
        self._write_lock = RLock()

    def transfer(
        self,
        *,
        user_id: str,
        account_id: str,
        payee_id: str,
        amount: Money,
        idempotency_key: str,
        booked_on: date,
        schedule_id: str | None = None,
        executed_on: date | None = None,
        before_write: BeforeWrite | None = None,
    ) -> WriteReceipt:
        if amount.cents <= 0:
            raise CoreBankError("转账金额必须为正")
        if schedule_id is not None:
            idempotency_key = due_execution_key("transfer", user_id, schedule_id)
        settlement_on = (executed_on or DEFAULT_BOOKING_DATE) if schedule_id else booked_on

        def validate_schedule(conn: sqlite3.Connection) -> None:
            conn.execute(SCHEDULED_TRANSFERS_SCHEMA_SQL)
            row = conn.execute(
                "SELECT * FROM scheduled_transfers WHERE schedule_id = ?", (schedule_id,)
            ).fetchone()
            if row is None:
                raise CoreBankError(f"预约转账不存在：{schedule_id}")
            original = (
                str(row["user_id"]), str(row["account_id"]), str(row["payee_id"]),
                int(row["amount_cents"]), str(row["booked_on"]),
            )
            if original != (user_id, account_id, payee_id, amount.cents, booked_on.isoformat()):
                raise CoreBankError("预约转账参数与原登记不一致")
            if booked_on > (executed_on or DEFAULT_BOOKING_DATE):
                raise CoreBankError("预约转账尚未到期")
            if str(row["status"]) not in ("scheduled", "completed"):
                raise CoreBankError("预约转账当前状态不可执行")

        def validate_transfer(conn: sqlite3.Connection) -> None:
            payee = conn.execute(
                "SELECT blocked FROM payees WHERE payee_id = ? AND user_id = ?",
                (payee_id, user_id),
            ).fetchone()
            if payee is None:
                raise UnknownPayeeError(payee_id)
            if int(payee["blocked"]) == 1:
                raise BlockedPayeeError(f"收款对象 {payee_id} 处于 blocked 状态")

        def body(conn: sqlite3.Connection) -> WriteReceipt:
            account = conn.execute(
                "SELECT balance_cents FROM accounts WHERE account_id = ? AND user_id = ?",
                (account_id, user_id),
            ).fetchone()
            if account is None:
                raise UnknownAccountError(account_id)
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
                    settlement_on.isoformat(),
                    -amount.cents,
                    f"转给{payee_id}",
                    "转账",
                    "agent",
                    "out",
                ),
            )
            record_ids: tuple[str, ...] = (f"transactions:{txn_id}",)
            if schedule_id is not None:
                changed = conn.execute(
                    "UPDATE scheduled_transfers SET status = 'completed'"
                    " WHERE schedule_id = ? AND status = 'scheduled'", (schedule_id,),
                ).rowcount
                if changed != 1:
                    raise CoreBankError("预约转账已完成，缺少可回放回执")
                record_ids += (f"scheduled_transfers:{schedule_id}",)
            return WriteReceipt(
                idempotency_key=idempotency_key,
                action="transfer",
                status="settled",
                record_ids=record_ids,
                effect_ids=(f"accounts:{account_id}", f"txn_{txn_id}"),
            )

        return self._run_once(
            "transfer", idempotency_key, body,
            validate=validate_schedule if schedule_id is not None else None,
            validate_new=validate_transfer,
            before_write=before_write,
        )

    def cancel_subscription(
        self, *, subscription_id: str, idempotency_key: str,
        before_write: BeforeWrite | None = None,
    ) -> WriteReceipt:
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

        return self._run_once("cancel_subscription", idempotency_key, body,
                              before_write=before_write)

    def freeze_funds(
        self, *, account_id: str, amount: Money, reason: str, idempotency_key: str,
        before_write: BeforeWrite | None = None,
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

        return self._run_once("freeze_funds", idempotency_key, body,
                              before_write=before_write)

    def unfreeze_funds(
        self, *, hold_id: str, idempotency_key: str,
        before_write: BeforeWrite | None = None,
    ) -> WriteReceipt:
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

        return self._run_once("unfreeze_funds", idempotency_key, body,
                              before_write=before_write)

    def reverse_transfer(
        self, *, txn_id: str, idempotency_key: str,
        before_write: BeforeWrite | None = None,
    ) -> WriteReceipt:
        """冲正一笔**流出**转账：回补余额 + 写一条反向分录（幂等）。

        判据：目标流水必须存在且 `direction = 'out'`；非流出流水抛 `NotReversibleError`
        （补偿方按「无可回退的资金影响」跳过）。冲正本身走 `_run_once` 的幂等机制。
        """

        def body(conn: sqlite3.Connection) -> WriteReceipt:
            row = conn.execute(
                "SELECT user_id, account_id, amount_cents, booked_on, direction"
                " FROM transactions WHERE txn_id = ?",
                (txn_id,),
            ).fetchone()
            if row is None:
                raise CoreBankError(f"冲正失败：流水不存在 {txn_id}")
            direction = str(row["direction"])
            amount_cents = -int(row["amount_cents"])
            if direction != "out" or amount_cents <= 0:
                raise NotReversibleError(f"{txn_id} 方向为 {direction}，无流出资金可冲正")
            account_id = str(row["account_id"])
            reversal_id = _effect_id("txn", "reverse", idempotency_key)
            conn.execute(
                "UPDATE accounts SET balance_cents = balance_cents + ? WHERE account_id = ?",
                (amount_cents, account_id),
            )
            conn.execute(
                "INSERT INTO transactions(txn_id, user_id, account_id, booked_on, amount_cents,"
                " merchant, category, channel, direction) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    reversal_id,
                    str(row["user_id"]),
                    account_id,
                    str(row["booked_on"]),
                    amount_cents,
                    f"冲正 {txn_id}",
                    "冲正",
                    "agent",
                    "in",
                ),
            )
            return WriteReceipt(
                idempotency_key=idempotency_key,
                action="reverse_transfer",
                status="reversed",
                record_ids=(f"transactions:{reversal_id}",),
                effect_ids=(f"accounts:{account_id}", f"txn_{reversal_id}"),
            )

        return self._run_once("reverse_transfer", idempotency_key, body,
                              before_write=before_write)

    def subscribe_product(
        self,
        *,
        user_id: str,
        account_id: str,
        product_id: str,
        amount: Money,
        idempotency_key: str,
        before_write: BeforeWrite | None = None,
    ) -> WriteReceipt:
        return self._wealth_flow(
            action="subscribe_product",
            user_id=user_id,
            account_id=account_id,
            product_id=product_id,
            amount=amount,
            idempotency_key=idempotency_key,
            direction="out",
            before_write=before_write,
        )

    def redeem_product(
        self,
        *,
        user_id: str,
        account_id: str,
        product_id: str,
        amount: Money,
        idempotency_key: str,
        before_write: BeforeWrite | None = None,
    ) -> WriteReceipt:
        return self._wealth_flow(
            action="redeem_product",
            user_id=user_id,
            account_id=account_id,
            product_id=product_id,
            amount=amount,
            idempotency_key=idempotency_key,
            direction="in",
            before_write=before_write,
        )

    def set_card_status(
        self, *, card_id: str, status: str, idempotency_key: str,
        before_write: BeforeWrite | None = None,
    ) -> WriteReceipt:
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

        return self._run_once("set_card_status", idempotency_key, body,
                              before_write=before_write)

    def apply_virtual_card(
        self, *, user_id: str, daily_limit: Money, idempotency_key: str,
        before_write: BeforeWrite | None = None,
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

        return self._run_once("apply_virtual_card", idempotency_key, body,
                              before_write=before_write)

    def set_card_limit(
        self, *, card_id: str, daily_limit: Money, idempotency_key: str,
        before_write: BeforeWrite | None = None,
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

        return self._run_once("set_card_limit", idempotency_key, body,
                              before_write=before_write)

    def sign_subscription(
        self,
        *,
        user_id: str,
        merchant: str,
        amount: Money,
        period: str,
        next_charge_on: date,
        idempotency_key: str,
        before_write: BeforeWrite | None = None,
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

        return self._run_once("sign_subscription", idempotency_key, body,
                              before_write=before_write)

    def request_collection(
        self,
        *,
        user_id: str,
        payer_id: str,
        amount: Money,
        note: str,
        idempotency_key: str,
        before_write: BeforeWrite | None = None,
    ) -> WriteReceipt:
        """发起一笔 **AA 收款**请求（待收款，**不产生资金流**；P2-3）。

        纪律与既有原语一致：幂等 / 单事务 / 确定性 id / 不做授权判断。付款方必须在名册内
        且非 `blocked`；本动作**不改动任何账户余额**，只登记一条待收款请求。
        """
        if amount.cents <= 0:
            raise CoreBankError("收款金额必须为正")

        def body(conn: sqlite3.Connection) -> WriteReceipt:
            conn.execute(COLLECTION_REQUESTS_SCHEMA_SQL)
            payee = conn.execute(
                "SELECT blocked FROM payees WHERE payee_id = ? AND user_id = ?",
                (payer_id, user_id),
            ).fetchone()
            if payee is None:
                raise UnknownPayeeError(payer_id)
            if int(payee["blocked"]) == 1:
                raise BlockedPayeeError(f"付款方 {payer_id} 处于 blocked 状态")
            request_id = _effect_id("collect", "request_collection", idempotency_key)
            conn.execute(
                "INSERT INTO collection_requests(request_id, user_id, payer_id, amount_cents,"
                " note, status, created_on) VALUES(?, ?, ?, ?, ?, 'pending', ?)",
                (
                    request_id,
                    user_id,
                    payer_id,
                    amount.cents,
                    note,
                    DEFAULT_BOOKING_DATE.isoformat(),
                ),
            )
            return WriteReceipt(
                idempotency_key=idempotency_key,
                action="request_collection",
                status="pending",
                record_ids=(f"collection_requests:{request_id}",),
                effect_ids=(f"collection_requests:{request_id}",),
            )

        return self._run_once("request_collection", idempotency_key, body,
                              before_write=before_write)

    def book_transfer(
        self,
        *,
        user_id: str,
        account_id: str,
        payee_id: str,
        amount: Money,
        booked_on: date,
        remark: str,
        idempotency_key: str,
        before_write: BeforeWrite | None = None,
    ) -> WriteReceipt:
        """**登记**一笔未来转账（P2-6；登记时**不扣款**）。

        只写 `scheduled_transfers` 一行；真正扣款由到期派发后的 `transfer.create` 既有链路承担。
        纪律与既有原语一致：幂等 / 单事务 / 确定性 id / 不做授权判断；账户与收款人必须是既存事实。
        """
        if amount.cents <= 0:
            raise CoreBankError("登记金额必须为正")

        def body(conn: sqlite3.Connection) -> WriteReceipt:
            conn.execute(SCHEDULED_TRANSFERS_SCHEMA_SQL)
            account = conn.execute(
                "SELECT account_id FROM accounts WHERE account_id = ? AND user_id = ?",
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
            schedule_id = _effect_id("sched", "book_transfer", idempotency_key)
            conn.execute(
                "INSERT INTO scheduled_transfers(schedule_id, user_id, account_id, payee_id,"
                " amount_cents, booked_on, remark, status)"
                " VALUES(?, ?, ?, ?, ?, ?, ?, 'scheduled')",
                (
                    schedule_id,
                    user_id,
                    account_id,
                    payee_id,
                    amount.cents,
                    booked_on.isoformat(),
                    remark,
                ),
            )
            return WriteReceipt(
                idempotency_key=idempotency_key,
                action="book_transfer",
                status="scheduled",
                record_ids=(f"scheduled_transfers:{schedule_id}",),
                effect_ids=(f"scheduled_transfers:{schedule_id}",),
            )

        return self._run_once("book_transfer", idempotency_key, body,
                              before_write=before_write)

    def place_merchant_order(
        self,
        *,
        user_id: str,
        merchant: str,
        amount: Money,
        order_on: date,
        idempotency_key: str,
        occasion_id: str | None = None,
        before_write: BeforeWrite | None = None,
    ) -> WriteReceipt:
        """向商户下单（P2-5 生日联动的流出动作）：登记一条 `merchant_orders` 事实。

        纪律与其他写原语一致：**幂等**（`write_receipts` 命中即回放）、**全或无**（单事务）、
        **确定性 id**（由 `action + key` 派生）、**不做授权判断**（裁决归 `policy.pdp`）。
        本原语只**登记订单**（订单本身即持久化效果），不在模拟核心银行里做资金划转。
        """
        if amount.cents <= 0:
            raise CoreBankError("下单金额必须为正")
        if occasion_id is not None:
            idempotency_key = due_execution_key("place_merchant_order", user_id, occasion_id)

        def validate_order(conn: sqlite3.Connection) -> None:
            order_id = _effect_id("order", "place_merchant_order", idempotency_key)
            row = conn.execute(
                "SELECT user_id, merchant, amount_cents, order_on FROM merchant_orders"
                " WHERE order_id = ?", (order_id,),
            ).fetchone()
            if row is not None and (
                str(row["user_id"]), str(row["merchant"]),
                int(row["amount_cents"]), str(row["order_on"]),
            ) != (user_id, merchant, amount.cents, order_on.isoformat()):
                raise CoreBankError("到期订单参数与原订单不一致")

        def body(conn: sqlite3.Connection) -> WriteReceipt:
            user = conn.execute(
                "SELECT user_id FROM users WHERE user_id = ?", (user_id,)
            ).fetchone()
            if user is None:
                raise CoreBankError(f"用户不存在：{user_id}")
            order_id = _effect_id("order", "place_merchant_order", idempotency_key)
            conn.execute(
                "INSERT INTO merchant_orders(order_id, user_id, merchant, amount_cents,"
                " order_on, status) VALUES(?, ?, ?, ?, ?, 'placed')",
                (order_id, user_id, merchant, amount.cents, order_on.isoformat()),
            )
            return WriteReceipt(
                idempotency_key=idempotency_key,
                action="place_merchant_order",
                status="placed",
                record_ids=(f"merchant_orders:{order_id}",),
                effect_ids=(f"merchant_orders:{order_id}",),
            )

        return self._run_once(
            "place_merchant_order", idempotency_key, body,
            validate=validate_order if occasion_id is not None else None,
            before_write=before_write,
        )

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
        before_write: BeforeWrite | None = None,
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

        return self._run_once(action, idempotency_key, body,
                              before_write=before_write)

    def _run_once(
        self,
        action: str,
        idempotency_key: str,
        body: Callable[[sqlite3.Connection], WriteReceipt],
        *,
        validate: Callable[[sqlite3.Connection], None] | None = None,
        validate_new: Callable[[sqlite3.Connection], None] | None = None,
        before_write: BeforeWrite | None = None,
    ) -> WriteReceipt:
        with self._write_lock:
            with self._store.transaction() as conn:
                conn.execute("BEGIN IMMEDIATE")
                if validate is not None:
                    validate(conn)
                existing = self._existing_receipt(action, idempotency_key)
                if existing is not None:
                    return existing
                # 当前对象状态只约束新写入，不能阻止已提交结果的幂等回放。
                if validate_new is not None:
                    validate_new(conn)
                if before_write is not None:
                    result = before_write(conn)
                    if result.status is not PreconditionStatus.ALLOW:
                        raise WritePreconditionError(result)
                receipt = body(conn)
                self._store_receipt(conn, receipt)
                return receipt

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

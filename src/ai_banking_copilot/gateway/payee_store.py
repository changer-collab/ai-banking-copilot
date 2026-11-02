"""收款人名册写入：只登记演示用的三要素，不保存完整卡号。

设计边界：
- 这是**名册维护**，不是第 26 个业务动作；不产生资金流，也不绕过 `CoreBank.transfer`
  的收款人存在性 / blocked 校验。
- 只保存姓名、账号后四位、开户行、手机号后四位；完整卡号不落库。
- 幂等：同一用户 + 姓名 + 卡号后四位 + 开户行已存在时直接复用，不重复插入。
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any

from ai_banking_copilot.gateway.hashing import canonical_json, sha256_hex
from ai_banking_copilot.gateway.store import Store

_TAIL = re.compile(r"^\d{4}$")
_PHONE_TAIL = re.compile(r"^\d{4}$")


class PayeeValidationError(ValueError):
    """名册登记参数不合法（由路由层转成 400）。"""


class PayeeWriter:
    def __init__(self, store: Store) -> None:
        self._store = store

    def add(
        self,
        *,
        user_id: str,
        full_name: str,
        account_tail: str,
        bank_name: str,
        phone_tail: str = "",
    ) -> dict[str, Any]:
        name = full_name.strip()
        tail = account_tail.strip()
        bank = bank_name.strip()
        phone = phone_tail.strip()
        if not 1 <= len(name) <= 20:
            raise PayeeValidationError("收款人姓名需为 1-20 个字符")
        if _TAIL.fullmatch(tail) is None:
            raise PayeeValidationError("收款账号需填写后四位数字")
        if not 2 <= len(bank) <= 20:
            raise PayeeValidationError("开户行需为 2-20 个字符")
        if phone and _PHONE_TAIL.fullmatch(phone) is None:
            raise PayeeValidationError("手机号后四位需为 4 位数字")

        with self._store.transaction() as conn:
            existing = conn.execute(
                "SELECT * FROM payees WHERE user_id = ? AND full_name = ?"
                " AND account_tail = ? AND COALESCE(bank_name, '') = ?",
                (user_id, name, tail, bank),
            ).fetchone()
            if existing is not None:
                return _row_to_payload(existing)

            digest = sha256_hex(canonical_json((user_id, name, tail, bank)))[:12]
            payee_id = f"p_new_{digest}"
            conn.execute(
                "INSERT OR IGNORE INTO payees(payee_id, user_id, full_name, account_tail,"
                " phone_tail, identity_confirmed, blocked, phone, bank_name)"
                " VALUES(?, ?, ?, ?, ?, 0, 0, NULL, ?)",
                (payee_id, user_id, name, tail, phone, bank),
            )
            row = conn.execute(
                "SELECT * FROM payees WHERE payee_id = ?", (payee_id,)
            ).fetchone()
            if row is None:
                raise PayeeValidationError("收款人登记失败，请稍后重试")
            return _row_to_payload(row)


def _row_to_payload(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "payee_id": str(row["payee_id"]),
        "name": str(row["full_name"]),
        "account_tail": str(row["account_tail"]),
        "phone_tail4": str(row["phone_tail"] or "")[-4:],
        "bank_name": str(row["bank_name"] or ""),
        "identity_confirmed": bool(row["identity_confirmed"]),
        "blocked": bool(row["blocked"]),
    }

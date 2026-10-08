"""补偿（冲正）通道：把**已执行**的写动作回退到执行前状态。

分权（H2）：`web/` 层不得直接触碰核心银行写原语，故本能力由**组合根**装配、以可调用对象
注入会话层；实现落在 `policy/`（与 `Pep` 同层，是该层唯一被允许调用写原语的位置）。

纪律（与 `policy/saga.py` 一致）：
- 只对**本次执行产生的回执记录**冲正（`record_ids` 来自 `ExecResult`，不是用户输入）；
- 无对应冲正原语的记录（纯查询等）**跳过**：它们没有可回退的资金影响；
- 真正失败（流水不存在 / 核心银行错误）**如实抛出** `CompensationError`，不静默吞掉。
"""

from __future__ import annotations

from collections.abc import Sequence

from ai_banking_copilot.gateway.core_bank import CoreBank
from ai_banking_copilot.gateway.errors import CoreBankError, NotReversibleError
from ai_banking_copilot.policy.saga import CompensationError

_REVERSIBLE_KIND = "transactions"
"""目前支持冲正的回执记录类型前缀（流水）。"""


class ReversalService:
    """把回执记录冲正回执行前状态（当前支持：流出转账）。"""

    def __init__(self, core_bank: CoreBank) -> None:
        self._core_bank = core_bank

    def compensate(self, records: Sequence[str]) -> tuple[str, ...]:
        """逐条冲正；返回**已完成**冲正的记录标识（无资金影响的记录不计入、也不算失败）。"""
        reversed_records: list[str] = []
        for record in records:
            kind, _, identifier = record.partition(":")
            if kind != _REVERSIBLE_KIND or not identifier:
                continue
            try:
                receipt = self._core_bank.reverse_transfer(
                    txn_id=identifier, idempotency_key=f"reversal:{identifier}"
                )
            except NotReversibleError:
                continue
            except CoreBankError as error:
                raise CompensationError(f"冲正失败：{record}（{error}）") from error
            reversed_records.extend(receipt.record_ids)
        return tuple(reversed_records)

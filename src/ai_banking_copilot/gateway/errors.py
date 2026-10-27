"""网关层共享异常。

集中定义（而不是各自散落）的原因：沙箱子进程需要 `SandboxViolation`，
但它**不能**反向 import `store` / `core_bank`——否则会把 `sqlite3`
带进受限环境，破坏"禁库"防护。
"""

from __future__ import annotations


class GatewayError(RuntimeError):
    """网关层错误基类。"""


class ReadOnlyViolation(GatewayError):
    """在只读上下文里尝试写，或用 `query()` 传入了非只读语句。"""


class StoreError(GatewayError):
    """存储层错误（如只读打开不存在的库）。"""


class GatewayNotFoundError(GatewayError):
    """事实记录不存在。"""


class CoreBankError(GatewayError):
    """模拟核心银行错误基类。"""


class InsufficientFundsError(CoreBankError):
    """余额不足。"""


class InsufficientHoldingError(CoreBankError):
    """理财产品持仓不足（赎回不得超过已持有份额：事实零生成）。"""


class BlockedPayeeError(CoreBankError):
    """收款对象处于 blocked 吸收态（PRD §5.2）。"""


class CurrencyMismatchError(CoreBankError):
    """付款账户缺少币种，或请求币种与付款账户事实不一致：拒绝写前复核。"""


class IdempotencyConflictError(CoreBankError):
    """同一幂等键被用于不同动作。"""


class NotReversibleError(CoreBankError):
    """该流水没有可冲正的资金影响（非流出）——补偿时**跳过**，不算失败。"""


class UnknownAccountError(GatewayError):
    """账户不存在。"""


class UnknownPayeeError(GatewayError):
    """收款对象不存在。"""


class UnknownSubscriptionError(GatewayError):
    """订阅不存在。"""


class UnknownProductError(GatewayError):
    """理财产品不存在。"""


class UnknownCardError(GatewayError):
    """卡片不存在。"""


class OperatorError(ValueError):
    """算子白名单错误基类。"""


class UnknownOperatorError(OperatorError):
    """算子不在白名单内。"""


class SandboxError(RuntimeError):
    """沙箱错误基类。"""


class SandboxViolation(SandboxError):
    """沙箱内发生越权（网络 / 禁库 / 写文件 / 非白名单算子）。"""


class SandboxTimeout(SandboxError):
    """沙箱执行超时。"""

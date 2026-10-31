"""未识别输入的**三分类**回应：闲聊 / 超范围 / 模糊。

口径见 `docs/superpowers/specs/2026-10-31-manual-test-fixes-design.md` §4：

1. 只产出**文案与候选句**，不查网关、不产出事实、不改变会话状态机；
2. 判定顺序 超范围 → 模糊 → 闲聊 → 兜底模糊（宁可澄清，也不越界回答）；
3. 三类都**不是安全拒绝**：不计入熔断，界面不用红色「已拦截」；
4. 话术里**只提本仓库真实存在的动作**（转账 / 账单 / 卡片 / 理财 / 订阅 / 生活事件），
   不得出现「银证转账」「生日礼遇」这类没有实现的能力。
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class TriageSuggestion:
    """一条可点示例：`label` 给人看，`text` 点一下填进输入框并提交。"""

    label: str
    text: str


@dataclass(frozen=True)
class TriageVerdict:
    kind: str
    message: str
    suggestions: tuple[TriageSuggestion, ...] = ()
    disclaimer: str = ""


_VIOLATION = re.compile(
    r"征信修复|代开卡|洗钱|套现|查别人|别人的账户|家人的账户|贷款提额|刷流水"
)
_SECURITIES = re.compile(r"炒股|股票|证券账户|打新|期货|外汇|彩票|赌博|基金代码")
_NON_BANK = re.compile(
    r"订(机票|酒店|外卖)|快递|话费|充值|水电|翻译|写代码|写作文|查天气|导航|点外卖|整理房间"
)
_LIFE_SIGNAL = re.compile(r"生日|纪念日|结婚|过寿|满月|周岁|房租|学费|保费|送礼")
_BUSINESS_VERB = re.compile(r"转|汇|查|看|买|卖|赎回|取消|挂失|锁卡|解锁|额度|账单|余额")
_SMALLTALK = re.compile(
    r"天气|心情|开心|难过|无聊|你好|您好|在吗|谢谢|谢了|再见|拜拜|早上好|晚上好"
    r"|吃了吗|你是谁|你叫什么|讲个笑话|好累|好困"
)

_SMALLTALK_MAX_LENGTH = 20

_SMALLTALK_MESSAGE = (
    "今天天气是挺好的，适合出去走走。我这边是银行的办理助手，"
    "能帮您查余额、看账单、转账，也能管卡片和订阅。想先看哪一项？"
)

_SECURITIES_MESSAGE = (
    "股票交易要在证券公司开立证券账户，银行不能代您下单，我这边也没有接证券通道。"
    "我能做的是行内理财：先做风险测评，再看适合您风险等级的产品，或查现有持仓。"
)
_SECURITIES_DISCLAIMER = "理财非存款，产品有风险，投资须谨慎。"

_VIOLATION_MESSAGE = (
    "这件事银行办不了，也帮不上忙：涉及他人账户、征信或账户资质的操作必须本人到柜面办理。"
    "我在演示环境里能做的是您本人名下的转账、账单、卡片、理财、订阅和生活事件。"
)

_NON_BANK_MESSAGE = (
    "这个不在银行能办的范围内，我接不到那类系统。"
    "银行这边我能做六类：转账、账单分析、卡片管理、理财、订阅管理、生活事件（生日、纪念日、房租这类）。"
)

_AMBIGUOUS_MESSAGE = (
    "没太确定您的意思。如果是想给家人过生日、提前把钱留出来，"
    "我可以用「冻结预算 → 下单 → 释放余额」帮您办；也可以直接告诉我您要办的业务。"
)

_FALLBACK_MESSAGE = (
    "这句话我没太理解，也没敢猜着执行。您可以换个说法，或者点下面的例子试试。"
)

_DEFAULT_SUGGESTIONS: tuple[TriageSuggestion, ...] = (
    TriageSuggestion(label="查一下余额", text="查一下余额"),
    TriageSuggestion(label="查上月账单", text="查一下上月账单"),
    TriageSuggestion(label="卡片挂失", text="把我的卡挂失"),
)

_BIRTHDAY_SUGGESTIONS: tuple[TriageSuggestion, ...] = (
    TriageSuggestion(label="我下周生日", text="我下周生日"),
    TriageSuggestion(label="给妈妈过生日", text="给妈妈过生日，预算 800 元"),
    TriageSuggestion(label="随便问问", text="我只是随便问问"),
)

_SECURITIES_SUGGESTIONS: tuple[TriageSuggestion, ...] = (
    TriageSuggestion(label="我要做风险测评", text="我要做风险测评"),
    TriageSuggestion(label="看理财持仓", text="看看我的理财持仓"),
    TriageSuggestion(label="查一下余额", text="查一下余额"),
)


def triage(utterance: str, *, low_confidence: bool = False) -> TriageVerdict | None:
    """把「没匹配到任何意图」的一句话归到三类之一；返回 `None` 表示规则未覆盖。"""
    text = (utterance or "").strip()
    if not text:
        return None
    if _VIOLATION.search(text):
        return TriageVerdict("out_of_scope", _VIOLATION_MESSAGE, _DEFAULT_SUGGESTIONS)
    if _SECURITIES.search(text):
        return TriageVerdict(
            "out_of_scope", _SECURITIES_MESSAGE, _SECURITIES_SUGGESTIONS,
            disclaimer=_SECURITIES_DISCLAIMER,
        )
    if _NON_BANK.search(text):
        return TriageVerdict("out_of_scope", _NON_BANK_MESSAGE, _DEFAULT_SUGGESTIONS)
    if low_confidence or _LIFE_SIGNAL.search(text) or _BUSINESS_VERB.search(text):
        # 有业务信号却没装配出意图 ⇒ 不猜，给候选句。
        suggestions = _BIRTHDAY_SUGGESTIONS if _LIFE_SIGNAL.search(text) else _DEFAULT_SUGGESTIONS
        return TriageVerdict("ambiguous", _AMBIGUOUS_MESSAGE, suggestions)
    if len(text) <= _SMALLTALK_MAX_LENGTH and _SMALLTALK.search(text):
        return TriageVerdict("smalltalk", _SMALLTALK_MESSAGE, _DEFAULT_SUGGESTIONS)
    return TriageVerdict("ambiguous", _FALLBACK_MESSAGE, _DEFAULT_SUGGESTIONS)

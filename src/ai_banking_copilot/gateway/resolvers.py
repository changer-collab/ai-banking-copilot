"""确定性解析器：实体 / 数值 / 时间语义的**唯一**抽取通道（H1 事实零生成）。

三条纪律：
1. **零模型参与**：只用正则与词典；不调用任何 LLM。
2. **不产出级别**：多候选时只标记 `needs_clarification`，**不猜**、不定级（PRD §5.1）。
3. **不进控制槽**：本模块的输出是"候选与证据"，写入控制槽由上层经 schema 把关。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from ai_banking_copilot.gateway.models import Money, Payee

_NUMBER_RUN = re.compile(r"\d{4,}")
_AMOUNT = re.compile(r"(?P<num>\d+(?:\.\d{1,2})?)\s*(?P<unit>元|块|块钱|人民币|万|k|K)?")
# 中文数字（口语）：五百 / 一千二 / 两千五 / 一百零八 / 十五块。
# 后面紧跟 月/号/日/年… 的是日期（「十月一号」），不算金额，用负向断言排除。
_CN_NUMERAL = re.compile(
    r"(?P<cn>[零〇一壹二两贰三叁四肆五伍六陆七柒八捌九玖十拾百佰千仟万萬]+)"
    r"\s*(?P<unit>元|块钱|块|人民币)?"
    r"(?!\s*[月号日年点个位张次天周])"
)
_CN_DIGITS = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "壹": 1,
    "二": 2,
    "两": 2,
    "贰": 2,
    "三": 3,
    "叁": 3,
    "四": 4,
    "肆": 4,
    "五": 5,
    "伍": 5,
    "六": 6,
    "陆": 6,
    "七": 7,
    "柒": 7,
    "八": 8,
    "捌": 8,
    "九": 9,
    "玖": 9,
}
_CN_UNITS = {"十": 10, "拾": 10, "百": 100, "佰": 100, "千": 1000, "仟": 1000}
_CN_TEN_THOUSAND = ("万", "萬")
_CN_SCALE_HINT = frozenset("十拾百佰千仟万萬")
_ISO_DATE = re.compile(r"(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})")
_MONTHLY_DAY = re.compile(r"每月\s*(?P<day>\d{1,2})\s*号")
_NEGATION_WORDS = ("不要", "别", "不用", "不转", "不办", "暂不", "拒绝")
_RELATIVE_DAYS = {"今天": 0, "明天": 1, "后天": 2, "大后天": 3}
_UNIT_MULTIPLIER = {"元": 1, "块": 1, "块钱": 1, "人民币": 1, "万": 10000, "k": 1000, "K": 1000}


@dataclass(frozen=True)
class Candidate:
    payee_id: str
    full_name: str
    account_tail: str
    phone_tail: str
    identity_confirmed: bool


@dataclass(frozen=True)
class PayeeResolution:
    """收款对象候选集。`needs_clarification` = 候选 ≥ 2（禁止自动择一）。"""

    candidates: tuple[Candidate, ...]
    needs_clarification: bool
    parse_trace: tuple[str, ...]


@dataclass(frozen=True)
class AmountParse:
    matched: bool
    value: Money | None
    evidence: str


@dataclass(frozen=True)
class TemporalParse:
    matched: bool
    when: date | None
    negation: bool
    evidence: str


def resolve_payees(payees: Sequence[Payee], text: str) -> PayeeResolution:
    """按手机尾号 / 卡号尾号 / 姓名匹配名册。**只认名册，不产出新实体。**"""
    hits: dict[str, Candidate] = {}
    trace: list[str] = []

    for run in dict.fromkeys(_NUMBER_RUN.findall(text)):
        matched_payees = [payee for payee in payees if payee.phone_tail == run]
        if matched_payees:
            trace.append(f"phone_tail:{run} -> {','.join(p.payee_id for p in matched_payees)}")
            for payee in matched_payees:
                hits[payee.payee_id] = _candidate(payee)
        matched_accounts = [payee for payee in payees if payee.account_tail == run]
        if matched_accounts:
            trace.append(
                f"account_tail:{run} -> {','.join(p.payee_id for p in matched_accounts)}"
            )
            for payee in matched_accounts:
                hits[payee.payee_id] = _candidate(payee)

    for payee in payees:
        if payee.full_name and payee.full_name in text:
            hits.setdefault(payee.payee_id, _candidate(payee))
            sentinel = f"name:{payee.full_name} -> "
            if not any(entry.startswith(sentinel) for entry in trace):
                same_name = ",".join(p.payee_id for p in payees if p.full_name == payee.full_name)
                trace.append(f"{sentinel}{same_name}")

    candidates = tuple(hits[key] for key in sorted(hits))
    return PayeeResolution(
        candidates=candidates,
        needs_clarification=len(candidates) >= 2,
        parse_trace=tuple(trace),
    )


def parse_amount(text: str) -> AmountParse:
    """抽取金额。

    - 阿拉伯数字**必须带单位**才算命中——无单位一律视为未命中（避免误读数字）；
    - 中文数字自带货币语义（「打五百」「十五块」），不强制带「元 / 块」；
    - **万级中文数字必须带货币单位**（「一万块」可解析，「一万」「一万五」视为未命中，
      先追问确认再执行）——大额不替用户推断单位，方向只加严；
    - 命中多个候选（如「300 元和五百块」）一律视为未命中，交由上层追问，**不猜**。
    """
    candidates: list[AmountParse] = []
    for match in _AMOUNT.finditer(text):
        unit = match.group("unit")
        if unit is None:
            continue
        candidates.append(_amount_from_arabic(match.group("num"), unit, match.group(0)))
    for cn_match in _CN_NUMERAL.finditer(text):
        parsed = _amount_from_chinese(cn_match)
        if parsed is not None:
            candidates.append(parsed)
    if len(candidates) != 1:
        return AmountParse(matched=False, value=None, evidence="")
    return candidates[0]


def _amount_from_arabic(num: str, unit: str, evidence: str) -> AmountParse:
    multiplier = _UNIT_MULTIPLIER[unit]
    raw = Decimal(num)
    cents = int((raw * multiplier * 100).to_integral_value())
    return AmountParse(matched=True, value=Money(cents), evidence=evidence)


def _amount_from_chinese(match: re.Match[str]) -> AmountParse | None:
    """中文数字 → 金额。

    单个汉字（「五」）不足以判定；万级不带货币单位（「一万」「一万五」）也不判定，
    一律交给上层追问——大额金额不替用户推断单位。
    """
    token = match.group("cn")
    unit = match.group("unit")
    if len(token) == 1 and unit is None and token not in _CN_SCALE_HINT:
        return None
    yuan = _chinese_to_yuan(token)
    if yuan <= 0:
        return None
    if yuan >= 10000 and unit is None:
        return None
    return AmountParse(matched=True, value=Money(yuan * 100), evidence=match.group(0).strip())


def _chinese_to_yuan(token: str) -> int:
    """中文数字转元。支持「一百零八」与口语简写「一千二 / 两千五 / 一万五」。"""
    for ten_thousand in _CN_TEN_THOUSAND:
        if ten_thousand not in token:
            continue
        head, _, tail = token.partition(ten_thousand)
        if not head:
            return 0
        high = _chinese_section_to_int(head)
        if high == 0:
            return 0
        return high * 10000 + _chinese_tail_to_int(tail)
    return _chinese_section_to_int(token)


def _chinese_tail_to_int(tail: str) -> int:
    """「万」后面那一段：单个数字按千进位（一万五 = 15000），否则按常规读法。"""
    if not tail:
        return 0
    if len(tail) == 1 and tail in _CN_DIGITS:
        return _CN_DIGITS[tail] * 1000
    return _chinese_section_to_int(tail)


def _chinese_section_to_int(section: str) -> int:
    """把不含「万」的中文数字转成整数；「零」打断口语简写的进位规则。"""
    total = 0
    pending: int | None = None
    last_unit = 0
    seen_zero = False
    for char in section:
        if char in _CN_DIGITS:
            value = _CN_DIGITS[char]
            if value == 0:
                seen_zero = True
                pending = None
                continue
            pending = value
            continue
        unit = _CN_UNITS.get(char)
        if unit is None:
            return 0
        total += (1 if pending is None else pending) * unit
        pending = None
        last_unit = unit
    if pending is None:
        return total
    if last_unit and not seen_zero:
        return total + pending * (last_unit // 10)
    return total + pending


def parse_temporal(text: str, today: date) -> TemporalParse:
    """抽取时间语义与否定词。否定词优先——"不要转"不得被解析成待执行意图。"""
    for word in _NEGATION_WORDS:
        if word in text:
            return TemporalParse(matched=False, when=None, negation=True, evidence=word)

    iso = _ISO_DATE.search(text)
    if iso is not None:
        try:
            when_iso = date(
                int(iso.group("year")), int(iso.group("month")), int(iso.group("day"))
            )
        except ValueError:
            return TemporalParse(matched=False, when=None, negation=False, evidence=iso.group(0))
        return TemporalParse(matched=True, when=when_iso, negation=False, evidence=iso.group(0))

    monthly = _MONTHLY_DAY.search(text)
    if monthly is not None:
        day = int(monthly.group("day"))
        year = today.year + (1 if today.month == 12 else 0)
        month = 1 if today.month == 12 else today.month + 1
        try:
            when_monthly = date(year, month, day)
        except ValueError:
            return TemporalParse(
                matched=False, when=None, negation=False, evidence=monthly.group(0)
            )
        return TemporalParse(
            matched=True, when=when_monthly, negation=False, evidence=monthly.group(0)
        )

    for word, offset in _RELATIVE_DAYS.items():
        if word in text:
            return TemporalParse(
                matched=True, when=today + timedelta(days=offset), negation=False, evidence=word
            )

    if "下周" in text:
        days_ahead = (7 - today.weekday()) % 7
        return TemporalParse(
            matched=True,
            when=today + timedelta(days=days_ahead or 7),
            negation=False,
            evidence="下周",
        )

    return TemporalParse(matched=False, when=None, negation=False, evidence="")


def _candidate(payee: Payee) -> Candidate:
    return Candidate(
        payee_id=payee.payee_id,
        full_name=payee.full_name,
        account_tail=payee.account_tail,
        phone_tail=payee.phone_tail,
        identity_confirmed=payee.identity_confirmed,
    )

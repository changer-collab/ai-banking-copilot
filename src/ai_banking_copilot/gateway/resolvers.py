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
    """抽取金额。**必须带单位**才算命中——无单位一律视为未命中（避免误读数字）。"""
    found = [
        match
        for match in _AMOUNT.finditer(text)
        if match.group("unit") is not None
    ]
    if len(found) != 1:
        return AmountParse(matched=False, value=None, evidence="")
    match = found[0]
    unit = match.group("unit")
    multiplier = _UNIT_MULTIPLIER[unit]
    raw = Decimal(match.group("num"))
    cents = int((raw * multiplier * 100).to_integral_value())
    return AmountParse(matched=True, value=Money(cents), evidence=match.group(0))


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

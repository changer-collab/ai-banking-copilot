"""转账自然语言的五类话术（完整 / 省略 / 模糊 / 错别字·口语 / 反问）各 3 条。

**测的是哪一层**：`planning.slots.fill_control_slots`——即「文本 → 控制槽」的补槽层，
这是场景①（转账）负责的语义面。模板命中（`planning.llm`）与多意图拆分属内核，
不在本文件的断言范围内，只在末尾用三条反问句守住「反问不得变成执行指令」。

**判定原则**（与 `docs/2026-10-26/三色基准表` 一致）：

- 缺失的控制槽**必须追问**，不得替用户猜（尤其收款人、金额）；
- 阿拉伯数字**必须带货币单位**才算金额；中文数字自带货币语义，口语「打五百」可解析；
- 完整 11 位手机号按**后四位**匹配名册（库内只存尾号，能力子集）。

已知边界（本文件**如实断言现状**，不倒逼实现放宽）：

1. 错别字「转帐」「李那」不会被纠错：不猜收款人，转成追问——安全方向；
2. 「我昨天转给李娜那 500 到了吗」这类**反问句**会被模板判成 `transfer.create`（追问金额），
   已作为缺陷上报组长，修的是 `planning/llm.py`（内核），不在本文件范围。
"""

from __future__ import annotations

from datetime import date

import pytest

from ai_banking_copilot.gateway.facts import FactGateway
from ai_banking_copilot.gateway.models import Payee
from ai_banking_copilot.gateway.seed import DEMO_USER_ID
from ai_banking_copilot.planning.llm import TemplateIntentModel
from ai_banking_copilot.planning.slots import SlotFill, SlotInputs, fill_control_slots

_TODAY = date(2026, 10, 1)
"""固定「今天」，让 `when` 默认可断言（周一 ~ 周五之外的日期无关紧要）。"""


def _payees(gateway: FactGateway) -> list[Payee]:
    return [record.payload for record in gateway.payees_of(DEMO_USER_ID)]


def _fill(gateway: FactGateway, text: str) -> SlotFill:
    return fill_control_slots(
        "transfer.create",
        SlotInputs(text=text, today=_TODAY, payees=_payees(gateway)),
    )


def _asked(fill: SlotFill) -> set[str]:
    """本次补槽真正要追问的槽位（`user_id` / `account` 由受信任上下文与选择提供，不计）。"""
    return {question.slot for question in fill.questions} - {"user_id", "account"}


# ---------------------------------------------------------------- 完整句（3 条）


def test_full_sentence_name_amount_and_unit(gateway: FactGateway) -> None:
    fill = _fill(gateway, "转给李娜 500 元")
    assert fill.slots["payee"] == "p_lina"
    assert fill.slots["amount"] == 50000
    assert fill.slots["when"] == _TODAY.isoformat()
    assert _asked(fill) == set()


def test_full_sentence_with_remark_keeps_amount_intact(gateway: FactGateway) -> None:
    """备注里的数字不得被当成金额（备注是数据，不是指令）。"""
    fill = _fill(gateway, "给李娜转 200 元，备注房租")
    assert fill.slots["payee"] == "p_lina"
    assert fill.slots["amount"] == 20000
    assert _asked(fill) == set()


def test_full_mobile_number_matches_payee_by_last_four(gateway: FactGateway) -> None:
    """完整 11 位手机号 + 中文数字金额：李娜尾号 2210。"""
    fill = _fill(gateway, "给 13812342210 转五百")
    assert fill.slots["payee"] == "p_lina"
    assert fill.slots["amount"] == 50000
    assert _asked(fill) == set()


# ---------------------------------------------------------------- 省略（3 条）


def test_ellipsis_pronoun_payee_asks_who_and_how_much(gateway: FactGateway) -> None:
    """「转 200 给他」：代词不可解析 ⇒ 收款人追问；200 无歧义，直接当 200 元（10-27 放宽）。"""
    fill = _fill(gateway, "转 200 给他")
    assert "payee" not in fill.slots
    assert fill.slots["amount"] == 20000
    assert _asked(fill) == {"payee"}


def test_ellipsis_incremental_transfer_never_guesses(gateway: FactGateway) -> None:
    """「再转 500」：上一笔上下文不在本层，必须重新问收款人（金额按无歧义裸数字取）。"""
    fill = _fill(gateway, "再转 500")
    assert "payee" not in fill.slots
    assert fill.slots["amount"] == 50000
    assert _asked(fill) == {"payee"}


def test_ellipsis_known_payee_fills_a_bare_amount(gateway: FactGateway) -> None:
    """「给李娜转 500」：收款人可定，且 500 唯一、不是尾号 ⇒ 直接当 500 元（2026-10-27 放宽）。"""
    fill = _fill(gateway, "给李娜转 500")
    assert fill.slots["payee"] == "p_lina"
    assert fill.slots["amount"] == 50000
    assert _asked(fill) == set()


# ---------------------------------------------------------------- 模糊（3 条）


def test_vague_unknown_nickname_is_not_guessed(gateway: FactGateway) -> None:
    """「给老王转点钱」：名册无「老王」，不得猜成王伟。"""
    fill = _fill(gateway, "给老王转点钱")
    assert "payee" not in fill.slots
    assert _asked(fill) == {"payee", "amount"}


def test_vague_reference_without_amount_asks_both(gateway: FactGateway) -> None:
    fill = _fill(gateway, "把欠他的钱还了")
    assert _asked(fill) == {"payee", "amount"}


def test_bare_verb_asks_everything(gateway: FactGateway) -> None:
    fill = _fill(gateway, "转账")
    assert _asked(fill) == {"payee", "amount"}


# ---------------------------------------------------------------- 错别字 / 口语（3 条）


def test_typo_does_not_fuzzy_match_payee(gateway: FactGateway) -> None:
    """「转帐给李那 500」：错别字不纠错、不猜收款人 ⇒ 只追问收款人（金额 500 无歧义）。"""
    fill = _fill(gateway, "转帐给李那 500")
    assert "payee" not in fill.slots
    assert _asked(fill) == {"payee"}


def test_colloquial_verb_parses_chinese_amount(gateway: FactGateway) -> None:
    """「给李娜打五百」：口语动词不影响槽位，中文数字直接成金额。"""
    fill = _fill(gateway, "给李娜打五百")
    assert fill.slots["payee"] == "p_lina"
    assert fill.slots["amount"] == 50000
    assert _asked(fill) == set()


def test_colloquial_unit_kuai_is_accepted(gateway: FactGateway) -> None:
    fill = _fill(gateway, "给李娜转 500 块")
    assert fill.slots["amount"] == 50000
    assert _asked(fill) == set()


# ---------------------------------------------------------------- 反问（3 条）


@pytest.mark.parametrize(
    "utterance",
    [
        "这 500 到底转没转出去？",
        "我不是说了转 500 吗，怎么扣我 800？",
    ],
)
def test_rhetorical_question_is_not_an_executable_transfer(utterance: str) -> None:
    """反问句**不得**产出转账意图——宁可识别不出，也不能把疑问当成付款指令。"""
    actions = [intent.action for intent in TemplateIntentModel().propose(utterance, {}).intents]
    assert "transfer.create" not in actions


def test_rhetorical_question_routes_to_the_read_only_ledger() -> None:
    """「查一下交易明细」必须走只读流水查询，而不是转账。"""
    actions = [
        intent.action for intent in TemplateIntentModel().propose("查一下交易明细", {}).intents
    ]
    assert actions == ["account.transactions"]

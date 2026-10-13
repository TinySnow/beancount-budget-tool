"""Explicit, source-bound same-day bridge for a credit-card repayment.

This does not turn a negative asset into cash.  It only permits a reviewed
USD debit followed by its exact FX funding before the date's close.  Reports
remain date-end only; an unfinished bridge cannot be carried to another day.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from fractions import Fraction
import json

from beancount.core import data

from ..errors import require
from ..exact import exact, exact_text
from ..financial import FinancialInput
from .facts import FactIndex


def _key(ref: dict) -> str:
    return json.dumps(ref, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class SameDaySettlement:
    asset_account: str
    credit_card_account: str
    commodity: str
    day: date
    units: Fraction
    debit_posting_id: int
    funding_posting_id: int
    debit_ref: dict
    liability_ref: dict
    funding_ref: dict
    reason: str

    def as_dict(self) -> dict:
        return {"asset_account": self.asset_account,
                "credit_card_account": self.credit_card_account,
                "date": self.day.isoformat(), "commodity": self.commodity,
                "units": exact_text(self.units), "debit_ref": self.debit_ref,
                "liability_ref": self.liability_ref, "funding_ref": self.funding_ref,
                "reason": self.reason}


def validate_same_day_settlements(fin: FinancialInput, facts: FactIndex,
                                  whitelist: object, claims: object, cutover: date,
                                  report_currency: str) -> tuple[list[SameDaySettlement], dict[int, tuple[str, SameDaySettlement]]]:
    """Validate every exception before any H inventory replay can use it."""
    liability_root = fin.options["name_liabilities"]
    asset_root = fin.options["name_assets"]
    require(type(whitelist) is list and all(type(x) is str and x.startswith(liability_root + ":")
                                            for x in whitelist) and len(set(whitelist)) == len(whitelist),
            "SETTLEMENT_WHITELIST_INVALID", "信用卡白名单须列唯一完整负债账户")
    require(type(claims) is list, "SETTLEMENT_CLAIM_INVALID", "同日结算声明须为列表")
    entries = [entry for entry in fin.entries if isinstance(entry, data.Transaction)]
    positions = {id(entry): index for index, entry in enumerate(entries)}
    by_ref = {_key(facts.for_posting(posting).as_dict()): (entry, posting)
              for entry in entries for posting in entry.postings}
    used_refs: set[str] = set()
    result: list[SameDaySettlement] = []
    roles: dict[int, tuple[str, SameDaySettlement]] = {}
    for row in claims:
        require(type(row) is dict and set(row) == {
            "asset_account", "credit_card_account", "debit_ref", "liability_ref",
            "funding_ref", "reason"},
            "SETTLEMENT_CLAIM_INVALID", "结算声明须绑定资产扣款、信用卡还款和购汇入账三条完整来源")
        account, card = row["asset_account"], row["credit_card_account"]
        require(type(account) is str and account.startswith(asset_root + ":") and
                type(card) is str and card in whitelist and
                type(row["reason"]) is str and bool(row["reason"].strip()),
                "SETTLEMENT_CLAIM_INVALID", "结算账户、白名单信用卡或审阅理由无效")
        refs = []
        for field in ("debit_ref", "liability_ref", "funding_ref"):
            ref = row[field]
            require(type(ref) is dict and _key(ref) in by_ref,
                    "SETTLEMENT_SOURCE_INVALID", "结算来源未对应本快照的 booked posting", field)
            key = _key(ref)
            require(key not in used_refs, "SETTLEMENT_SOURCE_REUSED", "结算来源被重复认领", field)
            used_refs.add(key)
            refs.append(by_ref[key])
        (debit_tx, debit), (liability_tx, liability), (funding_tx, funding) = refs
        amount = -exact(debit.units.number)
        require(debit.account == account and debit_tx.date >= cutover and
                amount > 0 and debit.cost is None and debit.price is None and
                debit.units.currency != report_currency,
                "SETTLEMENT_SHAPE_INVALID", "只接受外币真实资产的无成本还卡扣款", account)
        require(liability_tx is debit_tx and liability.account == card and
                liability.units.currency == debit.units.currency and
                exact(liability.units.number) == amount and liability.cost is None and
                liability.price is None,
                "SETTLEMENT_SHAPE_INVALID", "同笔须有等额、同商品的白名单信用卡负债减少", card)
        require(funding_tx.date == debit_tx.date and
                positions[id(funding_tx)] > positions[id(debit_tx)] and
                funding.account == account and funding.units.currency == debit.units.currency and
                exact(funding.units.number) == amount and funding.cost is None,
                "SETTLEMENT_SHAPE_INVALID", "同日较晚须有同账户、同商品、等额的购汇入账", account)
        notation, total, price_currency = fin.raw_price(funding)
        matches = [p for p in funding_tx.postings
                   if p.account == account and p.units.currency == report_currency and
                   exact(p.units.number) == -total]
        require(notation == "@@" and price_currency == report_currency and
                total > 0 and len(matches) == 1,
                "SETTLEMENT_FX_INVALID", "购汇须有明确报告货币总价和同笔等额借记腿", account)
        debt_before = sum((exact(p.units.number) for tx in entries[:positions[id(debit_tx)]]
                           for p in tx.postings if p.account == card and
                           p.units.currency == debit.units.currency), Fraction(0))
        require(debt_before <= -amount, "SETTLEMENT_LIABILITY_INVALID",
                "还款前白名单信用卡须有足额已记负债", card)
        claim = SameDaySettlement(account, card, debit.units.currency, debit_tx.date,
                                  amount, id(debit), id(funding), row["debit_ref"],
                                  row["liability_ref"], row["funding_ref"], row["reason"])
        result.append(claim)
        roles[id(debit)] = ("debit", claim)
        roles[id(funding)] = ("funding", claim)
    return result, roles

"""Native exchange evidence for explicitly declared single-purpose H accounts."""

from dataclasses import dataclass
from fractions import Fraction

from beancount.core import data

from ..exact import exact, exact_text


@dataclass(frozen=True)
class AccountExchange:
    assets: tuple
    counterparts: tuple
    owner_id: str
    currency: str
    shape: str
    fees: Fraction
    fee_account: str | None

    def evidence(self, entry, facts) -> dict:
        return {
            "date": entry.date.isoformat(), "protocol": "account_default_internal_v1",
            "shape": self.shape, "owner_id": self.owner_id,
            "proof": {"has_cost_or_price": self.shape == "securities_cost_or_price",
                      "currencies": sorted({posting.units.currency for posting in self.assets})},
            "accounts": sorted({posting.account for posting in self.assets}),
            "asset_refs": [facts.for_posting(posting).as_dict() for posting in self.assets],
            "income_expense_refs": [facts.for_posting(posting).as_dict()
                                    for posting in self.counterparts],
            "asset_units": [{"ref": facts.for_posting(posting).as_dict(),
                             "account": posting.account, "commodity": posting.units.currency,
                             "signed_units": exact_text(exact(posting.units.number))}
                            for posting in self.assets],
            "fees_native": exact_text(self.fees), "fee_currency": self.currency,
            "fee_paying_account": self.fee_account,
            "reason": "显式单用途 H 账户的已记账资产交换；保留原用途，不计新增划拨",
        }


def native_account_exchange(fin, entry, facts, modes, account_funds, claims,
                            currency: str) -> AccountExchange | None:
    """Recognize an exchange, never a blanket exemption for owned postings.

    Booking establishes the complete value equation. Ownership still needs
    one hold owner on every asset leg, and callers must independently check
    the pre-transaction inventory and retain all existing event boundaries.
    """
    if not isinstance(entry, data.Transaction):
        return None
    assets_root = fin.options["name_assets"]
    expenses_root = fin.options["name_expenses"]
    allowed = {assets_root, expenses_root, fin.options["name_income"]}
    if any(posting.account.split(":", 1)[0] not in allowed for posting in entry.postings):
        return None
    assets = tuple(posting for posting in entry.postings
                   if posting.account.split(":", 1)[0] == assets_root)
    outside = tuple(posting for posting in entry.postings
                    if posting.account.split(":", 1)[0] != assets_root)
    if (len(assets) < 2 or len({posting.account for posting in assets}) < 2 or
            not any(exact(posting.units.number) < 0 for posting in assets) or
            not any(exact(posting.units.number) > 0 for posting in assets) or
            any(modes.get(posting.account) != "H" or posting.account not in account_funds
                for posting in assets)):
        return None
    if any(posting.units.currency != currency or posting.cost is not None or
           posting.price is not None or fin.raw_price(posting)[0] != "none"
           for posting in outside):
        return None
    fees = tuple(posting for posting in outside
                 if posting.account.split(":", 1)[0] == expenses_root)
    if any(exact(posting.units.number) < 0 for posting in fees):
        return None
    costed = any(posting.cost is not None or posting.price is not None or
                 fin.raw_price(posting)[0] != "none" for posting in assets)
    if (any(posting.cost is not None and posting.cost.currency != currency or
            posting.price is not None and posting.price.currency != currency
            for posting in assets) or
            not costed and {posting.units.currency for posting in assets} != {currency}):
        return None
    if (any(posting.account.split(":", 1)[0] == fin.options["name_income"] for posting in outside) and
            not any(posting.cost is not None and exact(posting.units.number) < 0 for posting in assets)):
        return None  # New income alongside a purchase is not proved disposal income.
    owners = set()
    for posting in assets:
        claim = claims.get(facts.for_posting(posting).as_key())
        if not claim or not claim["slices"] or any(part["role"] != "hold" for part in claim["slices"]):
            return None
        owners.update(part["owner_id"] for part in claim["slices"])
    if len(owners) != 1:
        return None
    cash = {}
    for posting in assets:
        if (posting.units.currency == currency and posting.cost is None and
                posting.price is None and fin.raw_price(posting)[0] == "none"):
            cash[posting.account] = cash.get(posting.account, Fraction(0)) + exact(posting.units.number)
    outgoing = [account for account, amount in cash.items() if amount < 0]
    # With several cash sources, booking does not identify which one paid a
    # fee. Never spend that ambiguity as credit on another source's principal.
    fee_account = outgoing[0] if len(outgoing) == 1 and not any(amount > 0 for amount in cash.values()) else None
    return AccountExchange(assets, outside, next(iter(owners)), currency,
                           "securities_cost_or_price" if costed else "same_currency_asset_exchange",
                           sum((exact(posting.units.number) for posting in fees), Fraction(0)), fee_account)

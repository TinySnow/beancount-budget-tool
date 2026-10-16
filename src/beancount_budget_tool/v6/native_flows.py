"""Exact native asset/expense or asset/income groups, without pairwise guesses."""

from dataclasses import dataclass
from fractions import Fraction

from beancount.core import data

from ..exact import exact


@dataclass(frozen=True)
class NativeFlow:
    kind: str
    assets: tuple
    counterparts: tuple
    asset_total: Fraction


def native_asset_flow(fin, entry, modes: dict | None = None) -> NativeFlow | None:
    """Recognize a whole, one-direction, same-commodity booked flow.

    Expense and Income may not mix, nor may assets move in both directions.
    No costs, prices, liabilities, equity or unrecognized accounts are allowed.
    This proves only the joint native equation, never a cash/category pairing
    or the payment capability of an asset. Callers auditing H pass its modes.
    """
    if not isinstance(entry, data.Transaction):
        return None
    assets = tuple(p for p in entry.postings if p.account.split(":", 1)[0] == fin.options["name_assets"])
    if not assets or modes is not None and any(modes.get(p.account) != "H" for p in assets):
        return None
    remaining = tuple(p for p in entry.postings if p.account.split(":", 1)[0] != fin.options["name_assets"])
    roots = {p.account.split(":", 1)[0] for p in remaining}
    if roots == {fin.options["name_expenses"]}:
        kind = "expense"
    elif roots == {fin.options["name_income"]}:
        kind = "income"
    else:
        return None
    currency = assets[0].units.currency
    if any(p.cost is not None or p.price is not None or fin.raw_price(p)[0] != "none" or
           p.units.currency != currency for p in entry.postings):
        return None
    total = sum((exact(p.units.number) for p in assets), Fraction(0))
    if sum((exact(p.units.number) for p in remaining), total) != 0:
        return None
    if total == 0:
        if any(exact(p.units.number) != 0 for p in entry.postings):
            return None
    elif (any(exact(p.units.number) * total < 0 for p in assets) or
          any(exact(p.units.number) * total > 0 for p in remaining)):
        return None
    return NativeFlow(kind, assets, remaining, total)

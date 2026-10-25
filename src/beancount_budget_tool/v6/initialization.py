"""Recognize initial pad balances without hiding later reconciliation changes."""

from datetime import date

from beancount.core import data

from ..financial import FinancialInput
from .accounts import _lot
from .facts import FactIndex, GeneratedTransactionRef


def initial_pad_postings(fin: FinancialInput, cutover: date) -> dict[int, data.Transaction]:
    """Return first positive, cost-free Asset/Equity pad facts after cutover.

    The configured default income owner is also the initial money's default
    purpose. This is neither actual income nor a purpose allocation. Once a
    coordinate has any booked history, a later pad is a reconciliation change
    and must not silently reset its ownership. File/account names are irrelevant.
    """
    facts = FactIndex(fin)
    seen = set()
    result = {}
    for entry in fin.entries:
        if not isinstance(entry, data.Transaction):
            continue
        assets = [p for p in entry.postings
                  if p.account.split(":", 1)[0] == fin.options["name_assets"]]
        identity = facts.for_transaction(entry)
        if (entry.date >= cutover and len(entry.postings) == 2 and len(assets) == 1 and
                isinstance(identity, GeneratedTransactionRef) and identity.generated_kind == "pad"):
            asset = assets[0]
            counterpart = next(p for p in entry.postings if p is not asset)
            coordinate = (asset.account, asset.units.currency, _lot(asset))
            if (coordinate not in seen and asset.units.number > 0 and
                    asset.cost is None and asset.price is None and
                    counterpart.account.split(":", 1)[0] == fin.options["name_equity"] and
                    counterpart.cost is None and counterpart.price is None and
                    counterpart.units.currency == asset.units.currency and
                    counterpart.units.number == -asset.units.number):
                result[id(asset)] = entry
        seen.update((p.account, p.units.currency, _lot(p)) for p in assets)
    return result

"""Read-only historical valuation audit for classified v6 postings.

Native report-currency amounts and explicit prices on the classified source
posting can be proved here.  Payment legs, later settlement rates, cost lots
and current market quotes are never silently substituted for event value.
"""

from __future__ import annotations

from datetime import date
from fractions import Fraction
from pathlib import Path

from beancount.core import data

from ..errors import require
from ..exact import exact, exact_text
from ..financial import load_financial, shared_audit
from .classification import _key, audit_classification
from .facts import FactIndex, GeneratedPostingRef


@shared_audit
def audit_classification_valuation(root: Path, cutover: date, classifications: Path,
                                   report_currency: str) -> dict:
    """Price approved classification slices or list missing event evidence.

    This does not certify ``net_expense``: classification completeness,
    economic-benefit deduplication and other stage-three gates still apply.
    """
    claims = audit_classification(root, cutover, classifications)
    fin = load_financial(root)
    require(report_currency in fin.options.get("operating_currency", []),
            "REPORT_CURRENCY_INVALID", "报告货币须选自账本 operating_currency")
    facts = FactIndex(fin)
    postings = facts.table.postings
    valued = []
    missing = []
    native_totals: dict[tuple[str, str, str, str], Fraction] = {}
    for claim in claims["classifications"]:
        posting, booked_date = postings[_key(claim["ref"])]
        booked_month = booked_date.isoformat()[:7]
        native = exact(posting.units.number)
        role = ("net_expense" if posting.account.split(":", 1)[0] == fin.options["name_expenses"]
                else "income_supplement")
        for part in claim["slices"]:
            key = (booked_month, role, part["category_id"],
                   posting.units.currency)
            native_totals[key] = native_totals.get(key, Fraction(0)) + Fraction(part["signed_units"])
        if isinstance(facts.for_posting(posting), GeneratedPostingRef):
            notation, amount, currency = "none", Fraction(0), ""
        else:
            notation, amount, currency = fin.raw_price(posting)
        if notation != "none" and currency == report_currency:
            total = (amount * abs(native) if notation == "@" else amount)
            total = total if native >= 0 else -total
            proof = "source_price_" + notation
        elif posting.units.currency == report_currency:
            total = native
            proof = "native_report_currency"
        else:
            if notation == "none" or currency != report_currency:
                meta = posting.meta or {}
                missing.append({"ref": claim["ref"], "account": posting.account,
                                "signed_units": exact_text(native),
                                "commodity": posting.units.currency,
                                "month": booked_month,
                                "native_slices": claim["slices"],
                                "source": f"{meta.get('filename', '?')}:{meta.get('lineno', '?')}",
                                "reason_code": "HISTORICAL_VALUE_MISSING"})
                continue
        slices = []
        for part in claim["slices"]:
            units = Fraction(part["signed_units"])
            value = total * units / native if native else Fraction(0)
            slices.append({"id": part["id"], "category_id": part["category_id"],
                           "signed_report_value": exact_text(value)})
        require(sum((Fraction(row["signed_report_value"]) for row in slices), Fraction(0)) == total,
                "HISTORICAL_VALUE_INVALID", "分类切片历史价值不守恒", str(claim["ref"]))
        valued.append({"ref": claim["ref"], "account": posting.account,
                       "historical_report_value": exact_text(total),
                       "report_currency": report_currency, "proof": proof,
                       "slices": slices})
    return {"mode": "v6_classification_valuation_audit_only",
            "ledger_snapshot": fin.digest, "cutover": cutover.isoformat(),
            "report_currency": report_currency,
            "review_state": "complete" if claims["review_state"] == "complete" and not missing else "pending",
            "unclassified_postings": len(claims["missing_classifications"]),
            "classified_postings": claims["claimed_postings"],
            "native_totals": [
                {"month": month, "role": role, "category_id": category,
                 "commodity": commodity, "signed_units": exact_text(amount)}
                for (month, role, category, commodity), amount in sorted(native_totals.items())],
            "valued_postings": len(valued), "missing_historical_value": missing,
            "valued": valued,
            "note": "历史计价核对不证明经济利益去重，也不签发净费用或首页"}

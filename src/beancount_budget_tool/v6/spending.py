"""Monthly v6 expense and income-supplement projection, independently gated."""

from __future__ import annotations

from calendar import monthrange
from collections import defaultdict
from datetime import date
from fractions import Fraction
from pathlib import Path

from beancount.core import data

from ..errors import require
from ..exact import exact_text
from ..financial import load_financial
from ..yamlstrict import load_yaml
from .benefits import audit_economic_benefits
from .classification import _key, audit_classification
from .facts import FactIndex
from .valuation import audit_classification_valuation
from .review_diagnostics import posting_sources, review_item


def _audit_monthly_spending(root: Path, cutover: date, month: str,
                            classifications: Path, report_currency: str,
                            benefit_groups: Path | None = None,
                            budget_values: list[dict] | None = None, *, include_evidence: bool = False) -> dict:
    """Certify each monthly booked budget projection only when its inputs close.

    This is not a cash or plan report. Income-supplement classification does
    not assert that money is currently payable or that a plan was realized.
    """
    require(type(month) is str and len(month) == 7 and month[4] == "-" and
            month[:4].isdigit() and month[5:].isdigit(),
            "MONTH_INVALID", "月份须为 YYYY-MM", str(month))
    try:
        first = date.fromisoformat(month + "-01")
    except ValueError:
        require(False, "MONTH_INVALID", "月份须为有效 YYYY-MM", str(month))
    require(first >= cutover, "MONTH_INVALID", "报表月份不得早于 v6 cutover", month)
    last = date(first.year, first.month, monthrange(first.year, first.month)[1])
    return _audit_spending_window(root, cutover, first, last, classifications, report_currency,
                                  benefit_groups, budget_values, month=month, include_evidence=include_evidence)


def _audit_spending_window(root: Path, cutover: date, first: date, last: date,
                            classifications: Path, report_currency: str,
                            benefit_groups: Path | None = None,
                            budget_values: list[dict] | None = None, *, month: str | None = None,
                            include_evidence: bool = False) -> dict:
    """Project booked flows inside inclusive dates, retaining earlier source context."""
    require(cutover <= first <= last, "SPENDING_RANGE_INVALID", "费用区间须不早于 cutover，且起止有序")
    classification = audit_classification(root, cutover, classifications)
    valuation = audit_classification_valuation(root, cutover, classifications,
                                              report_currency)
    replacement = {_key(row["ref"]): row for row in (budget_values or [])}
    missing_keys = {_key(row["ref"]) for row in valuation["missing_historical_value"]}
    require(set(replacement) <= missing_keys and len(replacement) == len(budget_values or []),
            "BUDGET_BASIS_INVALID", "结算预算价值须唯一对应缺失历史计价的来源")
    benefits = audit_economic_benefits(root, cutover, classifications, benefit_groups)
    fin = load_financial(root)
    facts = FactIndex(fin)
    locations = posting_sources(fin)
    ref_dates = {_key(facts.for_posting(posting).as_dict()): entry.date
                 for entry in fin.entries if isinstance(entry, data.Transaction)
                 for posting in entry.postings}

    def in_range(row):
        return first <= ref_dates[_key(row["ref"])] <= last
    category_rows = load_yaml(classifications)["categories"]
    parents = {row["id"]: row["parent"] for row in category_rows}
    names = {row["id"]: row["name"] for row in category_rows}
    missing_classification = classification["missing_classifications"]
    missing_value = valuation["missing_historical_value"]
    pending_benefit = benefits["pending_benefits"]
    metrics = {}
    for role, root_name in (("net_expense", fin.options["name_expenses"]),
                            ("income_supplement", fin.options["name_income"])):
        role_claims = [row for row in classification["classifications"]
                       if row["account"].split(":", 1)[0] == root_name and
                       in_range(row)]
        role_missing = [row for row in missing_classification
                        if row["account"].split(":", 1)[0] == root_name and
                        in_range(row)]
        role_unvalued = [row for row in missing_value
                         if row["account"].split(":", 1)[0] == root_name and
                         in_range(row) and
                         (role != "net_expense" or _key(row["ref"]) not in replacement)]
        historical_unvalued_count = sum(
            row["account"].split(":", 1)[0] == root_name and
            in_range(row) for row in missing_value)
        role_benefits = [row for row in pending_benefit
                         if row["account"].split(":", 1)[0] == root_name and
                         in_range(row)]
        reasons = []
        if role_missing:
            reasons.append("CLASSIFICATION_PENDING")
        if role_unvalued:
            reasons.append("VALUATION_MISSING")
        if role_benefits:
            reasons.append("BENEFIT_SOURCE_PENDING")
        review_items = []
        for pending_rows, code in ((role_missing, "CLASSIFICATION_PENDING"),
                                   (role_unvalued, "VALUATION_MISSING"),
                                   (role_benefits, "BENEFIT_SOURCE_PENDING")):
            for row in pending_rows:
                review_items.append(review_item(
                    row["reason_code"] if code == "BENEFIT_SOURCE_PENDING" else code,
                    row.get("source") or locations[_key(row["ref"])],
                    account=row["account"], commodity=row["commodity"],
                    signed_units=row["signed_units"], ref=row["ref"],
                    blocking_reason_code=code))
        native_amounts = defaultdict(Fraction)
        for row in role_claims:
            booked_month = ref_dates[_key(row["ref"])].isoformat()[:7]
            for part in row["slices"]:
                key = (booked_month, part["category_id"], row["commodity"])
                native_amounts[key] += Fraction(part["signed_units"])
        native = [{"month": m, "role": role, "category_id": category,
                   "commodity": commodity, "signed_units": exact_text(amount)}
                  for (m, category, commodity), amount in sorted(native_amounts.items())]
        direct = {ident: Fraction(0) for ident in parents}
        gross_paid = {ident: Fraction(0) for ident in parents}
        gross_returned = {ident: Fraction(0) for ident in parents}

        def add_value(category: str, value: Fraction) -> None:
            direct[category] += value
            if role == "net_expense":
                if value >= 0:
                    gross_paid[category] += value
                else:
                    gross_returned[category] -= value

        for row in valuation["valued"]:
            if (row["account"].split(":", 1)[0] != root_name or
                    not in_range(row)):
                continue
            for part in row["slices"]:
                category = part["category_id"]
                if category != "-":
                    value = Fraction(part["signed_report_value"])
                    add_value(category, value if role == "net_expense" else -value)
        if role == "net_expense":
            for row in replacement.values():
                if not in_range(row):
                    continue
                for part in row["slices"]:
                    if part["category_id"] != "-":
                        add_value(part["category_id"], Fraction(part["signed_report_value"]))
        blocked_direct = set()
        for row in role_unvalued:
            blocked_direct.update(part["category_id"] for part in row["native_slices"]
                                  if part["category_id"] != "-")
        claims_by_ref = {_key(row["ref"]): row for row in role_claims}
        for row in role_benefits:
            claim = claims_by_ref.get(_key(row["ref"]))
            if claim is not None:
                blocked_direct.update(part["category_id"] for part in claim["slices"]
                                      if part["category_id"] != "-")
        if role_missing or any(part["category_id"] == "-"
                               for row in role_unvalued for part in row["native_slices"]):
            blocked_direct.update(parents)
        blocked_subtree = blocked_direct.copy()
        rolled = direct.copy()
        rolled_paid = gross_paid.copy()
        rolled_returned = gross_returned.copy()
        for ident, amount in direct.items():
            parent = parents[ident]
            while parent is not None:
                rolled[parent] += amount
                rolled_paid[parent] += gross_paid[ident]
                rolled_returned[parent] += gross_returned[ident]
                parent = parents[parent]
        for ident in blocked_direct:
            if ident == "-":
                continue
            parent = parents[ident]
            while parent is not None:
                blocked_subtree.add(parent)
                parent = parents[parent]
        category_details = []
        for ident in parents:
            detail = {"id": ident, "name": names[ident], "parent": parents[ident],
                      "active_direct": any(
                          part["category_id"] == ident
                          for row in role_claims for part in row["slices"]),
                      "direct_status": "NEEDS_REVIEW" if ident in blocked_direct else "CERTIFIED",
                      "direct": None if ident in blocked_direct else exact_text(direct[ident]),
                      "subtree_status": "NEEDS_REVIEW" if ident in blocked_subtree else "CERTIFIED",
                      "subtree": None if ident in blocked_subtree else exact_text(rolled[ident])}
            if role == "net_expense":
                detail.update({
                    "direct_gross_spent": None if ident in blocked_direct else exact_text(gross_paid[ident]),
                    "direct_refunded": None if ident in blocked_direct else exact_text(gross_returned[ident]),
                    "subtree_gross_spent": None if ident in blocked_subtree else exact_text(rolled_paid[ident]),
                    "subtree_refunded": None if ident in blocked_subtree else exact_text(rolled_returned[ident]),
                })
            category_details.append(detail)
        result = {"status": "NEEDS_REVIEW" if reasons else "CERTIFIED",
                  "reason_codes": reasons, "report_currency": report_currency,
                  "review_items": review_items,
                  "source_postings": len(role_claims) + len(role_missing),
                  "missing_classifications": len(role_missing),
                  "missing_historical_values": historical_unvalued_count,
                  "missing_budget_comparison_values": len(role_unvalued) if budget_values is not None else None,
                  "pending_benefit_sources": len(role_benefits),
                  "native_totals": native,
                  "source_refs": [row["ref"] for row in role_claims],
                  "report_total": None, "categories": category_details}
        if not reasons:
            result["report_total"] = exact_text(sum(direct.values(), Fraction(0)))
        metrics[role] = result
    evidence = ({"_transaction_evidence": {
        "classifications": [row for row in classification["classifications"] if in_range(row)],
        "valuations": [row for row in valuation["valued"] if in_range(row)],
        "benefits": [row for row in benefits["proved"] if in_range(row)],
    }} if include_evidence else {})
    return {**evidence, "mode": "v6_date_spending_report" if month is None else
                    "v6_monthly_budget_comparison_spending" if budget_values is not None
                    else "v6_monthly_spending_report", **({"month": month} if month is not None else
                          {"from": first.isoformat(), "through": last.isoformat()}),
            "cutover": cutover.isoformat(), "ledger_snapshot": fin.digest,
            "metrics": metrics,
            "note": ("费用预算比较可采用已核实的还款人民币成本；历史计价审计仍独立，原币消费不改写"
                     if budget_values is not None else
                     "净费用与收入补充独立签发；收入分类不证明现金可支付、用途归属或计划兑现")}


def audit_monthly_spending(root: Path, cutover: date, month: str,
                           classifications: Path, report_currency: str,
                           benefit_groups: Path | None = None) -> dict:
    """Public historical-value report; settlement values are never injected here."""
    return _audit_monthly_spending(root, cutover, month, classifications,
                                   report_currency, benefit_groups)

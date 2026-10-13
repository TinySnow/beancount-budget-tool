"""Expand an approved full-month free-only H policy into visible v6 claims.

The policy is personal review input, never an account-name default. A named
owner anywhere on the reviewed coordinate/month invalidates the entire
policy. Both the opening and every missing booked H leg are then claimed as
unassigned; the normal v6 audits still validate the expanded files.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import date
from fractions import Fraction
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory

import yaml
from beancount.core import data

from beancount_budget_tool.errors import require
from beancount_budget_tool.exact import exact, exact_text
from beancount_budget_tool.financial import load_financial
from beancount_budget_tool.yamlstrict import load_yaml
from beancount_budget_tool.v6.accounts import _lot, audit_accounts
from beancount_budget_tool.v6.classification import _key
from beancount_budget_tool.v6.facts import FactIndex
from beancount_budget_tool.v6.opening import _units, audit_opening
from beancount_budget_tool.v6.ownership import audit_ownership


def expand(root: Path, cutover: date, accounts: Path, opening: Path,
           ownership: Path, policies: Path) -> tuple[dict, dict, dict]:
    """Return expanded opening/ownership documents and a review summary."""
    fin = load_financial(root)
    account_audit = audit_accounts(root, cutover, accounts)
    require(account_audit["review_state"] == "complete", "FREE_POLICY_SCOPE_PENDING",
            "整月未指定用途策略须先完成账户语义与支付范围审阅")
    audit_opening(root, cutover, accounts, opening)
    owner_audit = audit_ownership(root, cutover, accounts, ownership)
    opening_doc = deepcopy(load_yaml(opening))
    owner_doc = deepcopy(load_yaml(ownership))
    policy_doc = load_yaml(policies)
    require(type(policy_doc) is dict and set(policy_doc) == {
        "schema", "cutover", "ledger_snapshot", "loader_version", "policies"},
        "FREE_POLICY_INVALID", "策略文件字段不完整或含未知字段", str(policies))
    require(type(policy_doc["schema"]) is int and policy_doc["schema"] == 6 and
            type(policy_doc["cutover"]) is str and policy_doc["cutover"] == cutover.isoformat() and
            policy_doc["ledger_snapshot"] == fin.digest and
            policy_doc["loader_version"] == fin.loader_version and
            type(policy_doc["policies"]) is list and bool(policy_doc["policies"]),
            "FREE_POLICY_STALE", "策略版本、源快照、cutover 或列表无效", str(policies))

    account_rows = {row["account"]: row for row in account_audit["accounts"]}
    facts = FactIndex(fin)
    missing = {_key(row["ref"]) for row in owner_audit["missing_h_ownership"]}
    existing = {_key(row["ref"]): row for row in owner_audit["ownership"]}
    seen_coords = set()
    generated_openings = 0
    generated_claims = 0
    details = []
    for policy in policy_doc["policies"]:
        require(type(policy) is dict and set(policy) == {
            "account", "commodity", "lot", "month", "opening_units", "reason"},
            "FREE_POLICY_INVALID", "每条策略须指定坐标、月份、期初数及理由")
        account, commodity, lot = (policy[field] for field in
                                   ("account", "commodity", "lot"))
        coord = (account, commodity, lot)
        require(all(type(value) is str and bool(value) for value in coord) and
                coord not in seen_coords,
                "FREE_POLICY_INVALID", "策略坐标非法或重复", str(coord))
        seen_coords.add(coord)
        require(type(policy["month"]) is str and
                policy["month"] == cutover.strftime("%Y-%m"),
                "FREE_POLICY_MONTH_UNSUPPORTED", "首版策略只支持 cutover 的完整自然月", str(coord))
        require(type(policy["reason"]) is str and bool(policy["reason"].strip()),
                "FREE_POLICY_INVALID", "整月未指定用途须有人工作出理由", str(coord))
        row = account_rows.get(account)
        require(row is not None and row.get("semantics") == "H" and
                commodity in row["commodities"],
                "FREE_POLICY_ACCOUNT_INVALID", "策略仅适用于已审 H 账户与已出现商品", str(coord))
        opening_coords = {(item["commodity"], item["lot"]): Fraction(item["signed_units"])
                          for item in row["opening_coordinates"]}
        require(not any(currency == commodity and other_lot != lot
                        for currency, other_lot in opening_coords),
                "FREE_POLICY_MULTI_LOT_UNSUPPORTED",
                "整月账户／商品存在其它期初 lot，不能只默认为其中一份", str(coord))
        booked_opening = opening_coords.get((commodity, lot), Fraction(0))
        require(_units(policy["opening_units"], str(coord)) == booked_opening and
                booked_opening >= 0,
                "FREE_POLICY_OPENING_MISMATCH", "策略期初数与 booked H 坐标不符", str(coord))
        existing_opening = [item for item in opening_doc["h_holdings"]
                            if (item["account"], item["commodity"], item["lot"]) == coord]
        require(not existing_opening or (len(existing_opening) == 1 and
                existing_opening[0]["owner"] == "-" and
                Fraction(existing_opening[0]["units"]) == booked_opening),
                "FREE_POLICY_NAMED_CONFLICT", "期初已有命名用途或部分认领，整月策略失效", str(coord))
        if booked_opening > 0 and not existing_opening:
            opening_doc["h_holdings"].append({
                "account": account, "commodity": commodity, "lot": lot,
                "owner": "-", "units": exact_text(booked_opening),
                "reason": f"整月未指定用途策略：{policy['reason']}"})
            generated_openings += 1

        month_postings = 0
        for entry in fin.entries:
            if (not isinstance(entry, data.Transaction) or
                    not (cutover <= entry.date and entry.date.strftime("%Y-%m") == policy["month"])):
                continue
            for posting in entry.postings:
                if posting.account == account and posting.units.currency == commodity:
                    require(_lot(posting) == lot, "FREE_POLICY_MULTI_LOT_UNSUPPORTED",
                            "整月账户／商品出现其它 lot，不能只默认为其中一份", str(coord))
                if (posting.account, posting.units.currency, _lot(posting)) != coord:
                    continue
                month_postings += 1
                ref = facts.for_posting(posting).as_dict()
                key = _key(ref)
                reviewed = existing.get(key)
                if reviewed is not None:
                    require(all(part["owner_id"] == "-" and part["role"] == "hold"
                                for part in reviewed["slices"]),
                            "FREE_POLICY_NAMED_CONFLICT",
                            "整月坐标已有命名用途或划拨声明，策略整月失效", key)
                    continue
                require(key in missing, "FREE_POLICY_REF_INVALID",
                        "H 来源既非已有未指定认领亦非待审腿", key)
                digest = sha256(key.encode()).hexdigest()
                owner_doc["claims"].append({
                    "claim": ref, "reason": f"整月未指定用途策略：{policy['reason']}",
                    "slices": [{"id": f"free_{digest}", "owner_id": "-",
                                "role": "hold",
                                "signed_units": exact_text(exact(posting.units.number))}]})
                generated_claims += 1
        require(month_postings > 0 or booked_opening > 0,
                "FREE_POLICY_NO_MATCH", "策略既无期初数也无当月 H posting", str(coord))
        details.append({"account": account, "commodity": commodity, "lot": lot,
                        "month": policy["month"], "booked_h_postings": month_postings})

    with TemporaryDirectory() as folder:
        temp_opening = Path(folder) / "opening.yaml"
        temp_ownership = Path(folder) / "ownership.yaml"
        temp_opening.write_text(yaml.safe_dump(opening_doc, allow_unicode=True,
                                              sort_keys=False), encoding="utf-8")
        temp_ownership.write_text(yaml.safe_dump(owner_doc, allow_unicode=True,
                                                sort_keys=False), encoding="utf-8")
        audit_opening(root, cutover, accounts, temp_opening)
        audit_ownership(root, cutover, accounts, temp_ownership)
    return opening_doc, owner_doc, {"generated_openings": generated_openings,
                                    "generated_claims": generated_claims,
                                    "coordinates": details,
                                    "ledger_snapshot": fin.digest}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", required=True, type=Path)
    parser.add_argument("--cutover", required=True, type=date.fromisoformat)
    parser.add_argument("--accounts", required=True, type=Path)
    parser.add_argument("--opening", required=True, type=Path)
    parser.add_argument("--ownership", required=True, type=Path)
    parser.add_argument("--policies", required=True, type=Path)
    parser.add_argument("--output-opening", required=True, type=Path)
    parser.add_argument("--output-ownership", required=True, type=Path)
    args = parser.parse_args()
    require(not args.output_opening.exists() and not args.output_ownership.exists() and
            args.output_opening != args.output_ownership,
            "FREE_POLICY_OUTPUT_EXISTS", "输出文件已存在或两个输出路径相同")
    opening, ownership, summary = expand(args.ledger, args.cutover, args.accounts,
                                          args.opening, args.ownership, args.policies)
    args.output_opening.write_text(yaml.safe_dump(opening, allow_unicode=True,
                                                  sort_keys=False), encoding="utf-8")
    args.output_ownership.write_text(yaml.safe_dump(ownership, allow_unicode=True,
                                                    sort_keys=False), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()

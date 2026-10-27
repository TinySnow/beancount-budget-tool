"""Run only the adjacent fictitious ledger through three consecutive months."""

import argparse
import json
from pathlib import Path
import sys

from beancount.core import data
import yaml

from beancount_budget_tool.console import configure_output
from beancount_budget_tool.errors import BudgetError, require
from beancount_budget_tool.financial import load_financial
from beancount_budget_tool.v6.input_compiler import (SetupDumper as _SetupDumper, publish_inputs as _publish, draft_inputs, compile_inputs, read_month_config)
from copy import deepcopy
from beancount_budget_tool.financial import month_bounds
from beancount_budget_tool.v6.facts import FactIndex
from beancount_budget_tool.v6.monthly_report import audit_month_workflow, render_workflow_text


def save(path, document):
    path.write_text(yaml.dump(document, Dumper=_SetupDumper, allow_unicode=True,
                              sort_keys=False), encoding="utf-8")


def build_demo(output: Path) -> dict:
    """Copy fixed public inputs; confirmations apply only to this virtual copy."""
    source = Path(__file__).resolve().parent
    output = output.resolve()
    _publish(output, {name: (source / name).read_text(encoding="utf-8")
                      for name in ("main.bean", "decisions.yaml")})
    ledger = output / "main.bean"
    decisions = yaml.safe_load((output / "decisions.yaml").read_text(encoding="utf-8"))
    sheet, issues = draft_inputs(ledger, '2026-10', income_purpose='自由资金',
                                  expense_purpose='生活费', expense_category='生活开销')
    sheet['ledger'] = str(ledger)
    setup = output/'inputs.yaml'
    sheet["defaults"]["confirmed"] = True
    sheet["completeness_confirmed"] = True
    for row in sheet["accounts"]:
        row.update(semantics="H", reason="本公开虚拟账本的人民币现金，可直接支付")
        row["payment_scope"] = {"CNY": "payable"}
        for opening in row["opening"]:
            opening["owners"] = decisions["opening"][row["account"]]
    sheet["expense_targets"] = decisions["expense_targets"]
    sheet["allocation_targets"] = decisions["allocation_targets"]
    save(setup, sheet)
    files, issues = compile_inputs(sheet, setup, output/'initial')
    require(not issues, 'DEMO_SETUP_FAILED', str(issues))
    _publish(output/'initial', files)
    config = output/'initial/month.yaml'
    ownership = yaml.safe_load((config.parent / "ownership.yaml").read_text(encoding="utf-8"))
    owners = {row["name"]: row["id"] for row in ownership["owners"]}
    obligations = yaml.safe_load((config.parent / "obligations.yaml").read_text(encoding="utf-8"))
    # Bind all four explicit virtual decisions, including later settlements.
    # The auditor must keep later events out of an earlier report.
    financial = load_financial(ledger)
    facts = FactIndex(financial)
    matched = set()
    for entry in financial.entries:
        if not isinstance(entry, data.Transaction) or entry.narration not in decisions["obligations"]:
            continue
        claim = dict(decisions["obligations"][entry.narration])
        if "owner" in claim:
            claim["owner_id"] = owners[claim.pop("owner")]
        index = next(i for i, posting in enumerate(entry.postings)
                     if posting.account.startswith("Liabilities:"))
        claim.update(liability_ref=facts.for_posting(entry.postings[index]).as_dict(),
                     counterpart_ref=facts.for_posting(entry.postings[1 - index]).as_dict(),
                     reason="公开虚拟账本的明确责任决定，见 decisions.yaml")
        obligations["events"].append(claim)
        matched.add(entry.narration)
    require(matched == set(decisions["obligations"]), "DEMO_DECISION_MISSING", "虚构责任决定没有匹配到账本")
    save(config.parent / "obligations.yaml", obligations)
    configs = {"2026-10": config}
    outcomes = {"2026-10": audit_month_workflow(config)}
    for month in ("2026-11", "2026-12"):
        directory = output/month
        directory.mkdir()
        document, paths, _ = read_month_config(config)
        old_month = document['month']
        document.update({key:str(path) for key,path in paths.items()})
        document['month'] = month
        for key in ('expense_plans','purpose_plans'):
            plan = yaml.safe_load(paths[key].read_text(encoding='utf-8'))
            plan.setdefault('months',{})[month] = deepcopy(plan.get('months',{}).get(old_month,{}))
            path = directory/(key+'.yaml')
            save(path,plan); document[key] = str(path)
        complete = directory/'completeness.yaml'
        save(complete, {'schema':6, 'cutover':document['cutover'], 'as_of':month_bounds(month)[1].isoformat(),
            'unbooked_obligations':'none', 'inflight_payments':'none','reason':'公开虚拟账本包含全部交易'})
        document['completeness'] = str(complete)
        config = directory/'month.yaml'
        save(config,document)
        configs[month] = config
        outcomes[month] = audit_month_workflow(config)
    for month, outcome in outcomes.items():
        (output / (month + ".txt")).write_text(render_workflow_text(outcome), encoding="utf-8")
        (output / (month + ".json")).write_text(json.dumps(outcome, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ledger": ledger, "configs": configs, "outcomes": outcomes}


def main(argv=None):
    configure_output()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="新的输出目录；不会覆盖已有文件")
    args = parser.parse_args(argv)
    try:
        result = build_demo(args.output)
    except (BudgetError, OSError, UnicodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    for month, outcome in result["outcomes"].items():
        report = outcome["report"]
        value = report["max_spendable"]["max_spendable"] if report else "待审"
        print(f"{month}: {outcome['status']}，最多能花 {value} CNY")
        print(f"  配置：{result['configs'][month]}")
    return 0 if all(row["status"] == "CERTIFIED_SCOPED" for row in result["outcomes"].values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())

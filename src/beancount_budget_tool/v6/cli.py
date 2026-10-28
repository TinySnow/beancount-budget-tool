"""v6 的独立审计入口；不签发账本预算报告。"""

import argparse
from datetime import date
import json
from pathlib import Path
import sys

from ..errors import BudgetError, require


def main(argv=None) -> int:
    from ..console import configure_output
    configure_output()
    parser = argparse.ArgumentParser(description="Beancount Budget v6 staged audits and monthly spending metrics")
    parser.add_argument("--ledger", type=Path, required=True, help="根 Beancount 文件")
    parser.add_argument("--cutover", required=True, help="YYYY-MM-01，v6 起算日")
    parser.add_argument("--account-audit", action="store_true", help="只读盘点全部直接记账资产账户")
    parser.add_argument("--facts-audit", action="store_true", help="只读核对原文与 booked 事实的稳定映射")
    parser.add_argument("--opening-audit", action="store_true", help="只读核对 H/K 期初声明")
    parser.add_argument("--liability-audit", action="store_true", help="只读盘点有直接 posting 的负债账户")
    parser.add_argument("--obligation-audit", action="store_true", help="逐笔核对简单消费卡债与付款来源")
    parser.add_argument("--classification-audit", action="store_true", help="只读核对费用／收入分类切片")
    parser.add_argument("--valuation-audit", action="store_true", help="只读核对已分类 posting 的历史报告价值")
    parser.add_argument("--benefit-audit", action="store_true", help="只读核对退款与收入补充的到账／减债来源")
    parser.add_argument("--spending-audit", action="store_true", help="按月分别签发净费用与收入补充")
    parser.add_argument("--target-audit", action="store_true", help="按月对比 v6 消费目标与已认证净费用")
    parser.add_argument("--purpose-target-audit", action="store_true", help="只读展开用途月划拨目标，不推断实际净划拨")
    parser.add_argument("--purpose-flow-audit", action="store_true", help="按月核对已认证用途边界，不完整时不签发净划拨总数")
    parser.add_argument("--purpose-progress-audit", action="store_true", help="按月比较用途目标 Q 与已认证净划拨 G")
    parser.add_argument("--purpose-pool-audit", action="store_true", help="核对排除支付范围的 H 证券池受保护名义本金")
    parser.add_argument("--ownership-audit", action="store_true", help="只读核对已审 H 资产腿的用途切片")
    parser.add_argument("--holdings-audit", action="store_true", help="按日末重放已审 H 用途原生库存")
    parser.add_argument("--k-event-audit", action="store_true", help="逐笔核对简单 K 申赎与 H 现金对手")
    parser.add_argument("--h-transfer-audit", action="store_true", help="逐笔核对简单同币种 H 资产转账")
    parser.add_argument("--h-gain-audit", action="store_true", help="逐笔核对已审 H 损益的 Income 来源")
    parser.add_argument("--h-fx-audit", action="store_true", help="逐笔核对有原文总价的单用途 H 购汇")
    parser.add_argument("--payable-cash-audit", action="store_true", help="核对报告货币可直接支付的完整 H 现金用途库存")
    parser.add_argument("--actual-audit", action="store_true", help="编译可支付现金、已记消费责任和已审覆盖的日末实际可花")
    parser.add_argument("--accounts", type=Path, help="schema 6 人工账户声明；供资产相关审计使用")
    parser.add_argument("--opening", type=Path, help="schema 6 人工期初声明；亦可供归属与 H 转账继承已审期初用途")
    parser.add_argument("--ownership", type=Path, help="schema 6 H 资产逐腿归属；供资产相关审计使用")
    parser.add_argument("--k-events", type=Path, help="schema 6 已审简单 K 申赎事件；须配合 --k-event-audit")
    parser.add_argument("--h-transfers", type=Path, help="schema 6 已审简单 H 转账事件；须配合 --h-transfer-audit")
    parser.add_argument("--h-fx", type=Path, help="schema 6 已审单用途购汇事件；须配合 --h-fx-audit")
    parser.add_argument("--obligations", type=Path, help="schema 6 已审期初未结责任、简单消费、还款或代垫事件")
    parser.add_argument("--completeness", type=Path, help="schema 6 报告日末未入账责任与在途付款完整性声明；须配合 --actual-audit")
    parser.add_argument("--debt-coverage", type=Path, help="schema 6 报告日已审责任覆盖；须配合 --actual-audit")
    parser.add_argument("--debt-settlements", type=Path, help="schema 6 外币债务锁定清偿账单；须配合 --actual-audit")
    parser.add_argument("--classifications", type=Path, help="schema 6 分类侧文件；供分类、计价、收益或月度审计使用")
    parser.add_argument("--benefit-groups", type=Path, help="schema 6 已审复杂收益来源组；供收益或月度审计使用")
    parser.add_argument("--plans", type=Path, help="schema 6 消费或用途目标；须配合对应目标审计")
    parser.add_argument("--purpose-pools", type=Path, help="schema 6 已审证券池本金保护声明")
    parser.add_argument("--report-currency", help="历史计价或月度报告的明确报告货币")
    parser.add_argument("--month", help="月度费用、消费目标或用途目标／净划拨审计的 YYYY-MM 月份")
    parser.add_argument("--as-of", help="持有、责任或可支付现金审计的 YYYY-MM-DD 日末")
    args = parser.parse_args(argv)
    # Help/argument errors do not need the financial engine. Keep the real
    # audit path unchanged and import it only after argparse accepts a request.
    from ..financial import load_financial
    from ..yamlstrict import load_yaml
    from .accounts import audit_accounts
    from .actual_snapshot import audit_actual_snapshot
    from .benefits import audit_economic_benefits
    from .classification import audit_classification
    from .facts import FactIndex
    from .holdings import audit_h_holdings
    from .h_gains import audit_h_income_gains
    from .h_fx import audit_h_fx
    from .h_transfers import audit_h_transfers
    from .k_events import audit_k_events
    from .liabilities import audit_liabilities
    from .obligations import audit_simple_obligations
    from .opening import audit_opening
    from .ownership import _owners, audit_ownership
    from .payable_cash import audit_payable_cash
    from .purpose_flows import audit_monthly_purpose_flows
    from .purpose_pools import audit_purpose_pools
    from .purpose_progress import audit_monthly_purpose_progress
    from .purpose_targets import load_purpose_targets
    from .source import scan_v6_source
    from .spending import audit_monthly_spending
    from .targets import audit_monthly_expense_targets
    from .valuation import audit_classification_valuation
    from beancount.core import data
    if sum((args.account_audit, args.facts_audit, args.opening_audit,
            args.liability_audit, args.obligation_audit, args.classification_audit, args.valuation_audit,
            args.benefit_audit, args.spending_audit, args.ownership_audit,
            args.target_audit, args.purpose_target_audit, args.purpose_flow_audit,
            args.purpose_progress_audit,
            args.purpose_pool_audit,
            args.holdings_audit, args.k_event_audit,
            args.h_transfer_audit, args.h_gain_audit, args.h_fx_audit,
            args.payable_cash_audit, args.actual_audit)) > 1:
        parser.error("审计模式只能选择一个")
    if args.accounts and not (args.account_audit or args.opening_audit or
                              args.ownership_audit or args.holdings_audit or
                              args.k_event_audit or args.h_transfer_audit or args.h_gain_audit or
                              args.h_fx_audit or args.payable_cash_audit or args.actual_audit or
                              args.purpose_target_audit or args.purpose_flow_audit or
                              args.purpose_progress_audit or args.target_audit):
        parser.error("--accounts 须配合账户、期初、归属、持有、K 事件、H 转账、H 损益或 H 购汇审计")
    if args.opening_audit and not args.accounts:
        parser.error("--opening-audit 必须提供 --accounts")
    if args.opening and not (args.opening_audit or args.holdings_audit or
                             args.payable_cash_audit or args.actual_audit or
                             args.purpose_flow_audit or args.purpose_progress_audit or
                             args.ownership_audit or args.h_transfer_audit):
        parser.error("--opening 只能与期初、归属、H 转账、持有或可支付现金审计一起使用")
    if args.ownership_audit and not args.accounts:
        parser.error("--ownership-audit 必须提供 --accounts")
    if args.ownership and not (args.ownership_audit or args.holdings_audit or
                               args.k_event_audit or args.h_transfer_audit or args.h_gain_audit or
                               args.h_fx_audit or args.payable_cash_audit or args.actual_audit or
                               args.purpose_target_audit or args.purpose_flow_audit or
                               args.purpose_progress_audit or args.target_audit):
        parser.error("--ownership 只能与归属、持有、K 事件、H 转账、H 损益或 H 购汇审计一起使用")
    if args.holdings_audit and not (args.accounts and args.opening and args.ownership and args.as_of):
        parser.error("--holdings-audit 必须提供账户、期初、归属和日末日期")
    if args.payable_cash_audit and not (args.accounts and args.opening and args.ownership and args.as_of):
        parser.error("--payable-cash-audit 必须提供账户、期初、归属和日末日期")
    if args.actual_audit and not (args.accounts and args.opening and args.ownership and
                                  args.obligations and args.as_of):
        parser.error("--actual-audit 必须提供账户、期初、归属、责任事件和日末日期")
    if args.as_of and not (args.holdings_audit or args.obligation_audit or
                           args.payable_cash_audit or args.actual_audit or args.purpose_pool_audit):
        parser.error("--as-of 只能与持有、责任或可支付现金审计一起使用")
    if args.k_event_audit and not (args.accounts and args.ownership):
        parser.error("--k-event-audit 必须提供账户和 H 归属声明")
    if args.k_events and not (args.k_event_audit or args.purpose_flow_audit or args.purpose_progress_audit):
        parser.error("--k-events 只能与 K 事件或用途净划拨审计一起使用")
    if args.h_transfer_audit and not (args.accounts and args.ownership):
        parser.error("--h-transfer-audit 必须提供账户和 H 归属声明")
    if args.h_transfers and not (args.h_transfer_audit or args.purpose_flow_audit or args.purpose_progress_audit):
        parser.error("--h-transfers 只能与 H 转账或用途净划拨审计一起使用")
    if args.h_gain_audit and not (args.accounts and args.ownership):
        parser.error("--h-gain-audit 必须提供账户和 H 归属声明")
    if args.h_fx_audit and not (args.accounts and args.ownership):
        parser.error("--h-fx-audit 必须提供账户和 H 归属声明")
    if args.h_fx and not (args.h_fx_audit or args.purpose_flow_audit or args.purpose_progress_audit or args.target_audit):
        parser.error("--h-fx 只能与 H 购汇或用途净划拨审计一起使用")
    if args.obligations and not (args.obligation_audit or args.actual_audit or args.target_audit or
                                 args.purpose_flow_audit or args.purpose_progress_audit):
        parser.error("--obligations 只能与责任、实际可花或用途划拨审计一起使用")
    if args.completeness and not args.actual_audit:
        parser.error("--completeness 只能与 --actual-audit 一起使用")
    if args.debt_coverage and not args.actual_audit:
        parser.error("--debt-coverage 只能与 --actual-audit 一起使用")
    if args.debt_settlements and not args.actual_audit:
        parser.error("--debt-settlements 只能与 --actual-audit 一起使用")
    if args.classifications and not (args.classification_audit or args.valuation_audit or
                                     args.benefit_audit or args.spending_audit or args.target_audit):
        parser.error("--classifications 只能与分类、计价、经济利益或月度审计一起使用")
    if args.benefit_groups and not (args.benefit_audit or args.spending_audit or args.target_audit):
        parser.error("--benefit-groups 只能与收益、月度或目标审计一起使用")
    if args.valuation_audit and (not args.classifications or not args.report_currency):
        parser.error("--valuation-audit 必须同时提供 --classifications 和 --report-currency")
    if args.report_currency and not (args.valuation_audit or args.spending_audit or
                                     args.target_audit or args.purpose_target_audit or
                                     args.purpose_progress_audit):
        parser.error("--report-currency 只能与计价、月度或目标审计一起使用")
    if args.spending_audit and not (args.classifications and args.report_currency and args.month):
        parser.error("--spending-audit 必须提供分类、报告货币和月份")
    if args.month and not (args.spending_audit or args.target_audit or
                           args.purpose_target_audit or args.purpose_flow_audit or
                           args.purpose_progress_audit):
        parser.error("--month 只能与月度费用、消费目标或用途目标审计一起使用")
    if args.plans and not (args.target_audit or args.purpose_target_audit or args.purpose_progress_audit):
        parser.error("--plans 只能与目标或用途进度审计一起使用")
    if args.target_audit and not (args.classifications and args.plans and
                                  args.report_currency and args.month):
        parser.error("--target-audit 必须提供分类、计划、报告货币和月份")
    if args.purpose_target_audit and not (args.accounts and args.ownership and
                                         args.plans and args.report_currency and args.month):
        parser.error("--purpose-target-audit 必须提供账户、用途归属、计划、报告货币和月份")
    if args.purpose_flow_audit and not (args.accounts and args.opening and
                                       args.ownership and args.month):
        parser.error("--purpose-flow-audit 必须提供账户、期初、用途归属和月份")
    if args.purpose_progress_audit and not (args.accounts and args.opening and
                                           args.ownership and args.plans and
                                           args.report_currency and args.month):
        parser.error("--purpose-progress-audit 必须提供账户、期初、归属、计划、货币和月份")
    if args.purpose_pool_audit and not (args.purpose_pools and args.as_of):
        parser.error("--purpose-pool-audit 必须提供稳定的资金池声明和日末日期")
    if args.purpose_pools and not (args.purpose_pool_audit or args.purpose_flow_audit or
                                   args.purpose_progress_audit):
        parser.error("--purpose-pools 只能与资金池或用途净划拨审计一起使用")
    try:
        cutover = date.fromisoformat(args.cutover)
        try:
            as_of = date.fromisoformat(args.as_of) if args.as_of else None
        except ValueError as exc:
            raise BudgetError("TIME_INVALID", "--as-of 必须是有效 YYYY-MM-DD") from exc
        if args.account_audit:
            result = audit_accounts(args.ledger, cutover, args.accounts)
        elif args.opening_audit:
            result = audit_opening(args.ledger, cutover, args.accounts, args.opening)
        elif args.liability_audit:
            result = audit_liabilities(args.ledger, cutover)
        elif args.obligation_audit:
            result = audit_simple_obligations(args.ledger, cutover, args.obligations, as_of)
        elif args.classification_audit:
            result = audit_classification(args.ledger, cutover, args.classifications)
        elif args.valuation_audit:
            result = audit_classification_valuation(args.ledger, cutover,
                                                    args.classifications, args.report_currency)
        elif args.benefit_audit:
            result = audit_economic_benefits(args.ledger, cutover, args.classifications,
                                             args.benefit_groups)
        elif args.spending_audit:
            result = audit_monthly_spending(args.ledger, cutover, args.month,
                                            args.classifications, args.report_currency,
                                            args.benefit_groups)
        elif args.target_audit:
            result = audit_monthly_expense_targets(args.ledger, cutover, args.month,
                                                   args.classifications, args.plans,
                                                   args.report_currency, args.benefit_groups,
                                                   args.accounts, args.ownership,
                                                   args.obligations, args.h_fx)
        elif args.purpose_target_audit:
            account_audit = audit_accounts(args.ledger, cutover, args.accounts)
            owner_audit = audit_ownership(args.ledger, cutover, args.accounts,
                                          args.ownership)
            require(args.report_currency == account_audit["report_currency"],
                    "REPORT_CURRENCY_INVALID", "用途目标货币须与账户声明的报告货币一致")
            owner_ids = set(_owners(load_yaml(args.ownership)["owners"]).values())
            result = load_purpose_targets(args.plans, args.month,
                                          args.report_currency, owner_ids)
            result["ledger_snapshot"] = owner_audit["ledger_snapshot"]
            result["owner_review_state"] = owner_audit["review_state"]
        elif args.purpose_flow_audit:
            result = audit_monthly_purpose_flows(
                args.ledger, cutover, args.month, args.accounts, args.opening,
                args.ownership,
                args.h_transfers, args.k_events, args.h_fx, args.purpose_pools,
                args.obligations)
        elif args.purpose_progress_audit:
            result = audit_monthly_purpose_progress(
                args.ledger, cutover, args.month, args.accounts, args.opening,
                args.ownership, args.plans, args.report_currency,
                args.h_transfers, args.k_events, args.h_fx, args.purpose_pools,
                args.obligations)
        elif args.purpose_pool_audit:
            result = audit_purpose_pools(args.ledger, cutover, as_of, args.purpose_pools)
        elif args.ownership_audit:
            result = audit_ownership(args.ledger, cutover, args.accounts,
                                     args.ownership, args.opening)
        elif args.holdings_audit:
            result = audit_h_holdings(args.ledger, cutover, as_of,
                                      args.accounts, args.opening, args.ownership)
        elif args.k_event_audit:
            result = audit_k_events(args.ledger, cutover, args.accounts,
                                    args.ownership, args.k_events)
        elif args.h_transfer_audit:
            result = audit_h_transfers(args.ledger, cutover, args.accounts,
                                       args.ownership, args.h_transfers, args.opening)
        elif args.h_gain_audit:
            result = audit_h_income_gains(args.ledger, cutover, args.accounts,
                                          args.ownership)
        elif args.h_fx_audit:
            result = audit_h_fx(args.ledger, cutover, args.accounts,
                                args.ownership, args.h_fx)
        elif args.payable_cash_audit:
            result = audit_payable_cash(args.ledger, cutover, as_of, args.accounts,
                                        args.opening, args.ownership)
        elif args.actual_audit:
            result = audit_actual_snapshot(args.ledger, cutover, as_of, args.accounts,
                                           args.opening, args.ownership,
                                           args.obligations, args.completeness, args.debt_coverage, args.debt_settlements)
        elif args.facts_audit:
            if cutover.day != 1:
                raise BudgetError("CUTOVER_INVALID", "v6 cutover 必须是自然月第一天")
            fin = load_financial(args.ledger)
            index = FactIndex(fin)
            transactions = [e for e in fin.entries if isinstance(e, data.Transaction)]
            result = {"mode": "v6_facts_audit_only", "source_digest": fin.digest,
                      "loader_version": fin.loader_version, "cutover": cutover.isoformat(),
                      "transactions": len(transactions),
                      "postings": sum(len(e.postings) for e in transactions),
                      "mapped_transactions": len(index.transaction_refs),
                      "mapped_postings": len(index.posting_refs),
                      "generated_pad_transactions": index.generated_transactions,
                      "generated_pad_postings": index.generated_postings,
                      "note": "来源映射不证明分类、归属或预算数字已认证"}
        else:
            audit = scan_v6_source(args.ledger, cutover)
            result = {"mode": "v6_source_audit_only",
                      "source_digest": audit.digest,
                      "loader_version": audit.loader_version,
                      "cutover": audit.cutover.isoformat(),
                      "source_files": audit.source_files,
                      "transactions": audit.transactions,
                      "postings": audit.postings,
                      "missing_inline_classification": audit.missing_inline_classification,
                      "missing_inline_asset_intent": audit.missing_inline_asset_intent}
    except ValueError:
        print(json.dumps({"error": {"code": "CUTOVER_INVALID",
                                    "message": "cutover 必须是有效 YYYY-MM-01"}},
                         ensure_ascii=False), file=sys.stderr)
        return 2
    except BudgetError as exc:
        print(json.dumps({"error": {"code": exc.code, "message": exc.message,
                                    "source": exc.source}}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0

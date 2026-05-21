//! 独立预算分析工具（面向 Beancount 账本）。
//!
//! 该工具读取 Beancount 账本与预算配置，支持：
//! - 月度预算与"同月额外预算"（如 `YYYY-MM 绩效`）聚合；
//! - 月度或累计视角（截至目标月）的预算结余统计；
//! - 交易级 `budget` metadata 优先；
//! - 未显式标注预算桶的 `Expenses:*` 自动归入默认生活费桶；
//! - 资产类预算桶（如储蓄）在 `Assets` 账户间转移时也可统计，并可查看资金位置。

mod cli;
mod util;
mod config;
mod ledger;
mod budget;
mod report;

use anyhow::{Context, Result, bail};
use clap::Parser;
use cli::Cli;

use crate::util::fmt_decimal;

/// 程序入口。
///
/// 1. 解析 CLI 参数
/// 2. 加载账本文件与配置文件
/// 3. 计算预算流与汇总
/// 4. 渲染并输出报告
/// 5. 可选导出 Markdown/CSV 文件
fn main() -> Result<()> {
    let cli = Cli::parse();
    util::validate_month(&cli.month)?;
    let ledger_files = cli::resolve_ledger_inputs(&cli)?;

    let budget_directives = config::load_budget_directives(&cli.budgets)
        .with_context(|| format!("Failed to load budgets: {}", cli.budgets.display()))?;
    let mappings = config::load_mappings(&cli.mappings)
        .with_context(|| format!("Failed to load mappings: {}", cli.mappings.display()))?;

    let target_currency = cli.currency.to_ascii_uppercase();
    let tx_flows = budget::collect_bucket_tx_flows(&ledger_files, &mappings, &target_currency)?;
    let summaries = budget::summarize_buckets(&budget_directives, &tx_flows, &cli.month, cli.scope);

    let known_buckets = config::collect_known_buckets(&budget_directives, &mappings);
    let warnings = budget::collect_scope_warnings(&tx_flows, &known_buckets, &cli.month, cli.scope);

    if let Some(bucket) = cli.bucket.as_ref() {
        let output = report::render_bucket_report_text(
            &budget::build_scoped_bucket_data(&cli, bucket, &mappings, &budget_directives, &tx_flows),
            &cli,
            &target_currency,
            cli.bucket_view,
            cli.show_locations,
            &tx_flows,
        );
        print!("{output}");
    } else {
        let output = report::render_summary_report_text(
            &cli.month,
            cli.scope,
            &target_currency,
            &summaries,
            &warnings,
        );
        print!("{output}");
    }

    if let Some(out_dir) = cli.out_dir.as_ref() {
        report::export_reports(
            out_dir,
            &cli,
            &target_currency,
            &mappings,
            &budget_directives,
            &tx_flows,
            &summaries,
            &warnings,
        )?;
    }

    if cli.strict && !warnings.unknown_bucket_amount.is_zero() {
        bail!(
            "Strict mode failed: unknown budget buckets amount = {} {}",
            fmt_decimal(warnings.unknown_bucket_amount),
            target_currency
        );
    }

    Ok(())
}

// ===========================================================================
// 单元测试
// ===========================================================================
// 测试保留在 main.rs 中以访问 crate 级别的所有公共接口。

#[cfg(test)]
mod tests {
    use crate::{
        config::{self, BucketKind, BudgetDirective, BudgetMappings},
        budget::{collect_bucket_tx_flows, summarize_buckets},
        ledger::parse_ledger_content,
        ledger::parse_metadata_value,
        util::{default_expense_bucket, is_month_in_scope},
        cli::ReportScope,
    };
    use rust_decimal_macros::dec;
    use std::{
        collections::BTreeMap,
        fs,
        path::PathBuf,
        time::{SystemTime, UNIX_EPOCH},
    };

    #[test]
    fn parses_budget_metadata_and_expense_posting() {
        let ledger = concat!(
            "2026-05-10 * \"iPad\"\n",
            "  budget: \"数码\"\n",
            "  Expenses:Consume:电子  4999 CNY\n",
            "  Liabilities:CreditCard  -4999 CNY\n",
        );

        let txs = parse_ledger_content(ledger).expect("ledger parse should succeed");
        assert_eq!(txs.len(), 1);
        assert_eq!(
            txs[0].metadata.get("budget").map(String::as_str),
            Some("数码")
        );
        assert_eq!(txs[0].postings.len(), 2);
        assert_eq!(txs[0].postings[0].account, "Expenses:Consume:电子");
        assert_eq!(txs[0].postings[0].amount, Some(dec!(4999)));
        assert_eq!(txs[0].payee.as_deref(), Some("iPad"));
    }

    #[test]
    fn metadata_parser_unquotes_values() {
        assert_eq!(parse_metadata_value("\"electronics\""), "electronics");
        assert_eq!(parse_metadata_value(" 'travel' "), "travel");
        assert_eq!(parse_metadata_value("unquoted"), "unquoted");
    }

    #[test]
    fn longest_prefix_mapping_wins() {
        let mappings = BudgetMappings {
            defaults: BTreeMap::from([
                ("Expenses:Consume".to_string(), "消费".to_string()),
                ("Expenses:Consume:电子".to_string(), "数码".to_string()),
            ]),
            default_expense_bucket: default_expense_bucket(),
            bucket_types: BTreeMap::new(),
            asset_bucket_accounts: BTreeMap::new(),
        };

        let bucket = config::resolve_bucket_by_account(&mappings, "Expenses:Consume:电子:配件")
            .expect("should match");
        assert_eq!(bucket, "数码");
    }

    #[test]
    fn parses_budget_key_with_optional_label() {
        let (m1, l1) = config::parse_budget_key("2026-06").expect("valid");
        assert_eq!(m1, "2026-06");
        assert_eq!(l1, None);

        let (m2, l2) = config::parse_budget_key("2026-06 绩效").expect("valid");
        assert_eq!(m2, "2026-06");
        assert_eq!(l2.as_deref(), Some("绩效"));
    }

    #[test]
    fn untagged_expense_falls_back_to_default_bucket() {
        let tmp = make_temp_file(
            "2026-06-16 * \"中国工商银行\" \"网购\"\n  Expenses:Consume:人情礼物  40 CNY\n  Assets:Bank:ICBC  -40 CNY\n",
        );

        let mappings = BudgetMappings {
            defaults: BTreeMap::new(),
            default_expense_bucket: "生活费".to_string(),
            bucket_types: BTreeMap::new(),
            asset_bucket_accounts: BTreeMap::new(),
        };

        let flows = collect_bucket_tx_flows(&[tmp.clone()], &mappings, "CNY").expect("flows");
        fs::remove_file(tmp).ok();

        assert_eq!(flows.len(), 1);
        assert_eq!(flows[0].bucket, "生活费");
        assert_eq!(flows[0].kind, BucketKind::Expense);
        assert_eq!(flows[0].flow, dec!(-40));
    }

    #[test]
    fn asset_bucket_transfer_is_counted_and_locatable() {
        let tmp = make_temp_file(
            "2026-06-17 * \"中国工商银行\" \"储蓄\"\n  budget: \"储蓄\"\n  Assets:Bank:建设银行:卡号  40 CNY\n  Assets:Bank:工商银行:卡号  -40 CNY\n",
        );

        let mappings = BudgetMappings {
            defaults: BTreeMap::new(),
            default_expense_bucket: "生活费".to_string(),
            bucket_types: BTreeMap::from([("储蓄".to_string(), BucketKind::Asset)]),
            asset_bucket_accounts: BTreeMap::new(),
        };

        let flows = collect_bucket_tx_flows(&[tmp.clone()], &mappings, "CNY").expect("flows");
        fs::remove_file(tmp).ok();

        assert_eq!(flows.len(), 1);
        assert_eq!(flows[0].bucket, "储蓄");
        assert_eq!(flows[0].kind, BucketKind::Asset);
        assert_eq!(flows[0].flow, dec!(40));
        assert_eq!(
            flows[0].location_deltas.get("Assets:Bank:建设银行:卡号"),
            Some(&dec!(40))
        );
    }

    #[test]
    fn summarize_supports_month_and_cumulative_scope() {
        let directives = vec![
            BudgetDirective {
                month: "2026-05".to_string(),
                label: None,
                source_key: "2026-05".to_string(),
                bucket: "旅行".to_string(),
                amount: dec!(3000),
            },
            BudgetDirective {
                month: "2026-06".to_string(),
                label: None,
                source_key: "2026-06".to_string(),
                bucket: "旅行".to_string(),
                amount: dec!(2000),
            },
            BudgetDirective {
                month: "2026-06".to_string(),
                label: Some("绩效".to_string()),
                source_key: "2026-06 绩效".to_string(),
                bucket: "旅行".to_string(),
                amount: dec!(2000),
            },
        ];

        let tmp = make_temp_file(
            "2026-05-16 * \"工行\" \"旅行费用\"\n  budget: \"旅行\"\n  Expenses:Consume:机酒旅行  1000 CNY\n  Assets:Bank:ICBC  -1000 CNY\n",
        );

        let mappings = BudgetMappings {
            defaults: BTreeMap::new(),
            default_expense_bucket: "生活费".to_string(),
            bucket_types: BTreeMap::new(),
            asset_bucket_accounts: BTreeMap::new(),
        };

        let flows = collect_bucket_tx_flows(&[tmp.clone()], &mappings, "CNY").expect("flows");
        fs::remove_file(tmp).ok();

        let month_summary = summarize_buckets(&directives, &flows, "2026-06", ReportScope::Month);
        assert_eq!(month_summary["旅行"].planned, dec!(4000));
        assert_eq!(month_summary["旅行"].actual, dec!(0));

        let cum_summary =
            summarize_buckets(&directives, &flows, "2026-06", ReportScope::Cumulative);
        assert_eq!(cum_summary["旅行"].planned, dec!(7000));
        assert_eq!(cum_summary["旅行"].actual, dec!(1000));
    }

    #[test]
    fn month_scope_filter_works() {
        assert!(is_month_in_scope("2026-06", "2026-06", ReportScope::Month));
        assert!(!is_month_in_scope("2026-05", "2026-06", ReportScope::Month));
        assert!(is_month_in_scope(
            "2026-05",
            "2026-06",
            ReportScope::Cumulative
        ));
    }

    #[test]
    fn multi_bucket_metadata_splits_into_multiple_flows() {
        // 货币基金同时归属储蓄与投资两桶，赎回时两桶各计全额扣减
        let tmp = make_temp_file(
            "2026-06-20 * \"应急\" \"赎回货币基金\"\n  budget: \"储蓄, 投资\"\n  Assets:Bank:工商银行  5000 CNY\n  Assets:Invest:货币基金  -5000 CNY\n",
        );

        let mappings = BudgetMappings {
            defaults: BTreeMap::new(),
            default_expense_bucket: "生活费".to_string(),
            bucket_types: BTreeMap::from([
                ("储蓄".to_string(), BucketKind::Asset),
                ("投资".to_string(), BucketKind::Asset),
            ]),
            asset_bucket_accounts: BTreeMap::from([
                ("储蓄".to_string(), vec!["Assets:Invest:货币基金".to_string()]),
                ("投资".to_string(), vec!["Assets:Invest:货币基金".to_string()]),
            ]),
        };

        let flows = collect_bucket_tx_flows(&[tmp.clone()], &mappings, "CNY").expect("flows");
        fs::remove_file(tmp).ok();

        assert_eq!(flows.len(), 2);

        let savings = flows.iter().find(|f| f.bucket == "储蓄").expect("储蓄 flow");
        let invest = flows.iter().find(|f| f.bucket == "投资").expect("投资 flow");

        assert_eq!(savings.kind, BucketKind::Asset);
        assert_eq!(savings.flow, dec!(-5000));
        assert_eq!(
            savings.location_deltas.get("Assets:Invest:货币基金"),
            Some(&dec!(-5000))
        );

        assert_eq!(invest.kind, BucketKind::Asset);
        assert_eq!(invest.flow, dec!(-5000));
        assert_eq!(
            invest.location_deltas.get("Assets:Invest:货币基金"),
            Some(&dec!(-5000))
        );
    }

    fn make_temp_file(content: &str) -> PathBuf {
        let mut path = std::env::temp_dir();
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("clock")
            .as_nanos();
        path.push(format!("budget_report_test_{}.bean", nonce));
        fs::write(&path, content).expect("write temp file");
        path
    }
}

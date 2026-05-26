//! 独立预算分析工具（面向 Beancount 账本）。
//!
//! 该工具读取 Beancount 账本与预算配置，支持：
//! - 月度预算与"同月额外预算"（如 `YYYY-MM 绩效`）聚合；
//! - 月度或累计视角（截至目标月）的预算结余统计；
//! - 时间范围查询（`--from YYYY-MM --to YYYY-MM`）；
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
use cli::{Cli, DateRange, ReportScope};

use crate::util::fmt_decimal;

/// 将 UTF-8 文本写入 stdout（绕过 Windows 控制台编码问题）。
/// 写 UTF-8 BOM 作为编码标记。
fn write_stdout(text: &str) {
    use std::io::Write;
    let stdout = std::io::stdout();
    let mut handle = stdout.lock();
    let _ = handle.write_all(b"\xEF\xBB\xBF"); // UTF-8 BOM
    let _ = handle.write_all(text.as_bytes());
    let _ = handle.flush();
}

/// 程序入口。
fn main() -> Result<()> {
    let mut cli = Cli::parse();

    // 解析时间范围：--from/--to 与 --month 互斥
    let date_range = resolve_date_range(&cli)?;

    // 当使用 --from/--to 时，为了复用现有的 cumulative 汇总逻辑，
    // 将输入数据裁剪到 [from, to] 区间，并将 cli.month/scope 设为区间末月的累积模式
    if let DateRange::Range { ref from, ref to } = date_range {
        cli.month = Some(to.clone());
        cli.scope = ReportScope::Cumulative;
        let _ = from; // used in filtering below
    }

    let month_str = cli.month.as_deref().unwrap_or("?");
    let config = cli.report_config();
    let ledger_files = cli::resolve_ledger_inputs(&cli)?;

    let budget_directives = config::load_budget_directives(&cli.budgets)
        .with_context(|| format!("Failed to load budgets: {}", cli.budgets.display()))?;
    let mappings = config::load_mappings(&cli.mappings)
        .with_context(|| format!("Failed to load mappings: {}", cli.mappings.display()))?;

    let target_currency = cli.currency.to_ascii_uppercase();
    let tx_flows = budget::collect_bucket_tx_flows(&ledger_files, &mappings, &target_currency)?;

    // 时间段裁剪
    let budget_directives = filter_directives_by_range(budget_directives, &date_range);
    let tx_flows = filter_flows_by_range(tx_flows, &date_range);

    let summaries = budget::summarize_buckets(&budget_directives, &tx_flows, month_str, cli.scope, &mappings);

    let known_buckets = config::collect_known_buckets(&budget_directives, &mappings);
    let warnings = budget::collect_scope_warnings(&tx_flows, &known_buckets, month_str, cli.scope);

    if let Some(ref compare_month) = cli.compare {
        util::validate_month(compare_month)?;
        let cmp_range = match &date_range {
            DateRange::Month { scope, .. } => DateRange::Month {
                target: compare_month.clone(),
                scope: *scope,
            },
            DateRange::Range { to: _, .. } => DateRange::Range {
                from: format!("{}-01", &compare_month[..4]),
                to: compare_month.clone(),
            },
        };
        let cmp_directives = filter_directives_by_range(config::load_budget_directives(&cli.budgets)
            .with_context(|| format!("Failed to load budgets: {}", cli.budgets.display()))?, &cmp_range);
        let cmp_flows = filter_flows_by_range(
            budget::collect_bucket_tx_flows(&ledger_files, &mappings, &target_currency)?,
            &cmp_range,
        );
        let cmp_target = cmp_range.end_month().to_string();
        let cmp_summaries = budget::summarize_buckets(&cmp_directives, &cmp_flows, &cmp_target, cli.scope, &mappings);
        let cmp_warnings = budget::collect_scope_warnings(&cmp_flows, &known_buckets, &cmp_target, cli.scope);

        let output = report::render_compare_report_text(
            &date_range, &summaries, &warnings,
            &cmp_range, &cmp_summaries, &cmp_warnings,
            &target_currency, config.sort_by.as_deref(),
        );
        write_stdout(&output);
    } else if let Some(bucket) = cli.bucket.as_ref() {
        let output = report::render_bucket_report_text(
            &budget::build_scoped_bucket_data(&config, bucket, &mappings, &budget_directives, &tx_flows),
            &config,
            &target_currency,
            config.bucket_view,
            config.show_locations,
            &tx_flows,
            &date_range,
        );
        write_stdout(&output);
    } else {
        let output = report::render_summary_report_text(
            &date_range,
            &target_currency,
            &summaries,
            &warnings,
            config.sort_by.as_deref(),
        );
        write_stdout(&output);
    }

    if let Some(out_dir) = cli.out_dir.as_ref() {
        report::export_reports(
            out_dir,
            &config,
            &target_currency,
            &mappings,
            &budget_directives,
            &tx_flows,
            &summaries,
            &warnings,
            &date_range,
        )?;
    }

    if config.strict && !warnings.unknown_bucket_amount.is_zero() {
        bail!(
            "Strict mode failed: unknown budget buckets amount = {} {}",
            fmt_decimal(warnings.unknown_bucket_amount),
            target_currency
        );
    }

    Ok(())
}

/// 从 CLI 参数解析统计时间范围。
fn resolve_date_range(cli: &Cli) -> Result<DateRange> {
    if let Some(year) = &cli.year {
        if cli.month.is_some() || cli.from.is_some() || cli.to.is_some() {
            bail!("--year is mutually exclusive with --month/--from/--to");
        }
        let y: i32 = year.parse().context("--year must be a 4-digit number")?;
        return Ok(DateRange::Range {
            from: format!("{:04}-01", y),
            to: format!("{:04}-12", y),
        });
    }
    match (&cli.from, &cli.to, &cli.month) {
        (Some(from), Some(to), None) => {
            util::validate_month(from)?;
            util::validate_month(to)?;
            if from > to {
                bail!("--from ({}) must not be later than --to ({})", from, to);
            }
            Ok(DateRange::Range { from: from.clone(), to: to.clone() })
        }
        (None, None, Some(month)) => {
            util::validate_month(month)?;
            Ok(DateRange::Month { target: month.clone(), scope: cli.scope })
        }
        (Some(_), None, _) => bail!("--from requires --to"),
        (None, Some(_), _) => bail!("--to requires --from"),
        (None, None, None) => bail!("Either --month or (--from + --to) is required"),
        (Some(_), Some(_), Some(_)) => bail!("--from/--to and --month are mutually exclusive"),
    }
}

/// 按时间范围过滤预算指令。
fn filter_directives_by_range(
    directives: Vec<config::BudgetDirective>,
    range: &DateRange,
) -> Vec<config::BudgetDirective> {
    directives
        .into_iter()
        .filter(|d| range.contains(&d.month))
        .collect()
}

/// 按时间范围过滤资金流动记录。
fn filter_flows_by_range(
    flows: Vec<budget::BucketTxFlow>,
    range: &DateRange,
) -> Vec<budget::BucketTxFlow> {
    flows
        .into_iter()
        .filter(|f| range.contains(&f.month))
        .collect()
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
        collections::{BTreeMap, BTreeSet},
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

        let month_summary = summarize_buckets(&directives, &flows, "2026-06", ReportScope::Month, &mappings);
        assert_eq!(month_summary["旅行"].planned, dec!(4000));
        assert_eq!(month_summary["旅行"].actual, dec!(0));

        let cum_summary =
            summarize_buckets(&directives, &flows, "2026-06", ReportScope::Cumulative, &mappings);
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

    #[test]
    fn parent_bucket_extracts_parent_name() {
        use crate::util::parent_bucket;
        assert_eq!(parent_bucket("生活费.交通"), Some("生活费"));
        assert_eq!(parent_bucket("数码"), None);
        assert_eq!(parent_bucket("A.B.C"), Some("A.B"));
    }

    #[test]
    fn nested_yaml_flattens_to_dotted_names() {
        use crate::config::load_budget_directives;
        let tmp_yaml = make_temp_file_with_ext(
            "\"2026-06\":\n  生活费:\n    交通: 1500\n    饮食: 2500\n  数码: 5000\n",
            "yaml",
        );
        let directives = load_budget_directives(&tmp_yaml).expect("parse nested yaml");
        fs::remove_file(tmp_yaml).ok();

        let buckets: BTreeSet<String> = directives.iter().map(|d| d.bucket.clone()).collect();
        assert!(buckets.contains("生活费.交通"));
        assert!(buckets.contains("生活费.饮食"));
        assert!(buckets.contains("数码"));
        assert!(!buckets.contains("生活费"));
    }

    #[test]
    fn summarize_hierarchy_aggregates_children_into_parent() {
        use crate::{budget::summarize_buckets, cli::ReportScope};
        let directives = vec![
            BudgetDirective {
                month: "2026-06".into(),
                label: None,
                source_key: "2026-06".into(),
                bucket: "生活费.交通".into(),
                amount: dec!(1500),
            },
            BudgetDirective {
                month: "2026-06".into(),
                label: None,
                source_key: "2026-06".into(),
                bucket: "生活费.饮食".into(),
                amount: dec!(2500),
            },
        ];
        let flows = vec![];
        let mappings = BudgetMappings {
            defaults: BTreeMap::new(),
            default_expense_bucket: "生活费".into(),
            bucket_types: BTreeMap::new(),
            asset_bucket_accounts: BTreeMap::new(),
        };
        let summaries = summarize_buckets(&directives, &flows, "2026-06", ReportScope::Month, &mappings);

        assert_eq!(summaries["生活费.交通"].planned, dec!(1500));
        assert_eq!(summaries["生活费.饮食"].planned, dec!(2500));
        assert_eq!(summaries["生活费"].planned, dec!(4000));
    }

    #[test]
    fn scoped_bucket_data_includes_children_for_parent() {
        use crate::{
            budget::build_scoped_bucket_data,
            cli::{ReportConfig, ReportScope, BucketView},
            config::BudgetMappings,
        };
        let config = ReportConfig {
            scope: ReportScope::Month,
            month: "2026-06".into(),
            filter: None,
            hide_asset_flows: false,
            bucket_view: BucketView::Summary,
            show_locations: false,
            sort_by: None,
            csv_pivot: false,
            bucket: None,
            strict: false,
        };
        let directives = vec![
            BudgetDirective {
                month: "2026-06".into(),
                label: None,
                source_key: "2026-06".into(),
                bucket: "生活费.交通".into(),
                amount: dec!(1500),
            },
            BudgetDirective {
                month: "2026-06".into(),
                label: None,
                source_key: "2026-06".into(),
                bucket: "生活费.饮食".into(),
                amount: dec!(2500),
            },
        ];
        let mappings = BudgetMappings {
            defaults: BTreeMap::new(),
            default_expense_bucket: "生活费".into(),
            bucket_types: BTreeMap::new(),
            asset_bucket_accounts: BTreeMap::new(),
        };
        let flows = vec![];

        let data = build_scoped_bucket_data(&config, "生活费", &mappings, &directives, &flows);
        assert_eq!(data.planned, dec!(4000));
        assert_eq!(data.directives.len(), 2);

        let data = build_scoped_bucket_data(&config, "生活费.交通", &mappings, &directives, &flows);
        assert_eq!(data.planned, dec!(1500));
        assert_eq!(data.directives.len(), 1);
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

    fn make_temp_file_with_ext(content: &str, ext: &str) -> PathBuf {
        let mut path = std::env::temp_dir();
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("clock")
            .as_nanos();
        path.push(format!("budget_report_test_{}.{}", nonce, ext));
        fs::write(&path, content).expect("write temp file");
        path
    }
}

//! 报告渲染与导出模块。
//!
//! 提供终端文本报告和 Markdown/CSV 文件导出的渲染逻辑。
//! 包括汇总报告、单桶明细报告、资产位置报告等。

use std::{
    collections::{BTreeMap, BTreeSet},
    fmt::Write as FmtWrite,
    fs,
    path::Path,
};

use anyhow::{Context, Result};
use rust_decimal::Decimal;

use crate::cli::{BucketView, Cli, ReportScope};
use crate::config::{BucketKind, BudgetDirective, BudgetMappings};
use crate::budget::{self, BucketSummary, BucketTxFlow, ScopedBucketData, WarningStats};
use crate::util::{fmt_decimal, format_tx_title, is_month_in_scope, sanitize_filename, shorten_account_label};

// ---------------------------------------------------------------------------
// 终端文本报告
// ---------------------------------------------------------------------------

/// 渲染汇总报告的终端文本格式。
///
/// 包含桶名、预算、实际、结余、状态五列及总计行。
pub fn render_summary_report_text(
    month: &str,
    scope: ReportScope,
    currency: &str,
    summaries: &BTreeMap<String, BucketSummary>,
    warnings: &WarningStats,
) -> String {
    let mut out = String::new();
    let _ = writeln!(
        out,
        "Budget Report ({}) [{}] scope={} ",
        month,
        currency,
        scope.label()
    );
    let _ = writeln!(
        out,
        "{:<24} {:>14} {:>14} {:>14} {:>10}",
        "Bucket", "Planned", "Actual", "Remain", "Status"
    );
    let _ = writeln!(out, "{}", "-".repeat(82));

    let mut total_planned = Decimal::ZERO;
    let mut total_actual = Decimal::ZERO;
    for (bucket, summary) in summaries {
        let remain = summary.planned - summary.actual;
        let status = if remain.is_sign_negative() {
            "OVER"
        } else {
            "OK"
        };
        total_planned += summary.planned;
        total_actual += summary.actual;

        let _ = writeln!(
            out,
            "{:<24} {:>14} {:>14} {:>14} {:>10}",
            bucket,
            fmt_decimal(summary.planned),
            fmt_decimal(summary.actual),
            fmt_decimal(remain),
            status
        );
    }

    let _ = writeln!(out, "{}", "-".repeat(82));
    let total_remain = total_planned - total_actual;
    let total_status = if total_remain.is_sign_negative() {
        "OVER"
    } else {
        "OK"
    };
    let _ = writeln!(
        out,
        "{:<24} {:>14} {:>14} {:>14} {:>10}",
        "TOTAL",
        fmt_decimal(total_planned),
        fmt_decimal(total_actual),
        fmt_decimal(total_remain),
        total_status
    );

    if !warnings.unknown_bucket_amount.is_zero() {
        let names = warnings
            .unknown_bucket_names
            .iter()
            .cloned()
            .collect::<Vec<_>>()
            .join(", ");
        let _ = writeln!(
            out,
            "WARNING: unknown buckets amount = {} {} (buckets: {})",
            fmt_decimal(warnings.unknown_bucket_amount),
            currency,
            names
        );
    }

    out
}

/// 渲染单桶报告的终端文本格式。
///
/// 根据 `bucket_view` 显示汇总统计、分月视图或明细历史。
/// 资产桶在 Detail/Monthly 视图下自动附带资产位置。
pub fn render_bucket_report_text(
    data: &ScopedBucketData,
    cli: &Cli,
    currency: &str,
    bucket_view: BucketView,
    show_locations: bool,
    all_flows: &[BucketTxFlow],
) -> String {
    let mut out = String::new();
    let _ = writeln!(out, "Bucket: {}", data.bucket);
    let _ = writeln!(out, "Type: {}", data.kind.label());
    let _ = writeln!(
        out,
        "Scope: {} (target month: {})",
        cli.scope.label(),
        cli.month
    );
    let _ = writeln!(out, "Planned: {} {}", fmt_decimal(data.planned), currency);
    let _ = writeln!(out, "Actual:  {} {}", fmt_decimal(data.actual), currency);
    let _ = writeln!(out, "Remain:  {} {}", fmt_decimal(data.remain), currency);

    match bucket_view {
        BucketView::Summary => {}
        BucketView::Monthly => append_bucket_monthly_view(
            &mut out,
            &data.bucket,
            data.kind,
            &cli.month,
            cli.scope,
            currency,
            &data.directives,
            &data.flows,
        ),
        BucketView::Detail => {
            append_bucket_detail_view(
                &mut out,
                currency,
                &data.bucket,
                data.kind,
                &cli.month,
                &data.directives,
                &data.flows,
                all_flows,
                true,
            )
        }
    }

    if data.kind == BucketKind::Asset {
        // Summary: 仅当 --show-locations 时显示
        // Monthly/Detail: 自动显示（Detail 已在明细函数内部处理）
        let show_here = match bucket_view {
            BucketView::Summary => show_locations,
            BucketView::Monthly => true,
            BucketView::Detail => false,
        };
        if show_here {
            let locations = budget::collect_asset_locations(&data.bucket, &cli.month, all_flows);
            append_asset_locations_view(&mut out, &cli.month, currency, &locations);
        }
    }

    out
}

/// 追加某个桶的分月统计视图到输出缓冲区。
///
/// 消费桶显示"预算收入/支出/结余"，资产桶显示"预算存入目标/实际存入/差额"。
pub fn append_bucket_monthly_view(
    out: &mut String,
    bucket: &str,
    bucket_kind: BucketKind,
    target_month: &str,
    scope: ReportScope,
    currency: &str,
    directives: &[BudgetDirective],
    flows: &[BucketTxFlow],
) {
    let mut per_month: BTreeMap<String, BucketSummary> = BTreeMap::new();

    for item in directives {
        if !is_month_in_scope(&item.month, target_month, scope) {
            continue;
        }
        per_month.entry(item.month.clone()).or_default().planned += item.amount;
    }

    for flow in flows {
        if !is_month_in_scope(&flow.month, target_month, scope) {
            continue;
        }
        per_month.entry(flow.month.clone()).or_default().actual += flow.actual_amount();
    }

    let _ = writeln!(out, "\n{} 分月视图:", bucket);
    for (month, summary) in per_month {
        let remain = summary.planned - summary.actual;
        match bucket_kind {
            BucketKind::Expense => {
                let _ = writeln!(
                    out,
                    "{}：预算收入 {} {}，支出 {} {}，结余 {} {}",
                    month,
                    fmt_decimal(summary.planned),
                    currency,
                    fmt_decimal(summary.actual),
                    currency,
                    fmt_decimal(remain),
                    currency
                );
            }
            BucketKind::Asset => {
                let _ = writeln!(
                    out,
                    "{}：预算存入目标 {} {}，实际存入 {} {}，差额 {} {}",
                    month,
                    fmt_decimal(summary.planned),
                    currency,
                    fmt_decimal(summary.actual),
                    currency,
                    fmt_decimal(remain),
                    currency
                );
            }
        }
    }
}

/// 追加某个桶的交易明细视图到输出缓冲区。
///
/// 按月份分组，每个月份下先列出预算收入指令，再列出实际交易流水。
/// 消费桶交易标注"支出"，资产桶根据正负标注"存入/转出"。
/// 资产桶末尾自动附带资产位置汇总。
pub fn append_bucket_detail_view(
    out: &mut String,
    currency: &str,
    bucket: &str,
    bucket_kind: BucketKind,
    target_month: &str,
    directives: &[BudgetDirective],
    flows: &[BucketTxFlow],
    all_flows: &[BucketTxFlow],
    show_locations_in_detail: bool,
) {
    let _ = writeln!(out, "\n历史明细:");

    let mut months = BTreeSet::new();
    for item in directives {
        months.insert(item.month.clone());
    }
    for flow in flows {
        months.insert(flow.month.clone());
    }

    for month in months {
        let month_budgets = directives
            .iter()
            .filter(|item| item.month == month)
            .collect::<Vec<_>>();
        for item in month_budgets {
            if let Some(label) = item.label.as_ref() {
                let _ = writeln!(
                    out,
                    "{} {}：预算收入 {} {}",
                    item.month,
                    label,
                    fmt_decimal(item.amount),
                    currency
                );
            } else {
                let _ = writeln!(
                    out,
                    "{}：预算收入 {} {}",
                    item.month,
                    fmt_decimal(item.amount),
                    currency
                );
            }
        }

        let mut month_flows = flows
            .iter()
            .filter(|flow| flow.month == month)
            .collect::<Vec<_>>();
        month_flows.sort_by_key(|flow| flow.date);

        for flow in month_flows {
            let action = match bucket_kind {
                BucketKind::Expense => "支出",
                BucketKind::Asset => {
                    if flow.flow.is_sign_positive() {
                        "存入"
                    } else {
                        "转出"
                    }
                }
            };
            let _ = writeln!(
                out,
                "{}：{} {} {} {}",
                flow.date.format("%Y-%m-%d"),
                format_tx_title(flow.payee.as_deref(), flow.narration.as_deref()),
                action,
                fmt_decimal(flow.flow),
                currency
            );
        }
    }

    if bucket_kind == BucketKind::Asset && show_locations_in_detail {
        let locations = budget::collect_asset_locations(bucket, target_month, all_flows);
        append_asset_locations_view(out, target_month, currency, &locations);
    }
}

/// 追加资产位置表格到输出缓冲区。
pub fn append_asset_locations_view(
    out: &mut String,
    target_month: &str,
    currency: &str,
    locations: &BTreeMap<String, Decimal>,
) {
    let _ = writeln!(out, "\n资产位置（截至 {}）:", target_month);
    if locations.is_empty() {
        let _ = writeln!(out, "(无资产位置数据)");
        return;
    }
    for (account, amount) in locations {
        let _ = writeln!(
            out,
            "{}: {} {}",
            shorten_account_label(account),
            fmt_decimal(*amount),
            currency
        );
    }
}

// ---------------------------------------------------------------------------
// Markdown / CSV 导出
// ---------------------------------------------------------------------------

/// 将报告导出到指定目录，生成 Markdown、CSV 和纯文本文件。
pub fn export_reports(
    out_dir: &Path,
    cli: &Cli,
    currency: &str,
    mappings: &BudgetMappings,
    directives: &[BudgetDirective],
    flows: &[BucketTxFlow],
    summaries: &BTreeMap<String, BucketSummary>,
    warnings: &WarningStats,
) -> Result<()> {
    fs::create_dir_all(out_dir)
        .with_context(|| format!("Failed to create output dir: {}", out_dir.display()))?;

    // 汇总报告：Markdown + 纯文本
    let summary_txt =
        render_summary_report_text(&cli.month, cli.scope, currency, summaries, warnings);
    let summary_md = render_summary_markdown(&cli.month, cli.scope, currency, summaries, warnings);
    let summary_path = out_dir.join(format!("summary-{}-{}.md", cli.month, cli.scope.label()));
    fs::write(&summary_path, summary_md)
        .with_context(|| format!("Failed to write {}", summary_path.display()))?;

    let summary_console_path =
        out_dir.join(format!("summary-{}-{}.txt", cli.month, cli.scope.label()));
    fs::write(&summary_console_path, summary_txt)
        .with_context(|| format!("Failed to write {}", summary_console_path.display()))?;

    // CSV 汇总
    let csv_path = out_dir.join(format!("buckets-{}-{}.csv", cli.month, cli.scope.label()));
    fs::write(&csv_path, render_summary_csv(summaries))
        .with_context(|| format!("Failed to write {}", csv_path.display()))?;

    // 每个桶的 Markdown 报告
    let buckets = budget::collect_buckets_for_export(cli, directives, flows, summaries);
    for bucket in buckets {
        let data = budget::build_scoped_bucket_data(cli, &bucket, mappings, directives, flows);
        let report = render_bucket_markdown(&data, cli, currency, flows);
        let filename = format!(
            "bucket-{}-{}-{}.md",
            sanitize_filename(&bucket),
            cli.month,
            cli.scope.label()
        );
        let path = out_dir.join(filename);
        fs::write(&path, report).with_context(|| format!("Failed to write {}", path.display()))?;

        if data.kind == BucketKind::Asset {
            let locations = budget::collect_asset_locations(&data.bucket, &cli.month, flows);
            let location_report =
                render_asset_locations_markdown(&data.bucket, &cli.month, currency, &locations);
            let location_filename = format!(
                "asset-locations-{}-{}-{}.md",
                sanitize_filename(&bucket),
                cli.month,
                cli.scope.label()
            );
            let location_path = out_dir.join(location_filename);
            fs::write(&location_path, location_report)
                .with_context(|| format!("Failed to write {}", location_path.display()))?;
        }
    }

    Ok(())
}

/// 渲染汇总报告的 Markdown 格式。
pub fn render_summary_markdown(
    month: &str,
    scope: ReportScope,
    currency: &str,
    summaries: &BTreeMap<String, BucketSummary>,
    warnings: &WarningStats,
) -> String {
    let mut out = String::new();
    let _ = writeln!(out, "# 预算汇总报告");
    let _ = writeln!(out);
    let _ = writeln!(out, "- 月份: `{}`", month);
    let _ = writeln!(out, "- 统计范围: `{}`", scope.label());
    let _ = writeln!(out, "- 币种: `{}`", currency);
    let _ = writeln!(out);
    let _ = writeln!(out, "| 预算桶 | 预算 | 实际 | 结余 | 状态 |");
    let _ = writeln!(out, "|---|---:|---:|---:|---|");

    let mut total_planned = Decimal::ZERO;
    let mut total_actual = Decimal::ZERO;
    for (bucket, summary) in summaries {
        let remain = summary.planned - summary.actual;
        let status = if remain.is_sign_negative() {
            "OVER"
        } else {
            "OK"
        };
        total_planned += summary.planned;
        total_actual += summary.actual;
        let _ = writeln!(
            out,
            "| {} | {} | {} | {} | {} |",
            bucket,
            fmt_decimal(summary.planned),
            fmt_decimal(summary.actual),
            fmt_decimal(remain),
            status
        );
    }

    let total_remain = total_planned - total_actual;
    let total_status = if total_remain.is_sign_negative() {
        "OVER"
    } else {
        "OK"
    };
    let _ = writeln!(
        out,
        "| **TOTAL** | **{}** | **{}** | **{}** | **{}** |",
        fmt_decimal(total_planned),
        fmt_decimal(total_actual),
        fmt_decimal(total_remain),
        total_status
    );

    if !warnings.unknown_bucket_amount.is_zero() {
        let names = warnings
            .unknown_bucket_names
            .iter()
            .cloned()
            .collect::<Vec<_>>()
            .join(", ");
        let _ = writeln!(out);
        let _ = writeln!(
            out,
            "> WARNING: unknown buckets amount = {} {} (buckets: {})",
            fmt_decimal(warnings.unknown_bucket_amount),
            currency,
            names
        );
    }

    out
}

/// 渲染汇总报告的 CSV 格式。
pub fn render_summary_csv(summaries: &BTreeMap<String, BucketSummary>) -> String {
    let mut out = String::from("bucket,planned,actual,remain,status\n");
    for (bucket, summary) in summaries {
        let remain = summary.planned - summary.actual;
        let status = if remain.is_sign_negative() {
            "OVER"
        } else {
            "OK"
        };
        let _ = writeln!(
            out,
            "{},{},{},{},{}",
            bucket,
            fmt_decimal(summary.planned),
            fmt_decimal(summary.actual),
            fmt_decimal(remain),
            status
        );
    }
    out
}

/// 渲染单桶报告的 Markdown 格式。
///
/// 包含分月视图、交易明细以及资产桶的资产位置表格。
pub fn render_bucket_markdown(
    data: &ScopedBucketData,
    cli: &Cli,
    currency: &str,
    all_flows: &[BucketTxFlow],
) -> String {
    let mut out = String::new();
    let _ = writeln!(out, "# 预算桶报告：{}", data.bucket);
    let _ = writeln!(out);
    let _ = writeln!(out, "- 类型: `{}`", data.kind.label());
    let _ = writeln!(out, "- 目标月份: `{}`", cli.month);
    let _ = writeln!(out, "- 统计范围: `{}`", cli.scope.label());
    let _ = writeln!(out, "- 预算: `{}` {}", fmt_decimal(data.planned), currency);
    let _ = writeln!(out, "- 实际: `{}` {}", fmt_decimal(data.actual), currency);
    let _ = writeln!(out, "- 结余: `{}` {}", fmt_decimal(data.remain), currency);
    let _ = writeln!(out);

    append_bucket_monthly_view(
        &mut out,
        &data.bucket,
        data.kind,
        &cli.month,
        cli.scope,
        currency,
        &data.directives,
        &data.flows,
    );
    let _ = writeln!(out);
    append_bucket_detail_view(
        &mut out,
        currency,
        &data.bucket,
        data.kind,
        &cli.month,
        &data.directives,
        &data.flows,
        all_flows,
        false,
    );

    if data.kind == BucketKind::Asset {
        let locations = budget::collect_asset_locations(&data.bucket, &cli.month, all_flows);
        let _ = writeln!(out);
        let _ = writeln!(out, "## 资产位置");
        let _ = writeln!(out);
        if locations.is_empty() {
            let _ = writeln!(out, "(无资产位置数据)");
        } else {
            let _ = writeln!(out, "| 账户 | 金额 |");
            let _ = writeln!(out, "|---|---:|");
            for (account, amount) in &locations {
                let _ = writeln!(
                    out,
                    "| {} | {} |",
                    shorten_account_label(account),
                    fmt_decimal(*amount)
                );
            }
        }
    }

    out
}

/// 渲染资产位置报告的 Markdown 格式。
pub fn render_asset_locations_markdown(
    bucket: &str,
    target_month: &str,
    currency: &str,
    locations: &BTreeMap<String, Decimal>,
) -> String {
    let mut out = String::new();
    let _ = writeln!(out, "# 资产位置报告：{}", bucket);
    let _ = writeln!(out);
    let _ = writeln!(out, "- 截至月份: `{}`", target_month);
    let _ = writeln!(out, "- 币种: `{}`", currency);
    let _ = writeln!(out);
    let _ = writeln!(out, "| 账户 | 金额 |");
    let _ = writeln!(out, "|---|---:|");

    if locations.is_empty() {
        let _ = writeln!(out, "| (无资产位置数据) | 0.00 |");
        return out;
    }

    for (account, amount) in locations {
        let _ = writeln!(
            out,
            "| {} | {} |",
            shorten_account_label(account),
            fmt_decimal(*amount)
        );
    }
    out
}

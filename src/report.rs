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

use crate::cli::{BucketView, DateRange, ReportConfig, ReportScope};
use crate::config::{BucketKind, BudgetDirective, BudgetMappings};
use crate::budget::{self, BucketSummary, BucketTxFlow, ScopedBucketData, WarningStats};
use crate::util::{fmt_decimal, format_tx_title, is_month_in_scope, parent_bucket, sanitize_filename, shorten_account_label};

// ---------------------------------------------------------------------------
// 终端文本报告
// ---------------------------------------------------------------------------

/// 渲染汇总报告的终端文本格式。
///
/// 包含桶名、月预算、已支出、使用率、结余、状态六列及总计行。
/// 支持 --sort-by name|planned|actual|remain 排序。
/// 若某桶的父桶已存在于汇总表中，则跳过子桶以避免重复计算。
pub fn render_summary_report_text(
    range: &DateRange,
    currency: &str,
    summaries: &BTreeMap<String, BucketSummary>,
    warnings: &WarningStats,
    sort_by: Option<&str>,
    expand: bool,
) -> String {
    let mut out = String::new();
    let _ = writeln!(out, "预算报告 ({}) [{}]", range.display(), currency);
    let _ = writeln!(out, "{:<20} {:>12} {:>12} {:>7} {:>12} {:>7}", "预算桶", "月预算", "已支出", "使用率", "结余", "状态");
    let _ = writeln!(out, "{}", "-".repeat(76));

    let mut entries = if expand {
        summaries.iter().collect()
    } else {
        filter_top_level(summaries.iter(), summaries)
    };
    sort_entries(&mut entries, sort_by);

    let mut total_planned = Decimal::ZERO;
    let mut total_actual = Decimal::ZERO;
    for (bucket, summary) in &entries {
        let remain = summary.planned - summary.actual;
        let status = if remain.is_sign_negative() { "超支" } else { "正常" };
        total_planned += summary.planned;
        total_actual += summary.actual;
        let _ = writeln!(out, "{:<20} {:>12} {:>12} {:>7} {:>12} {:>7}",
            bucket, fmt_decimal(summary.planned), fmt_decimal(summary.actual),
            fmt_pct(summary.actual, summary.planned), fmt_decimal(remain), status);
    }

    let _ = writeln!(out, "{}", "-".repeat(76));
    let total_remain = total_planned - total_actual;
    let total_status = if total_remain.is_sign_negative() { "超支" } else { "正常" };
    let _ = writeln!(out, "{:<20} {:>12} {:>12} {:>7} {:>12} {:>7}",
        "合计", fmt_decimal(total_planned), fmt_decimal(total_actual),
        fmt_pct(total_actual, total_planned), fmt_decimal(total_remain), total_status);

    if !warnings.unknown_bucket_amount.is_zero() {
        let names = warnings
            .unknown_bucket_names
            .iter()
            .cloned()
            .collect::<Vec<_>>()
            .join(", ");
        let _ = writeln!(
            out,
            "警告: unknown buckets amount = {} {} (buckets: {})",
            fmt_decimal(warnings.unknown_bucket_amount),
            currency,
            names
        );
    }

    out
}

/// 渲染同比/环比对比报告。
pub fn render_compare_report_text(
    cur_range: &DateRange,
    cur_summaries: &BTreeMap<String, BucketSummary>,
    _cur_warnings: &WarningStats,
    cmp_range: &DateRange,
    cmp_summaries: &BTreeMap<String, BucketSummary>,
    _cmp_warnings: &WarningStats,
    currency: &str,
    sort_by: Option<&str>,
) -> String {
    let mut out = String::new();
    let all_buckets: BTreeSet<&String> = cur_summaries.keys()
        .chain(cmp_summaries.keys())
        .filter(|bucket| {
            if let Some(parent) = parent_bucket(bucket) {
                !(cur_summaries.contains_key(parent) || cmp_summaries.contains_key(parent))
            } else { true }
        })
        .collect();

    let mut entries: Vec<(&String, &BucketSummary, &BucketSummary)> = all_buckets.iter()
        .map(|bucket| {
            let cur = cur_summaries.get(*bucket).unwrap_or(&EMPTY_SUMMARY);
            let cmp = cmp_summaries.get(*bucket).unwrap_or(&EMPTY_SUMMARY);
            (*bucket, cur, cmp)
        })
        .collect();

    match sort_by.unwrap_or("name") {
        "planned" => entries.sort_by(|a, b| b.1.planned.partial_cmp(&a.1.planned).unwrap_or(std::cmp::Ordering::Equal)),
        "actual" => entries.sort_by(|a, b| b.1.actual.partial_cmp(&a.1.actual).unwrap_or(std::cmp::Ordering::Equal)),
        "remain" => entries.sort_by(|a, b| {
            (a.1.planned - a.1.actual).partial_cmp(&(b.1.planned - b.1.actual)).unwrap_or(std::cmp::Ordering::Equal)
        }),
        _ => {}
    }

    let _ = writeln!(out, "预算对比报告");
    let _ = writeln!(out, "本期: {}  对比: {}  [{}]", cur_range.display(), cmp_range.display(), currency);
    let _ = writeln!(
        out,
        "{:<16} {:>10} {:>10} {:>10} | {:>10} {:>10} {:>10}",
        "预算桶", "月预算", "已支出", "结余", "月预算", "已支出", "结余"
    );
    let _ = writeln!(out, "{}", "-".repeat(74));

    for (bucket, cur, cmp) in &entries {
        let cur_remain = cur.planned - cur.actual;
        let cmp_remain = cmp.planned - cmp.actual;
        let _ = writeln!(
            out,
            "{:<16} {:>10} {:>10} {:>10} | {:>10} {:>10} {:>10}",
            bucket,
            fmt_decimal(cur.planned), fmt_decimal(cur.actual), fmt_decimal(cur_remain),
            fmt_decimal(cmp.planned), fmt_decimal(cmp.actual), fmt_decimal(cmp_remain),
        );
    }

    out
}

/// 空汇总，用于对比时缺失桶的兜底。
static EMPTY_SUMMARY: BucketSummary = BucketSummary { planned: Decimal::ZERO, actual: Decimal::ZERO };

// ---- 共享工具 ----

/// 格式化使用率百分比。
fn fmt_pct(actual: Decimal, planned: Decimal) -> String {
    if planned.is_zero() {
        "  --".to_string()
    } else {
        format!("{:5.1}%", (actual / planned * Decimal::from(100u32)).round_dp(1))
    }
}

/// 按指定字段排序桶条目列表。
fn sort_entries<'a>(
    entries: &mut [(&'a String, &'a BucketSummary)],
    sort_by: Option<&str>,
) {
    match sort_by.unwrap_or("name") {
        "planned" => entries.sort_by(|a, b| b.1.planned.partial_cmp(&a.1.planned).unwrap_or(std::cmp::Ordering::Equal)),
        "actual" => entries.sort_by(|a, b| b.1.actual.partial_cmp(&a.1.actual).unwrap_or(std::cmp::Ordering::Equal)),
        "remain" => entries.sort_by(|a, b| {
            (a.1.planned - a.1.actual).partial_cmp(&(b.1.planned - b.1.actual)).unwrap_or(std::cmp::Ordering::Equal)
        }),
        _ => {}
    }
}

/// 过滤掉已有父桶存在于 summaries 中的子桶（避免重复计算）。
fn filter_top_level<'a>(
    entries: impl IntoIterator<Item = (&'a String, &'a BucketSummary)>,
    summaries: &BTreeMap<String, BucketSummary>,
) -> Vec<(&'a String, &'a BucketSummary)> {
    entries
        .into_iter()
        .filter(|(bucket, _)| {
            if let Some(parent) = parent_bucket(bucket) {
                !summaries.contains_key(parent)
            } else { true }
        })
        .collect()
}

/// 渲染单桶报告的终端文本格式。
///
/// 根据 `bucket_view` 显示汇总统计、分月视图或明细历史。
/// 资产桶在 Detail/Monthly 视图下自动附带资产位置。
pub fn render_bucket_report_text(
    data: &ScopedBucketData,
    config: &ReportConfig,
    currency: &str,
    bucket_view: BucketView,
    show_locations: bool,
    all_flows: &[BucketTxFlow],
    range: &DateRange,
) -> String {
    let mut out = String::new();
    let _ = writeln!(out, "预算桶: {}", data.bucket);
    let _ = writeln!(out, "类型: {}", data.kind.label());
    let _ = writeln!(
        out,
        "统计区间: {}",
        range.display(),
    );
    let _ = writeln!(out, "月预算: {} {}", fmt_decimal(data.planned), currency);
    let _ = writeln!(out, "已支出: {} {}", fmt_decimal(data.actual), currency);
    let _ = writeln!(out, "结余:   {} {}", fmt_decimal(data.remain), currency);
    let _ = writeln!(
        out,
        "状态: {}",
        if data.remain.is_sign_negative() { "超支" } else { "正常" }
    );

    match bucket_view {
        BucketView::Summary => {}
        BucketView::Monthly => append_bucket_monthly_view(
            &mut out,
            &data.bucket,
            data.kind,
            range.end_month(),
            config.scope,
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
                range.end_month(),
                &data.directives,
                &data.flows,
                all_flows,
                true,
                data.remain,
                config.scope,
            )
        }
    }

    if data.kind == BucketKind::Asset || all_flows.iter().any(|f| f.bucket == data.bucket && !f.location_deltas.is_empty()) {
        let show_here = match bucket_view {
            BucketView::Summary => show_locations,
            BucketView::Monthly => true,
            BucketView::Detail => false,
        };
        if show_here {
            let locations = budget::collect_asset_locations(&data.bucket, range.end_month(), all_flows, config.scope);
            append_asset_locations_view(&mut out, range.end_month(), currency, &locations, data.remain);
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
    remain: Decimal,
    scope: ReportScope,
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
            // 当查询父桶（如 生活费）时，子桶指令（如 生活费.交通）的前缀追加桶名标签
            let child_label = if item.bucket != bucket {
                &item.bucket[bucket.len() + 1..]
            } else {
                ""
            };
            let child_tag = if child_label.is_empty() {
                String::new()
            } else {
                format!(" [{}]", child_label)
            };
            if let Some(label) = item.label.as_ref() {
                let _ = writeln!(
                    out,
                    "{} {}：预算收入{} {} {}",
                    item.month,
                    label,
                    child_tag,
                    fmt_decimal(item.amount),
                    currency
                );
            } else {
                let _ = writeln!(
                    out,
                    "{}：预算收入{} {} {}",
                    item.month,
                    child_tag,
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
            let action = match flow.kind {
                BucketKind::Expense => "支出",
                BucketKind::Asset => {
                    if flow.flow.is_sign_positive() {
                        "存入"
                    } else {
                        "转出"
                    }
                }
            };
            let child_tag = if flow.bucket != bucket {
                format!(" [{}]", &flow.bucket[bucket.len() + 1..])
            } else {
                String::new()
            };
            let _ = writeln!(
                out,
                "{}：{}{} {} {} {}",
                flow.date.format("%Y-%m-%d"),
                format_tx_title(flow.payee.as_deref(), flow.narration.as_deref()),
                child_tag,
                action,
                fmt_decimal(flow.flow),
                currency
            );
        }
    }

    if (bucket_kind == BucketKind::Asset || all_flows.iter().any(|f| f.bucket == bucket && !f.location_deltas.is_empty()))
        && show_locations_in_detail
    {
        let locations = budget::collect_asset_locations(bucket, target_month, all_flows, scope);
        append_asset_locations_view(out, target_month, currency, &locations, remain);
    }
}

/// 追加资产位置表格到输出缓冲区。
/// 正数归入「持仓/已分配」，负数归入「超支/出金来源」。
/// 超支时隐藏「持仓/已分配」段。
pub fn append_asset_locations_view(
    out: &mut String,
    target_month: &str,
    currency: &str,
    locations: &BTreeMap<String, Decimal>,
    remain: Decimal,
) {
    if locations.is_empty() { return; }

    let holdings: Vec<_> = locations.iter().filter(|(_, v)| v.is_sign_positive()).collect();
    let sources: Vec<_> = locations.iter().filter(|(_, v)| v.is_sign_negative()).collect();

    if !holdings.is_empty() && !remain.is_sign_negative() {
        let _ = writeln!(out, "\n持仓/已分配（截至 {}）:", target_month);
        for (account, amount) in &holdings {
            let _ = writeln!(out, "{}: {} {}", shorten_account_label(account), fmt_decimal(**amount), currency);
        }
    }

    if !sources.is_empty() {
        let _ = writeln!(out, "\n超支/出金来源（截至 {}）:", target_month);
        for (account, amount) in &sources {
            let _ = writeln!(out, "{}: {} {}", shorten_account_label(account), fmt_decimal(**amount), currency);
        }
    }

    if holdings.is_empty() && sources.is_empty() {
        let _ = writeln!(out, "\n资产位置（截至 {}）:", target_month);
        let _ = writeln!(out, "(无资产位置数据)");
    }
}

// ---------------------------------------------------------------------------
// Markdown / CSV 导出
// ---------------------------------------------------------------------------

/// 将报告导出到指定目录，生成 Markdown、CSV 和纯文本文件。
pub fn export_reports(
    out_dir: &Path,
    config: &ReportConfig,
    currency: &str,
    mappings: &BudgetMappings,
    directives: &[BudgetDirective],
    flows: &[BucketTxFlow],
    summaries: &BTreeMap<String, BucketSummary>,
    warnings: &WarningStats,
    range: &DateRange,
) -> Result<()> {
    fs::create_dir_all(out_dir)
        .with_context(|| format!("Failed to create output dir: {}", out_dir.display()))?;

    let scope_label = range.label();

    // 汇总报告：Markdown + 纯文本
    let summary_txt =
        render_summary_report_text(range, currency, summaries, warnings, None, config.expand);
    let summary_md = render_summary_markdown(range, currency, summaries, warnings, config.sort_by.as_deref(), config.expand);
    let summary_path = out_dir.join(format!("summary-{}.md", scope_label));
    fs::write(&summary_path, summary_md)
        .with_context(|| format!("Failed to write {}", summary_path.display()))?;

    let summary_console_path =
        out_dir.join(format!("summary-{}.txt", scope_label));
    fs::write(&summary_console_path, summary_txt)
        .with_context(|| format!("Failed to write {}", summary_console_path.display()))?;

    // CSV 汇总
    let csv_path = out_dir.join(format!("buckets-{}.csv", scope_label));
    fs::write(&csv_path, render_summary_csv(summaries))
        .with_context(|| format!("Failed to write {}", csv_path.display()))?;

    // Pivot CSV（横向月度透视）
    if config.csv_pivot {
        let pivot_path = out_dir.join(format!("pivot-{}.csv", scope_label));
        fs::write(&pivot_path, render_pivot_csv(flows, range))
            .with_context(|| format!("Failed to write {}", pivot_path.display()))?;
    }

    // 每个桶的 Markdown 报告
    let buckets = budget::collect_buckets_for_export(config, directives, flows, summaries);
    for bucket in buckets {
        let data = budget::build_scoped_bucket_data(config, &bucket, mappings, directives, flows);
        let report = render_bucket_markdown(&data, config, currency, flows, range);
        let filename = format!("bucket-{}-{}.md", sanitize_filename(&bucket), scope_label);
        let path = out_dir.join(filename);
        fs::write(&path, report).with_context(|| format!("Failed to write {}", path.display()))?;

        if data.kind == BucketKind::Asset || data.flows.iter().any(|f| !f.location_deltas.is_empty()) {
            let locations = budget::collect_asset_locations(&data.bucket, range.end_month(), flows, config.scope);
            let location_report =
                render_asset_locations_markdown(&data.bucket, range.end_month(), currency, &locations);
            let location_filename = format!(
                "asset-locations-{}-{}.md",
                sanitize_filename(&bucket),
                scope_label
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
    range: &DateRange,
    currency: &str,
    summaries: &BTreeMap<String, BucketSummary>,
    warnings: &WarningStats,
    sort_by: Option<&str>,
    expand: bool,
) -> String {
    let mut out = String::new();
    let _ = writeln!(out, "# 预算汇总报告");
    let _ = writeln!(out);
    let _ = writeln!(out, "- 统计区间: `{}`", range.display());
    let _ = writeln!(out, "- 币种: `{}`", currency);
    let _ = writeln!(out);
    let _ = writeln!(out, "| 预算桶 | 月预算 | 已支出 | 使用率 | 结余 | 状态 |");
    let _ = writeln!(out, "|---:|---:|---:|---:|---:|---|");

    let mut entries = if expand {
        summaries.iter().collect()
    } else {
        filter_top_level(summaries.iter(), summaries)
    };
    sort_entries(&mut entries, sort_by);

    let mut total_planned = Decimal::ZERO;
    let mut total_actual = Decimal::ZERO;
    for (bucket, summary) in &entries {
        let remain = summary.planned - summary.actual;
        let status = if remain.is_sign_negative() { "超支" } else { "正常" };
        total_planned += summary.planned;
        total_actual += summary.actual;
        let _ = writeln!(out, "| {} | {} | {} | {} | {} | {} |",
            bucket, fmt_decimal(summary.planned), fmt_decimal(summary.actual),
            fmt_pct(summary.actual, summary.planned), fmt_decimal(remain), status);
    }

    let total_remain = total_planned - total_actual;
    let total_status = if total_remain.is_sign_negative() { "超支" } else { "正常" };
    let _ = writeln!(out, "| **合计** | **{}** | **{}** | **{}** | **{}** | **{}** |",
        fmt_decimal(total_planned), fmt_decimal(total_actual),
        fmt_pct(total_actual, total_planned), fmt_decimal(total_remain), total_status);

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
            "> 警告: unknown buckets amount = {} {} (buckets: {})",
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
        let status = if remain.is_sign_negative() { "OVER" } else { "OK" };
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

/// 渲染月度横向透视 CSV：行为月份，列为预算桶，值为 actual。
fn render_pivot_csv(flows: &[BucketTxFlow], range: &DateRange) -> String {
    let mut per_month: BTreeMap<String, BTreeMap<String, Decimal>> = BTreeMap::new();
    let mut all_buckets = BTreeSet::new();

    for flow in flows {
        if !range.contains(&flow.month) {
            continue;
        }
        let amount = flow.actual_amount();
        if amount.is_zero() {
            continue;
        }
        all_buckets.insert(flow.bucket.clone());
        *per_month
            .entry(flow.month.clone())
            .or_default()
            .entry(flow.bucket.clone())
            .or_default() += amount;
    }

    let bucket_list: Vec<String> = all_buckets.into_iter().collect();
    let mut out = String::from("月份");
    for b in &bucket_list {
        let _ = write!(out, ",{}", b);
    }
    let _ = writeln!(out);

    for (month, bucket_amounts) in &per_month {
        let _ = write!(out, "{}", month);
        for b in &bucket_list {
            let _ = write!(out, ",{}", fmt_decimal(*bucket_amounts.get(b).unwrap_or(&Decimal::ZERO)));
        }
        let _ = writeln!(out);
    }

    out
}

/// 渲染单桶报告的 Markdown 格式。
///
/// 包含分月视图、交易明细以及资产桶的资产位置表格。
pub fn render_bucket_markdown(
    data: &ScopedBucketData,
    config: &ReportConfig,
    currency: &str,
    all_flows: &[BucketTxFlow],
    range: &DateRange,
) -> String {
    let mut out = String::new();
    let _ = writeln!(out, "# 预算桶报告：{}", data.bucket);
    let _ = writeln!(out);
    let _ = writeln!(out, "- 类型: `{}`", data.kind.label());
    let _ = writeln!(out, "- 统计区间: `{}`", range.display());
    let _ = writeln!(out, "- 月预算: `{}` {}", fmt_decimal(data.planned), currency);
    let _ = writeln!(out, "- 已支出: `{}` {}", fmt_decimal(data.actual), currency);
    let _ = writeln!(out, "- 结余: `{}` {}", fmt_decimal(data.remain), currency);
    let _ = writeln!(out, "- 状态: `{}`", if data.remain.is_sign_negative() { "超支" } else { "正常" });
    let _ = writeln!(out);

    append_bucket_monthly_view(
        &mut out,
        &data.bucket,
        data.kind,
        range.end_month(),
        config.scope,
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
        range.end_month(),
        &data.directives,
        &data.flows,
        all_flows,
        false,
        data.remain,
        config.scope,
    );

    if data.kind == BucketKind::Asset || data.flows.iter().any(|f| !f.location_deltas.is_empty()) {
        let locations = budget::collect_asset_locations(&data.bucket, range.end_month(), all_flows, config.scope);
        let _ = writeln!(out);
        let _ = writeln!(out, "## 资产位置");
        let _ = writeln!(out);
        if locations.is_empty() {
            let _ = writeln!(out, "(无资产位置数据)");
        } else {
            let holdings: Vec<_> = locations.iter().filter(|(_, v)| v.is_sign_positive()).collect();
            let negatives: Vec<_> = locations.iter().filter(|(_, v)| v.is_sign_negative()).collect();
            if !holdings.is_empty() {
                let _ = writeln!(out, "**持仓/已分配**");
                let _ = writeln!(out);
                let _ = writeln!(out, "| 账户 | 金额 |");
                let _ = writeln!(out, "|---|---:|");
                for (account, amount) in &holdings {
                    let _ = writeln!(out, "| {} | {} |", shorten_account_label(account), fmt_decimal(**amount));
                }
                if !negatives.is_empty() { let _ = writeln!(out); }
            }
            if !negatives.is_empty() {
                let _ = writeln!(out, "**超支/出金来源**");
                let _ = writeln!(out);
                let _ = writeln!(out, "| 账户 | 金额 |");
                let _ = writeln!(out, "|---|---:|");
                for (account, amount) in &negatives {
                    let _ = writeln!(out, "| {} | {} |", shorten_account_label(account), fmt_decimal(**amount));
                }
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

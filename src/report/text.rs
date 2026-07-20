//! 终端文本报告渲染。

use std::{
    collections::{BTreeMap, BTreeSet},
    fmt::Write as FmtWrite,
};

use rust_decimal::Decimal;

use crate::cli::{BucketView, DateRange, ReportConfig};
use crate::util::ReportScope;
use crate::config::{BucketKind, BudgetDirective};
use crate::budget::{self, BucketSummary, BucketTxFlow, ScopedBucketData, WarningStats};
use crate::util::{fmt_decimal, format_tx_title, is_month_in_scope, parent_bucket, shorten_account_label};

use super::shared::{fmt_pct, sort_entries, filter_top_level};
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
        filter_top_level(summaries.iter().collect(), summaries)
    };
    sort_entries(&mut entries, sort_by);

    let mut total_planned = Decimal::ZERO;
    let mut total_actual = Decimal::ZERO;
    for (bucket, summary) in &entries {
        // 跟踪桶 (planned=0) 不显示在汇总表中
        if summary.planned.is_zero() {
            continue;
        }
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
            &range.end_month(),
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
                &range.end_month(),
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
            let locations = budget::collect_asset_locations(&data.bucket, &range.end_month(), all_flows, config.scope);
            append_asset_locations_view(&mut out, &range.end_month(), currency, &locations, data.remain);
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
/// 每年末尾插入年度小结（本年合计 + 累计合计）。
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

    let income_label = match bucket_kind {
        BucketKind::Expense => "预算收入",
        BucketKind::Asset => "存入",
    };
    let expense_label = match bucket_kind {
        BucketKind::Expense => "支出",
        BucketKind::Asset => "转出",
    };

    let mut year_income = Decimal::ZERO;
    let mut year_expense = Decimal::ZERO;
    let mut cumulative_income = Decimal::ZERO;
    let mut cumulative_expense = Decimal::ZERO;
    let mut current_year = String::new();

    for month in &months {
        let year = &month[..4];

        // 年份切换时打印上一年小结
        if year != current_year {
            if !current_year.is_empty() {
                append_year_summary(out, &current_year, currency,
                    year_income, year_expense, cumulative_income, cumulative_expense,
                    income_label, expense_label);
            }
            current_year = year.to_string();
            year_income = Decimal::ZERO;
            year_expense = Decimal::ZERO;
        }

        let month_budgets = directives
            .iter()
            .filter(|item| item.month == *month)
            .collect::<Vec<_>>();
        for item in month_budgets {
            if item.amount.is_zero() && item.bucket == bucket {
                continue;
            }
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
            year_income += item.amount;
            cumulative_income += item.amount;
            if let Some(label) = item.label.as_ref() {
                let _ = writeln!(
                    out,
                    "{} {}：{}{} {} {}",
                    item.month,
                    label,
                    income_label,
                    child_tag,
                    fmt_decimal(item.amount),
                    currency
                );
            } else {
                let _ = writeln!(
                    out,
                    "{}：{}{} {} {}",
                    item.month,
                    income_label,
                    child_tag,
                    fmt_decimal(item.amount),
                    currency
                );
            }
        }

        let mut month_flows = flows
            .iter()
            .filter(|flow| flow.month == *month)
            .collect::<Vec<_>>();
        month_flows.sort_by_key(|flow| flow.date);

        for flow in month_flows {
            let actual = flow.actual_amount();
            year_expense += actual;
            cumulative_expense += actual;
            let action = match flow.kind {
                BucketKind::Expense => {
                    if flow.flow.is_sign_negative() { "支出" } else { "入账" }
                }
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

    // 最后一年小结
    if !current_year.is_empty() {
        append_year_summary(out, &current_year, currency,
            year_income, year_expense, cumulative_income, cumulative_expense,
            income_label, expense_label);
    }

    if (bucket_kind == BucketKind::Asset || all_flows.iter().any(|f| f.bucket == bucket && !f.location_deltas.is_empty()))
        && show_locations_in_detail
    {
        let locations = budget::collect_asset_locations(bucket, target_month, all_flows, scope);
        append_asset_locations_view(out, target_month, currency, &locations, remain);
    }
}

/// 打印年度小结。
fn append_year_summary(
    out: &mut String,
    year: &str,
    currency: &str,
    year_income: Decimal,
    year_expense: Decimal,
    cumulative_income: Decimal,
    cumulative_expense: Decimal,
    income_label: &str,
    expense_label: &str,
) {
    let _ = writeln!(out);
    let _ = writeln!(out, "========== {} 年小结 ==========", year);
    let _ = writeln!(out, "{} 本年合计: {} {}", income_label, fmt_decimal(year_income), currency);
    let _ = writeln!(out, "{} 累计合计: {} {}", income_label, fmt_decimal(cumulative_income), currency);
    let _ = writeln!(out, "{} 本年合计: {} {}", expense_label, fmt_decimal(year_expense), currency);
    let _ = writeln!(out, "{} 累计合计: {} {}", expense_label, fmt_decimal(cumulative_expense), currency);
    let _ = writeln!(out, "==============================");
    let _ = writeln!(out);
}

/// 追加资产位置表格到输出缓冲区。
/// 正数为资金存放账户，负数为支出来源账户。
/// 差额为已实际消费的金额（钱已花掉，不再存在于任何资产账户中）。
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

    let holdings_total: Decimal = holdings.iter().map(|(_, v)| **v).sum();
    let sources_total: Decimal = sources.iter().map(|(_, v)| **v).sum();

    // 资金存放 — 该桶计划预算当前存在哪些账户
    if !holdings.is_empty() && !remain.is_sign_negative() {
        let _ = writeln!(out, "\n资金存放（截至 {}）:", target_month);
        for (account, amount) in &holdings {
            let _ = writeln!(out, "{}: {} {}", shorten_account_label(account), fmt_decimal(**amount), currency);
        }
    }

    // 支出来源 — 该桶实际支出从哪些账户转出
    if !sources.is_empty() {
        let _ = writeln!(out, "\n支出来源（截至 {}）:", target_month);
        for (account, amount) in &sources {
            let _ = writeln!(out, "{}: {} {}", shorten_account_label(account), fmt_decimal(**amount), currency);
        }
    }

    // 已支出 = 支出来源 − 资金存放，即钱花到哪去了
    let spent = sources_total + holdings_total; // sources 为负数
    if !spent.is_zero() && !holdings.is_empty() && !sources.is_empty() {
        let _ = writeln!(out, "\n已支出（截至 {}）: {} {} ← 资金存放与支出来源的差额", target_month, fmt_decimal(-spent), currency);
    }

    if holdings.is_empty() && sources.is_empty() {
        let _ = writeln!(out, "\n资产位置（截至 {}）:", target_month);
        let _ = writeln!(out, "(无资产位置数据)");
    }
}

// ---------------------------------------------------------------------------
// Markdown / CSV 导出
// ---------------------------------------------------------------------------


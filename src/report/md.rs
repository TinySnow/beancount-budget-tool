//! Markdown 格式报告渲染。

use std::{
    collections::BTreeMap,
    fmt::Write as FmtWrite,
};

use rust_decimal::Decimal;

use crate::cli::{DateRange, ReportConfig};
use crate::config::BucketKind;
use crate::budget::{self, BucketSummary, BucketTxFlow, ScopedBucketData, WarningStats};
use crate::util::{fmt_decimal, shorten_account_label};

use super::shared::{fmt_pct, sort_entries, filter_top_level};
use super::text::{append_bucket_monthly_view, append_bucket_detail_view};
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
        &range.end_month(),
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
        &range.end_month(),
        &data.directives,
        &data.flows,
        all_flows,
        false,
        data.remain,
        config.scope,
    );

    if data.kind == BucketKind::Asset || data.flows.iter().any(|f| !f.location_deltas.is_empty()) {
        let locations = budget::collect_asset_locations(&data.bucket, &range.end_month(), all_flows, config.scope);
        let _ = writeln!(out);
        let _ = writeln!(out, "## 资产位置");
        let _ = writeln!(out);
        if locations.is_empty() {
            let _ = writeln!(out, "(无资产位置数据)");
        } else {
            let holdings: Vec<_> = locations.iter().filter(|(_, v)| v.is_sign_positive()).collect();
            let negatives: Vec<_> = locations.iter().filter(|(_, v)| v.is_sign_negative()).collect();
            if !holdings.is_empty() {
                let _ = writeln!(out, "**资金存放**");
                let _ = writeln!(out);
                let _ = writeln!(out, "| 账户 | 金额 |");
                let _ = writeln!(out, "|---|---:|");
                for (account, amount) in &holdings {
                    let _ = writeln!(out, "| {} | {} |", shorten_account_label(account), fmt_decimal(**amount));
                }
                if !negatives.is_empty() { let _ = writeln!(out); }
            }
            if !negatives.is_empty() {
                let _ = writeln!(out, "**支出来源**");
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

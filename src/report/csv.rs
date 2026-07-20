//! CSV 格式报告导出。

use std::{
    collections::{BTreeMap, BTreeSet},
    fmt::Write as FmtWrite,
};

use rust_decimal::Decimal;

use crate::cli::DateRange;
use crate::budget::{BucketSummary, BucketTxFlow};
use crate::util::fmt_decimal;
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
pub fn render_pivot_csv(flows: &[BucketTxFlow], range: &DateRange) -> String {
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

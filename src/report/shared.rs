//! 共享工具函数与导出逻辑。

use std::{
    collections::{BTreeMap, BTreeSet},
    fs,
    path::Path,
};

use anyhow::{Context, Result};
use rust_decimal::Decimal;

use crate::cli::{DateRange, ReportConfig};
use crate::config::{BucketKind, BudgetDirective, BudgetMappings};
use crate::budget::{self, BucketSummary, BucketTxFlow};
use crate::util::{parent_bucket, sanitize_filename};

use super::text;
use super::md;
use super::csv;
use super::json;

pub(crate) fn fmt_pct(actual: Decimal, planned: Decimal) -> String {
    if planned.is_zero() {
        "  --".to_string()
    } else {
        format!("{:5.1}%", (actual / planned * Decimal::from(100u32)).round_dp(1))
    }
}

pub(crate) fn sort_entries<'a>(
    entries: &mut Vec<(&'a String, &'a BucketSummary)>,
    sort_by: Option<&str>,
) {
    match sort_by {
        Some("planned") => entries.sort_by(|a, b| b.1.planned.cmp(&a.1.planned)),
        Some("actual")  => entries.sort_by(|a, b| b.1.actual.cmp(&a.1.actual)),
        Some("remain")  => entries.sort_by(|a, b| {
            let ra = a.1.planned - a.1.actual;
            let rb = b.1.planned - b.1.actual;
            rb.cmp(&ra)
        }),
        _ => entries.sort_by(|a, b| a.0.cmp(b.0)),
    }
}

pub(crate) fn filter_top_level<'a>(
    all_entries: Vec<(&'a String, &'a BucketSummary)>,
    summaries: &'a BTreeMap<String, BucketSummary>,
) -> Vec<(&'a String, &'a BucketSummary)> {
    let parent_keys: BTreeSet<&String> = summaries.keys().filter(|k| {
        all_entries.iter().any(|(bucket, _)| parent_bucket(bucket) == Some(k.as_str()))
    }).collect();
    all_entries.into_iter().filter(|(bucket, _)| !parent_keys.contains(bucket)).collect()
}

pub fn export_reports(
    out_dir: &Path,
    config: &ReportConfig,
    currency: &str,
    mappings: &BudgetMappings,
    directives: &[BudgetDirective],
    flows: &[BucketTxFlow],
    summaries: &BTreeMap<String, BucketSummary>,
    warnings: &crate::budget::WarningStats,
    range: &DateRange,
) -> Result<()> {
    fs::create_dir_all(out_dir)
        .with_context(|| format!("Failed to create output dir: {}", out_dir.display()))?;

    let scope_label = range.label();

    let summary_txt =
        text::render_summary_report_text(range, currency, summaries, warnings, None, config.expand);
    let summary_md = md::render_summary_markdown(range, currency, summaries, warnings, config.sort_by.as_deref(), config.expand);
    let summary_path = out_dir.join(format!("summary-{}.md", scope_label));
    fs::write(&summary_path, summary_md)
        .with_context(|| format!("Failed to write {}", summary_path.display()))?;

    let summary_console_path =
        out_dir.join(format!("summary-{}.txt", scope_label));
    fs::write(&summary_console_path, summary_txt)
        .with_context(|| format!("Failed to write {}", summary_console_path.display()))?;

    let csv_path = out_dir.join(format!("buckets-{}.csv", scope_label));
    fs::write(&csv_path, csv::render_summary_csv(summaries))
        .with_context(|| format!("Failed to write {}", csv_path.display()))?;

    if config.csv_pivot {
        let pivot_path = out_dir.join(format!("pivot-{}.csv", scope_label));
        fs::write(&pivot_path, csv::render_pivot_csv(flows, range))
            .with_context(|| format!("Failed to write {}", pivot_path.display()))?;
    }

    if config.out_json {
        let json_path = out_dir.join(format!("summary-{}.json", scope_label));
        fs::write(&json_path, json::render_summary_json(summaries, range, currency))
            .with_context(|| format!("Failed to write {}", json_path.display()))?;
    }

    let buckets = budget::collect_buckets_for_export(config, directives, flows, summaries);
    for bucket in buckets {
        let data = budget::build_scoped_bucket_data(config, &bucket, mappings, directives, flows);
        let report = md::render_bucket_markdown(&data, config, currency, flows, range);
        let filename = format!("bucket-{}-{}.md", sanitize_filename(&bucket), scope_label);
        let path = out_dir.join(filename);
        fs::write(&path, report).with_context(|| format!("Failed to write {}", path.display()))?;

        if data.kind == BucketKind::Asset || data.flows.iter().any(|f| !f.location_deltas.is_empty()) {
            let locations = budget::collect_asset_locations(&data.bucket, &range.end_month(), flows, config.scope);
            let location_report =
                md::render_asset_locations_markdown(&data.bucket, &range.end_month(), currency, &locations);
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

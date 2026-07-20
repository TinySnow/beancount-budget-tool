//! JSON 格式报告导出。

use std::{
    collections::BTreeMap,
    fmt::Write as FmtWrite,
};


use crate::cli::DateRange;
use crate::budget::BucketSummary;

pub fn render_summary_json(
    summaries: &BTreeMap<String, BucketSummary>,
    range: &DateRange,
    currency: &str,
) -> String {
    let mut out = String::from("{\n");
    let _ = writeln!(out, "  \"range\": \"{}\",", range.display());
    let _ = writeln!(out, "  \"currency\": \"{}\",", currency);
    let _ = writeln!(out, "  \"buckets\": [");
    let entries: Vec<_> = summaries.iter().collect();
    for (i, (bucket, summary)) in entries.iter().enumerate() {
        let comma = if i + 1 < entries.len() { "," } else { "" };
        let remain = summary.planned - summary.actual;
        let _ = writeln!(out, "    {{\"name\":\"{}\",\"planned\":{},\"actual\":{},\"remain\":{},\"status\":\"{}\"}}{}",
            bucket,
            summary.planned.round_dp(2),
            summary.actual.round_dp(2),
            remain.round_dp(2),
            if remain.is_sign_negative() { "超支" } else { "正常" },
            comma,
        );
    }
    let _ = writeln!(out, "  ]");
    let _ = writeln!(out, "}}");
    out
}

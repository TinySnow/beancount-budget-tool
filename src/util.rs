//! 工具函数与基础类型模块。

use chrono::{Datelike, NaiveDate};
use rust_decimal::Decimal;
use clap::ValueEnum;

/// 预算统计范围。
///
/// - `Month`: 仅统计目标月份的数据。
/// - `Cumulative`: 统计从最早月份到目标月份的累计数据。
#[derive(Debug, Clone, Copy, PartialEq, Eq, ValueEnum)]
pub enum ReportScope {
    Month,
    Cumulative,
}

impl ReportScope {
    pub fn label(self) -> &'static str {
        match self {
            Self::Month => "month",
            Self::Cumulative => "cumulative",
        }
    }
}

/// 去除 UTF-8 BOM 前缀。
pub fn strip_bom(content: &str) -> &str {
    content.strip_prefix('\u{feff}').unwrap_or(content)
}

/// 默认的生活费预算桶名称。
///
/// 当账本交易没有显式标注 `budget` metadata 且无法通过账户前缀映射到
/// 特定预算桶时，该交易的消费金额将自动归入此桶。
pub fn default_expense_bucket() -> String {
    "生活费".to_string()
}

/// 从点号分隔的桶名中提取父桶名称。
///
/// `"生活费.交通"` → `Some("生活费")`
/// `"数码"` → `None`
pub fn parent_bucket(bucket: &str) -> Option<&str> {
    bucket.rfind('.').map(|pos| &bucket[..pos])
}

/// 校验月份字符串格式为 `YYYY-MM` 且为合法日期。
///
/// 例如 `"2026-06"` 校验通过，`"2026-13"` 或 `"2026-06-01"` 将返回错误。
pub fn validate_month(raw: &str) -> anyhow::Result<()> {
    use anyhow::{anyhow, bail, Context};

    let mut parts = raw.split('-');
    let year = parts
        .next()
        .ok_or_else(|| anyhow!("Invalid month '{}'", raw))?;
    let month = parts
        .next()
        .ok_or_else(|| anyhow!("Invalid month '{}'", raw))?;
    if parts.next().is_some() {
        bail!("Invalid month '{}', expected YYYY-MM", raw);
    }

    let year: i32 = year
        .parse()
        .with_context(|| format!("Invalid year in month '{}'", raw))?;
    let month: u32 = month
        .parse()
        .with_context(|| format!("Invalid month number in '{}'", raw))?;
    if NaiveDate::from_ymd_opt(year, month, 1).is_none() {
        bail!("Invalid month '{}', expected YYYY-MM", raw);
    }
    Ok(())
}

/// 将 `NaiveDate` 转换为 `YYYY-MM` 格式的月份字符串。
pub fn month_of_date(date: NaiveDate) -> String {
    format!("{:04}-{:02}", date.year(), date.month())
}

/// 判断账本中的货币代码是否与目标币种匹配。
///
/// 若无显式货币代码，默认视为目标币种（兼容未标注币种的旧账本）。
/// 比较时忽略大小写。
pub fn is_target_currency(posting_currency: Option<&str>, target_currency: &str) -> bool {
    posting_currency
        .unwrap_or(target_currency)
        .to_ascii_uppercase()
        == target_currency
}

/// 判断给定月份是否在目标月份与范围的统计范围内。
///
/// - `Month`: 仅当 `month == target_month`。
/// - `Cumulative`: 当 `month <= target_month`（解析为数值比较）。
pub fn is_month_in_scope(month: &str, target_month: &str, scope: ReportScope) -> bool {
    match scope {
        ReportScope::Month => month == target_month,
        ReportScope::Cumulative => {
            let parse = |s: &str| -> Option<(i32, u32)> {
                let y: i32 = s[..4].parse().ok()?;
                let m: u32 = s[5..].parse().ok()?;
                Some((y, m))
            };
            match (parse(month), parse(target_month)) {
                (Some(a), Some(b)) => a <= b,
                _ => month <= target_month,
            }
        }
    }
}

/// 将 `Decimal` 格式化为保留两位小数的字符串。
///
/// 内部先做 `round_dp(2)` 确保精度一致。
pub fn fmt_decimal(v: Decimal) -> String {
    format!("{:.2}", v.round_dp(2))
}

/// 计算字符串在终端中的显示宽度（中文字符占 2 列）。
pub fn display_width(s: &str) -> usize {
    unicode_width::UnicodeWidthStr::width(s)
}

/// 将字符串填充到指定显示宽度（不足补空格，超过截断）。
pub fn pad_display(s: &str, width: usize, left_align: bool) -> String {
    let dw = display_width(s);
    if dw >= width {
        s.to_string()
    } else if left_align {
        format!("{}{}", s, " ".repeat(width - dw))
    } else {
        format!("{}{}", " ".repeat(width - dw), s)
    }
}

/// 格式化交易标题，组合 `payee` 和 `narration`。
///
/// 两者都用双引号包裹，若无则显示 `"(无标题)"`。
pub fn format_tx_title(payee: Option<&str>, narration: Option<&str>) -> String {
    match (payee, narration) {
        (Some(p), Some(n)) => format!("\"{}\" \"{}\"", p, n),
        (Some(p), None) => format!("\"{}\"", p),
        (None, Some(n)) => format!("\"{}\"", n),
        (None, None) => "\"(无标题)\"".to_string(),
    }
}

/// 缩短账户名称，仅保留最后两级以便在窄宽度终端中显示。
///
/// 例如 `Assets:Bank:中国:建设银行` 将被缩短为 `中国:建设银行`。
pub fn shorten_account_label(account: &str) -> String {
    let parts = account.split(':').collect::<Vec<_>>();
    if parts.len() >= 2 {
        let tail = &parts[parts.len() - 2..];
        return tail.join(":");
    }
    account.to_string()
}

/// 对文件名中的不安全字符做替换，防止写入失败。
pub fn sanitize_filename(raw: &str) -> String {
    raw.chars()
        .map(|c| match c {
            '/' | '\\' | ':' | '*' | '?' | '"' | '<' | '>' | '|' => '_',
            _ => c,
        })
        .collect::<String>()
}

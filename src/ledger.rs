//! Beancount 账本解析模块。
//!
//! 提供基于正则表达式的 Beancount 账本解析器，支持：
//! - 交易头（日期、payee、narration）
//! - Metadata 行（键值对，用于 `budget` 标签等）
//! - 过账行（账户名、金额、币种）
//!
//! 注意：这是一个简化的解析器，不支持 include、balance、price 等高级指令。

use std::{collections::HashMap, fs, path::Path, str::FromStr};

use anyhow::{Context, Result};
use chrono::NaiveDate;
use once_cell::sync::Lazy;
use regex::Regex;
use rust_decimal::Decimal;

/// 交易头正则：`YYYY-MM-DD * ...` 或 `YYYY-MM-DD ! ...`
static TX_HEADER_RE: Lazy<Regex> = Lazy::new(|| {
    Regex::new(r"^(?P<date>\d{4}-\d{2}-\d{2})\s+[*!](?:\s|$)").expect("valid tx header regex")
});

/// 引号文本提取正则，用于提取交易标题中的 payee / narration。
static QUOTED_TEXT_RE: Lazy<Regex> =
    Lazy::new(|| Regex::new(r#"\"((?:\\.|[^\"\\])*)\""#).expect("valid quoted text regex"));

/// Metadata 行正则：`  key: value`
///
/// 要求 key 必须是 `[A-Za-z_][A-Za-z0-9_]*` 的形式，
/// key 后必须有 `: `（冒号+空格），避免将 `Expenses:Food  10 CNY` 误判为 metadata。
static TX_METADATA_RE: Lazy<Regex> = Lazy::new(|| {
    Regex::new(r"^\s{2}(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*:\s+(?P<value>.+?)\s*$")
        .expect("valid tx metadata regex")
});

/// 过账行正则（金额可选）：
/// `  Expenses:Food  10 CNY`
/// `  Income:Misc`
///
/// 要求账户名后至少两个空格才识别金额，兼容无金额的过账行。
static POSTING_RE: Lazy<Regex> = Lazy::new(|| {
    Regex::new(
        r"^\s{2}(?:[*!]\s+)?(?P<account>\S+)(?:\s{2,}(?P<number>[+-]?\d+(?:\.\d+)?)(?:\s+(?P<currency>[A-Za-z0-9_.-]+))?)?",
    )
    .expect("valid posting regex")
});

// ---------------------------------------------------------------------------
// 数据结构
// ---------------------------------------------------------------------------

/// 一笔解析后的 Beancount 交易。
#[derive(Debug)]
pub struct LedgerTransaction {
    /// 交易日期
    pub date: NaiveDate,
    /// 收款人/商户名
    pub payee: Option<String>,
    /// 交易叙述
    pub narration: Option<String>,
    /// Metadata 键值对（含 budget 等自定义标签）
    pub metadata: HashMap<String, String>,
    /// 过账行列表
    pub postings: Vec<LedgerPosting>,
}

/// 单条过账行。
#[derive(Debug)]
pub struct LedgerPosting {
    /// Beancount 账户全路径（如 `Expenses:Food:Groceries`）
    pub account: String,
    /// 金额（可能为 None，如无金额的 balance 类过账）
    pub amount: Option<Decimal>,
    /// 币种代码（如 `CNY`、`USD`），None 时视为目标币种
    pub currency: Option<String>,
}

// ---------------------------------------------------------------------------
// 解析函数
// ---------------------------------------------------------------------------

/// 读取并解析单个账本文件。
///
/// 自动处理 UTF-8 BOM 头部。内部委托给 `parse_ledger_content`。
pub fn parse_ledger_file(path: &Path) -> Result<Vec<LedgerTransaction>> {
    let content = fs::read_to_string(path)?;
    parse_ledger_content(&content)
}

/// 逐行解析 Beancount 文本内容为交易列表。
///
/// 使用状态机模式：遇到新交易头时完成上一笔交易，逐行累积 metadata 和过账行。
/// 空行或文件结束也会触发当前交易的完工。
pub fn parse_ledger_content(content: &str) -> Result<Vec<LedgerTransaction>> {
    let content = content.strip_prefix('\u{feff}').unwrap_or(content);

    /// 内部构建器，逐行累积交易数据。
    #[derive(Debug)]
    struct Builder {
        date: NaiveDate,
        payee: Option<String>,
        narration: Option<String>,
        metadata: HashMap<String, String>,
        postings: Vec<LedgerPosting>,
    }

    impl Builder {
        fn finish(self) -> LedgerTransaction {
            LedgerTransaction {
                date: self.date,
                payee: self.payee,
                narration: self.narration,
                metadata: self.metadata,
                postings: self.postings,
            }
        }
    }

    let mut transactions = Vec::new();
    let mut current: Option<Builder> = None;

    for raw_line in content.lines() {
        let line = raw_line.trim_end();

        // 空行分隔交易
        if line.trim().is_empty() {
            if let Some(done) = current.take() {
                transactions.push(done.finish());
            }
            continue;
        }

        // 交易头：开始新一笔交易
        if let Some(header) = TX_HEADER_RE.captures(line) {
            if let Some(done) = current.take() {
                transactions.push(done.finish());
            }

            let date = NaiveDate::parse_from_str(&header["date"], "%Y-%m-%d")
                .with_context(|| format!("Invalid transaction date '{}'", &header["date"]))?;
            let (payee, narration) = parse_tx_title(line);

            current = Some(Builder {
                date,
                payee,
                narration,
                metadata: HashMap::new(),
                postings: Vec::new(),
            });
            continue;
        }

        // 当前无活跃交易时跳过孤立行
        let Some(builder) = current.as_mut() else {
            continue;
        };

        // Metadata 行
        if let Some(meta) = TX_METADATA_RE.captures(line) {
            let key = meta["key"].to_string();
            let value = parse_metadata_value(&meta["value"]);
            builder.metadata.insert(key, value);
            continue;
        }

        // 过账行
        if let Some(posting) = POSTING_RE.captures(line) {
            let account = posting["account"].to_string();
            let amount = posting
                .name("number")
                .and_then(|raw| Decimal::from_str(raw.as_str()).ok());
            let currency = posting
                .name("currency")
                .map(|raw| raw.as_str().trim().to_string())
                .filter(|value| !value.is_empty());

            builder.postings.push(LedgerPosting {
                account,
                amount,
                currency,
            });
            continue;
        }
    }

    // 文件末尾未完成交易
    if let Some(done) = current.take() {
        transactions.push(done.finish());
    }

    Ok(transactions)
}

/// 从交易头行提取引号内的 payee 和 narration。
///
/// 第一个引号字符串为 payee，第二个为 narration。
/// 支持 `\"` 和 `\\` 转义。
fn parse_tx_title(line: &str) -> (Option<String>, Option<String>) {
    let mut quoted = QUOTED_TEXT_RE
        .captures_iter(line)
        .filter_map(|cap| cap.get(1).map(|m| unescape_quoted_text(m.as_str())))
        .collect::<Vec<_>>();

    match quoted.len() {
        0 => (None, None),
        1 => (Some(quoted.remove(0)), None),
        _ => (Some(quoted.remove(0)), Some(quoted.remove(0))),
    }
}

/// 反解析引号字符串中的转义序列。
fn unescape_quoted_text(raw: &str) -> String {
    raw.replace("\\\"", "\"").replace("\\\\", "\\")
}

/// 解析 metadata 行中的值，支持双引号、单引号和无引号三种形式。
pub fn parse_metadata_value(raw: &str) -> String {
    let trimmed = raw.trim();

    if let Some(unquoted) = trimmed
        .strip_prefix('"')
        .and_then(|value| value.strip_suffix('"'))
    {
        return unquoted
            .replace("\\\"", "\"")
            .replace("\\\\", "\\")
            .trim()
            .to_string();
    }

    if let Some(unquoted) = trimmed
        .strip_prefix('\'')
        .and_then(|value| value.strip_suffix('\''))
    {
        return unquoted.trim().to_string();
    }

    trimmed.to_string()
}

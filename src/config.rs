//! 预算配置模块。
//!
//! 负责加载和解析 `budgets.yaml` 与 `mappings.yaml` 配置文件，
//! 定义预算桶类型、账户映射等核心配置数据结构。

use std::{
    collections::{BTreeMap, BTreeSet},
    fs,
    path::Path,
};

use anyhow::{Context, Result, anyhow};
use once_cell::sync::Lazy;
use regex::Regex;
use rust_decimal::Decimal;
use serde::Deserialize;

use crate::util::{default_expense_bucket, validate_month};

/// 预算 key 解析正则：`YYYY-MM` 或 `YYYY-MM 任意标签`
static BUDGET_KEY_RE: Lazy<Regex> = Lazy::new(|| {
    Regex::new(r"^(?P<month>\d{4}-\d{2})(?:\s+(?P<label>\S.*))?$").expect("valid budget key regex")
});

// ---------------------------------------------------------------------------
// 数据类型
// ---------------------------------------------------------------------------

/// 预算桶的类型：支出类或资产类。
///
/// - `Expense`: 支出桶，预算用于控制消费。
/// - `Asset`: 资产桶，预算用于追踪储蓄/投资等资产转移。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum BucketKind {
    Expense,
    Asset,
}

impl BucketKind {
    /// 返回桶类型的短标签。
    pub fn label(self) -> &'static str {
        match self {
            Self::Expense => "expense",
            Self::Asset => "asset",
        }
    }
}

/// 预算映射配置，对应 `mappings.yaml` 文件内容。
///
/// 包含账户前缀到预算桶的映射、桶类型定义、资产桶账户前缀等信息。
#[derive(Debug, Deserialize, Default)]
pub struct BudgetMappings {
    /// account_prefix -> budget_bucket
    #[serde(default)]
    pub defaults: BTreeMap<String, String>,

    /// 未标注预算桶的消费类分录默认归属桶名。
    #[serde(default = "default_expense_bucket")]
    pub default_expense_bucket: String,

    /// bucket -> kind(expense/asset)
    #[serde(default)]
    pub bucket_types: BTreeMap<String, BucketKind>,

    /// bucket -> [account_prefix, ...]（可选）
    ///
    /// 用于精确定位资产类预算桶资金归属。
    /// 若不配置，系统默认将资产桶交易中的正向资产腿视作"流入位置"。
    #[serde(default)]
    pub asset_bucket_accounts: BTreeMap<String, Vec<String>>,
}

impl BudgetMappings {
    /// 查询指定预算桶的类型，若未在 `bucket_types` 中配置则默认为 `Expense`。
    pub fn bucket_kind(&self, bucket: &str) -> BucketKind {
        self.bucket_types
            .get(bucket)
            .copied()
            .unwrap_or(BucketKind::Expense)
    }

    /// 获取指定资产桶显式配置的账户前缀列表。
    pub fn configured_asset_prefixes(&self, bucket: &str) -> Option<&[String]> {
        self.asset_bucket_accounts.get(bucket).map(Vec::as_slice)
    }
}

/// 单条预算指令，来自 `budgets.yaml` 中某个 bucket 的金额配置。
#[derive(Debug, Clone)]
pub struct BudgetDirective {
    /// 所属月份（YYYY-MM）
    pub month: String,
    /// 附加标签（如 "绩效"），`None` 表示常规月预算
    pub label: Option<String>,
    /// YAML 原始 key，用于排序
    pub source_key: String,
    /// 预算桶名称
    pub bucket: String,
    /// 预算金额
    pub amount: Decimal,
}

// ---------------------------------------------------------------------------
// 配置加载函数
// ---------------------------------------------------------------------------

/// 从 `budgets.yaml` 加载所有预算指令。
///
/// 解析格式：外层 `BTreeMap<String, BTreeMap<String, Decimal>>`，
/// 其中外层 key 为 `YYYY-MM` 或 `YYYY-MM 标签`，内层 key 为桶名。
pub fn load_budget_directives(path: &Path) -> Result<Vec<BudgetDirective>> {
    let content = fs::read_to_string(path)?;
    let content = content.strip_prefix('\u{feff}').unwrap_or(&content);
    let raw: BTreeMap<String, BTreeMap<String, Decimal>> =
        serde_yaml::from_str(content).context("Invalid budgets YAML")?;

    let mut directives = Vec::new();
    for (raw_key, bucket_map) in raw {
        let (month, label) = parse_budget_key(&raw_key)?;
        for (bucket, amount) in bucket_map {
            directives.push(BudgetDirective {
                month: month.clone(),
                label: label.clone(),
                source_key: raw_key.clone(),
                bucket,
                amount,
            });
        }
    }

    directives.sort_by(|a, b| {
        a.month
            .cmp(&b.month)
            .then(a.source_key.cmp(&b.source_key))
            .then(a.bucket.cmp(&b.bucket))
    });
    Ok(directives)
}

/// 解析预算 YAML 中的 key，提取月份和可选标签。
///
/// 示例：
/// - `"2026-06"` → `("2026-06", None)`
/// - `"2026-06 绩效"` → `("2026-06", Some("绩效"))`
pub fn parse_budget_key(raw: &str) -> Result<(String, Option<String>)> {
    let trimmed = raw.trim();
    let cap = BUDGET_KEY_RE.captures(trimmed).ok_or_else(|| {
        anyhow!(
            "Invalid budget key '{}', expected 'YYYY-MM' or 'YYYY-MM <label>'",
            raw
        )
    })?;

    let month = cap["month"].to_string();
    validate_month(&month)?;

    let label = cap
        .name("label")
        .map(|m| m.as_str().trim().to_string())
        .filter(|s| !s.is_empty());

    Ok((month, label))
}

/// 从 `mappings.yaml` 加载预算映射配置。
pub fn load_mappings(path: &Path) -> Result<BudgetMappings> {
    let content = fs::read_to_string(path)?;
    let content = content.strip_prefix('\u{feff}').unwrap_or(&content);
    serde_yaml::from_str(content).context("Invalid mappings YAML")
}

/// 收集所有已知的预算桶名称集合。
///
/// 来源包括：预算指令中的桶名、mappings 中声明的 bucket_types 键、
/// 以及默认生活费桶名。
pub fn collect_known_buckets(
    directives: &[BudgetDirective],
    mappings: &BudgetMappings,
) -> BTreeSet<String> {
    let mut buckets = BTreeSet::new();

    for item in directives {
        buckets.insert(item.bucket.clone());
    }
    for bucket in mappings.bucket_types.keys() {
        buckets.insert(bucket.clone());
    }

    buckets.insert(mappings.default_expense_bucket.clone());
    buckets
}

/// 根据账本账户前缀查询对应的预算桶名称。
///
/// 采用最长前缀匹配策略：配置中 prefix 越长的映射优先级越高，
/// 避免 `Expenses:Consume` 错误地吞掉 `Expenses:Consume:电子` 等更具体的子路径。
pub fn resolve_bucket_by_account(mappings: &BudgetMappings, account: &str) -> Option<String> {
    mappings
        .defaults
        .iter()
        .filter(|(prefix, _)| account.starts_with(prefix.as_str()))
        .max_by_key(|(prefix, _)| prefix.len())
        .map(|(_, bucket)| bucket.clone())
}

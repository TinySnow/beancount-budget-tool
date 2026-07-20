//! 预算配置模块。
//!
//! 负责加载和解析 `budgets.yaml` 与 `mappings.yaml` 配置文件，
//! 定义预算桶类型、账户映射等核心配置数据结构。
//!
//! `budgets.yaml` 支持 YAML 嵌套层级来表达预算桶的父子关系，例如：
//! ```yaml
//! "2026-06":
//!   生活费:
//!     交通: 1500
//!     饮食: 2500
//!   数码: 5000
//! ```
//! 加载后 `交通` 和 `饮食` 将展平为 `生活费.交通`、`生活费.饮食` 的全路径桶名，
//! 父桶 `生活费` 的统计值自动由子桶聚合得出。

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

use crate::util::{default_expense_bucket, strip_bom, validate_month};

/// 预算 key 解析正则：`YYYY-MM` / `YYYY.M.D` / `YYYY/M/D` 等格式，加可选标签
static BUDGET_KEY_RE: Lazy<Regex> = Lazy::new(|| {
    Regex::new(r"^(?P<year>\d{4})[-./](?P<month>\d{1,2})(?:[-./](?P<day>\d{1,2}))?(?:\s+(?P<label>\S.*))?$").expect("valid budget key regex")
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

    /// 跟踪桶列表：仅追踪实际支出，不参与 plan 计算、不计入合计、不聚合到父桶。
    #[serde(default)]
    pub tracking_buckets: Vec<String>,
}

impl BudgetMappings {
    /// 查询指定预算桶的类型。
    ///
    /// 优先级：`bucket_types` 显式声明 > `asset_bucket_accounts` 中出现 > 默认 Expense。
    pub fn bucket_kind(&self, bucket: &str) -> BucketKind {
        if let Some(k) = self.bucket_types.get(bucket) {
            return *k;
        }
        if self.asset_bucket_accounts.contains_key(bucket) {
            return BucketKind::Asset;
        }
        BucketKind::Expense
    }

    /// 获取指定资产桶显式配置的账户前缀列表。
    pub fn configured_asset_prefixes(&self, bucket: &str) -> Option<&[String]> {
        self.asset_bucket_accounts.get(bucket).map(Vec::as_slice)
    }

    /// 判断指定桶是否为跟踪桶（仅追踪实际支出，不参与 plan 计算）。
    pub fn is_tracking_bucket(&self, bucket: &str) -> bool {
        self.tracking_buckets.iter().any(|b| b == bucket || bucket.starts_with(&format!("{}.", b)))
    }
}

/// 单条预算指令，来自 `budgets.yaml` 中某个 bucket 的金额配置。
///
/// 嵌套的 YAML 会在加载时展平：父桶以点号路径命名（如 `生活费.交通`），
/// 父桶 `生活费` 本身不会生成指令，其统计值由子桶聚合得出。
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
// YAML 嵌套值类型与展平逻辑
// ---------------------------------------------------------------------------

/// YAML 嵌套预算值：叶节点为金额，分支节点为子映射。
///
/// 通过 `#[serde(untagged)]` 自动识别 YAML 中值是 Decimal 还是嵌套 Map。
#[derive(Debug, Deserialize, Clone)]
#[serde(untagged)]
enum BudgetValue {
    Amount(Decimal),
    Group(BTreeMap<String, BudgetValue>),
    /// YAML 空值（如 `额外储蓄:` 后面没填数字），视为金额 0
    Null,
}

/// 递归展平嵌套的 YAML 预算映射为扁平化的 `BudgetDirective` 列表。
///
/// 例如 `生活费: { 交通: 1500 }` 将生成桶名为 `生活费.交通` 的指令，
/// 父桶 `生活费` 本身不生成指令，其统计值由子桶聚合得出。
fn flatten_budget_map(
    prefix: &str,
    map: &BTreeMap<String, BudgetValue>,
    month: &str,
    label: Option<&str>,
    source_key: &str,
    directives: &mut Vec<BudgetDirective>,
) {
    for (key, value) in map {
        let full_name = if prefix.is_empty() {
            key.clone()
        } else {
            format!("{}.{}", prefix, key)
        };
        match value {
            BudgetValue::Amount(amount) => {
                directives.push(BudgetDirective {
                    month: month.to_string(),
                    label: label.map(|s| s.to_string()),
                    source_key: source_key.to_string(),
                    bucket: full_name,
                    amount: *amount,
                });
            }
            BudgetValue::Group(sub_map) => {
                flatten_budget_map(&full_name, sub_map, month, label, source_key, directives);
            }
            BudgetValue::Null => {
                directives.push(BudgetDirective {
                    month: month.to_string(),
                    label: label.map(|s| s.to_string()),
                    source_key: source_key.to_string(),
                    bucket: full_name,
                    amount: Decimal::ZERO,
                });
            }
        }
    }
}

// ---------------------------------------------------------------------------
// 模板支持
// ---------------------------------------------------------------------------

/// 递归合并两个预算桶映射。
///
/// 覆写层中的键优先：同名叶节点（Amount/Null）直接覆写；
/// 同名 Group 递归合并子映射。
fn merge_bucket_maps(
    base: &BTreeMap<String, BudgetValue>,
    override_map: &BTreeMap<String, BudgetValue>,
) -> BTreeMap<String, BudgetValue> {
    let mut merged = base.clone();
    for (key, value) in override_map {
        match value {
            BudgetValue::Group(ov_group) => {
                if let Some(BudgetValue::Group(base_group)) = merged.get(key) {
                    merged.insert(key.clone(), BudgetValue::Group(merge_bucket_maps(base_group, ov_group)));
                } else {
                    merged.insert(key.clone(), BudgetValue::Group(ov_group.clone()));
                }
            }
            _ => {
                merged.insert(key.clone(), value.clone());
            }
        }
    }
    merged
}

/// 为指定月份查找适用的模板映射。
///
/// 合并策略：
/// 1. 以 `default_monthly` 为起点
/// 2. 按日期顺序叠加上 label 含 "template" 的月度模板键
///    （<https://github.com/TinySnow/template> 生效日期 <https://github.com/TinySnow/template> 月的模板才适用）
/// 3. 同名键按上面顺序覆写（后面的盖前面的）
fn resolve_template(
    templates: &BTreeMap<String, BTreeMap<String, BudgetValue>>,
    default_template: &Option<BTreeMap<String, BudgetValue>>,
    target_month: &str,
) -> BTreeMap<String, BudgetValue> {
    let mut effective = default_template.clone().unwrap_or_default();
    for (tmpl_month, tmpl_map) in templates {
        if tmpl_month.as_str() <= target_month {
            effective = merge_bucket_maps(&effective, tmpl_map);
        }
    }
    effective
}

// ---------------------------------------------------------------------------
// 配置加载函数
// ---------------------------------------------------------------------------

/// 从 `budgets.yaml` 加载所有预算指令。
///
/// 解析格式：外层 `BTreeMap<String, BTreeMap<String, BudgetValue>>`，
/// 其中外层 key 为 `YYYY-MM` 或 `YYYY-MM 标签`，
/// 内层值可为 Decimal（叶节点）或嵌套 Map（分支节点）。
/// 嵌套 YAML 会在加载时递归展平为点号分隔的全路径桶名。
///
/// 支持模板机制 (`default_monthly` 和 `YYYY-MM template` 键)：
/// 月份键仅需写与模板不同的部分，其余金额自动从当前生效模板继承。
pub fn load_budget_directives(path: &Path) -> Result<Vec<BudgetDirective>> {
    let content = fs::read_to_string(path)?;
    let content = strip_bom(&content);
    let raw: BTreeMap<String, BTreeMap<String, BudgetValue>> =
        serde_yaml::from_str(content).context("Invalid budgets YAML")?;

    // --- 提取模板 ---
    let default_monthly = raw.get("default_monthly").cloned();
    let mut dated_templates: BTreeMap<String, BTreeMap<String, BudgetValue>> = BTreeMap::new();
    for (raw_key, bucket_map) in &raw {
        if raw_key == "default_monthly" { continue; }
        if let Ok((month, label)) = parse_budget_key(raw_key) {
            if let Some(ref l) = label {
                if l.to_lowercase().contains("template") {
                    dated_templates.insert(month, bucket_map.clone());
                }
            }
        }
    }

    let mut directives = Vec::new();
    for (raw_key, bucket_map) in raw {
        if raw_key == "default_monthly" { continue; }

        match parse_budget_key(&raw_key) {
            Ok((month, label)) => {
                // 跳过模板键本身
                let is_template_key = label
                    .as_deref()
                    .map(|l| l.to_lowercase().contains("template"))
                    .unwrap_or(false);
                if is_template_key { continue; }

                let base = resolve_template(&dated_templates, &default_monthly, &month);
                let merged = merge_bucket_maps(&base, &bucket_map);
                flatten_budget_map("", &merged, &month, label.as_deref(), &raw_key, &mut directives);
            }
            Err(_) => {
                // 无法解析为日期的键（如 YAML 顶层意外值）直接跳过
            }
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
/// 支持日期格式：`2026-06`, `2026.1.4`, `2026/01/03`。
/// 日期部分自动规范化为 YYYY-MM，日部分附加到标签中。
///
/// 示例：
/// - `"2026-06"` → `("2026-06", None)`
/// - `"2026.1.4 绩效"` → `("2026-01", Some("4日 绩效"))`
pub fn parse_budget_key(raw: &str) -> Result<(String, Option<String>)> {
    let trimmed = raw.trim();
    let cap = BUDGET_KEY_RE.captures(trimmed).ok_or_else(|| {
        anyhow!(
            "Invalid budget key '{}', expected 'YYYY-MM' / 'YYYY.M.D' / 'YYYY/M/D' with optional label",
            raw
        )
    })?;

    let y: u32 = cap["year"].parse()?;
    let m: u32 = cap["month"].parse()?;
    let month = format!("{:04}-{:02}", y, m);
    validate_month(&month)?;

    let day = cap.name("day").map(|m| m.as_str().trim_start_matches('0'));
    let text_label = cap
        .name("label")
        .map(|m| m.as_str().trim().to_string())
        .filter(|s| !s.is_empty());

    let label = match (day, text_label) {
        (Some(d), Some(t)) => Some(format!("{}日 {}", d, t)),
        (Some(d), None) => Some(format!("{}日", d)),
        (None, Some(t)) => Some(t),
        (None, None) => None,
    };

    Ok((month, label))
}

/// 从 `mappings.yaml` 加载预算映射配置。
pub fn load_mappings(path: &Path) -> Result<BudgetMappings> {
    let content = fs::read_to_string(path)?;
    let content = strip_bom(&content);
    serde_yaml::from_str(content).context("Invalid mappings YAML")
}

/// 收集所有已知的预算桶名称集合。
///
/// 来源包括：预算指令中的桶名、mappings 中声明的 bucket_types 键、
/// 默认生活费桶名，以及从点号桶名推导出的父桶。
pub fn collect_known_buckets(
    directives: &[BudgetDirective],
    mappings: &BudgetMappings,
) -> BTreeSet<String> {
    let mut buckets = BTreeSet::new();

    for item in directives {
        buckets.insert(item.bucket.clone());
        let mut name = item.bucket.as_str();
        while let Some(pos) = name.rfind('.') {
            name = &name[..pos];
            buckets.insert(name.to_string());
        }
    }
    for bucket in mappings.bucket_types.keys() {
        buckets.insert(bucket.clone());
    }
    // asset_bucket_accounts 中的桶也视为已知（自动推断为 asset 类型）
    for bucket in mappings.asset_bucket_accounts.keys() {
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

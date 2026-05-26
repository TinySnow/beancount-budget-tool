//! 预算流计算模块。
//!
//! 本模块是预算系统的核心引擎，负责：
//! - 将已解析的账本交易映射到预算桶（`collect_bucket_tx_flows`）
//!   - 支持显式 `budget` metadata 的单桶或多桶映射（逗号分隔，如 `budget: "储蓄, 投资"`）
//! - 资产桶的资金流入识别与位置跟踪（`derive_asset_bucket_flow`）
//! - 跨桶、跨月的预算汇总（`summarize_buckets`）
//! - 单桶作用域数据构建（`build_scoped_bucket_data`）
//! - 资产桶资金位置累计（`collect_asset_locations`）

use std::{
    collections::{BTreeMap, BTreeSet, HashMap},
    path::PathBuf,
    str::FromStr,
};

use anyhow::{Context, Result};
use rust_decimal::Decimal;

use crate::config::{BucketKind, BudgetDirective, BudgetMappings};
use crate::ledger::{self, LedgerTransaction};
use crate::util::{is_month_in_scope, is_target_currency, month_of_date, parent_bucket};
use crate::cli::{ReportConfig, ReportScope};

// ---------------------------------------------------------------------------
// 数据结构
// ---------------------------------------------------------------------------

/// 某笔交易对特定预算桶的资金流动记录。
#[derive(Debug, Clone)]
pub struct BucketTxFlow {
    /// 交易日期
    pub date: chrono::NaiveDate,
    /// 所属月份（YYYY-MM）
    pub month: String,
    /// 预算桶名称
    pub bucket: String,
    /// 桶类型
    pub kind: BucketKind,
    /// 流向桶余额的有符号值：
    /// - expense 桶：消费为负数（减少可用预算）
    /// - asset 桶：存入为正数（增加该桶资产）
    pub flow: Decimal,
    /// 收款人/商户名
    pub payee: Option<String>,
    /// 交易叙述
    pub narration: Option<String>,
    /// 资产类桶的位置变化（account -> delta）
    pub location_deltas: BTreeMap<String, Decimal>,
    /// 原始交易的 metadata 键值对（用于 --filter 关键词搜索）
    pub metadata: HashMap<String, String>,
}

impl BucketTxFlow {
    /// 计算对桶余额的实际影响（绝对值）。
    ///
    /// - expense 桶：`actual = -flow`（消费额转为正数）
    /// - asset 桶：`actual = flow`（存入额保持正数）
    pub fn actual_amount(&self) -> Decimal {
        match self.kind {
            BucketKind::Expense => -self.flow,
            BucketKind::Asset => self.flow,
        }
    }
}

/// 单个预算桶的计划与实际汇总。
#[derive(Debug, Default, Clone)]
pub struct BucketSummary {
    /// 计划预算总额
    pub planned: Decimal,
    /// 实际发生额
    pub actual: Decimal,
}

/// 单个预算桶在指定作用域下的完整数据视图。
#[derive(Debug, Clone)]
pub struct ScopedBucketData {
    /// 桶名称
    pub bucket: String,
    /// 桶类型
    pub kind: BucketKind,
    /// 范围内计划预算总额
    pub planned: Decimal,
    /// 范围内实际发生额
    pub actual: Decimal,
    /// 结余（planned - actual）
    pub remain: Decimal,
    /// 范围内相关的预算指令
    pub directives: Vec<BudgetDirective>,
    /// 范围内相关的资金流动记录
    pub flows: Vec<BucketTxFlow>,
}

/// 未知预算桶的警告统计。
#[derive(Debug, Default)]
pub struct WarningStats {
    /// 未知桶的总金额（绝对值求和）
    pub unknown_bucket_amount: Decimal,
    /// 未知桶的名称集合
    pub unknown_bucket_names: BTreeSet<String>,
}

// ---------------------------------------------------------------------------
// 核心函数
// ---------------------------------------------------------------------------

/// 解析 bucket metadata 值中的"桶名:金额"或"桶名900"语法。
///
/// 优先尝试冒号分隔，若无冒号则从尾部提取连续数字作为金额。
fn parse_bucket_amount(raw: &str) -> (&str, Option<Decimal>) {
    // 优先冒号分隔（半角 : 或全角 ：）
    if let Some((name, amt)) = raw.split_once(':').or_else(|| raw.split_once('：')) {
        let parsed = Decimal::from_str(amt.trim()).ok();
        return (name.trim(), parsed);
    }
    // 无冒号：从尾部提取连续数字（含可选的小数点和负号）
    let trimmed = raw.trim();
    // 找到最后一个非数字、非小数点的字符，其后即为金额
    if let Some((pos, ch)) = trimmed.char_indices().rev()
        .find(|(_, c)| !c.is_ascii_digit() && *c != '.' && *c != '-')
    {
        let number_start = pos + ch.len_utf8();
        let number_part = &trimmed[number_start..];
        if !number_part.is_empty() {
            if let Ok(amt) = Decimal::from_str(number_part) {
                let name_part = trimmed[..number_start].trim_end();
                if !name_part.is_empty() {
                    return (name_part, Some(amt));
                }
            }
        }
    }
    (trimmed, None)
}

/// 将一笔纯资产转移按资产桶方式处理（用于 expense 桶的退化分支）。
///
/// 与普通资产桶不同：这里直接收录所有 Assets: 腿（正负均记录），
/// 以便完整追踪转入方和转出方的位置变动。
fn process_as_asset(
    tx: &LedgerTransaction,
    bucket_name: &str,
    cap_amount: Option<Decimal>,
    target_currency: &str,
    month: &str,
    flows: &mut Vec<BucketTxFlow>,
) {
    // 收集所有目标币种的 Assets: 腿
    let mut asset_legs: Vec<(String, Decimal)> = Vec::new();
    for posting in &tx.postings {
        if !posting.account.starts_with("Assets:") { continue; }
        let Some(amount) = posting.amount else { continue; };
        if !is_target_currency(posting.currency.as_deref(), target_currency) { continue; }
        asset_legs.push((posting.account.clone(), amount));
    }
    if asset_legs.is_empty() { return; }

    let mut location_deltas: BTreeMap<String, Decimal> = BTreeMap::new();
    let mut positive_flow = Decimal::ZERO;
    for (account, amount) in &asset_legs {
        *location_deltas.entry(account.clone()).or_default() += *amount;
        if amount.is_sign_positive() {
            positive_flow += *amount;
        }
    }

    // flow 用正腿额做展示用（不为 0 才能在明细中显示存入/转出标签）
    // 净流 0 不影响 expense 汇总（summary 按类型过滤）
    let mut flow = positive_flow;
    // 用 cap 缩放
    if let Some(cap) = cap_amount {
        if !positive_flow.is_zero() {
            let ratio = (cap / positive_flow).round_dp(6);
            flow = cap;
            for (_account, delta) in location_deltas.iter_mut() {
                *delta = (*delta * ratio).round_dp(2);
            }
            location_deltas.retain(|_, v| !v.is_zero());
        } else {
            flow = cap;
        }
    }

    if !location_deltas.is_empty() {
        flows.push(BucketTxFlow {
            date: tx.date,
            month: month.to_string(),
            bucket: bucket_name.to_string(),
            kind: BucketKind::Asset,
            flow,
            payee: tx.payee.clone(),
            narration: tx.narration.clone(),
            location_deltas,
            metadata: tx.metadata.clone(),
        });
    }
}

/// 解析所有账本文件，将每笔交易映射到对应的预算桶资金流动记录。
///
/// # 映射逻辑
///
/// 1. **显式 bucket metadata**：若交易含有 `budget`（或兼容拼写 `budge`）metadata，
///    则根据桶类型（Expense/Asset）分别处理。支持 `桶名:金额` 和 `桶名900` 两种写法。
/// 2. **隐式映射**：无 budget metadata 的消费过账按最长前缀匹配账户映射，
///    最终回退到默认生活费桶。
///
/// 所有交易按日期排序处理，以保证资产桶的推断位置稳定。
pub fn collect_bucket_tx_flows(
    ledgers: &[PathBuf],
    mappings: &BudgetMappings,
    target_currency: &str,
) -> Result<Vec<BucketTxFlow>> {
    let mut all_txs = Vec::new();

    for ledger in ledgers {
        let txs = ledger::parse_ledger_file(ledger)
            .with_context(|| format!("Failed to parse ledger: {}", ledger.display()))?;
        all_txs.extend(txs);
    }

    // 按日期排序，保证资产桶的"推断位置"稳定
    all_txs.sort_by_key(|tx| tx.date);

    let mut inferred_asset_accounts: HashMap<String, BTreeSet<String>> = HashMap::new();
    let mut flows = Vec::new();

    for tx in all_txs {
        let month = month_of_date(tx.date);

        let bucket_override = tx
            .metadata
            .get("budget")
            .cloned()
            .or_else(|| tx.metadata.get("budge").cloned())
            .and_then(|value| {
                let trimmed = value.trim();
                if trimmed.is_empty() {
                    None
                } else {
                    Some(trimmed.to_string())
                }
            });

        if let Some(override_str) = bucket_override {
            // 支持半角和全角逗号分隔
            let normalized = override_str.replace('，', ",");
            let bucket_names: Vec<&str> = normalized
                .split(',')
                .map(|s| s.trim())
                .filter(|s| !s.is_empty())
                .collect();
            for raw_name in bucket_names {
                // 支持 budget: "桶名:金额" 或 "桶名900" 两种写法
                let (bucket_name, cap_amount) = parse_bucket_amount(raw_name);
                let kind = mappings.bucket_kind(bucket_name);
                match kind {
                    BucketKind::Expense => {
                        let mut flow = Decimal::ZERO;
                        for posting in &tx.postings {
                            if !posting.account.starts_with("Expenses:") { continue; }
                            let Some(amount) = posting.amount else { continue; };
                            if !is_target_currency(posting.currency.as_deref(), target_currency) { continue; }
                            flow -= amount;
                        }
                        if flow.is_zero() {
                            // 无实际支出 → 纯资产转移，回退为 Asset 模式记录位置
                            process_as_asset(
                                &tx, bucket_name, cap_amount, target_currency, &month, &mut flows,
                            );
                            continue;
                        }
                        if let Some(cap) = cap_amount {
                            let cap_abs = cap.abs();
                            if flow.abs() > cap_abs {
                                flow = if flow.is_sign_negative() { -cap_abs } else { cap_abs };
                            }
                        }

                        flows.push(BucketTxFlow {
                            date: tx.date,
                            month: month.clone(),
                            bucket: bucket_name.to_string(),
                            kind,
                            flow,
                            payee: tx.payee.clone(),
                            narration: tx.narration.clone(),
                            location_deltas: BTreeMap::new(),
                            metadata: tx.metadata.clone(),
                        });
                    }
                    BucketKind::Asset => {
                        let Some((mut flow, mut location_deltas)) = derive_asset_bucket_flow(
                            &tx,
                            bucket_name,
                            target_currency,
                            mappings,
                            &mut inferred_asset_accounts,
                        ) else {
                            continue;
                        };

                        // 若指定了固定金额，按比例缩放 flow 和 location_deltas
                        if let Some(cap) = cap_amount {
                            if flow.is_zero() {
                                // 无自然 flow（如配置未匹配到但手动分配金额），创建虚拟 flow
                                flow = cap;
                                // location_deltas 保持为空或手动分配
                            } else {
                                let ratio = (cap / flow).round_dp(6);
                                flow = cap;
                                for (_account, delta) in location_deltas.iter_mut() {
                                    *delta = (*delta * ratio).round_dp(2);
                                }
                                location_deltas.retain(|_, v| !v.is_zero());
                            }
                        }

                        if !flow.is_zero() || !location_deltas.is_empty() {
                            flows.push(BucketTxFlow {
                                date: tx.date,
                                month: month.clone(),
                                bucket: bucket_name.to_string(),
                                kind,
                                flow,
                                payee: tx.payee.clone(),
                                narration: tx.narration.clone(),
                                location_deltas,
                                metadata: tx.metadata.clone(),
                            });
                        }
                    }
                }
            }
            continue;
        }

        // 未显式标注 budget 的消费类过账：按映射或默认生活费桶归类
        let mut per_bucket_flow: BTreeMap<String, Decimal> = BTreeMap::new();
        for posting in &tx.postings {
            if !posting.account.starts_with("Expenses:") {
                continue;
            }
            let Some(amount) = posting.amount else {
                continue;
            };
            if !is_target_currency(posting.currency.as_deref(), target_currency) {
                continue;
            }

            let bucket = crate::config::resolve_bucket_by_account(mappings, &posting.account)
                .unwrap_or_else(|| mappings.default_expense_bucket.clone());
            *per_bucket_flow.entry(bucket).or_default() -= amount;
        }

        for (bucket, flow) in per_bucket_flow {
            if flow.is_zero() {
                continue;
            }
            flows.push(BucketTxFlow {
                date: tx.date,
                month: month.clone(),
                bucket,
                kind: BucketKind::Expense,
                flow,
                payee: tx.payee.clone(),
                narration: tx.narration.clone(),
                location_deltas: BTreeMap::new(),
                metadata: tx.metadata.clone(),
            });
        }
    }

    Ok(flows)
}

/// 从一笔资产转移交易中提取目标资产桶的资金流动和位置变化。
///
/// # 识别策略
///
/// 1. 优先使用 `asset_bucket_accounts` 配置的账户前缀做精确匹配。
/// 2. 若无配置但有正向资产腿，默认将正向腿视为流入位置。
/// 3. 若无正向腿但有历史推断位置，从中扣减。
/// 4. 兜底使用全部资产腿的净额。
pub fn derive_asset_bucket_flow(
    tx: &LedgerTransaction,
    bucket: &str,
    target_currency: &str,
    mappings: &BudgetMappings,
    inferred_asset_accounts: &mut HashMap<String, BTreeSet<String>>,
) -> Option<(Decimal, BTreeMap<String, Decimal>)> {
    let mut asset_postings: Vec<(String, Decimal)> = Vec::new();

    for posting in &tx.postings {
        if !posting.account.starts_with("Assets:") {
            continue;
        }
        let Some(amount) = posting.amount else {
            continue;
        };
        if !is_target_currency(posting.currency.as_deref(), target_currency) {
            continue;
        }
        asset_postings.push((posting.account.clone(), amount));
    }

    if asset_postings.is_empty() {
        return None;
    }

    // 优先使用显式配置的资产账户前缀做精确归因
    if let Some(prefixes) = mappings.configured_asset_prefixes(bucket) {
        if !prefixes.is_empty() {
            let selected = asset_postings
                .into_iter()
                .filter(|(account, _)| prefixes.iter().any(|p| account.starts_with(p)))
                .collect::<Vec<_>>();

            if selected.is_empty() {
                return None;
            }

            let mut location_deltas = BTreeMap::new();
            let mut flow = Decimal::ZERO;
            for (account, amount) in selected {
                *location_deltas.entry(account.clone()).or_default() += amount;
                flow += amount;
                if amount.is_sign_positive() {
                    inferred_asset_accounts
                        .entry(bucket.to_string())
                        .or_default()
                        .insert(account);
                }
            }
            return Some((flow, location_deltas));
        }
    }

    // 无显式配置时：
    // 1) 若有正向资产腿，默认视为"流入该桶"的资产位置
    // 2) 若没有正向资产腿，尝试从已推断位置中扣减（处理储蓄取出场景）
    // 3) 再兜底为全资产腿净额
    let positive_legs = asset_postings
        .iter()
        .filter(|(_, amount)| amount.is_sign_positive())
        .cloned()
        .collect::<Vec<_>>();

    if !positive_legs.is_empty() {
        let mut location_deltas = BTreeMap::new();
        let mut flow = Decimal::ZERO;
        for (account, amount) in positive_legs {
            *location_deltas.entry(account.clone()).or_default() += amount;
            flow += amount;
            inferred_asset_accounts
                .entry(bucket.to_string())
                .or_default()
                .insert(account);
        }
        return Some((flow, location_deltas));
    }

    if let Some(known_accounts) = inferred_asset_accounts.get(bucket) {
        let selected = asset_postings
            .iter()
            .filter(|(account, _)| known_accounts.contains(account))
            .cloned()
            .collect::<Vec<_>>();

        if !selected.is_empty() {
            let mut location_deltas = BTreeMap::new();
            let mut flow = Decimal::ZERO;
            for (account, amount) in selected {
                *location_deltas.entry(account).or_default() += amount;
                flow += amount;
            }
            return Some((flow, location_deltas));
        }
    }

    // 兜底：使用全部资产腿
    let mut location_deltas = BTreeMap::new();
    let mut flow = Decimal::ZERO;
    for (account, amount) in asset_postings {
        *location_deltas.entry(account.clone()).or_default() += amount;
        flow += amount;
        if amount.is_sign_positive() {
            inferred_asset_accounts
                .entry(bucket.to_string())
                .or_default()
                .insert(account);
        }
    }

    Some((flow, location_deltas))
}

/// 按桶聚合预算指令与资金流动，生成汇总统计。
///
/// 叶节点桶的 planned/actual 会自动向上聚合到父桶。
/// 仅计入与桶配置类型匹配的流（expense 桶只算 expense 流，asset 桶只算 asset 流）。
pub fn summarize_buckets(
    directives: &[BudgetDirective],
    flows: &[BucketTxFlow],
    target_month: &str,
    scope: ReportScope,
    mappings: &BudgetMappings,
) -> BTreeMap<String, BucketSummary> {
    let mut summaries: BTreeMap<String, BucketSummary> = BTreeMap::new();

    for item in directives {
        if !is_month_in_scope(&item.month, target_month, scope) {
            continue;
        }
        summaries
            .entry(item.bucket.clone())
            .or_default()
            .planned += item.amount;
    }

    for flow in flows {
        if !is_month_in_scope(&flow.month, target_month, scope) {
            continue;
        }
        // 只计入与桶配置类型匹配的流
        let bucket_kind = mappings.bucket_kind(&flow.bucket);
        if flow.kind != bucket_kind {
            continue;
        }
        summaries
            .entry(flow.bucket.clone())
            .or_default()
            .actual += flow.actual_amount();
    }

    // 第二轮：子桶向上聚合到父桶
    let all_buckets: Vec<String> = summaries.keys().cloned().collect();
    for bucket in all_buckets {
        let mut parent = parent_bucket(&bucket);
        let (planned, actual) = {
            let s = &summaries[&bucket];
            (s.planned, s.actual)
        };
        while let Some(p) = parent {
            let entry = summaries.entry(p.to_string()).or_default();
            entry.planned += planned;
            entry.actual += actual;
            parent = parent_bucket(p);
        }
    }

    summaries
}

/// 收集在统计范围内出现但未在预算或映射中定义的未知桶警告信息。
pub fn collect_scope_warnings(
    flows: &[BucketTxFlow],
    known_buckets: &BTreeSet<String>,
    target_month: &str,
    scope: ReportScope,
) -> WarningStats {
    let mut warnings = WarningStats::default();

    for flow in flows {
        if !is_month_in_scope(&flow.month, target_month, scope) {
            continue;
        }

        if known_buckets.contains(&flow.bucket) {
            continue;
        }

        warnings.unknown_bucket_names.insert(flow.bucket.clone());
        warnings.unknown_bucket_amount += flow.actual_amount().abs();
    }

    warnings
}

/// 构建指定预算桶在作用域内的完整数据视图。
///
/// 若目标桶是父桶（无直接预算指令，如 `生活费`），
/// 则自动聚合其所有点号子桶（如 `生活费.交通`、`生活费.饮食`）的指令与资金流动。
/// 若指定了 `--filter` 关键词，仅保留 payee/narration/metadata 中匹配的交易。
pub fn build_scoped_bucket_data(
    config: &ReportConfig,
    bucket: &str,
    mappings: &BudgetMappings,
    directives: &[BudgetDirective],
    flows: &[BucketTxFlow],
) -> ScopedBucketData {
    let prefix = format!("{}.", bucket);
    let target_month = &config.month;

    let directives = directives
        .iter()
        .filter(|item| {
            (item.bucket == bucket || item.bucket.starts_with(&prefix))
                && is_month_in_scope(&item.month, target_month, config.scope)
        })
        .cloned()
        .collect::<Vec<_>>();

    let mut flows: Vec<BucketTxFlow> = flows
        .iter()
        .filter(|flow| {
            (flow.bucket == bucket || flow.bucket.starts_with(&prefix))
                && is_month_in_scope(&flow.month, target_month, config.scope)
        })
        .cloned()
        .collect();

    // 关键词过滤
    if let Some(keyword) = config.filter.as_deref() {
        let kw = keyword.to_lowercase();
        flows.retain(|f| {
            f.payee.as_deref().map(|s| s.to_lowercase()).unwrap_or_default().contains(&kw)
                || f.narration.as_deref().map(|s| s.to_lowercase()).unwrap_or_default().contains(&kw)
                || f.metadata.values().any(|v| v.to_lowercase().contains(&kw))
        });
    }

    // --hide-asset-flows
    if config.hide_asset_flows {
        flows.retain(|f| f.kind != BucketKind::Asset);
    }

    let planned = directives
        .iter()
        .fold(Decimal::ZERO, |acc, item| acc + item.amount);
    // 仅汇总与桶配置类型匹配的流（exclude 退化 Asset 流 for expense 桶）
    let bucket_kind = mappings.bucket_kind(bucket);
    let actual = flows
        .iter()
        .filter(|f| f.kind == bucket_kind)
        .fold(Decimal::ZERO, |acc, flow| acc + flow.actual_amount());
    let remain = planned - actual;

    ScopedBucketData {
        bucket: bucket.to_string(),
        kind: mappings.bucket_kind(bucket),
        planned,
        actual,
        remain,
        directives,
        flows,
    }
}

/// 汇总所有资产桶资金流动，计算各账户的累计余额。
///
/// 仅纳入指定桶的资产类型流动，按 Cumulative 范围（截至目标月）累计。
/// 若查询的是父桶（无直接资产流动），则自动聚合所有子桶的位置数据。
pub fn collect_asset_locations(
    bucket: &str,
    target_month: &str,
    flows: &[BucketTxFlow],
) -> BTreeMap<String, Decimal> {
    let prefix = format!("{}.", bucket);
    let mut locations: BTreeMap<String, Decimal> = BTreeMap::new();
    for flow in flows {
        if flow.kind != BucketKind::Asset {
            continue;
        }
        if flow.bucket != bucket && !flow.bucket.starts_with(&prefix) {
            continue;
        }
        if !is_month_in_scope(&flow.month, target_month, ReportScope::Cumulative) {
            continue;
        }
        for (account, delta) in &flow.location_deltas {
            *locations.entry(account.clone()).or_default() += *delta;
        }
    }
    locations.retain(|_, amount| !amount.is_zero());
    locations
}

/// 收集应导出的预算桶名称集合。
///
/// 若用户在 CLI 中指定了 `--bucket`，则仅导出该桶；
/// 否则导出所有在范围内出现的桶。
pub fn collect_buckets_for_export(
    config: &ReportConfig,
    directives: &[BudgetDirective],
    flows: &[BucketTxFlow],
    summaries: &BTreeMap<String, BucketSummary>,
) -> BTreeSet<String> {
    if let Some(bucket) = config.bucket.as_ref() {
        return BTreeSet::from([bucket.clone()]);
    }

    let target_month = &config.month;
    let mut buckets = BTreeSet::new();
    for bucket in summaries.keys() {
        buckets.insert(bucket.clone());
    }
    for item in directives {
        if is_month_in_scope(&item.month, target_month, config.scope) {
            buckets.insert(item.bucket.clone());
        }
    }
    for flow in flows {
        if is_month_in_scope(&flow.month, target_month, config.scope) {
            buckets.insert(flow.bucket.clone());
        }
    }
    buckets
}

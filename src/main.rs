//! 独立预算分析工具（面向 Beancount 账本）。
//!
//! 该工具读取 Beancount 账本与预算配置，支持：
//! - 月度预算与“同月额外预算”（如 `YYYY-MM 绩效`）聚合；
//! - 月度或累计视角（截至目标月）的预算结余统计；
//! - 交易级 `budget` metadata 优先；
//! - 未显式标注预算桶的 `Expenses:*` 自动归入默认生活费桶；
//! - 资产类预算桶（如储蓄）在 `Assets` 账户间转移时也可统计，并可查看资金位置。

use std::{
    collections::{BTreeMap, BTreeSet, HashMap},
    fmt::Write as FmtWrite,
    fs,
    path::{Path, PathBuf},
    str::FromStr,
};

use anyhow::{Context, Result, anyhow, bail};
use chrono::{Datelike, NaiveDate};
use clap::{Parser, ValueEnum};
use once_cell::sync::Lazy;
use regex::Regex;
use rust_decimal::Decimal;
use serde::Deserialize;

/// 交易头：`YYYY-MM-DD * ...` 或 `YYYY-MM-DD ! ...`
static TX_HEADER_RE: Lazy<Regex> = Lazy::new(|| {
    Regex::new(r"^(?P<date>\d{4}-\d{2}-\d{2})\s+[*!](?:\s|$)").expect("valid tx header regex")
});

/// 交易标题中的引号字符串（用于提取 payee / narration）。
static QUOTED_TEXT_RE: Lazy<Regex> =
    Lazy::new(|| Regex::new(r#"\"((?:\\.|[^\"\\])*)\""#).expect("valid quoted text regex"));

/// 交易 metadata 行：`  key: value`
///
/// 注意：这里要求 `key` 后必须是 `:<空格>`，避免把
/// `Expenses:Food  10 CNY` 误判为 metadata。
static TX_METADATA_RE: Lazy<Regex> = Lazy::new(|| {
    Regex::new(r"^\s{2}(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*:\s+(?P<value>.+?)\s*$")
        .expect("valid tx metadata regex")
});

/// 过账行（可选金额）：
/// `  Expenses:Food  10 CNY`
/// `  Income:Misc`
static POSTING_RE: Lazy<Regex> = Lazy::new(|| {
    Regex::new(
        r"^\s{2}(?:[*!]\s+)?(?P<account>\S+)(?:\s{2,}(?P<number>[+-]?\d+(?:\.\d+)?)(?:\s+(?P<currency>[A-Za-z0-9_.-]+))?)?",
    )
    .expect("valid posting regex")
});

/// 预算 key：`YYYY-MM` 或 `YYYY-MM 任意标签`
static BUDGET_KEY_RE: Lazy<Regex> = Lazy::new(|| {
    Regex::new(r"^(?P<month>\d{4}-\d{2})(?:\s+(?P<label>\S.*))?$").expect("valid budget key regex")
});

#[derive(Parser, Debug)]
#[command(name = "beancount-budget-tool")]
#[command(about = "Budget report tool for Beancount ledgers")]
#[command(version)]
struct Cli {
    /// 账本文件路径（可重复传入多个）
    #[arg(long = "ledger", short = 'l')]
    ledgers: Vec<PathBuf>,

    /// 账本目录（可重复传入多个，递归扫描 *.bean/*.beancount）
    #[arg(long = "ledger-dir")]
    ledger_dirs: Vec<PathBuf>,

    /// 统计月份（YYYY-MM）
    #[arg(long, short = 'm')]
    month: String,

    /// 预算配置文件
    #[arg(long, required = true)]
    budgets: PathBuf,

    /// 映射配置文件
    #[arg(long, required = true)]
    mappings: PathBuf,

    /// 统计币种（默认 CNY）
    #[arg(long, default_value = "CNY")]
    currency: String,

    /// 统计范围：month（仅目标月）或 cumulative（截至目标月累计）
    #[arg(long, value_enum, default_value_t = ReportScope::Month)]
    scope: ReportScope,

    /// 指定预算桶名称；设置后输出该桶的历史查询
    #[arg(long)]
    bucket: Option<String>,

    /// 预算桶历史视图：summary（汇总）/monthly（分月）/detail（明细）
    #[arg(long, value_enum, default_value_t = BucketView::Summary)]
    bucket_view: BucketView,

    /// 对资产类预算桶显示“资金当前位置”（截至目标月）
    #[arg(long)]
    show_locations: bool,

    /// 输出目录；设置后自动生成 markdown/csv 报告文件
    #[arg(long = "out-dir")]
    out_dir: Option<PathBuf>,

    /// 严格模式：若存在未知预算桶则返回非零退出码
    #[arg(long)]
    strict: bool,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, ValueEnum)]
enum ReportScope {
    Month,
    Cumulative,
}

impl ReportScope {
    fn label(self) -> &'static str {
        match self {
            Self::Month => "month",
            Self::Cumulative => "cumulative",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, ValueEnum)]
enum BucketView {
    Summary,
    Monthly,
    Detail,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "lowercase")]
enum BucketKind {
    Expense,
    Asset,
}

impl BucketKind {
    fn label(self) -> &'static str {
        match self {
            Self::Expense => "expense",
            Self::Asset => "asset",
        }
    }
}

#[derive(Debug, Deserialize, Default)]
struct BudgetMappings {
    /// account_prefix -> budget_bucket
    #[serde(default)]
    defaults: BTreeMap<String, String>,

    /// 未标注预算桶的消费类分录默认归属（默认：生活费）
    #[serde(default = "default_expense_bucket")]
    default_expense_bucket: String,

    /// bucket -> kind(expense/asset)
    #[serde(default)]
    bucket_types: BTreeMap<String, BucketKind>,

    /// bucket -> [account_prefix, ...]（可选）
    /// 用于精确定位资产类预算桶资金归属。
    #[serde(default)]
    asset_bucket_accounts: BTreeMap<String, Vec<String>>,
}

impl BudgetMappings {
    fn bucket_kind(&self, bucket: &str) -> BucketKind {
        self.bucket_types
            .get(bucket)
            .copied()
            .unwrap_or(BucketKind::Expense)
    }

    fn configured_asset_prefixes(&self, bucket: &str) -> Option<&[String]> {
        self.asset_bucket_accounts.get(bucket).map(Vec::as_slice)
    }
}

fn default_expense_bucket() -> String {
    "生活费".to_string()
}

#[derive(Debug)]
struct LedgerTransaction {
    date: NaiveDate,
    payee: Option<String>,
    narration: Option<String>,
    metadata: HashMap<String, String>,
    postings: Vec<LedgerPosting>,
}

#[derive(Debug)]
struct LedgerPosting {
    account: String,
    amount: Option<Decimal>,
    currency: Option<String>,
}

#[derive(Debug, Clone)]
struct BudgetDirective {
    month: String,
    label: Option<String>,
    source_key: String,
    bucket: String,
    amount: Decimal,
}

#[derive(Debug, Clone)]
struct BucketTxFlow {
    date: NaiveDate,
    month: String,
    bucket: String,
    kind: BucketKind,
    /// 流向桶余额的签名值：
    /// - expense 桶：消费为负数（减少可用预算）
    /// - asset 桶：存入为正数（增加该桶资产）
    flow: Decimal,
    payee: Option<String>,
    narration: Option<String>,
    /// 资产类桶的位置变化（account -> delta）
    location_deltas: BTreeMap<String, Decimal>,
}

impl BucketTxFlow {
    fn actual_amount(&self) -> Decimal {
        match self.kind {
            BucketKind::Expense => -self.flow,
            BucketKind::Asset => self.flow,
        }
    }
}

#[derive(Debug, Default, Clone)]
struct BucketSummary {
    planned: Decimal,
    actual: Decimal,
}

#[derive(Debug, Clone)]
struct ScopedBucketData {
    bucket: String,
    kind: BucketKind,
    planned: Decimal,
    actual: Decimal,
    remain: Decimal,
    directives: Vec<BudgetDirective>,
    flows: Vec<BucketTxFlow>,
}

#[derive(Debug, Default)]
struct WarningStats {
    unknown_bucket_amount: Decimal,
    unknown_bucket_names: BTreeSet<String>,
}

fn main() -> Result<()> {
    let cli = Cli::parse();
    validate_month(&cli.month)?;
    let ledger_files = resolve_ledger_inputs(&cli)?;

    let budget_directives = load_budget_directives(&cli.budgets)
        .with_context(|| format!("Failed to load budgets: {}", cli.budgets.display()))?;
    let mappings = load_mappings(&cli.mappings)
        .with_context(|| format!("Failed to load mappings: {}", cli.mappings.display()))?;

    let target_currency = cli.currency.to_ascii_uppercase();
    let tx_flows = collect_bucket_tx_flows(&ledger_files, &mappings, &target_currency)?;
    let summaries = summarize_buckets(&budget_directives, &tx_flows, &cli.month, cli.scope);

    let known_buckets = collect_known_buckets(&budget_directives, &mappings);
    let warnings = collect_scope_warnings(&tx_flows, &known_buckets, &cli.month, cli.scope);

    if let Some(bucket) = cli.bucket.as_ref() {
        let output = render_bucket_report_text(
            &build_scoped_bucket_data(&cli, bucket, &mappings, &budget_directives, &tx_flows),
            &cli,
            &target_currency,
            cli.bucket_view,
            cli.show_locations,
            &tx_flows,
        );
        print!("{output}");
    } else {
        let output = render_summary_report_text(
            &cli.month,
            cli.scope,
            &target_currency,
            &summaries,
            &warnings,
        );
        print!("{output}");
    }

    if let Some(out_dir) = cli.out_dir.as_ref() {
        export_reports(
            out_dir,
            &cli,
            &target_currency,
            &mappings,
            &budget_directives,
            &tx_flows,
            &summaries,
            &warnings,
        )?;
    }

    if cli.strict && !warnings.unknown_bucket_amount.is_zero() {
        bail!(
            "Strict mode failed: unknown budget buckets amount = {} {}",
            fmt_decimal(warnings.unknown_bucket_amount),
            target_currency
        );
    }

    Ok(())
}

fn resolve_ledger_inputs(cli: &Cli) -> Result<Vec<PathBuf>> {
    if cli.ledgers.is_empty() && cli.ledger_dirs.is_empty() {
        bail!("No ledger input found. Use --ledger <file> and/or --ledger-dir <dir>.");
    }

    let mut paths = BTreeSet::new();
    for path in &cli.ledgers {
        if !path.is_file() {
            bail!("Ledger file not found: {}", path.display());
        }
        paths.insert(path.to_path_buf());
    }

    for dir in &cli.ledger_dirs {
        collect_ledger_files_recursively(dir, &mut paths)
            .with_context(|| format!("Failed to scan ledger dir: {}", dir.display()))?;
    }

    if paths.is_empty() {
        bail!("No ledger files discovered. Expected extensions: .bean / .beancount");
    }

    Ok(paths.into_iter().collect())
}

fn collect_ledger_files_recursively(dir: &Path, out: &mut BTreeSet<PathBuf>) -> Result<()> {
    if !dir.is_dir() {
        bail!("Ledger dir not found: {}", dir.display());
    }

    for entry in fs::read_dir(dir)? {
        let entry = entry?;
        let path = entry.path();

        if path.is_dir() {
            collect_ledger_files_recursively(&path, out)?;
            continue;
        }

        if !path.is_file() {
            continue;
        }

        let Some(ext) = path.extension().and_then(|v| v.to_str()) else {
            continue;
        };

        let ext = ext.to_ascii_lowercase();
        if ext == "bean" || ext == "beancount" {
            out.insert(path);
        }
    }

    Ok(())
}

fn validate_month(raw: &str) -> Result<()> {
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

fn load_budget_directives(path: &Path) -> Result<Vec<BudgetDirective>> {
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

fn parse_budget_key(raw: &str) -> Result<(String, Option<String>)> {
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

fn load_mappings(path: &Path) -> Result<BudgetMappings> {
    let content = fs::read_to_string(path)?;
    let content = content.strip_prefix('\u{feff}').unwrap_or(&content);
    serde_yaml::from_str(content).context("Invalid mappings YAML")
}

fn collect_known_buckets(
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

fn resolve_bucket_by_account(mappings: &BudgetMappings, account: &str) -> Option<String> {
    // 最长前缀优先，避免 `Expenses:Consume` 吃掉更具体的子路径。
    mappings
        .defaults
        .iter()
        .filter(|(prefix, _)| account.starts_with(prefix.as_str()))
        .max_by_key(|(prefix, _)| prefix.len())
        .map(|(_, bucket)| bucket.clone())
}

fn collect_bucket_tx_flows(
    ledgers: &[PathBuf],
    mappings: &BudgetMappings,
    target_currency: &str,
) -> Result<Vec<BucketTxFlow>> {
    let mut all_txs = Vec::new();

    for ledger in ledgers {
        let txs = parse_ledger_file(ledger)
            .with_context(|| format!("Failed to parse ledger: {}", ledger.display()))?;
        all_txs.extend(txs);
    }

    // 先按日期排序，保证资产桶的“推断位置”稳定。
    all_txs.sort_by_key(|tx| tx.date);

    let mut inferred_asset_accounts: HashMap<String, BTreeSet<String>> = HashMap::new();
    let mut flows = Vec::new();

    for tx in all_txs {
        let month = month_of_date(tx.date);

        let bucket_override = tx
            .metadata
            .get("budget")
            .cloned()
            // 兼容历史误拼写，避免直接丢分类。
            .or_else(|| tx.metadata.get("budge").cloned())
            .and_then(|value| {
                let trimmed = value.trim();
                if trimmed.is_empty() {
                    None
                } else {
                    Some(trimmed.to_string())
                }
            });

        if let Some(bucket_name) = bucket_override {
            let kind = mappings.bucket_kind(&bucket_name);
            match kind {
                BucketKind::Expense => {
                    let mut flow = Decimal::ZERO;
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

                        // 消费减少预算余额，记负数；退款（负金额）会转为正流入。
                        flow -= amount;
                    }

                    if !flow.is_zero() {
                        flows.push(BucketTxFlow {
                            date: tx.date,
                            month: month.clone(),
                            bucket: bucket_name,
                            kind,
                            flow,
                            payee: tx.payee.clone(),
                            narration: tx.narration.clone(),
                            location_deltas: BTreeMap::new(),
                        });
                    }
                }
                BucketKind::Asset => {
                    let Some((flow, location_deltas)) = derive_asset_bucket_flow(
                        &tx,
                        &bucket_name,
                        target_currency,
                        mappings,
                        &mut inferred_asset_accounts,
                    ) else {
                        continue;
                    };

                    if !flow.is_zero() || !location_deltas.is_empty() {
                        flows.push(BucketTxFlow {
                            date: tx.date,
                            month: month.clone(),
                            bucket: bucket_name,
                            kind,
                            flow,
                            payee: tx.payee.clone(),
                            narration: tx.narration.clone(),
                            location_deltas,
                        });
                    }
                }
            }
            continue;
        }

        // 未显式标注 budget 的费用类过账：按映射或默认生活费桶归类。
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

            let bucket = resolve_bucket_by_account(mappings, &posting.account)
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
            });
        }
    }

    Ok(flows)
}

fn derive_asset_bucket_flow(
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

    // 优先使用显式配置的资产账户前缀做精确归因。
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
    // 1) 若有正向资产腿，默认视为“流入该桶”的资产位置；
    // 2) 若没有正向资产腿，尝试从已推断位置中扣减（处理储蓄取出场景）；
    // 3) 再兜底为全资产腿净额。
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

fn parse_ledger_file(path: &Path) -> Result<Vec<LedgerTransaction>> {
    let content = fs::read_to_string(path)?;
    parse_ledger_content(&content)
}

fn parse_ledger_content(content: &str) -> Result<Vec<LedgerTransaction>> {
    let content = content.strip_prefix('\u{feff}').unwrap_or(content);

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

        if line.trim().is_empty() {
            if let Some(done) = current.take() {
                transactions.push(done.finish());
            }
            continue;
        }

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

        let Some(builder) = current.as_mut() else {
            continue;
        };

        if let Some(meta) = TX_METADATA_RE.captures(line) {
            let key = meta["key"].to_string();
            let value = parse_metadata_value(&meta["value"]);
            builder.metadata.insert(key, value);
            continue;
        }

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

    if let Some(done) = current.take() {
        transactions.push(done.finish());
    }

    Ok(transactions)
}

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

fn unescape_quoted_text(raw: &str) -> String {
    raw.replace("\\\"", "\"").replace("\\\\", "\\")
}

fn parse_metadata_value(raw: &str) -> String {
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

fn summarize_buckets(
    directives: &[BudgetDirective],
    flows: &[BucketTxFlow],
    target_month: &str,
    scope: ReportScope,
) -> BTreeMap<String, BucketSummary> {
    let mut summaries: BTreeMap<String, BucketSummary> = BTreeMap::new();

    for item in directives {
        if !is_month_in_scope(&item.month, target_month, scope) {
            continue;
        }
        summaries.entry(item.bucket.clone()).or_default().planned += item.amount;
    }

    for flow in flows {
        if !is_month_in_scope(&flow.month, target_month, scope) {
            continue;
        }
        summaries.entry(flow.bucket.clone()).or_default().actual += flow.actual_amount();
    }

    summaries
}

fn collect_scope_warnings(
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

fn render_summary_report_text(
    month: &str,
    scope: ReportScope,
    currency: &str,
    summaries: &BTreeMap<String, BucketSummary>,
    warnings: &WarningStats,
) -> String {
    let mut out = String::new();
    let _ = writeln!(
        out,
        "Budget Report ({}) [{}] scope={} ",
        month,
        currency,
        scope.label()
    );
    let _ = writeln!(
        out,
        "{:<24} {:>14} {:>14} {:>14} {:>10}",
        "Bucket", "Planned", "Actual", "Remain", "Status"
    );
    let _ = writeln!(out, "{}", "-".repeat(82));

    let mut total_planned = Decimal::ZERO;
    let mut total_actual = Decimal::ZERO;
    for (bucket, summary) in summaries {
        let remain = summary.planned - summary.actual;
        let status = if remain.is_sign_negative() {
            "OVER"
        } else {
            "OK"
        };
        total_planned += summary.planned;
        total_actual += summary.actual;

        let _ = writeln!(
            out,
            "{:<24} {:>14} {:>14} {:>14} {:>10}",
            bucket,
            fmt_decimal(summary.planned),
            fmt_decimal(summary.actual),
            fmt_decimal(remain),
            status
        );
    }

    let _ = writeln!(out, "{}", "-".repeat(82));
    let total_remain = total_planned - total_actual;
    let total_status = if total_remain.is_sign_negative() {
        "OVER"
    } else {
        "OK"
    };
    let _ = writeln!(
        out,
        "{:<24} {:>14} {:>14} {:>14} {:>10}",
        "TOTAL",
        fmt_decimal(total_planned),
        fmt_decimal(total_actual),
        fmt_decimal(total_remain),
        total_status
    );

    if !warnings.unknown_bucket_amount.is_zero() {
        let names = warnings
            .unknown_bucket_names
            .iter()
            .cloned()
            .collect::<Vec<_>>()
            .join(", ");
        let _ = writeln!(
            out,
            "WARNING: unknown buckets amount = {} {} (buckets: {})",
            fmt_decimal(warnings.unknown_bucket_amount),
            currency,
            names
        );
    }

    out
}

fn build_scoped_bucket_data(
    cli: &Cli,
    bucket: &str,
    mappings: &BudgetMappings,
    directives: &[BudgetDirective],
    flows: &[BucketTxFlow],
) -> ScopedBucketData {
    let directives = directives
        .iter()
        .filter(|item| {
            item.bucket == bucket && is_month_in_scope(&item.month, &cli.month, cli.scope)
        })
        .cloned()
        .collect::<Vec<_>>();

    let flows = flows
        .iter()
        .filter(|flow| {
            flow.bucket == bucket && is_month_in_scope(&flow.month, &cli.month, cli.scope)
        })
        .cloned()
        .collect::<Vec<_>>();

    let planned = directives
        .iter()
        .fold(Decimal::ZERO, |acc, item| acc + item.amount);
    let actual = flows
        .iter()
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

fn render_bucket_report_text(
    data: &ScopedBucketData,
    cli: &Cli,
    currency: &str,
    bucket_view: BucketView,
    show_locations: bool,
    all_flows: &[BucketTxFlow],
) -> String {
    let mut out = String::new();
    let _ = writeln!(out, "Bucket: {}", data.bucket);
    let _ = writeln!(out, "Type: {}", data.kind.label());
    let _ = writeln!(
        out,
        "Scope: {} (target month: {})",
        cli.scope.label(),
        cli.month
    );
    let _ = writeln!(out, "Planned: {} {}", fmt_decimal(data.planned), currency);
    let _ = writeln!(out, "Actual:  {} {}", fmt_decimal(data.actual), currency);
    let _ = writeln!(out, "Remain:  {} {}", fmt_decimal(data.remain), currency);

    match bucket_view {
        BucketView::Summary => {}
        BucketView::Monthly => append_bucket_monthly_view(
            &mut out,
            &data.bucket,
            data.kind,
            &cli.month,
            cli.scope,
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
                &cli.month,
                &data.directives,
                &data.flows,
                all_flows,
                true,
            )
        }
    }

    if data.kind == BucketKind::Asset {
        let show_here = match bucket_view {
            BucketView::Summary => show_locations,
            BucketView::Monthly => true,
            BucketView::Detail => false,
        };
        if show_here {
            let locations = collect_asset_locations(&data.bucket, &cli.month, all_flows);
            append_asset_locations_view(&mut out, &cli.month, currency, &locations);
        }
    }

    out
}

fn append_bucket_monthly_view(
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

fn append_bucket_detail_view(
    out: &mut String,
    currency: &str,
    bucket: &str,
    bucket_kind: BucketKind,
    target_month: &str,
    directives: &[BudgetDirective],
    flows: &[BucketTxFlow],
    all_flows: &[BucketTxFlow],
    show_locations_in_detail: bool,
) {
    let _ = writeln!(out, "\n历史明细:");

    let mut months = BTreeSet::new();
    for item in directives {
        months.insert(item.month.clone());
    }
    for flow in flows {
        months.insert(flow.month.clone());
    }

    for month in months {
        let month_budgets = directives
            .iter()
            .filter(|item| item.month == month)
            .collect::<Vec<_>>();
        for item in month_budgets {
            if let Some(label) = item.label.as_ref() {
                let _ = writeln!(
                    out,
                    "{} {}：预算收入 {} {}",
                    item.month,
                    label,
                    fmt_decimal(item.amount),
                    currency
                );
            } else {
                let _ = writeln!(
                    out,
                    "{}：预算收入 {} {}",
                    item.month,
                    fmt_decimal(item.amount),
                    currency
                );
            }
        }

        let mut month_flows = flows
            .iter()
            .filter(|flow| flow.month == month)
            .collect::<Vec<_>>();
        month_flows.sort_by_key(|flow| flow.date);

        for flow in month_flows {
            let action = match bucket_kind {
                BucketKind::Expense => "支出",
                BucketKind::Asset => {
                    if flow.flow.is_sign_positive() {
                        "存入"
                    } else {
                        "转出"
                    }
                }
            };
            let _ = writeln!(
                out,
                "{}：{} {} {} {}",
                flow.date.format("%Y-%m-%d"),
                format_tx_title(flow.payee.as_deref(), flow.narration.as_deref()),
                action,
                fmt_decimal(flow.flow),
                currency
            );
        }
    }

    if bucket_kind == BucketKind::Asset && show_locations_in_detail {
        let locations = collect_asset_locations(bucket, target_month, all_flows);
        append_asset_locations_view(out, target_month, currency, &locations);
    }
}

fn collect_asset_locations(
    bucket: &str,
    target_month: &str,
    flows: &[BucketTxFlow],
) -> BTreeMap<String, Decimal> {
    let mut locations: BTreeMap<String, Decimal> = BTreeMap::new();
    for flow in flows {
        if flow.bucket != bucket || flow.kind != BucketKind::Asset {
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

fn append_asset_locations_view(
    out: &mut String,
    target_month: &str,
    currency: &str,
    locations: &BTreeMap<String, Decimal>,
) {
    let _ = writeln!(out, "\n资产位置（截至 {}）:", target_month);
    if locations.is_empty() {
        let _ = writeln!(out, "(无资产位置数据)");
        return;
    }
    for (account, amount) in locations {
        let _ = writeln!(
            out,
            "{}: {} {}",
            shorten_account_label(account),
            fmt_decimal(*amount),
            currency
        );
    }
}

fn export_reports(
    out_dir: &Path,
    cli: &Cli,
    currency: &str,
    mappings: &BudgetMappings,
    directives: &[BudgetDirective],
    flows: &[BucketTxFlow],
    summaries: &BTreeMap<String, BucketSummary>,
    warnings: &WarningStats,
) -> Result<()> {
    fs::create_dir_all(out_dir)
        .with_context(|| format!("Failed to create output dir: {}", out_dir.display()))?;

    let summary_txt =
        render_summary_report_text(&cli.month, cli.scope, currency, summaries, warnings);
    let summary_md = render_summary_markdown(&cli.month, cli.scope, currency, summaries, warnings);
    let summary_path = out_dir.join(format!("summary-{}-{}.md", cli.month, cli.scope.label()));
    fs::write(&summary_path, summary_md)
        .with_context(|| format!("Failed to write {}", summary_path.display()))?;

    let summary_console_path =
        out_dir.join(format!("summary-{}-{}.txt", cli.month, cli.scope.label()));
    fs::write(&summary_console_path, summary_txt)
        .with_context(|| format!("Failed to write {}", summary_console_path.display()))?;

    let csv_path = out_dir.join(format!("buckets-{}-{}.csv", cli.month, cli.scope.label()));
    fs::write(&csv_path, render_summary_csv(summaries))
        .with_context(|| format!("Failed to write {}", csv_path.display()))?;

    let buckets = collect_buckets_for_export(cli, directives, flows, summaries);
    for bucket in buckets {
        let data = build_scoped_bucket_data(cli, &bucket, mappings, directives, flows);
        let report = render_bucket_markdown(&data, cli, currency, flows);
        let filename = format!(
            "bucket-{}-{}-{}.md",
            sanitize_filename(&bucket),
            cli.month,
            cli.scope.label()
        );
        let path = out_dir.join(filename);
        fs::write(&path, report).with_context(|| format!("Failed to write {}", path.display()))?;

        if data.kind == BucketKind::Asset {
            let locations = collect_asset_locations(&data.bucket, &cli.month, flows);
            let location_report =
                render_asset_locations_markdown(&data.bucket, &cli.month, currency, &locations);
            let location_filename = format!(
                "asset-locations-{}-{}-{}.md",
                sanitize_filename(&bucket),
                cli.month,
                cli.scope.label()
            );
            let location_path = out_dir.join(location_filename);
            fs::write(&location_path, location_report)
                .with_context(|| format!("Failed to write {}", location_path.display()))?;
        }
    }

    Ok(())
}

fn collect_buckets_for_export(
    cli: &Cli,
    directives: &[BudgetDirective],
    flows: &[BucketTxFlow],
    summaries: &BTreeMap<String, BucketSummary>,
) -> BTreeSet<String> {
    if let Some(bucket) = cli.bucket.as_ref() {
        return BTreeSet::from([bucket.clone()]);
    }

    let mut buckets = BTreeSet::new();
    for bucket in summaries.keys() {
        buckets.insert(bucket.clone());
    }
    for item in directives {
        if is_month_in_scope(&item.month, &cli.month, cli.scope) {
            buckets.insert(item.bucket.clone());
        }
    }
    for flow in flows {
        if is_month_in_scope(&flow.month, &cli.month, cli.scope) {
            buckets.insert(flow.bucket.clone());
        }
    }
    buckets
}

fn render_summary_markdown(
    month: &str,
    scope: ReportScope,
    currency: &str,
    summaries: &BTreeMap<String, BucketSummary>,
    warnings: &WarningStats,
) -> String {
    let mut out = String::new();
    let _ = writeln!(out, "# 预算汇总报告");
    let _ = writeln!(out);
    let _ = writeln!(out, "- 月份: `{}`", month);
    let _ = writeln!(out, "- 统计范围: `{}`", scope.label());
    let _ = writeln!(out, "- 币种: `{}`", currency);
    let _ = writeln!(out);
    let _ = writeln!(out, "| 预算桶 | 预算 | 实际 | 结余 | 状态 |");
    let _ = writeln!(out, "|---|---:|---:|---:|---|");

    let mut total_planned = Decimal::ZERO;
    let mut total_actual = Decimal::ZERO;
    for (bucket, summary) in summaries {
        let remain = summary.planned - summary.actual;
        let status = if remain.is_sign_negative() {
            "OVER"
        } else {
            "OK"
        };
        total_planned += summary.planned;
        total_actual += summary.actual;
        let _ = writeln!(
            out,
            "| {} | {} | {} | {} | {} |",
            bucket,
            fmt_decimal(summary.planned),
            fmt_decimal(summary.actual),
            fmt_decimal(remain),
            status
        );
    }

    let total_remain = total_planned - total_actual;
    let total_status = if total_remain.is_sign_negative() {
        "OVER"
    } else {
        "OK"
    };
    let _ = writeln!(
        out,
        "| **TOTAL** | **{}** | **{}** | **{}** | **{}** |",
        fmt_decimal(total_planned),
        fmt_decimal(total_actual),
        fmt_decimal(total_remain),
        total_status
    );

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
            "> WARNING: unknown buckets amount = {} {} (buckets: {})",
            fmt_decimal(warnings.unknown_bucket_amount),
            currency,
            names
        );
    }

    out
}

fn render_summary_csv(summaries: &BTreeMap<String, BucketSummary>) -> String {
    let mut out = String::from("bucket,planned,actual,remain,status\n");
    for (bucket, summary) in summaries {
        let remain = summary.planned - summary.actual;
        let status = if remain.is_sign_negative() {
            "OVER"
        } else {
            "OK"
        };
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

fn render_bucket_markdown(
    data: &ScopedBucketData,
    cli: &Cli,
    currency: &str,
    all_flows: &[BucketTxFlow],
) -> String {
    let mut out = String::new();
    let _ = writeln!(out, "# 预算桶报告：{}", data.bucket);
    let _ = writeln!(out);
    let _ = writeln!(out, "- 类型: `{}`", data.kind.label());
    let _ = writeln!(out, "- 目标月份: `{}`", cli.month);
    let _ = writeln!(out, "- 统计范围: `{}`", cli.scope.label());
    let _ = writeln!(out, "- 预算: `{}` {}", fmt_decimal(data.planned), currency);
    let _ = writeln!(out, "- 实际: `{}` {}", fmt_decimal(data.actual), currency);
    let _ = writeln!(out, "- 结余: `{}` {}", fmt_decimal(data.remain), currency);
    let _ = writeln!(out);

    append_bucket_monthly_view(
        &mut out,
        &data.bucket,
        data.kind,
        &cli.month,
        cli.scope,
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
        &cli.month,
        &data.directives,
        &data.flows,
        all_flows,
        false,
    );

    if data.kind == BucketKind::Asset {
        let locations = collect_asset_locations(&data.bucket, &cli.month, all_flows);
        let _ = writeln!(out);
        let _ = writeln!(out, "## 资产位置");
        let _ = writeln!(out);
        if locations.is_empty() {
            let _ = writeln!(out, "(无资产位置数据)");
        } else {
            let _ = writeln!(out, "| 账户 | 金额 |");
            let _ = writeln!(out, "|---|---:|");
            for (account, amount) in &locations {
                let _ = writeln!(
                    out,
                    "| {} | {} |",
                    shorten_account_label(account),
                    fmt_decimal(*amount)
                );
            }
        }
    }

    out
}

fn render_asset_locations_markdown(
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

fn sanitize_filename(raw: &str) -> String {
    raw.chars()
        .map(|c| match c {
            '/' | '\\' | ':' | '*' | '?' | '"' | '<' | '>' | '|' => '_',
            _ => c,
        })
        .collect::<String>()
}

fn format_tx_title(payee: Option<&str>, narration: Option<&str>) -> String {
    match (payee, narration) {
        (Some(p), Some(n)) => format!("\"{}\" \"{}\"", p, n),
        (Some(p), None) => format!("\"{}\"", p),
        (None, Some(n)) => format!("\"{}\"", n),
        (None, None) => "\"(无标题)\"".to_string(),
    }
}

fn shorten_account_label(account: &str) -> String {
    let parts = account.split(':').collect::<Vec<_>>();
    if parts.len() >= 2 {
        let tail = &parts[parts.len() - 2..];
        return tail.join(":");
    }
    account.to_string()
}

fn month_of_date(date: NaiveDate) -> String {
    format!("{:04}-{:02}", date.year(), date.month())
}

fn is_target_currency(posting_currency: Option<&str>, target_currency: &str) -> bool {
    posting_currency
        .unwrap_or(target_currency)
        .to_ascii_uppercase()
        == target_currency
}

fn is_month_in_scope(month: &str, target_month: &str, scope: ReportScope) -> bool {
    match scope {
        ReportScope::Month => month == target_month,
        ReportScope::Cumulative => month <= target_month,
    }
}

fn fmt_decimal(v: Decimal) -> String {
    format!("{:.2}", v.round_dp(2))
}

#[cfg(test)]
mod tests {
    use super::{
        BucketKind, BudgetDirective, BudgetMappings, ReportScope, collect_bucket_tx_flows,
        default_expense_bucket, is_month_in_scope, parse_budget_key, parse_ledger_content,
        parse_metadata_value, resolve_bucket_by_account, summarize_buckets,
    };
    use rust_decimal_macros::dec;
    use std::{
        collections::BTreeMap,
        fs,
        path::PathBuf,
        time::{SystemTime, UNIX_EPOCH},
    };

    #[test]
    fn parses_budget_metadata_and_expense_posting() {
        let ledger = concat!(
            "2026-05-10 * \"iPad\"\n",
            "  budget: \"数码\"\n",
            "  Expenses:Consume:电子  4999 CNY\n",
            "  Liabilities:CreditCard  -4999 CNY\n",
        );

        let txs = parse_ledger_content(ledger).expect("ledger parse should succeed");
        assert_eq!(txs.len(), 1);
        assert_eq!(
            txs[0].metadata.get("budget").map(String::as_str),
            Some("数码")
        );
        assert_eq!(txs[0].postings.len(), 2);
        assert_eq!(txs[0].postings[0].account, "Expenses:Consume:电子");
        assert_eq!(txs[0].postings[0].amount, Some(dec!(4999)));
        assert_eq!(txs[0].payee.as_deref(), Some("iPad"));
    }

    #[test]
    fn metadata_parser_unquotes_values() {
        assert_eq!(parse_metadata_value("\"electronics\""), "electronics");
        assert_eq!(parse_metadata_value(" 'travel' "), "travel");
        assert_eq!(parse_metadata_value("unquoted"), "unquoted");
    }

    #[test]
    fn longest_prefix_mapping_wins() {
        let mappings = BudgetMappings {
            defaults: BTreeMap::from([
                ("Expenses:Consume".to_string(), "消费".to_string()),
                ("Expenses:Consume:电子".to_string(), "数码".to_string()),
            ]),
            default_expense_bucket: default_expense_bucket(),
            bucket_types: BTreeMap::new(),
            asset_bucket_accounts: BTreeMap::new(),
        };

        let bucket = resolve_bucket_by_account(&mappings, "Expenses:Consume:电子:配件")
            .expect("should match");
        assert_eq!(bucket, "数码");
    }

    #[test]
    fn parses_budget_key_with_optional_label() {
        let (m1, l1) = parse_budget_key("2026-06").expect("valid");
        assert_eq!(m1, "2026-06");
        assert_eq!(l1, None);

        let (m2, l2) = parse_budget_key("2026-06 绩效").expect("valid");
        assert_eq!(m2, "2026-06");
        assert_eq!(l2.as_deref(), Some("绩效"));
    }

    #[test]
    fn untagged_expense_falls_back_to_default_bucket() {
        let tmp = make_temp_file(
            "2026-06-16 * \"中国工商银行\" \"网购\"\n  Expenses:Consume:人情礼物  40 CNY\n  Assets:Bank:ICBC  -40 CNY\n",
        );

        let mappings = BudgetMappings {
            defaults: BTreeMap::new(),
            default_expense_bucket: "生活费".to_string(),
            bucket_types: BTreeMap::new(),
            asset_bucket_accounts: BTreeMap::new(),
        };

        let flows = collect_bucket_tx_flows(&[tmp.clone()], &mappings, "CNY").expect("flows");
        fs::remove_file(tmp).ok();

        assert_eq!(flows.len(), 1);
        assert_eq!(flows[0].bucket, "生活费");
        assert_eq!(flows[0].kind, BucketKind::Expense);
        assert_eq!(flows[0].flow, dec!(-40));
    }

    #[test]
    fn asset_bucket_transfer_is_counted_and_locatable() {
        let tmp = make_temp_file(
            "2026-06-17 * \"中国工商银行\" \"储蓄\"\n  budget: \"储蓄\"\n  Assets:Bank:建设银行:卡号  40 CNY\n  Assets:Bank:工商银行:卡号  -40 CNY\n",
        );

        let mappings = BudgetMappings {
            defaults: BTreeMap::new(),
            default_expense_bucket: "生活费".to_string(),
            bucket_types: BTreeMap::from([("储蓄".to_string(), BucketKind::Asset)]),
            asset_bucket_accounts: BTreeMap::new(),
        };

        let flows = collect_bucket_tx_flows(&[tmp.clone()], &mappings, "CNY").expect("flows");
        fs::remove_file(tmp).ok();

        assert_eq!(flows.len(), 1);
        assert_eq!(flows[0].bucket, "储蓄");
        assert_eq!(flows[0].kind, BucketKind::Asset);
        assert_eq!(flows[0].flow, dec!(40));
        assert_eq!(
            flows[0].location_deltas.get("Assets:Bank:建设银行:卡号"),
            Some(&dec!(40))
        );
    }

    #[test]
    fn summarize_supports_month_and_cumulative_scope() {
        let directives = vec![
            BudgetDirective {
                month: "2026-05".to_string(),
                label: None,
                source_key: "2026-05".to_string(),
                bucket: "旅行".to_string(),
                amount: dec!(3000),
            },
            BudgetDirective {
                month: "2026-06".to_string(),
                label: None,
                source_key: "2026-06".to_string(),
                bucket: "旅行".to_string(),
                amount: dec!(2000),
            },
            BudgetDirective {
                month: "2026-06".to_string(),
                label: Some("绩效".to_string()),
                source_key: "2026-06 绩效".to_string(),
                bucket: "旅行".to_string(),
                amount: dec!(2000),
            },
        ];

        let tmp = make_temp_file(
            "2026-05-16 * \"工行\" \"旅行费用\"\n  budget: \"旅行\"\n  Expenses:Consume:机酒旅行  1000 CNY\n  Assets:Bank:ICBC  -1000 CNY\n",
        );

        let mappings = BudgetMappings {
            defaults: BTreeMap::new(),
            default_expense_bucket: "生活费".to_string(),
            bucket_types: BTreeMap::new(),
            asset_bucket_accounts: BTreeMap::new(),
        };

        let flows = collect_bucket_tx_flows(&[tmp.clone()], &mappings, "CNY").expect("flows");
        fs::remove_file(tmp).ok();

        let month_summary = summarize_buckets(&directives, &flows, "2026-06", ReportScope::Month);
        assert_eq!(month_summary["旅行"].planned, dec!(4000));
        assert_eq!(month_summary["旅行"].actual, dec!(0));

        let cum_summary =
            summarize_buckets(&directives, &flows, "2026-06", ReportScope::Cumulative);
        assert_eq!(cum_summary["旅行"].planned, dec!(7000));
        assert_eq!(cum_summary["旅行"].actual, dec!(1000));
    }

    #[test]
    fn month_scope_filter_works() {
        assert!(is_month_in_scope("2026-06", "2026-06", ReportScope::Month));
        assert!(!is_month_in_scope("2026-05", "2026-06", ReportScope::Month));
        assert!(is_month_in_scope(
            "2026-05",
            "2026-06",
            ReportScope::Cumulative
        ));
    }

    fn make_temp_file(content: &str) -> PathBuf {
        let mut path = std::env::temp_dir();
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("clock")
            .as_nanos();
        path.push(format!("budget_report_test_{}.bean", nonce));
        fs::write(&path, content).expect("write temp file");
        path
    }
}

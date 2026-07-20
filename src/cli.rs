//! 命令行接口定义与账本文件解析。
//!
//! 本模块定义了程序的所有 CLI 参数、统计范围/视图枚举，
//! 以及从命令行输入中解析账本文件路径集合的逻辑。

use std::{
    collections::BTreeSet,
    fs,
    path::{Path, PathBuf},
};

use anyhow::{Context, Result, bail};
use chrono::Datelike;
use clap::{Parser, ValueEnum};

/// CLI 参数集合，通过 `clap` derive 宏自动解析。
#[derive(Parser, Debug)]
#[command(name = "beancount-budget-tool")]
#[command(about = "Budget report tool for Beancount ledgers")]
#[command(version)]
pub struct Cli {
    /// 账本文件路径（可重复传入多个）
    #[arg(long = "ledger", short = 'l')]
    pub ledgers: Vec<PathBuf>,

    /// 账本目录（可重复传入多个，递归扫描 *.bean/*.beancount）
    #[arg(long = "ledger-dir")]
    pub ledger_dirs: Vec<PathBuf>,

    /// 统计月份（YYYY-MM）。与 --from/--to 互斥
    #[arg(long, short = 'm')]
    pub month: Option<String>,

    /// 预算配置文件（纯预算, 不含跟踪桶）
    #[arg(long, required = true)]
    pub budgets: PathBuf,

    /// 全局配置文件（账户映射、桶类型、跟踪桶等），--mappings 为旧名，仍可用
    #[arg(long = "config", short = 'c', visible_alias = "mappings", required = true)]
    pub config_file: PathBuf,

    /// 统计币种（默认 CNY）
    #[arg(long, default_value = "CNY")]
    pub currency: String,

    /// 统计范围：month（仅目标月）或 cumulative（截至目标月累计）
    #[arg(long, value_enum, default_value_t = ReportScope::Month)]
    pub scope: ReportScope,

    /// 指定预算桶名称；设置后输出该桶的历史查询
    #[arg(long)]
    pub bucket: Option<String>,

    /// 预算桶历史视图：summary（汇总）/monthly（分月）/detail（明细）
    #[arg(long, value_enum, default_value_t = BucketView::Summary)]
    pub bucket_view: BucketView,

    /// 对资产类预算桶显示"资金当前位置"（截至目标月）
    #[arg(long)]
    pub show_locations: bool,

    /// 输出目录；设置后自动生成 markdown/csv 报告文件
    #[arg(long = "out-dir")]
    pub out_dir: Option<PathBuf>,

    /// 严格模式：若存在未知预算桶则返回非零退出码
    #[arg(long)]
    pub strict: bool,

    /// 关键词过滤：仅显示 payee / narration / metadata 包含该关键词的交易
    #[arg(long)]
    pub filter: Option<String>,

    /// 明细视图中隐藏资产间转移记录，仅显示预算收入和实际支出
    #[arg(long = "hide-asset-flows")]
    pub hide_asset_flows: bool,

    /// 汇总表中展开显示所有子桶（不折叠到父桶）
    #[arg(long)]
    pub expand: bool,

    /// 统计起始月份（YYYY-MM）。与 --month 互斥，配合 --to 使用
    #[arg(long = "from")]
    pub from: Option<String>,

    /// 统计结束月份（YYYY-MM）。与 --month 互斥，配合 --from 使用
    #[arg(long = "to")]
    pub to: Option<String>,

    /// 快捷年份（YYYY）：等价于 --from YYYY-01 --to YYYY-12。与 --month/--from/--to 互斥
    #[arg(long = "year")]
    pub year: Option<String>,

    /// 汇总表排序方式 [default: name] [possible values: name, planned, actual, remain]
    #[arg(long = "sort-by")]
    pub sort_by: Option<String>,

    /// 额外生成月度横向透视 CSV（行为月份，列为桶名）
    #[arg(long = "csv-pivot")]
    pub csv_pivot: bool,

    /// 额外生成 JSON 格式报告
    #[arg(long = "out-json")]
    pub out_json: bool,

    /// 同比/环比对比月份（YYYY-MM）：在同一表内对比当前区间与历史区间的数据
    #[arg(long = "compare")]
    pub compare: Option<String>,
}

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
    /// 返回范围的短标签，用于文件名和报告输出。
    pub fn label(self) -> &'static str {
        match self {
            Self::Month => "month",
            Self::Cumulative => "cumulative",
        }
    }
}

/// 预算桶的历史视图粒度。
///
/// - `Summary`: 仅显示汇总统计。
/// - `Monthly`: 按月拆分显示。
/// - `Detail`: 显示每笔交易明细。
#[derive(Debug, Clone, Copy, PartialEq, Eq, ValueEnum)]
pub enum BucketView {
    Summary,
    Monthly,
    Detail,
}

/// 查询时间范围。
///
/// 用于替代 `--month` + `--scope` 的灵活时间段统计。
/// - `Month { target, scope }`: 原有月度/累计模式
/// - `Range { from, to }`: 起止日期（精确到天）
#[derive(Debug, Clone)]
pub enum DateRange {
    Month { target: String, scope: ReportScope },
    Range { from: chrono::NaiveDate, to: chrono::NaiveDate },
}

impl DateRange {
    /// 判断给定日期是否在范围内。
    pub fn contains_date(&self, date: chrono::NaiveDate) -> bool {
        match self {
            DateRange::Month { target, scope } => {
                let month = chrono::NaiveDate::parse_from_str(&format!("{}-01", target), "%Y-%m-%d")
                    .map(|d| format!("{:04}-{:02}", d.year(), d.month()))
                    .unwrap_or_default();
                crate::util::is_month_in_scope(&month, target, *scope)
            }
            DateRange::Range { from, to } => date >= *from && date <= *to,
        }
    }

    /// 判断给定月份字符串是否在范围内（向后兼容）。
    pub fn contains(&self, month: &str) -> bool {
        match self {
            DateRange::Month { target, scope } => crate::util::is_month_in_scope(month, target, *scope),
            DateRange::Range { from, to } => {
                let f = format!("{:04}-{:02}", from.year(), from.month());
                let t = format!("{:04}-{:02}", to.year(), to.month());
                month >= f.as_str() && month <= t.as_str()
            }
        }
    }

    /// 生成范围标签，用于报告标题和文件名。
    pub fn label(&self) -> String {
        match self {
            DateRange::Month { target, scope } => format!("{}-{}", target, scope.label()),
            DateRange::Range { from, to } => format!("{}_{}", from, to),
        }
    }

    /// 生成范围标签的中文形式。
    pub fn display(&self) -> String {
        match self {
            DateRange::Month { target, scope } => {
                format!("{} ({})", target, scope.label())
            }
            DateRange::Range { from, to } => {
                format!("{} ~ {}", from, to)
            }
        }
    }

    /// 范围对应的结束月份（用于资产位置累计等）。
    pub fn end_month(&self) -> String {
        match self {
            DateRange::Month { target, .. } => target.clone(),
            DateRange::Range { to, .. } => format!("{:04}-{:02}", to.year(), to.month()),
        }
    }
}

/// 报告配置：从 CLI 中提取，解耦核心逻辑与命令行层。
#[derive(Debug, Clone)]
pub struct ReportConfig {
    pub scope: ReportScope,
    pub month: String,
    pub filter: Option<String>,
    pub hide_asset_flows: bool,
    pub bucket_view: BucketView,
    pub show_locations: bool,
    pub sort_by: Option<String>,
    pub csv_pivot: bool,
    pub out_json: bool,
    pub bucket: Option<String>,
    pub expand: bool,
    pub strict: bool,
}

impl Cli {
    pub fn report_config(&self) -> ReportConfig {
        ReportConfig {
            scope: self.scope,
            month: self.month.clone().unwrap_or_default(),
            filter: self.filter.clone(),
            hide_asset_flows: self.hide_asset_flows,
            bucket_view: self.bucket_view,
            show_locations: self.show_locations,
            sort_by: self.sort_by.clone(),
            csv_pivot: self.csv_pivot,
            out_json: self.out_json,
            bucket: self.bucket.clone(),
            expand: self.expand,
            strict: self.strict,
        }
    }
}

/// 从 CLI 参数中解析所有账本文件路径。
///
/// 支持通过 `--ledger` 指定单个文件和 `--ledger-dir` 递归扫描目录。
/// 扩展名 `.bean` 或 `.beancount`（不区分大小写）会被自动收集。
pub fn resolve_ledger_inputs(cli: &Cli) -> Result<Vec<PathBuf>> {
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

/// 递归扫描目录，收集所有 `.bean` / `.beancount` 文件到 `out` 集合。
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

//! TUI 交互界面 — 基于 ratatui 的终端预算查看器。
//!
//! 所有参数通过键盘切换，底层调用 `app(cli)` 获取报告内容。
//! 当前选择通过顶部状态栏 + 底部快捷键栏显示。

use std::{
    io,
    path::{Path, PathBuf},
};

use chrono::{Datelike, NaiveDate};
use crossterm::{
    event::{self, Event, KeyCode, KeyEventKind},
    execute,
    terminal::{disable_raw_mode, enable_raw_mode, EnterAlternateScreen, LeaveAlternateScreen},
};
use ratatui::{
    prelude::*,
    widgets::*,
};

use crate::cli::{BucketView, Cli};
use crate::util::ReportScope;

/// 运行时保存的 TUI 配置
#[derive(serde::Deserialize, serde::Serialize, Default)]
struct TuiConfig {
    budgets: Option<String>,
    config: Option<String>,
    ledger_dir: Option<String>,
    currency: Option<String>,
}

/// TUI 状态
struct App {
    // 参数
    month: chrono::NaiveDate,     // 选中的月份（日固定为 1）
    scope: ReportScope,
    bucket: Option<String>,
    sort_by: Option<String>,      // name | planned | actual | remain
    expand: bool,
    filter: Option<String>,
    show_locations: bool,
    hide_asset_flows: bool,
    compare: Option<String>,
    out_dir: Option<String>,
    csv_pivot: bool,
    out_json: bool,
    strict: bool,
    bucket_view: BucketView,

    // 路径
    budgets_path: PathBuf,
    config_path: PathBuf,
    ledger_dir: PathBuf,
    currency: String,

    // 报告
    report_text: String,
    report_lines: Vec<String>,
    scroll: usize,
    run_error: Option<String>,
    status: String,

    // 日期范围模式
    range_mode: bool,     // true = from/to, false = single month
    adjusting_from: bool, // in range mode, which date to adjust
    from_date: chrono::NaiveDate,
    to_date: chrono::NaiveDate,

    // 选择
    filter_editing: bool,
    filter_buf: String,
    bucket_picker: bool,
    date_picker: Option<String>, // Some("from") / Some("to") / None

    // 界面
    running: bool,
}

impl App {
    fn new() -> io::Result<Self> {
        let cfg = load_config().unwrap_or_default();
        let now = chrono::Local::now().naive_local().date();
        let month = NaiveDate::from_ymd_opt(now.year(), now.month(), 1).unwrap_or(NaiveDate::from_ymd_opt(2026, 1, 1).unwrap());
        let from_date = NaiveDate::from_ymd_opt(now.year(), 1, 1).unwrap_or(month);
        let to_date = month;

        let budgets_path = cfg.budgets.map(PathBuf::from).unwrap_or_else(|| PathBuf::from("budgets.yml"));
        let config_path = cfg.config.map(PathBuf::from).unwrap_or_else(|| PathBuf::from("config.yml"));
        let ledger_dir = cfg.ledger_dir.map(PathBuf::from).unwrap_or_else(|| PathBuf::from("."));
        let currency = cfg.currency.unwrap_or_else(|| "CNY".to_string());

        Ok(App {
            month,
            scope: ReportScope::Month,
            bucket: None,
            sort_by: None,
            expand: false,
            filter: None,
            show_locations: false,
            hide_asset_flows: false,
            compare: None,
            out_dir: None,
            csv_pivot: false,
            out_json: false,
            strict: false,
            bucket_view: BucketView::Summary,
            budgets_path,
            config_path,
            ledger_dir,
            currency,
            report_text: String::new(),
            report_lines: Vec::new(),
            scroll: 0,
            run_error: None,
            status: String::from("按 r 运行"),
            range_mode: false,
            adjusting_from: true,
            from_date,
            to_date,
            filter_editing: false,
            filter_buf: String::new(),
            bucket_picker: false,
            date_picker: None,
            running: true,
        })
    }

    fn month_str(&self) -> String {
        format!("{:04}-{:02}", self.month.year(), self.month.month())
    }

    fn build_cli(&self, ledgers: Vec<PathBuf>) -> Cli {
        let (month, from, to) = if self.range_mode {
            (None, Some(format!("{:04}-{:02}", self.from_date.year(), self.from_date.month())), Some(format!("{:04}-{:02}", self.to_date.year(), self.to_date.month())))
        } else {
            (Some(self.month_str()), None, None)
        };
        Cli {
            ledgers,
            ledger_dirs: vec![self.ledger_dir.clone()],
            month,
            budgets: self.budgets_path.clone(),
            config_file: self.config_path.clone(),
            currency: self.currency.clone(),
            scope: self.scope,
            bucket: self.bucket.clone(),
            bucket_view: self.bucket_view,
            sort_by: self.sort_by.clone(),
            expand: self.expand,
            filter: self.filter.clone(),
            show_locations: self.show_locations,
            hide_asset_flows: self.hide_asset_flows,
            compare: self.compare.clone(),
            out_dir: self.out_dir.as_ref().map(PathBuf::from),
            csv_pivot: self.csv_pivot,
            out_json: self.out_json,
            strict: self.strict,
            from,
            to,
            year: None,
        }
    }
}

fn load_config() -> Option<TuiConfig> {
    let paths = [
        PathBuf::from("budget-tool.toml"),
        dirs_next().unwrap_or_default().join(".config").join("beancount-budget-tool").join("config.toml"),
    ];
    for p in &paths {
        if let Ok(content) = std::fs::read_to_string(p) {
            if let Ok(cfg) = toml::from_str(&content) {
                return Some(cfg);
            }
        }
    }
    None
}

fn dirs_next() -> Option<PathBuf> {
    std::env::var("HOME").or_else(|_| std::env::var("USERPROFILE")).ok().map(PathBuf::from)
}

pub fn run_tui(base_dir: &Path) -> anyhow::Result<()> {
    std::env::set_current_dir(base_dir)?;
    enable_raw_mode()?;
    let mut stdout = io::stdout();
    execute!(stdout, EnterAlternateScreen)?;
    let backend = ratatui::backend::CrosstermBackend::new(stdout);
    let mut terminal = Terminal::new(backend)?;

    let mut app = App::new()?;

    while app.running {
        terminal.draw(|f| draw(f, &app))?;
        handle_input(&mut app)?;
    }

    disable_raw_mode()?;
    execute!(terminal.backend_mut(), LeaveAlternateScreen)?;
    Ok(())
}

fn draw(f: &mut Frame, app: &App) {
    let chunks = Layout::vertical([
        Constraint::Length(3),  // status bar
        Constraint::Fill(1),     // report
        Constraint::Length(2),  // help bar
    ]).split(f.area());

    // Status bar
    let scope_label = match app.scope { ReportScope::Month => "当月", ReportScope::Cumulative => "累计" };
    let bucket_label = app.bucket.as_deref().unwrap_or("全部");
    let sort_label = app.sort_by.as_deref().unwrap_or("name");
    let expand_label = if app.expand { "展开" } else { "折叠" };
    let filter_label = if app.filter.is_some() { "🔍" } else { "" };

    let date_label = if app.range_mode {
        let adj = if app.adjusting_from { "←FROM→" } else { "←TO→" };
        format!("{}_{} [Tab:{}]", app.from_date.format("%Y-%m-%d"), app.to_date.format("%Y-%m-%d"), adj)
    } else {
        app.month_str()
    };

    let status = format!(
        " {} ({}) | 桶: {} | 排序: {} | {} {} | {} ",
        date_label, scope_label, bucket_label, sort_label, expand_label, filter_label, app.status,
    );
    let status_block = Paragraph::new(status)
        .style(Style::default().bg(Color::DarkGray).fg(Color::White))
        .block(Block::default());
    f.render_widget(status_block, chunks[0]);

    // Report
    let report_block = Paragraph::new(app.report_text.clone())
        .scroll((app.scroll as u16, 0))
        .block(Block::default().borders(Borders::NONE));
    f.render_widget(report_block, chunks[1]);

    // Help bar
    let help = if app.filter_editing {
        format!(" 输入过滤关键词: {}_ (回车确认, Esc取消) ", app.filter_buf)
    } else if app.bucket_picker {
        format!(" 输入桶名: {}_ (回车确认, Esc取消) ", app.filter_buf)
    } else if let Some(ref which) = app.date_picker {
        format!(" 输入{}日期 (YYYY-MM): {}_ (回车确认, Esc取消) ", which, app.filter_buf)
    } else if app.range_mode {
        format!(" {} ←→调整日期 d输入 Tab切From/To t:月模式 | s排序 e展开 f过滤 r运行 q退出 ", if app.adjusting_from { "调整 FROM" } else { "调整 TO" })
    } else {
        " ←→ 月 | d 跳转 | Tab scope | t 范围 | s 排序 | e 展开 | f 过滤 | b 桶 | v 视图 | ↑↓滚 | r 运行 | q 退出 ".to_string()
    };
    let help_block = Paragraph::new(help)
        .style(Style::default().bg(Color::DarkGray).fg(Color::White));
    f.render_widget(help_block, chunks[2]);
}

fn handle_input(app: &mut App) -> io::Result<()> {
    if event::poll(std::time::Duration::from_millis(50))? {
        if let Event::Key(key) = event::read()? {
            if key.kind != KeyEventKind::Press {
                return Ok(());
            }

            if app.filter_editing {
                match key.code {
                    KeyCode::Enter => {
                        app.filter = if app.filter_buf.is_empty() { None } else { Some(app.filter_buf.clone()) };
                        app.filter_editing = false;
                        app.filter_buf.clear();
                    }
                    KeyCode::Esc => {
                        app.filter = None;
                        app.filter_editing = false;
                        app.filter_buf.clear();
                    }
                    KeyCode::Backspace => { app.filter_buf.pop(); }
                    KeyCode::Char(c) => { app.filter_buf.push(c); }
                    _ => {}
                }
                return Ok(());
            }

            if app.bucket_picker {
                match key.code {
                    KeyCode::Enter => {
                        app.bucket = if app.filter_buf.is_empty() { None } else { Some(app.filter_buf.clone()) };
                        app.bucket_picker = false;
                        app.filter_buf.clear();
                    }
                    KeyCode::Esc => {
                        app.bucket = None;
                        app.bucket_picker = false;
                        app.filter_buf.clear();
                    }
                    KeyCode::Backspace => { app.filter_buf.pop(); }
                    KeyCode::Char(c) => { app.filter_buf.push(c); }
                    _ => {}
                }
                return Ok(());
            }

            if app.date_picker.is_some() {
                match key.code {
                    KeyCode::Enter => {
                        if let Ok(d) = chrono::NaiveDate::parse_from_str(
                            &format!("{}-01", app.filter_buf), "%Y-%m-%d",
                        ) {
                            if app.date_picker.as_deref() == Some("from") {
                                app.from_date = d;
                            } else if app.date_picker.as_deref() == Some("to") {
                                app.to_date = d;
                            } else {
                                // month mode: jump directly
                                app.month = d;
                            }
                        }
                        app.date_picker = None;
                        app.filter_buf.clear();
                    }
                    KeyCode::Esc => {
                        app.date_picker = None;
                        app.filter_buf.clear();
                    }
                    KeyCode::Backspace => { app.filter_buf.pop(); }
                    KeyCode::Char(c) => { app.filter_buf.push(c); }
                    _ => {}
                }
                return Ok(());
            }

            match key.code {
                KeyCode::Char('q') => app.running = false,
                KeyCode::Char('r') => run_report(app),
                KeyCode::Char('t') => {
                    app.range_mode = !app.range_mode;
                    if app.range_mode {
                        app.adjusting_from = true;
                    }
                }
                KeyCode::Char('d') if !app.range_mode => {
                    app.date_picker = Some("month".to_string());
                    app.filter_buf = app.month_str();
                }
                KeyCode::Char('d') if app.range_mode => {
                    let which = if app.adjusting_from { "from" } else { "to" };
                    app.date_picker = Some(which.to_string());
                    app.filter_buf = match which {
                        "from" => format!("{:04}-{:02}", app.from_date.year(), app.from_date.month()),
                        "to" => format!("{:04}-{:02}", app.to_date.year(), app.to_date.month()),
                        _ => String::new(),
                    };
                }
                KeyCode::Enter if app.range_mode => {
                    let which = if app.adjusting_from { "from" } else { "to" };
                    app.date_picker = Some(which.to_string());
                    app.filter_buf = match which {
                        "from" => format!("{:04}-{:02}", app.from_date.year(), app.from_date.month()),
                        "to" => format!("{:04}-{:02}", app.to_date.year(), app.to_date.month()),
                        _ => String::new(),
                    };
                }
                KeyCode::Left => {
                    if app.range_mode {
                        if app.adjusting_from {
                            app.from_date = app.from_date.pred_opt().unwrap_or(app.from_date);
                        } else {
                            app.to_date = app.to_date.pred_opt().unwrap_or(app.to_date);
                        }
                    } else {
                        app.month = app.month.pred_opt().unwrap_or(app.month);
                    }
                }
                KeyCode::Right => {
                    if app.range_mode {
                        if app.adjusting_from {
                            let next = app.from_date.succ_opt().unwrap_or(app.from_date);
                            if next <= app.to_date { app.from_date = next; }
                        } else {
                            let next = app.to_date.succ_opt().unwrap_or(app.to_date);
                            if next <= chrono::Local::now().naive_local().date() { app.to_date = next; }
                        }
                    } else {
                        let next = app.month.succ_opt().unwrap_or(app.month);
                        if next <= chrono::Local::now().naive_local().date() {
                            app.month = next;
                        }
                    }
                }
                KeyCode::Tab => {
                    if app.range_mode {
                        app.adjusting_from = !app.adjusting_from;
                    } else {
                        app.scope = match app.scope {
                            ReportScope::Month => ReportScope::Cumulative,
                            ReportScope::Cumulative => ReportScope::Month,
                        };
                    }
                }
                KeyCode::Char('s') => {
                    app.sort_by = match app.sort_by.as_deref() {
                        None | Some("name") => Some("planned".into()),
                        Some("planned") => Some("actual".into()),
                        Some("actual") => Some("remain".into()),
                        _ => None,
                    };
                }
                KeyCode::Char('e') => app.expand = !app.expand,
                KeyCode::Char('f') => {
                    app.filter_editing = true;
                    app.filter_buf = app.filter.as_deref().unwrap_or("").to_string();
                }
                KeyCode::Char('b') => {
                    app.bucket_picker = true;
                    app.filter_buf = app.bucket.as_deref().unwrap_or("").to_string();
                }
                KeyCode::Char('l') => app.show_locations = !app.show_locations,
                KeyCode::Char('h') => app.hide_asset_flows = !app.hide_asset_flows,
                KeyCode::Char('v') => {
                    app.bucket_view = match app.bucket_view {
                        BucketView::Summary => BucketView::Monthly,
                        BucketView::Monthly => BucketView::Detail,
                        BucketView::Detail => BucketView::Summary,
                    };
                }
                KeyCode::Char('c') => {
                    if app.compare.is_some() {
                        app.compare = None;
                    } else {
                        app.compare = Some(app.month_str());
                    }
                }
                KeyCode::Char('o') => {
                    if app.out_dir.is_some() {
                        app.out_dir = None;
                    } else {
                        app.out_dir = Some("./reports".to_string());
                    }
                }
                KeyCode::Char('p') => app.csv_pivot = !app.csv_pivot,
                KeyCode::Char('j') => app.out_json = !app.out_json,
                KeyCode::Up => { if app.scroll > 0 { app.scroll -= 1; } }
                KeyCode::Down => { app.scroll += 1; }
                KeyCode::PageUp => { app.scroll = app.scroll.saturating_sub(20); }
                KeyCode::PageDown => { app.scroll += 20; }
                _ => {}
            }
        }
    }
    Ok(())
}

fn run_report(app: &mut App) {
    app.status = "运行中...".into();
    app.run_error = None;

    // Resolve ledgers first
    let cli = app.build_cli(vec![]);
    let ledgers = match crate::cli::resolve_ledger_inputs(&cli) {
        Ok(l) => l,
        Err(e) => {
            app.run_error = Some(format!("{}", e));
            app.status = format!("错误: {}", e);
            return;
        }
    };

    let cli = app.build_cli(ledgers);

    let budget_directives = match crate::config::load_budget_directives(&cli.budgets) {
        Ok(d) => d,
        Err(e) => {
            app.run_error = Some(format!("加载预算失败: {}", e));
            app.status = "错误: budgets".into();
            return;
        }
    };
    let mappings = match crate::config::load_config(&cli.config_file) {
        Ok(m) => m,
        Err(e) => {
            app.run_error = Some(format!("加载配置失败: {}", e));
            app.status = "错误: config".into();
            return;
        }
    };

    let month_str = cli.month.as_deref().unwrap_or("?");

    // 范围模式下 scope 强制 Cumulative, month 设为 to
    let report_scope = if cli.from.is_some() { ReportScope::Cumulative } else { cli.scope };
    let report_month = if cli.from.is_some() {
        cli.to.clone().unwrap_or_else(|| month_str.to_string())
    } else {
        month_str.to_string()
    };

    let range = if cli.from.is_some() && cli.to.is_some() {
        let from = chrono::NaiveDate::parse_from_str(
            &format!("{}-01", cli.from.as_deref().unwrap()), "%Y-%m-%d"
        ).unwrap_or(chrono::NaiveDate::from_ymd_opt(2023, 1, 1).unwrap());
        let to_end = chrono::NaiveDate::parse_from_str(
            &format!("{}-01", cli.to.as_deref().unwrap()), "%Y-%m-%d"
        ).ok()
        .and_then(|d| d.checked_add_months(chrono::Months::new(1)))
        .and_then(|n| n.pred_opt())
        .unwrap_or(chrono::Local::now().naive_local().date());
        crate::cli::DateRange::Range { from, to: to_end }
    } else {
        crate::cli::DateRange::Month {
            target: report_month.to_string(),
            scope: report_scope,
        }
    };

    let budget_directives = crate::filter_directives_by_range(budget_directives, &range);
    let all_known = crate::config::collect_known_buckets(&budget_directives, &mappings);
    let tx_flows = match crate::budget::collect_bucket_tx_flows(
        &cli.ledgers, &mappings, &cli.currency, &all_known,
    ) {
        Ok(f) => crate::filter_flows_by_range(f, &range),
        Err(e) => {
            app.run_error = Some(format!("解析账本失败: {}", e));
            app.status = "错误: ledger".into();
            return;
        }
    };

    let summaries = crate::budget::summarize_buckets(
        &budget_directives, &tx_flows, &report_month, report_scope, &mappings,
    );
    let known = crate::config::collect_known_buckets(&budget_directives, &mappings);
    let warnings = crate::budget::collect_scope_warnings(
        &tx_flows, &known, &report_month, report_scope,
    );

    let mut config = cli.report_config();
    config.scope = report_scope;
    config.month = report_month;

    let mut captured = Vec::new();
    {
        use std::io::Write;
        if let Some(bucket) = &config.bucket {
            let data = crate::budget::build_scoped_bucket_data(
                &config, bucket, &mappings, &budget_directives, &tx_flows,
            );
            let text = crate::report::render_bucket_report_text(
                &data, &config, &cli.currency, app.bucket_view, app.show_locations,
                &tx_flows, &range,
            );
            write!(captured, "{}", text).ok();
        } else {
            let text = crate::report::render_summary_report_text(
                &range, &cli.currency, &summaries, &warnings,
                config.sort_by.as_deref(), config.expand,
            );
            write!(captured, "{}", text).ok();

            if !warnings.unknown_bucket_amount.is_zero() {
                let names = warnings.unknown_bucket_names.iter().cloned().collect::<Vec<_>>().join(", ");
                write!(captured, "\n警告: unknown buckets amount = {:.2} CNY (buckets: {})",
                    warnings.unknown_bucket_amount, names).ok();
            }
        }
    }

    app.report_text = String::from_utf8_lossy(&captured).to_string();
    app.report_lines = app.report_text.lines().map(|s| s.to_string()).collect();
    app.scroll = 0;

    let bucket_view_label = match app.bucket_view {
        BucketView::Summary => "汇总",
        BucketView::Monthly => "分月",
        BucketView::Detail => "明细",
    };
    let bucket_label = app.bucket.as_deref().unwrap_or("全部");
    if bucket_label != "全部" {
        app.status = format!("{} ({})", bucket_label, bucket_view_label);
    } else {
        app.status = format!("{} ✓", app.month_str());
    }
}

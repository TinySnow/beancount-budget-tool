//! 报告渲染与导出模块。
//!
//! 终端文本格式见 `text.rs`，Markdown 见 `md.rs`，
//! CSV 见 `csv.rs`，JSON 见 `json.rs`。

pub mod text;
pub mod md;
pub mod csv;
pub mod json;

mod shared;
pub(crate) use shared::export_reports;

// 重新导出所有公共渲染函数
pub use text::{
    render_summary_report_text,
    render_compare_report_text,
    render_bucket_report_text,
};

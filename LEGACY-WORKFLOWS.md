# 旧工作流与迁移

旧操作入口已下线。

正常运行只维护账本、config.yml、budgets.yml。独立审阅/批准保存及 state、clean、review、project 等旧命令已退役；TUI e/f 仅显示来源和配置指引。

旧 month.yaml 可用 configure --from-config 显式转换为两份 YAML，复杂协议成为可见 declarations，不生成隐藏状态。程序不会发现或读取旧 reviews.json.gz，也不会删除用户旧数据。已有隐藏批准必须由用户在账本/YAML重新明确，缺失时保持待审。详见 [STATELESS.md](docs/STATELESS.md)。

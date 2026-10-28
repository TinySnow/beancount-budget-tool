# 旧操作入口已下线

现在只维护 `config.yml`、`budgets.yml` 与账本。操作见 [QUICKSTART](QUICKSTART.md)，字段见[配置参考](docs/CONFIGURATION.md)。

`init`、`next-month`、`project`、`review`、`review-month`、`review-refs` 及一次性迁移脚本不再发布。TUI 直接切月，`e` 审阅，`f` 确认月末；引用诊断使用 `state --config config.yml --check`。

已有 schema 6 快照可用 `configure --from-config 最新month.yaml --output 新目录` 复制迁移。普通负债现在统一按逐笔 budget 责任用途清算。旧整户 purpose 须删除；如果旧负债 budget 只是“偿债／借款”等分类，应先核对并改为实际责任用途。不能仅改一个开关就认定旧标签含义相同。

复杂协议的导出、来源校验见[高级输入说明](docs/v6-monthly-report.md)。旧版本及工具历史仍可从 Git 历史查阅。

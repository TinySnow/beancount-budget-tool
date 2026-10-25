# beancount-budget-tool

从 Beancount 账本核对预算、资金用途和可花金额。当前为 v6 开发预览。

日常只编辑 **`config.yml`、`budgets.yml` 和你自己的账本**。运行后得到已核对月报，或带文件、行号的待审清单。无需手工编译、维护内部 ID 或逐月创建配置。

| 你要做什么 | 看这里 |
|---|---|
| Windows exe／Mac 从零开始，逐月清理历史账本 | [操作步骤](QUICKSTART.md) |
| 查询每个配置项、默认值、目标含义 | [配置参考](docs/CONFIGURATION.md) |
| 清理旧缓存，判断哪些文件必须保留 | [文件与清理](QUICKSTART.md#哪些文件要保留) |
| 使用旧 month.yaml、分项工具或高级协议 | [旧入口兼容说明](LEGACY-WORKFLOWS.md)、[高级月报](docs/v6-monthly-report.md) |
| 参与开发、核对需求 | [开发文档导航](docs/README.md) |

已有旧月报配置，复制迁移一次：

```bash
python -m beancount_budget_tool configure --from-config budget/2023-07/compiled/month.yaml --output budget/simple
python -m beancount_budget_tool --config budget/simple/config.yml --month 2023-07 --format tui
```

原配置保留。Windows exe 将 `python -m beancount_budget_tool` 换成 PowerShell 的 `& $Exe`。

TUI：`r` 重算，`m` 跳月，`n` 下一月，`Enter` 看完整来源，`?` 看帮助。明确的 `fund` 新用途自动识别；缺少金额、用途或证据时继续待审，不把计划变成现金。

## 哪些文件不能删

- 目录版 exe 旁边的 `_internal` 是运行依赖，必须和 exe 一起保留。
- `.budget-state` 中的 `reviews.json.gz` 保存已审决定、月末确认等数据，应随账本备份。
- 新版生成的运行输入在系统临时目录，正常重算／退出后自动释放。旧缓存可退出 TUI 后运行 `clean --config config.yml` 清理；不要直接删整个状态目录。

## 源码安装与示例

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m beancount_budget_tool --config examples/v6/simple/config.yml --format tui
```

虚拟示例应显示最多能花 **190 CNY**。Windows 可用 `py -3.12 -m venv .venv` 和 `.\.venv\Scripts\python.exe`；也可直接使用 [Actions 构建的 exe](https://github.com/TinySnow/beancount-budget-tool/actions/workflows/windows-exe.yml)。

运行测试：`.venv/bin/python -m unittest discover -s tests`。真实账本和私人审阅数据不随项目发布。

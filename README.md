# beancount-budget-tool

> **分支 `dpsk/v6`**：新增 `bgbudget/` —— 单引擎预算工具（一个引擎、两种视图：轻量视图与 v6 已审月报逐行一致；
> 完整视图实现 REQUIREMENTS 的全部语义）。推送本分支会自动构建 macOS / Linux / Windows 单文件可执行程序
> （workflow：`.github/workflows/bgbudget-exe.yml`）。用法与边界见 [bgbudget/README.md](bgbudget/README.md)。


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

[Actions 可执行包](https://github.com/TinySnow/beancount-budget-tool/actions/workflows/windows-exe.yml)提供 Windows x64，以及 Mac 的 Apple Silicon（arm64）／Intel（x64）单文件版。Mac 解压 Artifact 后再解压 tar.gz，在终端使用 `./beancount-budget-tool`；可单独移动程序，无需外置 `_internal` 或安装 Python。详见 [Mac 启动说明](packaging/MACOS-START.md)。

TUI：`r` 重算，`m` 跳月，`n` 下一月，`Enter` 看完整来源，`?` 看帮助。明确的 `fund` 新用途自动识别；缺少金额、用途或证据时继续待审，不把计划变成现金。

消费上限可按实际发薪周期：config.yml 设置 `expense_budget_basis: salary_cycle`，budgets.yml 的 `salary_cycles` 保存日期和额度。账务仍逐月核对，最多能花取当前周期剩余额度与已审可支付资金的较小值，额度不在月初重置。TUI `w` 直接选保存的周期，CLI 可用 `report --config config.yml --cycle current`。

## 哪些文件不能删

- 目录版 exe 旁边的 `_internal` 是运行依赖，必须和 exe 一起保留。
- Windows 单文件版不需要 `_internal`，会在用户 LocalAppData 的程序专用 Runtime 目录自动解包，正常退出清理本次目录；目录兼容包仍提供。
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

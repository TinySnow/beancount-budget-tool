# beancount-budget-tool

从 Beancount 账本核对预算、资金用途和可花金额。当前为 v6 开发预览。

日常只编辑 **`config.yml`、`budgets.yml` 和你自己的账本**。运行后得到已核对月报，或带文件、行号的待审清单。无需手工编译、维护内部 ID 或逐月创建配置。

| 你要做什么 | 看这里 |
|---|---|
| Windows exe／Mac 从零开始，逐月清理历史账本 | [操作步骤](QUICKSTART.md) |
| 查询每个配置项、默认值、目标含义 | [配置参考](docs/CONFIGURATION.md) |
| 了解零隐藏状态与旧版本差异 | [两文件运行合同](docs/STATELESS.md) |
| 查询复杂协议的显式声明 | [配置参考](docs/CONFIGURATION.md) |
| 理解代码结构与本轮删减 | [架构导读](docs/ARCHITECTURE.md) |
| 参与开发、核对需求 | [开发文档导航](docs/README.md) |

从零创建两份配置：

```bash
python -m beancount_budget_tool configure --ledger main.bean --month 2023-07 --output budget/simple
python -m beancount_budget_tool --config budget/simple/config.yml --month 2023-07 --format tui
```

创建后按操作步骤填写账户与预算目标；已有旧配置可查[迁移说明](LEGACY-WORKFLOWS.md)。Windows exe 将 `python -m beancount_budget_tool` 换成 PowerShell 的 `& $Exe`。

[Actions 可执行包](https://github.com/TinySnow/beancount-budget-tool/actions/workflows/windows-exe.yml)提供 Windows x64，以及 Mac 的 Apple Silicon（arm64）／Intel（x64）单文件版。Mac 解压 Artifact 后再解压 tar.gz，在终端使用 `./beancount-budget-tool`；可单独移动程序，无需外置 `_internal` 或安装 Python。详见 [Mac 启动说明](packaging/MACOS-START.md)。

TUI：`r` 重算，`m` 跳月，`n` 下一月，`Enter` 看完整来源，`?` 看帮助。明确的 `fund` 新用途自动识别；缺少金额、用途或证据时继续待审，不把计划变成现金。

按 `u` 查看从 cutover 到当前月的**累计用途余额**，可跨年：例如每月给电子产品存入 200，三个月共存入 600、购买花掉 500，剩余可支付余额为 100。主表取期末真实库存和已审核的用途可支付余额；历史划拨 Q/G 另列，不累加月末快照，也不把 K 账面或证券池成本算成现金。`p` 可选其他连续区间，任一月待审则不签发整个区间。

月报默认折叠无当月活动、无余额、无有效目标的旧科目，`z` 恢复全部；完整 JSON 数据仍保留。`i` 打开 K 申赎流水，`v` 打开 tracking 专题流水，K 汇总改为紧凑账面表。工资周期／区间／累计内按 `Esc` 返回月报，`m` 可直接跳月；`c` 可切换回上次查询。流水内 `r` 重查，`--watch` 下保存文件后自动重查，失败时清除旧结果；终端缩放会同步尺寸并完整重画。

月报的“生活费可支付现金（默认支出用途）”只指配置中 `defaults.expense_purpose` 对应的用途，显示名称随配置变化。它是支付范围内该用途的现金，经债务预留、未覆盖责任等核对后的金额，不是全部 H 资产或全部用途余额的合计。其他用途可支付余额单列，不并入该用途现金与上限。**最多能花 = min（默认支出用途可支付现金，对应目标／发薪周期剩余额度）**。

消费上限可按实际发薪周期：config.yml 设置 `expense_budget_basis: salary_cycle`，budgets.yml 的 `salary_cycles` 保存日期和额度。同一周期跨自然月连续累计，额度不在月初重置；新发薪周期使用新目标，不自动承接旧周期剩余额度。自然月目标则按月独立，实际现金余额始终随账本延续。月报显示截至月末的周期进度；TUI `w` 直接选保存的周期，CLI 可用 `report --config config.yml --cycle current`。例如 6 月 9 日发薪、7 月 15 日再次发薪，选择 6 月 9 日周期会连续统计 6 月 9 日至 7 月 14 日消费；6 月月报的进度只统计至 6 月 30 日，保持与月末资金余额相同的截止日。

多币种收支按原币并列：例如 550 JPY 消费与 5 CNY 消费分别累计，不要求汇率或 `price`，不自动折成人民币。原币完整且相关资金审核通过时显示 `CERTIFIED_NATIVE_SCOPED`。人民币目标和最多能花只包含原生人民币消费，外币预算／剩额显示 `-`；外币资产不因此变成人民币可支付资金。月报、区间、薪资周期及文本／TUI／JSON／CSV使用相同原币数据，见[合成示例](examples/v6/multicurrency/README.md)。

## 哪些文件不能删

- 目录版 exe 旁边的 `_internal` 是运行依赖，必须和 exe 一起保留。
- Windows 单文件版不需要 `_internal`，会在用户 LocalAppData 的程序专用 Runtime 目录自动解包，正常退出清理本次目录；目录兼容包仍提供。
- 运行判断只依赖账本、`config.yml`、`budgets.yml`；不读取或生成独立审阅文件。TUI 的 `e`／`f` 只提供修改指引，歧义不会被按键批准。
- 旧版留下的隐藏审阅记录不会自动导入或删除。请保留备份，并把需要的决定明确写进账本或两份 YAML；当前查询不会信任旧批准。
- 内部运行文档只存在于内存或系统临时目录，退出后释放；`state`／`clean` 保存审阅命令已退役。

## 源码安装与示例

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m beancount_budget_tool --config examples/v6/simple/config.yml --format tui
```

虚拟示例应显示最多能花 **190 CNY**。Windows 可用 `py -3.12 -m venv .venv` 和 `.\.venv\Scripts\python.exe`；也可直接使用 [Actions 构建的 exe](https://github.com/TinySnow/beancount-budget-tool/actions/workflows/windows-exe.yml)。

运行测试：`.venv/bin/python -m unittest discover -s tests`。真实账本和私人审阅数据不随项目发布。

# Windows 启动说明

适用于 Windows 10／11 x64，无需安装 Python。完整逐月步骤见 [QUICKSTART](../QUICKSTART.md)，字段见[配置参考](../docs/CONFIGURATION.md)。

## 下载后先试一下

可选单文件包 `beancount-budget-tool-windows-x64`，exe 可独立移动，不需要外置 `_internal`。完整解压后，在该目录打开 PowerShell／VS Code 终端：

```powershell
.\beancount-budget-tool.exe --version
.\beancount-budget-tool.exe --config .\example\simple\config.yml --format tui
```

虚拟例子应显示最多能花 **190 CNY**，按 `q` 退出。程序是终端工具，直接双击不会打开图形窗口。

新版单文件会在 `%LOCALAPPDATA%\BeancountBudgetTool\Runtime` 下自动创建本次运行的私有目录并解包依赖，正常退出清理本次目录；不再使用调用终端的 TEMP／TMP 路径。无需自己建目录或以管理员运行。

另保留 `beancount-budget-tool-windows-x64-folder` 目录兼容包，只有选择这个版本时，exe 与 `_internal` 才必须一起保留。切换为单文件需要下载对应包，不能删除旧目录版的依赖来转换。此前 VS Code 的故障根因尚未确认：固定解包目录只是绕过默认 Temp 路径，不能保证权限／终端策略问题都已解决。请用普通 VS Code 终端试新版；如仍失败，保留错误和 BUILD-COMMIT.txt。

## 接入自己的账本

只有旧 month.yaml 时，复制迁移当前实际使用的最新配置：

```powershell
.\beancount-budget-tool.exe configure --from-config "D:\Beancount\budget\2023-07\compiled\month.yaml" --output "D:\Beancount\budget\simple"
```

没有任何旧配置时，改用下面一条：

```powershell
.\beancount-budget-tool.exe configure --ledger "D:\Beancount\main.bean" --month 2023-07 --output "D:\Beancount\budget\simple"
```

输出目录须不存在。两种方式只选一种；路径换成自己的。打开生成的 config.yml，确认账户 H／K、支付范围和必要期初；在 budgets.yml 设置目标。历史上限记不清可明确填 `not_assessed`，`null` 仍是待决定，不能用 0 代替未知值。

```powershell
.\beancount-budget-tool.exe --config "D:\Beancount\budget\simple\config.yml" --month 2023-07 --format tui
```

修改账本 fund／budget 或两份配置后，保存并按 `r`。待审项按 `Enter` 查看完整文件与行号；`e` 显示修改指引，`f` 显示本月末完整性的 YAML 填写指引，`n` 进入下一月，`m` 跳月，`x` 导出。每月仍用同一 config、budgets 和 cutover，无需复制填写表或手工编译。

查看长流水：`a` 打开交易明细，可用 `/` 筛选预算科目；`h` 查询科目用途流水。默认最新在上，按 `o` 或 `O` 切换正序／倒序。按空格／PgDn 向下翻整页，`b`／PgUp 向上翻整页，`g/G` 或 Home/End 直接到首尾；无需逐行翻。排序只改变展示，每笔余额不变，不新增配置文件。

消费上限按发薪周期时，在 config.yml 设置 `expense_budget_basis: salary_cycle`，在 budgets.yml 的 `salary_cycles` 按实际发薪日保存 expense_targets；无需另设自然月上限。月报的最多能花使用当前周期剩余额度与已审可支付资金的较小值。`w` 直接选已保存周期；本次工资日包含，下一工资日全天归下一周期。完整示例见 QUICKSTART。

同日充值实际在先，却因不同文件行号报短时负余额时，可在充值交易头下、第一条分录前写 `budget-order: -1`，保存后 `r` 重算。未填为 0；必须使用支持此字段的新版 exe。只支持不跨成本交易或自动 pad 的普通无成本交易，不自动猜先后，也不改变 Fava 的排序。完整例子见 [QUICKSTART](../QUICKSTART.md#同日先充值再付款却报负余额)。

## 升级与清理

更新程序时保留自己的账本、config.yml 和 budgets.yml。当前程序不读取或创建独立审阅状态，不使用 reviews.json.gz；TUI e/f 仅提供修改指引，缺少明确依据时仍待审。用户应编辑账本或两份 YAML 后重算。

旧版留下的 .budget-state 不会自动加载、转换或删除；请保留备份，需要的决定应显式写入账本或两份 YAML。state/clean 保存审阅命令已退役。目录兼容包的 _internal 仍是运行依赖，不要删除。若报错，请保留完整命令、错误和 BUILD-COMMIT.txt。

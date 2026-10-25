# Windows 启动说明

适用于 Windows 10／11 x64，无需安装 Python。完整逐月步骤见 [QUICKSTART](../QUICKSTART.md)，字段见[配置参考](../docs/CONFIGURATION.md)。

## 下载后先试一下

推荐目录包 `beancount-budget-tool-windows-x64-folder`。完整解压后，在该目录打开 PowerShell／VS Code 终端：

```powershell
.\beancount-budget-tool.exe --version
.\beancount-budget-tool.exe --config .\example\simple\config.yml --format tui
```

虚拟例子应显示最多能花 **190 CNY**，按 `q` 退出。程序是终端工具，直接双击不会打开图形窗口。

**目录版 exe 与 `_internal` 必须一起保留。** `_internal` 内是 Python 和动态库，不能删除，也不要只复制 exe。另有单文件包，其 exe 可以单独复制，但启动需要在 Temp 解包；若遇到 `Could not create temporary directory!`，使用目录包，无需一直开启管理员终端。

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

修改账本 fund／budget 或两份配置后，保存并按 `r`。待审项按 `Enter` 查看完整文件与行号；`e` 保存分类／用途决定，`f` 核对本月末资料完整性，`n` 进入下一月，`m` 跳月，`x` 导出。每月仍用同一 config、budgets 和 cutover，无需复制填写表或手工编译。

工资周期按 `w`，填本次和下次工资日期、可选额度；本次工资日包含，下一工资日归下一周期。工资周期额度与自然月消费目标独立。

同日充值实际在先，却因不同文件行号报短时负余额时，可在充值交易头下、第一条分录前写 `budget-order: -1`，保存后 `r` 重算。未填为 0；必须使用支持此字段的新版 exe。只支持不跨成本交易或自动 pad 的普通无成本交易，不自动猜先后，也不改变 Fava 的排序。完整例子见 [QUICKSTART](../QUICKSTART.md#同日先充值再付款却报负余额)。

## 升级与清理

升级程序时替换解压包，保留账本、两份配置及 `.budget-state`。后者若包含已审决定，就不是可随意删除的缓存。

新版把必要审阅数据收拢在 `.budget-state/config/reviews.json.gz`，随账本备份。计算临时输入正常退出自动释放。旧版留下很多 runtime／snapshots／reviews 时，先退出其他预算工具窗口，再运行一次：

```powershell
.\beancount-budget-tool.exe clean --config "D:\Beancount\budget\simple\config.yml" --dry-run
.\beancount-budget-tool.exe clean --config "D:\Beancount\budget\simple\config.yml"
```

第一条只预览；第二条先保留决定与恢复资料，再删除旧缓存和散落文件。不会删除 `_internal`、账本或两份配置。纯行内标签且没有保存审阅的工作流可完全没有状态文件。

若报错，请保留运行命令、完整错误及包内 BUILD-COMMIT.txt，便于定位。兼容旧命令详见 [LEGACY-WORKFLOWS](../LEGACY-WORKFLOWS.md)，不要求重新配置已能运行的旧项目。

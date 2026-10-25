# Windows 试用（Windows 10／11，64 位 x64）

不用安装 Python。构建页面现在提供两种包：

- **推荐 `beancount-budget-tool-windows-x64-folder`：目录版。** 解压后保留 `beancount-budget-tool.exe` 及旁边的 `_internal` 文件夹；启动不需要先在 Temp 中解压自身。不要只复制这个版本的 exe。
- `beancount-budget-tool-windows-x64`：单文件版。只有这个版本的 exe 可以单独复制；每次启动要在系统临时目录解压运行文件。

请先解压整个下载包，在解压后的文件夹里打开 PowerShell 或 Windows Terminal。
这是终端程序；直接双击 exe 不会打开图形窗口。
两种包的命令相同，原有账本及 v6 配置可以继续使用。

**日常推荐只维护 config.yml + budgets.yml。** 已有配置可复制迁移一次，原文件和已审记录保留：

```powershell
.\beancount-budget-tool.exe configure --from-config "D:\账本\budget\2023-07\compiled\month.yaml" --output "D:\账本\budget\simple"
.\beancount-budget-tool.exe --config "D:\账本\budget\simple\config.yml" --month 2023-07 --format tui
```

以后改这两份配置或账本，按 `r` 重算；`m` 跳月、`n` 下一月，无需手工编译和新增月配置。请迁移当前实际使用的最新 `month.yaml`。详见 [两文件步骤](../QUICKSTART.md#两文件入口推荐)。下文保留原有兼容入口的示例。

如果单文件版在 VS Code 或普通 PowerShell 中报 `[PYI-...:ERROR] Could not create temporary directory!`，此时还未开始读取账本。先换目录版，在普通终端运行 `--version`；无需为了日常使用一直开启管理员终端。即便 PowerShell 可以新建文件夹，也不能据此确定打包器创建解压目录失败的具体原因。

## 1. 先试虚拟账本

推荐直接运行包内两文件例子，不需要 init 或编译：

```powershell
.\beancount-budget-tool.exe --version
.\beancount-budget-tool.exe --config .\example\simple\config.yml --format tui
```

应看到最多能花 **190 CNY**。这只适用于包内虚拟账本。按 `q` 退出，按 `?` 查看两文件模式按键。

<details>
<summary>兼容旧版本的虚拟示例和项目入口</summary>

依次复制运行这三行：

```powershell
.\beancount-budget-tool.exe --version
.\beancount-budget-tool.exe init --apply .\example\first-month-setup.yaml --output .\demo
.\beancount-budget-tool.exe --config .\demo\month.yaml --format tui
```

应看到 2026 年 9 月已签发月报，最多能花 **190 CNY**：收入 500，分出日常资金 200，消费 10。
这是包内虚构案例的已审配置，不适用于你的真实账本。
TUI 按 `q` 退出；若不想用 TUI，去掉 `--format tui` 即可看文本。

待审项分行显示，文件名和行号在账户前面，金额以小数显示。用上下键选中一项，按 `Enter` 查看完整 Windows 路径；详情用上下键或 `PgUp/PgDn` 滚动，`Esc` 返回，`r` 重算。窗口较窄时内容自动换行，`?` 查看其他按键。「本笔金额」与错误原因里的「交易后余额」是两个不同的数。

选中项开头有 `>` 和 `[选中]`。Windows 界面使用普通文字，不依赖反色或粗体，以避免 VS Code 等终端选中项的中文和行尾显示不完整。
`demo` 已存在时请换一个新的输出目录；程序不会覆盖它。

可再登记一个默认项目入口，以后不用反复填写配置路径：

```powershell
.\beancount-budget-tool.exe project --config .\demo\month.yaml --output .\budget-project.yaml
.\beancount-budget-tool.exe --format tui
```

已有多个月份时，可重复提供 `--config`。从这个文件夹启动会读取 `budget-project.yaml`；
也可用 `--project "项目文件路径" --month 2026-09 --format tui` 指定月份。
TUI 按 `m` 直接跳到已登记月份。在 TUI 中保存审阅、月末声明或载入下一月时，项目入口会更新到新配置。
入口只记录路径；每次查看仍重新审计，不能代替逐月确认。
若在另一个 PowerShell 窗口保存了新配置，回到 TUI 按 `r` 即可读取新入口，查看月份保持不变；导出前也会重读。保存的新配置未登记时会保留路径和提示，避免刷新丢失审阅结果。
查看多个月时按 `p`，填写起始和结束月份；起始填 `all` 看全部已载入月份。`c` 切回月报／上次区间，`x` 导出当前范围；区间内仍须逐月齐全。

没有带 `--project` 的命令保存了新配置时，可登记到原项目；同月替换入口，其他月份保留：

```powershell
.\beancount-budget-tool.exe project --update .\budget-project.yaml --config "新配置路径\month.yaml" --default-month 2026-10
.\beancount-budget-tool.exe report --from 2026-09 --through 2026-10
.\beancount-budget-tool.exe report --from 2026-09 --through 2026-10 --format csv --output .\累计.csv
```

把路径和月份换成自己的；区间必须逐月齐全。待审未完成时会列原因和位置，暂不输出累计数字。

后续审阅和准备下一月也能直接使用项目，保存后自动更新入口：

```powershell
.\beancount-budget-tool.exe review --project .\budget-project.yaml --output .\review-current
# 填写 review-current\decisions.yaml 的决定、理由和批准项后：
.\beancount-budget-tool.exe review --project .\budget-project.yaml --apply .\review-current\decisions.yaml
.\beancount-budget-tool.exe next-month --project .\budget-project.yaml --output .\next-month
```

打开新目录的 `START_HERE.md` 继续。准备填写表不会批准交易或月末资料；旧表引用的配置已被更新时，程序会要求从最新配置重新生成填写表。

</details>

## 2. 使用真实账本

如果已经有月报配置，直接换成它的路径：

```powershell
.\beancount-budget-tool.exe --config "D:\账本\budget\2023-07\month.yaml" --format tui
```

没有配置时，先生成首月填写表（下面路径换成你自己的）：

```powershell
.\beancount-budget-tool.exe init --ledger "D:\账本\main.bean" --month 2023-07 --cutover 2023-07-01 --currency CNY --income-purpose 自由资金 --expense-purpose 生活费 --expense-category 生活开销 --output .\budget-2023-07
```

用文本编辑器打开 `budget-2023-07\setup.yaml`，按同目录 `START_HERE.md` 填写账户、期初用途和目标。再运行：

账本最初用 `pad` 初始化的余额默认归已配置的「自由资金」，不用给自动生成的分录补 `fund`，也不计为收入或预算划拨。此规则只适用于某资产坐标从无交易记录开始的正数 Assets／Equity 初始化；后续余额校正继续待审。cutover 之前的期初用途仍按填写表确认。

```powershell
.\beancount-budget-tool.exe init --apply .\budget-2023-07\setup.yaml
.\beancount-budget-tool.exe --config .\budget-2023-07\compiled\month.yaml --format tui
```

如果年代久远，记不清当时的消费上限，在 `setup.yaml` 的目标中明确填 `not_assessed`，不要猜金额或填 0：

```yaml
expense_targets:
  生活开销: not_assessed
```

类别名用你原表里的名字。`null` 表示尚待决定，仍会待审；`not_assessed` 表示本月不考核该消费目标，实际消费及资金用途照常审核。该月不计算消费目标差和受它限制的「最多能花」，但不会仅因此阻挡完整月报。划拨目标 `allocation_targets` 同样可填 `not_assessed`。下一月可继续这样填，或改回明确金额；工资周期额度仍单独输入。此功能需要更新 exe，旧包不支持消费目标的 `not_assessed`。

未确认的地方会列入待审，并显示文件与行号；不必为了出数字随意填 `true`。
程序只读账本，生成的配置／报告放在你指定的新目录。账本、include 文件和已有 YAML 仍需保留。

## 3. 工资周期

TUI 按 `w` 输入本次工资日、下次工资日及可选周期额度；包含本次工资日，排除下次工资日。也可运行：

```powershell
.\beancount-budget-tool.exe report --project "D:\账本\budget\budget-project.yaml" --payday 2023-07-08 --next-payday 2023-08-02 --cycle-budget 2000
```

日期、路径和额度换成自己的；本例统计到 8 月 1 日，8 月 2 日全部归下一周期。需要周期末所在月份的最新配置。只签发周期消费和额度差，额度剩余不等于实际可花现金。

## 4. 旧命令怎样换成 exe

| 原命令 | exe 写法 |
|---|---|
| `beancount-budget-tool` | `.\beancount-budget-tool.exe` |
| `beancount-budget-init` | `.\beancount-budget-tool.exe init` |
| `beancount-budget-project` | `.\beancount-budget-tool.exe project` |
| `beancount-budget-review-month` | `.\beancount-budget-tool.exe review-month` |
| `beancount-budget-review` | `.\beancount-budget-tool.exe review` |
| `beancount-budget-next-month` | `.\beancount-budget-tool.exe next-month` |
| `beancount-budget-report` | `.\beancount-budget-tool.exe report` |
| `beancount-budget-review-refs` | `.\beancount-budget-tool.exe review-refs` |
| `beancount-budget-audit` | `.\beancount-budget-tool.exe audit` |

后面的参数保持不变。每种命令加 `--help` 可看参数。
Beancount 自动运行的 balance／pad 等模块已包含；账本显式声明 `plugin` 的认证限制与 Python 版相同。

试用时如果报错，请保留命令、完整报错及 `BUILD-COMMIT.txt` 的内容，方便定位。

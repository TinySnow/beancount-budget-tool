# Windows 试用（Windows 10／11，64 位 x64）

这个文件夹里的 `beancount-budget-tool.exe` 可以单独运行，不用安装 Python。
请先解压整个下载包，在解压后的文件夹里打开 PowerShell 或 Windows Terminal。
这是终端程序；直接双击 exe 不会打开图形窗口。

## 1. 先试虚拟账本

依次复制运行这三行：

```powershell
.\beancount-budget-tool.exe --version
.\beancount-budget-tool.exe init --apply .\example\first-month-setup.yaml --output .\demo
.\beancount-budget-tool.exe --config .\demo\month.yaml --format tui
```

应看到 2026 年 9 月已签发月报，最多能花 **190 CNY**：收入 500，分出日常资金 200，消费 10。
这是包内虚构案例的已审配置，不适用于你的真实账本。
TUI 按 `q` 退出；若不想用 TUI，去掉 `--format tui` 即可看文本。
`demo` 已存在时请换一个新的输出目录；程序不会覆盖它。

## 2. 使用真实账本

如果已经有月报配置，直接换成它的路径：

```powershell
.\beancount-budget-tool.exe --config "D:\账本\budget\2023-07\month.yaml" --format tui
```

没有配置时，先生成首月填写表（下面路径换成你自己的）：

```powershell
.\beancount-budget-tool.exe init --ledger "D:\账本\main.bean" --month 2023-07 --cutover 2023-07-01 --income-purpose 自由资金 --expense-purpose 生活费 --expense-category 生活费 --output .\budget-2023-07
```

用文本编辑器打开 `budget-2023-07\setup.yaml`，按同目录 `START_HERE.md` 填写账户、期初用途和目标。再运行：

```powershell
.\beancount-budget-tool.exe init --apply .\budget-2023-07\setup.yaml
.\beancount-budget-tool.exe --config .\budget-2023-07\compiled\month.yaml --format tui
```

未确认的地方会列入待审，并显示文件与行号；不必为了出数字随意填 `true`。
程序只读账本，生成的配置／报告放在你指定的新目录。账本、include 文件和已有 YAML 仍需保留。

## 3. 旧命令怎样换成 exe

| 原命令 | exe 写法 |
|---|---|
| `beancount-budget-tool` | `.\beancount-budget-tool.exe` |
| `beancount-budget-init` | `.\beancount-budget-tool.exe init` |
| `beancount-budget-review-month` | `.\beancount-budget-tool.exe review-month` |
| `beancount-budget-review` | `.\beancount-budget-tool.exe review` |
| `beancount-budget-next-month` | `.\beancount-budget-tool.exe next-month` |
| `beancount-budget-report` | `.\beancount-budget-tool.exe report` |
| `beancount-budget-review-refs` | `.\beancount-budget-tool.exe review-refs` |
| `beancount-budget-audit` | `.\beancount-budget-tool.exe audit` |

后面的参数保持不变。每种命令加 `--help` 可看参数。
Beancount 自动运行的 balance／pad 等模块已包含；账本显式声明 `plugin` 的认证限制与 Python 版相同。

试用时如果报错，请保留命令、完整报错及 `BUILD-COMMIT.txt` 的内容，方便定位。

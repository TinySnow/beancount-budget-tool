# Mac 可执行版启动说明

无需安装 Python。完整逐月步骤见 [QUICKSTART](../QUICKSTART.md)，字段见[配置参考](../docs/CONFIGURATION.md)。这是终端工具，不是图形 App。

## 选择和解压

在同一次 Actions 的 Artifacts 中选择：

| Mac | Artifact |
|---|---|
| Apple Silicon（M1／M2／M3／M4 等） | `beancount-budget-tool-macos-arm64` |
| Intel | `beancount-budget-tool-macos-x64` |

“关于本机”可查看芯片；终端 `uname -m` 原生环境一般分别显示 arm64／x86_64。

下载的 Artifact 是 zip，先解压，再解压里面的 `.tar.gz`。其中 `beancount-budget-tool` 是**单文件程序**，可以单独移动和运行，没有外置 `_internal`。其他文件是说明、许可证和虚拟示例，不是程序运行依赖；使用真实账本时仍需保留自己的账本、配置和审阅数据。

保留 tar.gz 中的可执行权限。依赖库已装在程序内部，启动时会解压到系统临时目录，正常退出后清理。

例如 Apple Silicon 包：

```bash
cd ~/Downloads
tar -xzf beancount-budget-tool-macos-arm64.tar.gz
cd beancount-budget-tool-macos-arm64
./beancount-budget-tool --version
./beancount-budget-tool --config example/simple/config.yml --format tui
```

如果浏览器已经自动解压，直接进入得到的目录。虚拟例子应显示最多能花 **190 CNY**，按 `q` 退出。

目前没有 Apple Developer ID 签名或公证。若系统提示无法验证开发者，确认下载来自本项目后，可按 [Apple 的单个程序放行步骤](https://support.apple.com/en-us/102445)操作。CI 在 macOS 15 上分别原生构建／检查两种架构；更旧系统的兼容性尚未验证。

## 接入已有账本

在程序目录的终端执行，把路径替换成自己的。已有两份配置时直接打开：

```bash
./beancount-budget-tool --config "$HOME/Beancount/budget/simple/config.yml" --month 2023-08 --format tui
```

如果只有旧 month.yaml，先从当前实际使用的最新文件复制迁移一次：

```bash
./beancount-budget-tool configure --from-config "$HOME/Beancount/budget/2023-07/compiled/month.yaml" --output "$HOME/Beancount/budget/simple"
```

没有任何配置时，改用：

```bash
./beancount-budget-tool configure --ledger "$HOME/Beancount/main.bean" --month 2023-07 --output "$HOME/Beancount/budget/simple"
```

两种创建方式只选一种，输出目录须不存在。确认 config.yml 的账户和必要期初、budgets.yml 的目标后再运行。历史目标记不清可填 `not_assessed`；`null` 仍待决定。不必手工编译或按月复制配置。

修改账本／两份配置后保存，按 `r` 重算；`Enter` 看完整待审位置，`e` 保存分类／用途决定，`f` 确认月末完整性，`n` 下一月，`m` 跳月，`c` 累计，`p` 区间，`w` 工资周期，`x` 导出。

两文件 TUI 当前没有自动 watch；保存后须按 `r`。同日明确先充值时，可在充值交易头下写 `budget-order: -1`；`tracking` 的专题支出出现在月报下方。

## 升级和清理

更新时替换单个 `beancount-budget-tool` 程序即可，保留账本、config.yml、budgets.yml 和保存审阅记录的 `.budget-state`。从旧 Mac 目录版升级，确认新单文件程序能运行后，可删除旧程序包及其 `_internal`；不要把账本或配置一起删掉。旧缓存很多时，退出其他预算工具窗口后执行：

```bash
./beancount-budget-tool clean --config "$HOME/Beancount/budget/simple/config.yml" --dry-run
./beancount-budget-tool clean --config "$HOME/Beancount/budget/simple/config.yml"
```

程序会保留必要审阅数据，不删除账本或运行依赖。若报错，请保留完整命令、错误和包内 BUILD-COMMIT.txt。

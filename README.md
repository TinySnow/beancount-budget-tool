# beancount-budget-tool

从 Beancount 账本核对消费预算、专项资金用途和可花金额的工具。

> **当前状态：v6 开发预览。** 它能审计导入流水和已审资料；2026 年 9 月隔离账本已有分项认证结果。新月份仍要补齐用途、转账和责任审阅，尚不能导入账单后直接得到完整首页。

## 从哪里开始

| 你现在要做什么 | 读哪一页 |
|---|---|
| 了解导入流水后如何标工资、消费、转账，以及混合用途账户 | [QUICKSTART](QUICKSTART.md) |
| 第一次建立预算配置，或先跑一个完整虚拟示例 | [首次配置填写表](QUICKSTART.md#首次建立配置先填一张表) |
| 看九个需求例子怎样结转到下一月，并自己试改标签 | [连续三个月虚拟案例](examples/v6/acceptance/README.md) |
| 给待审的交易选择类别／资金用途，不手工复制来源引用 | [交易待审填写表](QUICKSTART.md#待审的类别用途只填决定不复制来源引用) |
| 已有上一月配置，准备下一月目标和资料 | [准备下一月](QUICKSTART.md#先准备下一月) |
| 在终端查看月报、筛选待审并重算 | [TUI 用法](QUICKSTART.md#3-导入新月后复核) |
| 比较几个月的消费、查看区间累计并导出 CSV | [月份比较与导出](QUICKSTART.md#5-比较月份和导出-csv) |
| 按两次真实工资日查看生活费消费及周期额度差 | [工资周期](QUICKSTART.md#工资周期从本次到账日算到下次到账日前一天) |
| 把 2023 年以来的旧账按月修改、按月校对 | [QUICKSTART 的逐月迁移步骤](QUICKSTART.md#4-旧账按月迁移与校对) |
| 已有一套审阅过的 v6 配置，要查看月报或列出新交易待审项 | [月报与待审清单](docs/v6-monthly-report.md) |
| 只是想知道哪些功能已实现、哪些还缺 | [实施状态](docs/v6-implementation-status.md) |

**推荐顺序：**先看本页；导入账单时看 QUICKSTART；需要运行已审月报时再看月报说明。需求、架构和实施状态在[文档导航](docs/README.md)中按用途归类。

## v6 目前怎样工作

1. 用 importer 把银行、钱包、信用卡和证券流水导入 Beancount，先核对账务事实。
2. 给明确的费用／收入分录填 `budget`，给资产分录填 `fund`；混合用途可以在一条分录上写带金额的 `fund`，程序会核对合计。默认分类来自使用者批准的私人规则，不写进公共算法。
3. 运行待审清单，核对分类、资产用途、转账边界和责任；工具对金额守恒及来源引用进行校验。未审事项保留待审状态。
4. 审阅输入齐全后运行 v6 月报。每个指标独立显示认证状态；历史截止日的结果不等于今天余额。

`beancount-budget-review` 可把待审分类与 H 资产用途导出为填写表。填写名称、理由并明确批准后，工具保存来源认领并重跑月报；不必手工拼写引用。复杂归属、责任、转账协议与计价仍需各自审阅，完整交互界面尚在开发。

`beancount-budget-next-month` 复用上一月配置，准备紧接的下一月目标与账户变化；保留原 cutover，月末资料另行核对。交易审完后也可用本月最新配置补充完整性声明。

月报入口加 `--format tui` 可交互浏览，支持重算、中文关键词筛选和待审详情；按 `e` 填分类／H 资产用途，写理由并明确批准后保存新配置、自动复审；提供其他 `--tui-config` 后可切换各月配置。所有数字和待审项来自同一月报审计。

`beancount-budget-init` 可从账本生成一份集中填写的 `setup.yaml`，确认账户、期初用途及目标后编译月报侧文件。公开的[首月虚拟填写表](examples/v6/first-month-setup.yaml)可以运行完整月报，标签语法另见[示例账本](examples/v6/labeling.bean)。复杂责任、资金池和计价证据仍需单独审阅；完整日常首页与复杂事项的交互审阅仍在开发。9 月私人示范资料不随公开仓库发布。

月报签发后在 TUI 按 `a` 可查看当月交易，筛选后按 Enter 看各分录分类、资金用途、卡债责任及账本原文；CLI 加 `--transactions` 可附完整逐笔来源。修改文件后重算，来源和详情随新审计更新。

## 登记常用月份，直接启动

已有月份配置时，可登记一个项目入口：

```bash
beancount-budget-project --config budget/2023-07/compiled/month.yaml --config budget/2023-08/compiled/month.yaml --output budget-project.yaml
beancount-budget-tool --format tui
beancount-budget-tool --month 2023-07 --format tui
```

在该目录运行会读取 `budget-project.yaml`；在其他目录可用 `--project 路径` 指定。TUI 按 `m` 直接跳到已登记月份，按 `n` 准备／载入下一月。在 TUI 中保存审阅或月末声明、载入下一月时，入口自动指向新配置；重启默认打开上次保存的月份。入口只记录路径，每次查看仍会重新审计。Windows exe 写法见[试用步骤](packaging/WINDOWS-START.md)。

同时使用命令行和 TUI 时，命令行保存新配置后，在 TUI 按 `r` 会重读项目入口并跟进新路径，当前查看月份保持不变；导出也会先重读。若本次保存的新配置尚未登记，TUI 会保留该路径及提示，待登记后再同步。

TUI 按 `p` 输入起止月份，查看选定区间；起始填 `all` 恢复全部已载入月份。`c` 在月报和上次选择的区间间切换，`x` 导出当前范围。选择仅在本次会话保留，不改变 cutover 或历史余额的重放规则。

用未指定项目的命令生成新配置后，可将新路径登记到已有项目：

```bash
beancount-budget-project --update budget-project.yaml --config budget/2023-09/compiled/month.yaml --default-month 2023-09
beancount-budget-report --from 2023-07 --through 2023-09
beancount-budget-report --from 2023-07 --through 2023-09 --format csv --output 2023-Q3.csv
```

登记同一个月会替换该月入口，其他月份保留；省略 `--default-month` 会保留原启动月份。区间命令也支持 `--project 路径`，不指定范围时检查全部已登记月份。缺月会报错；有月份待审时，先显示待审原因和位置，完成审阅后再输出累计数字。

后续直接带上 `--project`，即可从最新配置继续；保存后入口自动更新：

```bash
beancount-budget-review --project budget-project.yaml --month 2023-07 --output review-july
# 填写 review-july/decisions.yaml 中的决定、理由和批准项，再保存：
beancount-budget-review --project budget-project.yaml --apply review-july/decisions.yaml
beancount-budget-next-month --project budget-project.yaml --output august
```

打开 `august/START_HERE.md` 填写下一月目标并保存；生成的命令会带上项目路径。月内审阅完成后，用 `beancount-budget-next-month --project budget-project.yaml --confirm-completeness --reason "已核对本月末全部账单及责任" --output closed-month` 确认当前默认月份，或加 `--month` 选择已登记月份。旧填写表不能覆盖更新后的月配置；资料未确认时继续待审。

## 开发资料

实现或审阅 v6：从[文档导航](docs/README.md)进入需求、架构和实施状态。

## 开发环境

在仓库根目录建立虚拟环境并安装项目：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m unittest discover -s tests
.venv/bin/beancount-budget-tool --help
```

安装后的 `beancount-budget-tool` 是 v6 已审月报入口；`beancount-budget-audit` 是 v6 分项审计入口；`beancount-budget-review-month` 用于逐月迁移校对。GitHub Release 工作流构建 Python wheel 和源码包。

Windows PowerShell 用 `py -3.12 -m venv .venv` 建环境，再运行 `.\.venv\Scripts\python.exe -m pip install -e .`；首月迁移命令见 [QUICKSTART](QUICKSTART.md#4-旧账按月迁移与校对)。Windows TUI 所需的 `windows-curses` 会随项目安装。

**Windows 免 Python 试用包：**打开 [Windows executable 构建](https://github.com/TinySnow/beancount-budget-tool/actions/workflows/windows-exe.yml)，选最新成功运行，在 Artifacts 下载推荐的 `beancount-budget-tool-windows-x64-folder`，完整解压并保留 exe 旁边的 `_internal` 文件夹。此目录版无需在启动时临时解压自身，可用于排查单文件版的 `Could not create temporary directory`。原 `beancount-budget-tool-windows-x64` 单文件包继续提供，两版命令相同。按包内 `START-HERE.md` 或[在线步骤](packaging/WINDOWS-START.md)操作。每次推送自动构建，下载保留 30 天；交互 TUI 的显示和按键仍需在本机终端试用。

此环境只解决运行问题；旧 Beancount 2.3.6 审阅侧文件不能不经复核就改写为 3.2.3。当前真实账本副本的两版 booked 事实已由[跨版本审计](docs/v6-implementation-status.md)核对一致，但 v6 月报仍需按新 loader 重验侧文件来源。

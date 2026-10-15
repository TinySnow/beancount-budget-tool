# beancount-budget-tool

从 Beancount 账本核对消费预算、专项资金用途和可花金额的工具。

> **当前状态：v6 开发预览。** 它能审计导入流水和已审资料；2026 年 9 月隔离账本已有分项认证结果。新月份仍要补齐用途、转账和责任审阅，尚不能导入账单后直接得到完整首页。

## 从哪里开始

| 你现在要做什么 | 读哪一页 |
|---|---|
| 了解导入流水后如何标工资、消费、转账，以及混合用途账户 | [QUICKSTART](QUICKSTART.md) |
| 第一次建立预算配置，或先跑一个完整虚拟示例 | [首次配置填写表](QUICKSTART.md#首次建立配置先填一张表) |
| 给待审的交易选择类别／资金用途，不手工复制来源引用 | [交易待审填写表](QUICKSTART.md#待审的类别用途只填决定不复制来源引用) |
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

`beancount-budget-init` 可从账本生成一份集中填写的 `setup.yaml`，确认账户、期初用途及目标后编译月报侧文件。公开的[首月虚拟填写表](examples/v6/first-month-setup.yaml)可以运行完整月报，标签语法另见[示例账本](examples/v6/labeling.bean)。复杂责任、资金池和计价证据仍需单独审阅；完整日常首页与交互式审阅仍在开发。9 月私人示范资料不随公开仓库发布。

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

此环境只解决运行问题；旧 Beancount 2.3.6 审阅侧文件不能不经复核就改写为 3.2.3。当前真实账本副本的两版 booked 事实已由[跨版本审计](docs/v6-implementation-status.md)核对一致，但 v6 月报仍需按新 loader 重验侧文件来源。

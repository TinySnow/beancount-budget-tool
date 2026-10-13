# 快速开始：给 Beancount 流水标 v6 预算

> v6 仍在开发。来源检查和已审月报已有命令；新月份仍需核对期初、账户、用途和责任，不能导入后直接签发。

## 1. 安装并检查示例

在仓库根目录运行：

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
python -m beancount_budget_tool.v6 \
  --ledger examples/v6/labeling.bean --cutover 2026-10-01
```

安装后的 `beancount-budget-tool --config path/to/reviewed-month.yaml` 运行 v6 已审月报；`beancount-budget-audit` 运行分项审计。

最后一条只做**来源与标签语法审计**，不会生成预算余额。示例账本见 [labeling.bean](examples/v6/labeling.bean)。当前测试环境使用项目依赖指定的 Beancount 3.2.3。

## 2. 导入后怎么写标签

先保留 importer 生成的账户、金额、日期和成本；只在确认含义后给**对应分录**加 metadata。`budget` 与 `fund` 回答不同的问题：

| 写在哪条分录下 | 写什么 | 表示什么 |
|---|---|---|
| `Income` 或 `Expenses` | `budget: "自由资金"`、`budget: "生活开销.餐饮"` | 收入／费用归到哪个**预算类别** |
| `Assets` | `fund: "自由资金"`、`fund: "储蓄"` | 真实余额类资产的资金用途；申赎累计类资产须与同笔现金腿共同校验 |
| 交易头后、首条分录前 | `tracking: "旅行"` | 本交易的费用进入该专题；不改变类别或用途，月报按原币列支出、退款和净额 |

工资到账并随后划出一部分储蓄时，可以这样写（账户名换成自己的）：

```beancount
2026-10-08 * "雇主" "工资"
  Assets:Bank:Daily 5000 CNY
    fund: "自由资金"
  Income:Salary -5000 CNY

2026-10-08 * "虚拟账务" "留出生活费"
  Assets:Bank:Daily 2000 CNY
    fund: "生活费"
  Assets:Bank:Daily -2000 CNY

2026-10-09 * "银行" "首次划入储蓄"
  Assets:Bank:Daily -700 CNY
    fund: "自由资金"
  Assets:Bank:Savings 700 CNY
    fund: "储蓄"
```

这里的私人配置将 Income 和到账资产的默认用途设为「自由资金」，将 Expenses 对应资产腿的默认用途设为「生活费」。第二笔同账户虚拟账务把 2000 元正式留作生活费；第三笔首次划入储蓄。**未划出的自由资金不计入生活费可花余额。**转账交易的资产腿写 `fund`，不要写 `budget`。`fund: "-"` 表示**预算外**，不是自由资金。

用途名称须登记在归属配置中。新双默认用途模式下，等额同币种的简单两资产腿转账可由已审 `fund` 与账本来源自动认证：同用途搬家净划拨为零，首次从自由资金划入储蓄则计入储蓄净划拨。简单的基金申购／赎回也可在累计申赎腿与同笔现金腿写 `fund`；程序核对金额和双方用途，不能再用侧文件重复认领该申赎。复杂交易仍需相应审阅。不方便改 importer 输出时，也可在审阅侧文件里逐腿声明用途，但同一腿不能同时由行内和侧文件认领。证券资金池内部买卖有单独池规则，入金、转出等池外边界仍需审阅。

只有申赎金额、没有可信份额的基金账户属于 K 账面累计账户。月报将它们的期初、本期变动、期末和 cutover 后已审用途变动单列；这些数不证明基金持仓或可赎回金额，也不与真实资产用途余额相加。

**一条资产分录混有多个用途时，只写一个带金额的 `fund`：**例如 `fund: "保险200，电子产品900，旅游900"`，对应 importer 的一条 `2000 CNY` 分录。合计不等于分录金额时，单月入口会列出 `FUND_GRAMMAR` 和账本行号；先修正金额并重跑，才会继续检查其他来源。跨账户搬移多项原用途时，两侧各用途金额须相同；不一致会列为 `H_TRANSFER_MIXED_REMAP_PENDING`，并显示双侧账本行号。如确要重新分配用途，请另记同账户虚拟划拨。也可用有完整来源引用的审阅侧文件列三份切片，行内和侧文件不得重复认领。不要为了贴多个 `fund` 而把 importer 的一条分录改成多条。

**默认分类由使用者配置，不写死在公共程序里。** 按最新私人规则，普通 Income 默认归自由资金，Expenses 默认归生活费；配置了对应默认值后可省略 `budget`。行内标签和侧文件分类不能同时认领同一分录，所以应在生成新侧文件**之前**完成手工标签。退款在原 `Expenses` 分录上沿用原类别；明确预算外时写 `budget: "-"`。一条分录的多类别拆分可写一个带金额的 `budget`，合计须精确等于分录金额。

## 3. 导入新月后复核

有已审 v6 配置和私人规则的账本，可用一条命令续用旧分类、生成新分类文件，并列出当前日期区间的待审来源：

```bash
python -m beancount_budget_tool.tools.v6_refresh_review \
  --config path/to/reviewed-month.yaml \
  --rules path/to/approved-rules.yaml \
  --since 2026-10-01 --through 2026-10-31 \
  --output-classifications path/to/classifications-2026-10.yaml
```

输出文件不会覆盖旧文件。清单会指出未分类收入／费用、未审资产用途、待审的简单 H 资产转账、K 基金申赎与 H 购汇事件及其来源行号、证券池边界、新账户与负债。**待审 0 项只表示已导入来源没有这些待办**；若区间没有任何交易，程序会提示核对 importer 是否真的覆盖该月。复杂用途事件、计划、计价和报告日完整性仍各自审阅。

配置文件的 YAML 语法、重复键以及月报月份、切换日期、缺失的文件路径等常见错误会给出 `文件:行号`；账本来源待审项则给出对应的账本行号。修正后重跑同一命令即可。

准备好当月配置后，直接运行月报入口：来源尚待审时会先列出交易和账本行号；来源已审但指标仍缺证据时会列出待审指标；各项通过后才显示这份已审月报。配置格式见[月报配置与命令](docs/v6-monthly-report.md)。终端交互浏览使用同一入口：

```bash
python -m beancount_budget_tool.v6.monthly_report \
  --config path/to/reviewed-month.yaml --format tui
```

TUI 用方向键或 `j/k` 滚动、`PgUp/PgDn` 翻页、`q` 退出；脚本调用可选 `--format text` 或 `json`。消费目标和储蓄／投资等用途月目标写在计划侧文件中，不能写在某笔工资或转账的 `budget` 标签里；漏存的月份也应保留目标。

## 4. 旧账按月迁移与校对

先保留原账本，使用 `beancount_budget_tool.tools.v6_make_review_copy` 生成一次完整审阅副本，或沿用已核过的审阅副本。每次保留修改前的完整账本，改完一个月后运行同一入口。例如修改 2023 年 7 月：

```bash
beancount-budget-review-month \
  --before path/to/checkpoint/main.bean \
  --after path/to/working/main.bean \
  --month 2023-07 --cutover 2026-09-01
```

**2026-09-01 cutover 以前**，该命令只核对两份账本是否都能加载、改动是否限于目标月、财务事实是否保持一致；不要求旧交易补 v6 标签，也不签发 v6 月报。若要为更早月份另行签发，须另定更早 cutover 并审定当时的期初用途。

**cutover 当月及之后**，给同一命令加 `--config path/to/reviewed-month.yaml`。配置须写本月、同一 cutover，并指向 `--after` 的账本。结构校验通过后，命令会继续列出来源待审、指标待审，或完整的已审月报；缺配置会明确提示。月报配置格式见[已审月报](docs/v6-monthly-report.md)。

如果本月纠正了原账中写错的账户、金额等财务事实，加 `--allow-financial-corrections`。输出会列出更正前后的 booked 分录及文件行号，供逐项核对；目标月以外的修改仍被拒绝。纯结构校验通过不代表预算数字已认证。账户、期初和证券资金池审阅不要求旧哈希相同；程序按当前账本重算。改动已认领交易仍会使该笔来源引用失效，须重审受影响的认领。完整月报只能由其独立审计签发。

若确实打算给某个月补行内 `budget`，要看**该月哪些费用／收入分录尚缺标签**，再运行：

```bash
python -m beancount_budget_tool.tools.v6_classification_proposals \
  --ledger path/to/working/main.bean \
  --month 2023-07 \
  --output path/to/review-2023-07.json
```

JSON 按账本科目列出逐笔日期、金额、摘要、来源引用和旧标签提示，供人工审阅；它不会自行决定类别。已写行内 `budget` 的分录不列入，尚未考虑分类侧文件中的已审认领。这一步只处理**预算类别**，不认证资产用途、目标兑现或月报。cutover 前的月份不必为了当前 v6 月报而补这些标签。

## 常见错误

- **交易级 `budget`**：v6 会拒绝。旧标签可先保留为 `legacy_budget`，再按分录审阅。
- **转账写 `budget: "投资"`**：`budget` 只用于 `Income`／`Expenses`；资金用途写在资产腿的 `fund` 或已审侧文件里。
- **把“到账 5000、划出 700”记成两笔收入**：收入只有工资一笔；后续是资产间转移，净划拨最多计一次。
- **刚导入就把新月月报当作今天余额**：月报有截止日；其后的交易和责任必须另核。

更完整的输入形状、限制和例外见 [v6 架构](docs/budget-v6-review.md)与[实施状态](docs/v6-implementation-status.md)。

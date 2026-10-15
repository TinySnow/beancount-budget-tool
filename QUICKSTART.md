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

Windows PowerShell 中可使用 Python 3.12 和以下命令；在 Windows Terminal 的交互式窗口里运行 `--format tui`：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\beancount-budget-review-month.exe --help
```

Windows 安装会一并安装 TUI 所需的 `windows-curses`。账本及审阅 YAML 尽量使用相对于各自入口文件的路径，方便在不同机器间移动。

最后一条只做**来源与标签语法审计**，不会生成预算余额。示例账本见 [labeling.bean](examples/v6/labeling.bean)。当前测试环境使用项目依赖指定的 Beancount 3.2.3。

### 首次建立配置：先填一张表

安装或更新项目后，用 `beancount-budget-init` 生成首月填写表。下面的默认名称是可配置的示例，程序不会按账户名称猜用途：

```bash
beancount-budget-init --ledger path/to/main.bean --month 2023-07 --currency CNY --income-purpose 自由资金 --expense-purpose 生活费 --expense-category 生活开销 --output budget-setup
```

Windows 未激活虚拟环境时，把开头替换成 `.\.venv\Scripts\beancount-budget-init.exe`；macOS 可使用 `.venv/bin/beancount-budget-init`。没有新命令时，重新运行 `python -m pip install -e .` 更新入口。

打开 `budget-setup/START_HERE.md`，按说明填写同目录的 `setup.yaml`：账户选 H／K、是否可直接支付、期初各用途金额、本月消费及划拨目标。未确定的字段保留 `null` 或 `false`。然后运行：

```bash
beancount-budget-init --apply budget-setup/setup.yaml
```

没填完会列出**填写表或账本的文件和行号**；填齐后生成 `budget-setup/compiled/month.yaml` 及所需侧文件：

```bash
beancount-budget-tool --config budget-setup/compiled/month.yaml
```

这一步仍会审核交易、负债和计价，生成配置不等于月报已经通过。初始化只在第一次做；生成过的目录不会覆盖，已有决定请在原配置中维护，或用 `--output` 指定新目录。默认 cutover 是首月第一天，所以这套命令从 2023 年 7 月开始，不继承此前用途。

想先体验完整流程，可运行公开的虚拟示例（已填好的决定仅适用于示例）：

```bash
beancount-budget-init --apply examples/v6/first-month-setup.yaml --output demo-budget
beancount-budget-tool --config demo-budget/month.yaml
```

预期：工资 500 元，正式留出 200 元，餐饮花 10 元，**最多能花 190 元**。

月报的「默认支出资金收付」会显示这 10 元的实际付款。信用卡在还款时计入资金收付，消费预算仍归原消费月；转账、购汇、申赎不计入本项。它只作资金流向汇总，不再扣减一次预算余额。

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

如果退回多发的工资，写正数 `Income` 和负数资产金额即可；这类两腿收入冲回省略 `fund` 时，会扣减默认收入用途（这里是自由资金）。消费退款则通过负数 `Expenses` 退回默认支出用途（这里是生活费）。有明确用途时仍可写 `fund`。

用途名称须登记在归属配置中。新双默认用途模式下，等额同币种的简单两资产腿转账可由已审 `fund` 与账本来源自动认证：同用途搬家净划拨为零，首次从自由资金划入储蓄则计入储蓄净划拨。若两腿都没写 `fund`，程序可从**完整已审的期初用途与此前流水**继承：转出账户当时只有一种用途且足额，或本次恰好搬走全部正数用途余额时，无须重复标注；混有多种用途而只转出部分时仍待审。已发生消费留下的负用途余额不会阻止另一份足额资金搬家，但主动转账不得把负值继续调低。简单的基金申购／赎回也可在累计申赎腿与同笔现金腿写 `fund`；程序核对金额和双方用途，不能再用侧文件重复认领该申赎。复杂交易仍需相应审阅。不方便改 importer 输出时，也可在审阅侧文件里逐腿声明用途，但同一腿不能同时由行内和侧文件认领。证券资金池内部买卖有单独池规则，入金、转出等池外边界仍需审阅。

只有申赎金额、没有可信份额的基金账户属于 K 账面累计账户。月报将它们的期初、本期变动、期末和 cutover 后已审用途变动单列；这些数不证明基金持仓或可赎回金额，也不与真实资产用途余额相加。

**一条资产分录混有多个用途时，只写一个带金额的 `fund`：**例如 `fund: "保险200，电子产品900，旅游900"`，对应 importer 的一条 `2000 CNY` 分录。合计不等于分录金额时，单月入口会列出 `FUND_GRAMMAR` 和账本行号；先修正金额并重跑，才会继续检查其他来源。跨账户搬移多项原用途时，两侧各用途金额须相同；不一致会列为 `H_TRANSFER_MIXED_REMAP_PENDING`，并显示双侧账本行号。如确要重新分配用途，请另记同账户虚拟划拨。也可用有完整来源引用的审阅侧文件列三份切片，行内和侧文件不得重复认领。不要为了贴多个 `fund` 而把 importer 的一条分录改成多条。

**默认分类由使用者配置，不写死在公共程序里。** 按最新私人规则，普通 Income 默认归自由资金，Expenses 默认归生活费；配置了对应默认值后可省略 `budget`。行内标签和侧文件分类不能同时认领同一分录，所以应在生成新侧文件**之前**完成手工标签。退款在原 `Expenses` 分录上沿用原类别；明确预算外时写 `budget: "-"`。一条分录的多类别拆分可写一个带金额的 `budget`，合计须精确等于分录金额。

## 3. 导入新月后复核

### 先准备下一月

用上一月**最后使用的配置**生成下一月填写表；程序自动处理月份和跨年，保留原 cutover 及已审决定：

```bash
beancount-budget-next-month --config path/to/latest-month.yaml --output next-month
```

打开 `next-month/next-month.yaml`：

- 核对消费目标、含子类别的总消费上限、用途划拨目标，然后把 `targets_confirmed` 改为 `true`。优先提取已写的新月计划；没有声明时参考上月作提议，不自动批准，也不会把目标当作到账金额。
- `account_changes` 只列新增账户、新币种或原来未确定的支付范围。按说明填 H／K、`payable`／`excluded` 和理由；没有变化时不用重填旧账户。
- 已确认账单覆盖到该月末、截至该日没有漏记责任或在途付款时，填 `completeness_confirmed: true` 及 `completeness_reason`。尚未核对可以保持 `false`，先审交易，月报仍保留完整性待审。

```bash
beancount-budget-next-month --apply next-month/next-month.yaml
beancount-budget-tool --config next-month/compiled/month.yaml
```

此后以 `compiled/month.yaml` 继续导出下面的交易待审表。余额沿原 cutover 重放，不需要把上月余额另记一次期初；原有分类、用途、责任与事件认领继续复用。上月的完整性声明、专项债务覆盖、外币清偿报价不自动作为新月证据。

**先审交易、后来补月末确认时，要用本月最新的已审配置：**

```bash
beancount-budget-next-month --confirm-completeness path/to/latest-reviewed-month.yaml --reason "已核对截至本月末全部账单，无漏记责任或在途付款" --output closed-month
```

这会保留本月已审交易和目标，生成 `closed-month/month.yaml` 并重跑月报；其他待审仍须解决。不用重新编译最初的下一月填写表，以免回到旧配置。所有输出目录均不会覆盖；Windows 未激活环境时将命令换成 `.\.venv\Scripts\beancount-budget-next-month.exe`。

### 待审的类别／用途：只填决定，不复制来源引用

已有月报配置后，可以把当前待审导出成填写表：

```bash
beancount-budget-review --config path/to/month.yaml --output review-month
```

打开 `review-month/decisions.yaml`，按日期、摘要、金额和账本行号核对。**每项只改三处：**

```yaml
decision: 生活开销.餐饮
reason: 核过原流水，是本人餐饮消费
approved: true
```

`kind: classification` 填收入／费用类别（相当于 `budget`）；`kind: asset_ownership` 填资产资金用途（相当于 `fund`）。可选名称在同目录的 `choices.yaml`。用途拆分可填 `decision: "保险200，电子产品900，旅游900"`，合计必须等于这条分录的绝对金额；正负号由原分录决定。明确预算外时填 `decision: "-"`。不确定的项目保持 `approved: false`，可以分批处理。

保存填写表，然后运行：

```bash
beancount-budget-review --apply review-month/decisions.yaml
```

程序保存已批准的决定到 `review-month/reviewed`，自动补来源引用并重跑月报。**此后用新配置继续：**

```bash
beancount-budget-tool --config review-month/reviewed/month.yaml
```

还没审完时，用这个新配置再次导出到另一个目录，只处理剩余项目。已有输出目录不会覆盖，需要另选目录时在 `--apply` 后加 `--output 新目录`。如果保存后发现下一层计划配置错误，终端仍会显示已保存的配置路径和错误位置；修正该配置后重跑月报即可。

账本和原审阅文件保持只读。已通过侧文件批准的分录，再补行内 `budget`／`fund` 会构成重复认领，需先撤掉对应外部认领再重审；不要同时使用两份决定。改动原交易后，旧填写表会拒绝使用已失效的引用，须重新导出。

这张表目前处理**已发现的分类与 H 资产用途**。`other-items.yaml` 保留账户、期初、负债、转账协议和计价等其他待审，它们仍需各自审核；保存后也可能出现下一层问题。全部月报检查通过才算签发。Windows 未激活环境时将命令名换成 `.\.venv\Scripts\beancount-budget-review.exe`；安装更新后可用。

### 已有私人批量分类规则

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

月报的待审清单会说明具体原因、受影响的指标，以及账本或配置位置。同一原因可能同时影响“实际可花”和“最多能花”，只会列一次。若看到 **`IMPLEMENTATION_BLOCKED`／程序实现缺口**，表示该交易形状或计算目前尚未支持，需要继续开发；这类项目会与需要你审阅的数据问题明确区分。不要为了消除实现缺口而改写正确的账务。

准备好当月配置后，直接运行月报入口：来源尚待审时会先列出交易和账本行号；来源已审但指标仍缺证据时会列出待审指标；各项通过后才显示这份已审月报。配置格式见[月报配置与命令](docs/v6-monthly-report.md)。终端交互浏览使用同一入口：

```bash
python -m beancount_budget_tool.v6.monthly_report \
  --config path/to/reviewed-month.yaml --format tui
```

TUI 用方向键或 `j/k` 滚动、`PgUp/PgDn` 翻页、`q` 退出；脚本调用可选 `--format text` 或 `json`。消费目标和储蓄／投资等用途月目标写在计划侧文件中，不能写在某笔工资或转账的 `budget` 标签里；漏存的月份也应保留目标。要让首页「最多能花」受生活费目标约束，还需在消费计划顶层指定 `spendable_cap_category_id`，并为该类别写本月子树总目标；[配置示例](docs/v6-monthly-report.md)列出了写法。

有旅游、储蓄等专项未偿卡债时，月报会要求明确哪份当前可支付现金用于偿还。可选的 `debt_coverage` 文件逐笔记录这项决定；也可明确没有专用覆盖，让未偿全额占用实际可花。具体写法见[月报说明中的专项卡债覆盖](docs/v6-monthly-report.md#专项卡债明确哪份现金用于偿还)。

## 4. 旧账按月迁移与校对

先保留原账本，使用 `beancount_budget_tool.tools.v6_make_review_copy` 生成一次完整审阅副本，或沿用已核过的审阅副本。每次保留修改前的完整账本，改完一个月后运行同一入口。例如修改 2023 年 7 月：

```bash
beancount-budget-review-month \
  --before path/to/checkpoint/main.bean \
  --after path/to/working/main.bean \
  --month 2023-07 --cutover 2026-09-01
```

**2026-09-01 cutover 以前**，该命令只核对两份账本是否都能加载、改动是否限于目标月、财务事实是否保持一致；不要求旧交易补 v6 标签，也不签发 v6 月报。若要为更早月份另行签发，须另定更早 cutover 并审定当时的期初用途。

**如果打算从首个账本月 2023-07 重新建立 v6 预算，改用 `--cutover 2023-07-01`。** `--before` 和 `--after` 必须是两个完整且独立的账本副本；首次可用相同内容的副本做基线检查。命令会先一次列出目标月的旧交易级标签等语法问题，附账本文件与行号。处理后重跑；准备好该月的审阅配置，再加 `--config` 进入账户、用途、责任和报表审计。月报只重放截至该月末的已记指令，因此不用先迁移未来月份的 v6 标签。来源预检清单可能只显示当前阻断层；修复后继续重跑，直至得到完整月报或下一层待审原因。不要把 `NEEDS_REVIEW` 当作已签发月报。

旧标签问题清零后，若还没写 `--config`，命令会打印首轮盘点：截至本月末需要审定的资产账户及其第一处账本位置、本月没有行内 `budget` 的收支分录按账户汇总及第一处位置。它还给出导出逐笔分类候选的命令。**这些笔数不是要求逐笔补标签**：审定的收入／费用默认类别可以覆盖普通分录。接着仍需人工建立账户语义、期初用途、资产与负债归属、分类默认值和本月计划等审阅文件，再把它们写入[月报配置](docs/v6-monthly-report.md)。此盘点不自动批准任何类别或用途，也不会签发月报。

PowerShell 中审核首月的命令是一行；路径换成你的两个完整账本副本：

```powershell
.\.venv\Scripts\beancount-budget-review-month.exe --before .\checkpoint\main.bean --after .\working\main.bean --month 2023-07 --cutover 2023-07-01
```

边修改边审阅时，在同一命令末尾加 `--watch`。它会先审核一次；之后保存账本目录中的 `.bean`／`.beancount` 或审阅目录中的 `.yaml`／`.yml`，会自动重跑并把新结果追加到终端。按 `Ctrl+C` 停止。`--before` 仍指向固定的修改前副本，不能随每次保存更新。准备好月报配置后，重启命令并加 `--config` 和 `--watch`，配置及其同目录的审阅 YAML 也会触发重跑。`--watch` 只支持文本输出；需要单份 JSON 时去掉它并运行一次 `--format json`。

**cutover 当月及之后**，给同一命令加 `--config path/to/reviewed-month.yaml`。配置须写本月、同一 cutover，并指向 `--after` 的账本。结构校验通过后，命令会继续列出来源待审、指标待审，或完整的已审月报；缺配置会明确提示。月报配置格式见[已审月报](docs/v6-monthly-report.md)。

如果本月纠正了原账中写错的账户、金额等财务事实，加 `--allow-financial-corrections`。输出会列出更正前后的 booked 分录及文件行号，供逐项核对；目标月以外的修改仍被拒绝。纯结构校验通过不代表预算数字已认证。账户、期初和证券资金池审阅不要求旧哈希相同；程序按当前账本重算。改动已认领交易仍会使该笔来源引用失效，须重审受影响的认领。完整月报只能由其独立审计签发。

若账本已改、旧侧文件仍指向原分录，可先运行 `beancount-budget-review-refs --config path/to/reviewed-month.yaml`，一次查看所有过期引用、旧 loader 版本和行内标签／侧文件重复认领，并定位审阅文件与账本行号。它只给候选，不自动替换已审决定；逐项复核后再跑月报。

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

# 从零开始，逐月核对账本

只需要读本页。字段不清楚时再查[配置参考](docs/CONFIGURATION.md)。旧版填写表、手工编译和项目登记命令已下线，已有旧配置见[迁移说明](LEGACY-WORKFLOWS.md)。

## 1. 准备程序和路径

Windows 可选单文件包 `beancount-budget-tool-windows-x64`，无需旁边的 `_internal`。新版在 `%LOCALAPPDATA%\BeancountBudgetTool\Runtime` 自动解包依赖，正常退出清理本次运行目录；不再依赖系统 TEMP 路径。另保留 `-folder` 目录兼容包，选择它时须保留 `_internal`。在 PowerShell／VS Code 终端运行，先将下面两个路径换成自己的：

```powershell
$ToolDir = "C:\Tools\beancount-budget-tool-windows-x64"
$LedgerDir = "D:\Beancount"
$Exe = Join-Path $ToolDir "beancount-budget-tool.exe"
$Config = Join-Path $LedgerDir "budget\simple\config.yml"
& $Exe --version
```

Mac 可下载 `beancount-budget-tool-macos-arm64`（Apple Silicon）或 `beancount-budget-tool-macos-x64`（Intel）。Artifact 的 zip 解压后，再解压其中的 tar.gz；其中 `beancount-budget-tool` 是单文件程序，可单独移动，没有外置 `_internal`。在解压目录先运行示例：

```bash
./beancount-budget-tool --config example/simple/config.yml --format tui
```

虚拟例子应显示最多能花 190 CNY。[Mac 启动说明](packaging/MACOS-START.md)有下载、解压及真实账本命令。也可从源码使用：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
```

下文 `& $Exe` 在 Mac 可执行版换成 `./beancount-budget-tool`，从源码运行则换成 `.venv/bin/python -m beancount_budget_tool`；路径用自己的绝对路径。命令分别在程序／项目目录执行，账本可以放在其他目录。

## 2. 创建两份配置，只做一次

**已经有 config.yml 和 budgets.yml：跳到第 4 步。**

只有旧 `month.yaml` 时，使用当前实际在用的最新文件复制迁移：

```powershell
& $Exe configure --from-config "$LedgerDir\budget\2023-07\compiled\month.yaml" --output "$LedgerDir\budget\simple"
```

旧版隐藏审阅记录不会读取或自动导入。请在账本、config.yml、budgets.yml 明确所需决定；原文件不会被程序删除。

没有任何预算配置时：

```powershell
& $Exe configure --ledger "$LedgerDir\main.bean" --month 2023-07 --output "$LedgerDir\budget\simple"
```

普通负债直接在 Liabilities 分录写责任用途 budget；不需要整户 purpose 或 liability_budget_mode 开关。旧纯分类标签须核对后迁移，不能将“偿债”直接当资金用途。

输出目录须不存在。两种方式只选一种，不用填表、编译或登记月份。

## 3. 确认 config.yml，填写 budgets.yml

打开 `budget/simple/config.yml`：

- 核对 `ledger`、`cutover`、`report_currency`。从 2023 年 7 月开始，cutover 就固定为 `'2023-07-01'`，以后不要每月重置。
- `defaults` 写默认收入用途、支出用途和费用类别，例如自由资金、生活费、生活开销。
- 每个账户的 `semantics` 选 `H`（账本数量代表真实余额）或 `K`（只有累计申赎，不能当持仓）。账户很多时可用[三组简写](docs/CONFIGURATION.md#账户多时按三组填写)，不用反复写相同属性。
- `payment_scope` 每个币种选 `payable`（可以直接支付）、`excluded`（不能直接支付）；不确定可保留 `null`／`pending`，程序会列待审。
- cutover 前有余额时，才需要 `opening` 的用途拆分。首次正数 Assets／Equity pad 初始化自动归默认收入用途，不算当月收入或正式划拨。
- 普通信用卡、应付款直接按 Liabilities 分录的 budget 责任用途自动计算，不用整户 purpose，也不用逐笔选择责任 ID。单位代垫应收、未来到期贷款才补必要的 role／due_on，见[配置参考](docs/CONFIGURATION.md)。本金与利息用分录区分，还款不重复计入消费额度。

打开 `budgets.yml`，按月份填写目标。例如：

```yaml
schema: 1
months:
  '2023-07':
    expense_targets:
      生活开销: not_assessed
    allocation_targets:
      生活费: not_assessed
      额外储蓄: not_assessed
```

历史目标记不清时，明确填 `not_assessed`；`null` 表示还没决定，`0` 是真实零额度。数字是计划目标，不会自动增加资金余额。新用途可以从账本 fund 自动识别，不需要再登记 ID。

完整字段、每月沿用目标、期初拆分和 K 账户含义见[配置参考](docs/CONFIGURATION.md)。

### 消费上限按发薪周期，而不是自然月

在 config.yml 顶层添加：

```yaml
expense_budget_basis: salary_cycle
```

在 budgets.yml 顶层保存实际发薪日及各周期目标（日期和金额只是示例，类别名使用你的费用类别）：

```yaml
salary_cycles:
  '2023-07-08':
    expense_targets:
      生活开销: 2000
  '2023-08-02':
    expense_targets:
      生活开销: 2000
```

7 月 8 日包含在第一周期；8 月 2 日全天归下一周期。每次实际发薪后补一条日期，不猜预计到账日；没有下一条时，末周期保持未结束，只算到查询截止日。目标记不清可填 `not_assessed`，不要填 null 或 0 代替。

只有一个消费总上限时，上面的周期可以压成 `"2023-08-14": "5299.32"`；类别沿用配置中的消费上限类别，不需要 expense_targets 这一层。

注意层级：`months` 只放 `'2023-07'` 这样的自然月，完整发薪日 `'2023-08-14'` 要放在顶层 `salary_cycles`，不能写进 months。

如果第一次工资到 8 月才到账，7 月没有工资，**7 月仍可核对并签出账务月报**；首个已配置周期之前会显示“工资周期目标尚未生效”，不给周期剩余额度或最多能花，也不编一个 7 月发薪日。即使这笔工资对应 7 月劳动报酬，预算周期仍从 8 月实际到账日开始。空的 salary_cycles 则表示连生效日期也没配置，仍需待审。

这种模式下，不需要另设自然月消费上限；将旧 `defaults`／`months` 中不再考核的 `expense_targets` 移走或明确改为 `not_assessed`，不要把同一额度在两处重复配置。已有划拨目标 `allocation_targets` 可以保留。

仍逐月审核账本，cutover 不变。7 月月报取 7 月 31 日的真实可支付资金，周期消费则从 7 月 8 日累计至 7 月 31 日；**最多能花＝可支付资金与该周期剩余额度的较小值**。额度不在月初重置，不拿未来交易来计算历史可花。

TUI 按 `w` 可从已保存的发薪日选完整周期，不再重填额度。也可直接查询：

```powershell
& $Exe report --config $Config --cycle 2023-07-08
& $Exe report --config $Config --cycle current --as-of 2023-07-31
```

第一条统计 7 月 8 日至 8 月 1 日，第二条只统计截至 7 月 31 日的当前周期。工资周期查询只给消费与目标差；可支付资金及最多能花由自然月末快照签出。周期早于 cutover 的缺失消费不能当零。

## 4. 修改 2023 年 7 月账本，运行核对

继续使用 importer 导入，review 时在**分录下面**添加标签：

```beancount
2023-07-25 * "午餐"
  Expenses:Food 25 CNY
    budget: "生活开销.餐饮"
  Assets:Bank -25 CNY
```

`budget` 决定费用／收入分类，`fund` 决定资产里的钱属于哪个用途。普通消费不写 fund，按默认支出用途处理；混合资金账户的部分转出可能需要显式 fund，不能替你猜。

一笔到账混有自己的收入和代收款时，负债腿不要写 `fund`。例如收到 480 元，其中自己的收入 240 元、另有 240 元待转交：

```beancount
2023-12-12 * "个人收入与代收款"
  Assets:Bank 480 CNY
    fund: "生活费"
  Liabilities:Colleague -240 CNY
  Income:Work -240 CNY
    budget: "自由资金"

2023-12-12 * "转交代收款"
  Liabilities:Colleague 240 CNY
  Assets:Wallet -240 CNY
```

收入只计 240 元，未转交的 240 元保留为责任；转交不计消费或正式用途划拨。收款须有唯一明确的资产用途；转交未写 `fund` 时按配置的默认支出用途（也可在资产腿明写）。只有同账户、同币种的唯一未结应付款才自动关联；多份未结或超额付款仍待审。`budget: "自由资金"` 与资产 `fund: "生活费"` 分别控制分类和资产用途，不必强行改成同名。

同账户正式分出生活费，可以这样记：

```beancount
2023-07-25 * "分出生活费"
  Assets:Bank -2000 CNY
    fund: "自由资金"
  Assets:Bank 2000 CNY
    fund: "生活费"
```

账户名换成账本真实科目。收入和首次初始化不会仅凭计划自动兑现这笔分配。

启动：

```powershell
& $Exe --config $Config --month 2023-07 --format tui
```

- 显示配置错误：到提示的文件、行号修改。配置预检未齐时，会说明交易尚未检查。
- 显示待审：选中后按 `Enter` 看完整位置与原因，修改账本／两份配置，保存后按 `r`。
- `e` 可保存一项分类／用途决定；如果已经用它认领某条分录，不要同时给同一腿再写行内标签。
- `f` 显示本月 `budgets.yml` 的填写指引；核对后由用户在对应月份填写 `completeness_confirmed: true`，程序不保存批准。不要求预测以后会发生什么。
- `IMPLEMENTATION_BLOCKED` 表示程序能力缺项，应反馈给开发者；不要改写正确账务来绕过。

显示 `CERTIFIED_SCOPED` 就得到了当前范围的已审月报。它不保证数据截止日之后的现实余额。按 `x` 可导出报告；有待审时只导出待审结果。

### 一笔转账还有手续费，可以写在同一笔吗？

可以。同币种 H 账户的组合转账按整笔金额和各用途核对，**不限制三条、四条或更多分录**。例如转移已有的额外储蓄，另用生活费支付提现费：

```beancount
2024-07-20 * "提现与手续费"
  Assets:Bank 10 CNY
    fund: "额外储蓄"
  Assets:Wallet -10 CNY
    fund: "额外储蓄"
  Expenses:Fee 0.10 CNY
  Assets:Wallet -0.10 CNY
    fund: "生活费"
```

纯多分录转账省略 fund 时，沿用现有的来源库存／转出顺序规则；唯一收款腿可继承完整用途向量，多户收款只在来源用途唯一时自动继承，混合来源的去向不明仍待审。

结果是额外储蓄只换账户、正式划拨 G=0，生活费消费 0.10 元；不会把转出的 10 元也算消费。**Wallet 交易前须已有这 10 元额外储蓄**；写 fund 是说明实际来源，不会凭空产生这份资金。

如果所有付款明确来自一种用途，也能同时划给其他用途并支付费用，程序单列费用和正式划拨。混合多用途转出又改用途、无法区分费用承担时，会列为数据待审，提示另记虚拟调拨。多个账户联合付费，能确定总额但不能确定各户分摊时，报告列「联合来源」，保留所有来源，不按金额或顺序猜配对。跨币、证券成本、K 申赎和负债仍按各自经济规则核验，账户数量不是处理边界。

### 专题统计 tracking 在哪里看

在费用分录下写 `tracking: "健康,大额支出"`，或在交易头下面写 tracking，让该交易的所有费用分录参与专题。名称不必登记到配置。

月报下方的“专题支出”按原币分别列出支出、退款和净额；TUI 向下滚动即可查看，区间／累计与 CSV 也会包含。只有参与的 Expenses 才统计，收入或纯转账不会变成专题支出。同一支出可同时归入多个专题，所以专题之间不能直接相加；budget 和 fund 的计算不受影响。

### 同日先充值、再付款，却报负余额

Beancount 同日交易会使用各文件的行号排序，跨文件时可能与实际先后不同。已确认充值实际在先时，在**充值交易头下面、第一条分录前面**加一行：

```beancount
2023-08-24 * "给微信零钱充值"
  budget-order: -1
  Assets:Bank -200 CNY
  Assets:Wallet 200 CNY
```

账户换成自己的，原有 fund／budget 保留。新版 budget 工具先处理 -1，再处理未填写（相当于 0）的同日普通交易；必要时可用 -2、-1、0、1 等明确先后，同值仍沿用原顺序。保存后按 `r`。日期不改，不必搬文件。

只在确认实际顺序时填写，不会自动把所有入账排在支出前；跨日补钱、日末确实不足仍报错。目前只支持不跨越成本交易或自动 pad 的无成本普通交易，不能用它重排证券批次。Fava／其他工具可能仍按其原排序展示；旧 exe 需升级才识别。已认领交易加元数据后，失效的旧审阅决定仍需重审。

### 账户有钱，却提示 H_OWNER_NEGATIVE

转账校验的是**当前账户内，被转出的那个资金用途**，有成本时还区分库存批次。提示会列出用途中文名称、划拨前余额、可划出金额、本次净划出、缺口及划拨后余额，并指向转出分录的文件行号。例如该用途原有 100 元、本次划出 150 元，缺口为 50 元；账户中其他用途的钱不会自动补进来。

核对转出腿的 `fund` 是否写了实际来源用途、此前是否完成了用途分配；如果同日入账实际在先，再按上一节确认顺序。消费已造成负数时，会同时显示旧负余额与本次新增缺口，不会把二者混成一笔。修改并保存后按 `r`。

### 信用卡消费或还款出现待审

选中待审项按 `e` 查看来源与修改指引，不会弹出批准保存表单。

- 消费或负债责任用途应在对应 Liabilities 分录的 `budget` 写清；资产资金用途写 `fund`。
- 多用途部分清偿应列出精确金额，程序不会按 FIFO、比例、账户名或同额猜测。
- 普通负债由 `config.yml` 的账户规则和账本标签自动核对；有多种解释就保留待审。
- 现有标签/规则不能唯一表达的复杂关联、同日未知先后、外币锁定清偿或证券池，保持待审或明确暂不支持；不新增配置字段，也不靠按键批准。详见[两文件合同](docs/STATELESS.md)。

修改账本或 YAML 保存后按 `r`，或等待 watch 重算。没有独立 reviews 文件，也不在程序中保存批准、理由或月末确认。月末完整性请在 budgets.yml 对应月份明确配置；按 `f` 仅显示填写指引。

## 5. 接着处理 8 月、9 月

7 月完成后，在 TUI 按 `n` 进入 8 月，或者下次启动：

```powershell
& $Exe --config $Config --month 2023-08 --format tui
```

仍使用同一个 config.yml、budgets.yml、cutover。只改 8 月标签，在 budgets.yml 增加需要的月份目标；新账户属性补在 config.yml。余额自动结转，不再复制期初，也不用生成下一月填写表。

往后每月同样处理。修改过旧月事实时应重审受影响月份；旧认领失效、复杂责任或外币证据可能仍需[高级入口](docs/CONFIGURATION.md#高级审阅数据)。

## 6. 常用按键与工资周期

| 按键 | 用法 |
|---|---|
| `r` | 重读账本及两份配置，重新计算 |
| `m`／`n` | 跳月份／下一月 |
| `↑↓`、`Enter`、`Esc` | 选项／完整详情／返回 |
| `/`、`?`、`q` | 筛选／帮助／退出 |
| `e`／`f` | 查看分类用途修改指引／查看月末 YAML 填写指引 |
| `a`、`p`、`x` | 已审交易明细／区间／导出 |
| `w` | 工资周期查询 |
| `h`／`d` | 会计科目内的预算用途历史／指定日日末用途分布 |

交易明细中按 `/` 可搜索摘要、账户、类别、用途、专题、显示金额及文件名／行号。父目录名和内部责任 ID 不参与普通关键词筛选，避免目录中的随机字符把无关交易也选进来；完整路径和 ID 仍可在详情查看。

保存了 `salary_cycles` 时，`w` 直接选择发薪日并复用额度；没有保存时仍可手填两次工资日、额度和类别做临时查询。**本次工资日包含在本周期，下一次工资日属于下一周期**。自然月目标不会自动折算成周期额度。

### 查一笔预算余额为什么变了／查看月中分布

在 TUI 按 **`h`**，填写截止日期，选择完整会计科目，再填预算名称（留空看全部）。流水从 cutover 开始，逐笔列出日期、预算增减、变动后余额、摘要、原因和**文件行号**。例如选工行卡和「额外储蓄」，就能追查这份钱的收入、转出及虚拟调拨。只查所选科目直接记账，不自动合并子科目。

按 **`d`**，输入如 `2024-03-16`，查看该日日末的用途分布。两个窗口都可用 PgUp／PgDn 翻页、Esc 返回；不会切走当前月份。查询窗口是本次只读结果，改账后退出窗口、重新打开即可更新。

**普通流水／日末分布要求截止日所在月份能签出月报**，查询时会重新核验；若还有待审，只显示待审原因，不拿旧报告或月末余额倒填当天。`--watch` 继续负责主月报的改动重算。H 显示原币余额；K 显示 cutover 后申赎用途净变动，证券池显示名义本金／账面成本，负债按用途显示欠款或预付。它们保留各自口径；这里没有计算月中“最多能花”或证券市值。

**遇到 `H_OWNER_NEGATIVE` 用途透支报错，也能查流水：**在报错页按 **`h`**，输入「自由资金」「生活费」等用途，或留空看该账户全部用途。窗口会显示期初、逐笔增减、余额、原因和文件行号；用 PgUp／PgDn 翻页，Esc 返回原报错。它严格停在**失败的整笔交易之前**，按账本及 `budget-order` 的顺序重放；失败交易和后来的补款都不计入。不要求该月先签发，也不会放行透支或签发局部月报。目前支持普通 H 资产账户；更早的用途尚不明确时，会指出阻断原因，不把未知余额当成零。改账后先按 `r` 或等自动重算，再打开流水。

也可用命令查询、导出，沿用同一份 config，不新增配置文件。Mac 示例（将可执行文件路径和完整账户名换成自己的）：

```bash
./beancount-budget-tool snapshot --config budget/simple/config.yml --as-of 2024-03-16
./beancount-budget-tool history --config budget/simple/config.yml --as-of 2024-03-31 \
  --account 'Assets:Bank:ICBC' --purpose '额外储蓄'
```

Windows PowerShell 写法相同，在已有 `$Exe` 变量时将命令开头替换为 `& $Exe`；换行用反引号，或把整条写在一行。`history` 可加 `--since 2024-03-01` 只看这一天起的流水（仍显示此前结转的期初），加 `--commodity CNY` 只看人民币。`--budget` 与 `--purpose` 同义。收支科目查询的是 budget 分类的累计净收支，资产查 fund，负债查责任用途。

两条命令都支持 `--format json`，或 `--format csv --output history.csv` 保存到新文件；CSV 含变动原因和完整来源位置，已有文件不会被覆盖。

报错前诊断也可从命令行查询（替换账户与错误来源；`--as-of` 选择错误所在月份，实际截止位置由 `--before-source` 决定）：

```bash
./beancount-budget-tool history --config budget/simple/config.yml --as-of 2024-09-30 \
  --account 'Assets:Bank:ICBC' --purpose '自由资金' \
  --before-source '/完整路径/src/transactions/2024/09/wechat.bean:134'
```

诊断输出明确标为未签发；JSON 的 `status` 为 `DIAGNOSTIC_ONLY`、`report` 为 `null`，流水放在 `history` 中。不会新增配置或审阅状态文件。

## 哪些文件要保留

日常保留账本、config.yml、budgets.yml 和可执行程序。目录兼容包的 `_internal` 仍是运行依赖；单文件包不需要它。

程序不创建或读取 `.budget-state` / `reviews.json.gz`，不再提供 `state`、`clean` 或 TUI 保存批准流程。旧记录原样保留且不影响计算，是否另存备份由用户决定，不会自动删除或加载。复杂依据应显式进入两份 YAML，缺失则待审。

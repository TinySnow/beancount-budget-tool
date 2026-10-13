# v6 已审月报：一条命令查看已认证结果

若希望月报首页给出受生活费预算约束的「最多能花」，在消费目标文件顶层指定 `spendable_cap_category_id`，并为该类别声明本月显式 `expense_subtree_targets`。首页取**已审用途现金可支付金额**与**该类别消费目标差**的较小值；两项原值在 JSON 中分别保留。上限类别可由使用者选择，不在公共代码里固定为「生活费」。未配置上限时只显示用途现金可支付金额，不称作「最多能花」；配置了上限但本月缺目标时列入指标待审。

```yaml
schema: 6
report_currency: CNY
spendable_cap_category_id: living
months:
  "2026-09":
    expense_subtree_targets:
      living: "2000"
```

首次给导入流水添加 `budget`／`fund` 标签，请先看 [QUICKSTART](../QUICKSTART.md)。

本页适用于**已有 v6 已审配置**的使用者。下面的 2026 年 9 月配置是维护者本地私人账本的实例，不随公开仓库发布；初次接入新账本仍需逐项建立并审阅这些输入，尚无一键生成完整月报的流程。

当前入口先检查本月来源待审项，再审计 **实际可花、消费目标差、用途划拨目标差、资产账户用途份额、证券资金池保护额与 `tracking` 专题**。有来源待审项时显示交易清单和账本行号；来源通过但某个指标未认证时显示指标及原因；全部通过后显示已审月报。消费类别保留支出、退款／冲回与净额；专题只汇总费用分录的原币金额，不改变预算类别或资金用途。同一费用可属于多个专题，因此专题间不能相加。账户用途按 H 资产的原币余额显示；非证券池账户中任一具名用途份额为负，会在文字报表警告并在 JSON 单列；证券池另按账面成本展示，K 与证券市值不并入这份余额。配置了收入、支出双默认用途时，实际可花只取默认支出用途的可支付余额；收入默认用途须显式划入后才可消费。已审人民币消费卡债若与负债账户余额逐项相符、责任用途属于默认支出用途，月末未还余额会从实际可花扣除一次，并在报告中列出；外币债务缺报告日清偿价、专项债务缺独占覆盖时仍待审。它目前只支持完整自然月，不是今天的实时首页，也不签发计划兑现后可花或完整历史净费用。

K 账面累计账户单列期初、cutover 后变动、期末，以及逐账户、逐用途的**已审申赎净变动**。只有 K 腿用途与同笔真实现金腿精确匹配的简单事件才进入已审切片；未审事件另列数量与账面变动。切片不含 cutover 前未知的用途分布，也不表示基金份额、市值或可赎回金额；不与真实 H 库存相加。

## 2026 年 9 月隔离副本配置示例

此私人配置的旧审阅文件绑定 Beancount 2.3.6；项目当前依赖 3.2.3。配置指向的隔离副本也比部分审阅文件所用的副本更新。直接运行会先因版本不一致停在分类校验；仅改版本号仍会遇到过期引用。下面的命令展示入口用法，须逐份复核审阅来源后才能用于这份账本。

可先用只读命令盘点配置中所有侧文件的过期引用及 loader 差异：

```bash
beancount-budget-review-refs --config path/to/reviewed-month.yaml
```

它列出审阅文件中的位置、当前账本里同文件同 booked 事实的**候选**来源，以及行内 `fund`／`budget` 与侧文件的重复认领；候选不能自动替换原批准。跨 loader 时先用 `beancount_budget_tool.tools.v6_compare_bookings` 分别生成两版 booked 快照并比较，再逐份重审受影响的引用、用途名册及金额，最后运行正式月报。原审 `ledger_snapshot` 是历史审阅凭据，不应为消除差异直接改成当前哈希。引用盘点本身不签发任何预算数字。

用途净划拨审计会接受已审的简单信用卡还款及单位代垫／报销，核对责任事件与 H 资产腿来源，并将它们记为 **G=0 的责任结算**。仅有用途标签而未审责任时仍待审。无需月度划拨目标的用途可在用途计划中显式写 `not_assessed`；文本月报仍显示它当月的实际净划拨，不能把未审实际划拨藏在“不考核”后面。

在项目根目录，使用已安装 Beancount 的 Python：

```bash
PYTHONPATH=src python -m beancount_budget_tool.v6.monthly_report \
  --config fixtures/private-ledger/reviews/v6-monthly-report-2026-09.yaml
```

需要待审来源和状态的结构化结果时加 `--format json`。只要来源或任一指标待审，JSON 的 `report` 就是 `null`，并在 `review_queue` 或 `pending_metrics` 列出阻断；通过全部审计后才提供报表。私有配置里的路径相对于配置文件所在目录；账本原件没有被改写。自己的月份须提供同样的已审输入，并把 `month` 改为目标月份；报表日自动取该月末。没有该日人工完整性声明时可省略 `completeness`，入口会报告实际可花待审。

完整性声明按指定月末使用，不要求账本哈希保持不变；旧声明中的 `ledger_snapshot` 和 `as_of_booked_fingerprint` 可留作历史记录，也可删除。补记旧月交易后，程序会重新计算现金与已入账责任；新交易缺用途或责任认领时，月报仍会列为待审。完整性声明只涉及其所写的月末，不把历史报表变成今天余额。

配置为 schema 6 YAML，字段如下。此例展示字段，路径须按自己的审阅文件填写：

```yaml
schema: 6
ledger: ../review-copy/main.bean
cutover: '2026-09-01'
month: '2026-09'
report_currency: CNY
accounts: accounts.yaml
opening: opening.yaml
ownership: ownership.yaml
obligations: obligations.yaml
completeness: completeness.yaml # 可省略；只适用于声明的月末
classifications: classifications.yaml
benefit_groups: benefits.yaml # 可省略；复杂收益未审时相关指标会待审
expense_plans: expense-plans.yaml
purpose_plans: purpose-plans.yaml
h_transfers: h-transfers.yaml
k_events: k-events.yaml
h_fx: h-fx.yaml
purpose_pools: purpose-pools.yaml
```

报表文字中消费“差额”是目标减实际，负数表示超支；用途“差额”是月划拨目标 Q 减已发生净划拨 G，负数表示超额划拨。外币消费保留原币事实，9 月目标比较可使用经核实的人民币还款成本，不能当作消费日历史汇率。证券池的金额是账面成本用途展示，市值未知。最新 9 月**临时候选**按已确认的钱包期初用途与生活费上限运行：生活费目标 2,000、成本 2,339.58，目标差 −339.58 CNY；用途现金可支付 −21.29 CNY；「最多能花」取较小值 −339.58 CNY。该候选未替换原已审侧文件，不代表历史正式重签。

历史月报只反映报表截止日。截止日之后发生的交易或负债不会倒扣到这份历史报表；若拿它判断今天可花，须另外核对其后的变化。`as_of` 也不能单独证明 importer 已经导入该日的全部流水。

## 新交易导入后先看待审清单

同一份私有配置可生成指定日期区间的来源待审清单：

```bash
PYTHONPATH=src python -m beancount_budget_tool.v6.review_queue \
  --config fixtures/private-ledger/reviews/v6-monthly-report-2026-09.yaml \
  --since 2026-10-01 --through 2026-10-12
```

`--format json` 保留完整 booked 来源引用，方便生成下一份审阅文件。清单按日期列出未认领的费用／收入分类、H 资产用途、负债事件、新账户语义与待定支付范围。经审阅的券商资金池内部买卖不要求逐腿标用途；与池外资金或负债的边界仍列出。清单为空仅表示这些**来源**暂无待办，不证明新月计划、人民币计价、用途库存或首页已经认证。

若所查区间没有已记交易，文字输出另显示最近一笔 booked 交易日期并提醒核对 importer 覆盖范围。交易最晚日期只是观察到的账本事实，不能证明此后真的没有发生交易。

## 续用已确认的私人分类习惯

旧私有 `v6-classification-rules-standing-2026-09.yaml` 记录了当时的分类偏好；它的普通 Income 默认仍是生活费。新需求已把普通 Income 默认改为自由资金，9 月临时候选的 `standing-classification-rules.yaml` 已体现这一点。证券交易费仍归生活费，已明确的证券／余额宝收益和逆回购结息归投资.待投资金，银行结息及已逐笔确认的返现保留原分类。这些规则只在个人侧文件，不写入公共分类器。新返现与尚未明确的金融收入被默认规则排除，留给逐笔审阅；9 月批准的餐饮、交通等账户到子类的映射也只适用于 9 月，没有自动延长。

导入新流水后，可用一条命令续用已审分类、生成**新**侧文件并查看待审清单。原有侧文件不覆盖；新文件会先经分类及待审审计，再写入指定路径：

```bash
PYTHONPATH=src python -m beancount_budget_tool.tools.v6_refresh_review \
  --config path/to/reviewed-month.yaml \
  --rules path/to/matching-approved-rules.yaml \
  --since 2026-10-01 --through 2026-10-12 \
  --output-classifications path/to/classifications-next.yaml
```

规则文件与配置所指的分类文件须使用同一类别名册；新私人规则把普通 Income 默认归自由资金。工具拒绝覆盖输出文件，逐条重验旧认领；新收入若落在返现或未知金融收入账户仍会待审。它不自动认领资产用途、债务、资金池外边界或新月计划。以后要用新分类文件跑完整月报，须在该月报配置里把 `classifications` 指向新文件，并另备该月已审输入。

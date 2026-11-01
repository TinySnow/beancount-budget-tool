# 月报与明确输入

日常操作见 [QUICKSTART](../QUICKSTART.md)，配置见 [CONFIGURATION](CONFIGURATION.md)。只维护两份 YAML 与账本，不需要手工生成下列内部文件。

## 输出合同

工具先检查来源，再核对账户用途、责任、消费／划拨目标和支付范围。结果为：

- `CERTIFIED_SCOPED`：指定 cutover 与截止范围内的已审月报。
- `NEEDS_REVIEW`：事实尚不明确，给出原因与文件行号，不显示正式数字。
- `IMPLEMENTATION_BLOCKED`：程序尚不支持该协议；人工批准不能代替计算。

消费只统计 Expenses，Liabilities 的 budget 单独统计责任用途，不重复消费。`fund` 记录 Assets 的实际用途；G 只记录正式净划拨。K 账面用途、证券池成本与 H 真实库存分别列示，不相加冒充市值或可提现金额。

混合用途坐标的缺失归属不再以默认用途兜底。核对依据是交易发生前的账户／原币／批次库存；多个非零用途或此前归属未决时，未有显式认领或可证明协议的资产腿列 `H_OWNER_PENDING`。这可能使旧版曾签发的月份转为待审，是为避免用途分错而有意收紧；不能用“各用途合计等于账户总额”替代逐笔归属证明。程序仅指出来源，不自动改真实账本。普通单／零用途默认兼容、明确切片及已证明搬移／原用途退回协议保持。

两文件入口的月报、区间与发薪周期都会阻断 `H_OWNER_PENDING`。周期按 cutover 至周期截止日重放来源，过去未决归属可能影响起点库存，不能因进入新周期而略过；截止日后的交易不参与判断。这项来源门禁不要求自然月目标或月末完整性声明，也不把消费周期报告改成现金上限报告。只有未来才出现的 K 账户，其已有零期初确认可随完整配置保留，不把未来账面变动引入早期周期。

月报按配置的 `defaults.expense_purpose` 显示“生活费可支付现金（默认支出用途）”等名称；它只指支付范围内该用途经债务预留、未覆盖责任等核对后的现金，不是全部 H 资产或全部用途余额。其他用途可支付余额单列，不并入该用途现金与上限。**最多能花 = min（默认支出用途可支付现金，对应目标／发薪周期剩余额度）**。未知历史目标可写 `not_assessed`，允许核对账务但不签发额度数值。

自然月负责核对账务。自然月消费目标按月独立，剩余额度不自动追加到下月目标；同一发薪周期跨自然月累计，新发薪日开始新周期，使用新目标，不自动承接旧周期剩余额度。实际现金余额始终随账本延续。月报的周期进度只统计至月末，TUI `w` 可查完整周期。月末之后现实中发生的新交易不纳入历史报告，报告列出截止日提醒。

cutover 是账本重放的起点，不是顺序关账的起点。期初余额来自此前账本交易，不依赖上月是否签发月报；本月签发也不代表此前每个月都已签发。区间报告会审核区间内各月，任一月待审则不签发该区间合计。

累计用途视图以期末余额回答“这笔多年积攒的用途资金还剩多少”，不把各月期末快照相加。`report.cumulative.purposes` 按用途和币种分行提供 `h_units`、`available_cash`，另列 `pool_book_cost`、`k_book_change` 和所选区间的划拨流量 `actual_g`／`target_q`。未知目标、外币可支付金额使用 null，不推定为零；K 账面及池成本不计入可支付现金。累计 Q/G 不是剩余额度，也不与库存相加。

月报正文默认只显示当月有活动、当前余额或有效目标的科目，保留零目标、负余额与原有警告；`z` 查看全部。该折叠仅影响展示，完整 JSON 不删行。K 月报汇总按当月事件显示账户、账面变动与笔数，相抵净零的事件仍显示；全部模式可查看截至月末的全部 K 账户。`report.k_flows` 保存当前月申赎事件及来源；区间合并各月 `k_flows` 和 `tracking_events`，不改既有审计金额。CSV 追加 `purpose_cumulative`、`k_flow`、`tracking_flow` 区段。

## 没有独立批准文件

程序只根据账本和两份YAML的现有字段判断。TUI `e`／`f`仅显示来源与配置指引，不保存批准或月末确认；缺少依据时保持待审。旧审阅状态不读取、不导入、不监听，也不自动删除。现有输入不能表达的复杂协议待审或明确不支持，不增加字段，见[零隐藏状态合同](STATELESS.md)。原始来源身份、精确守恒和截止日核验不变。

## 后端输入用途

| 输入 | 内容 |
|---|---|
| accounts、opening | H/K 语义、支付范围和期初用途 |
| ownership、classifications | 有来源的用途／类别认领和默认规则 |
| obligations | 逐用途责任、应收和清偿协议；普通场景从账本生成 |
| expense_plans、purpose_plans、salary_cycle | 消费、划拨、发薪周期目标 |
| h_transfers、k_events、h_fx | 必要的复杂转账、账面申赎和外币协议 |
| benefit_groups、purpose_pools | 证券成本利益来源组及本金保护 |
| debt_coverage、debt_settlements、completeness | 报告日适用的专款覆盖、锁定清偿成本、资料核对 |

这些模块保留独立来源证明与精确金额校验。完整性、到期和清偿报价绑定时点；未来证据不倒填过去。不同币种不靠无来源汇率合并。

## 查询与导出

TUI `m` 跳月、`[`／`]` 上下月、`p` 区间、`u` 累计用途余额、`w` 发薪周期、`i` K 申赎流水、`v` 专题流水、`h` 科目用途历史、`d` 指定日日末分布、`x` 导出。周期／区间内 Esc 返回月报，m 也可跳月。`--watch` 监测配置与 include 账本保存，错误页及流水弹窗也可自动重查；失败时不保留旧数字。终端缩放同步尺寸并强制完整重画。详情与筛选不减少待审总数。

```bash
beancount-budget-tool --config budget/simple/config.yml --month 2024-03 --format json --transactions
beancount-budget-tool report --config budget/simple/config.yml --from 2024-01 --through 2024-03 --format csv --output report.csv
beancount-budget-tool snapshot --config budget/simple/config.yml --as-of 2024-03-15 --format json
beancount-budget-tool history --config budget/simple/config.yml --as-of 2024-03-31 --account Assets:Bank --purpose 生活费 --format json
```

历史与日日末查询先重新核对所在月，月未签发则保留待审；不能用查询绕过整月校验。日日末分布是账户用途份额，不代表任意日经责任核对后的可支付现金或基金市值。CSV 使用 UTF-8 BOM，已有导出文件不覆盖。

现有范围和待实现的复杂协议见[实施状态](v6-implementation-status.md)；代码关系见[架构导读](ARCHITECTURE.md)。

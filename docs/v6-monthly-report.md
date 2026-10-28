# 月报与明确输入

日常操作见 [QUICKSTART](../QUICKSTART.md)，配置见 [CONFIGURATION](CONFIGURATION.md)。只维护两份 YAML 与账本，不需要手工生成下列内部文件。

## 输出合同

工具先检查来源，再核对账户用途、责任、消费／划拨目标和支付范围。结果为：

- `CERTIFIED_SCOPED`：指定 cutover 与截止范围内的已审月报。
- `NEEDS_REVIEW`：事实尚不明确，给出原因与文件行号，不显示正式数字。
- `IMPLEMENTATION_BLOCKED`：程序尚不支持该协议；人工批准不能代替计算。

消费只统计 Expenses，Liabilities 的 budget 单独统计责任用途，不重复消费。`fund` 记录 Assets 的实际用途；G 只记录正式净划拨。K 账面用途、证券池成本与 H 真实库存分别列示，不相加冒充市值或可提现金额。

自然月负责核对账务。消费上限可选择自然月或发薪周期；最多能花取已审可支付资金与相应目标余额的较小值。未知历史目标可写 `not_assessed`，允许核对账务但不签发额度数值。月末之后现实中发生的新交易不纳入历史报告，报告列出截止日提醒。

## 没有独立批准文件

程序只根据账本和两份YAML判断。TUI `e`／`f`仅显示来源与配置指引，不保存批准或月末确认；缺少依据时保持待审。旧审阅状态不读取、不导入、不监听，也不自动删除。复杂明确依据写在两份YAML的`declarations`，见[零隐藏状态合同](STATELESS.md)。原始来源身份、精确守恒和截止日核验不变。

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

TUI `m` 跳月、`[`／`]` 上下月、`p` 区间、`w` 发薪周期、`h` 科目用途历史、`d` 指定日日末分布、`x` 导出。`--watch` 监测配置与 include 账本保存，错误页也可自动重算。详情与筛选不减少待审总数。

```bash
beancount-budget-tool --config budget/simple/config.yml --month 2024-03 --format json --transactions
beancount-budget-tool report --config budget/simple/config.yml --from 2024-01 --through 2024-03 --format csv --output report.csv
beancount-budget-tool snapshot --config budget/simple/config.yml --as-of 2024-03-15 --format json
beancount-budget-tool history --config budget/simple/config.yml --as-of 2024-03-31 --account Assets:Bank --purpose 生活费 --format json
```

历史与日日末查询先重新核对所在月，月未签发则保留待审；不能用查询绕过整月校验。日日末分布不是任意日的实际可花或基金市值。CSV 使用 UTF-8 BOM，已有导出文件不覆盖。

现有范围和待实现的复杂协议见[实施状态](v6-implementation-status.md)；代码关系见[架构导读](ARCHITECTURE.md)。

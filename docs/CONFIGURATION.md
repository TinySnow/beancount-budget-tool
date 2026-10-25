# 配置参考：config.yml 与 budgets.yml

从零操作看 [QUICKSTART](../QUICKSTART.md)。本页列出两文件入口实际支持的字段。它们都用 `schema: 1`；旧 `month.yaml` 的 `schema: 6` 是兼容格式，不要混用字段。

## config.yml：稳定规则

```yaml
schema: 1
ledger: ../../main.bean
budgets: budgets.yml
cutover: '2023-07-01'
month: '2023-07'
report_currency: CNY
defaults:
  income_purpose: 自由资金
  expense_purpose: 生活费
  expense_category: 生活开销
accounts:
  Assets:Bank:
    semantics: H
    payment_scope:
      CNY: payable
```

路径相对于 config.yml 所在目录。账户名必须与账本一致。示例只是结构，请使用实际账户和目录。

| 字段 | 必填 | 含义 |
|---|---|---|
| `schema` | 是 | 固定为 `1` |
| `ledger` | 是 | 根账本文件，可包含其他账本文件 |
| `budgets` | 是 | 目标文件路径，通常为 `budgets.yml` |
| `cutover` | 是 | 从哪个自然月第一天开始重放预算；逐月操作时保持不变 |
| `month` | 是 | 默认查询月份；启动可用 `--month` 覆盖，TUI 可用 `m`／`n` 切换 |
| `report_currency` | 是 | 报告币种，必须在账本 operating_currency 中 |
| `defaults.income_purpose` | 是 | 收入资产腿省略 fund 时归入的用途；也用于首次符合条件的 pad 初始化 |
| `defaults.expense_purpose` | 是 | 普通消费、退款资产腿省略 fund 时使用的用途 |
| `defaults.expense_category` | 是 | 费用省略 budget 时使用的分类；也是默认消费总上限类别 |
| `defaults.income_category` | 否 | Income 省略 budget 的分类；默认同 income_purpose，迁移时可能单独保留 |
| `accounts` | 是 | 账户名到属性的映射；新账户通常在这里补充 |
| `account_defaults` | 否 | 未单独列出的账户使用的属性；结构同账户。只有明确希望未来新账户也按此处理时才设置 |
| `spendable_cap_category` | 否 | 用哪个费用类别及其子类的目标差限制最多能花；默认 expense_category。`null` 关闭此指标，不按现金假算上限 |
| `classification_defaults` | 否 | 默认 `true`；`false` 禁用未写 budget 的默认分类。某些旧配置迁移后会保留 false，不影响资产默认 fund 用途 |
| `expense_budget_basis` | 否 | 默认 `calendar_month` 沿用自然月消费上限；`salary_cycle` 改为按当前发薪周期剩余额度限制最多能花。账务审核和资金快照仍按自然月 |

### 账户属性

| 字段 | 可填什么 |
|---|---|
| `semantics` | `H`：真实原币数量／余额；`K`：只有累计申赎，不把账面数当基金份额或市值。`null` 尚未决定 |
| `payment_scope` | 每个币种填 `payable`、`excluded` 或 `pending`／`null`。是否能直接支付与是否持有专款是两件事；K 必须 excluded |
| `opening` | cutover 之前的余额用途拆分。没有非零期初可省略；不要逐月再填一次 |

例如银行里有自由资金 100、储蓄 200，真实期初余额须合计为 300：

```yaml
accounts:
  Assets:Bank:
    semantics: H
    payment_scope: {CNY: payable}
    opening:
      CNY:
        自由资金: '100'
        储蓄: '200'
```

无成本原币余额用上面的简写。带成本批次的高级 H 期初使用完整坐标列表：`commodity`、`lot`、`signed_units`、`owners`；迁移会保留已审内容，程序按真实 booked 坐标核对，不能猜 lot。K 不填写用途持仓切片。

首次正数、无成本 Assets／Equity pad 在该坐标此前没有交易时，自动归默认收入用途；不算收入或正式划拨。已有记录后的 pad 仍是余额校正，不能反复当作初始化。Beancount 自带的 balance 不要删除。

## budgets.yml：目标

```yaml
schema: 1
defaults:
  expense_targets:
    生活开销: 2000
  allocation_targets:
    生活费: 2000
effective_templates:
  '2023-09':
    expense_targets:
      生活开销: 2200
months:
  '2023-07':
    expense_targets:
      生活开销: not_assessed
    allocation_targets:
      额外储蓄: not_assessed
```

这些是目标示例，不代表资金已实际分配。

| 字段 | 含义 |
|---|---|
| `schema` | 必填，固定为 `1` |
| `purposes` | 可选的用途名称列表，预先声明尚无交易的用途；明确 fund 和目标名称也会自动发现，无需重复列出 |
| `defaults` | 每月沿用的目标 |
| `effective_templates` | 从某月起沿用，直到被后续模板覆盖 |
| `months` | 只覆盖指定月份的同名目标 |
| `expense_targets` | 消费目标，包含该类别及所有子类别 |
| `expense_direct_targets` | 高级用法：只考核该节点直接费用，不包含子类 |
| `allocation_targets` | 正式用途净划拨目标 Q，实际 G 从账本得出；不等于消费目标或持仓 |
| `months.<月份>.completeness_confirmed` | 只针对该月的资料完整性声明：true 已核对，false 明确未确认。不能放在 defaults 中跨月自动批准；也可省略，用 TUI `f` 核对并保存记录 |

覆盖顺序是 defaults → 已生效模板（按日期顺序）→ 当月设置，按每个名称覆盖，未覆盖项继续沿用。

目标金额可以是整数、普通小数或金额字符串，按原文精确解析，不经二进制浮点取近似。金额必须有限且非负。

| 写法 | 结果 |
|---|---|
| `2000`／`'2000.50'` | 真实目标 |
| `0` | 真实零额度，不是未知值 |
| `not_assessed` | 明确不考核，仍计算实际收支／用途余额 |
| `null` | 尚未决定，列待审 |
| 没有设置 | 新用途通常不考核；迁移的旧目标缺失会保留为 null 待审。只设子类目标时父类可派生汇总，但不能把派生值冒充整个父类别硬上限 |

要按「整个生活费最多 2000」限制可花，应在所选口径中设置对应费用父节点的 `expense_targets`。历史上限不记得时填 not_assessed；此时不计算目标差及受该目标限制的最多能花，实际现金和消费仍另行核对。

### 持久保存发薪周期目标

当 config.yml 的 `expense_budget_basis: salary_cycle` 时，在 budgets.yml 顶层添加：

```yaml
salary_cycles:
  '2023-07-08':
    expense_targets:
      生活开销: 2000
      生活开销.餐饮: 800
  '2023-08-02':
    expense_targets:
      生活开销: 2000
```

每个键是已确认的实际发薪日，包含当天；下一条发薪日结束上一周期，下一发薪日全天归新周期。末周期不假定未来发薪日，只统计到查询截止日。只设置周期目标时，不要求自然月 `expense_targets`；旧自然月字段若保留，仍作为独立月度目标审核，不会被静默覆盖。周期字段不继承月度 defaults 或 effective_templates，不会在月初重置。

`months`／`effective_templates` 的键只能是 YYYY-MM；YYYY-MM-DD 的发薪日放在顶层 salary_cycles。首个已配置日期同时定义周期目标的生效起点：之前月份仍正常审核费用、资产和责任，但周期状态为 `NOT_STARTED`、上限为 `NOT_ASSESSED`，不编造周期或零额度，也不将现金余额冒称最多能花。若 salary_cycles 完全为空，则尚未定义任何生效区间，保留待审。日期代表到账日，不因工资归属上个月而回填。

周期的 `expense_targets` 均含子类别，父子目标各自检查，不把它们相加。金额、精确小数、0、null 和 not_assessed 的意义与前文一致；缺少当前上限类别的周期目标会待审并定位 budgets.yml。类别名称自动登记，别名不能与原名重复设置同一目标。切换此模式不改变 fund、预算划拨 G、实际现金、债务或 cutover，也不创造或提前认定收入。

月报仅使用**月末所在的当前周期、截至该月末**的消费，最多能花取已审可支付资金与周期剩余额度的较小值；月报、累计及 CSV 会注明周期和统计截止日，周期余额快照不相加。TUI `w`／`report --cycle YYYY-MM-DD` 使用已保存的目标；闭合周期默认查完整范围，末周期默认查配置月份末。`--cycle current --as-of YYYY-MM-DD` 可指定截止日。单独周期报告不签发现金库存或可花，月末完整性也须单独确认。

## 账本标签与配置的分工

- `budget`：费用／收入分类；`fund`：资产资金用途；`tracking`：专题。
- 标签在分录下面。省略标签时只使用配置中可确定的默认规则；混合资金部分转出不能唯一判断时待审。
- 多用途可写 `fund: "保险200,电子产品900,旅游900"`。每项为正数金额，合计须等于该分录金额的绝对值；扣款的符号由分录决定，不能凭标签增加钱。
- 新用途名称自动识别只代表名称可使用，不替代金额守恒、真实余额、责任、计价和流向检查。
- 工资周期可用上述 salary_cycles 长期保存；没有保存时，TUI `w` 或 report 手填参数仍可临时查询，不复制自然月目标。

`tracking: "健康,大额支出"` 可写在交易头下（统计该笔全部 Expenses）或费用分录下（只统计该腿），名称不必登记。同笔同腿重复出现同一专题只算一次。已审月报下方“专题支出”、区间／累计及 CSV 都按原币列出支出、退款与净额；不统计 Income 或纯转账，也不把多个重叠专题相加为总消费。

### 显式同日顺序：budget-order

这是账本**交易级**元数据，不是 config 字段，也不是分录 fund：

```beancount
2023-08-24 * "充值确实先发生"
  budget-order: -1
  Assets:Bank -200 CNY
  Assets:Wallet 200 CNY
```

值为不加引号的整数，越小越先；未填为 0，同值保留 Beancount 原顺序。只改变同一日期普通交易的预算重放先后，跨 include 文件有效，原日期、金额、账户、价格、成本和源行号不变。程序不根据金额、账户名或负余额推测顺序。

`budget-order v1` 只支持无成本普通交易。不得标记成本交易／自动 pad，也不得跨越这两类事件，以免影响成本批次或初始化语义；遇到这些形状返回 TRANSACTION_ORDER_UNSUPPORTED。不能在 posting、其他指令或 pushmeta 中设置。真实负库存检查、用途校验和跨日边界仍然生效；Fava 等外部工具不会因此自动改排序。添加元数据改变了来源内容，已有来源认领仍按正常规则校验失效。

## 文件与存储

`_internal` 是目录版 exe 的依赖；完整保留。单文件版没有这个目录，启动前会解包依赖；不能通过删除目录版依赖来转换版本。Windows 单文件版在 `%LOCALAPPDATA%\BeancountBudgetTool\Runtime` 下创建每次运行的私有解包目录，正常退出清理；该设置在 Python 启动前生效，不依赖系统 TEMP 路径。原 VS Code 故障根因尚未确认，不能据此承诺所有权限问题已经解决；目录版保留作兼容选择。

当前 Mac 的 arm64／x64 包均为单文件版，可单独移动 `beancount-budget-tool`；Windows 仍提供目录版与单文件版。更换程序不影响账本、两份配置和审阅状态，后者继续单独备份。

正常使用只会在有已审数据时保存 `.budget-state/config/reviews.json.gz`，包含已审来源决定、月末声明、债务证据和恢复资料。它不是缓存，需备份。运行输入放系统临时目录，正常重算／退出自动释放；纯行内配置可完全没有持久状态。

升级后退出其他预算工具窗口，执行 `clean --config config.yml`，可将旧 latest.yaml、snapshots、reviews 收拢并删除旧 runtime 缓存。`--dry-run` 只预览。文件损坏或无法证明安全时会保留原文件，不通过删除审阅记录来消除错误。

## 高级审阅数据

复杂证券池、责任、同日结算和旧认领失效处理继续兼容高级入口；不会为了减少文件而自动批准。需要修改时，可导出一份自包含副本：

```powershell
& $Exe state --config $Config --export "D:\budget-advanced"
# 用高级工具核对、修改副本后，再保存其最新 month.yaml：
& $Exe state --config $Config --import-reviewed "D:\budget-advanced\month.yaml"
```

输出目录必须不存在。使用刚导出的最新副本，避免覆盖后来保存的决定；不要直接编辑压缩状态。日常逐月标账无需执行这两条命令。高级输入说明见[月报与待审](v6-monthly-report.md)。

# 当前代码结构

日常入口只有 `config.yml`、`budgets.yml` 和 Beancount 账本。没有先填表、编译、登记月份的操作。CLI、TUI 和可执行文件共用 `launcher.py`；不带参数时寻找当前目录的 config.yml 或 budget/simple/config.yml、budget/config.yml，发现多个则要求明确选择。

## 四层职责

| 层 | 主要模块 | 职责 |
|---|---|---|
| 读入与来源 | financial、refs、v6/source、facts、provenance | 保留原文、booked 数量／成本和来源位置，检查来源变更 |
| 配置组装 | simple_config、input_compiler、state_store | 两份配置加已审记录，生成本轮临时输入；原文件不变 |
| 计算与证明 | ownership、purpose_flows、liability_budgets、obligations、k_events 等 | 复用完整交易等式，重放用途、责任和申赎；不能唯一确定的输入待审 |
| 输出与交互 | monthly_report、period_report、account_queries、tui、simple_tui | 同一结果生成文本／JSON／CSV，支持切月、区间、发薪周期、历史和日末查询 |

内部输入是计算接口，不是另一套需要手工维护的配置。运行时临时文件会释放；TUI 保存的来源认领、理由和月末确认放在 `.budget-state` 的单个压缩状态文件中。状态文件属于用户数据，不能当作缓存删除。`state --export` 可导出可检查的副本，`state --import-reviewed` 导入来源有效的审阅记录。

## 一套普通负债计算

Liabilities 分录的 `budget` 是该笔责任用途，支持多用途金额。普通即期应付款自动发现；无需整户配置 purpose，也无需逐笔选择责任 ID。缺标签时使用已知单一用途、可唯一确定的完整清偿，或新增责任的默认支出用途；不能唯一分配的部分清偿待审。

`liability_rules` 证明完整原币交易形状，`liability_budgets` 按账户／用途／币种重放欠款和预付。原来的整户标量余额重放已移除，现金用途校验与正式责任报表复用同一份责任向量。`CreditOwners` 另外追踪预付款的真实资产用途：它与“谁承担债务”是两个问题，不能合并成一个余额。

可选 `liabilities` 仅声明财务性质 `role` 和贷款 `due_on`，例如单位代垫应收和未来到期贷款。程序不能从用途名称猜借款性质或到期日。这不是第二套用途模式。

复杂来源清算、证券成本组、外币锁定清偿等后端协议继续保留。它们验证当前自动流程不能表达的事实，不提供旧版的项目登记或填表命令。

## 通用交易与专门协议

同币无成本 H 转账／费用组按整笔守恒计算，支持任意数量分录。H 理财本金加损益、K 账面申赎、证券成本出售、外币、负债分别复用各自的来源证明。不能为了减少文件，把 K 账面金额误当真实库存，或把本金偿还当 Expenses。

主动用途划拨不能透支原用途；被动消费形成负用途保留负数和警告。G 记录正式净划拨，消费和清偿另记用途库存与收付。计划不制造现金。

## 本轮删除与保留

删除 tools 下的一次性迁移 CLI、project/project_actions、下一月填表分支和整户负债 purpose 模式；TUI 在同一份配置上直接切月。来源引用诊断保留为 `state --check`，不签发财务数字。

旧 `init`、`project`、`review`、`next-month` 等命令会明确提示已下线。旧整户 purpose、classification 模式及可识别的旧状态会给出有位置的迁移提示，不把旧“偿债”分类静默解释为新责任用途。已有 `liability_budget_mode: purpose` 可保留，省略也相同。

测试中的 fixture_inputs／fixture_months 仅组装边界测试用的后端输入，不随安装包发布。九例连续验收、来源失效、跨月责任、币种和容量守恒测试继续保留。

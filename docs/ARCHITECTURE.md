# 查询架构

输入仍是 config.yml、budgets.yml 和 Beancount 账本。功能合同见 [FEATURES.md](FEATURES.md)，重构验收见 [REBUILD.md](REBUILD.md)。

| 组件 | 实现 | 职责与边界 |
|---|---|---|
| 来源快照 | source_snapshot、financial | 一次官方parser同时获得include、raw位置和记账输入，再运行官方booking、transformations、validation，前后核对来源 |
| 稳定身份与事实表 | refs、identity、v6/facts | 外部claim不变；原始身份跨截止日共享，有界FactTable只暴露当前可见分录、日期和来源 |
| 运行输入 | runtime_inputs、simple_config | 只读取账本与两份公开YAML，构造内存文档；不加载独立审阅状态 |
| 查询会话 | query.QuerySession | 统一月报/区间入口，拥有输入文档、事实索引与投影图生命周期，结束前执行来源验证 |
| 投影图 | projection.ProjectionGraph、shared_audit | 按输入视图、参数及Path内容摘要复用纯审计；内部结果递归只读，出口恢复独立可变副本 |
| 财务规则 | v6各域 | 账户、用途、类别、负债、投资、外币与周期分别证明；规则和校验没有被删减 |
| 展示与写适配 | monthly_report、period_report、tui | 文本/JSON/CSV、来源与配置修改指引；不提供批准保存 |

## 生命周期

audit_simple与audit_simple_period是原API薄适配，编排交给QuerySession。区间公用完整来源和原始身份，但财务投影仍按各月截止日、配置及批准范围隔离。

来源校验从冻结的官方include语法独立重建可达闭包，并重新展开glob，核对文件名、物理来源及include边；不以capture自己生成的options名单证明完整性。官方loader的完整entries/errors/options另有多文件、递归glob、软链、重复和无匹配边界对照测试。每个源文件仍只解析一次。

只读月报和区间不再生成临时schema 6文件再回读。内部文档作用域结束即不可见，内容不可修改，消费者收到独立副本。物理用户文件仍每次读内容验证，不能由虚拟文档掩盖变化。

同一截止日视图的账户基础盘点与声明overlay分层；用途来源/声明/行内切片准备在opening变体之间共享，但期初容量和真实库存仍分别重放。准备层显式接收该视图FactIndex作为依赖，只存身份键和纯值，不把booked AST放进只读投影。进一步性能与边界见[ROUND2-PERFORMANCE.md](ROUND2-PERFORMANCE.md)。

高级明确协议放在两份公开YAML的declarations中，继续使用原严格财务验证。TUI计算进入同一QuerySession，e/f仅说明如何修改公开输入，不保存批准。旧审阅状态不读取、不导入、不监听，也不会被自动删除。旧多文件配置仅在用户显式configure迁移时读取，输出仍为两份YAML。

## 只读计算与输出

投影图归查询会话所有，与可复用来源快照分开。共享结果使用FrozenDict/FrozenList、不可变元组/集合和已支持的不可变标量，拒绝未知可变对象；注解需复制。外部API仍返回普通字典和列表，修改返回值不会污染后续查询。

引用外部JSON、交易摘要、occurrence、booked子腿、lot和fact摘要均不变。内部使用身份对象的稳定key和集中join表。新解析或TUI重新解包后建立独立缓存，旧对象ID不会进入新数据。

H真实库存与K累计申赎、budget类别与fund用途、责任用途与预付来源、成本与市值、消费与本金、真实现金与未来计划继续分开。主动划拨不能透支，歧义待审，重复责任和覆盖资源按原协议去重。

旧CLI/配置下线与迁移诊断继续保留，见LEGACY-WORKFLOWS.md；没有删除高级金融协议或把实现缺口改成人工批准。

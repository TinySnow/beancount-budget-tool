# 当前功能与重构验收合同

重构副本保留下面全部用户能力。内部数据通路可以重写；不能将删除后端证明、忽略待审或减少输出字段作为性能收益。

| 功能域 | 当前入口／实现 | 必须保留的行为 |
|---|---|---|
| 启动与配置 | launcher、simple_config | 两文件入口、默认发现、歧义拒绝、跨平台相对路径、定位到配置行 |
| 创建与迁移 | configure、LEGACY-WORKFLOWS | 新配置不伪造批准，保留原始数据，旧语义给明确迁移提示 |
| 月报 | monthly_report | 完整已审结果或完整待审清单；文本／JSON；历史月末口径 |
| 跨月区间 | period_report | 连续月份校验、名册／口径一致、流量可累计、库存／可花能力不相加；CSV |
| 发薪周期 | salary_cycle、salary_plan | 实际工资日、自然月账务核对、周期额度、周期前范围、计划不制造现金 |
| 历史与快照 | history、snapshot、account_queries | 任意日日末分布、用途来源与变动、截止日与根账本一致性 |
| 原始事实与来源 | financial、refs、facts、chronology | 官方解析／booking、原始价格、cost／lot、pad、重复交易occurrence、源顺序声明 |
| 严格账本门禁 | source、accounts | 禁止标签层级／注入、完整范围、H/K性质与支付范围、真实库存负数检查 |
| 用途与消费类别 | ownership、classification、inline_amounts | fund与budget双轴、精确多用途切片、默认／明确继承、冲突不能猜 |
| 用途库存与划拨 | holdings、purpose_flows、purpose_progress | 同账户虚拟调拨、跨账户用途守恒、主动划拨不能透支、被动负用途保留警告 |
| 消费目标与追踪 | spending、targets、tracking | 费用／退款分开、直接／子树目标、来源去重、专题不影响财务金额 |
| 负债责任 | liability_rules、liability_budgets、obligations | 逐笔budget责任、普通贷款／应收性质与到期、同账户多用途债务不净额掩盖 |
| 负债清算 | settlements、debt_coverage、debt_settlements | 本金／费用区别、共享现金去重、覆盖资源容量、锁定外币清偿、重复责任只扣一次 |
| 代垫与预付 | CreditOwners、default_outflows | 应收不算现金；预付退回恢复原用途；收入与本金合并到账分开证明 |
| H转账／理财 | h_transfers、h_gains、native_flows | 任意腿守恒、手续费、理财本金与损益、未支持组合明确阻断 |
| K申赎 | k_events、k_book_purposes | 累计申赎不冒充库存、同名用途差额、费用与损益有完整证据 |
| 外币与历史计价 | h_fx、valuation、settlement_budget | 原币／报告币分开、原始@／@@证据、历史计价不采用当前行情替代 |
| 证券成本池 | purpose_pools、benefits | 成本与市值区分、受保护用途／余量、重复受益和归属证据不重用 |
| 现金与首页 | payable_cash、actual_snapshot、core | 可支付范围、专用资金、未覆盖责任、可花金额、计划模拟逐项认证 |
| 待审与解释 | review_queue、metric_review、review_diagnostics | 错误家族、实现缺口与缺事实区分、准确来源行、同原因合并但不丢证据 |
| 人工审阅 | review、month_actions、obligation_review | 有理由的来源认领、完整性确认、明确批准范围、源变更后拒绝旧批准 |
| 状态生命周期 | state_store | reviews.json.gz、内容守卫、原子保存、导出导入、清理不删除已审决定、恢复历史 |
| TUI | tui、simple_tui | 切月／区间／周期、搜索、来源详情、审阅与月末确认、编辑失败恢复、watch、退出清理 |
| 导出与分发 | tui_export、packaging、CI | 文本／JSON／CSV、Unicode与Windows控制台、Mac/Windows独立可执行包 |

## 必须证明的安全性质

1. 根账本、include别名与glob集合按内容冻结，结束前再次验证；同大小同mtime修改仍失效。
2. 复用只对同一输入／截止日／配置合同有效；外部变更与未来事实不会污染当前月份。
3. 外部引用字典与稳定ID不变；迁移执行架构不能使用户已有审阅记录无效。
4. 金额保持精确数、责任与覆盖容量守恒，不能用部分成功冒充完整签发。
5. 报表读路径不写真实账本或状态，写入只发生在用户明确触发的审阅／配置／导出动作。

## 结构性重复与替换目标

- 来源捕获与loader分别解析同一批文本 → 一次官方解析同时生成manifest、raw位置和booking输入。
- 每个报表模块生成相同引用键、来源位置和posting映射 → 共享FactTable，输出边界才转换引用格式。
- 每个历史月建立相同原始索引 → 完整来源身份与有界可见性分离。
- 配置编译生成YAML再被投影反复读取 → 明确的运行输入和依赖访问，文件输出仅用于兼容审阅持久化边界。
- 审计调用关系和缓存分散 → 一个查询会话管理事实、配置守卫与投影依赖。

验收覆盖现有完整测试、公开连续案例、用户最新账本2023-07至2024-08逐月／区间输出、首次与后续刷新以及来源／配置／批准失效。若最新配置或审阅状态缺失，单独标记验证缺口，不能人工补批准来伪造认证结果。

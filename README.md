# beancount-budget-tool

Beancount 预算工具。独立的预算分析工具，输入账本与预算配置后自动生成预算统计与报告。

## 功能

- 月度预算 + 同月额外预算（如 `2026-06 绩效`）
- **YAML 嵌套层级预算**（父子桶自动聚合，支持 `--expand` 展开子桶）
- **模板机制**：`default_monthly` 定义默认结构，月份键仅写变动的数字（自动继承）
- **多模板切换**：label 含 `template` 的日期键定义新生效模板（如升职加薪后的预算调整）
- **任意时间范围查询**（`--month` / `--from` / `--to` / `--year`）
- **多桶归属**（`budget: "数码, 爱好"` 逗号分隔，各计全额；支持中文逗号 `，`）
- **桶名:金额 / 桶名900 分配**（一笔转账拆入多桶）
- **Income 归入预算**（显式写标签的收入计入入账，抵消支出）
- **简写桶名自动补全**（`额外储蓄` → `投资.额外储蓄`）
- **同比对比**（`--compare 2025-12` 并排展示两期数据）
- 四种统计范围：月度 / 累计 / 区间 / 年份
- 预算桶历史查询（汇总 / 分月 / 明细 + 关键词过滤 + 资产流隐藏）
- **资产位置追踪**（「持仓/已分配」/「超支/出金来源」分组，超支时隐藏前者）
- **Expense 桶资产退化**：纯资产转移自动记录位置（不虚增支出）
- **跟踪桶自动沉底**（planned=0 的桶排在底部，合计不纳入）
- 使用率百分比 + 按需排序（`--sort-by`）
- `bucket_types` 可选：`asset_bucket_accounts` 中的桶自动推断为 asset
- 未标注 `budget:` 的 `Expenses:*` 自动归入默认生活费桶
- 递归扫描目录（兼容 Tab 缩进、单空格分隔、YAML 空值、具体日期 key）
- 报告全中文化 + 导出 Markdown / CSV / TXT + 横向透视 CSV
- Windows 重定向 UTF-8 输出

## 构建

```bash
cargo build --release
# 二进制在 target/release/beancount-budget-tool
```

## 5 分钟上手

假设你的账本长这样（`my_ledger.bean`）：

```beancount
2026-06-16 * "工商银行" "地铁通勤"
  Expenses:Consume:交通  6 CNY
  Assets:Bank:工行  -6 CNY

2026-06-17 * "京东" "买耳机"
  budget: "数码"
  Expenses:Consume:电子  2000 CNY
  Assets:Bank:工行  -2000 CNY

2026-06-18 * "工商银行" "储蓄转入"
  budget: "储蓄"
  Assets:Bank:建设银行  5000 CNY
  Assets:Bank:工行  -5000 CNY
```

**Step 1** — 写预算（`my_budgets.yaml`）：

```yaml
"2026-06":
  生活费:
    交通: 1500
    饮食: 2500
  数码: 3000
  储蓄: 10000
```

**Step 2** — 写映射（`my_mappings.yaml`）：

```yaml
default_expense_bucket: 生活费

defaults:
  "Expenses:Consume:交通": 生活费.交通
  "Expenses:Consume:电子": 数码

asset_bucket_accounts:
  储蓄:
    - "Assets:Bank:建设银行"
```

> `bucket_types` 可以省略——`asset_bucket_accounts` 中出现的桶自动识别为 asset，其余默认 expense。

**Step 3** — 运行：

```bash
beancount-budget-tool -l my_ledger.bean -m 2026-06 \
  --budgets my_budgets.yaml --mappings my_mappings.yaml --scope cumulative
```

输出：

```
预算报告 (2026-06 (cumulative)) [CNY]
预算桶                月预算     已支出   使用率    结余   状态
--------------------------------------------------------------
储蓄                10000.00  5000.00   50.0% 5000.00   正常
数码                 3000.00  2000.00   66.7% 1000.00   正常
生活费                4000.00     6.00    0.2% 3994.00   正常
--------------------------------------------------------------
合计                17000.00  7006.00   41.2% 9994.00   正常
```

## 配置说明

### budgets.yaml

四种 key 格式：

```yaml
# 1. 月度（YYYY-MM）
"2026-06":
  数码: 3000
  旅行: 2000

# 2. 具体日期（YYYY-MM-DD）
"2000-08-20 对方还款":
  生活费: 500

# 3. 月度 + 标签（绩效、年终奖等，与基础月预算叠加）
"2026-06 绩效":
  旅行: 2000

# 4. 嵌套层级 → 自动展平为 生活费.餐饮、生活费.交通
"2026-06":
  生活费:
    餐饮: 1200
    交通: 200
```

- `生活费` 是父桶，`生活费.交通` 是子桶。父桶统计自动由子桶之和得出。
- 查询 `--bucket 生活费` 汇总全子桶，`--bucket 生活费.交通` 只看交通。
- 日期 key（`YYYY-MM-DD`）的月份部分用于范围过滤，日期信息保留在标签中展示。

### 模板 (default_monthly + template switch)

如果每月预算结构相同只是金额微调，用 `default_monthly` 定义基准，月份键只写变化的数字：

```yaml
default_monthly:
  储蓄: 700
  生活费:
    餐饮: 1200
    交通: 200
  数码: 5000

2026-06-04:
  储蓄: 589.03      # 只写储蓄金额，生活费.餐饮、交通、数码自动从模板继承

2026-07-05:
  储蓄: 1200        # 升职了，储蓄翻倍，其他不变
```

**多套模板**：工作中途薪资变动时，用 label 含 `template` 的日期键切换：

```yaml
default_monthly:
  储蓄: 700
  生活费: 2000
  数码: 5000

# 七月升职加薪，切新模板
"2026-07-01 template":
  储蓄: 1200
  生活费: 3000
  # 数码: 5000 ← 不变，从 default_monthly 继承

# 六月的预算（七月前的模板不生效）
2026-06-04:
  储蓄: 589.03      # 用 default_monthly 的 700 → 覆盖为 589.03

# 七月的预算（新模板生效）
2026-07-05:
  储蓄: 0           # 用新模板的 1200 → 覆盖为 0
```

**合并规则**：`default_monthly` → 按日期叠加 `template` 键 → 月份键覆写。同名桶后者覆盖前者。月份键只写与模板不同的桶：没写的自动继承，写了的（包括 0）以月份键为准。

**注意事项**：
- 月份键写 `储蓄: 0` 会覆盖模板的 `储蓄: 700`——不是相加，是覆写
- template 生效日以月份为准（`YYYY-MM`），日期部分不参与比较
- 与模板无关的键（跟踪桶、特殊标签）照常工作，不受模板影响

### mappings.yaml

```yaml
default_expense_bucket: 生活费           # 无匹配时的兜底桶

# bucket_types 是可选的：asset_bucket_accounts 中出现的桶自动推断为 asset
# 只有需要显式声明时才写
bucket_types:
  储蓄: asset

defaults:                                # 账户前缀 → 桶名（最长前缀优先）
  "Expenses:Consume:交通": 生活费.交通
  "Expenses:Consume:电子": 数码

asset_bucket_accounts:                   # 可选：资产桶的账户前缀
  储蓄:
    - "Assets:Bank:建设银行"
    - "Assets:Invest:货币基金"
```

- **不想自动划到某桶**：删掉对应 `defaults` 行，只有显式写 `budget:` 才入桶。
- **不想手写 `bucket_types`**：`asset_bucket_accounts` 中的桶自动识别为 asset。

## Beancount 交易标注

### 基本用法

```beancount
2026-06-17 * "京东" "买耳机"
  budget: "数码"
  Expenses:Consume:电子  2000 CNY
  Assets:Bank:工行  -2000 CNY
```

> 程序兼容 Tab 缩进和单空格分隔，但建议用 Beancount 标准格式（至少两个空格分隔账户与金额）。

### 多桶归属

```beancount
2026-06-20 * "京东" "买耳机和游戏"
  budget: "数码, 爱好"
  Expenses:Consume:电子  2000 CNY
  Assets:Bank:工行  -2000 CNY
```
数码和爱好各计支出 2000。

### 桶名:金额 / 桶名900（基金拆分）

三种写法等价，支持中文逗号 `，` 混用：

```beancount
; 半角冒号 + 英文逗号
budget: "电子产品:900, 旅游:900, 保险:200"

; 全角冒号（不用切输入法）+ 中文逗号
budget: "电子产品：900，旅游900，保险200"

; 无分隔符（尾部数字自动识别）
budget: "电子产品900, 旅游900, 保险200"
```

配合 `asset_bucket_accounts` 可独立追踪各桶的资金位置及后续消费扣减。

### 简写桶名自动补全

写 `额外储蓄` 会自动查找 `*.额外储蓄` 的完整桶名，所以：

```beancount
budget: "额外储蓄13847"    # → 自动匹配到 投资.额外储蓄
budget: "待投资金2000"     # → 自动匹配到 投资.待投资金
```

`--bucket 额外储蓄` 同样自动补全。

### Income 标签通道

工资分桶在 `budgets.yml` 里做，不写标签。零星收入写 `budget: "生活费"` 即可入账抵消支出：

```beancount
2026-04-03 * "广告" "广告费"
  budget: "生活费"
  Income:Misc:广告  -50 CNY
  Assets:Bank:工行  50 CNY
```

Income 只有**显式写 `budget:` 标签**的才入桶，不写标签的收入（工资、退款等）不影响预算。

### 跟踪桶（纯统计不预算）

`budgets.yml` 里写 `planned: 0`，只看不设预算：

```yaml
"2026-06":
  大额支出: 0        # 仅跟踪，不计入预算总额
```

```beancount
budget: "数码, 大额支出"   # 花费同时计入数码预算和大额支出跟踪
```

汇总表中跟踪桶排在底部，合计不纳入其 actual。

### Expense 桶自动退化

纯资产转移（只有 Assets↔Assets）不会凭空生成支出，而是自动记录位置变动。超支时「持仓/已分配」段隐藏，仅显示「超支/出金来源」。

### 支付链路标注

链路中每段都可写 `budget:`，不重复算支出：

```beancount
# 只有最后一段（有 Expenses: 行）才算支出
2026-04-02 * "京东" "买MacBook"
  budget: "数码"
  Expenses:Consume:电子  12000 CNY
  Assets:Invest:货币基金  -5000 CNY
  Assets:Bank:工行  -7000 CNY
  → 支出 12000，位置扣减 Fund+Bank
```

### #tag / ^link（Beancount 原生标签）

```beancount
2026-05-16 * "工行" "机票" #东京 #家族旅行 ^trip-2026
  budget: "旅行"
  Expenses:Travel  5000 CNY
```

标签被提取为 metadata，配合 `--filter` 搜索。

### 不写 budget（自动归类）

```beancount
2026-06-16 * "工行" "地铁"       ; 没写 budget
  Expenses:Consume:交通  6 CNY    ; → 前缀匹配到 生活费.交通

2026-06-19 * "淘宝" "杂货"       ; 没写 budget
  Expenses:Consume:杂货  50 CNY   ; → 无匹配，回退到 生活费
```

## CLI 参数

| 参数 | 说明 |
|------|------|
| `-l, --ledger <FILE>` | 账本文件（可重复传多个） |
| `--ledger-dir <DIR>` | 递归扫描目录中的 `.bean` / `.beancount` |
| `-m, --month <YYYY-MM>` | 目标月份。与 `--from/--to/--year` 互斥 |
| `--from <YYYY-MM>` | 统计起始月份（需配合 `--to`） |
| `--to <YYYY-MM>` | 统计结束月份（需配合 `--from`） |
| `--year <YYYY>` | 快捷全年（等价 `--from YYYY-01 --to YYYY-12`） |
| `--budgets <FILE>` | 预算配置文件（必需） |
| `--mappings <FILE>` | 映射配置文件（必需） |
| `--scope <month\|cumulative>` | 统计范围。默认 `month` |
| `--bucket <NAME>` | 指定桶名称，输出该桶单独报告（支持简写自动补全） |
| `--bucket-view <summary\|monthly\|detail>` | 桶视图粒度。默认 `summary` |
| `--filter <KEYWORD>` | 过滤交易（匹配 payee / narration / metadata / #tag） |
| `--sort-by <name\|planned\|actual\|remain>` | 汇总表排序。默认 `name` |
| `--expand` | 展开所有子桶到汇总表 |
| `--compare <YYYY-MM>` | 同比对比：并排展示两期数据 |
| `--show-locations` | Summary 视图下显示资产桶资金位置 |
| `--hide-asset-flows` | 明细视图中隐藏资产间转移，仅显示预算收入和实际支出 |
| `--out-dir <DIR>` | 导出报告到目录 |
| `--csv-pivot` | 额外生成横向月表 CSV（月 × 桶） |
| `--out-json` | 额外生成 JSON 格式报告 |
| `--currency <CODE>` | 统计币种，默认 `CNY` |
| `--strict` | 严格模式：存在未知预算桶则非零退出 |

### 时间范围示例

```bash
--month 2026-06 --scope month       # 仅 6 月
--month 2026-06 --scope cumulative  # 最早 ~ 6 月
--from 2026-03 --to 2026-06         # 3 ~ 6 月
--year 2026                         # 2026 全年
```

### 常用组合

```bash
# 今年的整体预算执行情况
--year 2026

# 展开子桶看明细
--year 2026 --expand

# 今年 vs 去年对比
--year 2026 --compare 2025-12

# 按超支程度排
--year 2026 --sort-by remain

# 搜旅行桶里"东京"相关的消费
--bucket 旅行 --bucket-view detail --filter "东京"

# 导出全年报告 + 横向月度 CSV
--year 2026 --out-dir ./reports --csv-pivot
```

## 输出文件

| 文件 | 内容 |
|------|------|
| `summary-{range}.md` / `.txt` | 全量汇总报告 |
| `buckets-{range}.csv` | 每桶 planned/actual/remain |
| `bucket-{桶名}-{range}.md` | 某桶完整报告（分月 + 明细 + 资产位置） |
| `asset-locations-{桶名}-{range}.md` | 资产桶资金位置 |
| `pivot-{range}.csv`（需 `--csv-pivot`） | 横向月表（行=月，列=桶） |
| `summary-{range}.json`（需 `--out-json`） | 每桶 planned/actual/remain JSON |

## 注意事项

- **位置跟着 scope 走**：`--scope month` 只看当月位置，`--scope cumulative` 看全部历史。
- **格式兼容**：支持 Tab 缩进、单空格分隔、YAML 空值（视为 0）。
- **跟踪桶**：planned=0 的桶排在底部，合计不纳入其实际数据。
- **Income**：仅显式写 `budget:` 的 Income 入桶，不写标签的（工资、退款等）完全忽略。
- **Windows**：输出 UTF-8，重定向 `> tmp.txt` 正常显示。

## 完整示例

`examples/` 目录下：

| 文件 | 说明 |
|------|------|
| `budgets.yaml` | 嵌套层级预算配置 |
| `mappings.yaml` | 账户映射 + 储蓄资产桶 |
| `demo.bean` | 示范账本（含各种标注场景） |

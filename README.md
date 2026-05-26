# beancount-budget-tool

Beancount 预算工具。独立的预算分析工具，输入账本与预算配置后自动生成预算统计与报告。

## 功能

- 月度预算 + 同月额外预算（如 `2026-06 绩效`）
- **YAML 嵌套层级预算**（`生活费: {交通: 1500, 饮食: 2500}` 父子桶自动聚合）
- **任意时间范围查询**（`--from` / `--to` / `--year`）
- **多桶归属**（`budget: "数码, 爱好"` 逗号分隔，各计全额）
- **桶名:金额分配**（`budget: "电子产品:900, 旅游:900"` 一笔转账拆入多桶）
- **同比对比**（`--compare 2025-12` 并排展示两期数据）
- 月度 / 累计 / 区间 / 年份四种统计范围
- 预算桶历史查询（汇总 / 分月 / 明细 + 关键词过滤）
- 资产桶资金位置追踪（含多账户仓位、消费扣减后剩余）
- 使用率百分比列 + 按需排序（`--sort-by remain|actual|planned`）
- 未标注预算的 `Expenses:*` 自动归入默认生活费桶
- 递归扫描账本目录（兼容 Tab 缩进、单空格分隔等非标准格式）
- 报告全中文化输出 + 自动导出 Markdown / CSV / TXT + 横向透视 CSV

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

bucket_types:
  生活费: expense
  数码: expense
  储蓄: asset

defaults:
  "Expenses:Consume:交通": 生活费.交通
  "Expenses:Consume:饮食": 生活费.饮食
  "Expenses:Consume:电子": 数码

asset_bucket_accounts:
  储蓄:
    - "Assets:Bank:建设银行"
```

**Step 3** — 运行：

```bash
beancount-budget-tool \
  -l my_ledger.bean \
  -m 2026-06 \
  --budgets my_budgets.yaml \
  --mappings my_mappings.yaml \
  --scope cumulative
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

三种写法：

```yaml
# 1. 扁平桶
"2026-06":
  数码: 3000
  旅行: 2000

# 2. 嵌套层级 → 自动展平为 生活费.交通、生活费.饮食
"2026-06":
  生活费:
    交通: 1500
    饮食: 2500

# 3. 额外标签预算（与基础月预算叠加）
"2026-06 绩效":
  旅行: 2000
  爱好: 1000
```

`生活费` 是父桶，`生活费.交通` 是子桶。父桶统计自动由子桶之和得出。查询 `--bucket 生活费` 汇总全子桶，`--bucket 生活费.交通` 只看交通。

### mappings.yaml

```yaml
default_expense_bucket: 生活费           # 无匹配时的兜底桶

bucket_types:                            # 声明桶类型（expense / asset）
  生活费: expense
  储蓄: asset

defaults:                                # 账户前缀 → 桶名（最长前缀优先）
  "Expenses:Consume:交通": 生活费.交通
  "Expenses:Consume:电子": 数码

asset_bucket_accounts:                   # 可选：资产桶的账户前缀
  储蓄:
    - "Assets:Bank:建设银行"
    - "Assets:Invest:货币基金"
```

> 如果不想让某类账户自动划到特定桶，删掉对应 `defaults` 行即可。删掉后只有显式写 `budget:` 才会进入该桶。

## Beancount 交易标注

### 基本

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

一笔转账同时属于多个桶，按指定金额分配（两种写法等价）：

```beancount
; 冒号写法
2026-04-01 * "工行" "月定投"
  budget: "电子产品:900, 旅游:900, 保险:200"

; 无冒号写法（尾部数字自动识别）
2026-04-01 * "工行" "月定投"
  budget: "电子产品900, 旅游900, 保险200"
  Assets:Invest:货币基金A  2000 CNY
  Assets:Bank:工行  -2000 CNY
```

电子产品桶入 +900，旅游 +900，保险 +200。配合 `asset_bucket_accounts` 配置前缀后，各桶可独立追踪资金位置和消费扣减：

```beancount
; 消费扣减
2026-05-01 * "京东" "买MacBook"
  budget: "电子产品"
  Expenses:Consume:电子  1200 CNY
  Assets:Invest:货币基金A  -1200 CNY
```

`--bucket 电子产品 --bucket-view detail` 就能看到：存入 +900 → 消费 -1200 → 位置 基金A:-300。

### 不写 budget（自动归类）

```beancount
2026-06-16 * "工行" "地铁"       ; 没写 budget
  Expenses:Consume:交通  6 CNY    ; → 前缀匹配到 生活费.交通
  Assets:Bank:工行  -6 CNY

2026-06-19 * "淘宝" "杂货"       ; 没写 budget
  Expenses:Consume:杂货  50 CNY   ; → 无匹配，回退到 生活费
  Assets:Bank:工行  -50 CNY
```

### #tag / ^link（Beancount 原生标签）

```beancount
2026-05-16 * "工行" "机票" #东京 #家族旅行 ^trip-2026
  budget: "旅行"
  Expenses:Travel  5000 CNY
  Assets:Bank:工行  -5000 CNY
```

标签会被提取为 metadata，可配合 `--filter` 搜索。

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
| `--bucket <NAME>` | 指定桶名称，输出该桶单独报告 |
| `--bucket-view <summary\|monthly\|detail>` | 桶视图粒度。默认 `summary` |
| `--filter <KEYWORD>` | 过滤交易（匹配 payee / narration / metadata） |
| `--sort-by <name\|planned\|actual\|remain>` | 汇总表排序。默认 `name` |
| `--compare <YYYY-MM>` | 同比对比：并排展示两期数据 |
| `--show-locations` | Summary 视图下显示资产桶资金位置 |
| `--out-dir <DIR>` | 导出报告到目录 |
| `--csv-pivot` | 额外生成横向月表 CSV（月 × 桶） |
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

# 今年 vs 去年对比
--year 2026 --compare 2025-12

# 按超支程度排，最严重的在顶部
--year 2026 --sort-by remain

# 搜旅行桶里"东京"相关的消费
--bucket 旅行 --bucket-view detail --filter "东京"

# 导出全年报告 + 横向月度 CSV
--year 2026 --out-dir ./reports --csv-pivot
```

### `--bucket-view` 选项

```bash
--bucket 旅行 --bucket-view summary   # 仅汇总统计
--bucket 旅行 --bucket-view monthly   # 按月拆分（预算收入/支出/结余）
--bucket 旅行 --bucket-view detail    # 每笔交易明细 + 资产位置
```

### `--sort-by` 排序

```bash
--sort-by name      # 按桶名字典序（默认）
--sort-by planned   # 按预算从大到小
--sort-by actual    # 按实际支出从大到小
--sort-by remain    # 按结余从小到大（超支的排最前）
```

## 输出文件

`--out-dir ./reports` 后生成：

| 文件 | 内容 |
|------|------|
| `summary-{range}.md` / `.txt` | 全量汇总报告 |
| `buckets-{range}.csv` | 每桶 planned/actual/remain |
| `bucket-{桶名}-{range}.md` | 某桶完整报告（分月 + 明细 + 资产位置） |
| `asset-locations-{桶名}-{range}.md` | 资产桶资金位置 |
| `pivot-{range}.csv`（需 `--csv-pivot`） | 横向月表（行=月，列=桶，拖 Excel 画图） |

`{range}` 示例：`2026-06-month`、`2026-06-cumulative`、`2026-01_2026-06`。

## 完整示例

`examples/` 目录下：

| 文件 | 说明 |
|------|------|
| `budgets.yaml` | 嵌套层级预算配置 |
| `mappings.yaml` | 账户映射 + 储蓄资产桶 |
| `demo.bean` | 示范账本（含各种标注场景） |

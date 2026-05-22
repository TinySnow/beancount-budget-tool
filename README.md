# beancount-budget-tool

Beancount 预算工具。独立的预算分析工具，输入账本与预算配置后自动生成预算统计与报告。

## 功能

- 月度预算 + 同月额外预算（如 `2026-06 绩效`）
- **YAML 嵌套层级预算**（`生活费: {交通: 1500, 饮食: 2500}` 自动聚合父桶）
- **任意时间范围查询**（`--from 2026-01 --to 2026-06`）
- **多桶归属**（`budget: "数码, 爱好"` 一笔交易同时扣减多桶）
- 月度 / 累计 / 区间三种统计范围
- 预算桶历史查询（汇总 / 分月 / 明细）
- 资产类预算桶（储蓄等）统计与资金位置追踪
- 未标注预算的 `Expenses:*` 自动归入默认生活费桶
- 递归扫描账本目录（`.bean` / `.beancount`）
- 报告全中文化输出 + 自动导出 Markdown / CSV / TXT

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
  Assets:Bank:ICBC  -6 CNY

2026-06-17 * "工商银行" "买耳机"
  budget: "数码"
  Expenses:Consume:电子  2000 CNY
  Assets:Bank:ICBC  -2000 CNY

2026-06-18 * "工商银行" "储蓄转入"
  budget: "储蓄"
  Assets:Bank:建设银行  5000 CNY
  Assets:Bank:ICBC  -5000 CNY
```

**Step 1** — 写好预算（`my_budgets.yaml`）：

```yaml
"2026-06":
  生活费:
    交通: 1500
    饮食: 2500
  数码: 3000
  储蓄: 10000
```

**Step 2** — 写好映射（`my_mappings.yaml`）：

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
预算桶                                 月预算            已支出             结余         状态
------------------------------------------------------------------------------------------
储蓄                               10000.00        5000.00        5000.00         正常
数码                                3000.00        2000.00        1000.00         正常
生活费                               4000.00           6.00        3994.00         正常
------------------------------------------------------------------------------------------
合计                               17000.00        7006.00        9994.00         正常
```

## 配置说明

### budgets.yaml

支持三种写法：

```yaml
# 1. 扁平桶（无子级）
"2026-06":
  数码: 3000
  旅行: 2000

# 2. 嵌套层级（YAML map 自动展平为父子桶）
"2026-06":
  生活费:
    交通: 1500    # 桶名：生活费.交通
    饮食: 2500    # 桶名：生活费.饮食

# 3. 额外标签预算（绩效、年终奖等，与基础月预算叠加）
"2026-06 绩效":
  旅行: 2000
  爱好: 1000
```

层级规则：`生活费` 是父桶，`生活费.交通` 是子桶。父桶不单独写金额，统计时自动由于子桶之和得出。查询 `--bucket 生活费` 汇总所有子桶，`--bucket 生活费.交通` 只看交通。

### mappings.yaml

```yaml
# 默认生活费桶：未显式标注 budget 标签且无前缀匹配的消费归入此桶
default_expense_bucket: 生活费

# 桶类型声明：expense（支出桶）或 asset（资产桶）
bucket_types:
  生活费: expense
  储蓄: asset

# 账户前缀 → 桶名映射（最长前缀优先匹配）
defaults:
  "Expenses:Consume:交通": 生活费.交通
  "Expenses:Consume:饮食": 生活费.饮食
  "Expenses:Consume:电子": 数码

# 可选：资产桶的账户前缀，用于精确定位资金在哪个账户
asset_bucket_accounts:
  储蓄:
    - "Assets:Bank:建设银行"
    - "Assets:Invest:货币基金"
```

## Beancount 交易标注

### 基本用法

在交易的 metadata 行标注 `budget:` 即可指定所属桶：

```beancount
2026-06-17 * "京东" "买耳机"
  budget: "数码"
  Expenses:Consume:电子  2000 CNY
  Assets:Bank:ICBC  -2000 CNY
```

> 注意：账户名和金额之间必须至少两个空格（`Expenses:xxx  金额`），这是 Beancount 标准格式。

### 多桶归属（逗号分隔）

一笔交易同时扣减多个桶，各计全额：

```beancount
2026-06-20 * "京东" "买耳机和游戏"
  budget: "数码, 爱好"
  Expenses:Consume:电子  2000 CNY
  Assets:Bank:ICBC  -2000 CNY
```

结果：数码桶记支出 2000，爱好桶也记支出 2000。

### 资产桶（储蓄/投资）

```beancount
; 存入建设银行（配置了 asset_bucket_accounts 时精确匹配）
2026-06-18 * "工商银行" "储蓄转入"
  budget: "储蓄"
  Assets:Bank:建设银行  5000 CNY
  Assets:Bank:ICBC  -5000 CNY

; 赎回货币基金（多桶归属，两桶各计全额扣减）
2026-07-01 * "应急" "赎回"
  budget: "储蓄, 投资"
  Assets:Bank:ICBC  10000 CNY
  Assets:Invest:货币基金  -10000 CNY
```

### 不写 budget（自动归类）

没写 `budget:` 的消费交易，系统按 `defaults` 前缀匹配 → 回退到 `default_expense_bucket`：

```beancount
2026-06-16 * "工商银行" "地铁通勤"
  ; 没写 budget，Expenses:Consume:交通 → 匹配到 生活费.交通
  Expenses:Consume:交通  6 CNY
  Assets:Bank:ICBC  -6 CNY

2026-06-19 * "淘宝" "杂货"
  ; 没写 budget，无前缀匹配 → 归入 生活费
  Expenses:Consume:杂货  50 CNY
  Assets:Bank:ICBC  -50 CNY
```

## CLI 参数

| 参数 | 说明 |
|------|------|
| `-l, --ledger <FILE>` | 指定账本文件（可重复传多个） |
| `--ledger-dir <DIR>` | 递归扫描目录中的 `.bean` / `.beancount` 文件 |
| `-m, --month <YYYY-MM>` | 目标月份。与 `--from/--to` 互斥 |
| `--from <YYYY-MM>` | 统计起始月份（需配合 `--to`）|
| `--to <YYYY-MM>` | 统计结束月份（需配合 `--from`）|
| `--budgets <FILE>` | 预算配置文件（必需）|
| `--mappings <FILE>` | 映射配置文件（必需）|
| `--scope <month\|cumulative>` | 统计范围。默认 `month`（仅目标月），`cumulative`（截至目标月累计）|
| `--bucket <NAME>` | 指定预算桶名称，输出该桶的单独报告 |
| `--bucket-view <summary\|monthly\|detail>` | 桶视图粒度。默认 `summary` |
| `--show-locations` | 在 Summary 视图下显示资产桶资金位置 |
| `--out-dir <DIR>` | 导出 Markdown / CSV / TXT 报告到指定目录 |
| `--currency <CODE>` | 统计币种，默认 `CNY` |
| `--strict` | 严格模式：存在未知预算桶时返回非零退出码 |

### `--scope` 与 `--from/--to` 的区别

```bash
# 仅看 2026-06 一个月
-m 2026-06 --scope month

# 从最早到 2026-06 累计
-m 2026-06 --scope cumulative

# 只看 2026-03 到 2026-06（互斥于 -m）
--from 2026-03 --to 2026-06

# 整个 2026 年
--from 2026-01 --to 2026-12
```

### `--bucket-view` 选项

```bash
# 仅看汇总统计
--bucket 旅行 --bucket-view summary

# 按月拆分（每月一行：预算收入 / 支出 / 结余）
--bucket 旅行 --bucket-view monthly

# 每笔交易明细（预算收入行 + 交易流水分组）
--bucket 旅行 --bucket-view detail
```

## 输出文件

设置 `--out-dir ./reports` 后生成：

| 文件 | 内容 |
|------|------|
| `summary-{range}.md` | 全量预算汇总（Markdown 表格）|
| `summary-{range}.txt` | 终端同款文本报告 |
| `buckets-{range}.csv` | 每个桶的 planned/actual/remain CSV |
| `bucket-{桶名}-{range}.md` | 某桶的完整报告（分月 + 明细）|
| `asset-locations-{桶名}-{range}.md` | 资产桶的资金位置报告 |

`{range}` 取决于查询方式：
- `-m 2026-06 --scope month` → `2026-06-month`
- `-m 2026-06 --scope cumulative` → `2026-06-cumulative`
- `--from 2026-01 --to 2026-06` → `2026-01_2026-06`

## 完整示例文件

`examples/` 目录下有完整可运行的示例：

- `budgets.yaml` — 嵌套层级预算配置
- `mappings.yaml` — 账户映射 + 储蓄资产桶配置
- `demo.bean` — 示范账本（含各种预算标注场景）

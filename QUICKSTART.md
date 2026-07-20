# QUICKSTART — 功能清单与用法速查

> 本文档是完整的功能速查表。初次使用看前半部分（配置怎么写），忘了怎么用看后半部分（场景速查）。

---

## 一、你需要准备三样东西

```
beancount-budget-tool \
  --budgets budgets.yml \     # ← ① 预算计划
  --config config.yml \       # ← ② 全局配置
  --ledger-dir ./tx/          # ← ③ Beancount 账本
```

### ① budgets.yml — 纯预算计划

只管预算。**跟踪桶不写在这里。**

```yaml
# 模板（可选）：定义每月默认值
default_monthly:
  储蓄: 700
  生活费:
    饮食: 2000
    交通: 300
  数码: 3000

# 按月覆盖：只写与模板不同的桶
"2026-06":
  数码: 5000         # 覆盖模板 → 5000
  # 生活费.饮食 没写 → 从模板继承 2000

"2026-06 绩效":        # 标签键：与基础月叠加
  数码: 2000          # 6月总预算 = 5000 + 2000 = 7000

# 模板切换（加薪/降薪场景）
"2026-07-01 template":  # label 含 "template" 即模板键
  储蓄: 1200
  生活费: 3000
  # 数码没写 → 从 default_monthly 继承 3000
```

**规则：**
- 嵌套 YAML 自动展平为点号桶名：`生活费.饮食`
- 父桶不写金额，统计 = 子桶之和
- 模板合并：`default_monthly` → 按日期叠 `template` → 月键覆写
- 月键写了但模板没有的桶 → 保留
- 月键写 0 → 覆盖模板值（不是相加）

### ② config.yml — 全局配置

管理账户映射、桶类型、跟踪桶、资产位置。

```yaml
# 兜底桶：无匹配时默认归入
default_expense_bucket: 生活费

# 账户前缀 → 桶名（最长前缀优先匹配）
defaults:
  "Expenses:Consume:饮食": 生活费.饮食
  "Expenses:Consume:交通": 生活费.交通
  "Expenses:Consume:电子": 数码

# 跟踪桶：只追踪实际支出，不参与预算计算
# 不显示在汇总表、不计入合计、不聚合到父桶
tracking_buckets:
  - 基金.旅游
  - 基金.电子产品
  - 大额支出

# 桶类型声明（可省略，asset_bucket_accounts 中的桶自动识别为 asset）
bucket_types:
  储蓄: asset

# 资产桶的关联账户（用于追踪资金位置）
asset_bucket_accounts:
  储蓄:
    - "Assets:Bank:建设银行"
    - "Assets:Invest:货币基金"
```

### ③ Beancount 账本 — 交易标注

```beancount
; ── 基本标注 ──
2026-06-17 * "京东" "买耳机"
  budget: "数码"
  Expenses:Consume:电子  2000 CNY
  Assets:Bank:工行       -2000 CNY

; ── 多桶归属（半角逗号分隔，中文逗号也支持）──
2026-06-20 * "京东" "耳机+游戏"
  budget: "数码, 爱好"
  Expenses:Consume:电子  2000 CNY
  Assets:Bank:工行       -2000 CNY
  → 数码和爱好各计 2000

; ── 金额拆分（尾部数字自动识别）──
budget: "电子产品900, 旅游900, 保险200"
budget: "电子产品：900，旅游：900"     ; 全角冒号+中文逗号也行
budget: "电子产品900，旅游900"         ; 无分隔符也行
  → 三桶分别计 900、900、200

; ── 简写桶名自动补全 ──
budget: "额外储蓄13847"   → 自动匹配到 投资.额外储蓄
budget: "待投资金2000"    → 自动匹配到 投资.待投资金

; ── 不写 budget（自动归类）──
2026-06-16 * "工行" "地铁"
  Expenses:Consume:交通  6 CNY       → 匹配到 生活费.交通
  Assets:Bank:工行      -6 CNY

; 无匹配 → 回退到 default_expense_bucket（默认"生活费"）
2026-06-19 * "淘宝" "杂货"
  Expenses:Consume:杂货  50 CNY      → 回退到 生活费

; ── Income 入账（只有写 budget 的才入桶）──
2026-04-03 * "广告" "广告费"
  budget: "生活费"
  Income:Misc:广告  -50 CNY
  Assets:Bank:工行   50 CNY

; ── Beancount 原生标签（配合 --filter）──
2026-05-16 * "工行" "机票" #东京 ^trip-2026
  budget: "旅行"
  Expenses:Travel  5000 CNY
```

---

## 二、功能速查

### 汇总报告

```bash
# 单月
beancount-budget-tool -m 2026-06 --budgets budgets.yml --config config.yml -l ledger.bean

# 累计（最早～当月）
beancount-budget-tool -m 2026-06 --scope cumulative --budgets budgets.yml --config config.yml --ledger-dir ./tx/

# 时间范围
beancount-budget-tool --from 2026-03 --to 2026-06 --budgets budgets.yml --config config.yml --ledger-dir ./tx/

# 全年
beancount-budget-tool --year 2026 --budgets budgets.yml --config config.yml --ledger-dir ./tx/
```

### 排序与展开

```bash
# 按超支程度排（结余少的在前）
--year 2026 --sort-by remain

# 按实际支出排
--year 2026 --sort-by actual

# 展开所有子桶（不折叠到父桶）
--year 2026 --expand
```

### 单桶明细

```bash
# 汇总
--bucket 基金.旅游 --bucket-view summary

# 按月拆分
--bucket 基金.旅游 --bucket-view monthly

# 逐笔明细（含年度小结）
--bucket 基金.旅游 --bucket-view detail
```

明细视图中每年末尾自动打印：
```
========== 2025 年小结 ==========
预算收入 本年合计: xxx
预算收入 累计合计: xxx
支出 本年合计: xxx
支出 累计合计: xxx
==============================
```

### 同比对比

```bash
--year 2026 --compare 2025-12
```

并排展示两期数据的预算桶、月预算、已支出、结余及差异。

### 关键词搜索

```bash
--bucket 旅行 --bucket-view detail --filter "东京"
```

匹配 payee / narration / metadata / #tag。

### 文件导出

```bash
--year 2026 --out-dir ./reports --csv-pivot --out-json
```

输出：
| 文件 | 内容 |
|---|---|
| `summary-*.md` / `.txt` | 汇总 |
| `buckets-*.csv` | 桶级 planned/actual |
| `bucket-*.md` | 单桶完整报告 |
| `pivot-*.csv` | 月×桶 横向透视 |
| `summary-*.json` | JSON |

---

## 三、跟踪桶专篇

**场景：** 基金.旅游每月有固定存入，也有实际旅行支出，但我不设预算——只想知道花多少、剩多少。

### 配置

`config.yml`：
```yaml
tracking_buckets:
  - 基金.旅游
```

`budgets.yml`：
```yaml
# default_monthly 里不要写 基金.旅游
default_monthly:
  储蓄: 700
  生活费: 2000
```

### 效果

- 汇总表里不显示 `基金.旅游`
- 不参与合计，不聚合到父桶
- `--bucket 基金.旅游 --bucket-view detail` 仍然可以看完整历史追踪
- 资产位置（资金存放/支出来源/已支出）正常显示

---

## 四、资产桶专篇

用于追踪储蓄/投资等资产转移，不标记消费支出。

```yaml
# config.yml
asset_bucket_accounts:
  储蓄:
    - "Assets:Bank:建设银行"
    - "Assets:Invest:货币基金"
```

```beancount
2026-06-18 * "工行" "储蓄转入"
  budget: "储蓄"
  Assets:Bank:建设银行  5000 CNY
  Assets:Bank:工行      -5000 CNY
```

查询资产桶时自动显示：
```
资金存放（截至 2026-06）:       ← 钱存在哪
建设银行: 28100.00 CNY

支出来源（截至 2026-06）:       ← 钱从哪来
工商银行: -29000.00 CNY

已支出（截至 2026-06）: 9649.90 CNY  ← 差额 = 实际花掉的钱
```

---

## 五、CLI 参数速查

```
-m, --month <YYYY-MM>      目标月份
--from/--to <YYYY-MM>      时间范围
--year <YYYY>              全年（= --from YYYY-01 --to YYYY-12）

--budgets <FILE>           预算文件（必需）
--config, -c <FILE>        配置文件（必需，旧名 --mappings 仍可用）
--ledger, -l <FILE>        账本文件（可重复多次）
--ledger-dir <DIR>         账本目录（递归扫描 .bean/.beancount）

--scope <month|cumulative> 统计范围（默认 month）
--bucket <NAME>            指定桶（支持简写自动补全）
--bucket-view <s|m|d>      桶视图粒度（summary|monthly|detail，默认 summary）
--sort-by <key>            排序（name|planned|actual|remain）
--expand                   展开子桶
--compare <YYYY-MM>        同比对比
--filter <KEYWORD>         过滤交易

--show-locations           汇总视图下显示资产桶位置
--hide-asset-flows          明细视图下隐藏资产转移记录

--out-dir <DIR>            导出报告
--csv-pivot                横向月表 CSV
--out-json                 JSON 报告
--strict                   未知桶报错退出

--currency <CODE>          币种（默认 CNY）
```

---

## 六、常见场景

```bash
# 本月花了多少 -- 只看当月预算执行
beancount-budget-tool -m 2026-06 \
  --budgets budgets.yml --config config.yml \
  --ledger-dir ./tx/ --scope month

# 年初至今累计 -- 看全年预算还剩下多少
beancount-budget-tool -m 2026-06 --scope cumulative \
  --budgets budgets.yml --config config.yml --ledger-dir ./tx/

# 旅行花了多少 -- 跟踪桶明细
beancount-budget-tool -m 2026-06 --scope cumulative \
  --budgets budgets.yml --config config.yml --ledger-dir ./tx/ \
  --bucket 基金.旅游 --bucket-view detail

# 哪些桶超支了 -- 按结余排序
beancount-budget-tool --year 2026 \
  --budgets budgets.yml --config config.yml --ledger-dir ./tx/ \
  --sort-by remain

# 今年 vs 去年 -- 同比
beancount-budget-tool --year 2026 \
  --budgets budgets.yml --config config.yml --ledger-dir ./tx/ \
  --compare 2025-12

# 搜某次旅行 -- 关键词过滤
beancount-budget-tool --year 2026 \
  --budgets budgets.yml --config config.yml --ledger-dir ./tx/ \
  --bucket 基金.旅游 --bucket-view detail --filter "东京"

# 导出全年完整报告
beancount-budget-tool --year 2026 \
  --budgets budgets.yml --config config.yml --ledger-dir ./tx/ \
  --out-dir ./reports --csv-pivot --out-json
```

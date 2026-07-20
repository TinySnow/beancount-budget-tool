# beancount-budget-tool

Beancount 预算分析工具。输入账本 + 预算配置，自动生成预算统计与多格式报告。

## 快速开始

> 详细功能清单与用法见 **[QUICKSTART.md](QUICKSTART.md)**

**三样东西：**

| 文件 | 职责 | 写什么 |
|---|---|---|
| `budgets.yml` | **纯预算** | `default_monthly` 模板 + 按月覆盖 |
| `config.yml` | **全局配置** | 账户映射、桶类型、跟踪桶、资产账户 |
| Beancount 账本 | **实际支出** | `budget:` metadata 标注交易所属桶 |

**一行命令：**

```bash
beancount-budget-tool -m 2026-06 \
  --budgets budgets.yml --config config.yml \
  --ledger-dir ./transactions/ --scope cumulative
```

## 构建

```bash
cargo build --release
# 二进制在 target/release/beancount-budget-tool
```

## 核心概念

### budgets.yml — 纯预算计划

```yaml
default_monthly:          # 月预算模板（可选）
  储蓄: 700
  生活费:
    饮食: 2000
    交通: 300
  数码: 3000

"2026-06":                # 6月只写与模板不同的部分
  数码: 5000              # 覆盖模板 → 5000
  # 生活费.饮食 没写 → 继承模板 2000
```

- 嵌套 YAML 自动展平为 `生活费.饮食`、`生活费.交通`
- 父桶统计 = 子桶之和
- 支持模板继承 + 多套模板切换（`YYYY-MM template` 键）
- **跟踪桶不写在这里** — 移到 `config.yml` 的 `tracking_buckets`

### config.yml — 全局配置

```yaml
default_expense_bucket: 生活费

defaults:                              # 账户 → 桶（最长前缀匹配）
  "Expenses:Consume:饮食": 生活费.饮食
  "Expenses:Consume:交通": 生活费.交通
  "Expenses:Consume:电子": 数码

tracking_buckets:                      # 跟踪桶：只追踪不预算
  - 基金.旅游
  - 大额支出

asset_bucket_accounts:                 # 资产桶（可选）
  储蓄:
    - "Assets:Bank:建设银行"
```

### Beancount 标注

```beancount
; 基本用法：budget metadata
2026-06-17 * "京东" "买耳机"
  budget: "数码"
  Expenses:Consume:电子  2000 CNY
  Assets:Bank:工行       -2000 CNY

; 多桶归属（逗号分隔）
2026-06-20 * "京东" "耳机+游戏"
  budget: "数码, 爱好"
  Expenses:Consume:电子  2000 CNY
  Assets:Bank:工行       -2000 CNY

; 金额拆分（budget "桶名900" 尾部数字自动识别）
2026-06-25 * "基金定投"
  budget: "电子产品900, 旅游900"
  Assets:Invest:基金  1800 CNY
  Assets:Bank:工行    -1800 CNY

; 不写 budget → 自动匹配 defaults 前缀 → 回退 default_expense_bucket
2026-06-16 * "工行" "地铁"
  Expenses:Consume:交通  6 CNY
  Assets:Bank:工行      -6 CNY
```

## CLI

| 参数 | 说明 |
|---|---|
| `-m, --month <YYYY-MM>` | 目标月份 |
| `--from/--to <YYYY-MM>` | 时间范围 |
| `--year <YYYY>` | 全年快捷方式 |
| `--budgets <FILE>` | 预算文件（必需） |
| `--config, -c <FILE>` | 配置文件（必需，旧名 `--mappings` 仍可用） |
| `--scope <month\|cumulative>` | 统计范围（默认 month） |
| `--bucket <NAME>` | 指定桶明细 |
| `--bucket-view <summary\|monthly\|detail>` | 桶视图粒度 |
| `--sort-by <name\|planned\|actual\|remain>` | 排序 |
| `--expand` | 展开子桶 |
| `--compare <YYYY-MM>` | 同比对比 |
| `--filter <KEYWORD>` | 按关键词过滤交易 |
| `--out-dir <DIR>` | 导出报告 |
| `--csv-pivot` | 横向月表 CSV |
| `--out-json` | JSON 导出 |
| `--strict` | 未知桶报错退出 |

常用：

```bash
--year 2026 --expand                         # 展开看全年
--year 2026 --sort-by remain                 # 按结余排序
--bucket 基金.旅游 --bucket-view detail      # 单桶明细
--year 2026 --compare 2025-12               # 同比
--year 2026 --out-dir ./reports --csv-pivot # 导出
```

## 项目结构

```
src/
  cli.rs            CLI 参数
  config.rs         budgets.yml + config.yml 加载
  ledger.rs         Beancount 账本解析
  budget.rs         预算引擎（映射、聚合、资产追踪）
  util.rs           基础类型与工具
  main.rs           入口 + 集成测试
  report/
    mod.rs          重导出
    shared.rs       共享工具 + 文件导出
    text.rs         终端文本报告
    md.rs           Markdown 报告
    csv.rs          CSV 导出
    json.rs         JSON 导出
```

## 输出文件

| 文件 | 内容 |
|---|---|
| `summary-{range}.md` / `.txt` | 汇总报告 |
| `buckets-{range}.csv` | 桶级 planned/actual CSV |
| `bucket-{桶名}-{range}.md` | 单桶完整报告 |
| `asset-locations-{桶名}-{range}.md` | 资产桶资金位置 |
| `pivot-{range}.csv`（`--csv-pivot`） | 月×桶 横向透视 |
| `summary-{range}.json`（`--out-json`） | JSON 导出 |

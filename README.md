# beancount-budget-tool

Beancount 预算分析工具。输入账本 + 预算配置，自动生成预算统计与多格式报告。

支持 **TUI 交互模式**（无参数启动）和 **CLI 批处理模式**（传参运行）。

## 快速开始

> 详细功能清单与用法见 **[QUICKSTART.md](QUICKSTART.md)**

**TUI 模式 — 推荐日常使用：**

```bash
# 直接启动，全键盘操作
cargo run
```

**CLI 模式 — 脚本/自动化：**

```bash
beancount-budget-tool -m 2026-06 \
  --budgets budgets.yml --config config.yml \
  --ledger-dir ./transactions/ --scope cumulative
```

**三样东西：**

| 文件 | 职责 | 写什么 |
|---|---|---|
| `budgets.yml` | **纯预算** | `default_monthly` 模板 + 按月覆盖 |
| `config.yml` | **全局配置** | 账户映射、桶类型、跟踪桶、资产账户、汇率表 |
| Beancount 账本 | **实际支出** | `budget:` metadata 标注交易所属桶 |

## 构建

```bash
cargo build --release
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

tracking_buckets:                      # 跟踪桶：只追踪不预算
  - 基金.旅游
  - 大额支出

# 多币种汇率（无 @@ 注释的纯外币交易自动换算）
currency_rates:
  JPY: 0.04327
  USD: 6.77

asset_bucket_accounts:                 # 资产桶（可选）
  储蓄:
    - "Assets:Bank:建设银行"
```

### Beancount 标注

```beancount
; 基本用法
2026-06-17 * "京东" "买耳机"
  budget: "数码"
  Expenses:Consume:电子  2000 CNY
  Assets:Bank:工行       -2000 CNY

; 多桶 + 金额拆分
2026-06-25 * "基金定投"
  budget: "电子产品900, 旅游900"
  Assets:Invest:基金  1800 CNY
  Assets:Bank:工行    -1800 CNY

; 多币种：@@ 自动转换，或配 currency_rates
2026-07-16 * "Hotel" "日本旅行住宿"
  budget: "宁波日本之旅"
  Expenses:Travel  158.12 USD @@ 1070.16 CNY
  Liabilities:CreditCard  -158.12 USD @@ 1070.16 CNY
```

## CLI

| 参数 | 说明 |
|---|---|
| `-m, --month <YYYY-MM>` | 目标月份 |
| `--from/--to <YYYY-MM>` | 时间范围 |
| `--year <YYYY>` | 全年快捷方式 |
| `--budgets <FILE>` | 预算文件 |
| `--config, -c <FILE>` | 配置文件 |
| `--scope <month\|cumulative>` | 统计范围 |
| `--bucket <NAME>` | 指定桶明细 |
| `--bucket-view <s\|m\|d>` | 桶视图粒度 |
| `--sort-by <name\|planned\|actual\|remain>` | 排序 |
| `--expand` | 展开子桶 |
| `--compare <YYYY-MM>` | 同比对比 |
| `--filter <KEYWORD>` | 关键词过滤 |
| `--out-dir <DIR>` | 导出报告 |
| `--csv-pivot` | 横向月表 CSV |
| `--out-json` | JSON 导出 |
| `--currency <CODE>` | 币种（默认 CNY） |

## 项目结构

```
src/
  cli.rs            CLI 参数
  config.rs         budgets.yml + config.yml 加载
  ledger.rs         Beancount 账本解析（含 @@ 价格注释）
  budget.rs         预算引擎（FlowKind 分类 + 多币种转换）
  util.rs           基础类型（ReportScope）+ CJK 对齐
  tui.rs            TUI 交互界面
  main.rs           入口 + CLI/TUI 路由 + 集成测试
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
| `pivot-{range}.csv` | 月×桶 横向透视 |
| `summary-{range}.json` | JSON 导出 |

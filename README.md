# beancount-budget-tool

Beancount budget tool./Beancount 预算工具。

独立的 Beancount 预算分析工具。  
输入账本目录与预算配置后，自动生成预算统计与报告文件。

## 功能

- 支持月度预算与同月额外预算（如 `2026-06 绩效`）。
- 支持 `month`（仅当月）与 `cumulative`（截至当月累计）两种统计范围。
- 支持预算桶历史查询（汇总/分月/明细）。
- 支持资产类预算桶（如储蓄）统计与“资金位置”追踪。
- 未显式标注预算桶的 `Expenses:*` 自动归入默认生活费桶。
- 支持递归扫描账本目录（`.bean` / `.beancount`）。
- 支持自动导出 Markdown / CSV 报告。

## 构建

```bash
cargo build --release
```

可执行文件位置：

```bash
./target/release/beancount-budget-tool
```

## 快速使用

### 1. 扫描账本目录并生成报告

```bash
./target/release/beancount-budget-tool \
  --month 2026-06 \
  --ledger-dir /path/to/ledger-root \
  --budgets /path/to/budgets.yaml \
  --mappings /path/to/mappings.yaml \
  --scope cumulative \
  --out-dir /path/to/reports
```

### 2. 同时传多个账本文件

```bash
./target/release/beancount-budget-tool \
  --month 2026-06 \
  --ledger /path/a.beancount \
  --ledger /path/b.bean \
  --budgets /path/to/budgets.yaml \
  --mappings /path/to/mappings.yaml
```

### 3. 只看某个预算桶

```bash
./target/release/beancount-budget-tool \
  --month 2026-06 \
  --ledger-dir /path/to/ledger-root \
  --budgets /path/to/budgets.yaml \
  --mappings /path/to/mappings.yaml \
  --scope cumulative \
  --bucket 储蓄 \
  --bucket-view detail \
  --show-locations
```

## 输出文件

设置 `--out-dir` 后，会生成：

- `summary-<month>-<scope>.md`：预算总览报告。
- `summary-<month>-<scope>.txt`：终端同款文本报告。
- `buckets-<month>-<scope>.csv`：每个预算桶的 planned/actual/remain。
- `bucket-<bucket>-<month>-<scope>.md`：每个预算桶的历史报告。
- `asset-locations-<bucket>-<month>-<scope>.md`：资产类预算桶位置报告。

## 配置说明

示例文件：

- `examples/budgets.yaml`
- `examples/mappings.yaml`

`budgets.yaml` 支持：

- `YYYY-MM`：当月基础预算
- `YYYY-MM 标签`：同月额外预算（如绩效/年终奖）

`mappings.yaml` 关键字段：

- `default_expense_bucket`
- `bucket_types`（`expense` / `asset`）
- `defaults`（账户前缀映射，最长匹配优先）
- `asset_bucket_accounts`（可选，提升资产位置定位精度）

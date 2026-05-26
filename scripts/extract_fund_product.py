#!/usr/bin/env python3
"""
dry-run 预览：从支付宝基金 narration 中提取产品名，生成新的账户路径建议。

不修改任何原始文件，仅输出变更预览。

用法：
  python3 scripts/extract_fund_product.py ~/homelab/projects/beancount/transactions/
"""
import re
import sys
import os
from pathlib import Path
from typing import Optional

# ---------- 解析配置 ----------

FUND_ACCOUNT = "Assets:Invest-投资:基金:支付宝"
FUND_PREFIX = "Assets:Invest-投资:基金:支付宝"

# narration 中产品名的提取模式（按优先级）
PRODUCT_PATTERNS = [
    (r"蚂蚁财富-(.+?)-(?:卖出|买入|分红|转出)", "蚂蚁财富买卖"),
    (r"蚂蚁财富-(.+?)$", "蚂蚁财富其他"),
    (r"余额宝-(.+?)-收益发放", "余额宝收益"),
    (r"余额宝", "余额宝自身"),
    # 可扩展：添加更多基金公司的 pattern
]

# ---------- 产品名清理 ----------

def clean_product_name(raw: str) -> str:
    """清理和标准化产品名"""
    name = raw.strip()
    # 去除常见的后缀词
    suffixes = [
        '联接', '发起式', 'A', 'B', 'C', 'D', 'E',
        '（QDII）', '(QDII)', '（LOF）', '(LOF)', '（ETF）', '(ETF)',
        '-卖出至余额宝', '-买入', '-分红', '-转出',
        '-活动赠送',
    ]
    for s in sorted(suffixes, key=len, reverse=True):
        name = name.replace(s, '')
    name = name.strip()
    # 非法字符替换
    name = name.replace('/', '-').replace('\\', '-').replace(':', '-').replace('（', '(').replace('）', ')')
    # 限制长度
    if len(name) > 30:
        name = name[:30]
    return name.strip() or raw[:30]


def extract_product(narration: str) -> Optional[str]:
    """从 narration 中提取基金产品名"""
    for pattern, _desc in PRODUCT_PATTERNS:
        m = re.search(pattern, narration)
        if m:
            return clean_product_name(m.group(1))
    return None


# ---------- 扫描与生成变更 ----------

def scan_files(root_dir: str):
    """扫描所有 .bean 文件，生成变更建议"""
    root = Path(root_dir)
    changes = {}

    for bean_file in sorted(root.rglob("*.bean")):
        try:
            content = bean_file.read_text(encoding='utf-8')
        except Exception:
            continue

        lines = content.split('\n')
        file_changes = []

        # 第一遍：收集 narration
        current_narration = None
        for i, line in enumerate(lines):
            stripped = line.strip()
            # 交易头 → 提取 narration
            if stripped.startswith(tuple('0123456789')) and '*' in stripped:
                m = re.search(r'"([^"]*)"\s*"([^"]*)"', stripped)
                if m:
                    current_narration = m.group(2)
                else:
                    m = re.search(r'"([^"]*)"', stripped)
                    current_narration = m.group(1) if m else None

            # 基金过账行（已带产品路径的跳过）
            if line.strip().startswith(FUND_PREFIX):
                rest = line.strip()[len(FUND_PREFIX):].lstrip()
                if rest.startswith(':') and rest[1:].split(' ')[0].strip():
                    continue  # 已有产品名
                if current_narration:
                    product = extract_product(current_narration)
                    if product:
                        indent = line[:len(line) - len(line.lstrip())]
                        parts = line.strip().split(None, 1)
                        if len(parts) >= 2:
                            new_account = f"{FUND_PREFIX}:{product}"
                            new_line = f"{indent}{new_account}  {parts[1]}"
                            file_changes.append((i + 1, line, new_line, product))

        if file_changes:
            changes[str(bean_file)] = file_changes

    return changes


# ---------- 输出预览 ----------

def print_diff(changes: dict):
    total = sum(len(v) for v in changes.values())
    print(f"共 {len(changes)} 个文件，{total} 条变更建议\n")

    products = set()
    for filepath, file_changes in changes.items():
        print(f"=== {filepath} ({len(file_changes)} 条) ===")
        for lineno, old_line, new_line, product in file_changes:
            products.add(product)
            # 缩短账户路径只显示差异部分
            old_short = old_line.strip().replace(FUND_PREFIX, '…')
            new_short = new_line.strip().replace(f'{FUND_PREFIX}:{product}', f'…:{product}')
            print(f"  L{lineno}: {old_short}")
            print(f"       → {new_short}")
        print()

    print(f"识别到的产品 ({len(products)} 个):")
    for p in sorted(products):
        print(f"  - {p}")


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    root_dir = sys.argv[1]
    if not os.path.isdir(root_dir):
        print(f"错误: 目录不存在: {root_dir}")
        sys.exit(1)

    changes = scan_files(root_dir)
    if not changes:
        print("未发现可提取产品名的基金交易")
    else:
        print_diff(changes)

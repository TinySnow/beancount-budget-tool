#!/usr/bin/env python3
"""
从支付宝基金 narration 中提取产品名，更新过账行账户路径。

默认 dry-run：不修改任何文件，仅输出变更预览。
--apply：实际写入变更（自动创建 .bak 备份）。

用法：
  # 预览
  python3 scripts/extract_fund_product.py ~/homelab/projects/beancount/transactions/

  # 实际写入
  python3 scripts/extract_fund_product.py ~/homelab/projects/beancount/transactions/ --apply
"""
import argparse
import re
import os
import sys
from pathlib import Path
from typing import Optional

FUND_ACCOUNT = "Assets:Invest-投资:基金:支付宝"
FUND_PREFIX = "Assets:Invest-投资:基金:支付宝"

PRODUCT_PATTERNS = [
    (r"蚂蚁财富-(.+?)-(?:卖出|买入|分红|转出)", "蚂蚁财富买卖"),
    (r"蚂蚁财富-(.+?)$", "蚂蚁财富其他"),
    (r"余额宝-(.+?)-收益发放", "余额宝收益"),
    (r"余额宝", "余额宝自身"),
]


def clean_product_name(raw: str) -> str:
    name = raw.strip()
    suffixes = [
        '联接', '发起式', 'A', 'B', 'C', 'D', 'E',
        '（QDII）', '(QDII)', '（LOF）', '(LOF)', '（ETF）', '(ETF)',
        '-卖出至余额宝', '-买入', '-分红', '-转出',
        '-活动赠送',
    ]
    for s in sorted(suffixes, key=len, reverse=True):
        name = name.replace(s, '')
    name = name.strip()
    name = name.replace('/', '-').replace('\\', '-').replace(':', '-').replace('（', '(').replace('）', ')')
    if len(name) > 30:
        name = name[:30]
    return name.strip() or raw[:30]


def extract_product(narration: str) -> Optional[str]:
    for pattern, _desc in PRODUCT_PATTERNS:
        m = re.search(pattern, narration)
        if m:
            return clean_product_name(m.group(1))
    return None


def scan_files(root_dir: str):
    root = Path(root_dir)
    changes = {}

    for bean_file in sorted(root.rglob("*.bean")):
        try:
            content = bean_file.read_text(encoding='utf-8')
        except Exception:
            continue

        lines = content.split('\n')
        file_changes = []

        current_narration = None
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith(tuple('0123456789')) and '*' in stripped:
                m = re.search(r'"([^"]*)"\s*"([^"]*)"', stripped)
                if m:
                    current_narration = m.group(2)
                else:
                    m = re.search(r'"([^"]*)"', stripped)
                    current_narration = m.group(1) if m else None

            if line.strip().startswith(FUND_PREFIX):
                rest = line.strip()[len(FUND_PREFIX):].lstrip()
                if rest.startswith(':') and rest[1:].split(' ')[0].strip():
                    continue
                if current_narration:
                    product = extract_product(current_narration)
                    if product:
                        indent = line[:len(line) - len(line.lstrip())]
                        parts = line.strip().split(None, 1)
                        if len(parts) >= 2:
                            new_account = f"{FUND_PREFIX}:{product}"
                            new_line = f"{indent}{new_account}  {parts[1]}"
                            file_changes.append((i, line, new_line, product))

        if file_changes:
            changes[str(bean_file)] = file_changes

    return changes


def print_diff(changes: dict):
    total = sum(len(v) for v in changes.values())
    print(f"共 {len(changes)} 个文件，{total} 条变更建议\n")

    products = set()
    for filepath, file_changes in changes.items():
        print(f"=== {filepath} ({len(file_changes)} 条) ===")
        for lineno, old_line, new_line, product in file_changes:
            products.add(product)
            old_short = old_line.strip().replace(FUND_PREFIX, '…')
            new_short = new_line.strip().replace(f'{FUND_PREFIX}:{product}', f'…:{product}')
            print(f"  L{lineno + 1}: {old_short}")
            print(f"       → {new_short}")
        print()

    print(f"识别到的产品 ({len(products)} 个):")
    for p in sorted(products):
        print(f"  - {p}")


def apply_changes(changes: dict):
    total = sum(len(v) for v in changes.values())
    print(f"正在写入 {len(changes)} 个文件（共 {total} 条变更）...\n")

    for filepath, file_changes in changes.items():
        p = Path(filepath)
        bak_path = p.with_suffix(p.suffix + '.bak')
        content = p.read_text(encoding='utf-8')
        bak_path.write_text(content, encoding='utf-8')

        lines = content.split('\n')
        for lineno, old_line, new_line, product in file_changes:
            lines[lineno] = new_line

        p.write_text('\n'.join(lines), encoding='utf-8')
        print(f"  ✓ {filepath} ({len(file_changes)} 条)  [备份: {bak_path.name}]")

    print(f"\n完成。备份文件后缀为 .bean.bak，确认无误后可删除。")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description="从支付宝基金 narration 中提取产品名，更新过账行账户路径。"
    )
    parser.add_argument(
        'root_dir',
        help='账本根目录（递归扫描 .bean 文件）'
    )
    parser.add_argument(
        '--apply',
        action='store_true',
        help='实际写入变更（自动创建 .bak 备份）；不传则为 dry-run 预览'
    )
    args = parser.parse_args()

    root_dir = args.root_dir
    if not os.path.isdir(root_dir):
        print(f"错误: 目录不存在: {root_dir}")
        sys.exit(1)

    changes = scan_files(root_dir)
    if not changes:
        print("未发现可提取产品名的基金交易")
    elif args.apply:
        apply_changes(changes)
    else:
        print_diff(changes)
        print("\n[提示] 这是 dry-run 预览。加 --apply 可实际写入变更。")

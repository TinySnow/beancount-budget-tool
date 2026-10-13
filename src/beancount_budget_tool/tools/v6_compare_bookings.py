"""只读比较两个 Beancount 版本对同一份账本的 booked 事实。

分别用两个 Python 环境执行 ``snapshot``，再用任一环境执行
``compare``。本工具仅证明源文件与 booked 财务事实是否一致；它不会
改写旧审阅文件，也不会把旧 loader 的人工批准自动迁移到新版。
"""

from __future__ import annotations

import argparse
from datetime import date
from decimal import Decimal
from enum import Enum
from hashlib import sha256
import json
from pathlib import Path
import sys

from beancount.core import data

from beancount_budget_tool.errors import BudgetError, require
from beancount_budget_tool.financial import load_financial


def _value(value):
    """消除 Python/Beancount 对象表示差异，保留会计字段和元数据。"""
    if value is None or type(value) in (str, int, bool):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Enum):
        return f"{type(value).__module__}.{type(value).__qualname__}.{value.name}"
    if isinstance(value, type):
        return f"{value.__module__}.{value.__qualname__}"
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [_value(part) for part in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_value(part) for part in value), key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True))
    if isinstance(value, dict):
        return {str(key): _value(item) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
                if str(key) not in ("filename", "lineno") and not str(key).startswith("__")}
    if hasattr(value, "_asdict"):
        return {key: _value(item) for key, item in value._asdict().items()}
    # 未知对象不能用 repr 猜其稳定语义。拒绝对照，等显式加入适配。
    raise BudgetError("BOOKING_COMPARE_UNSUPPORTED", f"未支持的 booked 字段类型：{type(value).__name__}")


def booking_snapshot(root: Path) -> dict:
    fin = load_financial(root)
    entries = []
    for entry in fin.entries:
        require(hasattr(entry, "_asdict"), "BOOKING_COMPARE_UNSUPPORTED",
                "无法比较没有结构字段的 booked 指令", type(entry).__name__)
        row = {"kind": type(entry).__name__, "fields": _value(entry._asdict())}
        entries.append(row)
    sources = {str(path.relative_to(fin.root.parent)) if path.is_relative_to(fin.root.parent)
               else str(path): source.sha256 for path, source in sorted(fin.sources.items())}
    return {"schema": 1, "loader_version": fin.loader_version,
            "root": str(fin.root), "sources": sources,
            "entry_count": len(entries), "entries": entries}


def compare_snapshots(left: dict, right: dict, *, limit: int = 12) -> dict:
    require(type(left) is dict and type(right) is dict and
            left.get("schema") == right.get("schema") == 1 and
            isinstance(left.get("entries"), list) and isinstance(right.get("entries"), list),
            "BOOKING_COMPARE_INVALID", "输入不是受支持的 booked 快照")
    require(left.get("root") == right.get("root") and left.get("sources") == right.get("sources"),
            "BOOKING_COMPARE_SOURCE_MISMATCH", "两次运行不是同一路径、同一组源文件内容")
    require(type(limit) is int and limit >= 0, "BOOKING_COMPARE_INVALID", "差异示例数量不能为负")
    a, b = left["entries"], right["entries"]
    differences = []
    count = 0
    for index in range(max(len(a), len(b))):
        one = a[index] if index < len(a) else None
        two = b[index] if index < len(b) else None
        if one == two:
            continue
        count += 1
        if len(differences) < limit:
            differences.append({"entry_index": index, "left_kind": one.get("kind") if one else None,
                                "right_kind": two.get("kind") if two else None,
                                "left": one, "right": two})
    def digest(rows):
        return sha256(json.dumps(rows, ensure_ascii=False, sort_keys=True,
                                 separators=(",", ":")).encode()).hexdigest()
    return {"mode": "v6_booking_cross_loader_compare",
            "source_identical": True,
            "left_loader": left["loader_version"], "right_loader": right["loader_version"],
            "left_entries": len(a), "right_entries": len(b),
            "left_booked_sha256": digest(a), "right_booked_sha256": digest(b),
            "booked_identical": count == 0, "difference_count": count,
            "difference_examples": differences,
            "note": "booked 一致也不自动迁移人工审阅；需按新版 loader 重验引用和侧文件"}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    scan = commands.add_parser("snapshot", help="在当前 Python/Beancount 环境产生只读快照")
    scan.add_argument("--ledger", type=Path, required=True)
    scan.add_argument("--output", type=Path, required=True, help="写入新的私有 JSON 文件，拒绝覆盖")
    compare = commands.add_parser("compare", help="比较两个版本的快照")
    compare.add_argument("--left", type=Path, required=True)
    compare.add_argument("--right", type=Path, required=True)
    compare.add_argument("--limit", type=int, default=12)
    args = parser.parse_args(argv)
    try:
        if args.command == "snapshot":
            result = booking_snapshot(args.ledger)
        else:
            result = compare_snapshots(json.loads(args.left.read_text(encoding="utf-8")),
                                       json.loads(args.right.read_text(encoding="utf-8")),
                                       limit=args.limit)
    except (BudgetError, OSError, ValueError) as exc:
        if isinstance(exc, BudgetError):
            error = {"code": exc.code, "message": exc.message, "source": exc.source}
        else:
            error = {"code": "BOOKING_COMPARE_INVALID", "message": str(exc)}
        print(json.dumps({"error": error}, ensure_ascii=False), file=sys.stderr)
        return 2
    output = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
    if args.command == "snapshot":
        try:
            with args.output.open("x", encoding="utf-8") as stream:
                stream.write(output)
                stream.write("\n")
        except OSError as exc:
            print(json.dumps({"error": {"code": "BOOKING_COMPARE_OUTPUT_INVALID",
                                        "message": str(exc)}}, ensure_ascii=False), file=sys.stderr)
            return 2
        print(json.dumps({"snapshot": str(args.output.resolve()),
                          "loader_version": result["loader_version"],
                          "entry_count": result["entry_count"]}, ensure_ascii=False))
    else:
        print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Expand approved private categories and show the new importer review queue."""

from __future__ import annotations

import argparse
from datetime import date
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

import yaml

from beancount_budget_tool.errors import BudgetError, require
from beancount_budget_tool.v6.review_queue import audit_review_queue, render_text
from beancount_budget_tool.yamlstrict import load_yaml

from .v6_expand_classification_rules import expand


def refresh(config: Path, rules: Path, since: date, through: date,
            output_classifications: Path) -> dict:
    config = config.resolve()
    output_classifications = output_classifications.resolve()
    doc = load_yaml(config)
    needed = {"schema", "ledger", "cutover", "accounts", "ownership",
              "obligations", "classifications", "purpose_pools"}
    require(type(doc) is dict and needed <= set(doc) and doc["schema"] == 6,
            "REVIEW_QUEUE_CONFIG_INVALID", "刷新配置缺少必要字段", str(config))
    require(not output_classifications.exists() and output_classifications.parent.is_dir(),
            "OUTPUT_EXISTS", "新分类文件已存在或输出目录不存在", str(output_classifications))
    try:
        cutover = date.fromisoformat(doc["cutover"])
    except (TypeError, ValueError) as exc:
        raise BudgetError("TIME_INVALID", "切换点必须是有效日期", str(config)) from exc

    def source_path(key: str) -> Path:
        value = doc[key]
        require(type(value) is str and bool(value.strip()),
                "REVIEW_QUEUE_CONFIG_INVALID", "配置路径不能为空", key)
        return (config.parent / value).resolve()

    ledger = source_path("ledger")
    existing = source_path("classifications")
    expanded, summary = expand(ledger, cutover, rules, existing)
    with TemporaryDirectory(prefix=".v6-review-", dir=output_classifications.parent) as folder:
        temporary = Path(folder)
        classifications = temporary / "classifications.yaml"
        classifications.write_text(yaml.safe_dump(expanded, allow_unicode=True,
                                                  sort_keys=False), encoding="utf-8")
        temporary_config = {"schema": 6, "ledger": str(ledger),
                            "cutover": cutover.isoformat(),
                            "classifications": str(classifications)}
        for key in ("accounts", "ownership", "obligations", "purpose_pools"):
            temporary_config[key] = str(source_path(key))
        review_config = temporary / "config.yaml"
        review_config.write_text(yaml.safe_dump(temporary_config, allow_unicode=True,
                                                sort_keys=False), encoding="utf-8")
        queue = audit_review_queue(review_config, since, through)
        require(summary["ledger_snapshot"] == queue["ledger_snapshot"],
                "INPUT_CHANGED", "分类展开和待审清单读取到不同账本快照", str(ledger))
        try:
            os.link(classifications, output_classifications)
        except FileExistsError as exc:
            raise BudgetError("OUTPUT_EXISTS", "新分类文件已存在，拒绝覆盖",
                              str(output_classifications)) from exc
    return {"classification_expansion": summary,
            "output_classifications": str(output_classifications),
            "review_queue": queue}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--rules", required=True, type=Path)
    parser.add_argument("--since", required=True, type=date.fromisoformat)
    parser.add_argument("--through", required=True, type=date.fromisoformat)
    parser.add_argument("--output-classifications", required=True, type=Path)
    parser.add_argument("--format", choices=("text", "json"), default="text")
    args = parser.parse_args(argv)
    try:
        result = refresh(args.config, args.rules, args.since, args.through,
                         args.output_classifications)
    except BudgetError as exc:
        print(json.dumps({"error": {"code": exc.code, "message": exc.message,
                                    "source": exc.source}}, ensure_ascii=False), file=sys.stderr)
        return 2
    if args.format == "json":
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        summary = result["classification_expansion"]
        print(f"分类续审：保留 {summary['preserved_claims']} 条，新增 "
              f"{summary['generated_claims']} 条，仍待审 {summary['still_unreviewed']} 条")
        print(f"新分类文件：{result['output_classifications']}")
        print(render_text(result["review_queue"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

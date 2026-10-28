"""Compact approved state; generated runtime inputs are not persistent data."""

from __future__ import annotations

import argparse
import base64
from copy import deepcopy
import gzip
import json
import os
from pathlib import Path
import shutil
from uuid import uuid4

import yaml

from ..console import configure_output
from ..errors import BudgetError, require
from ..yamlstrict import load_yaml


MONTHLY = {"completeness", "debt_coverage", "debt_settlements"}
SCALARS = {"schema", "ledger", "cutover", "month", "report_currency"}


def _undo_diff(old, new):
    """Retain overwritten evidence without copying every appended claim."""
    if type(old) is type(new) and old == new:
        return None
    if type(old) is dict and type(new) is dict:
        return {"kind": "mapping", "added": list(new.keys() - old.keys()),
                "removed": {key: deepcopy(old[key]) for key in old.keys() - new.keys()},
                "changed": {key: delta for key in old.keys() & new.keys()
                            if (delta := _undo_diff(old[key], new[key])) is not None}}
    if type(old) is list and type(new) is list:
        return {"kind": "sequence", "length": len(old), "tail": deepcopy(old[len(new):]),
                "changed": {str(index): delta for index in range(min(len(old), len(new)))
                            if (delta := _undo_diff(old[index], new[index])) is not None}}
    return {"kind": "value", "value": deepcopy(old)}


def restore_previous(new, delta):
    """Reconstruct an earlier approved value for recovery/export tooling."""
    if delta is None:
        return deepcopy(new)
    if delta["kind"] == "value":
        return deepcopy(delta["value"])
    result = deepcopy(new)
    if delta["kind"] == "mapping":
        for key in delta["added"]:
            result.pop(key, None)
        result.update(deepcopy(delta["removed"]))
        for key, change in delta["changed"].items():
            result[key] = restore_previous(new[key], change)
    else:
        result = result[:delta["length"]]
        for index, change in delta["changed"].items():
            result[int(index)] = restore_previous(new[int(index)], change)
        result.extend(deepcopy(delta["tail"]))
    return result


def state_file(config: Path) -> Path:
    from .simple_config import state_dir
    return state_dir(config) / "reviews.json.gz"


def _empty():
    return {"schema": 1, "header": None, "shared": {}, "monthly": {}, "history": [], "recovery_files": {}}


def _read(path: Path):
    try:
        data = json.loads(gzip.decompress(path.read_bytes()))
    except (OSError, EOFError, ValueError) as exc:
        raise BudgetError("SIMPLE_STATE_INVALID", "审阅状态损坏；请恢复备份，不要删除后继续签发", str(path)) from exc
    require(type(data) is dict and data.get("schema") == 1 and
            all(type(data.get(key)) is dict for key in ("shared", "monthly", "recovery_files")) and
            (data.get("header") is None or type(data["header"]) is dict),
            "SIMPLE_STATE_INVALID", "审阅状态格式无效；原文件保留", str(path))
    require(data["header"] is None or SCALARS <= set(data["header"]),
            "SIMPLE_STATE_INVALID", "审阅状态缺少账本范围；原文件保留", str(path))
    require(all(type(month) is str and type(documents) is dict for month, documents in data["monthly"].items()),
            "SIMPLE_STATE_INVALID", "月份审阅状态格式无效", str(path))
    data.setdefault("history", [])
    require(type(data["history"]) is list, "SIMPLE_STATE_INVALID", "审阅历史格式无效", str(path))
    return data


def load_view(config: Path, inputs: dict, month: str):
    path = state_file(config)
    if not path.exists():
        inputs[path] = None
        return None
    inputs[path] = path.read_bytes()
    data = _read(path)
    documents = deepcopy(data["shared"])
    documents.update(deepcopy(data["monthly"].get(month, {})))
    return documents, data["header"]


def _legacy_data(config: Path, watched: dict):
    from .simple_config import _legacy_baseline, _unchanged, state_dir
    manifest = state_dir(config) / "latest.yaml"
    data = _empty()
    if not manifest.exists():
        watched[manifest] = None
        return data
    # A deliberately unrelated month returns common decisions without a
    # monthly completeness/debt statement. No financial evidence is invented.
    def read_month(month):
        inputs = {}
        result = _legacy_baseline(config, inputs, month)
        for path, body in inputs.items():
            watched.setdefault(path, body)
        _unchanged(watched)
        return result
    shared, header = read_month("0001-01")
    pointer = load_yaml(manifest)
    months = set(pointer.get("months", {})) | {header["month"]}
    data.update(header=deepcopy(header), shared={key: deepcopy(value) for key, value in shared.items() if key not in MONTHLY})
    for month in months:
        documents, _ = read_month(month)
        data["monthly"][month] = {key: documents[key] for key in MONTHLY & set(documents)}
    data["legacy_manifest"] = watched[manifest].decode("utf-8")
    return data


def _archive(config: Path, data: dict, watched: dict):
    """Preserve old receipts and referenced inputs before removing their files.

    Unreferenced runtime caches are deliberately excluded. Raw recovery files
    preserve even incomplete or superseded review receipts without keeping a
    directory full of duplicates. Ledger files are never copied or deleted.
    """
    from .simple_config import state_dir
    root = state_dir(config).resolve()
    queue = []
    for name in ("snapshots", "reviews"):
        folder = root / name
        if folder.exists():
            require(not folder.is_symlink(), "SIMPLE_STATE_LINK", "清理不跟随内部目录的符号链接", str(folder))
            queue.extend(path for path in folder.rglob("*") if path.is_file() or path.is_symlink())
    if (root / "latest.yaml").exists():
        queue.append(root / "latest.yaml")
    seen = set()
    while queue:
        path = queue.pop()
        require(not path.is_symlink() and path.resolve().is_relative_to(root),
                "SIMPLE_STATE_LINK", "清理不跟随越界路径或符号链接；原文件保留", str(path))
        path = path.resolve()
        if path in seen or not path.is_file():
            continue
        seen.add(path)
        body = path.read_bytes()
        watched.setdefault(path, body)
        relative = path.relative_to(root).as_posix()
        try:
            text = body.decode("utf-8")
            entry = {"encoding": "utf-8", "data": text}
        except UnicodeDecodeError:
            text = None
            entry = {"encoding": "base64", "data": base64.b64encode(body).decode("ascii")}
        data["recovery_files"][relative] = entry
        if text is None or path.suffix not in (".yaml", ".yml"):
            continue
        try:
            document = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise BudgetError("SIMPLE_STATE_RECOVERY_INVALID", "旧审阅记录无法解析；未删除缓存或记录", str(path)) from exc
        if type(document) is not dict:
            continue
        references = []
        if SCALARS <= set(document):
            references.extend(value for key, value in document.items() if key not in SCALARS and type(value) is str)
        if type(document.get("config")) is str:
            references.append(document["config"])
        for value in references:
            referenced = (path.parent / value).resolve()
            if referenced.is_relative_to(root) and referenced.is_file():
                queue.append(referenced)


def _store(config: Path, data: dict, watched: dict):
    from .simple_config import _unchanged
    path = state_file(config)
    # Keep declaration key order: existing reports fingerprint the rendered
    # completeness document, so compaction must not reorder its fields.
    raw = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    body = gzip.compress(raw, mtime=0)
    require(json.loads(gzip.decompress(body)) == data, "SIMPLE_STATE_INVALID", "状态序列化核对失败；原数据保留")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(uuid4().hex + ".tmp")
    try:
        temporary.write_bytes(body)
        _unchanged(watched)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def save(config: Path, reviewed: Path, *, expected: dict | None = None):
    from .simple_config import _unchanged
    watched = dict(expected or {})
    _unchanged(watched)
    path = state_file(config)
    watched.setdefault(path, path.read_bytes() if path.exists() else None)
    data = _read(path) if path.exists() else _legacy_data(config, watched)
    if not path.exists():
        _archive(config, data, watched)
    watched[reviewed] = reviewed.read_bytes()
    document = load_yaml(reviewed)
    require(type(document) is dict and SCALARS <= set(document) and document["schema"] == 6,
            "SIMPLE_STATE_INVALID", "须保存完整的已审月配置", str(reviewed))
    header = {key: document[key] for key in SCALARS}
    header["ledger"] = str((reviewed.parent / document["ledger"]).resolve())
    old_header = data.get("header")
    require(old_header is None or all(old_header[key] == header[key] for key in ("cutover", "report_currency")),
            "SIMPLE_STATE_CONTEXT_CHANGED", "审阅记录的 cutover／报告货币不一致", str(reviewed))
    documents = {}
    for key in set(document) - SCALARS:
        source = (reviewed.parent / document[key]).resolve()
        watched[source] = source.read_bytes()
        documents[key] = load_yaml(source)
    shared = {key: value for key, value in documents.items() if key not in MONTHLY}
    monthly = {**data["monthly"], document["month"]: {key: documents[key] for key in MONTHLY & set(documents)}}
    if old_header is not None:
        undo_shared = _undo_diff(data["shared"], shared)
        undo_monthly = _undo_diff(data["monthly"], monthly)
        if undo_shared is not None or undo_monthly is not None or old_header != header:
            data["history"].append({"header": deepcopy(old_header), "undo_shared": undo_shared, "undo_monthly": undo_monthly})
    data.update(header=header, shared=shared, monthly=monthly)
    _store(config, data, watched)


def clean(config: Path, *, dry_run=False):
    from .simple_config import _unchanged, state_dir
    config = config.resolve()
    location = state_dir(config)
    require(not location.is_symlink() and not location.parent.is_symlink(),
            "SIMPLE_STATE_LINK", "清理不跟随状态目录的符号链接；原文件保留", str(location))
    root = location.resolve()
    path = state_file(config)
    require(not (root / "latest.yaml").is_symlink(), "SIMPLE_STATE_LINK", "不能清理符号链接状态指针", str(root / "latest.yaml"))
    watched = {path: path.read_bytes() if path.exists() else None}
    data = _read(path) if path.exists() else _legacy_data(config, watched)
    manifest = root / "latest.yaml"
    if path.exists() and manifest.exists() and "legacy_manifest" in data:
        require(manifest.read_text(encoding="utf-8") == data["legacy_manifest"],
                "SIMPLE_LEGACY_CHANGED", "旧版又保存了审阅记录；原数据全部保留，请先合并最新决定", str(manifest))
    _archive(config, data, watched)
    folders = [root / name for name in ("runtime", "snapshots", "reviews") if (root / name).exists()]
    for folder in folders:
        require(not folder.is_symlink() and folder.resolve().is_relative_to(root),
                "SIMPLE_STATE_LINK", "不能清理符号链接或越界目录", str(folder))
        require(not any(p.is_symlink() for p in folder.rglob("*")),
                "SIMPLE_STATE_LINK", "内部目录含符号链接；未清理", str(folder))
    files = sum(sum(p.is_file() for p in folder.rglob("*")) for folder in folders) + int(manifest.exists())
    if not dry_run:
        if data["header"] is not None or data["recovery_files"]:
            _store(config, data, watched)
            watched[path] = path.read_bytes()
        _unchanged(watched)
        for folder in folders:
            shutil.rmtree(folder)
        manifest.unlink(missing_ok=True)
        for folder in (root, root.parent):
            if folder.is_dir() and not any(folder.iterdir()):
                folder.rmdir()
    return {"dry_run": dry_run, "files_removed": files,
            "state_file": str(path) if path.exists() or data["header"] is not None else None,
            "retained_months": sorted(data["monthly"]),
            "note": "运行依赖 _internal、账本和两份用户配置均未删除；已有审阅记录保留在单个压缩状态文件中。"}


def export(config: Path, output: Path, month=None):
    from .simple_config import build_runtime, _write
    require(not output.exists() and not output.is_symlink(), "STATE_EXPORT_EXISTS", "导出目录须不存在", str(output))
    runtime = build_runtime(config, month)
    try:
        require(runtime.config is not None, "SIMPLE_CONFIG_REVIEW", "先补齐 config.yml／budgets.yml 中的配置项")
        document = load_yaml(runtime.config)
        files = {key: (runtime.config.parent / document[key]).read_bytes() for key in set(document) - SCALARS}
        runtime.check()
        output.mkdir(parents=True)
        for key, body in files.items():
            (output / f"{key}.yaml").write_bytes(body)
            document[key] = f"{key}.yaml"
        _write(output / "month.yaml", document)
        return {"config": str((output / "month.yaml").resolve()), "note": "高级配置副本；修改并审阅后可用 state --import-reviewed 保存回两文件入口。"}
    finally:
        runtime.close()


def main(argv=None):
    configure_output()
    parser = argparse.ArgumentParser(description="清理可重建缓存、压缩审阅记录；请先退出其他预算工具窗口")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true", help="只查看清理清单")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--export", type=Path, help="导出当前有效的高级配置副本")
    action.add_argument("--import-reviewed", type=Path, help="保存已审阅的高级 month.yaml")
    action.add_argument("--check", action="store_true", help="核对当前审阅记录的来源引用，不签发财务数字")
    parser.add_argument("--month")
    args = parser.parse_args(argv)
    try:
        from .simple_config import is_simple, save_snapshot
        require(is_simple(load_yaml(args.config)), "SIMPLE_CONFIG_INVALID", "请指定两文件入口 config.yml", str(args.config))
        if args.check:
            from .simple_config import build_runtime
            from .reference_checks import diagnose
            runtime = build_runtime(args.config, args.month)
            try:
                require(runtime.config is not None, 'SIMPLE_CONFIG_REVIEW', '先补齐两份配置')
                result = diagnose(runtime.config)
                runtime.check()
            finally:
                runtime.close()
        elif args.export:
            result = export(args.config, args.export, args.month)
        elif args.import_reviewed:
            save_snapshot(args.config, args.import_reviewed)
            result = {"state_file": str(state_file(args.config)), "note": "已保存审阅状态；按 r 重算。"}
        else:
            result = clean(args.config, dry_run=args.dry_run)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (BudgetError, OSError, UnicodeError, TypeError) as exc:
        import sys
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

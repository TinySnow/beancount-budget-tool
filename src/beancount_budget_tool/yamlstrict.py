"""Read reviewable YAML without silently accepting duplicate or implicit dates."""

from pathlib import Path
from typing import Any

import yaml

from .errors import BudgetError


class StrictLoader(yaml.SafeLoader):
    pass


def _mapping(loader: StrictLoader, node: yaml.MappingNode, deep: bool = False) -> dict:
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in result
        except TypeError as exc:
            raise BudgetError("YAML_KEY_INVALID", "YAML 映射键不可散列",
                              f"{loader.source_path}:{key_node.start_mark.line + 1}") from exc
        if duplicate:
            raise BudgetError("YAML_DUPLICATE_KEY", f"重复 YAML 键：{key}",
                              f"{loader.source_path}:{key_node.start_mark.line + 1}")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def load_yaml(path: Path) -> Any:
    try:
        loader = StrictLoader(path.read_text(encoding="utf-8-sig"))
        loader.source_path = str(path.resolve())
        try:
            return loader.get_single_data()
        finally:
            loader.dispose()
    except BudgetError:
        raise
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        mark = getattr(exc, "problem_mark", None) or getattr(exc, "context_mark", None)
        source = f"{path.resolve()}:{mark.line + 1}" if mark else str(path.resolve())
        raise BudgetError("YAML_INVALID", str(exc), source) from exc


def yaml_key_location(path: Path, key: str) -> str:
    """Locate a top-level config key; missing keys point at the document start."""
    try:
        node = yaml.compose(path.read_text(encoding="utf-8-sig"), Loader=StrictLoader)
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        mark = getattr(exc, "problem_mark", None) or getattr(exc, "context_mark", None)
        source = f"{path.resolve()}:{mark.line + 1}" if mark else str(path.resolve())
        raise BudgetError("YAML_INVALID", str(exc), source) from exc
    if isinstance(node, yaml.MappingNode):
        for name, _ in node.value:
            if name.value == key:
                return f"{path.resolve()}:{name.start_mark.line + 1}"
    return f"{path.resolve()}:1"


def require_quoted_month_keys(path: Path) -> None:
    """检查所有按月索引的字段；YAML 构造后的字符串已丢失引号信息。

    `months` 是只作用于该月的覆盖，`effective_templates` 是从该月
    起持续生效的字段级模板。两者都必须用明确引号，不能由 YAML
    自行决定某个键是日期对象还是普通字符串。
    """
    try:
        document = yaml.compose(path.read_text(encoding="utf-8-sig"), Loader=StrictLoader)
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise BudgetError("YAML_INVALID", str(exc), str(path)) from exc
    if not isinstance(document, yaml.MappingNode):
        return
    for key, value in document.value:
        if key.value not in ("months", "effective_templates") or not isinstance(value, yaml.MappingNode):
            continue
        for month_key, _ in value.value:
            if month_key.style not in ("'", '"'):
                raise BudgetError("PLAN_AMBIGUOUS", f"月份键必须加引号：{month_key.value}",
                                  f"{path.resolve()}:{month_key.start_mark.line + 1}")

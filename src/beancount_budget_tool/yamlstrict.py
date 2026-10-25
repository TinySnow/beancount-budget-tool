"""Read reviewable YAML without silently accepting duplicate or implicit dates."""

from pathlib import Path
from functools import lru_cache
from typing import Any

import yaml

from .errors import BudgetError


# Both loaders use the same safe constructors and strict mapping checks.
# Wheels normally include libyaml; pure-Python installations keep the fallback.
class StrictLoader(getattr(yaml, "CSafeLoader", yaml.SafeLoader)):
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
    return yaml_path_location(path, (key,))


def require_yaml(condition: bool, code: str, message: str, path: Path | None,
                 keys: tuple[str | int, ...] = (), fallback: str | None = None) -> None:
    """Resolve a diagnostic location only when validation actually fails."""
    if not condition:
        raise BudgetError(code, message, yaml_path_location(path, keys) if path is not None else fallback)


@lru_cache(maxsize=32)
def _location_tree(text: str):
    """Read-only syntax for positions/styles, keyed by the complete current text.

    Callers still read the file on every use. Constructed configuration values,
    approvals, source validation and budget results are deliberately not cached.
    The private tree is never mutated or returned to a configuration consumer.
    """
    return yaml.compose(text, Loader=StrictLoader)


def yaml_path_location(path: Path, keys: tuple[str | int, ...]) -> str:
    """Locate a nested field; an absent field points at its closest ancestor."""
    try:
        node = _location_tree(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        mark = getattr(exc, "problem_mark", None) or getattr(exc, "context_mark", None)
        source = f"{path.resolve()}:{mark.line + 1}" if mark else str(path.resolve())
        raise BudgetError("YAML_INVALID", str(exc), source) from exc
    line = 1
    for key in keys:
        if isinstance(node, yaml.MappingNode):
            match = next(((name, value) for name, value in node.value
                          if name.value == str(key)), None)
            if match is None:
                break
            name, node = match
            line = name.start_mark.line + 1
        elif isinstance(node, yaml.SequenceNode) and type(key) is int and 0 <= key < len(node.value):
            node = node.value[key]
            line = node.start_mark.line + 1
        else:
            break
    return f"{path.resolve()}:{line}"


def require_quoted_month_keys(path: Path) -> None:
    """检查所有按月索引的字段；YAML 构造后的字符串已丢失引号信息。

    `months` 是只作用于该月的覆盖，`effective_templates` 是从该月
    起持续生效的字段级模板。两者都必须用明确引号，不能由 YAML
    自行决定某个键是日期对象还是普通字符串。
    """
    try:
        document = _location_tree(path.read_text(encoding="utf-8-sig"))
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

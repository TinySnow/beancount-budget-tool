"""Position lookup reuses syntax without hiding edits or source validation."""

import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import yaml

from beancount_budget_tool.errors import BudgetError
from beancount_budget_tool.yamlstrict import (
    _location_tree, _mapping, StrictLoader, load_yaml, require_quoted_month_keys, yaml_path_location,
)


class YamlLocationCacheTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.path = self.base / "first.yaml"
        _location_tree.cache_clear()
        self.addCleanup(_location_tree.cache_clear)

    def strict_loaders(self):
        class PythonStrictLoader(yaml.SafeLoader):
            pass
        PythonStrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)
        return (StrictLoader, PythonStrictLoader)

    def test_accelerated_and_python_loaders_preserve_values_and_fresh_copies(self):
        text = "\ufeffschema: 1\n名称: '生活费'\nflags: [true, false, null]\namount: '0.000000000000000001'\ndate: 2023-07-01\n"
        self.path.write_text(text, encoding="utf-8")
        documents = []
        for loader in self.strict_loaders():
            with patch("beancount_budget_tool.yamlstrict.StrictLoader", loader):
                document = load_yaml(self.path)
                documents.append(document)
                document["flags"].append("caller modification")
                self.assertEqual(load_yaml(self.path)["flags"], [True, False, None])
        self.assertEqual(documents[0], documents[1])

    def test_accelerated_and_python_loaders_keep_duplicate_and_error_positions(self):
        for text in ("outer:\n  item: one\n  item: two\n", "items: [\n",
                     "? [bad, key]\n: value\n", "bad: !!python/object:builtins.object {}\n"):
            with self.subTest(text=text):
                self.path.write_text(text, encoding="utf-8")
                errors = []
                for loader in self.strict_loaders():
                    with patch("beancount_budget_tool.yamlstrict.StrictLoader", loader):
                        with self.assertRaises(BudgetError) as caught:
                            load_yaml(self.path)
                        errors.append((caught.exception.code, caught.exception.source))
                self.assertEqual(errors[0], errors[1])

    def test_repeated_lookup_reuses_syntax_but_reports_each_callers_path(self):
        text = "items:\n  - name: one\n    amount: '10'\n"
        self.path.write_text(text, encoding="utf-8")
        other = self.base / "second.yaml"
        other.write_text(text, encoding="utf-8")
        with patch("beancount_budget_tool.yamlstrict.yaml.compose", wraps=yaml.compose) as compose:
            self.assertEqual(yaml_path_location(self.path, ("items", 0, "amount")), f"{self.path}:3")
            self.assertEqual(yaml_path_location(self.path, ("items", 0, "name")), f"{self.path}:2")
            self.assertEqual(yaml_path_location(other, ("items", 0, "amount")), f"{other}:3")
            self.assertEqual(compose.call_count, 1)

    def test_same_size_edit_with_preserved_timestamp_changes_source_line(self):
        before, after = "first: 1\nsecond: 2\n", "second: 2\nfirst: 1\n"
        self.assertEqual(len(before), len(after))
        self.path.write_text(before, encoding="utf-8")
        original = self.path.stat()
        self.assertEqual(yaml_path_location(self.path, ("second",)), f"{self.path}:2")
        self.path.write_text(after, encoding="utf-8")
        os.utime(self.path, ns=(original.st_atime_ns, original.st_mtime_ns))
        self.assertEqual(yaml_path_location(self.path, ("second",)), f"{self.path}:1")

    def test_cached_location_cannot_hide_a_removed_file(self):
        self.path.write_text("key: value\n", encoding="utf-8")
        yaml_path_location(self.path, ("key",))
        self.path.unlink()
        with self.assertRaises(BudgetError) as caught:
            yaml_path_location(self.path, ("key",))
        self.assertEqual(caught.exception.code, "YAML_INVALID")
        self.assertEqual(caught.exception.source, str(self.path))

    def test_month_quote_style_is_rechecked_after_edit(self):
        self.path.write_text("months:\n  '2026-09': {}\n", encoding="utf-8")
        require_quoted_month_keys(self.path)
        yaml_path_location(self.path, ("months", "2026-09"))
        self.path.write_text("months:\n  2026-09: {}\n", encoding="utf-8")
        with self.assertRaises(BudgetError) as caught:
            require_quoted_month_keys(self.path)
        self.assertEqual((caught.exception.code, caught.exception.source), ("PLAN_AMBIGUOUS", f"{self.path}:2"))
        self.path.write_text("months: [\n", encoding="utf-8")
        with self.assertRaises(BudgetError) as caught:
            yaml_path_location(self.path, ("months",))
        self.assertEqual(caught.exception.code, "YAML_INVALID")
        self.assertTrue(caught.exception.source.startswith(str(self.path) + ":"))

    def test_syntax_cache_does_not_bypass_duplicate_key_validation_or_reuse_values(self):
        self.path.write_text("amount: '10'\n", encoding="utf-8")
        yaml_path_location(self.path, ("amount",))
        document = load_yaml(self.path)
        document["amount"] = "modified only in memory"
        self.assertEqual(load_yaml(self.path)["amount"], "10")
        self.path.write_text("amount: '10'\namount: '20'\n", encoding="utf-8")
        yaml_path_location(self.path, ("amount",))
        with self.assertRaises(BudgetError) as caught:
            load_yaml(self.path)
        self.assertEqual((caught.exception.code, caught.exception.source), ("YAML_DUPLICATE_KEY", f"{self.path}:2"))

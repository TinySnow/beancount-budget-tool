"""Projection reuse must preserve identities, caller isolation and invalidation."""

from copy import deepcopy
from datetime import date
from importlib import import_module
import os
import unittest
from unittest.mock import patch

import yaml
from beancount import __version__ as BEANCOUNT_VERSION

from beancount_budget_tool.financial import _copy_audit_result, frozen_financial
from beancount_budget_tool.v6.facts import FactIndex, frozen_fact_indices
from beancount_budget_tool.v6.h_fx import audit_h_fx
from beancount_budget_tool.v6.h_gains import audit_h_income_gains
from beancount_budget_tool.v6.h_transfers import audit_h_transfers
from beancount_budget_tool.v6.holdings import audit_h_holdings
from beancount_budget_tool.v6.k_events import audit_k_events
from beancount_budget_tool.v6 import input_compiler
from beancount_budget_tool.v6.valuation import audit_classification_valuation
import test_financial_session


class ProjectionReuseTests(unittest.TestCase):
    def setUp(self):
        test_financial_session.SharedBaseAuditTests.setUp(self)

    def projection_cases(self):
        opening = self.base / "opening.yaml"
        opening.write_text(yaml.safe_dump({"schema": 6, "cutover": self.cutover.isoformat(),
                                           "h_holdings": [], "k_openings": []}), encoding="utf-8")
        categories = self.base / "categories.yaml"
        categories.write_text(yaml.safe_dump({
            "schema": 6, "cutover": self.cutover.isoformat(),
            "loader_version": BEANCOUNT_VERSION,
            "categories": [{"id": "free", "name": "自由资金", "parent": None, "aliases": []}],
            "classifications": [], "defaults": {"income_category_id": "free", "expense_category_id": "free"},
        }, allow_unicode=True), encoding="utf-8")
        common = (self.root, self.cutover, self.accounts, self.ownership)
        return [(audit_h_fx, common, self.ownership),
                (audit_h_income_gains, common, self.ownership),
                (audit_h_transfers, common, self.ownership),
                (audit_k_events, common, self.ownership),
                (audit_h_holdings, (self.root, self.cutover, date(2023, 7, 31),
                                    self.accounts, opening, self.ownership), self.ownership),
                (audit_classification_valuation, (self.root, self.cutover, categories, "CNY"), categories)]

    def test_projections_match_uncached_and_recheck_same_stamp_sidefiles(self):
        for audit, args, watched in self.projection_cases():
            original = watched.read_bytes()
            stamp = watched.stat()
            with self.subTest(audit=audit.__name__), frozen_financial(self.root), frozen_fact_indices():
                expected = audit.__wrapped__(*args)
                with patch.object(import_module(audit.__module__), "FactIndex", wraps=FactIndex) as index:
                    first = audit(*args)
                    self.assertEqual(first, expected)
                    first["caller"] = [{"changed": True}]
                    second = audit(*args)
                    self.assertEqual(second, expected)
                    self.assertEqual(index.call_count, 1)
                    second["caller"] = ["changed again"]
                    self.assertEqual(audit(*args), expected)
                    watched.write_text(original.decode().replace("自由资金", "剩余资金"), encoding="utf-8")
                    os.utime(watched, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
                    audit(*args)
                    self.assertEqual(index.call_count, 2)
            watched.write_bytes(original)

    def test_inspection_reuses_only_the_same_scope_and_defaults(self):
        with frozen_financial(self.root), frozen_fact_indices():
            with patch.object(input_compiler, "load_financial", wraps=test_financial_session.load_financial) as load:
                args = (self.root, self.cutover, "自由资金", "生活费", "餐饮")
                first = input_compiler._inspect(*args)
                expected = deepcopy(first)
                first[1].add("caller modification")
                self.assertEqual(input_compiler._inspect(*args), expected)
                self.assertEqual(load.call_count, 1)
                changed = input_compiler._inspect(*args[:-1], "其他类别")
                self.assertIn("其他类别", changed[2])
                self.assertEqual(load.call_count, 2)

    def test_copy_preserves_cycles_aliases_and_non_json_values(self):
        shared = [{"value": 1}]
        class TaggedList(list):
            pass
        tagged = TaggedList([shared])
        tagged.note = []
        original = {"left": shared, "right": shared, "other": (tagged, {1, 2}), "date": self.cutover}
        original["cycle"] = original
        copied = _copy_audit_result(original)
        self.assertIs(copied["cycle"], copied)
        self.assertIs(copied["left"], copied["right"])
        self.assertIs(copied["other"][0][0], copied["left"])
        self.assertIsNot(copied["left"], original["left"])
        self.assertIsNot(copied["other"][0].note, tagged.note)
        copied["left"][0]["value"] = 2
        self.assertEqual(original["left"][0]["value"], 1)
        self.assertEqual(copied["date"], deepcopy(original["date"]))

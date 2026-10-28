"""Event file mistakes point to editable YAML, rather than opaque fact keys."""

from copy import deepcopy
from datetime import date
from unittest.mock import patch
import unittest

import test_v6_h_transfers as transfers
import test_v6_h_fx as fx
import test_v6_k_events as k_events

from beancount_budget_tool.errors import BudgetError
from beancount_budget_tool.v6.provenance import validate_event_declaration


class EventLocationTests(unittest.TestCase):
    def fixture(self, kind):
        cls = {"H_TRANSFER": transfers.HTransferTests, "H_FX": fx.HFXTests,
               "K_EVENT": k_events.KEventTests}[kind]
        case = cls()
        self.addCleanup(case.doCleanups)
        case.setUp()
        if kind == "H_TRANSFER":
            case.set_owner_legs("-", "-")
        elif kind == "H_FX":
            case.set_legs()
        return case

    def reject(self, case, document, code, *, field=None, last=False):
        with self.assertRaises(BudgetError) as caught:
            case.audit(document)
        error = caught.exception
        self.assertEqual(error.code, code)
        filename, lineno = error.source.rsplit(":", 1)
        self.assertEqual(filename, str(case.events.resolve()))
        lines = case.events.read_text(encoding="utf-8").splitlines()
        self.assertTrue(1 <= int(lineno) <= len(lines))
        if field:
            matches = [index + 1 for index, line in enumerate(lines)
                       if line.lstrip(" -").startswith(field + ":")]
            self.assertTrue(matches)
            self.assertEqual(int(lineno), matches[-1] if last else matches[0])
        return error

    def test_each_event_header_field_and_unknown_key_has_its_own_line(self):
        for kind in ("H_TRANSFER", "H_FX", "K_EVENT"):
            case = self.fixture(kind)
            for field, value in (("schema", 7), ("cutover", "wrong"),
                                 ("ledger_snapshot", "bad"), ("loader_version", "wrong"),
                                 ("events", {}), ("unexpected", True)):
                with self.subTest(kind=kind, field=field):
                    document = case.declaration()
                    document[field] = value
                    self.reject(case, document, kind + "_DECLARATION_INVALID", field=field)

    def test_missing_headers_non_mapping_events_and_missing_row_fields_are_located(self):
        for kind in ("H_TRANSFER", "H_FX", "K_EVENT"):
            case = self.fixture(kind)
            document = case.declaration()
            document.pop("events")
            self.reject(case, document, kind + "_DECLARATION_INVALID")
            for malformed in ("not an event", {"reason": "incomplete"}):
                with self.subTest(kind=kind, malformed=malformed):
                    document = case.declaration()
                    document["events"] = [malformed]
                    self.reject(case, document, kind + "_CLAIM_INVALID")

    def test_stale_opposite_and_duplicate_claims_identify_the_reference(self):
        for kind in ("H_TRANSFER", "H_FX", "K_EVENT"):
            case = self.fixture(kind)
            source, target = ("k_ref", "cash_ref") if kind == "K_EVENT" else ("from_ref", "to_ref")
            for field, code in ((source, kind + "_REF_INVALID"),
                                (target, kind + ("_CASH_REF_INVALID" if kind == "K_EVENT" else "_TO_REF_INVALID"))):
                with self.subTest(kind=kind, field=field):
                    document = case.declaration()
                    document["events"][0][field]["fact_sha256"] = "0" * 64
                    self.reject(case, document, code, field=field)
            document = case.declaration()
            document["events"].append(deepcopy(document["events"][0]))
            self.reject(case, document, kind + "_DOUBLE_CLAIM", field=source, last=True)

    def test_bad_numeric_values_identify_the_native_amount_field(self):
        for kind in ("H_TRANSFER", "H_FX", "K_EVENT"):
            case = self.fixture(kind)
            fields = ("signed_k_units", "signed_cash_units") if kind == "K_EVENT" else (
                "signed_from_units", "signed_to_units")
            for field in fields:
                for value in ("oops", 10, "1/0"):
                    with self.subTest(kind=kind, field=field, value=value):
                        document = case.declaration()
                        row = document["events"][0]
                        part = row if kind == "H_FX" else row["slices"][0]
                        part[field] = value
                        self.reject(case, document, kind + "_UNITS_INVALID", field=field)

    def test_duplicate_slice_id_and_invalid_role_identify_the_nested_field(self):
        for kind in ("H_TRANSFER", "K_EVENT"):
            case = self.fixture(kind)
            document = case.declaration()
            parts = document["events"][0]["slices"]
            parts.append(deepcopy(parts[0]))
            self.reject(case, document, kind + "_SLICE_INVALID", field="id", last=True)
            document = case.declaration()
            document["events"][0]["slices"][0]["boundary_role"] = "unknown"
            self.reject(case, document, kind + "_ROLE_INVALID", field="boundary_role")

    def test_ownership_conflict_after_parsing_retains_the_declaration_location(self):
        case = self.fixture("H_TRANSFER")
        self.reject(case, case.declaration("savings", "savings", "preserve"),
                    "H_TRANSFER_OWNER_MISMATCH")
        case = self.fixture("H_FX")
        self.reject(case, case.declaration("travel"), "H_FX_OWNER_MISMATCH")
        case = self.fixture("K_EVENT")
        document = case.declaration()
        for part in document["events"][0]["slices"]:
            part["k_owner_id"] = part["cash_owner_id"] = "travel"
        self.reject(case, document, "K_EVENT_CASH_OWNER_MISMATCH")

    def test_successful_header_checks_do_not_read_yaml_for_positions(self):
        case = self.fixture("H_TRANSFER")
        with patch("beancount_budget_tool.yamlstrict.yaml_path_location",
                   side_effect=AssertionError("eager location lookup")):
            validate_event_declaration(case.declaration(), case.events, date(2026, 9, 1),
                                       case.declaration()["loader_version"], "H_TRANSFER_DECLARATION_INVALID")


if __name__ == "__main__":
    unittest.main()

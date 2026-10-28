"""Contracts at the snapshot, identity, input-document and query boundaries."""

from copy import deepcopy
from datetime import date
from hashlib import sha256
import json
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import yaml
from unittest.mock import patch

from beancount import loader
from beancount.core import data

from beancount_budget_tool.errors import BudgetError
from beancount_budget_tool.financial import booked_through, financial_session, frozen_financial, load_financial
from beancount_budget_tool.identity import canonical_key, identity_scope
from beancount_budget_tool.projection import freeze, FrozenDict
from beancount_budget_tool.query import QuerySession
from beancount_budget_tool.runtime_inputs import document_scope, register_documents, input_bytes, input_is_file, document
from beancount_budget_tool.v6.facts import FactIndex, frozen_fact_indices
from beancount_budget_tool.v6.input_compiler import SetupDumper
from beancount_budget_tool.yamlstrict import load_yaml, yaml_path_location


class QueryArchitectureTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name).resolve()
        self.root = self.folder / 'main.bean'
        self.root.write_text('option "operating_currency" "CNY"\n'
                             '2026-01-01 open Assets:Cash CNY\n2026-01-01 open Equity:Opening CNY\n'
                             '2026-01-02 pad Assets:Cash Equity:Opening\n'
                             '2026-01-03 balance Assets:Cash 100 CNY\n'
                             '2026-02-01 open Income:Salary CNY\n'
                             '2026-02-02 * "salary"\n  Assets:Cash 20 CNY\n  Income:Salary -20 CNY\n', encoding='utf-8')

    def test_snapshot_pipeline_matches_official_booking_and_options(self):
        expected, errors, options = loader._uncached_load_file(str(self.root), None, None, None)
        self.assertEqual(errors, [])
        with patch('beancount_budget_tool.source_snapshot.parser.parse_string',
                   wraps=__import__('beancount.parser.parser', fromlist=['parse_string']).parse_string) as parse:
            actual = load_financial(self.root)
        self.assertEqual(list(actual.entries), expected)
        # DisplayContext has identity equality; compare its public precision
        # description while checking every other option directly.
        before, after = dict(options), dict(actual.options)
        self.assertEqual(str(before.pop('dcontext')), str(after.pop('dcontext')))
        self.assertEqual(after, before)
        self.assertEqual(parse.call_count, 1)

    def test_views_share_raw_identities_without_future_visibility(self):
        from beancount_budget_tool import refs
        with financial_session(), frozen_fact_indices(), patch.object(refs, '_fact', wraps=refs._fact) as hashed:
            with booked_through(date(2026, 2, 28)), frozen_financial(self.root) as february:
                full = FactIndex(february)
                salary = next(e for e in february.entries if isinstance(e, data.Transaction) and e.flag != 'P')
                reference = full.for_posting(salary.postings[0])
                count = hashed.call_count
            with booked_through(date(2026, 1, 31)), frozen_financial(self.root) as january:
                early = FactIndex(january)
                with self.assertRaises(BudgetError):
                    early.for_posting(salary.postings[0])
                self.assertEqual(hashed.call_count, count)
            with booked_through(date(2026, 2, 28)), frozen_financial(self.root):
                self.assertEqual(FactIndex(february).for_posting(salary.postings[0]), reference)

    def test_fact_tables_are_read_only_and_sources_do_not_need_rejoining(self):
        with frozen_financial(self.root) as fin, frozen_fact_indices():
            table = FactIndex(fin).table
            self.assertIs(FactIndex(fin).table, table)
            self.assertEqual(len(table.sources), len(table.rows))
            with self.assertRaises(TypeError):
                table.dates['injected'] = date(2026, 1, 1)

    def test_identity_values_and_mutations_cannot_collide(self):
        with identity_scope():
            for left, right in [({'x': True}, {'x': 1}), ({'x': (True,)}, {'x': (1,)}),
                                ({'x': None}, {'x': 'None'})]:
                self.assertNotEqual(canonical_key(left), canonical_key(right))
            value = {'路径': '账本', 'ordinal': 1}
            old = canonical_key(value)
            value['ordinal'] = 2
            self.assertNotEqual(old, canonical_key(value))
            self.assertEqual(canonical_key(value), json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')))

    def test_internal_projection_is_immutable_but_public_copy_is_mutable(self):
        shared = [{'value': 1}]
        original = {'left': shared, 'right': shared}
        original['cycle'] = original
        frozen = freeze(original)
        self.assertIsInstance(frozen, FrozenDict)
        self.assertIs(frozen['left'], frozen['right'])
        self.assertIs(frozen['cycle'], frozen)
        with self.assertRaises(TypeError):
            frozen['left'][0]['value'] = 2
        public = deepcopy(frozen)
        self.assertIs(type(public), dict)
        self.assertIs(type(public['left']), list)
        self.assertIs(public['cycle'], public)
        public['left'][0]['value'] = 2
        self.assertEqual(frozen['left'][0]['value'], 1)
        with self.assertRaises(TypeError):
            freeze({'opaque': object()})

    def test_generated_documents_never_escape_their_query_scope(self):
        directory = self.folder / 'not-created'
        path = directory / 'config.yaml'
        with document_scope():
            register_documents(directory, {'config.yaml': {'schema': 6, 'values': [1]}}, SetupDumper)
            self.assertFalse(path.exists())
            self.assertTrue(input_is_file(path))
            content = input_bytes(path)
            with self.assertRaises(TypeError):
                document(path).value['values'].append(999)
            self.assertEqual(input_bytes(path), content)
            value = load_yaml(path)
            value['values'].append(2)
            self.assertEqual(load_yaml(path)['values'], [1])
            self.assertIn(b'schema: 6', input_bytes(path))
            self.assertTrue(yaml_path_location(path, ('values',)).endswith(':2'))
        self.assertFalse(input_is_file(path))
        with self.assertRaises(BudgetError):
            load_yaml(path)

    def test_generated_document_bytes_match_materialized_yaml_for_both_line_endings(self):
        value = {'schema': 6, 'reason': '保留原批准理由', 'values': ['第一行\n第二行', 1]}
        rendered = yaml.dump(value, Dumper=SetupDumper, allow_unicode=True, sort_keys=False)
        for ending in ('\n', '\r\n'):
            with self.subTest(ending=repr(ending)), document_scope(), patch(
                    'beancount_budget_tool.runtime_inputs.os.linesep', ending):
                physical = self.folder / 'materialized.yaml'
                physical.write_text(rendered, encoding='utf-8', newline=ending)
                directory = self.folder / 'virtual'
                register_documents(directory, {'review.yaml': value}, SetupDumper)
                virtual = input_bytes(directory / 'review.yaml')
                self.assertEqual(virtual, physical.read_bytes())
                self.assertEqual(sha256(virtual).hexdigest(), sha256(physical.read_bytes()).hexdigest())

    def test_public_input_files_are_rechecked_after_internal_document_queries(self):
        path = self.folder / 'actual.yaml'
        path.write_text('value: 1\n', encoding='utf-8')
        with document_scope():
            self.assertEqual(load_yaml(path), {'value': 1})
            path.write_text('value: 2\n', encoding='utf-8')
            self.assertEqual(load_yaml(path), {'value': 2})

    def test_query_public_results_are_mutable_and_cannot_poison_later_queries(self):
        example = Path(__file__).resolve().parents[1] / 'examples/v6/simple'
        config = self.folder / 'config.yml'
        for source in example.iterdir():
            if source.is_file():
                shutil.copyfile(source, self.folder / source.name)
        with QuerySession() as query:
            first = query.month(config, include_transactions=True)
            original = deepcopy(first)
            first['report']['account_purposes']['rows'][0]['units'] = '999999'
            first['report']['transactions']['rows'].clear()
            self.assertEqual(query.month(config, include_transactions=True), original)

    def test_query_requires_an_active_scope_and_reports_the_public_config(self):
        query = QuerySession()
        with self.assertRaises(RuntimeError):
            query.month(self.root)
        example = Path(__file__).resolve().parents[1] / 'examples/v6/simple'
        for source in example.iterdir():
            if source.is_file():
                shutil.copyfile(source, self.folder / source.name)
        config = self.folder / 'config.yml'
        with query:
            result = query.period(config, '2026-09', '2026-09')
            self.assertEqual(result['months'][0]['config'], str(config))
        with self.assertRaises(RuntimeError):
            query.period(config)

    def test_query_entry_failure_cleans_up_financial_and_document_contexts(self):
        from contextlib import contextmanager
        from beancount_budget_tool import financial, runtime_inputs

        @contextmanager
        def fail():
            raise RuntimeError('injected fact-index setup failure')
            yield

        query = QuerySession()
        with patch('beancount_budget_tool.v6.facts.frozen_fact_indices', fail):
            with self.assertRaisesRegex(RuntimeError, 'fact-index setup failure'):
                query.__enter__()
        self.assertIsNone(financial._REPORT_INPUTS.get())
        self.assertIsNone(runtime_inputs._DOCUMENTS.get())
        self.assertIsNone(query._stack)
        with query:
            self.assertIsNotNone(financial._REPORT_INPUTS.get())

    def test_typed_reference_keys_preserve_the_existing_claim_serialization(self):
        with frozen_financial(self.root) as fin:
            facts = FactIndex(fin)
            for entry in fin.entries:
                if not isinstance(entry, data.Transaction):
                    continue
                references = [facts.for_transaction(entry), *(facts.for_posting(p) for p in entry.postings)]
                for reference in references:
                    self.assertEqual(reference.as_key(), json.dumps(reference.as_dict(), ensure_ascii=False,
                                                                   sort_keys=True, separators=(',', ':')))

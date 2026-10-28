"""Source preparation can be shared; approval and opening decisions cannot."""

from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import os
import unittest

import yaml
from beancount import __version__ as BEANCOUNT_VERSION

from beancount_budget_tool.errors import BudgetError
from beancount_budget_tool.financial import frozen_financial, load_financial
from beancount_budget_tool.query import QuerySession
from beancount_budget_tool.v6 import accounts, ownership


class SharedAuditInputTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.root = self.folder / 'main.bean'
        self.cutover = date(2023, 7, 1)
        self.root.write_text('option "operating_currency" "CNY"\n'
                            '2023-06-01 open Assets:Cash CNY\n'
                            '2023-06-01 open Assets:Bank CNY\n'
                            '2023-06-01 open Income:Pay CNY\n'
                            '2023-06-02 * "opening"\n  Assets:Cash 100 CNY\n  Income:Pay -100 CNY\n'
                            '2023-07-02 * "transfer"\n  Assets:Cash -20 CNY\n  Assets:Bank 20 CNY\n',
                            encoding='utf-8')
        self.accounts = self.folder / 'accounts.yaml'
        self.owners = self.folder / 'ownership.yaml'
        self.opening = self.folder / 'opening.yaml'
        self.write(self.accounts, {'schema': 6, 'cutover': '2023-07-01', 'report_currency': 'CNY',
                   'accounts': [{'account': account, 'semantics': 'H', 'balance_semantics': 'real_inventory',
                                 'payment_scope': {'CNY': 'payable'}, 'reason': 'virtual fixture'}
                                for account in ('Assets:Cash', 'Assets:Bank')]})
        self.write(self.owners, {'schema': 6, 'cutover': '2023-07-01',
                   'ledger_snapshot': load_financial(self.root).digest, 'loader_version': BEANCOUNT_VERSION,
                   'owners': [{'id': 'free', 'name': 'Free', 'aliases': []},
                              {'id': 'save', 'name': 'Saved', 'aliases': []}],
                   'claims': [], 'defaults': {'income_owner_id': 'free', 'expense_owner_id': 'free'}})
        self.write(self.opening, {'schema': 6, 'cutover': '2023-07-01',
                   'h_holdings': [{'account': 'Assets:Cash', 'commodity': 'CNY', 'lot': 'CNY/no-cost',
                                   'owner': 'free', 'units': '100', 'reason': 'virtual reviewed opening'}],
                   'k_openings': []})

    def write(self, path, value):
        path.write_text(yaml.safe_dump(value, sort_keys=False), encoding='utf-8')

    def audit(self, opening=None):
        return ownership.audit_ownership(self.root, self.cutover, self.accounts, self.owners, opening)

    def edit_same_stamp(self, path, before, after):
        original, stamp = path.read_bytes(), path.stat()
        changed = original.replace(before.encode(), after.encode())
        self.assertNotEqual(changed, original)
        self.assertEqual(len(changed), len(original))
        path.write_bytes(changed)
        os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))

    def test_opening_variants_share_preparation_but_keep_distinct_results(self):
        with QuerySession(), frozen_financial(self.root), \
                patch.object(ownership, 'scan_v6_source', wraps=ownership.scan_v6_source) as scan:
            absent = self.audit()
            reviewed = self.audit(self.opening)
            self.assertEqual(absent['review_state'], 'pending')
            self.assertEqual(len(absent['missing_h_ownership']), 2)
            self.assertEqual(reviewed['review_state'], 'complete')
            self.assertEqual(len(reviewed['ownership']), 2)
            self.assertEqual(self.audit(), absent)
            self.assertEqual(self.audit(self.opening), reviewed)
            self.assertEqual(scan.call_count, 1)

    def test_opening_edit_replays_without_reusing_the_previous_capacity(self):
        with QuerySession(), frozen_financial(self.root), \
                patch.object(ownership, 'scan_v6_source', wraps=ownership.scan_v6_source) as scan:
            before = self.audit(self.opening)
            self.edit_same_stamp(self.opening, 'owner: free', 'owner: save')
            after = self.audit(self.opening)
            self.assertEqual({part['owner_id'] for row in before['ownership'] for part in row['slices']}, {'free'})
            self.assertEqual({part['owner_id'] for row in after['ownership'] for part in row['slices']}, {'save'})
            self.assertEqual(self.audit()['review_state'], 'pending')
            self.assertEqual(scan.call_count, 1)

    def test_ownership_document_edit_cannot_reuse_previous_validation(self):
        with QuerySession(), frozen_financial(self.root):
            self.assertEqual(self.audit(self.opening)['review_state'], 'complete')
            self.edit_same_stamp(self.owners, BEANCOUNT_VERSION, '9' * len(BEANCOUNT_VERSION))
            with self.assertRaises(BudgetError) as caught:
                self.audit()
            self.assertEqual(caught.exception.code, 'OWNERSHIP_DECLARATION_INVALID')

    def test_account_overlay_does_not_annotate_the_shared_source_rows(self):
        with QuerySession(), frozen_financial(self.root), \
                patch.object(accounts, 'asset_opening_fingerprint', wraps=accounts.asset_opening_fingerprint) as fingerprint:
            raw = accounts.audit_accounts(self.root, self.cutover)
            approved = accounts.audit_accounts(self.root, self.cutover, self.accounts)
            self.assertTrue(all('semantics' not in row for row in raw['accounts']))
            self.assertTrue(all(row['semantics'] == 'H' for row in approved['accounts']))
            self.assertEqual(accounts.audit_accounts(self.root, self.cutover), raw)
            self.assertEqual(fingerprint.call_count, 1)

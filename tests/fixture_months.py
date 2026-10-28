"""Synthetic multi-month protocol inputs; no installed worksheet workflow."""
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory

import yaml

from beancount_budget_tool.financial import month_bounds
from beancount_budget_tool.v6.input_compiler import read_month_config
from beancount_budget_tool.v6.month_actions import confirm_month
from beancount_budget_tool.v6.monthly_report import audit_month_workflow, render_workflow_text
from beancount_budget_tool.v6.purpose_targets import load_purpose_targets
from beancount_budget_tool.v6.targets import load_expense_targets
from beancount_budget_tool.v6.tui import ReportBrowser
from fixture_inputs import SetupDumper


def save(path, document):
    path.write_text(yaml.dump(document, Dumper=SetupDumper, allow_unicode=True,
                              sort_keys=False), encoding='utf-8')


def create_next_month(config, output):
    config, output = Path(config).resolve(), Path(output)
    output.mkdir(parents=True)
    document, paths, _ = read_month_config(config)
    year, month = map(int, document['month'].split('-'))
    next_month = f'{year + (month == 12):04d}-{month % 12 + 1:02d}'
    classes = yaml.safe_load(paths['classifications'].read_text(encoding='utf-8'))['categories']
    categories = {row['id']: row['name'] for row in classes}
    owners = {row['id']: row['name'] for row in
              yaml.safe_load(paths['ownership'].read_text(encoding='utf-8'))['owners']}
    expense = load_expense_targets(paths['expense_plans'], document['month'], document['report_currency'], classes)
    purpose = load_purpose_targets(paths['purpose_plans'], document['month'], document['report_currency'], set(owners))
    sheet = {
        'config': str(config), 'month': next_month, 'targets_confirmed': False,
        'completeness_confirmed': False, 'completeness_reason': '',
        'expense_targets': {categories[key]: value for key, value in expense['targets'].items()},
        'expense_subtree_targets': {categories[key]: value for key, value in expense['subtree_targets'].items()},
        'allocation_targets': {owners[row['owner_id']]: row['target'] if row['status'] == 'TARGET' else 'not_assessed'
                               for row in purpose['owners']},
    }
    path = output / 'next-month.yaml'
    save(path, sheet)
    return {'worksheet': str(path), 'status': 'DRAFT_CREATED', 'month': next_month}


def compile_next_month(worksheet, output=None, **kwargs):
    worksheet = Path(worksheet)
    sheet = yaml.safe_load(worksheet.read_text(encoding='utf-8'))
    if any(value is None for field in ('expense_targets', 'expense_subtree_targets', 'allocation_targets')
           for value in sheet.get(field, {}).values()):
        return {'config': None, 'status': 'NEEDS_REVIEW',
                'review_items': [{'detail': '目标请填金额或 not_assessed', 'source': str(worksheet) + ':1'}]}
    document, paths, _ = read_month_config(Path(sheet['config']))
    output = Path(output or worksheet.parent / 'compiled')
    output.mkdir(parents=True)
    document.update({key: str(path) for key, path in paths.items()})
    document['month'] = sheet['month']
    categories = {row['name']: row['id'] for row in
                  yaml.safe_load(paths['classifications'].read_text(encoding='utf-8'))['categories']}
    owners = {row['name']: row['id'] for row in
              yaml.safe_load(paths['ownership'].read_text(encoding='utf-8'))['owners']}
    for key, fields, names in (
        ('expense_plans', ('expense_targets', 'expense_subtree_targets'), categories),
        ('purpose_plans', ('allocation_targets',), owners),
    ):
        plan = deepcopy(yaml.safe_load(paths[key].read_text(encoding='utf-8')))
        plan.setdefault('months', {})[sheet['month']] = {
            field: {names[name]: value for name, value in sheet.get(field, {}).items()} for field in fields}
        path = output / (key + '.yaml')
        save(path, plan)
        document[key] = str(path.resolve())
    if sheet['completeness_confirmed']:
        path = output / 'completeness.yaml'
        save(path, {'schema': 6, 'cutover': document['cutover'],
                    'as_of': month_bounds(sheet['month'])[1].isoformat(),
                    'unbooked_obligations': 'none', 'inflight_payments': 'none',
                    'reason': sheet['completeness_reason']})
        document['completeness'] = str(path.resolve())
    else:
        document.pop('completeness', None)
    path = output / 'month.yaml'
    save(path, document)
    return {'config': str(path), 'month': sheet['month'], 'status': 'MONTH_PREPARED',
            'workflow': audit_month_workflow(path, **{key: value for key, value in kwargs.items()
                                                    if key == 'include_transactions'})}


class ProtocolBrowser(ReportBrowser):
    """Test explicit source protocols independently of public automatic rules."""
    def __init__(self, config, month='2026-09'):
        self.workspace = TemporaryDirectory(prefix='protocol-test-')
        self.counter = 0
        self.month_documents = {}
        super().__init__([Path(config)], lambda path: audit_month_workflow(path, include_transactions=True),
                         render_workflow_text)
        if yaml.safe_load(self.config.read_text(encoding='utf-8'))['month'] != month:
            self.jump_month(month)

    def close(self):
        self.workspace.cleanup()

    def jump_month(self, month):
        current, paths, _ = read_month_config(self.config)
        # Preserve each month's evidence bytes while sharing new source claims.
        self.month_documents.setdefault(current['month'], {
            key: paths[key] for key in ('expense_plans', 'purpose_plans', 'completeness') if key in paths})
        self.counter += 1
        folder = Path(self.workspace.name) / str(self.counter)
        result = create_next_month(self.config, folder)
        sheet = yaml.safe_load(Path(result['worksheet']).read_text(encoding='utf-8'))
        sheet.update(month=month, targets_confirmed=True, completeness_confirmed=True,
                     completeness_reason='Public virtual fixture complete')
        save(Path(result['worksheet']), sheet)
        compiled = compile_next_month(Path(result['worksheet']))
        path = Path(compiled['config'])
        if month in self.month_documents:
            document = yaml.safe_load(path.read_text(encoding='utf-8'))
            document.update({key: str(value) for key, value in self.month_documents[month].items()})
            save(path, document)
        self.configs, self.index = [path], 0
        self.review_roots = [self.config.parent / 'tui-reviews']
        self.clear()

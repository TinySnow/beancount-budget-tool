"""Build synthetic inputs for the internal compiler, outside the installed app."""
from pathlib import Path

from beancount_budget_tool.yamlstrict import load_yaml
from beancount_budget_tool.v6.input_compiler import (
    SetupDumper, draft_inputs, compile_inputs, publish_inputs, relative_reference,
)


def create_setup(ledger, month, output, **kwargs):
    output = Path(output).resolve()
    document, issues = draft_inputs(ledger, month, **kwargs)
    document['ledger'] = relative_reference(Path(ledger).resolve(), output)
    publish_inputs(output, {'setup.yaml': document})
    return {'status': 'DRAFT_CREATED', 'setup': str(output / 'setup.yaml'),
            'source_issues': issues, 'accounts': len(document['accounts'])}


def compile_setup(setup, output=None):
    setup = Path(setup).resolve()
    output = Path(output or setup.parent / 'compiled').resolve()
    files, issues = compile_inputs(load_yaml(setup), setup, output)
    if issues:
        return {'status': 'NEEDS_REVIEW', 'setup': str(setup),
                'review_items': issues, 'config': None}
    publish_inputs(output, files)
    return {'status': 'CONFIG_CREATED', 'config': str(output / 'month.yaml'), 'review_items': []}


def write_explicit_inputs(config, reviewed):
    """Test setup: emulate the user editing the two public YAML files.

    This is not an application save/approval path and creates no hidden state.
    Low-level protocol fixtures can still test exact source-bound declarations.
    """
    import yaml
    from beancount_budget_tool.v6.simple_config import _split_declarations, _write, SCALARS
    from beancount_budget_tool.yamlstrict import config_path
    config, reviewed = Path(config), Path(reviewed)
    public = load_yaml(config)
    header = load_yaml(reviewed)
    documents = {key: load_yaml(config_path(reviewed, value, key))
                 for key, value in header.items() if key not in SCALARS}
    shared, monthly = _split_declarations(documents)
    public['declarations'] = shared
    budgets = config_path(config, public['budgets'], 'budgets')
    plans = load_yaml(budgets)
    layer = plans.setdefault('months', {}).setdefault(header['month'], {})
    layer.pop('completeness_confirmed', None)
    layer['declarations'] = monthly
    _write(config, public)
    _write(budgets, plans)

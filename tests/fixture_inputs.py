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

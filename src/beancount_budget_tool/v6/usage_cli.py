"""Read-only first-use guidance and separate monthly comparisons."""

import argparse
import json
from pathlib import Path
import sys

from ..console import configure_output
from ..errors import BudgetError


def _main(kind, argv=None):
    from .usage_tools import onboarding, trend, render_onboarding, render_trend
    configure_output()
    parser = argparse.ArgumentParser(description='只读首次使用指引' if kind=='guide' else '逐月已审原值与独立比较；待审月份不填零')
    parser.add_argument('--config', type=Path)
    parser.add_argument('--format', choices=['text', 'json', 'markdown'], default='text')
    if kind == 'guide':
        parser.add_argument('--ledger', type=Path)
        parser.add_argument('--month')
        parser.add_argument('--currency')
    else:
        parser.add_argument('--since', required=True)
        parser.add_argument('--through', required=True)
        parser.add_argument('--owner')
        parser.add_argument('--category')
    args = parser.parse_args(argv)
    if not args.config and not (kind=='guide' and args.ledger):
        from .input_compiler import default_config
        args.config = default_config()
        if args.config is None: parser.error('请提供 --config；首次使用也可 --ledger --month')
    try:
        if kind == 'guide':
            if args.month:
                from .targets import _month
                args.month = _month(args.month)
            result = onboarding(config=args.config, ledger=args.ledger, month=args.month, currency=args.currency)
            text = render_onboarding(result)
        else:
            result = trend(args.config, args.since, args.through, owner=args.owner, category=args.category)
            text = render_trend(result)
        print(json.dumps(result, ensure_ascii=False, indent=2) if args.format=='json' else
              '```text\n'+text+'\n```' if args.format=='markdown' else text)
        return 0
    except (BudgetError, OSError, UnicodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print('查询已取消；未输出部分结果。', file=sys.stderr)
        return 130


def guide_main(argv=None): return _main('guide', argv)
def trend_main(argv=None): return _main('trend', argv)

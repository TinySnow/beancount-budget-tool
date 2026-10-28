"""Read-only H purpose history strictly before a failed transaction.

This deliberately has no monthly certification gate. It reuses ownership and
holdings replay, and withholds history if earlier attribution is incomplete.
"""

from collections import defaultdict
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace

from beancount.core import data

from ..errors import BudgetError, require
from ..financial import financial_session, booked_through, frozen_financial, frozen_before, month_bounds
from ..yamlstrict import load_yaml, config_path
from .account_queries import _day, _history, _position, render_text as render_query
from .facts import FactIndex, frozen_fact_indices
from .holdings import audit_h_holdings
from .ownership import audit_ownership


def locate(fin, source):
    """Resolve an exact source line; never guess a transaction by its amount."""
    path, separator, line = (source or '').rpartition(':')
    require(separator and line.isdigit(), 'QUERY_TARGET_INVALID', '请提供失败交易的完整文件路径:行号', source)
    path = Path(path).resolve()
    matches = []
    for entry in fin.entries:
        if not isinstance(entry, data.Transaction):
            continue
        for obj in (entry, *entry.postings):
            meta = obj.meta or {}
            if meta.get('filename') == str(path) and meta.get('lineno') == int(line):
                matches.append((entry, obj))
    require(len(matches) == 1, 'QUERY_TARGET_INVALID', '该位置未唯一对应当前账本交易；请先按 r 重算再查流水', source)
    return matches[0]


def query(config, month, source, *, account=None, purpose=None, since=None, commodity=None, anchor=None):
    """Return diagnostic evidence, never a partial monthly report or approval."""
    config = Path(config).resolve()
    public = load_yaml(config)
    from .simple_config import is_simple
    require(is_simple(public) or public.get('month') == month, 'QUERY_MONTH_CONFIG_REQUIRED',
            '月份配置须对应诊断交易所在月；两文件 config.yml 可直接查询其他月份', str(config))
    cutover = _day(public['cutover'], 'cutover')
    root = config_path(config, public['ledger'], 'ledger')
    with financial_session(), booked_through(month_bounds(month)[1]), frozen_financial(root) as complete, frozen_fact_indices():
        entry, obj = locate(complete, source)
        require(cutover <= entry.date and entry.date.strftime('%Y-%m') == month,
                'QUERY_TARGET_INVALID', '失败交易须在所选月份及 cutover 之后', source)
        if anchor:
            require(anchor['ledger_snapshot'] == complete.digest and
                    anchor['transaction_ref'] == FactIndex(complete).for_transaction(entry).as_dict(),
                    'QUERY_SOURCE_CHANGED', '账本已更改；请按 r 重算，随后重新查流水', source)
        account = account or (anchor or {}).get('account') or getattr(obj, 'account', None)
        require(bool(account), 'QUERY_ACCOUNT_UNKNOWN', '请填写要查询的完整资产科目名', source)
        first = _day(since, '起始日') if since else cutover
        require(cutover <= first <= entry.date, 'QUERY_RANGE_INVALID', '起始日须在 cutover 与失败交易日期之间', source)
        with frozen_before(complete, entry) as fin:
            return _replay(config, public, month, root, cutover, entry, source, account, purpose, first, commodity, fin)


def _replay(config, public, month, root, cutover, entry, source, account, purpose, first, commodity, fin):
    from .simple_config import is_simple, build_runtime, _unchanged
    runtime = build_runtime(config, month) if is_simple(public) else None
    try:
        if runtime and runtime.pending:
            item = runtime.pending['review_items'][0]
            raise BudgetError('QUERY_CONFIG_PENDING',
                f"诊断交易之前仍有未解决的配置／来源问题：{item['reason_code']}：{item['detail']}", item['source'])
        compiled = runtime.config if runtime else config
        doc = load_yaml(compiled)
        paths = {key: config_path(compiled, doc[key], key) for key in
                 ('accounts', 'opening', 'ownership', 'purpose_pools', 'classifications')}
        inputs = {path: path.read_bytes() for path in (config, compiled, *paths.values())}
        declarations = load_yaml(paths['accounts'])
        require(any(row['account'] == account and row['semantics'] == 'H' for row in declarations['accounts']),
                'QUERY_ACCOUNT_UNSUPPORTED', '报错前诊断流水目前支持 H 真实资产账户', str(config))
        require(not any(account in pool['accounts'] for pool in load_yaml(paths['purpose_pools'])['pools']),
                'QUERY_ACCOUNT_UNSUPPORTED', '证券池须按保护本金协议查看，不能当作普通 H 用途流水', str(config))
        holdings = audit_h_holdings(root, cutover, entry.date, paths['accounts'], paths['opening'], paths['ownership'])
        rows = [row for row in holdings['positions'] if row['account'] == account]
        require(all(row['status'] == 'CERTIFIED' for row in rows), 'QUERY_ATTRIBUTION_PENDING',
                '该账户在失败交易之前已有未认领用途，不能生成确定余额；请先处理更早的 fund／期初问题', source)
        owners = load_yaml(paths['ownership'])
        q = SimpleNamespace(fin=fin, root=root, config=config, day=entry.date, cutover=cutover, paths=paths,
                            facts=FactIndex(fin), owner_doc=owners, pools={'pools': []}, positions=[],
                            names={row['id']: row['name'] for row in owners['owners']} | {'-': '预算外'})
        q.ownership = audit_ownership(root, cutover, paths['accounts'], paths['ownership'], paths['opening'])
        # Holdings prove the same pre-transaction ending vector that
        # the history renderer independently reconciles against.
        q.positions = [_position(q, (account, part['owner_id'], row['commodity'], 'H'), Fraction(part['units']))
                       for row in rows for part in row['owner_units']]
        # History merges lots within a commodity, as the normal query does.
        totals = defaultdict(Fraction)
        for row in q.positions:
            totals[(account, row['owner_id'], row['commodity'], 'H')] += Fraction(row['amount'])
        q.positions = [_position(q, key, amount) for key, amount in totals.items() if amount]
        history = _history(q, account, purpose, first, commodity, h_only=True)
        history['through_semantics'] = 'before_transaction'
        history['note'] = '余额按整笔交易更新；以上仅为所选 H 账户在失败交易前的用途流水。'
        _unchanged(inputs)
        if runtime:
            runtime.check()
        return {'mode': 'v6_diagnostic_history', 'status': 'DIAGNOSTIC_ONLY', 'report': None,
                'as_of': entry.date.isoformat(), 'before_source': source, 'ledger_snapshot': fin.digest,
                'history': history, 'note': '仅重放到失败交易之前；不含该笔及后续交易。不是已审月报，不签发最多能花。'}
    finally:
        if runtime:
            runtime.close()


def render_text(result):
    # Reuse the normal event/amount/source formatting without conferring its
    # certified-month status on this diagnostic envelope.
    lines = render_query({'report': result['history'], 'as_of': result['as_of'],
                          'approval_month': '', 'note': result['note']}).splitlines()
    lines[0] = '诊断流水（失败交易前；未签发）'
    lines.insert(2, '截止位置：' + result['before_source'] + '（该笔未计入）')
    lines[4] = lines[4].replace(' 日末', ' 失败交易前')
    lines = ['失败交易前余额：' if line == '期末：' else line for line in lines]
    return '\n'.join(lines)


def render_csv(result):
    from .account_queries import render_csv as render_query_csv
    import csv
    import io
    content = render_query_csv({'report': result['history'], 'as_of': result['as_of']})
    output = io.StringIO()
    reader = csv.DictReader(io.StringIO(content))
    writer = csv.DictWriter(output, fieldnames=reader.fieldnames)
    writer.writeheader()
    writer.writerow({'section': 'diagnostic_only', 'narration': result['note'], 'source': result['before_source']})
    writer.writerows(reader)
    return output.getvalue()

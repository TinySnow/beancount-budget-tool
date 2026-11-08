"""Read-only preflight, attribution evidence and explanations of signed numbers.

Diagnostics never approve a posting or issue a partial monthly report. All
amounts come from existing bounded audits; filters only select evidence rows.
"""

import argparse
from collections import Counter
from contextlib import contextmanager
import csv
from datetime import date
from fractions import Fraction
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace

from ..console import configure_output
from ..errors import BudgetError, require
from ..exact import amount_text
from ..financial import booked_through, frozen_financial, month_bounds
from ..query import QuerySession
from ..yamlstrict import config_path, load_yaml, yaml_path_location
from .review_diagnostics import review_item
from .simple_config import _documents, _grouped_accounts, _layer, _unchanged, build_runtime
from .targets import _month


NOTE = '只读诊断，不是月报签发或可花额度；筛选不改变审计状态，不代选用途、不修改账本。'
STATIC_SCOPE = ['两文件结构与默认声明', '输入路径和日期', '账户组重复与声明形状', '所选月份目标层结构']
REPLAY_SCOPE = ['截至所选月末的账户及来源', 'H归属与真实库存', 'H/K期初', 'K及负债/责任来源']


def _issue(error):
    return review_item(error.code, error.source, detail=error.message)


def _input_error(exc, config):
    return exc if isinstance(exc, BudgetError) else BudgetError('SIMPLE_INPUT_ERROR', str(exc), str(config))


def _header(config, month):
    inputs = {}
    doc, budgets, plans = _documents(config, inputs)
    selected = _month(month or doc['month'])
    try:
        cutover = date.fromisoformat(doc['cutover'])
    except ValueError as exc:
        raise BudgetError('CUTOVER_INVALID', 'cutover 须为 YYYY-MM-DD',
                          yaml_path_location(config, ('cutover',))) from exc
    first, last = month_bounds(selected)
    require(cutover.day == 1 and cutover <= first, 'CUTOVER_INVALID',
            'cutover 须为不晚于所选月份的自然月第一天', yaml_path_location(config, ('cutover',)))
    root = config_path(config, doc['ledger'], 'ledger')
    require(root.is_file(), 'SIMPLE_INPUT_ERROR', '根账本路径不存在', yaml_path_location(config, ('ledger',)))
    return SimpleNamespace(config=config, month=selected, inputs=inputs, doc=doc,
                           budgets=budgets, plans=plans, cutover=cutover, first=first, last=last, root=root)


def _static(q):
    accounts, _ = _grouped_accounts(q.doc, q.config, {})
    for name, settings in accounts.items():
        location = yaml_path_location(q.config, ('accounts', name))
        require(set(settings) <= {'semantics', 'payment_scope', 'opening'}, 'SIMPLE_ACCOUNT_INVALID',
                '账户只接受语义、支付范围和期初', location)
        require(settings.get('semantics') in (None, 'H', 'K'), 'SIMPLE_ACCOUNT_INVALID',
                '资产语义须为 H 或 K；未决定可留空待审', location)
        require(all(type(currency) is str and bool(currency) and value in ('payable', 'excluded', 'pending')
                    for currency, value in settings['payment_scope'].items()), 'SIMPLE_ACCOUNT_INVALID',
                '支付范围须为币种到 payable／excluded／pending 的映射', location)
        opening = settings.get('opening', {})
        require(type(opening) in (dict, list) and
                (all(type(row) is dict for row in opening) if type(opening) is list else
                 all(type(value) is dict for value in opening.values())), 'SIMPLE_ACCOUNT_INVALID',
                '期初须为币种用途映射或完整坐标列表', location)
    _layer(q.plans, q.month, q.budgets)
    _unchanged(q.inputs)


@contextmanager
def _context(config, month=None):
    q = _header(Path(config).resolve(), month)
    with QuerySession() as session:
        # Compile with the same complete source metadata as QuerySession.month.
        # The compiler bounds inventory internally, but needs actual later
        # currencies to distinguish future declarations from invented ones.
        q.runtime = build_runtime(q.config, q.month, materialize=False)
        try:
            with booked_through(q.last), frozen_financial(q.root) as fin:
                q.session, q.fin = session, fin
                if not q.runtime.pending:
                    q.compiled = load_yaml(q.runtime.config)
                    q.paths = {key: config_path(q.runtime.config, q.compiled[key], key)
                               for key in ('accounts', 'opening', 'ownership', 'purpose_pools', 'obligations')}
                yield q
                q.runtime.check()
                _unchanged(q.inputs)
        finally:
            q.runtime.close()


def _replay(q, *, evidence=False):
    from .opening import audit_opening
    from .ownership import audit_ownership
    from .review_queue import audit_review_queue
    if q.runtime.pending:
        return None, q.runtime.pending['review_items'], True
    queue = audit_review_queue(q.runtime.config, q.cutover, q.last)
    owned = audit_ownership(q.root, q.cutover, q.paths['accounts'], q.paths['ownership'],
                            q.paths['opening'], **({'include_evidence': True} if evidence else {}))
    opening = audit_opening(q.root, q.cutover, q.paths['accounts'], q.paths['opening'])
    items = list(queue['items'])
    for row in opening['missing_h_ownership'] + opening['missing_k_openings']:
        items.append(review_item('OPENING_UNREVIEWED', str(q.config),
            detail='期初坐标尚未完整认领；先核对 config.yml 的 opening。', **row))
    # The queue audits real negative inventory and event protocols. A queue
    # preflight stops early; do not describe the remaining checks as passed.
    return owned, items, bool(queue.get('preflight_only'))


def doctor(config, month=None, *, replay=True):
    config = Path(config).resolve()
    result = {'mode': 'v6_doctor', 'status': 'DIAGNOSTIC_ONLY', 'config': str(config),
              'month': month, 'report': None, 'note': NOTE,
              'checks': {'static': {'state': 'not_run', 'scope': STATIC_SCOPE, 'issues': []},
                         'replay': {'state': 'not_run', 'scope': REPLAY_SCOPE, 'issues': []}}}
    try:
        q = _header(config, month)
        result['month'] = q.month
        _static(q)
        result['checks']['static']['state'] = 'passed'
    except (BudgetError, OSError, UnicodeError) as exc:
        result['checks']['static'].update(state='error', issues=[_issue(_input_error(exc, config))])
        return result
    if not replay:
        result['note'] += ' 静态检查通过不等于库存、归属或责任已证明；重放审计未运行。'
        return result
    try:
        with _context(config, q.month) as context:
            owned, items, stopped = _replay(context)
            result['ledger_snapshot'] = context.fin.digest
            public = context.runtime.public_sources({'review_items': items})['review_items']
            result['checks']['replay'].update(state='pending' if items or stopped else 'passed',
                issues=public, complete_scope=not stopped,
                summary={'h_postings': owned['h_postings'], 'missing_h_ownership': len(owned['missing_h_ownership'])}
                        if owned else {})
        _unchanged(q.inputs)
    except (BudgetError, OSError, UnicodeError) as exc:
        result['checks']['replay'].update(state='error', complete_scope=False, issues=[_issue(_input_error(exc, config))])
    return result


def attribution(config, month=None, *, account=None, reason=None, source=None, purpose=None,
                amount=None, since=None, through=None, all_rows=False, text=None, commodity=None, lot=None):
    from .purpose_workspace import attribution_rows
    try:
        selected_amount = Fraction(amount) if amount is not None else None
        first = date.fromisoformat(since) if since else None
        last = date.fromisoformat(through) if through else None
    except (ValueError, ZeroDivisionError) as exc:
        raise BudgetError('DIAGNOSTIC_FILTER_INVALID', '日期须为 YYYY-MM-DD；金额须为精确数字') from exc
    with _context(config, month) as q:
        start, end = first or q.cutover, last or q.last
        require(q.cutover <= start <= end <= q.last, 'DIAGNOSTIC_FILTER_INVALID',
                '筛选日期须在 cutover 与所选月末之间')
        result = {'mode': 'v6_attribution_workspace', 'status': 'DIAGNOSTIC_ONLY', 'report': None,
                  'config': str(q.config), 'month': q.month, 'from': start.isoformat(), 'through': end.isoformat(),
                  'ledger_snapshot': q.fin.digest, 'note': NOTE, 'rows': [], 'review_items': [],
                  'summary': {}, 'filters': {'account': account, 'reason': reason, 'source': source,
                    'purpose': purpose, 'amount': amount, 'all_rows': all_rows, 'text': text,
                    'commodity': commodity, 'lot': lot}}
        try:
            owned, items, stopped = _replay(q, evidence=True)
            rows = attribution_rows(owned, items) if owned else []
            names = owned.get('owner_names', {}) if owned else {}
            result['owner_names'] = names
            result['evidence_available'] = owned is not None
            result['replay_state'] = 'pending' if items or stopped else 'passed'
            result['summary'] = {'h_postings': owned['h_postings'] if owned else None,
                'evidence_counts': dict(Counter(row['evidence'] for row in rows)),
                'pending_h_legs': sum(bool(row.get('reason_code')) or row['evidence']=='pending' for row in rows),
                'unknown_before_h_legs': sum(row.get('before_state') != 'complete' for row in rows),
                'review_items': len(items)}
            for row in rows:
                reasons = [row.get('reason_code'), *row.get('reason_codes', [])]
                if not all_rows and not any(reasons) and row['evidence'] != 'pending': continue
                if not start.isoformat() <= row.get('date', '') <= end.isoformat(): continue
                if account and account not in row['account']: continue
                if commodity and commodity != row['commodity']: continue
                if lot and lot != row['lot']: continue
                if reason and reason not in reasons: continue
                if source and source not in (row.get('source') or ''): continue
                if text and text.casefold() not in json.dumps(row, ensure_ascii=False).casefold() and not any(
                        text.casefold() in name.casefold() and ident in json.dumps(row, ensure_ascii=False)
                        for ident, name in names.items()): continue
                if selected_amount is not None and Fraction(row['signed_units']) != selected_amount: continue
                if purpose and not any(purpose == ident or purpose == names.get(ident) for ident in
                    set(row.get('before') or {}) | {part['owner_id'] for part in row['slices']}): continue
                result['rows'].append(row)
            result['summary']['shown_h_legs'] = len(result['rows'])
            result['review_items'] = items
        except BudgetError as exc:
            result.update(replay_state='error', error={'code': exc.code, 'message': exc.message, 'source': exc.source},
                          review_items=[_issue(exc)], evidence_available=False)
        return q.runtime.public_sources(result)


def explain(config, month=None, *, metric='cash', owner=None, category=None):
    from .purpose_workspace import explain_cash, explain_expense, explain_purpose
    from .payable_cash import audit_payable_cash
    require(metric in {'cash', 'expense', 'purpose', 'max'}, 'EXPLAIN_METRIC_INVALID',
            '可解释 cash、expense、purpose 或 max')
    require(owner is None or metric in {'cash', 'purpose'}, 'EXPLAIN_FILTER_INVALID',
            '--owner 仅用于现金或用途余额解释；消费分类请用 --category')
    require(category is None or metric == 'expense', 'EXPLAIN_FILTER_INVALID',
            '--category 仅用于消费解释')
    with _context(config, month) as q:
        from .monthly_report import audit_month_workflow
        # Reuse the runtime already compiled before the bounded replay.
        # Recompiling the public config here would lose metadata about real
        # later currencies and mistakenly reject their shared declarations.
        gate = q.runtime.pending or audit_month_workflow(q.runtime.config, include_transactions=True, native_only=True)
        gate = q.runtime.public_sources(gate)
        result = {'mode': 'v6_number_explanation', 'config': str(q.config), 'month': q.month,
                  'metric': metric, 'status': gate['status'], 'report': None,
                  'review_items': gate.get('review_items', []), 'explanation': None,
                  'note': '仅拆解同一份已审月报的原数值；未签发时不提供局部现金或额度。'}
        if gate.get('report') is None:
            return result
        report = gate['report']
        require(report['ledger_snapshot'] == q.fin.digest, 'EXPLAIN_SNAPSHOT_MISMATCH', '解释与月报来源不一致')
        result['ledger_snapshot'] = report['ledger_snapshot']
        result['owner_names'] = report['owner_names']
        result['report_currency'] = report['report_currency']
        if metric in {'cash', 'max'}:
            cash = audit_payable_cash(q.root, q.cutover, q.last, q.paths['accounts'], q.paths['opening'], q.paths['ownership'])
            require(cash['ledger_snapshot'] == report['ledger_snapshot'], 'EXPLAIN_SNAPSHOT_MISMATCH', '现金拆分来源不一致')
            detail = explain_cash(report, cash, owner_id=owner)
            if metric == 'max':
                require(owner is None, 'EXPLAIN_METRIC_INVALID', '最多能花只解释默认支出用途，不接受其他用途')
                detail = {'max_spendable': report['max_spendable'], 'cash': detail,
                          'expense_targets': report['expense_targets'], 'salary_cycle': report.get('salary_cycle')}
        elif metric == 'expense':
            detail = explain_expense(report, category_id=category, expense_root=q.fin.options['name_expenses'])
        else:
            require(owner is not None, 'EXPLAIN_OWNER_REQUIRED', '用途余额解释须指定 --owner 用途名称或ID')
            detail = explain_purpose(report, owner)
        result['explanation'] = detail
        return result


def _details(value, prefix=''):
    """Render copied evidence without manufacturing totals or rounded values."""
    lines = []
    for key, item in value.items() if isinstance(value, dict) else enumerate(value):
        if isinstance(item, (dict, list)):
            lines.append(f'{prefix}{key}:')
            lines.extend(_details(item, prefix+'  '))
        else:
            lines.append(f'{prefix}{key}: {item if item is not None else "未知／未提供"}')
    return lines


def _cash_text(detail):
    owner = detail['selected_owner']
    currency = detail['report_currency']
    lines = [f"{owner['name']}可支付现金：{detail['selected_available_cash'] if detail['selected_available_cash'] is not None else '未提供'} {currency}",
             '以下是可直接支付范围的用途库存（尚未扣责任和预留）：']
    for position in detail['inventory']['known_payable_positions']:
        for part in position['owner_units']:
            lines.append(f"  {position['account']} · {part['units']} {position['commodity']} · {position['lot']}")
    actual = detail['global_actual_spendable']
    lines.append('责任／预留核对（完整已审上下文；不表示其他用途并入所选现金）：')
    lines.append(f"  未覆盖责任：{actual.get('uncovered_responsibility')} {currency}；保护缺口：{actual.get('protected_shortfall')} {currency}")
    for row in actual.get('booked_debts') or []:
        lines.append(f"  已记负债：{row.get('account','')} · {row.get('remaining_native',row.get('remaining_units','见JSON'))} {row.get('commodity',currency)}")
    for kind, title in (('coverage_allocations', '债务覆盖'), ('obligation_coverage', '责任覆盖'), ('settlement_allocations', '清算预留')):
        rows = actual.get(kind) or []
        if rows:
            lines.append(title+'：')
            lines.extend(_details(rows, '  '))
    excluded = detail['inventory']['excluded_h_coordinates']
    if excluded:
        lines.append('以下账户／币种排除直接支付，不并入上方现金：')
        lines.extend(f"  {row['account']} · {row['commodity']} · {row['lot']}" for row in excluded)
    lines.append('用途库存与最终可支付现金分别来自原审计；不把其他用途、K账面或证券池当作现金，不重新分摊责任。')
    return lines


def _explanation_text(result):
    detail, metric = result['explanation'], result['metric']
    if metric == 'cash': return _cash_text(detail)
    if metric == 'max':
        limit = detail['max_spendable']
        return [f"最多能花：{limit.get('max_spendable') if limit.get('max_spendable') is not None else '未签发'} {result['report_currency']}",
                '取默认支出用途可支付现金与对应目标／发薪周期剩余额度的较小值；其他用途不并入。',
                f"对应额度依据：{'发薪周期' if limit.get('basis') == 'salary_cycle' else '自然月目标'}；类别：{limit.get('category_name',limit.get('category_id','未配置'))}",
                f"消费剩余额度：{limit.get('consumption_target_remaining',limit.get('target_remaining','未签发'))}",
                *_cash_text(detail['cash'])]
    if metric == 'expense':
        names = {r['id']:r['name'] for r in detail['categories']}
        lines = ['净消费解释 · '+(detail['category_name'] or '全部消费类别'), '原币分别列出，不折算、不混入同名收入：']
        for row in detail['native_expenses']:
            lines.append(f"  {names.get(row['category_id'],row['category_id'])} · 支出 {row.get('gross')}／退款 {row.get('refund')}／净额 {row['net']} {row['currency']}")
        lines.append('原报告目标及剩额（库存与现金不由此产生）：')
        for row in detail['categories']:
            if row['id'] in detail['scope_category_ids']:
                target = row.get('subtree_target')
                remaining = row.get('subtree_remaining')
                lines.append(f"  {row['name']} · 目标 {target if target is not None else '未考核'}／净消费 {row.get('subtree_spent')}／剩额 {remaining if remaining is not None else '未签发'}")
        lines.append('来源分录（只显示所选类别匹配切片；正数支出、负数退款）：')
        for transaction in detail['transactions']:
            for posting in transaction['postings']:
                for part in posting['selected_slices']:
                    lines.append(f"  {transaction['date']} · {names.get(part['category_id'],part['category_id'])} {part['signed_units']} {posting['commodity']} · {posting['account']}")
                    lines.append(f"    {posting.get('source',transaction.get('source',''))} · {transaction.get('narration','')}")
        return lines
    purpose = detail['purpose']
    lines = [f"{detail['owner_name']} · 期末用途余额（不等于可支付现金）"]
    for field, title in (('h_native_balances','H原币库存'), ('k_book_purpose_changes','K账面用途变动'),
                         ('pool_book_cost_balances','证券池账面用途成本')):
        lines.append(title+'：')
        for row in purpose[field]:
            lines.append(f"  {row.get('units',row.get('signed_change'))} {row['commodity']}")
            lines.extend(f"    {r['account']} · {r.get('units',r.get('signed_change'))} {row['commodity']}" for r in row.get('accounts', []))
        if not purpose[field]: lines.append('  无对应余额')
    progress = detail.get('purpose_progress')
    if progress:
        lines.append(f"本月目标与流量另列：目标Q {progress.get('target_q')}；实际划拨G {progress.get('actual_g')}（不是库存加项）")
    lines.append('H／K／证券池与不同币种分别核对，不相互加总，不表示市值或最多能花。')
    return lines


def render_text(result, *, columns=76):
    if result['mode'] == 'v6_doctor':
        labels = {'passed': '通过', 'pending': '待审', 'error': '错误', 'not_run': '未运行'}
        lines = [f"只读预检 · {result['month'] or ''}", result['note']]
        for key, title in (('static', '静态检查'), ('replay', '重放审计')):
            layer = result['checks'][key]
            lines += ['', f"{title}：{labels[layer['state']]}", '范围：'+'；'.join(layer['scope'])]
            for row in layer['issues']:
                lines += [f"{row['reason_code']} · {row.get('source') or ''}", row['detail']]
        lines.append('预检通过仍不证明月末完整性、预算目标或可花额度已签发。')
        return '\n'.join(lines)
    if result['mode'] == 'v6_number_explanation':
        lines = [f"数字解释 · {result['month']} · {result['metric']}", result['note']]
        if result['explanation'] is not None:
            lines.extend(_explanation_text(result))
        else:
            lines.append('当前未签发：不展示局部数字。')
            for row in result['review_items']:
                lines.append(f"{row['reason_code']} · {row.get('source') or ''} · {row['detail']}")
        return '\n'.join(lines)
    lines = [f"只读归属工作台 · {result['from']} 至 {result['through']}", result['note'],
             f"重放：{result['replay_state']}；筛选前统计：{json.dumps(result['summary'], ensure_ascii=False)}"]
    names = result.get('owner_names', {})
    origins = {'inline':'行内 fund', 'external':'外部精确认领', 'account_default':'账户级默认 fund', 'default':'既有默认规则',
               'protocol':'可证明协议／用途继承', 'pending':'归属待补'}
    for index, row in enumerate(result['rows'], 1):
        lines += ['', f"{index}. {row['date']} · {row['account']} · {row['signed_units']} {row['commodity']}",
                  f"来源：{row.get('source') or ''}", f"批次：{row['lot']}；依据：{origins.get(row['evidence'],row['evidence'])}",
                  f"交易：{row.get('payee') or ''} {row.get('narration') or ''}".rstrip(),
                  f"fund：{row.get('labels', {}).get('fund', '未写')}；budget：{row.get('budget_labels', [])}"]
        if row['before'] is None:
            lines.append('交易前用途余额：未知（前序归属未决或未重放），不按零处理。')
        else:
            lines.append('交易前用途余额：'+('已知为零' if not row['before'] else ''))
            lines.extend(f"  {names.get(owner, owner)}：{units} {row['commodity']}" for owner, units in row['before'].items())
        if row.get('review_reason'): lines.append('依据说明：'+row['review_reason'])
        if row.get('reason_code'): lines.append('待审原因：'+row['reason_code'])
        for review in row.get('review_items', []):
            if review.get('detail'): lines.append('  '+review['detail'])
        for part in row['slices']:
            lines.append(f"归属切片：{names.get(part['owner_id'],part['owner_id'])} {part['signed_units']} {row['commodity']}")
        if row.get('source_text'):
            lines.append('原交易：')
            lines.extend('  '+line for line in row['source_text'].splitlines())
    if not result.get('evidence_available', True):
        lines.append('当前配置或来源尚未完成，交易前证据未能生成；先核对以下问题。')
    elif not result['rows']:
        lines.append('没有匹配的H资产腿；筛选不改变待审状态。')
    if result.get('error') or not result.get('evidence_available', True):
        for row in result['review_items']:
            lines.append(f"待审：{row['reason_code']} · {row.get('source') or ''} · {row['detail']}")
    else:
        lines.append(f"未筛选待审共 {len(result['review_items'])} 项；完整清单保留在JSON或只读预检中。")
    return '\n'.join(lines)


def render_csv(result):
    require(result['mode'] == 'v6_attribution_workspace', 'DIAGNOSTIC_FORMAT_INVALID', 'CSV仅用于归属核对清单')
    output = io.StringIO()
    fields = ['scope', 'ledger_snapshot', 'review_items_total', 'date', 'account', 'commodity', 'lot', 'signed_units',
              'source', 'transaction_source', 'ref', 'evidence', 'before_state', 'before', 'labels', 'budget_labels',
              'slices', 'reason_code', 'reason_codes']
    writer = csv.DictWriter(output, fieldnames=fields, extrasaction='ignore')
    writer.writeheader()
    for row in result['rows']:
        writer.writerow({'scope': 'DIAGNOSTIC_ONLY', 'ledger_snapshot': result['ledger_snapshot'],
            'review_items_total': len(result['review_items']), **{k: json.dumps(row.get(k), ensure_ascii=False)
                if isinstance(row.get(k), (dict,list)) else row.get(k) for k in fields[3:]}})
    return output.getvalue()


def _main(kind, argv=None):
    configure_output()
    parser = argparse.ArgumentParser(description='只读预检／归属证据／数字解释；不自动修改输入')
    parser.add_argument('--config', type=Path)
    parser.add_argument('--month')
    parser.add_argument('--format', choices=('text', 'markdown', 'json', 'csv'), default='text')
    if kind == 'doctor': parser.add_argument('--static-only', action='store_true')
    if kind == 'attribution':
        for name in ('account','reason','source','purpose','amount','since','through','text','commodity','lot'):
            parser.add_argument('--'+name)
        parser.add_argument('--all', action='store_true', dest='all_rows')
    if kind == 'explain':
        parser.add_argument('metric', choices=('cash','expense','purpose','max'))
        parser.add_argument('--owner')
        parser.add_argument('--category')
    args = vars(parser.parse_args(argv))
    fmt, config = args.pop('format'), args.pop('config')
    try:
        if config is None:
            from .input_compiler import default_config
            config = default_config()
            require(config is not None, 'SIMPLE_CONFIG_REQUIRED', '请用 --config 指定两文件入口')
        if kind in {'doctor','lint'}:
            result = doctor(config, replay=kind=='doctor' and not args.pop('static_only', False), **args)
        else:
            result = (attribution if kind=='attribution' else explain)(config, **args)
        rendered = (json.dumps(result, ensure_ascii=False, indent=2) if fmt=='json' else
                    render_csv(result) if fmt=='csv' else render_text(result))
        if fmt == 'markdown':
            fence = '````' if '```' in rendered else '```'
            rendered = '# 只读诊断与数字解释\n\n'+fence+'text\n'+rendered+'\n'+fence
        print(rendered)
        return 2 if result.get('error') or any(layer['state']=='error' for layer in result.get('checks', {}).values()) else 0
    except (BudgetError, OSError, UnicodeError) as exc:
        error = exc if isinstance(exc, BudgetError) else BudgetError('SIMPLE_INPUT_ERROR', str(exc), str(config))
        print(json.dumps({'error': {'code': error.code, 'message': error.message, 'source': error.source}}, ensure_ascii=False), file=sys.stderr)
        return 2


def doctor_main(argv=None): return _main('doctor', argv)
def lint_main(argv=None): return _main('lint', argv)
def attribution_main(argv=None): return _main('attribution', argv)
def explain_main(argv=None): return _main('explain', argv)

"""Native income/expense facts shared by JSON, text/TUI and CSV."""

from collections import defaultdict
from fractions import Fraction

from ..exact import exact_text, amount_text
from ..text_table import table
from .classification import _key
from .facts import FactIndex


def _totals(rows):
    result = defaultdict(lambda: {key: Fraction(0) for key in ('gross', 'refund', 'net')})
    for row in rows:
        for key in ('gross', 'refund', 'net'):
            result[row['currency']][key] += Fraction(row[key])
    return [{'currency': currency, **{key: exact_text(value) for key, value in totals.items()}}
            for currency, totals in sorted(result.items())]


def spending_view(fin, first, last, classification, report_currency, names):
    dates = FactIndex(fin).table.dates
    groups = defaultdict(lambda: {key: Fraction(0) for key in ('gross', 'refund', 'net')})
    for row in classification['classifications']:
        if not first <= dates[_key(row['ref'])] <= last:
            continue
        expense = row['account'].split(':', 1)[0] == fin.options['name_expenses']
        role, sign = ('native_expenses', 1) if expense else ('native_income', -1)
        for part in row['slices']:
            amount = Fraction(part['signed_units']) * sign
            group = groups[role, row['commodity'], part['category_id']]
            group['gross' if amount >= 0 else 'refund'] += abs(amount)
            group['net'] += amount
    complete = not any(first <= dates[_key(row['ref'])] <= last
                       for row in classification['missing_classifications'])
    result = {'scope': 'booked_native_income_expense_only',
              'status': 'CERTIFIED_NATIVE_SCOPED' if complete else 'NEEDS_REVIEW',
              'from': first.isoformat(), 'through': last.isoformat(), 'report_currency': report_currency,
              'native_expenses': [], 'native_income': [],
              'note': f'{report_currency} 目标和最多能花仅涵盖原生 {report_currency} 消费；外币按自身币种分别报告，不计入该消费上限。'}
    for (role, currency, category), totals in sorted(groups.items()):
        result[role].append({'currency': currency, 'category_id': category,
            'category_name': names.get(category, '预算外' if category == '-' else category),
            **{key: exact_text(value) for key, value in totals.items()},
            **({'budget': None, 'remaining': None} if currency != report_currency else {})})
    result['native_totals'] = _totals(result['native_expenses'])
    result['native_income_totals'] = _totals(result['native_income'])
    result['unscoped_commodities'] = sorted({row['currency'] for role in ('native_expenses', 'native_income')
                                           for row in result[role] if row['currency'] != report_currency})
    return result


def combine(views, first=None, through=None):
    if not views:
        return None
    result = {key: views[0][key] for key in ('scope', 'report_currency', 'note')}
    result.update({'from': first or views[0]['from'], 'through': through or views[-1]['through'],
                   'status': 'CERTIFIED_NATIVE_SCOPED' if all(v['status'] == 'CERTIFIED_NATIVE_SCOPED' for v in views) else 'NEEDS_REVIEW'})
    for role in ('native_expenses', 'native_income'):
        groups = {}
        for view in views:
            for row in view[role]:
                key = row['currency'], row['category_id']
                if key not in groups:
                    groups[key] = {**row, **{metric: Fraction(0) for metric in ('gross', 'refund', 'net')}}
                for metric in ('gross', 'refund', 'net'):
                    groups[key][metric] += Fraction(row[metric])
        result[role] = [{**row, **{key: exact_text(row[key]) for key in ('gross', 'refund', 'net')}}
                        for _, row in sorted(groups.items())]
    result['native_totals'] = _totals(result['native_expenses'])
    result['native_income_totals'] = _totals(result['native_income'])
    result['unscoped_commodities'] = sorted({currency for view in views for currency in view['unscoped_commodities']})
    return result


def has_foreign(view):
    return bool(view) and bool(view['unscoped_commodities'])


def render_text(view, *, columns=76):
    if not has_foreign(view):
        return []
    lines = [f'多币种原币收支（{view["from"]} 至 {view["through"]}；各币种独立）']
    for role, label in (('native_expenses', '原币支出'), ('native_income', '原币收入')):
        if view[role]:
            lines.append(label)
            lines += table(['类别', '币种', '原额', '退回／冲回', '净额', '预算', '剩余'], [
                [row['category_name'], row['currency'], amount_text(row['gross']), amount_text(row['refund']),
                 amount_text(row['net']), '见主表' if row['currency'] == view['report_currency'] else '-',
                 '见主表' if row['currency'] == view['report_currency'] else '-']
                for row in view[role]], numeric=(2, 3, 4), columns=columns)
    lines.append(view['note'])
    return lines


def csv_records(view):
    for role in ('native_expenses', 'native_income'):
        for row in view[role]:
            for metric in ('gross', 'refund', 'net'):
                yield {'section': role, 'metric': metric, 'value': row[metric],
                       'id': row['category_id'], 'name': row['category_name'], 'commodity': row['currency'],
                       'status': view['status'], 'detail': view['note']}
            if row['currency'] != view['report_currency']:
                for metric in ('budget', 'remaining'):
                    yield {'section': role, 'metric': metric, 'value': None,
                           'id': row['category_id'], 'name': row['category_name'], 'commodity': row['currency'],
                           'status': 'NOT_ASSESSED', 'detail': '未设置独立原币预算；不使用报告币目标。'}

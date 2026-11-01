"""Display audited closing purpose balances without adding monthly stocks."""

from fractions import Fraction

from ..exact import exact_text, amount_text
from ..text_table import table


def nonzero(value):
    return value is not None and Fraction(value) != 0


def expense_active(row):
    return (row.get('direct_target') is not None or
        row.get('subtree_target_kind') == 'explicit' and row.get('subtree_target') is not None or
        any(nonzero(row.get(key)) for key in (
        'direct_gross_spent', 'direct_refunded', 'direct_spent',
        'subtree_gross_spent', 'subtree_refunded', 'subtree_spent',
        'direct_target', 'subtree_target')) or
        any(row.get(key) not in (None, 'CERTIFIED', 'NOT_ASSESSED')
            for key in ('direct_remaining_status', 'subtree_remaining_status')))


def balance_active(row):
    return bool(row.get('default_roles')) or any(
        nonzero(part.get('units', part.get('signed_change')))
        for key in ('h_native_balances', 'pool_book_cost_balances', 'k_book_purpose_changes')
        for part in row.get(key, [])) or row.get('has_negative_h_position') or row.get('has_negative_pool_position')


def active_owners(report):
    result = set(report.get('active_owner_ids', []))
    result.update(part['k_owner_id'] for event in report.get('k_flows', []) for part in event.get('slices') or [])
    result.update(row['owner_id'] for row in report.get('purpose_balances', {}).get('purposes') or []
                  if balance_active(row))
    result.update(row['owner_id'] for row in report.get('purpose_progress', {}).get('owners', [])
                  if nonzero(row.get('actual_g')) or row.get('target_q') is not None or
                  row.get('status') not in ('CERTIFIED', 'NOT_ASSESSED'))
    return result


def summarize(reports, first, through):
    """Cash comes from the last certified snapshot; only Q/G are summed."""
    last = reports[-1]
    currency = last['report_currency']
    actual = last['actual_spendable']
    default = last.get('default_outflows', {}).get('owner_id')
    available = {row['owner_id']: row['units'] for row in actual.get('purpose_available') or []}
    if default is not None:
        available[default] = actual['actual_spendable']
    balances = last['purpose_balances']
    purposes = list(balances['purposes']) + ([balances['outside_budget']] if balances['outside_budget'] else [])
    rows = []
    for purpose in purposes:
        owner = purpose['owner_id']
        coordinates = {currency}
        for key in ('h_native_balances', 'pool_book_cost_balances', 'k_book_purpose_changes'):
            coordinates.update(part['commodity'] for part in purpose[key])
        history = [row for report in reports for row in report['purpose_progress']['owners']
                   if row['owner_id'] == owner]
        targets_known = len(history) == len(reports) and all(row.get('target_q') is not None for row in history)
        def total(key, metric, commodity):
            return exact_text(sum((Fraction(part[metric]) for part in purpose[key]
                                   if part['commodity'] == commodity), Fraction(0)))
        for commodity in sorted(coordinates):
            report_units = commodity == currency
            rows.append({'owner_id': owner, 'name': purpose['name'], 'commodity': commodity,
                         'default_roles': purpose['default_roles'],
                         'h_units': total('h_native_balances', 'units', commodity),
                         'available_cash': (available.get(owner, '0') if owner != '-' and
                                            actual['status'] == 'CERTIFIED' else None) if report_units else None,
                         'pool_book_cost': total('pool_book_cost_balances', 'units', commodity),
                         'k_book_change': total('k_book_purpose_changes', 'signed_change', commodity),
                         'actual_g': exact_text(sum((Fraction(row['actual_g']) for row in history), Fraction(0)))
                         if report_units else None,
                         'target_q': exact_text(sum((Fraction(row['target_q']) for row in history), Fraction(0)))
                         if report_units and targets_known else None})
    return {'from': first, 'through': through, 'as_of': last['as_of'], 'report_currency': currency,
            'purposes': rows,
            'note': '库存和可支付余额只取期末，包含原期初及历史增减，不累加月末快照；Q/G只是所选期间划拨流量，不是余额。K账面变动、证券池成本及不同币种单列，不冒充现金或市值。'}


def render_summary(summary, *, columns=76, show_all=False):
    rows = [row for row in summary['purposes'] if show_all or row['default_roles'] or any(
        nonzero(row.get(key)) for key in ('h_units', 'available_cash', 'pool_book_cost', 'k_book_change', 'actual_g', 'target_q'))]
    fmt = lambda value: '-' if value is None else amount_text(value)
    lines = [f"累计用途余额（截至 {summary['as_of']}；跨月、跨年沿用）",
             '先看用途可支付余额；H库存可能包含不可直接支付的资产，不能直接当作消费上限。']
    lines += table(['用途', '币种', '可支付余额', 'H库存'],
                   [[row['name'], row['commodity'], fmt(row['available_cash']), fmt(row['h_units'])]
                    for row in rows], numeric=(2, 3), columns=columns)
    books = [row for row in rows if nonzero(row['k_book_change']) or nonzero(row['pool_book_cost'])]
    if books:
        lines += ['', '账面口径（不计入可支付余额；不是市值）']
        lines += table(['用途', '币种', 'K申赎净变动', '证券池成本'],
                       [[row['name'], row['commodity'], fmt(row['k_book_change']), fmt(row['pool_book_cost'])]
                        for row in books], numeric=(2, 3), columns=columns)
    flows = [row for row in rows if nonzero(row['actual_g']) or nonzero(row['target_q'])]
    if flows:
        lines += ['', f"所选期间划拨流量（{summary['from']} 至 {summary['through']}；不与上表相加）"]
        lines += table(['用途', '币种', '累计目标Q', '累计实际G'],
                       [[row['name'], row['commodity'], fmt(row['target_q']), fmt(row['actual_g'])]
                        for row in flows], numeric=(2, 3), columns=columns)
    lines += [summary['note']]
    return lines


def render_cumulative(outcome, *, columns=76, show_all=False):
    if outcome.get('report') is None:
        from .period_report import render_text
        return render_text(outcome)
    report = outcome['report']
    lines = [f"v6 累计用途 {outcome['from']} 至 {outcome['through']}：{outcome['status']}"]
    lines += render_summary(report['cumulative'], columns=columns, show_all=show_all)
    lines += [f"{row['month']} {row['detail']}" for row in report['warnings']]
    return '\n'.join(lines)

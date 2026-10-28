"""Versioned responsibility vectors for liability posting budgets.

The ordinary account engine proves whole transaction shapes. This projection
splits only their responsibilities; it never rewrites booked postings, cash,
consumption or income. Positive credit in one purpose cannot cancel another
purpose's debt, even if the accounting account nets to zero.
"""

from collections import defaultdict
from fractions import Fraction
from hashlib import sha256

from beancount.core import data

from ..exact import exact, exact_text, amount_text
from ..refs import _lot
from .classification import _key
from .liability_rules import liability_budget, certify_credit_income_receipt


def apply_budget_responsibilities(fin, facts, cutover, rules, owners, result):
    events = {_key(row['liability_ref']): row for row in result['events']}
    balances, refs = defaultdict(dict), {}
    names = {owner: name for name, owner in owners.items()}
    parts_by_posting, unknown = {}, set()
    before = result['before_entry']
    # The caller has already bounded entries by as_of / before_ref. Historical
    # balances use the declared cutover default, not historical consumption.
    visible = set(events)
    output = []
    def identity(coordinate, owner, rule):
        return 'budget_' + sha256(_key({'coordinate': coordinate, 'owner': owner, 'role': rule['role'],
                                       'due_on': rule.get('due_on')}).encode()).hexdigest()
    for entry in fin.entries:
        if not isinstance(entry, data.Transaction) or entry is before:
            if entry is before:
                break
            continue
        managed = [p for p in entry.postings if p.account in rules]
        if entry.date >= cutover and not any(facts.for_posting(p).as_key() in visible for p in managed):
            continue
        group = []
        for posting in managed:
            ref = facts.for_posting(posting).as_dict()
            key = _key(ref)
            coordinate = (posting.account, posting.units.currency, _lot(posting))
            rule, amount = rules[posting.account], exact(posting.units.number)
            current = balances[coordinate]
            if entry.date < cutover:
                owner = rule['owner_id']
                current[owner] = current.get(owner, Fraction(0)) + amount
                parts_by_posting[id(posting)] = [(owner, amount)]
                refs[coordinate, owner] = ref
                continue
            event = dict(events[key])
            for field in ('receipt_protocol', 'receipt_credit_native', 'receipt_income_native'):
                event.pop(field, None)
            flow = event['shape']
            labels = liability_budget(posting, owners)
            meaningful = [part for part in labels or [] if part['name'] != '-']
            parts = None
            if meaningful:
                # Ignoring a budget slice never erases its financial debt.
                parts = [(owners[part['name']] if part['name'] != '-' else rule['owner_id'],
                          Fraction(part['signed_units'])) for part in labels]
            elif coordinate not in unknown:
                # A missing label can inherit a unique source, or clear every
                # outstanding slice exactly. Never pick FIFO/proportional debt.
                opposite = {o: -v for o, v in current.items() if v * amount < 0}
                if len(opposite) == 1:
                    parts = [(next(iter(opposite)), amount)]
                elif opposite and sum(opposite.values(), Fraction(0)) == amount:
                    parts = list(opposite.items())
                elif not opposite:
                    parts = [(rule['owner_id'], amount)]
            event.update(proof='configured_liability_account_v1',
                         responsibility_protocol='posting_liability_budget_v1',
                         review_reason='逐笔负债 budget 声明责任用途；省略时只继承唯一来源、完整清偿或配置默认用途；不增加消费')
            if parts is None or coordinate in unknown:
                event.update(status='NEEDS_REVIEW', reason_codes=['LIABILITY_BUDGET_AMBIGUOUS'],
                             owner_id=None, obligation_id=None, clearing_id=None,
                             responsibility_slices=[])
                available = '；'.join(f"{names[owner]}：{'欠款' if units < 0 else '预付／应收'} "
                                      f"{amount_text(abs(units))} {coordinate[1]}"
                                      for owner, units in sorted(current.items()) if units)
                event['detail'] = ('本条责任用途无法唯一确定；请在该 Liabilities 分录填写 budget 及金额。'
                                   + (' 交易前：' + available + '。' if available else '')
                                   + ' 此前存在未定用途时，先补齐前笔标签再重算。')
                unknown.add(coordinate)
                parts_by_posting[id(posting)] = []
                output.append(event)
                continue
            vector = defaultdict(Fraction)
            for owner, units in parts:
                vector[owner] += units
            parts_by_posting[id(posting)] = list(vector.items())
            event['responsibility_slices'] = [{'owner_id': o, 'signed_units': exact_text(v)}
                                              for o, v in sorted(vector.items())]
            event['owner_id'] = next(iter(vector)) if len(vector) == 1 else None
            event['obligation_id'] = event['clearing_id'] = None
            identities = [identity(coordinate, owner, rule) for owner in sorted(vector)]
            field = 'clearing_id' if rule['role'] == 'receivable' else 'obligation_id'
            if len(identities) == 1:
                event[field] = identities[0]
            else:
                event[field + 's'] = identities
            # Re-evaluate credit returns per purpose, not by the account net.
            kind = {'receipt': 'advance_reimbursement' if rule['role'] == 'receivable' else 'payable_receipt',
                    'payment': 'advance_outflow' if rule['role'] == 'receivable' else 'payment'}.get(flow, flow)
            returning = {o: -v for o, v in vector.items() if v < 0 and current.get(o, 0) > 0}
            if flow == 'receipt' and rule['role'] != 'receivable' and returning:
                kind = 'credit_return'
                if len(returning) != len([v for v in vector.values() if v]) or any(
                        units > current[o] for o, units in returning.items()):
                    event.update(status='UNSUPPORTED', reason_codes=['LIABILITY_CREDIT_MIXED_UNSUPPORTED'])
                else:
                    event.update(status='CERTIFIED', reason_codes=[], credit_return_native=exact_text(-amount))
            elif flow != 'unsupported':
                event.update(status='CERTIFIED', reason_codes=[])
                event.pop('credit_return_native', None)
            event['kind'] = kind
            for owner, units in vector.items():
                current[owner] = current.get(owner, Fraction(0)) + units
                refs[coordinate, owner] = ref
            group.append(event)
            output.append(event)
            result['rules_by_ref'][key] = {**rule, 'owner_id': event['owner_id']}
        # Debt transfers must preserve every purpose, even with different
        # account defaults. Labels cannot quietly reassign debt responsibility.
        if group and group[0]['shape'] == 'refinance':
            changes = defaultdict(Fraction)
            for event in group:
                for part in event.get('responsibility_slices', []):
                    changes[part['owner_id']] += Fraction(part['signed_units'])
            if any(changes.values()):
                for event in group:
                    event.update(status='NEEDS_REVIEW', reason_codes=['LIABILITY_BUDGET_TRANSFER_MISMATCH'])
        kinds = {event['kind'] for event in group if Fraction(event['signed_units'])}
        if 'credit_return' in kinds and (len(kinds) > 1 or any(
                p.account.split(':', 1)[0] == fin.options['name_income'] and exact(p.units.number)
                for p in entry.postings)):
            if not certify_credit_income_receipt(fin, entry, group):
                for event in group:
                    event.update(status='UNSUPPORTED', reason_codes=['LIABILITY_CREDIT_MIXED_UNSUPPORTED'])
    debts, credits, pending = [], [], []
    for coordinate, vector in sorted(balances.items()):
        account, currency, lot = coordinate
        rule = rules[account]
        for owner, balance in sorted(vector.items()):
            ident = identity(coordinate, owner, rule)
            common = {'account': account, 'commodity': currency, 'lot': lot, 'owner_id': owner,
                      'origin': 'account_rule', 'account_role': rule['role'],
                      'responsibility_protocol': 'posting_liability_budget_v1'}
            if balance < 0:
                debts.append({**common, 'obligation_id': ident, 'remaining_native': exact_text(-balance),
                              'purchase_ref': refs[coordinate, owner], 'purchase_date': None,
                              **({'debt_kind': 'nonconsumer', 'due_on': rule['due_on']} if rule['role'] == 'loan' else {})})
            elif balance > 0:
                credits.append({**common, 'clearing_id': ident, 'remaining_native': exact_text(balance),
                                'outflow_ref': refs[coordinate, owner]})
            if rule['role'] == 'receivable' and balance < 0 or lot != currency + '/no-cost':
                pending.append({**common, 'signed_booked_units': exact_text(balance),
                                'reason_code': 'LIABILITY_RULE_BALANCE_INVALID'})
    return {**result, 'events': output, 'obligations': debts, 'clearing_advances': credits,
            'opening_pending': pending, 'responsibility_parts': parts_by_posting}

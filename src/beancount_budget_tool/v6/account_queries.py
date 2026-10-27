"""Read-only native purpose snapshots and source-bound account histories.

A freshly certified parent month authorizes the view. The query then rebuilds
under its own day-end cutoff: later transactions/settlements never enter the
numbers. This view does not issue a daily spending limit or market valuation.
"""

import argparse
from collections import defaultdict
import csv
from datetime import date
from fractions import Fraction
import io
import json
from pathlib import Path
from types import SimpleNamespace

from beancount.core import data

from ..console import configure_output
from ..errors import BudgetError, require
from ..exact import exact_text, amount_text
from ..financial import financial_session, booked_through, frozen_financial, month_bounds
from ..yamlstrict import load_yaml, config_path
from tempfile import TemporaryDirectory
from .classification import _key, audit_classification
from .inline_amounts import parse_inline_amounts
from .facts import FactIndex, frozen_fact_indices
from .holdings import audit_h_holdings
from .k_book_purposes import audit_k_book_purposes
from .k_events import audit_k_events
from .monthly_report import audit_month_workflow, summarize_account_purposes, REQUIRED, OPTIONAL
from .obligations import audit_simple_obligations
from .ownership import audit_ownership
from .purpose_balances import summarize_purpose_balances
from .purpose_pools import audit_purpose_pools, allocate_nominal, _book_weight
from .review_diagnostics import review_item, posting_sources


BASES = {'H': 'H 真实原币用途余额', 'K': 'K 切换后申赎用途净变动',
         'POOL': '证券池名义用途／账面成本', 'LIABILITY': '负债用途符号余额（负数欠款，正数预付／应收）',
         'EXPENSE': '切换后累计净费用（原币）', 'INCOME': '切换后累计净收入（原币）'}


def _day(value, field):
    try:
        result = date.fromisoformat(value)
        require(value == result.isoformat(), 'QUERY_DATE_INVALID', f'{field} 须为 YYYY-MM-DD')
        return result
    except (TypeError, ValueError) as exc:
        raise BudgetError('QUERY_DATE_INVALID', f'{field} 须为有效 YYYY-MM-DD') from exc


def _position(q, key, amount):
    account, owner, currency, basis = key
    return {'account': account, 'owner_id': owner, 'purpose': q.names.get(owner, '未分预算用途的旧清算'),
            'commodity': currency, 'basis': basis, 'amount': exact_text(amount)}


def _liability_vectors(q):
    owners = {}
    for event in q.debt['events']:
        if event.get('owner_id'):
            for field in ('obligation_id', 'clearing_id'):
                if event.get(field): owners[event[field]] = event['owner_id']
    for row in q.debt['obligations']:
        owners[row['obligation_id']] = row['owner_id']
    for row in q.debt['opening_declarations']:
        if row.get('owner_id') and row.get('obligation_id'):
            owners[row['obligation_id']] = row['owner_id']
    rules = load_yaml(q.paths['obligations']).get('account_rules', {})
    def owner(row):
        return row.get('owner_id') or owners.get(row.get('clearing_id')) or rules.get(row['account'], {}).get('owner_id')
    closing = defaultdict(Fraction)
    for rows, sign in ((q.debt['obligations'], -1), (q.debt['clearing_advances'], 1)):
        for row in rows:
            closing[(row['account'], owner(row), row['commodity'], 'LIABILITY')] += sign * Fraction(row['remaining_native'])
    deltas = {}
    for event in q.debt['events']:
        slices = event.get('responsibility_slices')
        if slices is None:
            ident = event.get('owner_id') or owners.get(event.get('obligation_id')) or owners.get(event.get('clearing_id'))
            slices = [{'owner_id': ident, 'signed_units': event['signed_units']}]
        deltas[_key(event['liability_ref'])] = slices
    return closing, deltas


def _read(q):
    q.facts = FactIndex(q.fin)
    q.owner_doc = load_yaml(q.paths['ownership'])
    q.names = {row['id']: row['name'] for row in q.owner_doc['owners']} | {'-': '预算外'}
    q.holdings = audit_h_holdings(q.root, q.cutover, q.day, q.paths['accounts'], q.paths['opening'], q.paths['ownership'])
    q.ownership = audit_ownership(q.root, q.cutover, q.paths['accounts'], q.paths['ownership'], q.paths['opening'])
    q.pools = audit_purpose_pools(q.root, q.cutover, q.day, q.paths['purpose_pools'])
    q.k = audit_k_book_purposes(q.root, q.cutover, q.day, q.paths['accounts'], q.paths['opening'], q.paths['ownership'], q.paths['k_events'])
    q.debt = audit_simple_obligations(q.root, q.cutover, q.paths['obligations'], q.day)
    h = summarize_account_purposes(q.holdings, q.pools)
    summary = summarize_purpose_balances(h, q.pools, q.k, q.owner_doc)
    if summary['status'] != 'CERTIFIED' or q.debt['review_state'] != 'complete':
        items = list(q.k.get('review_items', []))
        sources = posting_sources(q.fin)
        for event in q.debt['events']:
            if event['status'] != 'CERTIFIED':
                items.append(review_item(event['reason_codes'][0], sources.get(_key(event['liability_ref'])),
                                         account=event['account'], ref=event['liability_ref']))
        if not items:
            items.append(review_item('QUERY_ATTRIBUTION_PENDING', str(q.config),
                detail='该日日末的用途证据尚未完整；不能把月末已知余额倒填到当天。'))
        return None, items
    positions = []
    for row in h['rows']:
        positions.append(_position(q, (row['account'], row['owner_id'], row['commodity'], 'H'), Fraction(row['units'])))
    for pool in q.pools['pools']:
        for row in pool['account_purposes']:
            for owner_field, amount_field in (('protected_owner_id','protected_nominal'), ('remainder_owner_id','remainder_book_cost')):
                if Fraction(row[amount_field]):
                    positions.append(_position(q, (row['account'], pool[owner_field], q.pools['report_currency'], 'POOL'), Fraction(row[amount_field])))
    for row in q.k['rows']:
        for part in row['purpose_changes']:
            positions.append(_position(q, (row['account'], part['owner_id'], row['commodity'], 'K'), Fraction(part['signed_change'])))
    q.liability_closing, q.liability_deltas = _liability_vectors(q)
    positions.extend(_position(q, key, amount) for key, amount in q.liability_closing.items() if amount)
    # Native history merges lots only within the same account, purpose and
    # commodity. Different commodities and accounting bases remain separate.
    totals = defaultdict(Fraction)
    for row in positions:
        totals[(row['account'], row['owner_id'], row['commodity'], row['basis'])] += Fraction(row['amount'])
    positions = [_position(q, key, value) for key, value in totals.items() if value]
    q.positions = positions
    q.accounts = sorted({p.account for e in q.fin.entries if isinstance(e, data.Transaction) for p in e.postings} |
                        {e.account for e in q.fin.entries if isinstance(e, data.Open)})
    return {'positions': positions, 'purpose_balances': summary, 'accounts': q.accounts,
            'purposes': sorted(q.names.values()), 'k_book_accounts': q.k['rows'],
            'zero_value_records': q.k.get('zero_value_records', [])}, []


def query(config: Path, as_of: str, *, account=None, purpose=None, since=None, commodity=None):
    """Keep the month gate and daily replay on one verified source session."""
    with financial_session():
        return _query(config, as_of, account=account, purpose=purpose, since=since, commodity=commodity)


def _query(config: Path, as_of: str, *, account=None, purpose=None, since=None, commodity=None):
    """One parent-month gate, then a strictly bounded native replay."""
    from .simple_config import is_simple, build_runtime, _unchanged
    config = Path(config).resolve()
    day = _day(as_of, '截止日')
    public = load_yaml(config)
    require(type(public) is dict and type(public.get('cutover')) is str, 'QUERY_CONFIG_INVALID',
            '请提供含 cutover 的 config.yml 或月份配置', str(config))
    require(account is None or type(account) is str and bool(account) and account==account.strip(),
            'QUERY_ACCOUNT_UNKNOWN', '请填写完整会计科目名', str(config))
    simple = is_simple(public)
    cutover = _day(public['cutover'], 'cutover')
    require(day >= cutover, 'QUERY_BEFORE_CUTOVER', '查询日期不得早于 cutover', str(config))
    first = _day(since, '起始日') if since else cutover
    require(cutover <= first <= day, 'QUERY_RANGE_INVALID', '历史范围须为 cutover ≤ 起始日 ≤ 截止日', str(config))
    require(simple or public.get('month') == as_of[:7], 'QUERY_MONTH_CONFIG_REQUIRED',
            '高级配置须对应查询日期所在月；两文件 config.yml 可直接查询其他月份', str(config))
    envelope = {'mode': 'v6_account_history' if account else 'v6_day_purpose_snapshot',
                'as_of': as_of, 'cutover': cutover.isoformat(), 'config': str(config),
                'approval_month': as_of[:7], 'report': None}
    root = config_path(config, public['ledger'], 'ledger')
    with booked_through(month_bounds(as_of[:7])[1]), frozen_financial(root), frozen_fact_indices():
        runtime = build_runtime(config, as_of[:7]) if simple else None
        try:
            if runtime and runtime.pending:
                return {**envelope, 'status': runtime.pending['status'], 'review_items': runtime.pending['review_items']}
            compiled = runtime.config if runtime else config
            doc = load_yaml(compiled)
            sidecars = (REQUIRED | OPTIONAL) - {'schema','ledger','cutover','month','report_currency'}
            watched = [compiled, *[config_path(compiled, doc[key], key) for key in sidecars if key in doc]]
            inputs = {path: path.read_bytes() for path in watched}
            gate = audit_month_workflow(compiled)
            if runtime:
                gate = runtime.public_sources(gate)
            if gate['status'] != 'CERTIFIED_SCOPED':
                return {**envelope, 'status': gate['status'], 'review_items': gate['review_items'],
                        'note': '先完成查询日期所在月份的审核；本次只显示待审原因，不出历史余额或日末分布。'}
            _unchanged(inputs)
            with booked_through(day), frozen_financial(root) as fin, frozen_fact_indices():
                q = SimpleNamespace(fin=fin, root=root, config=config, day=day, cutover=cutover,
                                    paths={key: config_path(compiled, doc[key], key) for key in
                                           ('accounts','opening','ownership','purpose_pools','k_events','obligations','classifications')})
                # The month compiler acknowledges zero opening coordinates of K
                # accounts first used later that month. They are not yet active
                # in this daily replay. The month gate has validated them all.
                opening = load_yaml(q.paths['opening'])
                active = {p.account for e in fin.entries if isinstance(e, data.Transaction) for p in e.postings}
                opening['k_openings'] = [r for r in opening['k_openings']
                                         if r['account'] in active or Fraction(r['signed_units'])]
                with TemporaryDirectory(prefix="budget-query-") as temporary:
                    import yaml
                    path = Path(temporary) / 'day-opening.yaml'
                    path.write_text(yaml.safe_dump(opening, allow_unicode=True), encoding='utf-8')
                    q.paths['opening'] = path
                    report, items = _read(q)
                    if report is not None and account:
                        require(account in q.accounts, 'QUERY_ACCOUNT_UNKNOWN', '该日日末未找到此完整会计科目名', str(config))
                        report = _history(q, account, purpose, first, commodity)
                _unchanged(inputs)
                if runtime: runtime.check()
                return {**envelope, 'status': 'CERTIFIED_SCOPED' if report is not None else 'NEEDS_REVIEW',
                        'ledger_snapshot': fin.digest, 'parent_month_snapshot': gate['report']['ledger_snapshot'],
                        'review_items': items, 'report': report,
                        'note': '仅查询账本该日日末的原币用途；K 与证券池各保留自身口径。不是当天实际可花、消费限额或证券市值。'}
        finally:
            if runtime: runtime.close()


def _history(q, account, purpose, first, commodity):
    roots = {q.fin.options['name_'+kind]: kind for kind in ('assets','liabilities','expenses','income')}
    kind = roots.get(account.split(':',1)[0])
    require(kind is not None, 'QUERY_ACCOUNT_UNSUPPORTED', '该类科目不承载 fund／budget；请查询对应的资产、负债或收支科目')
    categories = load_yaml(q.paths['classifications'])['categories']
    names = ({row['id']:row['name'] for row in categories} | {'-':'预算外'}) if kind in ('expenses','income') else q.names
    aliases = {name:row['id'] for row in (categories if kind in ('expenses','income') else q.owner_doc['owners'])
               for name in (row['id'],row['name'],*row.get('aliases',[]))} | {'-':'-','预算外':'-'}
    selected = aliases.get(purpose) if purpose is not None else None
    require(purpose is None or selected is not None, 'QUERY_PURPOSE_UNKNOWN', '未找到该预算名称；资产查 fund，负债查责任用途，收支查 budget 分类')
    opening = defaultdict(Fraction)
    changes = defaultdict(list)
    pool = next((row for row in q.pools['pools'] if account in row['accounts']), None)
    def add(entry, currency, owner, basis, delta, ref, reason):
        changes[id(entry)].append(((account,owner,currency,basis),delta,ref,reason))
    if kind == 'assets' and pool:
        _pool_history(q, account, pool, opening, add)
    else:
        if kind == 'assets':
            for row in load_yaml(q.paths['opening'])['h_holdings']:
                if row['account']==account:
                    opening[(account,row['owner'],row['commodity'],'H')] += Fraction(row['units'])
        claimed = {_key(row['ref']):row for row in q.ownership['ownership']}
        books = {_key(row['k_ref']):row for row in audit_k_events(q.root,q.cutover,q.paths['accounts'],q.paths['ownership'],q.paths['k_events'])['events']} if kind=='assets' else {}
        classified = {_key(row['ref']):row for row in audit_classification(q.root,q.cutover,q.paths['classifications'])['classifications']} if kind in ('expenses','income') else {}
        if kind == 'liabilities':
            opening.update({key:value for key,value in q.liability_closing.items() if key[0]==account})
        for entry in q.fin.entries:
            if not isinstance(entry,data.Transaction) or not q.cutover <= entry.date <= q.day: continue
            for posting in entry.postings:
                if posting.account != account: continue
                ref=q.facts.for_posting(posting).as_dict(); key=_key(ref); currency=posting.units.currency
                if kind=='assets' and key in claimed:
                    claim=claimed[key]
                    for part in claim['slices']:
                        add(entry,currency,part['owner_id'],'H',Fraction(part['signed_units']),ref,
                            claim.get('review_reason') or {'inline':'行内 fund','external':'已保存用途审阅','default':'默认用途'}.get(claim['origin'],'已审用途继承'))
                elif kind=='assets' and key in books:
                    event=books[key]
                    for part in event['slices']:
                        add(entry,currency,part['k_owner_id'],'K',Fraction(part['signed_k_units']),ref,'已审 K 申赎用途；不是持仓或市值')
                    if event['shape']=='zero_record':
                        raw=(posting.meta or {}).get('fund')
                        for name,amount in parse_inline_amounts(raw,Fraction(0),'零金额记录') if raw is not None else [(None,Fraction(0))]:
                            add(entry,currency,aliases.get(name),'K',amount,ref,'零金额备注；实际赠送份额和价值未记录')
                elif kind=='liabilities':
                    for part in q.liability_deltas[key]:
                        delta=Fraction(part['signed_units']);owner=part['owner_id']
                        opening[(account,owner,currency,'LIABILITY')] -= delta
                        add(entry,currency,owner,'LIABILITY',delta,ref,'已审责任变动；负数增加欠款／收回预付，正数还款／增加预付')
                elif kind in ('expenses','income'):
                    claim=classified[key]
                    for part in claim['slices']:
                        add(entry,currency,part['category_id'],'EXPENSE' if kind=='expenses' else 'INCOME',Fraction(part['signed_units'])*(1 if kind=='expenses' else -1),ref,
                            '行内 budget' if claim['origin']=='inline' else '已审分类／默认 budget')
    running=defaultdict(Fraction,opening)
    start=None;rows=[]
    def wanted(key):return (selected is None or key[1]==selected) and (commodity is None or key[2]==commodity)
    locations=posting_sources(q.fin)
    for entry in q.fin.entries:
        if not isinstance(entry,data.Transaction) or not q.cutover<=entry.date<=q.day:continue
        if entry.date>=first and start is None:start=dict(running)
        grouped=defaultdict(Fraction);details=defaultdict(list)
        for key,delta,ref,reason in changes.get(id(entry),[]):
            grouped[key]+=delta;details[key].append((ref,reason))
        for key,delta in grouped.items():
            running[key]+=delta
            if entry.date<first or not wanted(key):continue
            refs=[ref for ref,reason in details[key]]
            txref=q.facts.for_transaction(entry).as_dict();meta=entry.meta
            source=(f"{meta['filename']}:{meta['lineno']}" if 'generated_kind' in txref else locations[_key(refs[0])])
            path,line=source.rsplit(':',1)
            try:relative=str(Path(path).relative_to(q.root.parent))
            except ValueError:relative=path
            text=(q.fin.line(meta['filename'],meta['lineno']) if 'generated_kind' in txref else q.fin.transaction_text(meta['filename'],meta['lineno']))
            rows.append({'date':entry.date.isoformat(),'account':account,'owner_id':key[1],
                         'purpose':names.get(key[1],'未分预算用途／零金额备注'),'commodity':key[2],'basis':key[3],
                         'delta':exact_text(delta),'balance':exact_text(running[key]),'narration':entry.narration,
                         'source':source, 'sources':list(dict.fromkeys([source, *[locations[_key(ref)] for ref in refs]])),
                         'source_label':f'第 {line} 行 · {relative}',
                         'transaction_source':f"{meta['filename']}:{meta['lineno']}",'refs':refs,'transaction_ref':txref,
                         'reason':('初始化 pad 余额归属；不是收入或预算划拨' if 'generated_kind' in txref else '；'.join(dict.fromkeys(reason for ref,reason in details[key]))), 'source_text':text})
    start=dict(running) if start is None else start
    if kind in ('assets','liabilities'):
        expected={(row['account'],row['owner_id'],row['commodity'],row['basis']):Fraction(row['amount']) for row in q.positions if row['account']==account}
        require(all(running.get(key,0)==expected.get(key,0) for key in set(running)|set(expected)),
                'QUERY_RECONCILIATION_FAILED','历史期末用途余额与同日日末审计不一致，不能显示流水',str(q.config))
    require(selected is None or not any(key[1] is None and value for key,value in running.items()),
            'QUERY_PURPOSE_UNASSIGNED','该旧清算协议未声明预算用途；请先用逐笔负债模式和 budget 明确，不能把它当成所选用途的零变动')
    def positions(values):
        return [{**_position(q,key,value),'purpose':names.get(key[1],'未分预算用途')} for key,value in sorted(values.items(),key=lambda pair:str(pair[0])) if value and wanted(key)]
    return {'account':account,'purpose':purpose,'from':first.isoformat(),'through':q.day.isoformat(),
            'opening_positions':positions(start),'closing_positions':positions(running),'events':rows,'count':len(rows),
            'note':'余额按交易整体更新；K／收支为 cutover 后累计流量，非资产市值。负债按预算用途分别保留欠款与预付。'}


def _pool_history(q, account, pool, opening, add):
    balances={name:Fraction(0) for name in pool['accounts']}
    def vector():
        allocated=allocate_nominal(balances,pool['reserve_order'],Fraction(pool['protected_nominal_principal']),pool['remainder_policy']=='allow_negative')
        return {pool['protected_owner_id']:allocated[account],pool['remainder_owner_id']:balances[account]-allocated[account]}
    previous=None
    for entry in q.fin.entries:
        if not isinstance(entry,data.Transaction) or entry.date>q.day:continue
        if entry.date>=q.cutover and previous is None:
            previous=vector()
            for owner,amount in previous.items():opening[(account,owner,q.pools['report_currency'],'POOL')]=amount
        postings=[p for p in entry.postings if p.account in balances]
        for posting in postings:balances[posting.account]+=_book_weight(posting,q.pools['report_currency'])
        if entry.date>=q.cutover and postings:
            current=vector()
            for owner in current:
                if current[owner]!=previous[owner]:
                    add(entry,q.pools['report_currency'],owner,'POOL',current[owner]-previous[owner],
                        q.facts.for_posting(postings[0]).as_dict(),'按证券池保护顺序重算名义份额；不代表真实账户间划拨')
            previous=current
    if previous is None:
        for owner,amount in vector().items():opening[(account,owner,q.pools['report_currency'],'POOL')]=amount


def render_text(result):
    if result['report'] is None:
        return '\n'.join(['查询未签发；以下为待审原因：', *[
            f"{row['reason_code']}：{row.get('detail','')} [{row.get('source')}]" for row in result['review_items']]])
    report=result['report']
    lines=[f"日末用途查询：{result['as_of']}（依据 {result['approval_month']} 当前已审月报）", result['note']]
    if 'events' in report:
        lines += [f"科目：{report['account']}",f"预算：{report['purpose'] or '全部'}；{report['from']} 至 {report['through']} 日末",
                  '期初（起始日交易前）：']
        for row in report['opening_positions']:
            lines.append(f"  {row['purpose']}：{amount_text(row['amount'])} {row['commodity']}；{BASES[row['basis']]}")
        if not report['opening_positions']:lines.append('  无非零期初份额；K／收支以 cutover 后净流量为基数。')
        for row in report['events']:
            lines += [f"{row['date']}  {row['purpose']}  变动 {amount_text(row['delta'])} {row['commodity']} → {amount_text(row['balance'])}",
                      f"  位置：{row['source_label']}",f"  {row['narration']}；{row['reason']}"]
        lines.append('期末：')
        for row in report['closing_positions']:
            lines.append(f"  {row['purpose']}：{amount_text(row['amount'])} {row['commodity']}；{BASES[row['basis']]}")
        if not report['closing_positions']:lines.append('  所查询范围无非零期末份额。')
        lines.append(report['note'])
    else:
        for basis,label in BASES.items():
            rows=[row for row in report['positions'] if row['basis']==basis]
            if not rows:continue
            lines += ['',label+'：']
            for row in sorted(rows,key=lambda x:(x['account'],x['purpose'],x['commodity'])):
                lines.append(f"  {row['account']}：{row['purpose']} {amount_text(row['amount'])} {row['commodity']}")
        if not report['positions']:lines.append('该日日末没有已记非零用途份额。')
        if report['zero_value_records']:lines.append('另有 K 零金额备注；份额与价值未知，可查该科目历史。')
    return '\n'.join(lines)


def render_csv(result):
    columns=('section','date','account','purpose','commodity','basis','delta','balance','narration','reason','source')
    output=io.StringIO();writer=csv.DictWriter(output,fieldnames=columns);writer.writeheader()
    if result['report'] is None:
        for row in result['review_items']:
            writer.writerow({'section':'review','date':row.get('date',''),'account':row.get('account',''),
                             'source':row.get('source',''),'narration':row['reason_code']+': '+row.get('detail','')})
        return output.getvalue()
    report=result['report']
    for section,rows in (('opening',report.get('opening_positions',[])),('closing',report.get('closing_positions',report.get('positions',[])))):
        for row in rows:
            writer.writerow({'section':section,'date':report.get('from',result['as_of']) if section=='opening' else result['as_of'],
                             **{key:row[key] for key in ('account','purpose','commodity','basis')},'balance':amount_text(row['amount'])})
    for row in report.get('events',[]):
        writer.writerow({'section':'change',**{key:row[key] for key in ('date','account','purpose','commodity','basis','narration','reason','source')},
                         'delta':amount_text(row['delta']),'balance':amount_text(row['balance'])})
    return output.getvalue()


def _main(argv, history):
    configure_output()
    parser=argparse.ArgumentParser(description='查询已审月份内指定日日末的用途；历史流水与日末审计逐项核对')
    parser.add_argument('--config',type=Path,required=True)
    parser.add_argument('--as-of',required=True,help='YYYY-MM-DD 日末；所在月份须能签发月报')
    if history:
        parser.add_argument('--account',required=True,help='完整会计科目名，只查本级直接记账')
        parser.add_argument('--purpose','--budget',dest='purpose',help='资产 fund／负债责任用途／收支 budget 名称；省略显示全部')
        parser.add_argument('--since',help='起始日 YYYY-MM-DD；省略从 cutover 开始')
        parser.add_argument('--commodity',help='只看指定原币／商品，不做换算')
    parser.add_argument('--format',choices=('text','json','csv'),default='text')
    parser.add_argument('--output',type=Path,help='保存到新文件；不覆盖已有文件')
    args=parser.parse_args(argv)
    try:
        result=query(args.config,args.as_of,**({key:getattr(args,key) for key in ('account','purpose','since','commodity')} if history else {}))
        content=(json.dumps(result,ensure_ascii=False,indent=2) if args.format=='json' else render_csv(result) if args.format=='csv' else render_text(result))
        if args.output:
            with args.output.open('x',encoding='utf-8-sig' if args.format=='csv' else 'utf-8',newline='') as stream:
                stream.write(content+'\n')
        else:print(content)
        return 0
    except (BudgetError,OSError,UnicodeError) as exc:
        import sys
        error={'code':exc.code if isinstance(exc,BudgetError) else 'QUERY_IO_ERROR','message':str(exc),
               'source':getattr(exc,'source',None)}
        print(json.dumps({'error':error},ensure_ascii=False),file=sys.stderr)
        return 2


def history_main(argv=None):return _main(argv,True)
def snapshot_main(argv=None):return _main(argv,False)

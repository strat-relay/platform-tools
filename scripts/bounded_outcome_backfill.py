"""Hash-pinned historical recovery; archive writes only with --apply.

Reads a bounded plan from stdin. Never writes canonical outcomes or Redis and
only calls the read-only mt5_rates_range bridge method.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import math
import os
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal
from urllib.request import Request, urlopen

PAGE = 200
MAX_SLOTS = 5000
MAX_REQUESTS = 35


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def make_plan(audit):
    if audit.get('signals_truncated') or not audit.get('read_only'):
        raise ValueError('incomplete audit')
    groups = {}
    ids = []
    for item in audit['signals']:
        if item['classification'] != 'BLOCKED_PRICE_PATH_GAP':
            continue
        c = item['coverage_audit']
        if c['source_errors_or_truncation'] or c['missing_intervals_truncated']:
            raise ValueError('incomplete coverage')
        contract = item.get('outcome_contract') or {}
        step = int(contract.get('timeframe_minutes', 15))*60
        key = (item['provider_symbol'], step)
        ids.append(item['signal_id'])
        for gap in c['missing_intervals']:
            a = int(datetime.fromisoformat(gap['start']).timestamp())
            b = int(datetime.fromisoformat(gap['end_exclusive']).timestamp())
            groups.setdefault(key, []).append((a,b))
    intervals = []
    for (symbol,step), gaps in sorted(groups.items()):
        merged = []
        for a,b in sorted(gaps):
            if merged and a <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(b,merged[-1][1]))
            else:
                merged.append((a,b))
        intervals.extend(dict(symbol=symbol, timeframe=f'M{step//60}', start=a, end=b)
                         for a,b in merged)
    plan = dict(version='bounded-outcome-backfill.v1', provider='MT5',
                evidence_sha256=digest(audit), observed_at=audit['observed_at'],
                signal_ids=sorted(set(ids)), intervals=intervals)
    plan['plan_sha256'] = digest(plan)
    validate_plan(plan, plan['plan_sha256'], time.time())
    return plan


def validate_plan(plan, expected, now):
    body = {k:v for k,v in plan.items() if k != 'plan_sha256'}
    if digest(body) != expected or plan.get('plan_sha256') != expected:
        raise ValueError('plan hash mismatch')
    if plan.get('version') != 'bounded-outcome-backfill.v1' or plan.get('provider') != 'MT5':
        raise ValueError('unsupported plan')
    intervals = plan['intervals']
    if not intervals or len(intervals)>100:
        raise ValueError('invalid interval count')
    slots = requests = 0
    streams = set()
    ends = {}
    for item in intervals:
        symbol,tf,a,b = (item[k] for k in ('symbol','timeframe','start','end'))
        if not isinstance(symbol,str) or not symbol.isalnum() or len(symbol)>32 or tf not in ('M5','M15'):
            raise ValueError('unsupported stream')
        step = int(tf[1:])*60
        if type(a) is not int or type(b) is not int or a<0 or b<=a or a%step or b%step or b>now:
            raise ValueError('invalid time bounds')
        key = (symbol,tf)
        if key in ends and a < ends[key]:
            raise ValueError('overlapping or unordered intervals')
        ends[key] = b
        streams.add(key)
        count = (b-a)//step
        slots += count
        requests += math.ceil(count/PAGE)
    if len(streams)>6 or slots>MAX_SLOTS or requests>MAX_REQUESTS:
        raise ValueError('backfill budget exceeded')
    if max(x['end'] for x in intervals)-min(x['start'] for x in intervals)>14*86400:
        raise ValueError('backfill span exceeded')
    return dict(streams=len(streams), intervals=len(intervals), slots=slots, requests=requests)


def validate_rows(rows, start, end, step):
    if not isinstance(rows,list) or len(rows)>PAGE+1:
        raise ValueError('invalid page size')
    accepted = []
    previous = None
    for row in rows:
        t = row['time']
        if type(t) is not int or t%step or not start<=t<=end or (previous is not None and t<=previous):
            raise ValueError('invalid candle timestamp/order')
        previous = t
        if row.get('is_closed') is False or row.get('complete') is False:
            raise ValueError('forming candle')
        values = [Decimal(str(row[k])) for k in ('open','high','low','close')]
        if any(not v.is_finite() or v<=0 or v>=Decimal('1e12') or v!=v.quantize(Decimal('.00000001')) for v in values):
            raise ValueError('invalid candle price/precision')
        op,hi,lo,cl = values
        if lo>min(op,cl) or hi<max(op,cl) or lo>hi:
            raise ValueError('invalid OHLC geometry')
        if t == end:  # Some bridge implementations include the upper bound.
            continue
        accepted.append(dict(time=t, **{k:str(v) for k,v in zip(('open','high','low','close'),values)}))
    return accepted


def read_page(url, item, start, end, timeout):
    payload = dict(jsonrpc='2.0', id=f'bounded-{start}', method='tools/call', params={
        'name':'mt5_rates_range', 'arguments':dict(symbol=item['symbol'], timeframe=item['timeframe'],
            start_timestamp=start, end_timestamp=end, page_size=PAGE, completed_only=True)})
    req = Request(url, data=json.dumps(payload).encode(), headers={'Content-Type':'application/json'})
    with urlopen(req, timeout=timeout) as response:
        envelope = json.load(response)
    result = envelope['result']
    if envelope.get('error') or result.get('isError'):
        raise ValueError('bridge request failed')
    parsed = json.loads(result['content'][0]['text'])
    if isinstance(parsed,dict):
        if parsed.get('error') or ('symbol' in parsed and parsed['symbol']!=item['symbol']):
            raise ValueError('bridge response mismatch')
        return parsed['rates']
    return parsed


def fetch(plan, url, *, reader=read_page, sleep=time.sleep, clock=time.monotonic):
    started = clock()
    pages = []
    for index,item in enumerate(plan['intervals']):
        step = int(item['timeframe'][1:])*60
        for a in range(item['start'], item['end'], PAGE*step):
            remaining = 300-(clock()-started)
            if remaining<=1:
                raise TimeoutError('total bridge budget exhausted')
            if pages:
                sleep(1)
            b = min(item['end'],a+PAGE*step)
            rows = validate_rows(reader(url,item,a,b,min(30,remaining-1)),a,b,step)
            pages.append(dict(interval=index, start=a, end=b, rows=rows))
            print(json.dumps(dict(event='bounded_page_validated',page=len(pages),bars=len(rows))),file=sys.stderr,flush=True)
    return pages


def preflight(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT current_database(), pg_is_in_recovery()")
        database, recovery = cur.fetchone()
        if database != 'trading_platform' or recovery:
            raise ValueError('unexpected database target')
        for table, rights in (('candle_archive',('SELECT','INSERT')), ('backfill_runs',('SELECT','INSERT','UPDATE'))):
            for right in rights:
                cur.execute('SELECT has_table_privilege(current_user,%s,%s)',(f'market_data.{table}',right))
                if not cur.fetchone()[0]:
                    raise PermissionError(f'missing {right} on market_data.{table}')


def apply(conn, plan, pages):
    """One atomic append-only archive transaction; any conflict aborts all writes."""
    inserted = unchanged = 0
    with conn.cursor() as cur:
        cur.execute("SET LOCAL statement_timeout = '20s'")
        cur.execute("SET LOCAL lock_timeout = '5s'")
        cur.execute("SELECT pg_try_advisory_xact_lock(hashtextextended('bounded-candle-backfill',0))")
        if not cur.fetchone()[0]:
            raise RuntimeError('another bounded backfill is running')
        for index,item in enumerate(plan['intervals']):
            run = f"bounded-{plan['plan_sha256'][:20]}-{index}"
            cur.execute("""SELECT provider,provider_symbol,timeframe,requested_start,requested_end
                FROM market_data.backfill_runs WHERE run_id=%s""",(run,))
            previous = cur.fetchone()
            identity = ('MT5',item['symbol'],item['timeframe'],item['start'],item['end'])
            if previous and tuple(previous)!=identity:
                raise ValueError('backfill run identity conflict')
            rows = [r for p in pages if p['interval']==index for r in p['rows']]
            for row in rows:
                values = tuple(Decimal(row[k]) for k in ('open','high','low','close'))
                key = ('MT5',item['symbol'],item['timeframe'],row['time'])
                cur.execute("""INSERT INTO market_data.candle_archive
                    (provider,provider_symbol,timeframe,candle_time,open_price,high_price,low_price,close_price,source_run_id)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (provider,provider_symbol,timeframe,candle_time) DO NOTHING""",(*key,*values,run))
                if cur.rowcount:
                    inserted += 1
                else:
                    cur.execute("""SELECT open_price,high_price,low_price,close_price FROM market_data.candle_archive
                        WHERE provider=%s AND provider_symbol=%s AND timeframe=%s AND candle_time=%s""",key)
                    if tuple(cur.fetchone() or ())!=values:
                        raise ValueError('archived OHLC conflict; transaction must roll back')
                    unchanged += 1
            expected = (item['end']-item['start'])//(int(item['timeframe'][1:])*60)
            missing = expected-len(rows)
            step = int(item['timeframe'][1:])*60
            missing_times = set(range(item['start'],item['end'],step))-{r['time'] for r in rows}
            gap_count = sum(t-step not in missing_times for t in missing_times)
            cur.execute("""INSERT INTO market_data.backfill_runs
                (run_id,provider,provider_symbol,timeframe,requested_start,requested_end,
                 checkpoint_cursor,status,bars_fetched,gaps_detected,completed_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now())
                ON CONFLICT (run_id) DO UPDATE SET status=EXCLUDED.status,
                 checkpoint_cursor=EXCLUDED.checkpoint_cursor,bars_fetched=EXCLUDED.bars_fetched,
                 gaps_detected=EXCLUDED.gaps_detected,completed_at=EXCLUDED.completed_at""",
                (run,*identity,item['end'],'PARTIAL' if missing else 'COMPLETE',len(rows),gap_count))
    return dict(inserted=inserted, unchanged=unchanged)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--make-plan')
    parser.add_argument('--expected-plan-sha')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    if args.make_plan:
        with open(args.make_plan) as f:
            print(json.dumps(make_plan(json.load(f)),indent=2))
        return
    plan = json.load(sys.stdin)
    budget = validate_plan(plan,args.expected_plan_sha,time.time())
    from postgres.config import PostgresConfig
    from postgres.db import connect
    cfg = PostgresConfig.from_env()
    cfg.require_explicit_target()
    # Check target and privileges without holding a transaction during bridge IO.
    with connect(cfg) as conn:
        conn.read_only = True
        preflight(conn)
        conn.rollback()
    url = os.environ['MARKET_DATA_BRIDGE_URL']
    pages = fetch(plan,url)
    result = dict(status='VALIDATED',plan_sha256=plan['plan_sha256'],budget=budget,
                  fetched_bars=sum(len(p['rows']) for p in pages),broker_writes=0)
    result['unobserved_slots'] = budget['slots']-result['fetched_bars']
    if args.apply:
        with connect(cfg) as conn:
            preflight(conn)
            result.update(apply(conn,plan,pages))
            conn.commit()
        result['status'] = 'ARCHIVE_APPLIED'
    # Preserve fetched observations in the report, including missing-slot counts.
    result['observations_sha256'] = digest(pages)
    result['pages'] = pages
    print(json.dumps(result,indent=2))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        result = dict(status='BLOCKED',error_class=type(exc).__name__,broker_writes=0)
        if type(exc) is PermissionError:
            result['reason'] = str(exc)  # Fixed permission identifiers, never credentials.
        print(json.dumps(result))
        raise SystemExit(1)

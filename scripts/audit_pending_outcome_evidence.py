"""Read-only pending-outcome inventory and archive/cache coverage audit.

Run inside the resolver image with its existing environment. This script does
not acquire a lease, resolve/persist outcomes, call brokers, or repair data.
"""
from __future__ import annotations

import json
import os
from collections import Counter, defaultdict
from datetime import datetime, timezone

from outcome_resolver import EvaluationContract
from outcome_resolver_runtime import ResolverRedisCandleStore, ResolverSignal
from postgres.config import PostgresConfig
from postgres.db import connect

MAX_ARCHIVE_ROWS = 20000
MAX_SIGNALS = 10000


def iso(stamp):
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat()


def missing_intervals(times, start, end, step):
    """Exact missing candle slots in [start, end), with aligned bounds."""
    if step <= 0 or start % step or end % step:
        raise ValueError('unaligned coverage bounds')
    present = set(times)
    gaps, first = [], None
    for stamp in range(start, end, step):
        if stamp not in present:
            if first is None:
                first = stamp
        elif first is not None:
            gaps.append((first, stamp))
            first = None
    if first is not None:
        gaps.append((first, end))
    return gaps


def classification(state, evidence):
    if state == 'AMBIGUOUS_INTRABAR':
        return 'AMBIGUOUS_INTRABAR'
    if evidence.get('reason') == 'CANDLE_GAP' or evidence.get('status') == 'DATA_GAP_UNRESOLVED':
        return 'BLOCKED_PRICE_PATH_GAP'
    if evidence.get('status') == 'OPEN_AT_DATASET_END':
        return 'OPEN_THROUGH_LAST_EVALUATED_CANDLE'
    return 'NEEDS_INDIVIDUAL_REVIEW'


def coverage(cache_times, archive_times, *, entry, end, step):
    start = entry // step * step
    gaps = missing_intervals(set(cache_times) | set(archive_times), start, end, step)
    cache = sorted(t for t in set(cache_times) if start <= t < end)
    supplied = sorted(t for t in set(archive_times) - set(cache_times) if start <= t < end)
    return {
        'entry_aligned_to_timeframe': entry % step == 0,
        'entry_candle_available': start in set(cache_times) | set(archive_times),
        'archive_only_candles': len(supplied),
        'archive_only_candles_inside_cache_window': sum(cache[0] < t < cache[-1] for t in supplied) if cache else 0,
        'missing_candle_slots': sum((b-a)//step for a,b in gaps),
        'missing_interval_count': len(gaps),
        'missing_intervals_truncated': len(gaps) > 100,
        'missing_intervals': [{'start': iso(a), 'end_exclusive': iso(b),
                               'slots': (b-a)//step,
                               'session_status': 'UNVERIFIED_NO_BROKER_CALENDAR'} for a,b in gaps[:100]],
    }


def audit(conn, redis_client, *, now):
    """Connection must already enforce a read-only transaction."""
    with conn.cursor() as cur:
        cur.execute('SHOW transaction_read_only')
        if cur.fetchone()[0] != 'on':
            raise RuntimeError('audit requires a read-only transaction')
        cur.execute("""SELECT s.signal_id, s.strategy_id, s.instrument, s.direction,
                     s.entry_price, s.stop_price, s.target_price, s.decision_time,
                     s.entry_type, s.strategy_metadata, s.source_provenance,
                     w.resolution_state, w.evidence, w.updated_at, w.attempt_count,
                     w.last_error, w.coverage_end, o.resolution_state,
                     o.resolution_evidence
              FROM strategy.entry_signals s
              LEFT JOIN strategy.entry_signal_outcomes o USING (signal_id)
              LEFT JOIN platform.outcome_resolver_signal_state w USING (signal_id)
              WHERE (o.signal_id IS NULL OR o.status = 'OPEN')
              ORDER BY s.decision_time, s.signal_id LIMIT %s""", (MAX_SIGNALS+1,))
        rows = cur.fetchall()
    report = {'observed_at': iso(now), 'read_only': True, 'broker_writes': 0,
              'signals_truncated': len(rows) > MAX_SIGNALS,
              'signals': [], 'streams': {}, 'scope': 'COVERAGE_ONLY_NO_OUTCOME_MUTATION',
              'limitations': ['No broker session calendar is available; gaps are not excused.',
                              'Coverage does not validate prices, revisions, or BID/ASK history.',
                              'Archive availability is not proof of runtime consumption.']}
    groups = defaultdict(list)
    for row in rows[:MAX_SIGNALS]:
        evidence = row[12] or row[18] or {}
        if isinstance(evidence, str):
            evidence = json.loads(evidence)
        item = {'signal_id': row[0], 'strategy_id': row[1], 'instrument': row[2],
                'decision_time': str(row[7]), 'resolution_state': row[11] or row[17],
                'last_attempt': str(row[13]) if row[13] else None,
                'attempt_count': row[14], 'last_error_class': (row[15] or '').split(':',1)[0] or None,
                'coverage_end': str(row[16]) if row[16] else None, 'evidence': evidence}
        item['classification'] = classification(item['resolution_state'], evidence)
        report['signals'].append(item)
        try:
            signal = ResolverSignal.from_row(row[:11])
            contract = EvaluationContract.from_signal({'strategy_id': signal.strategy_id,
                'entry_type': signal.entry_type, 'strategy_metadata': signal.strategy_metadata})
            step = contract.timeframe_minutes * 60
            if step <= 0:
                raise ValueError('invalid timeframe')
            symbol = ResolverRedisCandleStore._default_symbol(signal)
            item['provider_symbol'] = symbol
            item['outcome_contract'] = signal.strategy_metadata.get('outcome_contract')
            groups[(symbol, step)].append((signal, item))
        except Exception as exc:
            item['audit_error_class'] = type(exc).__name__
    for (symbol, step), members in groups.items():
        key = f'{symbol}:M{step//60}'
        stream = report['streams'][key] = {}
        start = min(int(s.decision_time.timestamp())//step*step for s,_ in members)
        end = int(now)//step*step
        try:
            # A savepoint keeps a failed SELECT from aborting the remaining audit.
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute("""SELECT provider, candle_time FROM market_data.candle_archive
                        WHERE provider_symbol=%s AND timeframe=%s
                          AND candle_time >= %s AND candle_time < %s
                        ORDER BY candle_time, provider LIMIT %s""",
                        (symbol, f'M{step//60}', start, end, MAX_ARCHIVE_ROWS+1))
                    archived = cur.fetchall()
            stream['archive_truncated'] = len(archived) > MAX_ARCHIVE_ROWS
            providers = defaultdict(set)
            for provider, stamp in archived[:MAX_ARCHIVE_ROWS]:
                providers[provider].add(int(stamp))
            stream['archive_providers'] = {p: {'bars':len(ts), 'first':iso(min(ts)),
                                                   'last':iso(max(ts))} for p,ts in providers.items()}
            stream['multiple_providers_require_selection'] = len(providers) > 1
            # Never stitch different providers into an apparently complete path.
            archive_times = next(iter(providers.values())) if len(providers) == 1 else set()
        except Exception as exc:
            stream['archive_error_class'] = type(exc).__name__
            archive_times = set()
        try:
            raw = redis_client.get(f'md:bars:{key}')
            bars = json.loads(raw) if raw else []
            times = [int(b['time']) for b in bars]
            cache_times = {t for t in times if t+step <= now}
            stream.update(cache_bars=len(cache_times), cache_first=iso(min(cache_times)) if cache_times else None,
                          cache_last=iso(max(cache_times)) if cache_times else None,
                          duplicate_times=len(times)-len(set(times)),
                          off_grid_times=sum(t % step != 0 for t in times),
                          incomplete_candles=sum(t+step > now for t in times))
        except Exception as exc:
            stream['cache_error_class'] = type(exc).__name__
            cache_times = set()
        for signal, item in members:
            item['coverage_audit'] = coverage(cache_times, archive_times,
                entry=int(signal.decision_time.timestamp()), end=end, step=step)
            item['coverage_audit']['source_errors_or_truncation'] = any(
                stream.get(k) for k in ('archive_error_class', 'cache_error_class',
                                       'archive_truncated', 'multiple_providers_require_selection'))
    report['counts'] = dict(Counter(r['classification'] for r in report['signals']))
    return report


def main():
    import redis
    cfg = PostgresConfig.from_env()
    cfg.require_explicit_target()
    client = redis.Redis.from_url(os.environ['MARKET_DATA_REDIS_URL'],
                                 socket_connect_timeout=5, socket_timeout=5)
    try:
        with connect(cfg) as conn:
            conn.read_only = True
            with conn.cursor() as cur:
                cur.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
                cur.execute("SET LOCAL statement_timeout = '20s'")
            print(json.dumps(audit(conn, client, now=datetime.now(timezone.utc).timestamp()), default=str, indent=2))
            conn.rollback()
    finally:
        client.close()


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        # Do not print connection strings or exception messages into reports.
        print(json.dumps({'status':'BLOCKED', 'error_class':type(exc).__name__,
                          'read_only':True, 'broker_writes':0}))
        raise SystemExit(1)

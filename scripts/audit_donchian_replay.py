"""Bounded, read-only Donchian source-event and reference-candle audit.

No bridge access, publication, checkpoint mutation or outcome persistence.
Replay uses the existing frozen theoretical resolver, not broker P/L.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path

STRATEGY = 'BREAKOUT_DONCHIAN_BREAKOUT'
STEP = 900
MAX_SIGNALS = 500
MAX_BARS = 20000
AUDIT_VERSION = 'donchian-evidence-audit.v1'


def field(obj, snake, camel=None):
    return obj.get(snake, obj.get(camel)) if camel else obj.get(snake)


def epoch(value):
    if isinstance(value, (int, float)):
        return float(value)
    parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('timezone required')
    return parsed.timestamp()


def normalise(bars, *, now):
    """Reject malformed/duplicate input; discard forming bars explicitly."""
    result = {}
    forming = 0
    for raw in bars:
        stamp = raw['time']
        if type(stamp) is not int or stamp % STEP:
            raise ValueError('invalid candle identity')
        prices = {k: float(raw[k]) for k in ('open', 'high', 'low', 'close')}
        if any(not math.isfinite(v) or v <= 0 for v in prices.values()):
            raise ValueError('invalid price')
        if not (prices['low'] <= min(prices['open'], prices['close']) <=
                max(prices['open'], prices['close']) <= prices['high']):
            raise ValueError('invalid OHLC')
        if stamp in result:
            raise ValueError('duplicate candle')
        if stamp+STEP > now or raw.get('is_closed') is False:
            forming += 1
            continue
        result[stamp] = {'time': stamp, **prices}
    return result, forming


def combine(archived, cached):
    conflicts = [t for t in archived.keys() & cached.keys()
                 if any(abs(archived[t][k]-cached[t][k]) > 1e-8
                        for k in ('open', 'high', 'low', 'close'))]
    return archived | cached, sorted(conflicts)


def timing_audit(signal, source):
    m, p = signal.get('strategy_metadata') or {}, signal.get('source_provenance') or {}
    entry = epoch(signal['decision_time'])
    emitted = epoch(signal['signal_emitted_at'])
    raw = (source or {}).get('payload') or {}
    provenance = raw.get('provenance') or {}
    entry_detail = (provenance.get('research_entry') or {}).get('provenance') or {}
    classes = {
        'canonical_classification': p.get('classification'),
        'canonical_emission_class': field(p, 'signal_emission_class', 'signalEmissionClass'),
        'metadata_emission_class': field(m, 'signal_emission_class', 'signalEmissionClass'),
        'metadata_evidence_class': field(m, 'research_evidence_class', 'researchEvidenceClass'),
        'source_evidence_class': (source or {}).get('evidence_class'),
        'entry_classification': entry_detail.get('entry_classification'),
    }
    reconstructed = any('RECONSTRUCT' in str(v) for v in classes.values() if v)
    prospective = any(v in {'PROSPECTIVE', 'PROSPECTIVE_ORCHESTRATOR_SIGNAL'}
                      for k, v in classes.items() if k.startswith('canonical') or k.startswith('metadata_emission'))
    return {**classes, 'entry_to_emission_seconds': emitted-entry,
            'reconstruction_evidenced': reconstructed,
            'prospective_label_conflict': prospective and (reconstructed or emitted-entry > 60),
            'source_event_available': source is not None,
            'current_policy_expectation': ('RETROSPECTIVE_RESEARCH_EVIDENCE'
                if reconstructed or emitted-entry > 60 else 'REQUIRES_OPEN_OBSERVATION_EVIDENCE')}


def replay(signal, bars, *, tick, parameter_hash):
    """Inspect causal geometry and rerun the existing frozen outcome resolver."""
    from core.strategies.evaluation import canonical_hash
    from strategy_backtest.library.catalog import BY_KEY
    from strategy_backtest.library import primitives as p
    from strategy_backtest.models import MarketEvent
    from strategy_backtest.research_outcomes import ResearchOutcomeEntry, resolve_research_entry
    definition = BY_KEY['donchian_breakout']
    stamp = int(epoch(signal['decision_time']))
    before = [bars[t] for t in sorted(bars) if t < stamp][-40:]
    result = {'confirmation_time': stamp, 'entry_time': stamp,
              'parameter_identity_matches': parameter_hash == canonical_hash(definition.manifest()),
              'tick_size_from_frozen_proposal': tick}
    # Confirmation candle and 39 predecessors must all be observed. No calendar guesses.
    complete = len(before) == 40 and [b['time'] for b in before] == list(range(stamp-40*STEP, stamp, STEP))
    result['confirmation_window_complete'] = complete
    if complete:
        prior, current = before[:-1], before[-1]
        low, high = min(b['low'] for b in prior[-20:]), max(b['high'] for b in prior[-20:])
        direction = signal['direction']
        result.update(channel_low=low, channel_high=high,
                      confirmation_close=current['close'],
                      direction_matches_breakout=(current['close'] > high if direction == 'LONG' else current['close'] < low))
        events = [MarketEvent(signal['instrument'], 'M15', b['time'], b['time']+STEP,
                             b['open'], b['high'], b['low'], b['close']) for b in before]
        atr = p.atr(events[:-1], definition.atr_period)
        result['prior_atr'] = atr
        result['confirmation_body_atr'] = abs(current['close']-current['open'])/atr if atr else None
        result['breakout_distance_atr'] = ((current['close']-high if direction == 'LONG'
                                          else low-current['close'])/atr) if atr else None
        if tick is not None and math.isfinite(tick) and tick > 0 and result['parameter_identity_matches']:
            sign = 1 if direction == 'LONG' else -1
            buffer = max(tick, definition.buffer_atr*atr)
            stop = (min(b['low'] for b in before[-5:])-buffer if sign == 1
                    else max(b['high'] for b in before[-5:])+buffer)
            target = current['close']+sign*definition.target_r*abs(current['close']-stop)
            result.update(expected_stop=stop, expected_target=target,
                          stop_matches=abs(stop-float(signal['stop_price'])) <= 1e-7,
                          target_matches=abs(target-float(signal['target_price'])) <= 1e-7,
                          next_open_gap_within_policy=(abs(float(signal['entry_price'])-current['close']) <= definition.max_entry_gap_atr*atr))
    opening = bars.get(stamp)
    result['entry_candle_available'] = opening is not None
    result['entry_matches_reference_open'] = (abs(opening['open']-float(signal['entry_price'])) <= 1e-7 if opening else None)
    entry = ResearchOutcomeEntry(signal['signal_id'], STRATEGY+'@V1', signal['instrument'],
                                 signal['direction'], stamp, float(signal['entry_price']),
                                 float(signal['stop_price']), float(signal['target_price']), parameter_hash)
    path = [bars[t] for t in sorted(bars) if t >= stamp]
    outcome = resolve_research_entry(entry, path, timeframe_seconds=STEP)
    result['shared_resolver_replay'] = asdict(outcome)
    result['path_hash'] = hashlib.sha256(json.dumps(path, sort_keys=True).encode()).hexdigest()
    result['path_bars'] = len(path)
    result['path_last_open'] = path[-1]['time'] if path else None
    # Raw path is retained so another implementation can independently inspect touches.
    result['candles'] = before + path
    return result


def audit(conn, cache, *, now):
    with conn.cursor() as cur:
        cur.execute('SHOW transaction_read_only')
        if cur.fetchone()[0] != 'on':
            raise RuntimeError('read-only transaction required')
        cur.execute('''SELECT to_jsonb(s), to_jsonb(o), pub.publish_status, pub.published_at
            FROM strategy.entry_signals s
            LEFT JOIN strategy.entry_signal_outcomes o USING(signal_id)
            LEFT JOIN LATERAL (SELECT publish_status,published_at FROM platform.outbox_events
                WHERE aggregate_type='signal' AND aggregate_id=s.signal_id
                  AND event_type='signal.entry.created.v1'
                ORDER BY created_at DESC,event_id LIMIT 1) pub ON TRUE
            WHERE s.strategy_id=%s ORDER BY s.decision_time,s.signal_id LIMIT %s''',
                    (STRATEGY, MAX_SIGNALS+1))
        rows = cur.fetchall()
    if len(rows) > MAX_SIGNALS:
        raise ValueError('signal audit bound exceeded; choose a narrower scope')
    report = {'audit_version': AUDIT_VERSION, 'observed_at_utc': datetime.fromtimestamp(now, timezone.utc).isoformat(),
              'read_only': True, 'broker_calls': 0, 'signals': [], 'streams': {},
              'limitations': ['Shared-resolver replay is consistency checking, not an independent outcome implementation.',
                             'Current reference candles do not prove historical availability or executable prices.',
                             'No guessed session calendar, cost model or synthetic candles.',
                             'Historical labels, strategy rules and outcomes are never rewritten.']}
    streams = {}
    for signal, _, _, _ in rows:
        m, p = signal.get('strategy_metadata') or {}, signal.get('source_provenance') or {}
        symbol = p.get('provider_symbol')
        if not symbol:
            report['streams'][signal['instrument']] = {'status': 'PROVIDER_SYMBOL_MISSING'}
            continue
        start = int(epoch(signal['decision_time']))-40*STEP
        streams[symbol] = min(start, streams.get(symbol, start))
    for symbol, start in streams.items():
        detail = report['streams'][symbol] = {}
        try:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute('''SELECT candle_time,open_price,high_price,low_price,close_price,is_closed
                        FROM market_data.candle_archive WHERE provider='MT5' AND provider_symbol=%s
                        AND timeframe='M15' AND candle_time >= %s AND candle_time < %s
                        ORDER BY candle_time LIMIT %s''', (symbol, start, int(now)//STEP*STEP, MAX_BARS+1))
                    archived = cur.fetchall()
            if len(archived) > MAX_BARS:
                raise ValueError('archive row bound exceeded')
            a, _ = normalise([dict(zip(('time','open','high','low','close','is_closed'), row)) for row in archived], now=now)
            raw = cache.get('md:bars:'+symbol+':M15')
            cached = json.loads(raw) if raw else []
            if len(cached) > MAX_BARS:
                raise ValueError('cache row bound exceeded')
            c, forming = normalise(cached, now=now)
            c = {t:b for t,b in c.items() if t >= start}
            bars, conflicts = combine(a, c)
            detail.update(provider='MT5', archive_bars=len(a), cache_bars=len(c),
                          forming_bars_excluded=forming, conflict_times=conflicts,
                          status='CONFLICT' if conflicts else 'AVAILABLE')
            detail['_bars'] = bars
        except Exception as exc:
            detail.update(status='UNAVAILABLE', error_class=type(exc).__name__)
    for signal, outcome, pub_status, pub_at in rows:
        m, p = signal.get('strategy_metadata') or {}, signal.get('source_provenance') or {}
        item = {k:signal.get(k) for k in ('signal_id','strategy_version','strategy_instance_id','instrument',
                'direction','decision_time','signal_emitted_at','entry_price','stop_price','target_price')}
        item.update(parameter_hash=m.get('parameter_hash'), run_id=m.get('library_run_id'),
                    source_event_id=signal.get('source_event_id'), source_process=p.get('source_process'),
                    artifact_digest=m.get('artifact_digest'), published_at=pub_at, publication_state=pub_status,
                    metadata=m, source_provenance=p)
        item['stored_outcome'] = {k:(outcome or {}).get(k) for k in ('status','outcome_kind','realized_r',
                'exit_timestamp','resolution_state','resolution_evidence')}
        source = proposal = None
        run = m.get('library_run_id') or p.get('library_run_id')
        event = m.get('library_event_id')
        if run and event:
            try:
                with conn.transaction():
                    with conn.cursor() as cur:
                        cur.execute('''SELECT payload FROM research.strategy_library_forward_event
                            WHERE run_id=%s AND event_id=%s AND strategy_key='donchian_breakout'
                            AND instrument=%s''', (run, event, signal['instrument']))
                        found = cur.fetchone()
                        source = found[0] if found else None
                        episode = ((source or {}).get('payload', {}).get('provenance', {}).get('research_entry') or {}).get('episode_id')
                        if episode:
                            cur.execute('''SELECT payload FROM research.strategy_library_forward_event
                                WHERE run_id=%s AND strategy_key='donchian_breakout' AND instrument=%s
                                AND payload->'payload'->'provenance'->'proposal'->>'episode_id'=%s
                                ORDER BY observed_at,event_id LIMIT 2''', (run, signal['instrument'], episode))
                            candidates = cur.fetchall()
                            if len(candidates) == 1:
                                proposal = candidates[0][0]
                            elif candidates:
                                item['proposal_lookup_status'] = 'MULTIPLE_MATCHES'
            except Exception as exc:
                item['source_lookup_error_class'] = type(exc).__name__
        upstream = (source or {}).get('payload', {}).get('provenance', {}).get('research_entry') or {}
        item['source_to_canonical'] = {
            'entry_time_matches': epoch(signal['decision_time']) == upstream.get('entry_timestamp') if upstream else None,
            'direction_matches': signal['direction'] == upstream.get('direction') if upstream else None,
            **{k+'_matches': abs(float(signal[k])-float(upstream[k])) <= 1e-7
               if k in upstream else None for k in ('entry_price','stop_price','target_price')},
        }
        item['source_observer_event'] = source
        item['source_proposal_event'] = proposal
        item['timing'] = timing_audit(signal, source)
        proposal_detail = (proposal or {}).get('payload', {}).get('provenance', {}).get('proposal') or {}
        tick = proposal_detail.get('evidence', {}).get('tick_size')
        detail = report['streams'].get(p.get('provider_symbol'), {})
        if detail.get('status') == 'AVAILABLE':
            try:
                item['replay'] = replay(signal, detail['_bars'], tick=tick, parameter_hash=m.get('parameter_hash'))
                stored = outcome or {}
                computed = item['replay']['shared_resolver_replay']
                replay_status = {'SL':'STOPPED','TP':'TARGET_HIT'}.get(computed['status'])
                terminal = replay_status and stored.get('resolution_state') == 'RESOLVED'
                item['replay']['stored_exit_time_matches'] = (epoch(stored['exit_timestamp']) == computed['exit_timestamp']
                    if terminal and stored.get('exit_timestamp') else None)
                item['replay']['stored_realized_r_matches'] = (abs(float(stored['realized_r'])-computed['realized_r']) <= 1e-7
                    if terminal and stored.get('realized_r') is not None else None)
                item['replay']['stored_terminal_status_matches'] = (replay_status == stored.get('status')
                    if replay_status and stored.get('resolution_state') == 'RESOLVED' else None)
            except Exception as exc:
                item['replay_error_class'] = type(exc).__name__
        else:
            item['replay_status'] = 'BLOCKED_REFERENCE_STREAM_UNAVAILABLE_OR_CONFLICTED'
        report['signals'].append(item)
    for detail in report['streams'].values():
        detail.pop('_bars', None)
    report['summary'] = {'signals':len(rows),
        'prospective_label_conflicts':sum(x['timing']['prospective_label_conflict'] for x in report['signals']),
        'source_events_available':sum(x['timing']['source_event_available'] for x in report['signals']),
        'replay_statuses':dict(Counter(x.get('replay', {}).get('shared_resolver_replay', {}).get('status',
                                           x.get('replay_status', x.get('replay_error_class', 'UNKNOWN')))
                                     for x in report['signals']))}
    return report


def main():
    import psycopg
    import redis
    from postgres.config import PostgresConfig
    cfg = PostgresConfig.from_env()
    cfg.require_explicit_target()
    cache = redis.Redis.from_url(os.environ['MARKET_DATA_REDIS_URL'], socket_connect_timeout=5, socket_timeout=5)
    try:
        with psycopg.connect(**cfg.connect_kwargs(), connect_timeout=5) as conn:
            conn.read_only = True
            with conn.cursor() as cur:
                cur.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
                cur.execute("SET LOCAL statement_timeout = '20s'")
            report = audit(conn, cache, now=datetime.now(timezone.utc).timestamp())
            root = Path(__import__('strategy_backtest').__file__).resolve().parent.parent
            report['runtime_code_hashes'] = {
                path: hashlib.sha256((root/path).read_bytes()).hexdigest() if (root/path).is_file() else None
                for path in ('orchestration/adapters/strategy_library.py','orchestration/entry_timing.py',
                             'orchestration/canonical_signal_publisher.py','strategy_backtest/library/compositions.py',
                             'strategy_backtest/research_outcomes.py')
            }
            conn.rollback()
            print(json.dumps(report, default=str, indent=2, allow_nan=False))
    finally:
        cache.close()


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(json.dumps({'status':'BLOCKED','error_class':type(exc).__name__,
                          'read_only':True,'broker_calls':0}))
        raise SystemExit(1)

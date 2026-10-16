"""Read-only library observer facts, independent of publication/execution gates."""
from datetime import datetime, timezone
from copy import deepcopy
from .library_market_schedule import expected_weekend


def observation_status(state, key, *, now=None):
    health = (state or {}).get('health', {})
    observed = health.get('observed_at')
    streams = [deepcopy(s) for s in health.get('streams', []) if s.get('key') == key]
    now = datetime.now(timezone.utc).timestamp() if now is None else now
    fresh = isinstance(observed, (float,int)) and 0 <= now-observed <= 330
    for stream in streams:
        stored = (state or {}).get('streams', {}).get(key+':'+str(stream.get('instrument')), {})
        closure = expected_weekend(stream.get('instrument'), now, stored) if fresh else None
        if stream.get('status') == 'BLOCKED' and stream.get('error_type') == 'ValueError' and closure:
            stream.update(raw_status=stream['status'], status='MARKET_CLOSED', schedule=closure,
                          last_finalized_close=stored.get('last_close'))
    all_closed = bool(streams) and all(s.get('status') == 'MARKET_CLOSED' for s in streams)
    status = ('NOT_OBSERVED' if observed is None else 'STALE' if not fresh else
              'MARKET_CLOSED' if all_closed else
              'RUNNING' if streams and any(s.get('status') == 'OK' for s in streams)
              and all(s.get('status') in {'OK','MARKET_CLOSED'} for s in streams) else 'DEGRADED')
    finalized = [s['last_finalized_close'] for s in streams
                 if s.get('last_finalized_close') is not None and (all_closed or s.get('status') == 'OK')]
    return {'status': status, 'observed_at': observed,
            'last_finalized_close': min(finalized) if finalized else None,
            'streams': streams, 'execution_eligible': False,
            'raw_status':health.get('status'),
            'closed_instruments':sorted({s['instrument'] for s in streams if s.get('status') == 'MARKET_CLOSED'})}


def load_observation(cur, attributes):
    key, run_id = attributes.get('library_key'), attributes.get('orchestrator_run_id')
    if not key or not run_id:
        return None
    cur.execute('SELECT state FROM research.strategy_library_forward_state WHERE run_id=%s', (run_id,))
    row = cur.fetchone()
    result = observation_status(row[0] if row else None, key)
    cur.execute('''SELECT count(*), count(*) FILTER (
        WHERE payload->'payload'->'provenance'->'research_entry' IS NOT NULL)
        FROM research.strategy_library_forward_event
        WHERE run_id=%s AND strategy_key=%s AND kind='EVALUATION' ''', (run_id,key))
    counts = cur.fetchone()
    result.update({'run_id': run_id, 'evaluations': int(counts[0]), 'accepted_entries': int(counts[1]),
                   'evidence_class': 'COMPLETED_CANDLE_RECONSTRUCTION'})
    return result

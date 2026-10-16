"""Read-only library observer facts, independent of publication/execution gates."""
from datetime import datetime, timezone


def observation_status(state, key, *, now=None):
    health = (state or {}).get('health', {})
    observed = health.get('observed_at')
    streams = [s for s in health.get('streams', []) if s.get('key') == key]
    now = datetime.now(timezone.utc).timestamp() if now is None else now
    fresh = isinstance(observed, (float,int)) and 0 <= now-observed <= 330
    status = ('NOT_OBSERVED' if observed is None else 'STALE' if not fresh else
              'RUNNING' if streams and all(s.get('status') == 'OK' for s in streams) else 'DEGRADED')
    finalized = [s['last_finalized_close'] for s in streams if s.get('last_finalized_close') is not None]
    return {'status': status, 'observed_at': observed,
            'last_finalized_close': min(finalized) if finalized else None,
            'streams': streams, 'execution_eligible': False}


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

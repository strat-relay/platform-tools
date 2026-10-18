"""Read-only library observer facts, independent of publication/execution gates."""
from datetime import datetime, timezone
from copy import deepcopy
from .library_market_schedule import expected_weekend


def observation_status(state, key, *, now=None, asset_classes=None):
    asset_classes = asset_classes or {}
    health = (state or {}).get('health', {})
    observed = health.get('observed_at')
    streams = [deepcopy(s) for s in health.get('streams', []) if s.get('key') == key]
    now = datetime.now(timezone.utc).timestamp() if now is None else now
    fresh = isinstance(observed, (float,int)) and 0 <= now-observed <= 330
    for stream in streams:
        stored = (state or {}).get('streams', {}).get(key+':'+str(stream.get('instrument')), {})
        asset_class = asset_classes.get(stream.get('instrument'))
        stream['asset_class'] = asset_class
        closure = expected_weekend(asset_class, now, stored) if fresh else None
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


def _resolve_run_id(cur, key, run_id=None):
    """Resolve a library instance to its authoritative forward run.

    Older registrations may contain ``library_key`` without copying the run ID
    into instance attributes.  The forward-event ledger is the source of truth;
    never invent a run ID or fall back to generic strategy evaluations here.
    """
    if run_id:
        return run_id
    cur.execute('''SELECT run_id
                   FROM research.strategy_library_forward_event
                   WHERE strategy_key=%s
                   GROUP BY run_id
                   ORDER BY max(observed_at) DESC, run_id DESC
                   LIMIT 1''', (key,))
    row = cur.fetchone()
    return row[0] if row else None


def load_observation(cur, attributes):
    key = attributes.get('library_key')
    run_id = _resolve_run_id(cur, key, attributes.get('orchestrator_run_id')) if key else None
    if not key or not run_id:
        return None
    cur.execute('SELECT state FROM research.strategy_library_forward_state WHERE run_id=%s', (run_id,))
    row = cur.fetchone()
    state = row[0] if row else None
    instruments = sorted({s['instrument'] for s in (state or {}).get('health', {}).get('streams', [])
                          if s.get('key') == key and s.get('instrument')})
    cur.execute("""SELECT canonical_instrument, asset_class
                   FROM platform.instrument_provider_mapping
                   WHERE provider='MT5' AND state='ACTIVE'
                     AND canonical_instrument=ANY(%s)""", (instruments,))
    asset_classes = dict(cur.fetchall())
    result = observation_status(state, key, asset_classes=asset_classes)
    cur.execute('''SELECT count(*), count(*) FILTER (
        WHERE payload->'payload'->'provenance'->'research_entry' IS NOT NULL)
        FROM research.strategy_library_forward_event
        WHERE run_id=%s AND strategy_key=%s AND kind='EVALUATION' ''', (run_id,key))
    counts = cur.fetchone()
    result.update({'run_id': run_id, 'evaluations': int(counts[0]), 'accepted_entries': int(counts[1]),
                   'evidence_class': 'COMPLETED_CANDLE_RECONSTRUCTION'})
    return result

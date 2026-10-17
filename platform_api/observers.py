"""Read-only price action observer state — per-instance evaluator diagnostics."""
from __future__ import annotations
from typing import Any


def list_observers(conn) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT instance_id, strategy_key, state, last_observed_at, updated_at
            FROM research.price_action_observer_state
            ORDER BY updated_at DESC
        """)
        rows = cur.fetchall()

    result = []
    for row in rows:
        instance_id, strategy_key, state, last_observed_at, updated_at = row
        runtime = (state or {}).get("runtime") or {}
        evaluators_raw = (state or {}).get("evaluators") or {}
        evaluators = []
        for key in sorted(evaluators_raw):
            val = evaluators_raw[key]
            snap = (val.get("snapshot") or {})
            seen = snap.get("seen_events") or []
            rc = snap.get("rejection_counts") or {}
            evaluators.append({
                "key": key,
                "instrument": key.split("|", 1)[-1] if "|" in key else key,
                "seen_events": len(seen) if isinstance(seen, list) else 0,
                "live_evaluations": val.get("evaluations", 0),
                "rejection_counts": rc,
                "signal_count": rc.get("ENTRY_OBSERVED_RESEARCH_ONLY", 0),
            })
        result.append({
            "instance_id": instance_id,
            "strategy_key": strategy_key,
            "last_observed_at": last_observed_at.isoformat() if last_observed_at else None,
            "updated_at": updated_at.isoformat() if updated_at else None,
            "status": "RUNNING" if int(runtime.get("successful_instrument_count") or 0) > 0 else "DEGRADED",
            "cache_replay_version": runtime.get("cache_replay_version"),
            "successful_instrument_count": int(runtime.get("successful_instrument_count") or 0),
            "instrument_count": int(runtime.get("instrument_count") or 0),
            "evaluators": evaluators,
        })
    return result

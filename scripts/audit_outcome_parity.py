"""Read-only parity audit for the unified outcome resolver.

This script deliberately never calls ``persist_outcome_row``.  It compares the
current canonical outcome row with a fresh candle replay and emits one JSON
record per signal, including a conservative classification for differences.
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone

from outcome_resolver import EvaluationContract, resolve_candle_path
from outcome_resolver_runtime import ResolverRedisCandleStore, ResolverSignal


REQUIRED_CONTRACT_FIELDS = ("version", "timeframe_minutes", "activation",
                            "time_exit_price", "price_basis")


def contract_gap(signal: dict) -> list[str]:
    raw = (signal.get("strategy_metadata") or {}).get("outcome_contract")
    if not isinstance(raw, dict):
        return ["strategy_metadata.outcome_contract"]
    missing = [field for field in REQUIRED_CONTRACT_FIELDS if raw.get(field) in (None, "")]
    if raw.get("version") and not str(raw["version"]).startswith("entry-outcome."):
        missing.append("outcome_contract.version_supported")
    return missing


def classify(existing, replay):
    if replay.resolution_state in {"INSUFFICIENT_DATA", "AMBIGUOUS_INTRABAR"}:
        return "INSUFFICIENT_EVIDENCE"
    if replay.status == existing.get("status") and replay.exit_timestamp == existing.get("exit_timestamp"):
        return "MATCH"
    if existing.get("status") in (None, "OPEN") and replay.status != "OPEN":
        return "INTENDED_SEMANTIC_CORRECTION"
    if replay.status != existing.get("status"):
        return "REGRESSION"
    return "UNRESOLVED"


def _dsn():
    return os.environ.get("DATABASE_URL") or os.environ.get("TRADING_POSTGRES_DSN")


def run(limit: int, strategies: list[str], *, prospective: bool = False) -> list[dict]:
    import psycopg
    dsn = _dsn()
    if not dsn:
        raise RuntimeError("DATABASE_URL or TRADING_POSTGRES_DSN is required")
    redis_url = os.environ.get("MARKET_DATA_REDIS_URL") or os.environ.get("REDIS_URL")
    if not redis_url:
        raise RuntimeError("MARKET_DATA_REDIS_URL or REDIS_URL is required")
    import redis
    redis_client = redis.Redis.from_url(redis_url, decode_responses=True)
    store = ResolverRedisCandleStore(redis_client)
    placeholders = ",".join(["%s"] * len(strategies))
    sql = f"""SELECT s.signal_id, s.strategy_id, s.instrument, s.direction,
                      s.entry_price, s.stop_price, s.target_price, s.decision_time,
                      s.entry_type, s.strategy_metadata, s.source_provenance,
                      o.status, o.exit_timestamp, o.realized_r
                 FROM strategy.entry_signals s
            LEFT JOIN strategy.entry_signal_outcomes o USING (signal_id)
                WHERE s.strategy_id IN ({placeholders})
             ORDER BY s.decision_time, s.signal_id LIMIT %s"""
    rows = []
    # read_only prevents accidental mutation even if a future query is added.
    with psycopg.connect(dsn, options="-c default_transaction_read_only=on") as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (*strategies, limit))
            rows = cur.fetchall()
    report = []
    for row in rows:
        signal = ResolverSignal.from_row(row[:11])
        contract_missing = contract_gap({"strategy_metadata": signal.strategy_metadata})
        if prospective and contract_missing:
            report.append({"signal_id": signal.signal_id, "strategy_id": signal.strategy_id,
                "entry": {"decision_time": signal.decision_time, "entry_price": signal.entry,
                           "stop_price": signal.stop, "target_price": signal.target,
                           "entry_type": signal.entry_type},
                "classification": "MISSING_CONTRACT",
                "missing_contract_fields": contract_missing,
                "existing": {"status": row[11], "exit_timestamp": row[12],
                             "realized_r": row[13]},
                "resolver": None})
            continue
        contract = EvaluationContract.from_signal({"strategy_id": signal.strategy_id,
            "entry_type": signal.entry_type, "strategy_metadata": signal.strategy_metadata})
        candles = store.candles(signal, contract.timeframe_minutes)
        replay = resolve_candle_path(direction=signal.direction, entry=signal.entry,
            stop=signal.stop, target=signal.target, entry_timestamp=signal.decision_time,
            candles=candles, max_hold_minutes=contract.max_hold_minutes,
            timeframe_minutes=contract.timeframe_minutes,
            expiration_minutes=contract.expiration_minutes,
            activation=contract.activation, time_exit_price=contract.time_exit_price,
            price_basis=contract.price_basis,
            same_candle_priority=contract.same_candle_priority,
            time_exit_priority=contract.time_exit_priority)
        existing = {"status": row[11], "exit_timestamp": row[12],
                    "realized_r": row[13], "resolution_state": None,
                    "resolution_evidence": {}}
        report.append({"signal_id": signal.signal_id, "strategy_id": signal.strategy_id,
            "entry": {"decision_time": signal.decision_time, "entry_price": signal.entry,
                       "stop_price": signal.stop, "target_price": signal.target,
                       "entry_type": signal.entry_type},
            "contract": {"version": contract.version, "activation": contract.activation,
                          "timeframe_minutes": contract.timeframe_minutes,
                          "price_basis": contract.price_basis},
            "activation": {"contract": contract.activation,
                           "resolver": replay.activation_price,
                           "existing": (existing["resolution_evidence"] or {}).get("activated_at")
                                       if isinstance(existing["resolution_evidence"], dict) else None},
            "existing": existing,
            "resolver": {"status": replay.status, "exit_timestamp": replay.exit_timestamp,
                         "exit_price": replay.exit_price, "reason": replay.evidence.get("reason"),
                         "resolution_state": replay.resolution_state,
                         "price_basis": replay.price_basis},
            "classification": classify(existing, replay)})
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument("--strategy", action="append", dest="strategies")
    parser.add_argument("--prospective", action="store_true",
                        help="require a complete versioned contract and never infer legacy semantics")
    args = parser.parse_args()
    strategies = args.strategies or ["CONTEXT_STRUCTURE_RETRACE_V1",
                                      "LIQUIDITY_DISPLACEMENT_SCALP_V1", "KOJO_STRUCTURE_RECLAIM_V3"]
    print(json.dumps({"generated_at": datetime.now(timezone.utc).isoformat(),
                      "read_only": True, "prospective": args.prospective,
                      "persists_outcomes": False,
                      "signals": run(args.limit, strategies, prospective=args.prospective)},
                     default=str, indent=2))


if __name__ == "__main__":
    main()

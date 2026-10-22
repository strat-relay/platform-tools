"""Managed, PostgreSQL-backed read-only runtime for one Price Action observer.

This is a thin runtime adapter around the frozen ``Observer`` and evaluators.  It owns
no signal publication, outbox, account, order, or execution dependency.  PostgreSQL is
the durable checkpoint and evaluation ledger; the local directory is only a restart-safe
staging area for the existing evaluator state during one process cycle.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from market_data_cache.canonical import CanonicalMarketDataReader
from market_data_cache.reader import default_store
from postgres.config import PostgresConfig
from postgres.db import connect
from research.price_action_forward_observer import MANIFEST, Observer, UNIVERSE, now_iso
from research.price_action_metadata import POLICY as MARKET_INPUT_POLICY, bind_rows, tick_size, validate_checkpoint

REQUIRED_HISTORY = 320
CACHE_REPLAY_VERSION = "price-action-cache-causal-v1"


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"price action observer requires {name}")
    return value


def _symbols() -> dict[str, str]:
    raw = os.environ.get("PRICE_ACTION_SYMBOL_MAP", "")
    if raw:
        value = json.loads(raw)
        if not isinstance(value, dict) or not value:
            raise ValueError("PRICE_ACTION_SYMBOL_MAP must be a non-empty JSON object")
        return {str(k): str(v) for k, v in value.items()}
    return {name: name for name in UNIVERSE}


def _completed(rows: Any) -> list[dict[str, Any]]:
    if not isinstance(rows, list):
        return []
    # The bridge marks a forming row explicitly.  Rows without that optional field are
    # accepted because the rates contract historically returns completed rows only.
    return [row for row in rows if isinstance(row, dict)
            and row.get("complete") is not False and row.get("forming") is not True]


def _event_id(instance_id: str, row: dict[str, Any]) -> str:
    return hashlib.sha256((instance_id + "|" + _json(row)).encode()).hexdigest()


def _read_cached_symbol(reader: CanonicalMarketDataReader, canonical: str, broker_symbol: str) -> dict[str, Any]:
    """Read one causal, validated snapshot from the shared market-data cache.

    The collector is the sole bridge reader.  This path deliberately fails closed when the
    cache is missing, stale, shallow, or in recovery, rather than falling back to duplicate
    observer bridge traffic.
    """
    snapshot = reader.get_snapshot(canonical, broker_symbol,
                                   {"H1": REQUIRED_HISTORY, "M15": REQUIRED_HISTORY})
    provider_tick = tick_size(snapshot.symbol_info.get("tick_size"))
    return {
        "symbol_info": snapshot.symbol_info,
        "quote": snapshot.quote,
        "H1": bind_rows(snapshot.bars["H1"].bars, provider_tick),
        "M15": bind_rows(snapshot.bars["M15"].bars, provider_tick),
        "source": snapshot.source,
        "cache_age_seconds": snapshot.cache_age_seconds,
        "latest_completed_open": {
            "H1": snapshot.bars["H1"].latest_completed_timestamp,
            "M15": snapshot.bars["M15"].latest_completed_timestamp,
        },
        "continuity_status": snapshot.continuity_status,
        "gap_status": snapshot.gap_status,
        "recovery_status": snapshot.recovery_status,
    }


def _causal_bars(snapshot: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Expose only completed H1 bars whose close is covered by completed M15 data.

    The cache can observe a newly completed H1 candle before the final constituent M15
    candle is visible. Holding that H1 row until the lower timeframe catches up prevents
    a later M15 increment from arriving behind an already-consumed higher-timeframe event.
    """
    m15 = list(snapshot["M15"])
    latest_m15_close = int(m15[-1]["time"]) + 900 if m15 else -1
    h1 = [row for row in snapshot["H1"] if int(row["time"]) + 3600 <= latest_m15_close]
    return h1, m15


class PostgresLedger:
    def __init__(self, instance_id: str):
        self.instance_id = instance_id

    def load_state(self, conn) -> dict[str, Any] | None:
        with conn.cursor() as cur:
            cur.execute("SELECT state FROM research.price_action_observer_state WHERE instance_id=%s", (self.instance_id,))
            row = cur.fetchone()
            return row[0] if row else None

    def save(self, conn, state: dict[str, Any], events: list[dict[str, Any]], *, observed_at: str) -> None:
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO research.price_action_observer_state
                (instance_id, strategy_key, state, last_observed_at, updated_at)
                VALUES (%s,%s,%s::jsonb,%s,now())
                ON CONFLICT (instance_id) DO UPDATE SET state=EXCLUDED.state,
                strategy_key=EXCLUDED.strategy_key, last_observed_at=EXCLUDED.last_observed_at,
                updated_at=now()""", (self.instance_id, os.environ["PRICE_ACTION_STRATEGY"], _json(state), observed_at))
            for event in events:
                payload = _json(event)
                cur.execute("""INSERT INTO research.price_action_observer_evaluation
                    (event_id, instance_id, strategy_key, instrument, observed_at, payload)
                    VALUES (%s,%s,%s,%s,%s,%s::jsonb) ON CONFLICT (event_id) DO NOTHING""",
                    (_event_id(self.instance_id, event), self.instance_id,
                     os.environ["PRICE_ACTION_STRATEGY"], event.get("instrument"), observed_at, payload))


def run_once() -> dict[str, Any]:
    instance_id = _required("PRICE_ACTION_INSTANCE_ID")
    strategy = _required("PRICE_ACTION_STRATEGY")
    manifest = Path(os.environ.get("PRICE_ACTION_MANIFEST_PATH", str(MANIFEST)))
    symbols = _symbols()
    original_ids = {"7f1d6e5a-5ab3-4b59-8d2f-0c6f9e3d0104", "7f1d6e5a-5ab3-4b59-8d2f-0c6f9e3d0204"}
    if instance_id in original_ids:
        raise ValueError("corrected provider ticks require a new instance; preserve the affected frozen run")
    if json.loads(manifest.read_text()).get("market_input_policy") != MARKET_INPUT_POLICY:
        raise ValueError("corrected observation requires a new manifest with explicit market_input_policy")
    mode = os.environ.get("PRICE_ACTION_ENTRY_MODE", "COMPLETED_CANDLE_RECONSTRUCTION")
    observer_type = Observer
    timing_binding = None
    if mode == "CONTEMPORANEOUS_CANDLE_OPEN":
        from research.price_action_next_open import NextOpenObserver, mode_binding
        frozen = {"7f1d6e5a-5ab3-4b59-8d2f-0c6f9e3d0104", "7f1d6e5a-5ab3-4b59-8d2f-0c6f9e3d0204"}
        if instance_id in frozen or json.loads(manifest.read_text()).get("entry_mode") != mode:
            raise ValueError("next-open mode requires a new instance and explicitly frozen manifest")
        observer_type = NextOpenObserver
        timing_binding = mode_binding(manifest, instance_id, strategy)
    elif mode != "COMPLETED_CANDLE_RECONSTRUCTION":
        raise ValueError("unsupported entry mode")
    cache = default_store()
    reader = CanonicalMarketDataReader(cache, max_age=float(os.environ.get("MARKET_DATA_MAX_AGE_SECONDS", "330")))
    # Ephemeral staging; the authoritative state is PostgreSQL.
    with tempfile.TemporaryDirectory(prefix=f"price-action-{strategy}-") as staging:
        with connect(PostgresConfig.from_env()) as conn:
            ledger = PostgresLedger(instance_id)
            stored = ledger.load_state(conn)
            observer = observer_type(Path(staging), manifest, strategy_filter=strategy)
            if stored:
                validate_checkpoint(stored, binding=observer.input_binding)
                if stored.get("entry_timing_mode", "COMPLETED_CANDLE_RECONSTRUCTION") != mode:
                    raise ValueError("entry mode changed; start a separate instance/run")
                if mode == "CONTEMPORANEOUS_CANDLE_OPEN" and stored.get("entry_timing_binding") != timing_binding:
                    raise ValueError("next-open policy/manifest changed; start a separate instance/run")
                observer.state = stored
            observer.state["entry_timing_mode"] = mode
            if timing_binding:
                observer.state["entry_timing_binding"] = timing_binding
            if observer.state.get("runtime", {}).get("cache_replay_version") != CACHE_REPLAY_VERSION:
                # The first cache-backed cycle must not inherit a checkpoint created by the
                # direct-bridge path: that path could consume H1 before the matching M15 tail
                # was available. Rebuild only the evaluator cursor/bars for this strategy from
                # the shared cache; durable evaluation/entry rows remain append-only and
                # event identity suppression remains authoritative.
                for canonical in symbols:
                    observer.state.get("cursors", {}).pop(canonical, None)
                    observer.state.get("bars", {}).pop(canonical, None)
                    observer.state.get("health", {}).pop(canonical, None)
                    observer.state.get("evaluators", {}).pop(f"{strategy}|{canonical}", None)
                observer.state["runtime"] = {"cache_replay_version": CACHE_REPLAY_VERSION}
            observer.state["status"] = "RUNNING"
            before = observer.state.get("events_count", 0)
            results = []
            emitted: list[dict[str, Any]] = []
            successful = 0
            for canonical, broker_symbol in symbols.items():
                state_before = deepcopy(observer.state) if mode == "CONTEMPORANEOUS_CANDLE_OPEN" else None
                try:
                    snapshot = _read_cached_symbol(reader, canonical, broker_symbol)
                    info, quote = snapshot["symbol_info"], snapshot["quote"]
                    h1, m15 = _causal_bars(snapshot)
                    if not info or info.get("error") or not quote or quote.get("error"):
                        raise RuntimeError(f"market metadata/quote unavailable for {canonical}")
                    result = observer.ingest(canonical, broker_symbol, h1, m15,
                                             observed_at=now_iso(), snapshot_id=_event_id(instance_id, {"i": canonical, "h": h1[-1:], "m": m15[-1:]}),
                                             source_kind="LIVE_MARKET")
                    if mode == "CONTEMPORANEOUS_CANDLE_OPEN" and not result.get("first_snapshot_reconstructed"):
                        from market_data_cache.opening import read_opening
                        opening = read_opening(cache.snapshot(broker_symbol) or {}, canonical, broker_symbol,
                                               "M15", observed_at=time.time())
                        immediate = observer.ingest_open(canonical, broker_symbol, opening,
                                                        observed_at=now_iso(), snapshot_id=_event_id(instance_id, {"open": str(opening)}))
                        result.setdefault("emitted", []).extend(immediate)
                        result["emitted_evaluations"] += len(immediate)
                    for event in result.get("emitted", []):
                        event["data_provenance"] = {
                            "source": snapshot["source"],
                            "cache_age_seconds": snapshot["cache_age_seconds"],
                            "continuity_status": snapshot["continuity_status"],
                            "gap_status": snapshot["gap_status"],
                            "recovery_status": snapshot["recovery_status"],
                            "latest_completed_open": snapshot["latest_completed_open"],
                        }
                    result.update({"market_data_source": snapshot["source"],
                                  "cache_age_seconds": snapshot["cache_age_seconds"],
                                  "h1_bars": len(h1), "m15_bars": len(m15),
                                  "latest_completed_open": snapshot["latest_completed_open"]})
                    results.append(result)
                    emitted.extend(result.get("emitted", []))
                    successful += 1
                except Exception as exc:  # one slow symbol must not hide other read-only coverage
                    if state_before is not None:
                        observer.state = state_before
                    results.append({"instrument": canonical, "broker_symbol": broker_symbol,
                                    "status": "DEGRADED", "error": f"{type(exc).__name__}: {exc}"})
            observer.state["status"] = "RUNNING" if successful else "DEGRADED"
            observer.state["runtime"] = {"last_cycle_at": now_iso(),
                                          "successful_instrument_count": successful,
                                          "instrument_count": len(symbols),
                                          "degraded_instrument_count": len(symbols) - successful,
                                          "market_data_source": "REDIS_CANONICAL_CACHE",
                                          "cache_replay_version": CACHE_REPLAY_VERSION,
                                          "bridge_requests": 0,
                                          "overlapping_cycles": 0}
            observer.state["events_count"] = before + len(emitted)
            observed_at = now_iso()
            ledger.save(conn, observer.state, emitted, observed_at=observed_at)
            if emitted:
                from infrastructure.messaging.runtime_events import notify_ready
                notify_ready([_event_id(instance_id, event) for event in emitted], conn=conn)
            conn.commit()
            return {"instance_id": instance_id, "strategy": strategy,
                    "market_data_source": "REDIS_CANONICAL_CACHE", "bridge_requests": 0,
                    "observed_at": observed_at, "instruments": results,
                    "successful_instrument_count": successful,
                    "execution_disabled": True, "evaluations_persisted": len(emitted)}


def health() -> dict[str, Any]:
    instance_id = _required("PRICE_ACTION_INSTANCE_ID")
    with connect(PostgresConfig.from_env(), readonly=True) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT strategy_key, state, last_observed_at, updated_at FROM research.price_action_observer_state WHERE instance_id=%s", (instance_id,))
            row = cur.fetchone()
    successful = int((row[1].get("runtime") or {}).get("successful_instrument_count", 0)) if row else 0
    return {"status": "healthy" if successful else ("degraded" if row else "starting"), "instance_id": instance_id,
            "strategy": row[0] if row else os.environ.get("PRICE_ACTION_STRATEGY"),
            "last_observed_at": row[2].isoformat() if row and row[2] else None,
            "updated_at": row[3].isoformat() if row and row[3] else None,
            "successful_instrument_count": successful,
            "execution_disabled": True, "broker_writes": 0}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("run", "once", "health"))
    args = parser.parse_args()
    if args.command == "health":
        result = health(); print(json.dumps(result, default=str))
        if result["status"] != "healthy":
            raise SystemExit(1)
        return
    from infrastructure.messaging.runtime_events import Wakeup
    wakeup = Wakeup(symbols=_symbols().values()).start()
    interval = max(60.0, float(os.environ.get("PRICE_ACTION_POLL_SECONDS", "60")))
    try:
        while True:
            try:
                print(json.dumps(run_once(), default=str), flush=True)
            except Exception as exc:  # fail closed for one cycle; preserve durable state
                print(json.dumps({"status": "degraded", "error": f"{type(exc).__name__}: {exc}", "broker_writes": 0}), flush=True)
            if args.command == "once":
                return
            wakeup.wait(interval)
    finally:
        wakeup.close()


if __name__ == "__main__":
    main()

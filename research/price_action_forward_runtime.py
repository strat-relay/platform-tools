"""Managed, PostgreSQL-backed read-only runtime for one Price Action observer.

This is a thin runtime adapter around the frozen ``Observer`` and evaluators.  It owns
no signal publication, outbox, account, order, or execution dependency.  PostgreSQL is
the durable checkpoint and evaluation ledger; the local directory is only a restart-safe
staging area for the existing evaluator state during one process cycle.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from contracts.mt5_bridge import BridgeEndpoint, CallContext, Mt5ReadClient
from postgres.config import PostgresConfig
from postgres.db import connect
from research.price_action_forward_observer import MANIFEST, Observer, UNIVERSE, now_iso

BRIDGE_URL = "http://10.10.10.100:22347/mcp"


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
    bridge_url = os.environ.get("PRICE_ACTION_MCP_URL", BRIDGE_URL).strip()
    manifest = Path(os.environ.get("PRICE_ACTION_MANIFEST_PATH", str(MANIFEST)))
    symbols = _symbols()
    # Ephemeral staging; the authoritative state is PostgreSQL.
    with tempfile.TemporaryDirectory(prefix=f"price-action-{strategy}-") as staging:
        client = Mt5ReadClient(BridgeEndpoint(bridge_url, profile="research"), timeout_s=25)
        with connect(PostgresConfig.from_env()) as conn:
            ledger = PostgresLedger(instance_id)
            stored = ledger.load_state(conn)
            observer = Observer(Path(staging), manifest, strategy_filter=strategy)
            if stored:
                observer.state = stored
            observer.state["status"] = "RUNNING"
            before = observer.state.get("events_count", 0)
            results = []
            emitted: list[dict[str, Any]] = []
            successful = 0
            for canonical, broker_symbol in symbols.items():
                try:
                    info = client.symbol_info(broker_symbol, CallContext(origin="PRICE_ACTION_RESEARCH", priority="BACKGROUND"))
                    quote = client.quote(broker_symbol, CallContext(origin="PRICE_ACTION_RESEARCH", priority="BACKGROUND"))
                    h1 = _completed((client.rates(broker_symbol, "H1", 320,
                                                   CallContext(origin="PRICE_ACTION_RESEARCH", priority="BACKGROUND")) or {}).get("rates", []))
                    m15 = _completed((client.rates(broker_symbol, "M15", 320,
                                                    CallContext(origin="PRICE_ACTION_RESEARCH", priority="BACKGROUND")) or {}).get("rates", []))
                    if not info or info.get("error") or not quote or quote.get("error"):
                        raise RuntimeError(f"market metadata/quote unavailable for {canonical}")
                    result = observer.ingest(canonical, broker_symbol, h1, m15,
                                             observed_at=now_iso(), snapshot_id=_event_id(instance_id, {"i": canonical, "h": h1[-1:], "m": m15[-1:]}),
                                             source_kind="LIVE_MARKET")
                    results.append(result)
                    emitted.extend(result.get("emitted", []))
                    successful += 1
                except Exception as exc:  # one slow symbol must not hide other read-only coverage
                    results.append({"instrument": canonical, "broker_symbol": broker_symbol,
                                    "status": "DEGRADED", "error": f"{type(exc).__name__}: {exc}"})
            observer.state["status"] = "RUNNING" if successful else "DEGRADED"
            observer.state["runtime"] = {"last_cycle_at": now_iso(),
                                          "successful_instrument_count": successful,
                                          "instrument_count": len(symbols),
                                          "degraded_instrument_count": len(symbols) - successful}
            observer.state["events_count"] = before + len(emitted)
            observed_at = now_iso()
            ledger.save(conn, observer.state, emitted, observed_at=observed_at)
            conn.commit()
            return {"instance_id": instance_id, "strategy": strategy, "bridge": bridge_url,
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
    interval = max(60.0, float(os.environ.get("PRICE_ACTION_POLL_SECONDS", "60")))
    while True:
        try:
            print(json.dumps(run_once(), default=str), flush=True)
        except Exception as exc:  # fail closed for one cycle; preserve durable state
            print(json.dumps({"status": "degraded", "error": f"{type(exc).__name__}: {exc}", "broker_writes": 0}), flush=True)
        if args.command == "once":
            return
        time.sleep(interval)


if __name__ == "__main__":
    main()

"""Read-only forward observer for the frozen Price Action V1 evaluators.

This module deliberately owns no signal, outbox, broker, account, or execution
dependency.  It consumes completed H1/M15 rows from the research MT5 bridge,
uses the frozen evaluators, and stores only a local research ledger.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import math
import os
import signal
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from contracts.mt5_bridge import BridgeEndpoint, Mt5ReadClient
from strategy_backtest.models import MarketEvent, fingerprint
from strategy_backtest.price_action_engulfing_continuation import (
    EngulfingContinuationEvaluator, parameter_set as engulf_parameters,
    strategy_version as engulf_version, EVALUATOR_VERSION as ENGULF_EVALUATOR,
    PARAMETER_HASH as ENGULF_PARAMETER_HASH, DEPENDENCY_MANIFEST_HASH as ENGULF_DEP_HASH,
)
from strategy_backtest.price_action_sr_retest import (
    SupportResistanceRetestEvaluator, parameter_set as sr_parameters,
    strategy_version as sr_version, EVALUATOR_VERSION as SR_EVALUATOR,
    PARAMETER_HASH as SR_PARAMETER_HASH, DEPENDENCY_MANIFEST_HASH as SR_DEP_HASH,
)
from strategy_backtest.research_outcomes import (
    POLICY_VERSION, RESOLVER_VERSION, ResearchOutcomeEntry, resolve_research_entry,
)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STATE = ROOT / "docs/research/price_action_forward/state"
MANIFEST = ROOT / "docs/research/price_action_forward/forward_manifest.json"
REPORT = ROOT / "docs/research/price_action_forward/forward_report.json"
UNIVERSE = ("EURUSD", "GBPUSD", "USDJPY", "USDCHF", "USDCAD", "AUDUSD", "NZDUSD",
            "EURJPY", "GBPJPY", "EURGBP", "AUDJPY", "CADJPY", "CHFJPY", "GBPAUD", "GBPCAD")
TF_SECONDS = {"H1": 3600, "M15": 900}
READ_ONLY_TOOLS = frozenset({"mt5_symbol_info", "mt5_quote", "mt5_rates"})
SCHEMA = "price-action-forward-observer-v1"
VERSION = "price-action-forward-observer-v1"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def durable_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    with tmp.open("rb") as fh:
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    fd = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def append_jsonl(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(value, sort_keys=True, default=str) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def source_hash() -> str:
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def identities() -> dict[str, Any]:
    return {"sr_retest": {"strategy_version": "PRICE_ACTION_SUPPORT_RESISTANCE_RETEST@V1",
                           "evaluator_version": SR_EVALUATOR, "parameter_hash": SR_PARAMETER_HASH,
                           "dependency_manifest_hash": SR_DEP_HASH,
                           "parameters": dict(sr_parameters().values)},
            "engulfing_continuation": {"strategy_version": "PRICE_ACTION_ENGULFING_CONTINUATION@V1",
                           "evaluator_version": ENGULF_EVALUATOR, "parameter_hash": ENGULF_PARAMETER_HASH,
                           "dependency_manifest_hash": ENGULF_DEP_HASH,
                           "parameters": dict(engulf_parameters().values)}}


def _valid_row(row: dict[str, Any], timeframe: str) -> tuple[bool, str | None]:
    try:
        ts = int(row["time"]); values = [float(row[k]) for k in ("open", "high", "low", "close")]
    except (KeyError, TypeError, ValueError):
        return False, "MALFORMED_BAR"
    if ts % TF_SECONDS[timeframe] != 0:
        return False, "MISALIGNED_TIMESTAMP"
    if not all(math.isfinite(x) and x > 0 for x in values):
        return False, "INVALID_OHLC"
    o, h, l, c = values
    if h < max(o, c) or l > min(o, c) or l > h:
        return False, "INVALID_OHLC"
    if row.get("complete") is False or row.get("forming") is True:
        return False, "INCOMPLETE_CANDLE"
    return True, None


def normalize_rows(rows: list[dict[str, Any]], timeframe: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return completed, canonical rows and explicit rejection/gap diagnostics."""
    accepted: list[dict[str, Any]] = []
    rejects: list[dict[str, Any]] = []
    seen: dict[int, str] = {}
    for row in rows:
        ok, reason = _valid_row(row, timeframe)
        ts = row.get("time")
        if not ok:
            rejects.append({"time": ts, "reason": reason}); continue
        key = str(ts)
        digest = fingerprint(row)
        if int(ts) in seen:
            rejects.append({"time": ts, "reason": "CONFLICTING_DUPLICATE" if seen[int(ts)] != digest else "DUPLICATE"})
            continue
        seen[int(ts)] = digest
        accepted.append(dict(row))
    accepted.sort(key=lambda x: int(x["time"]))
    for previous, current in zip(accepted, accepted[1:]):
        delta = int(current["time"]) - int(previous["time"])
        if delta != TF_SECONDS[timeframe]:
            rejects.append({"from": previous["time"], "to": current["time"], "reason": "EXPECTED_CLOSURE_GAP" if delta > TF_SECONDS[timeframe] else "OUT_OF_ORDER_OR_OVERLAP", "missing_bars": max(0, delta // TF_SECONDS[timeframe] - 1)})
    return accepted, rejects


def _event(instrument: str, timeframe: str, row: dict[str, Any], snapshot_id: str, source_kind: str) -> MarketEvent:
    step = TF_SECONDS[timeframe]; ts = int(row["time"])
    return MarketEvent(instrument, timeframe, ts, ts + step, float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"]), True, source_kind,
                       {"snapshot_id": snapshot_id, "tick_size": float(row.get("tick_size", 1.0)),
                        "source_timestamp_semantics": "candle_open_utc", "availability_class": source_kind})


def freeze(*, t0: str | None = None, output: Path = MANIFEST) -> dict[str, Any]:
    if output.exists():
        return json.loads(output.read_text())
    t0 = t0 or now_iso()
    payload = {"schema": SCHEMA, "observer_version": VERSION, "freeze_status": "FROZEN_NOT_ACTIVATED",
               "t0": t0, "window_days": 30, "minimum_review_sample_per_strategy": 100,
               "universe_requested": list(UNIVERSE), "healthy_universe": [],
               "instrument_mapping": "resolve only by read-only mt5_symbol_info/quote; no suffix guessing",
               "timeframes": {"context": "H1", "execution": "M15"},
               "timestamp_semantics": "UTC candle-open; evaluator information cutoff is completed candle close; accepted entry is next completed M15 candle open",
               "availability_policy": {"pre_t0": "warmup only; no new entries", "post_t0_missing": "record gap; do not fill", "reconstructed": "label as delayed/reconstructed; never call live", "stale_or_forming": "reject"},
               "outcome_policy": {"policy_version": POLICY_VERSION, "resolver_version": RESOLVER_VERSION, "max_hold": "none", "same_candle": "ambiguous without lower timeframe evidence", "costs": "gross/reference only; no spread, commission, slippage, or net P&L"},
               "strategies": identities(), "source_hash": source_hash(),
               "execution_isolation": {"mode": "RESEARCH_READ_ONLY_LOCAL_PERSISTENCE", "broker_orders": False, "signal_publication": False, "outbox": False, "execution_credentials": False, "allowed_bridge_tools": sorted(READ_ONLY_TOOLS)},
               "activation": {"status": "BLOCKED_PENDING_HEALTH_CHECK", "reason": "manifest is frozen before any observer activation"}}
    payload["manifest_hash"] = fingerprint(payload)
    durable_json(output, payload)
    return payload


class Observer:
    def __init__(self, state_dir: Path = DEFAULT_STATE, manifest: Path = MANIFEST,
                 strategy_filter: str | None = None):
        self.dir = state_dir; self.manifest_path = manifest
        if strategy_filter is not None and strategy_filter not in {"sr_retest", "engulfing_continuation"}:
            raise ValueError(f"unknown strategy filter: {strategy_filter}")
        self.strategy_filter = strategy_filter
        self.manifest = json.loads(manifest.read_text())
        self.state_path = state_dir / "state.json"; self.events_path = state_dir / "events.jsonl"; self.outcomes_path = state_dir / "outcomes.jsonl"
        if self.state_path.exists(): self.state = json.loads(self.state_path.read_text())
        else: self.state = {"schema": SCHEMA, "status": "STOPPED", "t0": self.manifest["t0"], "cursors": {}, "evaluators": {}, "entries": {}, "bars": {}, "health": {}, "counters": Counter()}
        self.state.setdefault("counters", {})
        self.state.setdefault("bars", {})

    def _save(self) -> None: durable_json(self.state_path, self.state)

    def _evaluator(self, strategy: str, instrument: str):
        key = f"{strategy}|{instrument}"
        if key not in self.state["evaluators"]:
            evaluator = SupportResistanceRetestEvaluator() if strategy == "sr_retest" else EngulfingContinuationEvaluator()
            evaluator.initialize(sr_version(), sr_parameters()) if strategy == "sr_retest" else evaluator.initialize(engulf_version(), engulf_parameters())
            self.state["evaluators"][key] = {"snapshot": evaluator.snapshot_state(), "evaluations": 0, "rejections": {}}
        evaluator = SupportResistanceRetestEvaluator() if strategy == "sr_retest" else EngulfingContinuationEvaluator()
        evaluator.initialize(sr_version(), sr_parameters()) if strategy == "sr_retest" else evaluator.initialize(engulf_version(), engulf_parameters())
        evaluator.restore_state(self.state["evaluators"][key]["snapshot"])
        return key, evaluator

    def ingest(self, instrument: str, broker_symbol: str, h1: list[dict[str, Any]], m15: list[dict[str, Any]], *, observed_at: str, snapshot_id: str, source_kind: str = "LIVE_MARKET") -> dict[str, Any]:
        h1_ok, h1_rej = normalize_rows(h1, "H1"); m15_ok, m15_rej = normalize_rows(m15, "M15")
        rows = {"H1": h1_ok, "M15": m15_ok}; cursor = self.state["cursors"].get(instrument, {})
        history = self.state["bars"].setdefault(instrument, {"M15": [], "H1": []})
        for tf, values in rows.items():
            known = {int(row["time"]): row for row in history[tf]}
            known.update({int(row["time"]): row for row in values})
            history[tf] = [known[ts] for ts in sorted(known)]
        new_events = []
        for tf, values in rows.items():
            for row in values:
                if int(row["time"]) > int(cursor.get(tf, -1)): new_events.append(_event(instrument, tf, row, snapshot_id, source_kind))
        new_events.sort(key=lambda e: (e.close_timestamp, 0 if e.timeframe == "H1" else 1))
        first_seen = not cursor
        emitted = []
        strategies = (self.strategy_filter,) if self.strategy_filter else ("sr_retest", "engulfing_continuation")
        for strategy in strategies:
            key, evaluator = self._evaluator(strategy, instrument)
            if first_seen:
                for event in new_events: evaluator.consume_market_event(event)
                evaluator.pending = {}
                self.state["cursors"][instrument] = {tf: max((int(x["time"]) for x in vals), default=-1) for tf, vals in rows.items()}
                self.state["evaluators"][key]["snapshot"] = evaluator.snapshot_state(); continue
            strategy_emitted = 0
            for event in new_events:
                for evaluation in evaluator.consume_market_event(event):
                    row = {"strategy": strategy, "instrument": instrument, "broker_symbol": broker_symbol, "observed_at": observed_at, "source_kind": source_kind, "event": evaluation.to_dict()}
                    emitted.append(row); append_jsonl(self.events_path, row)
                    strategy_emitted += 1
                    reason = evaluation.provenance.get("rejection_reason", "UNKNOWN")
                    self.state["evaluators"][key]["rejections"][reason] = self.state["evaluators"][key]["rejections"].get(reason, 0) + 1
                    if reason == "ENTRY_OBSERVED_RESEARCH_ONLY" and not evaluation.provenance.get("entry_cancelled"):
                        self._register_entry(strategy, instrument, evaluation, snapshot_id)
            self.state["evaluators"][key]["snapshot"] = evaluator.snapshot_state()
            self.state["evaluators"][key]["evaluations"] += strategy_emitted
            self._refresh_outcomes(strategy, instrument, snapshot_id)
        self.state["cursors"][instrument] = {tf: max((int(x["time"]) for x in vals), default=cursor.get(tf, -1)) for tf, vals in rows.items()}
        self.state["health"][instrument] = {"broker_symbol": broker_symbol, "last_observed_at": observed_at, "last_completed_open": {tf: (vals[-1]["time"] if vals else None) for tf, vals in rows.items()}, "rejections": h1_rej + m15_rej, "new_event_count": len(new_events), "first_snapshot_reconstructed": first_seen}
        self._save(); return {"instrument": instrument, "first_snapshot_reconstructed": first_seen,
                              "new_events": len(new_events), "emitted_evaluations": len(emitted),
                              "emitted": emitted, "rejections": h1_rej + m15_rej}

    def _refresh_outcomes(self, strategy: str, instrument: str, snapshot_id: str) -> None:
        bars = self.state["bars"].get(instrument, {}).get("M15", [])
        if not bars:
            return
        expected = []
        for previous, current in zip(bars, bars[1:]):
            delta = int(current["time"]) - int(previous["time"])
            if delta > 900:
                expected.append({"from": int(previous["time"]), "to": int(current["time"]), "missing_bars": delta // 900 - 1})
        prefix = f"{strategy}|{instrument}|"
        for episode_id, stored in self.state["entries"].items():
            if not episode_id.startswith(prefix):
                continue
            entry = ResearchOutcomeEntry(**stored["entry"])
            outcome = resolve_research_entry(entry, bars, expected_gaps=expected)
            status = outcome.status if outcome.status != "OPEN_AT_DATASET_END" else "OPEN_PENDING_FORWARD"
            if stored.get("status") == status and stored.get("last_snapshot_id") == snapshot_id:
                continue
            stored.update({"status": status, "last_snapshot_id": snapshot_id, "last_updated_at": now_iso(), "outcome": outcome.to_dict()})
            if status not in {"OPEN_PENDING_FORWARD"}:
                append_jsonl(self.outcomes_path, {"type": "OUTCOME_UPDATED", "outcome": outcome.to_dict(), "recorded_at": now_iso()})

    def _register_entry(self, strategy: str, instrument: str, evaluation: Any, snapshot_id: str) -> None:
        p = evaluation.provenance; episode_id = f"{strategy}|{instrument}|{evaluation.candidate_id}|{p['information_cutoff']}"
        if episode_id in self.state["entries"]: return
        entry = ResearchOutcomeEntry(episode_id, evaluation.strategy_version, instrument, p["direction"], int(p["information_cutoff"]), float(p["entry_open"]), float(p["stop"]), float(p["target"]), p.get("parameter_hash"), p.get("dependency_manifest_hash"), self.manifest["manifest_hash"], {"data_snapshot_id": snapshot_id, "source": "forward_observer"})
        self.state["entries"][episode_id] = {"entry": entry.__dict__, "status": "OPEN_PENDING_FORWARD"}
        append_jsonl(self.outcomes_path, {"type": "ENTRY_ACCEPTED_SIMULATED", "entry": entry.__dict__, "recorded_at": now_iso()})

    def health(self) -> dict[str, Any]:
        return {"schema": SCHEMA, "status": self.state.get("status"), "observer_running_is_not_health": True,
                "strategy_filter": self.strategy_filter, "manifest_hash": self.manifest["manifest_hash"],
                "health": self.state.get("health", {}), "entry_count": len(self.state.get("entries", {})),
                "execution_isolation": self.manifest["execution_isolation"]}


def audit_order_isolation() -> dict[str, Any]:
    tree = ast.parse(Path(__file__).read_text())
    calls = [node.args[0].value for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in {"call", "call_write"} and node.args and isinstance(node.args[0], ast.Constant)]
    forbidden = sorted(set(calls) & {"mt5_canonical_order_send", "mt5_market_order", "mt5_pending_order", "mt5_close_position", "mt5_trailing_stop"})
    return {"pass": not forbidden, "forbidden_bridge_tools": forbidden, "allowed_bridge_tools": sorted(READ_ONLY_TOOLS), "production_publisher_imported": False}


def main() -> None:
    p = argparse.ArgumentParser(); sub = p.add_subparsers(dest="command", required=True)
    f = sub.add_parser("freeze"); f.add_argument("--t0", default=None); f.add_argument("--output", type=Path, default=MANIFEST)
    h = sub.add_parser("health"); h.add_argument("--mcp-url", default="http://192.168.1.166:22347/mcp")
    r = sub.add_parser("report"); r.add_argument("--state-dir", type=Path, default=DEFAULT_STATE)
    sub.add_parser("audit-order-isolation")
    stop = sub.add_parser("stop"); stop.add_argument("--state-dir", type=Path, default=DEFAULT_STATE)
    args = p.parse_args()
    if args.command == "freeze": print(json.dumps(freeze(t0=args.t0, output=args.output), indent=2)); return
    if args.command == "audit-order-isolation": print(json.dumps(audit_order_isolation(), indent=2)); return
    if args.command == "stop":
        args.state_dir.mkdir(parents=True, exist_ok=True)
        (args.state_dir / "STOP").write_text(now_iso() + "\n", encoding="utf-8")
        print(json.dumps({"status": "STOP_REQUESTED", "scope": str(args.state_dir)}, indent=2)); return
    if args.command == "report": print(json.dumps(Observer(args.state_dir).health(), indent=2)); return
    client = Mt5ReadClient(BridgeEndpoint(args.mcp_url, profile="research")); results = {}
    for canonical in UNIVERSE:
        candidates = (canonical, canonical + "m")
        result = {"canonical": canonical, "status": "UNAVAILABLE"}
        for symbol in candidates:
            try:
                info = client.symbol_info(symbol); quote = client.quote(symbol)
                if info and not info.get("error") and quote and not quote.get("error"):
                    result = {"canonical": canonical, "broker_symbol": symbol, "status": "HEALTHY", "info": info, "quote": quote}; break
            except Exception as exc:
                result["error"] = f"{type(exc).__name__}: {exc}"
        results[canonical] = result
    print(json.dumps({"observer": VERSION, "bridge": args.mcp_url, "read_only": True, "results": results}, indent=2, default=str))


if __name__ == "__main__": main()

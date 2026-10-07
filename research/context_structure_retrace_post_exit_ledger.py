"""Research-only post-exit observation ledger for Context Structure Retrace.

This module observes only already-stopped Context trades.  It reads the compact
Context state and completed M5/M15 bars from the canonical Redis market-data
cache, then appends immutable observation events to a separate research ledger.
It never changes a Context decision, writes to PostgreSQL, calls the bridge, or
imports another strategy.

The fixed horizon is an observation boundary, not a new exit rule.  The
``<0.25R`` field is retained as a diagnostic cohort label only; it is never
used to select, reject, or modify a trade.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
STRATEGY_ID = "CONTEXT_STRUCTURE_RETRACE_V1"
SCHEMA = "context-structure-retrace-post-exit-ledger-v1"
HORIZON_MINUTES = 180
HORIZON_SECONDS = HORIZON_MINUTES * 60
TIMEFRAMES = ("M5", "M15")
TIMEFRAME_SECONDS = {"M5": 300, "M15": 900}
LOW_RR_DIAGNOSTIC_THRESHOLD = 0.25


def iso(ts: int | float) -> str:
    return datetime.fromtimestamp(int(ts), timezone.utc).isoformat()


def epoch(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp())


def canonical_symbol(symbol: Any) -> str:
    value = str(symbol or "")
    return value[:-1] if value.endswith("m") else value


def provider_symbols(symbol: Any) -> tuple[str, ...]:
    value = str(symbol or "")
    canonical = canonical_symbol(value)
    return tuple(dict.fromkeys((value, canonical + "m", canonical)))


def _json_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def _position_rows(state: dict[str, Any]) -> Iterable[dict[str, Any]]:
    if state.get("strategy_version") not in (None, STRATEGY_ID):
        raise ValueError("state is not a Context Structure Retrace state")
    seen: set[str] = set()
    for setup in (state.get("setups") or {}).values():
        for position in setup.get("opportunities", []):
            row = dict(position)
            for field in ("symbol", "direction", "setup_id", "pattern"):
                row.setdefault(field, setup.get(field))
            key = str(row.get("economic_position_id") or row.get("entry_opportunity_id") or "")
            if key and key not in seen:
                seen.add(key)
                yield row
    for position in (state.get("positions") or {}).values():
        row = dict(position)
        key = str(row.get("economic_position_id") or row.get("entry_opportunity_id") or "")
        if key and key not in seen:
            seen.add(key)
            yield row


def stopped_positions(state: dict[str, Any]) -> list[dict[str, Any]]:
    """Select only Context trades whose existing lifecycle says STOPPED."""
    result = []
    for position in _position_rows(state):
        if position.get("status") == "STOPPED" or position.get("exit_reason") == "STOPPED":
            if epoch(position.get("exit_timestamp")) is not None:
                result.append(position)
    return result


def _price_sides(bar: dict[str, Any], contract: dict[str, Any]) -> tuple[float, float, float, float]:
    point = float(contract.get("point", contract.get("tick_size", 0.0)) or 0.0)
    spread = float(bar.get("spread", 0.0) or 0.0) * point
    bid_high, bid_low = float(bar["high"]), float(bar["low"])
    return bid_high, bid_low, bid_high + spread, bid_low + spread


def _favorable(direction: str, price: float, entry: float) -> float:
    return price - entry if direction == "LONG" else entry - price


def _adverse(direction: str, price: float, entry: float) -> float:
    return entry - price if direction == "LONG" else price - entry


def _target_reached(direction: str, target: float, bid_high: float, bid_low: float,
                    ask_high: float, ask_low: float) -> bool:
    return (bid_high >= target) if direction == "LONG" else (ask_low <= target)


def _stop_excursion(direction: str, stop: float, bid_low: float, ask_high: float) -> float:
    return max(0.0, stop - bid_low) if direction == "LONG" else max(0.0, ask_high - stop)


def _bar_observations(position: dict[str, Any], bars: list[dict[str, Any]], timeframe: str,
                      contract: dict[str, Any]) -> list[dict[str, Any]]:
    exit_ts = epoch(position.get("exit_timestamp"))
    if exit_ts is None:
        return []
    end_ts = exit_ts + HORIZON_SECONDS
    entry = float(position["executable_paper_entry"])
    stop = float(position["stop"])
    target = float(position["target"])
    risk = float((position.get("geometry") or {}).get("stop_distance") or abs(entry - stop))
    if risk <= 0:
        raise ValueError("stopped Context trade has non-positive risk distance")
    direction = str(position["direction"])
    observations = []
    for bar in sorted(bars, key=lambda row: int(row["time"])):
        bar_ts = int(bar["time"])
        # A bar containing the exit may have a pre-exit portion.  Start at the
        # next completed candle to avoid attributing pre-stop movement to the
        # post-exit ledger.
        if bar_ts <= exit_ts or bar_ts > end_ts or bar.get("complete") is False:
            continue
        bid_high, bid_low, ask_high, ask_low = _price_sides(bar, contract)
        favorable_price = bid_high if direction == "LONG" else ask_low
        adverse_price = bid_low if direction == "LONG" else ask_high
        favorable = max(0.0, _favorable(direction, favorable_price, entry))
        adverse = max(0.0, _adverse(direction, adverse_price, entry))
        observations.append({
            "bar_time": iso(bar_ts),
            "bar_epoch": bar_ts,
            "timeframe": timeframe,
            "entry_side": "BID",
            "favorable_side": "BID_HIGH" if direction == "LONG" else "ASK_LOW",
            "adverse_side": "BID_LOW" if direction == "LONG" else "ASK_HIGH",
            "favorable_r": favorable / risk,
            "adverse_r": adverse / risk,
            "excursion_beyond_original_stop_r": _stop_excursion(direction, stop, bid_low, ask_high) / risk,
            "original_target_reached": _target_reached(direction, target, bid_high, bid_low, ask_high, ask_low),
        })
    return observations


def _summarize(position: dict[str, Any], timeframe: str, observations: list[dict[str, Any]]) -> dict[str, Any]:
    exit_ts = epoch(position["exit_timestamp"])
    expected_until = exit_ts + HORIZON_SECONDS
    last_ts = max((row["bar_epoch"] for row in observations), default=None)
    target_rows = [row for row in observations if row["original_target_reached"]]
    return {
        "timeframe": timeframe,
        "bars_observed": len(observations),
        "observed_from": observations[0]["bar_time"] if observations else None,
        "observed_until": observations[-1]["bar_time"] if observations else None,
        "expected_until": iso(expected_until),
        "observation_complete": bool(last_ts is not None and last_ts + TIMEFRAME_SECONDS[timeframe] >= expected_until),
        "post_exit_mfe_r": max((row["favorable_r"] for row in observations), default=None),
        "post_exit_mae_r": max((row["adverse_r"] for row in observations), default=None),
        "max_excursion_beyond_original_stop_r": max((row["excursion_beyond_original_stop_r"] for row in observations), default=None),
        "original_target_after_stop": bool(target_rows),
        "time_to_original_target_minutes": ((target_rows[0]["bar_epoch"] - exit_ts) / 60) if target_rows else None,
    }


def observe_stopped_trade(position: dict[str, Any], bars_by_timeframe: dict[str, list[dict[str, Any]]],
                          contract: dict[str, Any]) -> dict[str, Any]:
    """Build a deterministic, read-only observation record from completed bars."""
    if not (position.get("status") == "STOPPED" or position.get("exit_reason") == "STOPPED"):
        raise ValueError("post-exit ledger accepts stopped trades only")
    target_r = (position.get("geometry") or {}).get("target_R")
    observations = {
        timeframe: _bar_observations(position, bars_by_timeframe.get(timeframe, []), timeframe, contract)
        for timeframe in TIMEFRAMES
    }
    summaries = {timeframe: _summarize(position, timeframe, rows) for timeframe, rows in observations.items()}
    preferred = summaries["M5"] if summaries["M5"]["bars_observed"] else summaries["M15"]
    record = {
        "schema": SCHEMA,
        "record_type": "CONTEXT_STOPPED_POST_EXIT_OBSERVATION",
        "research_only": True,
        "strategy_id": STRATEGY_ID,
        "trade_id": position.get("economic_position_id") or position.get("entry_opportunity_id"),
        "signal_id": position.get("signal_id"),
        "symbol": canonical_symbol(position.get("symbol")),
        "provider_symbol": position.get("symbol"),
        "direction": position.get("direction"),
        "setup_id": position.get("setup_id"),
        "pattern": position.get("pattern"),
        "entry_timestamp": position.get("fill_timestamp_iso") or position.get("fill_timestamp"),
        "exit_timestamp": position.get("exit_timestamp"),
        "entry_price": position.get("executable_paper_entry"),
        "original_stop": position.get("stop"),
        "original_target": position.get("target"),
        "risk_distance": (position.get("geometry") or {}).get("stop_distance"),
        "planned_target_r": target_r,
        "low_rr_diagnostic_cohort": target_r is not None and float(target_r) < LOW_RR_DIAGNOSTIC_THRESHOLD,
        "low_rr_diagnostic_only": True,
        "observations": observations,
        "timeframe_summary": summaries,
        "post_exit_mfe_r": preferred["post_exit_mfe_r"],
        "post_exit_mae_r": preferred["post_exit_mae_r"],
        "max_excursion_beyond_original_stop_r": preferred["max_excursion_beyond_original_stop_r"],
        "original_target_after_stop": preferred["original_target_after_stop"],
        "time_to_original_target_minutes": preferred["time_to_original_target_minutes"],
        "coverage_complete": bool(observations["M5"] or observations["M15"]) and all(
            summary["observation_complete"] for summary in summaries.values() if summary["bars_observed"]
        ),
        "coverage_note": "M5/M15 bars begin after the exit candle to avoid pre-exit leakage; missing bars are not interpolated",
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }
    record["record_hash"] = _json_hash(record)
    return record


class ObservationLedger:
    """Append-only JSONL ledger with idempotent record hashes."""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._known: set[str] = set()
        if self.path.exists():
            # The ledger is append-only and can be hundreds of MB. Stream it so the
            # observer's memory use is bounded by one JSON record, rather than loading
            # the complete file and all hashes at once.
            with self.path.open(encoding="utf-8") as stream:
                for line in stream:
                    if line.strip():
                        self._known.add(json.loads(line).get("record_hash", ""))

    def append(self, record: dict[str, Any]) -> bool:
        if record["record_hash"] in self._known:
            return False
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True, separators=(",", ":"), default=str) + "\n")
        self._known.add(record["record_hash"])
        return True


class PostgresObservationLedger:
    """PostgreSQL-backed ledger. Idempotent via record_hash PRIMARY KEY + ON CONFLICT DO NOTHING."""

    def __init__(self, dsn: str):
        self._dsn = dsn

    def _connect(self) -> Any:
        try:
            import psycopg
        except ImportError as exc:
            raise RuntimeError("psycopg is required for PostgresObservationLedger") from exc
        return psycopg.connect(self._dsn, autocommit=False)

    def append(self, record: dict[str, Any]) -> bool:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO research.context_post_exit_observations
                           (record_hash, signal_id, trade_id, record_type, observed_at, payload)
                       VALUES (%s, %s, %s, %s, %s, %s)
                       ON CONFLICT (record_hash) DO NOTHING
                       RETURNING record_hash""",
                    (
                        record.get("record_hash", ""),
                        record.get("signal_id"),
                        record.get("trade_id"),
                        record.get("record_type", ""),
                        record.get("observed_at"),
                        json.dumps(record, sort_keys=True, default=str),
                    ),
                )
                inserted = cur.fetchone() is not None
            conn.commit()
        return inserted


def _cached_bars(store: Any, symbol: str) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]], str]:
    for candidate in provider_symbols(symbol):
        metadata = store.metadata(candidate)
        bars = {timeframe: store.bars(candidate, timeframe) for timeframe in TIMEFRAMES}
        if metadata and any(bars.values()):
            return metadata.get("symbol_info") or {}, bars, candidate
    raise RuntimeError(f"Context post-exit market data unavailable for {symbol}")


def observe_state(state_path: Path, ledger_path: Path, store: Any) -> dict[str, Any]:
    state = json.loads(state_path.read_text(encoding="utf-8"))
    pg_dsn = os.environ.get("TRADING_POSTGRES_DSN", "")
    ledger: ObservationLedger | PostgresObservationLedger = (
        PostgresObservationLedger(pg_dsn) if pg_dsn else ObservationLedger(ledger_path)
    )
    records = []
    gaps = []
    for position in stopped_positions(state):
        try:
            contract, bars, provider = _cached_bars(store, str(position.get("symbol")))
            record = observe_stopped_trade(position, bars, contract)
            record["provider_symbol"] = provider
            record["record_hash"] = _json_hash({key: value for key, value in record.items() if key != "record_hash"})
            ledger.append(record)
            records.append(record)
        except Exception as exc:
            gap = {"schema": SCHEMA, "record_type": "CONTEXT_STOPPED_POST_EXIT_DATA_GAP",
                   "research_only": True, "strategy_id": STRATEGY_ID,
                   "trade_id": position.get("economic_position_id"), "signal_id": position.get("signal_id"),
                   "symbol": canonical_symbol(position.get("symbol")), "provider_symbol": position.get("symbol"),
                   "reason": f"{type(exc).__name__}: {exc}",
                   "low_rr_diagnostic_cohort": ((position.get("geometry") or {}).get("target_R") is not None and
                                                  float((position.get("geometry") or {}).get("target_R")) < LOW_RR_DIAGNOSTIC_THRESHOLD),
                   "low_rr_diagnostic_only": True}
            gap["record_hash"] = _json_hash(gap)
            ledger.append(gap)
            gaps.append({"trade_id": gap["trade_id"], "symbol": gap["provider_symbol"], "reason": gap["reason"]})
    return {"schema": SCHEMA, "research_only": True, "strategy_id": STRATEGY_ID,
            "stopped_trade_count": len(stopped_positions(state)), "records_written": len(records),
            "data_gaps": gaps, "ledger_path": str(ledger_path),
            "low_rr_diagnostic_cohort_is_non_operational": True}


def default_paths() -> tuple[Path, Path]:
    state_dir = Path(os.environ.get("CONTEXT_RUNNER_STATE_DIR") or ROOT)
    ledger_dir = Path(os.environ.get("CONTEXT_POST_EXIT_LEDGER_DIR") or state_dir)
    return state_dir / "context_structure_retrace_forward_state_compact.json", ledger_dir / "context_structure_retrace_post_exit.jsonl"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    observe = sub.add_parser("observe")
    observe.add_argument("--state", type=Path)
    observe.add_argument("--ledger", type=Path)
    watch = sub.add_parser("watch")
    watch.add_argument("--state", type=Path)
    watch.add_argument("--ledger", type=Path)
    watch.add_argument("--interval", type=int, default=60)
    args = parser.parse_args()
    state_path, ledger_path = default_paths()
    if args.state:
        state_path = args.state
    if args.ledger:
        ledger_path = args.ledger
    from market_data_cache.reader import default_store
    if args.command == "observe":
        print(json.dumps(observe_state(state_path, ledger_path, default_store()), indent=2))
        return
    stop = {"requested": False}
    signal.signal(signal.SIGTERM, lambda *_: stop.__setitem__("requested", True))
    signal.signal(signal.SIGINT, lambda *_: stop.__setitem__("requested", True))
    while not stop["requested"]:
        try:
            print(json.dumps(observe_state(state_path, ledger_path, default_store()), separators=(",", ":")), flush=True)
        except Exception as exc:
            print(json.dumps({"schema": SCHEMA, "research_only": True, "strategy_id": STRATEGY_ID,
                              "observer_error": f"{type(exc).__name__}: {exc}"}), flush=True)
        time.sleep(max(5, args.interval))


if __name__ == "__main__":
    main()

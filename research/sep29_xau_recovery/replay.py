"""Read-only Sep-29 XAUUSDm replay against a paged native-MT5 export.

This is an evidence runner: it writes only the replay report and never invokes an execution
client or broker-write tool.  Strategy configuration and rule modules are imported unchanged.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def load_rows(root: Path, timeframe: str) -> list[dict]:
    path = root / "XAUUSDm" / f"{timeframe}.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def ts(value: str) -> int:
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())


def replay_context(root: Path, start: int, end: int) -> dict:
    import context_structure_retrace_forward as runner

    # Keep the runner's append-only event/state side effects in an isolated temporary directory.
    temp = Path(tempfile.mkdtemp(prefix="sep29-context-replay-"))
    runner.STATE = temp / "state.json"
    runner.EVENTS = temp / "events.jsonl"
    runner.HEARTBEAT = temp / "heartbeat.json"
    runner._EVENT_IDENTITIES = set()
    runner._EVENT_IDENTITY_CACHE_PATH = None

    series = {tf: load_rows(root, tf) for tf in ("M5", "M15", "H1", "H4")}
    m5 = [row for row in series["M5"] if start <= int(row["time"]) <= end]
    warm_m5 = [row for row in series["M5"] if int(row["time"]) < start]
    if not warm_m5:
        raise RuntimeError("replay has no warmup M5 history")
    state = runner.empty_state()
    contract = {"symbol": "XAUUSDm", "point": 0.001, "tick_size": 0.001, "digits": 3}

    def snapshot(until: int) -> dict[str, list[dict]]:
        return {tf: [row for row in series[tf] if int(row["time"]) <= until]
                for tf in ("M5", "M15", "H1", "H4")}

    baseline = warm_m5[-1]
    close = float(baseline["close"])
    spread = float(baseline.get("spread", 0)) * 0.001
    runner.process_symbol(state, "XAUUSDm", contract,
                          {"bid": close - spread / 2, "ask": close + spread / 2, "time": int(baseline["time"])},
                          snapshot(int(baseline["time"])))
    for bar in m5:
        bar_time = int(bar["time"])
        close = float(bar["close"])
        spread = float(bar.get("spread", 0)) * 0.001
        runner.process_symbol(state, "XAUUSDm", contract,
                              {"bid": close - spread / 2, "ask": close + spread / 2, "time": bar_time},
                              snapshot(bar_time))

    setups = [setup for setup in state["setups"].values() if setup.get("symbol") == "XAUUSDm"]
    longs = [setup for setup in setups if setup.get("direction") == "LONG"]
    long_fills = [position for setup in longs for position in setup.get("opportunities", [])]
    return {
        "long_detected": bool(longs),
        "first_long_time": min((int(setup["setup_timestamp"]) for setup in longs), default=None),
        "long_setup_count": len(longs),
        "long_fill_count": len(long_fills),
        "long_fills": [{"time": int(position["fill_timestamp"]), "status": position.get("status")}
                       for position in long_fills],
        "event_count": state["counters"]["events"],
        "setup_count": len(setups),
    }


def replay_liquidity(root: Path, start: int, end: int) -> dict[str, dict]:
    from liquidity_market_data import LiveMarketSnapshot
    from orchestration.liquidity_live import PARAMETER_SETS, LiquidityLiveEvaluator

    m5 = load_rows(root, "M5")
    m15 = load_rows(root, "M15")
    replay_m5 = tuple(row for row in m5 if int(row["time"]) <= end)
    replay_m15 = tuple(row for row in m15 if int(row["time"]) <= end)
    last = replay_m5[-1]
    spread = float(last.get("spread", 0)) * 0.001
    close = float(last["close"])
    snapshot = LiveMarketSnapshot(replay_m5, replay_m15,
                                  {"bid": close - spread / 2, "ask": close + spread / 2, "time": int(last["time"])},
                                  {"symbol": "XAUUSDm", "point": 0.001, "tick_size": 0.001, "digits": 3},
                                  "XAUUSD", "XAUUSDm", datetime.fromtimestamp(int(last["time"]), timezone.utc).isoformat().replace("+00:00", "Z"),
                                  validated_by="market-data-cache")
    result = {}
    for instance_id in ("liquidity-xau-base", "liquidity-xau33"):
        evaluator = LiquidityLiveEvaluator(PARAMETER_SETS[instance_id])
        evaluator.evaluate(snapshot, evaluation_time=datetime.fromtimestamp(end, timezone.utc).isoformat().replace("+00:00", "Z"))
        entered = [setup for setup in evaluator.export_state() if setup.get("state") == "ENTERED" and setup.get("entry_signal_id")]
        sep29 = [setup for setup in entered if start <= int(setup.get("sweep_time", 0)) <= end]
        result[instance_id] = {"long_detected": any(setup.get("direction") == "LONG" for setup in sep29),
                               "signal_count": len(sep29),
                               "first_long_time": min((int(setup["sweep_time"]) for setup in sep29
                                                        if setup.get("direction") == "LONG"), default=None),
                               "entered_setups": len(entered),
                               "sep29_setups": len(sep29),
                               "states": {state: sum(1 for setup in evaluator.export_state() if setup.get("state") == state)
                                          for state in ("PENDING_RETRACE", "ENTERED", "EXPIRED")}}
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--start", default="2026-09-29T00:00:00Z")
    parser.add_argument("--end", default="2026-09-30T00:00:00Z")
    args = parser.parse_args()
    start, end = ts(args.start), ts(args.end)
    m5 = load_rows(args.dataset, "M5")
    in_window = [row for row in m5 if start <= int(row["time"]) < end]
    continuity = [int(row["time"]) for row in in_window]
    missing = [value for value in range(continuity[0], continuity[-1] + 300, 300) if value not in set(continuity)] if continuity else []
    metadata = json.loads((args.dataset / "XAUUSDm" / "M5.metadata.json").read_text())
    report = {"read_only": True, "broker_writes": 0, "dataset": str(args.dataset.resolve()),
              "window_start": args.start, "window_end": args.end, "m5_candle_count": len(in_window),
              "m5_continuity_valid": metadata.get("unexpected_gap_count", 1) == 0,
              "m5_missing_timestamps": missing[:100], "m5_gap_ledger": metadata.get("gap_ledger", []),
              "context": replay_context(args.dataset, start, end),
              "liquidity": replay_liquidity(args.dataset, start, end)}
    output = args.dataset / "replay_report.json"
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

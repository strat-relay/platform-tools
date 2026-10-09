"""Run KOJO_STRUCTURE_RECLAIM_V3 discovery backtest on the frozen canonical dataset.

Same frozen canonical source and discovery window as V1/V2 runs:
  xauusd_study_canonical.json  (XAUUSDm, MT5/Exness)
  window: 2026-07-01 to 2026-08-09  (1782864000 – 1786319999)

V3 source fidelity status:
  SOURCE_FIDELITY_BLOCKED  = false
  READY_FOR_DISCOVERY      = true
  VALIDATION_OUTCOMES_ACCESSED = false
  PARAMETER_SEARCH = false
  BROKER_WRITES = 0
  PRODUCTION_CHANGED = false
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from strategy_backtest import (
    BacktestArtifactStore,
    BacktestEngine,
    CostModel,
    HistoricalMarketFeed,
    MarketEvent,
    StrategyVersion,
    StrategyEvaluatorRegistry,
    register_builtin_evaluators,
)
from strategy_backtest.kojo_structure_reclaim_v3 import (
    STRATEGY_ID,
    VERSION,
    EVALUATOR_KEY,
    SOURCE_FIDELITY_BLOCKED,
    READY_FOR_DISCOVERY,
    kojo_structure_reclaim_v3_parameter_schema,
    kojo_structure_reclaim_v3_baseline_parameter_set,
)

UTC = timezone.utc
DISCOVERY_START = 1782864000  # 2026-07-01 00:00:00 UTC
DISCOVERY_END   = 1786319999  # 2026-08-09 23:59:59 UTC
H1_SECONDS = 3600
M15_SECONDS = 900


def iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat()


def load_canonical(path: Path) -> tuple[dict, str]:
    raw = path.read_bytes()
    return json.loads(raw), hashlib.sha256(raw).hexdigest()


def build_events(canonical: dict, source_sha256: str) -> list[MarketEvent]:
    """Extract H1 and M15 events from the canonical multi-timeframe JSON.

    Only returns events within the discovery window. Validation events are NOT accessed.
    """
    prov = canonical.get("provenance", {})
    broker_symbol = prov.get("broker_symbol", "XAUUSDm")
    provenance_base = {
        "provider": "MT5",
        "provider_symbol": broker_symbol,
        "timezone": "UTC",
        "source_sha256": source_sha256,
    }

    events: list[MarketEvent] = []

    for tf, key, seconds in (("H1", "h1_bars", H1_SECONDS), ("M15", "m15_bars", M15_SECONDS)):
        rows = canonical.get(key, [])
        if not rows:
            continue
        for row in sorted(rows, key=lambda r: int(r["time"])):
            t = int(row["time"])
            if t < DISCOVERY_START or t + seconds > DISCOVERY_END + 1:
                continue
            o, h, lo, c = float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"])
            if h < max(o, c) or lo > min(o, c) or lo > h:
                continue  # skip malformed bars
            events.append(MarketEvent(
                canonical_instrument="XAUUSD",
                timeframe=tf,
                open_timestamp=t,
                close_timestamp=t + seconds,
                open=o, high=h, low=lo, close=c,
                completed=True,
                source="MT5_TERMINAL_HISTORY",
                provenance={**provenance_base},
            ))

    if not events:
        raise ValueError("no discovery events after filtering")

    # HistoricalMarketFeed requires (open_timestamp, canonical_instrument, timeframe) order
    events.sort(key=lambda e: (e.open_timestamp, e.canonical_instrument, e.timeframe))
    return events


def main() -> None:
    assert not SOURCE_FIDELITY_BLOCKED, "SOURCE_FIDELITY_BLOCKED must be False before discovery"
    assert READY_FOR_DISCOVERY, "READY_FOR_DISCOVERY must be True before discovery"

    parser = argparse.ArgumentParser(description="Run KOJO_STRUCTURE_RECLAIM_V3 discovery backtest")
    parser.add_argument(
        "--canonical",
        type=Path,
        default=Path("/private/tmp/claude-501/-Users-caleb-mt5-native-bridge/4e5c7ef3-7ead-4349-9793-d980ff1d24ec/scratchpad/xauusd_study_canonical.json"),
        help="Frozen canonical data file (same as V1/V2 run)",
    )
    parser.add_argument(
        "--artifact-root",
        type=Path,
        default=Path("artifacts/backtests/kojo-v3-discovery-run-1"),
        help="Directory to write the result artifact",
    )
    parser.add_argument("--dry-run", action="store_true", help="Build events only, do not run backtest")
    args = parser.parse_args()

    canonical, source_sha256 = load_canonical(args.canonical)
    print(f"loaded canonical: sha256={source_sha256[:16]}...", file=sys.stderr)

    events = build_events(canonical, source_sha256)
    h1_count = sum(1 for e in events if e.timeframe == "H1")
    m15_count = sum(1 for e in events if e.timeframe == "M15")
    print(f"events: {len(events)} total ({h1_count} H1, {m15_count} M15)", file=sys.stderr)
    print(f"window: {iso(DISCOVERY_START)} → {iso(DISCOVERY_END)}", file=sys.stderr)

    if args.dry_run:
        print(json.dumps({"dry_run": True, "events": len(events), "h1": h1_count, "m15": m15_count}))
        return

    sv = StrategyVersion(STRATEGY_ID, VERSION, EVALUATOR_KEY, kojo_structure_reclaim_v3_parameter_schema())
    ps = kojo_structure_reclaim_v3_baseline_parameter_set(sv.strategy_version_id)
    print(f"strategy_version_id: {sv.strategy_version_id}", file=sys.stderr)
    print(f"parameter_set_id: {ps.parameter_set_id}", file=sys.stderr)

    feed = HistoricalMarketFeed(
        tuple(events),
        dataset_id=f"mt5-xauusdm-canonical:{source_sha256}",
        partition="DISCOVERY",
        requested_start=DISCOVERY_START,
        requested_end=DISCOVERY_END,
    )
    cost = CostModel("zero-cost-discovery", spread_price=0.0, commission_r=0.0)
    registry = register_builtin_evaluators(StrategyEvaluatorRegistry())
    result = BacktestEngine(registry).run(sv, ps, feed, cost, run_id="kojo-v3-discovery-run-1")

    args.artifact_root.mkdir(parents=True, exist_ok=True)
    result_path = BacktestArtifactStore(args.artifact_root).write(result)
    print(f"artifact: {result_path}", file=sys.stderr)

    signals = result.signals
    diag = result.diagnostics or {}
    metrics = result.metrics or {}

    overall = metrics.get("instrument", {}).get("XAUUSD", metrics)
    signal_count = len(signals)
    rejection_counts = diag.get("rejection_counts", {})
    weak_m15 = diag.get("weak_m15_rejections", 0)
    consumed_keys = diag.get("consumed_opportunity_keys_count", 0)

    # Episode deduplication: unique (structural_level_id, direction) in signals
    episode_keys = {
        (s.provenance.get("structural_level_id"), s.provenance.get("direction"))
        for s in signals
    }

    # TP1 planned R distribution
    planned_rs = sorted(
        r for s in signals
        if (r := (s.provenance.get("tp1_provenance") or {}).get("planned_r")) is not None
    )
    p10     = planned_rs[max(0, len(planned_rs) // 10 - 1)]  if planned_rs else None
    median_r = planned_rs[len(planned_rs) // 2]              if planned_rs else None
    p90     = planned_rs[int(len(planned_rs) * 0.9)]          if planned_rs else None

    # Reaction zone evidence summary
    total_wick_reactions = sum(
        (s.provenance.get("tp1_provenance") or {}).get("wick_reaction_count", 0)
        for s in signals
    )
    total_body_reactions = sum(
        (s.provenance.get("tp1_provenance") or {}).get("body_close_rejection_count", 0)
        for s in signals
    )
    signals_with_tp2 = sum(
        1 for s in signals
        if (s.provenance.get("tp1_provenance") or {}).get("tp2_objective") is not None
    )

    # Spread diagnostic
    spread_col = canonical.get("h1_bars", [{}])[0].get("spread")

    summary = {
        # ── mandatory flags (spec section J) ───────────────────────────────
        "VALIDATION_OUTCOMES_ACCESSED": False,
        "PARAMETER_SEARCH": False,
        "BROKER_WRITES": 0,
        "SOURCE_FIDELITY_BLOCKED": SOURCE_FIDELITY_BLOCKED,
        "READY_FOR_DISCOVERY": READY_FOR_DISCOVERY,
        # ── run identity ───────────────────────────────────────────────────
        "strategy_version_id": sv.strategy_version_id,
        "parameter_set_id": ps.parameter_set_id,
        "parameter_set_fingerprint": ps.fingerprint,
        "source_sha256": source_sha256,
        "discovery_start": iso(DISCOVERY_START),
        "discovery_end": iso(DISCOVERY_END),
        "h1_bars": h1_count,
        "m15_bars": m15_count,
        # ── signal counts ──────────────────────────────────────────────────
        "TOTAL_ACCEPTED_SIGNALS": signal_count,
        "TOTAL_UNIQUE_EPISODES_WITH_SIGNAL": len(episode_keys),
        # ── V3 reaction zone invariants ────────────────────────────────────
        "TP1_CLASS_CURRENT_DAY_M15_REACTION_ZONE": all(
            s.provenance.get("tp1_class") == "CURRENT_DAY_M15_REACTION_ZONE"
            for s in signals
        ),
        "TP1_PLANNED_R_ALL_GTE_1R": all(
            (s.provenance.get("tp1_provenance") or {}).get("planned_r", 0) >= 1.0
            for s in signals
        ),
        "TOTAL_WICK_REACTION_EVIDENCE": total_wick_reactions,
        "TOTAL_BODY_CLOSE_EVIDENCE": total_body_reactions,
        "SIGNALS_WITH_TP2": signals_with_tp2,
        # ── V3 retirement invariants ───────────────────────────────────────
        "CONSUMED_OPPORTUNITY_KEYS": consumed_keys,
        # ── rejection breakdown ────────────────────────────────────────────
        "REJECTED_NO_QUALIFYING_TP1_REACTION_ZONE": rejection_counts.get("NO_QUALIFYING_TP1_REACTION_ZONE", 0),
        "REJECTED_INVALID_STOP_GEOMETRY": rejection_counts.get("INVALID_STOP_GEOMETRY", 0),
        "REJECTED_INVALID_SIGNAL_GEOMETRY": rejection_counts.get("INVALID_SIGNAL_GEOMETRY", 0),
        "WEAK_M15_REJECTIONS": weak_m15,
        # ── TP1 planned R distribution ─────────────────────────────────────
        "TP1_PLANNED_R_P10": p10,
        "TP1_PLANNED_R_MEDIAN": median_r,
        "TP1_PLANNED_R_P90": p90,
        # ── spread diagnostic ──────────────────────────────────────────────
        "SPREAD_COLUMN_IN_BARS": spread_col is not None,
        "SPREAD_SAMPLE": spread_col,
        "ZERO_COST_RESULT_IS_NOT_DEPLOYMENT_EVIDENCE": True,
        # ── outcome metrics (discovery only — not validation access) ───────
        "WIN_RATE": overall.get("win_rate"),
        "EXPECTANCY_R": overall.get("expectancy_r"),
        "PROFIT_FACTOR": overall.get("profit_factor"),
        "MAX_DRAWDOWN_R": overall.get("maximum_drawdown_r"),
        "TRADE_COUNT": overall.get("trade_count"),
        # ── result artifact ────────────────────────────────────────────────
        "result_artifact": str(result_path),
    }

    print(json.dumps(summary, indent=2, default=str))


if __name__ == "__main__":
    main()

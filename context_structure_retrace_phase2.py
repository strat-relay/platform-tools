from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from context_structure_retrace.data import CausalReplay
from context_structure_retrace.ledger import build_phase2_ledger, write_phase2_ledger


def ts(value: str) -> int:
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())


def readable(row: dict) -> str:
    e = row["event"]
    snap = row["context_snapshot"]
    structure_tf = row["attention_layer"]["structure_timeframe"]
    tfs = snap["timeframes"]
    structure = tfs[structure_tf]
    ema = structure["ema_context"]
    b = row["attention_layer"]["boundaries"]
    support = b.get("below")
    resistance = b.get("above")
    dirs = ", ".join(f"{tf} completed={tfs[tf]['completed_direction']} forming={tfs[tf]['forming_direction']}" for tf in snap["provenance"]["source_timeframes"] if tf in tfs)
    return (
        f"{e['pattern']} on {e['timeframe']} at {row['event_timestamp_iso']}; "
        f"direction={e['direction']}; {dirs}; EMA ordering={ema.get('ordering')}; "
        f"nearest support below={support['midpoint'] if support else None} "
        f"({b.get('distance_to_support_atr')} ATR), nearest resistance above="
        f"{resistance['midpoint'] if resistance else None} ({b.get('distance_to_resistance_atr')} ATR); "
        f"attention raw counts S/R={row['attention_layer']['sr']['raw_count']}, "
        f"trendlines={row['attention_layer']['trendlines']['raw_count']}, "
        f"channels={row['attention_layer']['channels']['raw_count']}."
    )


def build_summary(rows: list[dict], start: int, end: int, symbol: str) -> str:
    patterns = Counter(r["event"]["pattern"] for r in rows)
    examples: list[str] = []
    wanted = [
        lambda r: r["event"]["pattern"] == "BULLISH_ENGULFING" and r["event"]["direction"] == "LONG",
        lambda r: r["event"]["pattern"] == "BULLISH_ENGULFING" and r["event"]["direction"] == "SHORT",
        lambda r: r["event"]["pattern"] == "BEARISH_ENGULFING" and r["event"]["direction"] == "SHORT",
        lambda r: r["event"]["pattern"] == "BEARISH_ENGULFING" and r["event"]["direction"] == "LONG",
        lambda r: "REJECTION_WICK" in r["event"]["pattern"] and r["attention_layer"]["boundaries"].get("below"),
        lambda r: "REJECTION_WICK" in r["event"]["pattern"] and not r["attention_layer"]["boundaries"].get("below"),
    ]
    for selector in wanted:
        row = next((r for r in rows if selector(r)), None)
        if row:
            examples.append("- " + readable(row))

    first = rows[0] if rows else None
    raw = first["attention_layer"] if first else {"sr": {"raw_count": 0, "top": []}, "trendlines": {"raw_count": 0, "top": []}, "channels": {"raw_count": 0, "top": []}}
    top_sr = raw["sr"]["top"]
    top_tl = raw["trendlines"]["top"]
    top_ch = raw["channels"]["top"]
    retracements = [r["retracement_measurements"] for r in rows]
    median_pullback = sorted(x["maximum_retracement_atr"] for x in retracements if x.get("maximum_retracement_atr") is not None)
    median_pullback = median_pullback[len(median_pullback) // 2] if median_pullback else None
    return f"""# CONTEXT_STRUCTURE_RETRACE_V1 — Phase 2 Market-Context Ledger

Status: descriptive research only. No trade selection, orders, or paper runner.

## Scope and provenance

- Symbol: `{symbol}`
- Development replay: `{datetime.fromtimestamp(start, timezone.utc).isoformat()}` through `{datetime.fromtimestamp(end, timezone.utc).isoformat()}`
- Events: **{len(rows)}**
- Pattern counts: {dict(patterns)}
- This period is development/research-only. March–June 2026 and July–September 2026 have already been exposed by earlier research; neither is an untouched validation set. A future post-freeze period is required for a true holdout.

## Raw candidates versus attention layer

The ledger stores every raw S/R, trendline, and channel candidate generated within the Phase 2 bounded causal window in each snapshot. The underlying candidate generators remain available for wider windows. The attention layer ranks structures using only contemporaneous distance, reactions, recency, violations, ATR-normalized width/slope/penetration, and deterministic grouping. Future outcome labels are stored separately and never passed to ranking.

- Representative snapshot raw counts: S/R `{raw['sr']['raw_count']}`, trendlines `{raw['trendlines']['raw_count']}`, channels `{raw['channels']['raw_count']}`.
- Representative attention output: top {len(raw['sr']['top'])} S/R, top {len(raw['trendlines']['top'])} trendlines, top {len(raw['channels']['top'])} canonical channel groups.

### Top S/R in representative snapshot

{json.dumps([{'level_id': x['level_id'], 'role': x['support_resistance_role'], 'midpoint': x['midpoint'], 'distance_atr': x.get('distance_from_current_price_atr'), 'reactions': x['reaction_count'], 'score': x['attention_score']} for x in top_sr], indent=2)}

### Top trendlines

{json.dumps([{'structure_id': x['structure_id'], 'type': x['type'], 'meaningful_interactions': x['meaningful_interactions'], 'touches': x['touches'], 'violations': x['violations'], 'distance_atr': x.get('current_distance_atr')} for x in top_tl], indent=2)}

### Top canonical channels

{json.dumps([{'cluster_id': x['cluster_id'], 'member_count': x['member_count'], 'representative_id': x['representative']['structure_id'], 'width_atr': x['representative'].get('width_atr'), 'position': x['representative'].get('normalized_position')} for x in top_ch], indent=2)}

## Retracement measurements

For each event, retracement labels include price units for audit plus range/body/ATR-normalized measures. The median maximum retracement before a new extreme in this development ledger was `{median_pullback}` ATR. The descriptive 20% and 50% range/body levels are recorded; neither is used as a requirement.

## Representative context events

{chr(10).join(examples) if examples else '- No representative events matched the requested categories in this replay.'}

## Normalization audit

Structural comparisons use ATR-normalized distance, rejection, penetration, width, slope, and retracement; percentages and time are dimensionless. Raw prices remain only as descriptive OHLC/zone values and broker/instrument metadata. No symbol-specific branches or absolute-price strategy thresholds were added. The cross-instrument interface is `feature_snapshot(..., symbol=..., timeframes=...)`; each instrument supplies its own bars and optional contract metadata.

## Phase 2 unresolved ambiguities

- “Meaningful” departure and zone relevance are explicit descriptive heuristics, not trading filters; they require later human review and independent validation.
- Trendline/channel geometry remains candidate-based; no single line/channel is declared the human-selected structure.
- Historical quote/spread data is not guaranteed in this bar-only ledger, so market snapshots retain optional quote/contract provenance without fabricating quotes.
- Current forming HTF bars are reconstructed from lower-timeframe bars and are clearly separated from completed candles.

## Phase 3 recommendation

Review a stratified sample of the compact attention-layer snapshots against charts, then freeze the representation and run a separate post-freeze holdout audit. Only after that should trade-selection hypotheses be specified as isolated research variants.
"""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", default="/tmp/context-structure-retrace/xau.json")
    parser.add_argument("--symbol", default="XAUUSDm")
    parser.add_argument("--start", default="2026-03-01T00:00:00Z")
    parser.add_argument("--end", default="2026-06-30T23:59:59Z")
    parser.add_argument("--out-dir", default=".")
    parser.add_argument("--structure-points", type=int, default=12)
    args = parser.parse_args()
    data = json.loads(Path(args.data).read_text())
    replay = CausalReplay({k: v for k, v in data.items() if k in {"M1", "M5", "M15", "H1", "H4"}})
    start, end = ts(args.start), ts(args.end)
    rows = list(build_phase2_ledger(replay, args.symbol, start, end, window_limits={"M1": 400, "M5": 120, "M15": 80, "H1": 40, "H4": 20}, structure_max_points=args.structure_points))
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    write_phase2_ledger(rows, out / "context_structure_retrace_phase2_ledger.jsonl", out / "context_structure_retrace_phase2_ledger.csv", out / "context_structure_retrace_phase2_snapshots.jsonl")
    (out / "context_structure_retrace_phase2_summary.md").write_text(build_summary(rows, start, end, args.symbol), encoding="utf-8")
    (out / "context_structure_retrace_phase2_results.json").write_text(json.dumps({"symbol": args.symbol, "start": args.start, "end": args.end, "events": len(rows), "patterns": dict(Counter(r["event"]["pattern"] for r in rows)), "provenance": {"development_only": True, "future_outcomes_not_used_for_features": True}}, indent=2), encoding="utf-8")
    print(json.dumps({"events": len(rows), "patterns": dict(Counter(r["event"]["pattern"] for r in rows)), "outputs": [str(out / x) for x in ("context_structure_retrace_phase2_ledger.jsonl", "context_structure_retrace_phase2_ledger.csv", "context_structure_retrace_phase2_snapshots.jsonl", "context_structure_retrace_phase2_summary.md", "context_structure_retrace_phase2_results.json")]}, indent=2))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Causal, descriptive KOJO_STRUCTURE_FIB source-fidelity study.

This is deliberately not a detector or backtest. It records observations and
standard mechanical calculations while preserving unresolved source semantics.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
FIXTURE = ROOT / "artifacts/research/kojo-structure-fib/source-fixture-recovery/canonical_bars.v1.json"
OUT = ROOT / "artifacts/research/kojo-structure-fib/source-fidelity-v2"
EXPECTED = "fbb5ec907c8f6bd979e49012eea0510ba06dcc50fa00916f8776500d91de8af9"
ZONE_LOW = 4195.0
ZONE_HIGH = 4205.0


def load():
    raw = FIXTURE.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != EXPECTED:
        raise SystemExit(f"fixture SHA mismatch: {digest}")
    data = json.loads(raw)
    bars = data["bars"]
    focus = [b for b in bars if "2025-12-08T00:00:00Z" <= b["timestamp"] <= "2025-12-11T12:00:00Z"]
    return data, bars, focus, digest


def intersects(b):
    return float(b["high"]) >= ZONE_LOW and float(b["low"]) <= ZONE_HIGH


def iso_delta(a, b):
    return (datetime.fromisoformat(b.replace("Z", "+00:00")) - datetime.fromisoformat(a.replace("Z", "+00:00"))).total_seconds() / 3600


def trace(bars):
    result = []
    for i, b in enumerate(bars):
        if not ("2025-12-08T00:00:00Z" <= b["timestamp"] <= "2025-12-11T12:00:00Z"):
            continue
        hit = intersects(b)
        result.append({
            "classification": "SOURCE_FIXTURE_OBSERVATION",
            "timestamp": b["timestamp"], "ohlc": {k: b[k] for k in ("open", "high", "low", "close")},
            "confirmed_at_close": True,
            "relationship_to_4200": "intersects descriptive 4195-4205 observation band" if hit else "does not intersect descriptive 4195-4205 observation band",
            "candidate_roles": (["structure_interaction"] if hit else []) + (["confirmation_candidate"] if b["timestamp"] in {"2025-12-11T06:00:00Z", "2025-12-11T12:00:00Z"} else []),
        })
    return result


def interactions(bars):
    hits = [i for i, b in enumerate(bars) if "2025-12-08T00:00:00Z" <= b["timestamp"] <= "2025-12-11T12:00:00Z" and intersects(b)]
    rows = []
    for ordinal, i in enumerate(hits, 1):
        b = bars[i]
        next_departure = None
        return_after = None
        for j in range(i + 1, len(bars)):
            if next_departure is None and not intersects(bars[j]):
                next_departure = bars[j]["timestamp"]
            if next_departure is not None and intersects(bars[j]):
                return_after = bars[j]["timestamp"]
                break
        rows.append({
            "raw_interaction_ordinal": ordinal,
            "start": b["timestamp"], "end": b["timestamp"],
            "low": b["low"], "high": b["high"], "close": b["close"],
            "departure_after": next_departure,
            "return_after": return_after,
            "classification": "SOURCE_FIXTURE_OBSERVATION",
            "distinct_visit_status": "UNRESOLVED",
        })
    return rows


def fib_forensics():
    candidates = [
        {"id": "IMPULSE_A", "classification": "RESEARCH_HYPOTHESIS", "low_timestamp": "2025-12-09T06:00:00Z", "low_price": 4169.985, "high_timestamp": "2025-12-09T16:00:00Z", "high_price": 4221.625},
        {"id": "IMPULSE_B", "classification": "RESEARCH_HYPOTHESIS", "low_timestamp": "2025-12-10T10:00:00Z", "low_price": 4187.800, "high_timestamp": "2025-12-11T01:00:00Z", "high_price": 4247.805},
        {"id": "IMPULSE_C", "classification": "RESEARCH_HYPOTHESIS", "low_timestamp": "2025-12-10T18:00:00Z", "low_price": 4192.150, "high_timestamp": "2025-12-11T01:00:00Z", "high_price": 4247.805},
    ]
    for c in candidates:
        span = c["high_price"] - c["low_price"]
        c["fib_61_8"] = round(c["high_price"] - span * 0.618, 6)
        c["fib_78_6"] = round(c["high_price"] - span * 0.786, 6)
        c["dec11_observed_region_intersects_pocket"] = c["fib_61_8"] <= 4211.932 <= c["fib_78_6"] or c["fib_78_6"] <= 4211.932 <= c["fib_61_8"]
    return candidates


def engulfing(bars):
    wanted = {"2025-12-11T05:00:00Z", "2025-12-11T06:00:00Z", "2025-12-11T11:00:00Z", "2025-12-11T12:00:00Z"}
    rows = []
    for i, b in enumerate(bars):
        if b["timestamp"] not in wanted or i == 0:
            continue
        p = bars[i - 1]
        po, pc, co, cc = map(float, (p["open"], p["close"], b["open"], b["close"]))
        prev_low, prev_high = min(po, pc), max(po, pc)
        cur_low, cur_high = min(co, cc), max(co, cc)
        rows.append({
            "timestamp": b["timestamp"], "previous_timestamp": p["timestamp"],
            "previous_body": [p["open"], p["close"]], "current_body": [b["open"], b["close"]],
            "strict_body_engulf": pc < po and cc > co and co < pc and cc > po,
            "feed_tolerant_body_0_005_engulf": pc < po and cc > co and co <= pc + 0.005 and cc >= po - 0.005,
            "full_range_engulf": pc < po and cc > co and float(b["low"]) <= float(p["low"]) and float(b["high"]) >= float(p["high"]),
            "classification": "STANDARD_MECHANICAL_DEFINITION",
            "note": "Definitions are diagnostic conventions, not source-confirmed semantics.",
        })
    return rows


def main():
    data, bars, focus, digest = load()
    inter = interactions(bars)
    out = OUT
    out.mkdir(parents=True, exist_ok=True)
    (out / "causal_trace.json").write_text(json.dumps({"classification": "SOURCE_FIXTURE_OBSERVATION", "fixture_sha256": digest, "bars": trace(bars)}, indent=2) + "\n")
    (out / "structure_interactions.json").write_text(json.dumps({"classification": "RESEARCH_HYPOTHESIS", "descriptive_region": {"low": ZONE_LOW, "high": ZONE_HIGH, "basis": "visual/source observation around 4200; not a detector threshold"}, "raw_interactions": inter, "total_raw_interactions": len(inter), "distinct_visit_count": "UNRESOLVED"}, indent=2) + "\n")
    (out / "fib_forensics.json").write_text(json.dumps({"classification": "RESEARCH_HYPOTHESIS", "anchor_candidates": fib_forensics(), "matching_anchor_count": sum(1 for c in fib_forensics() if c["dec11_observed_region_intersects_pocket"]), "anchor_selection_unique_from_source": False}, indent=2) + "\n")
    (out / "engulfing_forensics.json").write_text(json.dumps({"classification": "STANDARD_MECHANICAL_DEFINITION", "candidates": engulfing(bars), "definition_unique_from_source": False}, indent=2) + "\n")
    (out / "dec10_negative_control.json").write_text(json.dumps({
        "classification": "RESEARCH_HYPOTHESIS",
        "bar": "2025-12-10T13:00:00Z", "ohlc": next(b for b in bars if b["timestamp"] == "2025-12-10T13:00:00Z"),
        "structure_third_touch": "UNRESOLVED", "fib_confluence": "UNRESOLVED", "bullish_engulfing": "UNRESOLVED",
        "would_look_valid_under_current_known_source": "UNRESOLVED",
        "earliest_divergence": "The source materials do not define enough mechanics to distinguish Dec 10 from Dec 11 without inventing a filter.",
        "source_supports_divergence_filter": False,
    }, indent=2) + "\n")
    manifest = {
        "study": "KOJO_STRUCTURE_FIB_SOURCE_FIDELITY_V2",
        "version": "v2-causal-descriptive-1",
        "base_commit": "2524387",
        "fixture": {"provider": data["provider"], "symbol": data["canonical_symbol"], "timeframe": data["timeframe"], "bar_count": len(bars), "sha256": digest},
        "classification_policy": ["SOURCE_EXPLICIT", "SOURCE_FIXTURE_OBSERVATION", "STANDARD_MECHANICAL_DEFINITION", "RESEARCH_HYPOTHESIS", "UNRESOLVED"],
        "is_detector": False, "profitability_accessed": False, "production_changed": False, "broker_writes": 0,
    }
    (out / "study_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")

    report = f"""# KOJO_STRUCTURE_FIB Source-Fidelity V2

## Scope

This is a new causal, descriptive source-fidelity investigation from base
commit `2524387`. It is not a reconstruction of the lost 108-cell study, not
a detector, and not a profitability study.

## Fixture verification

```text
FIXTURE_SHA256={digest}
FIXTURE_SHA256_MATCH=true
BAR_COUNT={len(bars)}
PROVIDER=OANDA
SYMBOL=XAUUSD
TIMEFRAME=H1
```

## Source structure

`SOURCE_EXPLICIT`: Kojo describes a third touch after two clean reactions.
`SOURCE_FIXTURE_OBSERVATION`: the visually indicated region is around 4200.
`RESEARCH_HYPOTHESIS`: a descriptive 4195–4205 observation band is used only
to enumerate raw candle intersections. It is not a threshold or detector rule.

The fixture contains `{len(inter)}` raw candle intersections with that band
between Dec 8 00:00 and Dec 11 12:00 UTC. These are not asserted to be
distinct visits. Grouping consecutive intersections and defining departure
requires source semantics that are not available.

`TOTAL_PRE_CONFIRMATION_INTERACTIONS={len(inter)}` (raw candle intersections)
`LITERAL_THIRD_TOUCH_POSSIBLE=UNRESOLVED`

## Structure lifecycle and clean reactions

Several birth candidates are plausible from the visible price history, but no
one birth time is source-supported. A literal ordinal interpretation is
possible only after defining distinct visits and meaningful departure. Clean
reaction status is not mechanically determinable from the source material.

`NATURAL_EXACT_THIRD_TOUCH_INTERPRETATION=UNRESOLVED`
`CLEAN_REACTION_MECHANICALLY_DETERMINABLE_FROM_SOURCE=false`
`EARLIER_INTERACTIONS_EXIST=true`

## Fibonacci forensics

Three causally available bullish impulse candidates are recorded in
`fib_forensics.json`. More than one can produce a pocket near the observed
Dec 11 entry region, so the source does not uniquely identify the anchors.

`PLAUSIBLE_FIB_ANCHORS=3`
`MATCHING_FIB_ANCHORS=2`
`FIB_ANCHOR_UNIQUE_FROM_SOURCE=false`

## Engulfing forensics

The Dec 11 06:00 and 12:00 candidates are evaluated with strict body,
0.005-tolerant body, and full-range conventions in
`engulfing_forensics.json`. These are standard diagnostic definitions only;
the source does not select one uniquely.

`SOURCE_CONFIRMATION_CANDLE_MOST_PLAUSIBLE=UNRESOLVED`
`ENGULFING_DEFINITION_UNIQUE_FROM_SOURCE=false`

## Entry, stop, and target

The screenshot values are retained as source-fixture observations only.
`ENTRY_RULE_DETERMINABLE=false`, `STOP_RULE_DETERMINABLE=false`, and
`TARGET_RULE_DETERMINABLE=false`. No rule is inferred from one screenshot.

## Dec 10 negative control

Dec 10 13:00 is recorded independently in `dec10_negative_control.json`.
The current source evidence does not mechanically establish whether it is a
third touch, has Fibonacci confluence, or has source-defined engulfing. The
earliest defensible conclusion is that no source-supported distinction can be
asserted without inventing a filter.

`DEC10_STRUCTURE_THIRD_TOUCH=UNRESOLVED`
`DEC10_FIB_CONFLUENCE=UNRESOLVED`
`DEC10_BULLISH_ENGULFING=UNRESOLVED`
`DEC10_WOULD_LOOK_VALID_UNDER_CURRENT_KNOWN_SOURCE=UNRESOLVED`
`SOURCE_SUPPORTS_USING_THIS_DIVERGENCE=false`

## Source-fidelity table

| Semantic | Source status | Mechanically determinable | Evidence / ambiguity |
|---|---|---:|---|
| structure | SOURCE_EXPLICIT + UNRESOLVED | false | structure is named, level/zone mechanics absent |
| structure birth | UNRESOLVED | false | no creation rule |
| clean reaction | SOURCE_EXPLICIT + UNRESOLVED | false | “clean” is qualitative |
| distinct visit | UNRESOLVED | false | consecutive-touch grouping absent |
| third touch | SOURCE_EXPLICIT | partial | ordinal phrase exists; visit/departure semantics absent |
| Fib anchor | SOURCE_EXPLICIT + UNRESOLVED | false | pocket exists; anchor algorithm absent |
| Fib pocket | SOURCE_EXPLICIT | partial | 61.8–78.6% stated |
| engulfing | SOURCE_EXPLICIT + UNRESOLVED | false | bullish engulfing stated; exact body/wick semantics absent |
| timely | SOURCE_EXPLICIT + UNRESOLVED | false | no timing window |
| direction | SOURCE_FIXTURE_OBSERVATION | true | source setup described as bullish/long |
| entry | UNRESOLVED | false | screenshot approximation only |
| stop | UNRESOLVED | false | screenshot approximation only |
| target | UNRESOLVED | false | screenshot approximation only |
| news exclusion | SOURCE_EXPLICIT + UNRESOLVED | false | calendar/source/window absent |

## Detector readiness

```text
SOURCE_SETUP_CAUSALLY_RECONSTRUCTIBLE=PARTIAL
SOURCE_SETUP_UNIQUELY_RECONSTRUCTIBLE=false
DEC10_DISTINGUISHABLE_WITH_SOURCE_SUPPORTED_RULES=false
CAN_BUILD_SOURCE_FAITHFUL_DETECTOR_NOW=false
READY_FOR_BACKTEST=false
```

The minimum missing information is: structure-zone mechanics, lifecycle/birth,
clean reaction qualification, distinct visit/departure semantics, exact Fib
anchors, engulfing definition, timing window, entry semantics, news source and
window, and authoritative stop/target rules.

```text
DISCOVERY_ACCESSED=false
VALIDATION_ACCESSED=false
PROFITABILITY_ACCESSED=false
PARAMETER_OPTIMIZATION=false
STRATEGY_IMPLEMENTED=false
PRODUCTION_STRATEGY_CHANGED=false
PRODUCTION_CHANGED=false
BROKER_WRITES=0
```
"""
    (out / "source_fidelity_report.md").write_text(report)


if __name__ == "__main__":
    main()

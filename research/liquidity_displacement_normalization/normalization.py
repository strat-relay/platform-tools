"""Event/opportunity normalization for liquidity-displacement research.

This module is deliberately not imported by any forward runner.  It provides
an auditable identity layer for historical replay and future paper-engine
integration.  Raw candidate observations remain intact; stop interpretations
are represented as hypotheses, never selected by this module.
"""
from __future__ import annotations

import hashlib
from collections import defaultdict
from typing import Any, Iterable


STOP_HYPOTHESES = (
    "PRIMARY_DEEPEST",
    "COMPLETE_SEQUENCE_EXTREME",
    "MOST_RECENT_NESTED",
    "DIRECTLY_RESPONSIBLE_INTERNAL",
)


def _stable(prefix: str, parts: Iterable[Any]) -> str:
    payload = "|".join(str(x) for x in parts)
    return f"{prefix}-{hashlib.sha256(payload.encode()).hexdigest()[:16]}"


def opportunity_key(row: dict[str, Any]) -> tuple[Any, ...]:
    """The shared thesis/price interaction key; no stop fields are included."""
    return (
        row.get("symbol", "BTCUSDm"), row["direction"], round(float(row["sweep_level"]), 8),
        int(row["displacement_index"]), round(float(row["entry_theoretical"]), 8),
    )


def attach_raw_ids(row: dict[str, Any], ordinal: int = 0) -> dict[str, Any]:
    """Return a copy with stable IDs while retaining the raw candidate."""
    out = dict(row)
    symbol = out.get("symbol", "BTCUSDm")
    event_key = opportunity_key(out)
    event_id = _stable("MKT", event_key)
    out["market_event_id"] = event_id
    out["sweep_sequence_id"] = _stable("SWEEP", event_key)
    out["candidate_observation_id"] = _stable("OBS", (event_key, out.get("sweep_timestamp"), ordinal))
    # setup_id is observation identity, not opportunity identity.
    out["setup_id"] = out.get("setup_id") or _stable("SETUP", (symbol, out.get("sweep_timestamp"), ordinal))
    return out


def _selector(group: list[dict[str, Any]], hypothesis: str) -> dict[str, Any]:
    direction = group[0]["direction"]
    if hypothesis in {"PRIMARY_DEEPEST", "COMPLETE_SEQUENCE_EXTREME"}:
        return min(group, key=lambda x: float(x["sweep_extreme"]) if direction == "LONG" else -float(x["sweep_extreme"]))
    if hypothesis == "MOST_RECENT_NESTED":
        return max(group, key=lambda x: int(x["sweep_timestamp"]))
    if hypothesis == "DIRECTLY_RESPONSIBLE_INTERNAL":
        return min(group, key=lambda x: (int(x["displacement_index"]) - int(x.get("bar_index", x["displacement_index"])), -int(x["sweep_timestamp"])))
    raise ValueError(hypothesis)


def stop_hypotheses(group: list[dict[str, Any]], target_r: float = 1.25) -> dict[str, dict[str, Any]]:
    """Calculate competing geometry for one opportunity without choosing one."""
    ordered = sorted(group, key=lambda x: int(x["sweep_timestamp"]))
    deepest = _selector(ordered, "PRIMARY_DEEPEST")
    latest = _selector(ordered, "MOST_RECENT_NESTED")
    direct = _selector(ordered, "DIRECTLY_RESPONSIBLE_INTERNAL")
    selected = {
        "PRIMARY_DEEPEST": deepest,
        "COMPLETE_SEQUENCE_EXTREME": deepest,
        "MOST_RECENT_NESTED": latest,
        "DIRECTLY_RESPONSIBLE_INTERNAL": direct,
    }
    result: dict[str, dict[str, Any]] = {}
    for name, row in selected.items():
        direction = row["direction"]
        extreme = float(row["sweep_extreme"])
        buffer = abs(float(row["stop_loss"]) - extreme)
        stop = extreme - buffer if direction == "LONG" else extreme + buffer
        entry = float(row["entry_theoretical"])
        risk = entry - stop if direction == "LONG" else stop - entry
        target = entry + risk * target_r if direction == "LONG" else entry - risk * target_r
        result[name] = {
            "hypothesis": name,
            "source_setup_id": row.get("setup_id"),
            "structural_reference": {
                "sweep_timestamp": row.get("sweep_timestamp"),
                "sweep_extreme": extreme,
                "sweep_level": row.get("sweep_level"),
                "buffer": buffer,
            },
            "stop_loss": stop,
            "stop_distance": risk,
            "entry": entry,
            "target_r": target_r,
            "target": target,
            "rr_geometry": target_r,
        }
    return result


def _group_by_event(rows: Iterable[dict[str, Any]]) -> dict[tuple[Any, ...], list[dict[str, Any]]]:
    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[opportunity_key(row)].append(row)
    return groups


def _has_departure_between(
    bars: list[dict[str, Any]],
    start_i: int,
    end_i: int,
    entry: float,
    direction: str,
    atr_value: float,
    departure_atr: float,
) -> bool:
    if end_i <= start_i + 1 or atr_value <= 0:
        return False
    threshold = atr_value * departure_atr
    for bar in bars[start_i + 1:end_i]:
        excursion = float(bar["high"]) - entry if direction == "LONG" else entry - float(bar["low"])
        if excursion >= threshold:
            return True
    return False


def normalize_candidates(
    rows: Iterable[dict[str, Any]],
    *,
    bars: list[dict[str, Any]] | None = None,
    atr_by_index: dict[int, float] | None = None,
    departure_atr: float = 2.0,
    target_r: float = 1.25,
) -> dict[str, Any]:
    """Normalize raw records into event/sequence/opportunity views.

    Rows with the same thesis and shared displacement/entry interaction form
    one opportunity. Distinct fill interactions become re-entries only when a
    configurable ATR-normalized departure occurred between them; otherwise
    they remain one opportunity with multiple attempts. The threshold is an
    explicit research parameter, not an optimized policy.
    """
    raw = [attach_raw_ids(row, i) for i, row in enumerate(rows)]
    groups = _group_by_event(raw)
    events, sequences, opportunities, attempts = [], [], [], []
    for key, group in sorted(groups.items(), key=lambda item: item[0]):
        group.sort(key=lambda x: int(x["sweep_timestamp"]))
        event_id = group[0]["market_event_id"]
        sequence_id = group[0]["sweep_sequence_id"]
        for row in group:
            row["market_event_id"] = event_id
            row["sweep_sequence_id"] = sequence_id
        events.append({
            "market_event_id": event_id,
            "direction": group[0]["direction"],
            "symbol": group[0].get("symbol"),
            "liquidity_level": group[0]["sweep_level"],
            "displacement_index": group[0]["displacement_index"],
            "displacement_timestamp": group[0].get("displacement_timestamp"),
            "entry_theoretical": group[0]["entry_theoretical"],
        })
        sequences.append({
            "sweep_sequence_id": sequence_id,
            "market_event_id": event_id,
            "primary_deepest": min(group, key=lambda x: float(x["sweep_extreme"]) if group[0]["direction"] == "LONG" else -float(x["sweep_extreme"])),
            "nested_sweeps": group,
            "complete_sequence_extreme": min(group, key=lambda x: float(x["sweep_extreme"]) if group[0]["direction"] == "LONG" else -float(x["sweep_extreme"])),
            "most_recent_extreme": max(group, key=lambda x: int(x["sweep_timestamp"])),
            "displacement_mss": {"timestamp": group[0].get("displacement_timestamp"), "index": group[0]["displacement_index"], "break_levels": [x.get("break_level") for x in group]},
        })
        fills = sorted({x.get("fill_index") for x in group if x.get("fill_index") is not None})
        interaction_sets: list[list[int]] = []
        for fill_i in fills:
            if not interaction_sets:
                interaction_sets.append([fill_i]); continue
            previous = interaction_sets[-1][-1]
            atr_value = (atr_by_index or {}).get(fill_i, float(group[0].get("atr", 0)))
            departed = bool(bars and _has_departure_between(bars, previous, fill_i, float(group[0]["entry_theoretical"]), group[0]["direction"], atr_value, departure_atr))
            if departed:
                interaction_sets.append([fill_i])
            else:
                interaction_sets[-1].append(fill_i)
        if not interaction_sets:
            interaction_sets = [[None]]
        for op_no, fill_set in enumerate(interaction_sets, 1):
            op_rows = [x for x in group if x.get("fill_index") in fill_set] if fill_set != [None] else group
            op_id = _stable("OPP", (event_id, op_no))
            is_reentry = op_no > 1
            opp = {
                "entry_opportunity_id": op_id,
                "market_event_id": event_id,
                "sweep_sequence_id": sequence_id,
                "entry_opportunity_number": op_no,
                "is_reentry": is_reentry,
                "potential_scale_in": op_no > 1,
                "candidate_observation_ids": [x["candidate_observation_id"] for x in op_rows],
                "entry_attempt_id": _stable("ATTEMPT", (op_id, fill_set)),
                "fill_indices": fill_set,
                "stop_hypotheses": stop_hypotheses(group, target_r),
                "raw_observations": op_rows,
            }
            opportunities.append(opp)
            for row in op_rows:
                row["entry_opportunity_id"] = op_id
            attempts.append({
                "entry_attempt_id": opp["entry_attempt_id"],
                "entry_opportunity_id": op_id,
                "market_event_id": event_id,
                "sweep_sequence_id": sequence_id,
                "fill_indices": fill_set,
                "candidate_observation_ids": opp["candidate_observation_ids"],
            })
    return {
        "raw_candidate_observations": raw,
        "market_events": events,
        "sweep_sequences": sequences,
        "entry_opportunities": opportunities,
        "entry_attempts": attempts,
        "departure_definition": {"departure_atr": departure_atr, "requires_intervening_bar": True, "future_outcomes_not_used": True},
        "stop_hypotheses_are_alternatives": True,
    }


def exposure_timeline(rows: Iterable[dict[str, Any]], normalized: dict[str, Any], hypothesis: str = "PRIMARY_DEEPEST") -> dict[str, float | int]:
    """Compare raw candidate exposure with one alternative hypothesis view."""
    raw_filled = [r for r in rows if r.get("fill_index") is not None]
    norm_filled = []
    for opp in normalized["entry_opportunities"]:
        fills = [r for r in opp["raw_observations"] if r.get("fill_index") is not None]
        if not fills:
            continue
        h = opp["stop_hypotheses"][hypothesis]
        norm_filled.append({"fill_index": fills[0]["fill_index"], "exit_index": fills[0].get("exit_index"), "risk": h["stop_distance"]})

    def maximum(items: list[dict[str, Any]], raw: bool) -> float:
        max_total = 0.0
        for point in [x.get("fill_index") for x in items if x.get("fill_index") is not None]:
            total = 0.0
            for item in items:
                if item.get("fill_index") is None or int(item["fill_index"]) > int(point): continue
                exit_i = item.get("exit_index")
                if exit_i is None or int(point) <= int(exit_i): total += float(item["risk"] if not raw else item.get("risk", 0.0))
            max_total = max(max_total, total)
        return max_total
    return {"raw_max_simultaneous_risk_price": maximum(raw_filled, True), "normalized_max_simultaneous_risk_price": maximum(norm_filled, False)}

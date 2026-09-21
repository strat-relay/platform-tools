from __future__ import annotations

from collections import defaultdict
from typing import Any


def _recency_score(seconds: float | int | None, scale: float = 7 * 86400) -> float:
    return 1.0 / (1.0 + max(0.0, float(seconds or 0)) / scale)


def rank_sr(zones: list[dict[str, Any]], current_price: float, atr_value: float | None, limit: int = 3) -> dict[str, Any]:
    ranked = []
    for zone in zones:
        distance = abs(float(zone["midpoint"]) - current_price)
        normalized_distance = distance / atr_value if atr_value else None
        reaction_quality = sum(max(0.0, float(x.get("atr_normalized_rejection") or 0.0)) for x in zone.get("reactions", []))
        penetration_penalty = sum(float(x.get("body_penetration") or 0.0) for x in zone.get("reactions", []))
        score = (2.0 / (1.0 + (normalized_distance or 999.0))) + min(3.0, zone.get("reaction_count", 0) / 3.0) + min(3.0, reaction_quality / 3.0) + _recency_score(zone.get("recency_seconds")) - min(1.0, penetration_penalty / max((atr_value or 1.0) * 10, 1e-12))
        row = {**zone, "attention_score": score, "attention_measurements": {"distance": distance, "distance_atr": normalized_distance, "reaction_quality": reaction_quality, "penetration_penalty": penetration_penalty, "recency_score": _recency_score(zone.get("recency_seconds"))}}
        ranked.append(row)
    ranked.sort(key=lambda x: (-x["attention_score"], x["level_id"]))
    return {"raw_count": len(zones), "ranked": ranked, "top": ranked[:limit]}


def boundary_pair(zones: list[dict[str, Any]], current_price: float, atr_value: float | None) -> dict[str, Any]:
    supports = [z for z in zones if float(z["midpoint"]) <= current_price]
    resistances = [z for z in zones if float(z["midpoint"]) >= current_price]
    below = max(supports, key=lambda z: float(z["midpoint"]), default=None)
    above = min(resistances, key=lambda z: float(z["midpoint"]), default=None)
    below_distance = current_price - float(below["midpoint"]) if below else None
    above_distance = float(above["midpoint"]) - current_price if above else None
    total = (below_distance or 0) + (above_distance or 0)
    position = below_distance / total if below_distance is not None and above_distance is not None and total else None
    return {"below": below, "above": above, "distance_to_support": below_distance, "distance_to_resistance": above_distance, "distance_to_support_atr": below_distance / atr_value if below_distance is not None and atr_value else None, "distance_to_resistance_atr": above_distance / atr_value if above_distance is not None and atr_value else None, "position_between_boundaries": position, "room_upward": above_distance, "room_downward": below_distance}


def _cluster_key(channel: dict[str, Any]) -> tuple[float, float, float]:
    return (round(float(channel.get("slope_price_per_minute", 0.0)), 4), round(float(channel.get("current_upper", 0.0)), 1), round(float(channel.get("current_lower", 0.0)), 1))


def group_channels(channels: list[dict[str, Any]], atr_value: float | None, limit: int = 3) -> dict[str, Any]:
    groups: list[list[dict[str, Any]]] = []
    for candidate in sorted(channels, key=lambda x: x["structure_id"]):
        upper, lower = float(candidate.get("current_upper", 0)), float(candidate.get("current_lower", 0))
        slope = float(candidate.get("slope_price_per_minute", 0))
        matched = None
        for group in groups:
            representative = group[0]
            if abs(upper - float(representative.get("current_upper", 0))) <= max(atr_value or 1.0, 1e-12) and abs(lower - float(representative.get("current_lower", 0))) <= max(atr_value or 1.0, 1e-12) and abs(float(candidate.get("slope_atr_per_hour") or 0) - float(representative.get("slope_atr_per_hour") or 0)) <= 0.5:
                matched = group; break
        (matched if matched is not None else groups.append([]) or groups[-1]).append(candidate)
    canonical = []
    for index, group in enumerate(groups, 1):
        representative = max(group, key=lambda x: (x.get("touches_upper", 0) + x.get("touches_lower", 0), -len(x.get("violations", [])), x["structure_id"]))
        canonical.append({"cluster_id": f"CHANNEL-{index}", "member_count": len(group), "member_ids": [x["structure_id"] for x in group], "representative": representative})
    canonical.sort(key=lambda x: (-x["representative"].get("touches_upper", 0) - x["representative"].get("touches_lower", 0), x["cluster_id"]))
    return {"raw_count": len(channels), "cluster_count": len(canonical), "clusters": canonical, "top": canonical[:limit], "provenance": {"grouping": "upper/lower boundary distance <= 1 ATR and slope difference <= 0.5 ATR/hour", "future_outcomes_not_used": True}}


def rank_trendlines(lines: list[dict[str, Any]], current_price: float, atr_value: float | None, limit: int = 3) -> dict[str, Any]:
    ranked = []
    for line in lines:
        anchor = line["anchors"][-1]["price"] if line.get("anchors") else current_price
        distance = abs(float(anchor) - current_price)
        score = 2.0 * line.get("meaningful_interactions", 0) + 0.5 * line.get("touches", 0) + _recency_score(0) - 1.5 * line.get("violations", 0) - (distance / atr_value if atr_value else 0)
        ranked.append({**line, "attention_score": score, "current_distance": distance, "current_distance_atr": distance / atr_value if atr_value else None})
    ranked.sort(key=lambda x: (-x["attention_score"], x["structure_id"]))
    return {"raw_count": len(lines), "ranked": ranked, "top": ranked[:limit]}


def attention_layer(snapshot: dict[str, Any], limit: int = 3) -> dict[str, Any]:
    structure_tf = snapshot.get("provenance", {}).get("structure_timeframe", "M15")
    structure = snapshot["timeframes"][structure_tf]
    current = float(structure["completed_candle"]["close"]) if structure.get("completed_candle") else 0.0
    atr_value = structure["ema_context"].get("atr")
    zones = structure["sr_context"].get("zones", [])
    sr = rank_sr(zones, current, atr_value, limit)
    return {"structure_timeframe": structure_tf, "sr": sr, "boundaries": boundary_pair(zones, current, atr_value), "trendlines": rank_trendlines(snapshot.get("trendline_context", []), current, atr_value, limit), "channels": group_channels(snapshot.get("channel_context", []), atr_value, limit), "provenance": {"future_outcomes_not_used": True, "ranking_is_structural_only": True, "normalization": "distance/reaction/width/slope similarity use ATR-relative values where applicable"}}

"""Phase 5B review-only decisions and zone-departure model."""
from __future__ import annotations

import json
import gzip
import html
import math
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from research.context_structure_retrace_phase5.phase5 import _geometry
from research.context_structure_retrace_phase4.phase4 import PHASE3, _flatten, _read_rows

ROOT = Path(__file__).resolve().parent
DATA_DIR = Path("/tmp/context-structure-retrace")
DATA_FILES = {"XAUUSDm": "xau.json", "BTCUSDm": "btc_basic.json", "USDJPYm": "USDJPYm.json", "EURUSDm": "EURUSDm.json"}
TF_SECONDS = {"M5": 300, "M15": 900, "H1": 3600, "H4": 14400}


def _timestamp(value: Any) -> int:
    if isinstance(value, (int, float)):
        return int(value)
    return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp())


def classify_zone_interaction(
    bars: list[dict[str, Any]],
    zone_low: float,
    zone_high: float,
    *,
    thesis_valid: Callable[[dict[str, Any]], bool] | None = None,
) -> dict[str, Any]:
    """Classify interaction/leave/return without an ATR threshold."""
    thesis_valid = thesis_valid or (lambda _: True)
    interacted = left = invalidated = False
    outside_timestamp = return_timestamp = None
    for bar in bars:
        low, high, close = float(bar["low"]), float(bar["high"]), float(bar["close"])
        touches = low <= zone_high and high >= zone_low
        if not interacted and touches:
            interacted = True
            continue
        if interacted and not thesis_valid(bar):
            invalidated = True
        if interacted and not left and (close < zone_low or close > zone_high):
            left = True
            outside_timestamp = int(bar.get("time", 0))
            continue
        if interacted and left and touches:
            return_timestamp = int(bar.get("time", 0))
            break
    if invalidated and return_timestamp is not None:
        classification = "INVALIDATED_NO_REENTRY"
    elif left and return_timestamp is not None:
        classification = "NEW_OPPORTUNITY"
    else:
        classification = "SAME_OPPORTUNITY_HOVER"
    return {
        "classification": classification,
        "interacted": interacted,
        "left_zone": left,
        "outside_timestamp": outside_timestamp,
        "return_timestamp": return_timestamp,
        "thesis_invalidated": invalidated,
        "atr_excursion": "DESCRIPTIVE_ONLY_NOT_REQUIRED",
    }


def _context_text(case: dict[str, Any]) -> str:
    context = case.get("context") or {}
    flags = ", ".join(case.get("context_flags") or []) or "none"
    htf = context.get("htf") or {}
    return (
        f"Context flags: {flags}. M15 EMA ordering: {context.get('ema_ordering') or 'unknown'}. "
        f"H1: {htf.get('H1', {}).get('completed_direction') or 'unknown'}; "
        f"H4: {htf.get('H4', {}).get('completed_direction') or 'unknown'}. "
        f"Room: support {context.get('distance_to_support_atr')}, "
        f"resistance {context.get('distance_to_resistance_atr')} ATR."
    )


def _causal_bars(series: list[dict[str, Any]], timeframe: str, as_of: int) -> list[dict[str, Any]]:
    step = TF_SECONDS[timeframe]
    return [b for b in series if int(b["time"]) + step <= int(as_of)][-80:]


def _aggregate_h4(h1: list[dict[str, Any]], as_of: int) -> list[dict[str, Any]]:
    groups = {}
    for b in h1:
        if int(b["time"]) + 3600 > int(as_of):
            continue
        start = (int(b["time"]) // 14400) * 14400
        groups.setdefault(start, []).append(b)
    out = []
    for start, bars in sorted(groups.items()):
        out.append({"time": start, "open": bars[0]["open"], "high": max(float(x["high"]) for x in bars), "low": min(float(x["low"]) for x in bars), "close": bars[-1]["close"], "spread": bars[-1].get("spread", 0)})
    return out[-80:]


def _ema_values(bars: list[dict[str, Any]], period: int) -> list[float | None]:
    alpha = 2.0 / (period + 1.0)
    result = []
    value = None
    for bar in bars:
        close = float(bar["close"])
        value = close if value is None else alpha * close + (1 - alpha) * value
        result.append(value)
    return result


def _svg_panel(timeframe: str, bars: list[dict[str, Any]], snapshot: dict[str, Any] | None, case: dict[str, Any], width: int = 980, height: int = 260) -> str:
    if not bars:
        return f"<div class=panel><h3>{timeframe}</h3><p>Historical bars unavailable before the causal review point.</p></div>"
    pad = 42
    xs = lambda i: pad + (width - 2 * pad) * (i / max(1, len(bars) - 1))
    levels = [float(case[k]) for k in ("entry", "originating_stop", "extension_target", "effective_capped_target") if case.get(k) is not None]
    levels += [float(case["opposing_structure"])] if case.get("opposing_structure") is not None else []
    zones = []
    trendlines = []
    channels = []
    if snapshot:
        tfctx = snapshot.get("timeframes", {}).get(timeframe, {})
        zones = sorted(tfctx.get("sr_context", {}).get("zones", []), key=lambda z: abs(float(z.get("midpoint", 0)) - float(bars[-1]["close"])))[:8]
        trendlines = snapshot.get("trendline_context", [])[:2]
        channels = snapshot.get("channel_context", [])[:1]
    for z in zones:
        levels.extend([float(z["zone_low"]), float(z["zone_high"])])
    for tl in trendlines:
        levels.extend(float(a["price"]) for a in tl.get("anchors", []))
    for ch in channels:
        levels.extend([float(ch.get("current_upper", 0)), float(ch.get("current_lower", 0))])
    lo = min([float(b["low"]) for b in bars] + levels)
    hi = max([float(b["high"]) for b in bars] + levels)
    margin = max((hi - lo) * .06, 1e-9); lo -= margin; hi += margin
    y = lambda price: height - 22 - (float(price) - lo) / max(hi - lo, 1e-12) * (height - 42)
    parts = [f'<div class=panel><h3>{timeframe} — causal through {case.get("review_timestamp")}</h3><svg viewBox="0 0 {width} {height}" role="img" aria-label="{timeframe} causal candle chart">', f'<rect x="0" y="0" width="{width}" height="{height}" fill="#fff"/>']
    for z in zones:
        color = "#dcefe0" if z.get("support_resistance_role") == "SUPPORT" else "#f5dfdf"
        top, bottom = min(y(z["zone_high"]), y(z["zone_low"])), max(y(z["zone_high"]), y(z["zone_low"]))
        parts.append(f'<rect x="{pad}" y="{top:.2f}" width="{width-2*pad}" height="{max(1,bottom-top):.2f}" fill="{color}" opacity=".75"/>')
    body_w = max(2.0, (width - 2 * pad) / max(100, len(bars)) * .65)
    for i, b in enumerate(bars):
        x = xs(i); yo, yc, yh, yl = y(b["open"]), y(b["close"]), y(b["high"]), y(b["low"])
        up = float(b["close"]) >= float(b["open"]); color = "#2e7d4f" if up else "#b34b4b"
        top, bottom = min(yo, yc), max(yo, yc)
        parts.append(f'<line x1="{x:.2f}" y1="{yh:.2f}" x2="{x:.2f}" y2="{yl:.2f}" stroke="{color}" stroke-width="1"/><rect x="{x-body_w/2:.2f}" y="{top:.2f}" width="{body_w:.2f}" height="{max(1,bottom-top):.2f}" fill="{color}"/>')
    ema_colors = {20: "#2463a6", 50: "#8b5a2b", 100: "#7b4aa3", 200: "#333"}
    for period, color in ema_colors.items():
        vals = _ema_values(bars, period)
        points = " ".join(f"{xs(i):.2f},{y(v):.2f}" for i, v in enumerate(vals) if v is not None)
        if points: parts.append(f'<polyline points="{points}" fill="none" stroke="{color}" stroke-width="1.2" opacity=".85"/>')
    for tl in trendlines:
        anchors = tl.get("anchors", [])
        if len(anchors) >= 2:
            a, b = anchors[-2], anchors[-1]
            slope = (float(b["price"]) - float(a["price"])) / max(1, _timestamp(b["timestamp"]) - _timestamp(a["timestamp"]))
            p1 = float(a["price"]) + slope * (int(bars[0]["time"]) - _timestamp(a["timestamp"]))
            p2 = float(a["price"]) + slope * (int(bars[-1]["time"]) - _timestamp(a["timestamp"]))
            parts.append(f'<line x1="{xs(0):.2f}" y1="{y(p1):.2f}" x2="{xs(len(bars)-1):.2f}" y2="{y(p2):.2f}" stroke="#c47f00" stroke-width="1.4" stroke-dasharray="4 3"/>')
    for ch in channels:
        for side, color in (("upper", "#aa6b00"), ("lower", "#aa6b00")):
            pts = ch.get("boundaries", {}).get(side, [])
            if len(pts) >= 2:
                p1, p2 = pts[-2], pts[-1]
                slope = (float(p2["price"]) - float(p1["price"])) / max(1, _timestamp(p2["timestamp"]) - _timestamp(p1["timestamp"]))
                v1 = float(p1["price"]) + slope * (int(bars[0]["time"]) - _timestamp(p1["timestamp"]))
                v2 = float(p1["price"]) + slope * (int(bars[-1]["time"]) - _timestamp(p1["timestamp"]))
                parts.append(f'<line x1="{xs(0):.2f}" y1="{y(v1):.2f}" x2="{xs(len(bars)-1):.2f}" y2="{y(v2):.2f}" stroke="{color}" stroke-width="1" stroke-dasharray="2 3"/>')
    line_colors = {"entry": "#0066cc", "originating_stop": "#c62828", "extension_target": "#2e7d32", "effective_capped_target": "#7b1fa2", "opposing_structure": "#d17b00"}
    for key, color in line_colors.items():
        if case.get(key) is not None:
            yy = y(case[key]); parts.append(f'<line x1="{pad}" y1="{yy:.2f}" x2="{width-pad}" y2="{yy:.2f}" stroke="{color}" stroke-width="1.5" stroke-dasharray="6 4"/><text x="{pad+4}" y="{max(12,yy-3):.2f}" fill="{color}" font-size="10">{key}</text>')
    parts.append(f'<line x1="{pad}" y1="{height-22}" x2="{width-pad}" y2="{height-22}" stroke="#999"/><text x="6" y="16" font-size="11">{timeframe}</text></svg></div>')
    return "".join(parts)


def _load_snapshots(keys: set[str]) -> dict[str, dict[str, Any]]:
    found = {}
    path = PHASE3 / "context_structure_retrace_phase3_snapshots.jsonl.gz"
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            key = f"{row['symbol']}|{row['timestamp']}"
            if key in keys:
                found[key] = row["context_snapshot"]
                if len(found) == len(keys):
                    break
    return found


def _attach_charts(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    data_by_symbol = {symbol: json.loads((DATA_DIR / DATA_FILES[symbol]).read_text(encoding="utf-8")) for symbol in DATA_FILES}
    snapshots = _load_snapshots({f"{c['symbol']}|{c['timestamp']}" for c in cases})
    out = []
    for case in cases:
        c = dict(case)
        data = data_by_symbol[c["symbol"]]
        as_of = int(c.get("review_timestamp") or c["timestamp"])
        snapshot = snapshots.get(f"{c['symbol']}|{c['timestamp']}")
        charts = []
        for tf in ("M15", "M5", "H1", "H4"):
            if tf == "H4":
                bars = _aggregate_h4(data.get("H1", []), as_of) if "H4" not in data else _causal_bars(data["H4"], "H4", as_of)
            else:
                bars = _causal_bars(data.get(tf, []), tf, as_of)
            charts.append(_svg_panel(tf, bars, snapshot, {**c, "review_timestamp": as_of}))
        c["review_timestamp"] = as_of
        c["charts"] = charts
        out.append(c)
    return out


def _geometry_audit() -> dict[str, Any]:
    result = {}
    for symbol, rows in _read_rows().items():
        data = json.loads((DATA_DIR / DATA_FILES[symbol]).read_text(encoding="utf-8"))
        _, normalized = _flatten(symbol, rows, data)
        raw_behind = raw_at = corrected_behind = corrected_at = 0
        for x in normalized["economic_positions"]:
            g = _geometry(x)
            opposing = x["target_hypotheses"].get("NEXT_OPPOSING_STRUCTURE")
            if opposing is not None:
                if (x["direction"] == "LONG" and opposing < x["entry_price"]) or (x["direction"] == "SHORT" and opposing > x["entry_price"]):
                    raw_behind += 1
                elif opposing == x["entry_price"]:
                    raw_at += 1
            corrected_behind += g["target_state"] == "TARGET_BEHIND_ENTRY"
            corrected_at += g["target_state"] == "TARGET_AT_ENTRY"
        result[symbol] = {"full_phase5_economic_positions": len(normalized["economic_positions"]), "raw_opposing_structure_behind_entry": raw_behind, "raw_opposing_structure_at_entry": raw_at, "corrected_effective_target_behind_entry": corrected_behind, "corrected_effective_target_at_entry": corrected_at}
    return result


def build_review_html(cases: list[dict[str, Any]]) -> str:
    """Build a local, no-outcome review page with downloadable answers."""
    payload = json.dumps(cases, separators=(",", ":"), ensure_ascii=False).replace("</", "<\\/")
    cards = []
    for i, case in enumerate(cases):
        flags = ", ".join(case.get("structural_flags") or []) or "none"
        target_state = case.get("target_state") or "UNKNOWN"
        state_class = "bad" if target_state != "TARGET_BEYOND_ENTRY" else "good"
        cards.append(f"""<article class=\"case\" data-index=\"{i}\">
<h2>Case {i + 1} — {case.get('symbol')} {case.get('direction')}</h2>
<p class=\"meta\">Setup: {case.get('timestamp')} · Review/entry: {case.get('review_timestamp')} · {case.get('pattern')} · Signals: {', '.join(case.get('signal_evidence') or [])}</p>
<p class=\"target-state {state_class}\"><strong>{target_state}</strong> — this is descriptive geometry only; no automatic rejection is applied.</p>
{''.join(case.get('charts', []))}
<div class=\"grid\">{''.join(f'<div><span>{label}</span><strong>{case.get(key)}</strong></div>' for label,key in (("Entry","entry"),("Originating stop","originating_stop"),("Extension target","extension_target"),("Opposing structure","opposing_structure"),("Capped target","effective_capped_target"),("Target R","target_r"),("Target ATR","target_atr"),("Spread / target","spread_to_target")))}</div>
<p>{_context_text(case)}</p><p class=\"flags\">Structural flags: {flags}</p>
<fieldset><legend>Human review</legend>
<label><input type=radio name=\"decision-{i}\" value=WOULD_TAKE> WOULD_TAKE</label>
<label><input type=radio name=\"decision-{i}\" value=WOULD_NOT_TAKE> WOULD_NOT_TAKE</label>
<label><input type=radio name=\"decision-{i}\" value=UNSURE> UNSURE</label>
<select id=\"reason-{i}\"><option value=\"\">Optional reason</option><option>opposing structure too close</option><option>poor reward relative to stop</option><option>spread/cost too large</option><option>context conflict</option><option>setup itself unattractive</option><option>would take this</option><option>other</option></select>
<input id=\"note-{i}\" placeholder=\"Optional note\"></fieldset></article>""")
    return f"""<!doctype html><html><head><meta charset=utf-8><title>CONTEXT_STRUCTURE_RETRACE_V1 Phase 5B review</title>
<style>body{{font:15px system-ui,sans-serif;background:#f5f5f2;color:#20201d;margin:24px;max-width:1100px}}h1{{font-size:24px}}.notice{{padding:12px;background:#fff4d6;border:1px solid #d8b65a}}.case{{background:white;border:1px solid #d9d9d2;padding:16px;margin:16px 0}}.meta,.flags{{color:#666}}.target-state{{padding:10px;border-left:5px solid}}.target-state.bad{{background:#ffe5e5;border-color:#b3261e;color:#8b1a1a}}.target-state.good{{background:#e5f4e9;border-color:#287a45;color:#205b34}}.grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:8px}}.grid div{{background:#f3f3ef;padding:8px}}.grid span{{display:block;color:#666;font-size:12px}}.grid strong{{display:block;margin-top:4px}}.panel{{margin:12px 0;border:1px solid #ddd;padding:6px;background:#fafafa}}.panel h3{{margin:4px;font-size:14px}}fieldset{{margin-top:14px;border:1px solid #ddd;padding:10px}}label{{margin-right:16px}}select,input{{padding:6px;margin:5px 5px 0 0}}button{{padding:9px 12px;margin:4px 8px 4px 0}}@media(max-width:700px){{.grid{{grid-template-columns:repeat(2,1fr)}}}}</style></head><body>
<h1>CONTEXT_STRUCTURE_RETRACE_V1 — Phase 5B human review</h1><div class=notice><strong>No future outcomes are included.</strong> Review only whether the represented room and context match your discretionary process. Answers are not converted into rules automatically.</div>
<p><button onclick=save()>Save review JSON</button><button onclick=clearReview()>Clear local review</button> <span id=progress></span></p><section id=cases>{''.join(cards)}</section>
<script>const cases={payload};function key(i){{return 'csr-phase5b-'+i}}function update(){{let n=0;cases.forEach((_,i)=>{{if(document.querySelector('input[name=decision-'+i+']:checked'))n++}});document.getElementById('progress').textContent=n+' / '+cases.length+' reviewed'}}function save(){{const reviews=cases.map((c,i)=>({{setup_id:c.setup_id,symbol:c.symbol,timestamp:c.timestamp,decision:(document.querySelector('input[name=decision-'+i+']:checked')||{{}}).value||null,reason:document.getElementById('reason-'+i).value,note:document.getElementById('note-'+i).value}}));const blob=new Blob([JSON.stringify({{schema:'context_structure_retrace_phase5b_review',future_outcomes_included:false,reviews}},null,2)],{{type:'application/json'}});const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download='context_structure_retrace_phase5b_human_review.json';a.click();reviews.forEach((r,i)=>localStorage.setItem(key(i),JSON.stringify(r)))}}function clearReview(){{cases.forEach((_,i)=>localStorage.removeItem(key(i)));location.reload()}}cases.forEach((_,i)=>document.querySelectorAll('input[name=decision-'+i+'],#reason-'+i+',#note-'+i).forEach(e=>e.addEventListener('change',update)));update();</script></body></html>"""


def main() -> None:
    data = json.loads((ROOT / "context_structure_retrace_phase5_results.json").read_text(encoding="utf-8"))
    cases = [case for report in data["instruments"].values() for case in report["review_cases"]]
    cases = _attach_charts(cases)
    (ROOT / "context_structure_retrace_phase5b_human_review.html").write_text(build_review_html(cases), encoding="utf-8")
    for preview_index, case in enumerate(cases[:3], 1):
        match = re.search(r"(<svg\b.*?</svg>)", case["charts"][0], re.DOTALL)
        if match:
            (ROOT / f"context_structure_retrace_phase5b_preview_{preview_index}.svg").write_text(match.group(1), encoding="utf-8")
    metadata = {"schema": "context_structure_retrace_phase5b", "review_cases": len(cases), "future_outcomes_in_review": False, "reentry_definition": "INTERACT -> LEAVE_ZONE -> COMPLETED_CLOSE_OUTSIDE -> RETURN -> THESIS_VALID", "atr_excursion": "DESCRIPTIVE_ONLY_NOT_REQUIRED", "target_policy": "STRUCTURE_CAPPED_EXTENSION_PROVISIONAL", "entry_mechanisms": ["DEPTH_ONLY", "REJECTION_WICK", "LOWER_TF_ENGULFING", "MORNING_EVENING_STAR", "EMA_TOUCH_REJECTION", "STRUCTURE_TOUCH_REJECTION"], "target_geometry_audit": _geometry_audit(), "chart_timeframes": ["M15", "M5", "H1", "H4"], "chart_right_edge": "review_timestamp; only bars completed by that timestamp are rendered"}
    (ROOT / "context_structure_retrace_phase5b_metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    print(json.dumps(metadata, indent=2))


if __name__ == "__main__":
    main()

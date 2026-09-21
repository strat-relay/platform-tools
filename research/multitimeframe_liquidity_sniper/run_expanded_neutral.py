"""Run the already-frozen neutral configuration on available history.

This is an architectural sanity replay, not a parameter search.  It uses the
same M5 trigger for controls A-D and precomputes higher-timeframe state so the
deep USDJPY export can be replayed without quadratic work.
"""
from __future__ import annotations

import bisect, hashlib, json, subprocess
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from .config import CONTROL_GROUPS
from .engine import _range, _ts, pip_size, structure_direction
from .neutral import NEUTRAL_CONFIG
from .state_machine import detect_m15_setups

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "artifacts/research/multitimeframe_liquidity_sniper"


def iso(ts):
    return datetime.fromtimestamp(int(ts), timezone.utc).isoformat() if ts is not None else None


def atr(rows, i, n=14):
    values = [_range(x) for x in rows[max(0, i-n):i] if _range(x) > 0]
    return sum(values) / len(values) if values else _range(rows[i])


def raw_trigger(rows, i):
    if i < 1:
        return None
    c, p = rows[i], rows[i-1]
    if float(c["close"]) > float(c["open"]) and float(c["close"]) > float(p["high"]):
        direction = "LONG"
    elif float(c["close"]) < float(c["open"]) and float(c["close"]) < float(p["low"]):
        direction = "SHORT"
    else:
        return None
    return {"trigger_id": f"RAW-M5-{_ts(c['time'])}-{direction}", "direction": direction,
            "trigger_timestamp": _ts(c["time"]), "entry": float(c["close"])}


def stop(rows, i, direction):
    lo = min(float(x["low"]) for x in rows[max(0, i-2):i+1])
    hi = max(float(x["high"]) for x in rows[max(0, i-2):i+1])
    a = atr(rows, i)
    return lo - 0.10*a if direction == "LONG" else hi + 0.10*a


def outcome(rows, i, entry, sl, direction):
    risk = entry-sl if direction == "LONG" else sl-entry
    if risk <= 0:
        return None
    target = entry + 1.25*risk if direction == "LONG" else entry - 1.25*risk
    end = min(len(rows), i+24+1)
    mae = mfe = 0.0
    for j in range(i+1, end):
        high, low = float(rows[j]["high"]), float(rows[j]["low"])
        mae = max(mae, (entry-low)/risk if direction == "LONG" else (high-entry)/risk)
        mfe = max(mfe, (high-entry)/risk if direction == "LONG" else (entry-low)/risk)
        hit_sl = low <= sl if direction == "LONG" else high >= sl
        hit_tp = high >= target if direction == "LONG" else low <= target
        if hit_sl or hit_tp:
            return {"gross_R": -1.0 if hit_sl else 1.25, "exit": "STOP" if hit_sl else "TARGET",
                    "hold_minutes": (j-i)*5, "mae_R": mae, "mfe_R": mfe, "target": target}
    px = float(rows[end-1]["close"])
    return {"gross_R": (px-entry)/risk if direction == "LONG" else (entry-px)/risk,
            "exit": "TIME", "hold_minutes": (end-1-i)*5, "mae_R": mae, "mfe_R": mfe, "target": target}


def direction_at(rows, close_times, decision, tf):
    n = bisect.bisect_right(close_times, decision) - 1
    if n < 0:
        return None, None
    start = max(0, n-4)
    return structure_direction(rows[start:n+1], 5), _ts(rows[n]["time"])


def summarize(rows):
    rs = [float(x["net_R"]) for x in rows]
    gross = [float(x["gross_R"]) for x in rows]
    neg = [x for x in rs if x < 0]
    return {"fills": len(rs), "gross_expectancy_R": sum(gross)/len(gross) if gross else None,
            "net_expectancy_R": sum(rs)/len(rs) if rs else None,
            "cost_R": sum(float(x["spread_cost_R"]) for x in rows)/len(rs) if rs else None,
            "profit_factor": sum(x for x in rs if x > 0)/abs(sum(neg)) if neg else None,
            "max_DD_R": max_dd(rs)}


def cost_buckets(rows):
    labels = ("<=0.05R", "0.05-0.10R", "0.10-0.15R", "0.15-0.20R", "0.20-0.30R", "0.30-0.50R", ">0.50R")
    out = {x: {"fills": 0, "gross_expectancy_R": None, "net_expectancy_R": None} for x in labels}
    groups = defaultdict(list)
    for x in rows:
        v = float(x["spread_cost_R"])
        key = labels[0] if v <= .05 else labels[1] if v <= .10 else labels[2] if v <= .15 else labels[3] if v <= .20 else labels[4] if v <= .30 else labels[5] if v <= .50 else labels[6]
        groups[key].append(x)
    for key, group in groups.items():
        out[key] = {"fills": len(group), "gross_expectancy_R": sum(float(x["gross_R"]) for x in group)/len(group), "net_expectancy_R": sum(float(x["net_R"]) for x in group)/len(group)}
    return out


def max_dd(values):
    equity = peak = dd = 0.0
    for x in values:
        equity += x; peak = max(peak, equity); dd = max(dd, peak-equity)
    return dd


def run():
    dataset = json.loads((OUT / "expanded_historical_dataset.json").read_text())
    manifest = json.loads((OUT / "historical_dataset_manifest.json").read_text())
    ledger_path = OUT / "expanded_neutral_ledger.jsonl"
    ledger = []
    attrition = Counter()
    per_symbol = defaultdict(lambda: defaultdict(list))
    for symbol, frames in dataset.items():
        if not all(tf in frames and frames[tf] for tf in ("M5", "M15", "H1", "H4")):
            continue
        m5 = sorted(frames["M5"], key=lambda x: _ts(x["time"]))
        m15 = sorted(frames["M15"], key=lambda x: _ts(x["time"]))
        h1 = sorted(frames["H1"], key=lambda x: _ts(x["time"]))
        h4 = sorted(frames["H4"], key=lambda x: _ts(x["time"]))
        setups = detect_m15_setups(m15, NEUTRAL_CONFIG["m15"])
        setup_times = [_ts(x.get("bos_choch_timestamp", 0)) + 900 for x in setups]
        h1_closes = [_ts(x["time"])+3600 for x in h1]
        h4_closes = [_ts(x["time"])+14400 for x in h4]
        for i in range(1, len(m5)-1):
            raw = raw_trigger(m5, i)
            if not raw:
                continue
            decision = _ts(m5[i]["time"])+300
            prior = bisect.bisect_right(setup_times, decision)-1
            setup = setups[prior] if prior >= 0 else None
            if setup is not None and setup.get("direction") != raw["direction"]:
                setup = None
            h1_dir, h1_source = direction_at(h1, h1_closes, decision, "H1")
            h4_dir, h4_source = direction_at(h4, h4_closes, decision, "H4")
            aligned = {"LONG": ("BULLISH_HH_HL",), "SHORT": ("BEARISH_LH_LL",)}[raw["direction"]]
            controls = {"CONTROL_A_M5_ONLY": True, "CONTROL_B_M15": setup is not None,
                        "CONTROL_C_M15_H1": setup is not None and h1_dir in aligned,
                        "CONTROL_D_H4_H1_M15": setup is not None and h1_dir in aligned and h4_dir in aligned}
            if setup is None: attrition["M15_SETUP_MISSING_OR_DIRECTION_MISMATCH"] += 1
            elif not controls["CONTROL_C_M15_H1"]: attrition["H1_CONTEXT_MISMATCH"] += 1
            elif not controls["CONTROL_D_H4_H1_M15"]: attrition["H4_CONTEXT_MISMATCH"] += 1
            for group, ok in controls.items():
                if not ok: continue
                entry = float(raw["entry"]); sl = stop(m5, i, raw["direction"]); result = outcome(m5, i, entry, sl, raw["direction"])
                if not result: continue
                spread_price = float(m5[i].get("spread", 0)) * (0.001 if "JPY" in symbol.upper() else 0.00001)
                cost_r = spread_price / abs(entry-sl) if entry != sl else None
                row = {"setup_id": setup.get("setup_id") if setup else None, "stable_setup_id": f"{symbol}|{raw['trigger_timestamp']}|{raw['direction']}",
                       "symbol": symbol, "direction": raw["direction"], "control": group,
                       "decision_timestamp": iso(decision), "h4_source_timestamp": iso(h4_source), "h1_source_timestamp": iso(h1_source),
                       "m15_setup": setup, "m5_trigger": raw, "fill_timestamp": iso(raw["trigger_timestamp"]), "fill_price": entry,
                       "spread_timestamp": iso(raw["trigger_timestamp"]), "spread_points": m5[i].get("spread"),
                       "spread_pips": spread_price/(0.01 if "JPY" in symbol.upper() else 0.0001), "stop": sl,
                       "stop_distance_pips": abs(entry-sl)/(0.01 if "JPY" in symbol.upper() else 0.0001), **result,
                       "spread_cost_R": cost_r, "net_R": result["gross_R"]-(cost_r or 0),
                       "config_hash": hashlib.sha256(json.dumps(NEUTRAL_CONFIG, sort_keys=True).encode()).hexdigest(),
                       "data_hash": manifest["dataset_hash"], "source_commit": manifest["source_commit"]}
                ledger.append(row); per_symbol[symbol][group].append(row)
    ledger_path.write_text("".join(json.dumps(x, sort_keys=True, default=str)+"\n" for x in ledger))
    metrics = {s: {g: summarize(rows) for g, rows in groups.items()} for s, groups in per_symbol.items()}
    aggregate = {g: summarize([x for x in ledger if x["control"] == g]) for g in CONTROL_GROUPS}
    equal_pair = {}
    for g in CONTROL_GROUPS:
        vals = [metrics[s][g]["net_expectancy_R"] for s in metrics if g in metrics[s] and metrics[s][g]["net_expectancy_R"] is not None]
        equal_pair[g] = {"pairs": len(vals), "net_expectancy_R": sum(vals)/len(vals) if vals else None,
                         "median_pair_expectancy_R": sorted(vals)[len(vals)//2] if vals else None}
    report = {"schema": "expanded-neutral-replay-v1", "research_only": True, "config": NEUTRAL_CONFIG,
              "dataset_hash": manifest["dataset_hash"], "metrics": metrics, "aggregate_equal_trade": aggregate,
              "aggregate_equal_pair": equal_pair, "cost_to_risk_buckets": {g: cost_buckets([x for x in ledger if x["control"] == g]) for g in CONTROL_GROUPS},
              "context_attrition": dict(attrition), "ledger_path": str(ledger_path), "ledger_rows": len(ledger),
              "available_symbols": sorted(dataset), "source_commit": manifest["source_commit"]}
    (OUT / "expanded_neutral_replay.json").write_text(json.dumps(report, indent=2, default=str)+"\n")
    return report


if __name__ == "__main__":
    print(json.dumps(run(), indent=2, default=str))

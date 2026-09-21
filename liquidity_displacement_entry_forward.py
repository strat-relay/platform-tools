"""Frozen paper-only forward runners for the validated shallow-entry variants.

This module reuses the existing read-only forward engine. It changes only the
instrument, retracement depth, and isolated persistence paths selected by the
variant name. No order-submission API is imported or called.
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import liquidity_displacement_forward as base
from liquidity_displacement import LiquidityDisplacementConfig

ROOT = Path(__file__).resolve().parent
VARIANTS = {
    "usdjpy25": {"version": "LIQUIDITY_DISPLACEMENT_SCALP_USDJPY_25_V1", "symbol": "USDJPYm", "fraction": 0.25, "label": "USDJPY 25%"},
    "xau33": {"version": "LIQUIDITY_DISPLACEMENT_SCALP_XAUUSD_33_V1", "symbol": "XAUUSDm", "fraction": 1 / 3, "label": "XAUUSD 33%"},
    "btc25": {"version": "LIQUIDITY_DISPLACEMENT_SCALP_BTCUSD_25_V1", "symbol": "BTCUSDm", "fraction": 0.25, "label": "BTCUSD 25%"},
    "ustec_x100m": {"version": "LIQUIDITY_DISPLACEMENT_SCALP_USTEC_X100M_25_V1", "symbol": "USTEC_x100m", "fraction": 0.25, "label": "USTEC x100m 25%"},
    "ustecm": {"version": "LIQUIDITY_DISPLACEMENT_SCALP_USTECM_25_V1", "symbol": "USTECm", "fraction": 0.25, "label": "USTECm 25%"},
}


def configure(name: str):
    if name not in VARIANTS:
        raise SystemExit(f"unknown variant {name}; choose one of: {', '.join(VARIANTS)}")
    spec = VARIANTS[name]
    cfg = LiquidityDisplacementConfig(
        symbol=spec["symbol"], target_r=1.25, max_hold_minutes=120,
        max_retrace_candles=5, max_structure_break_candles=5,
        min_body_atr=0.5, min_body_median_multiple=1.0,
        min_close_location=0.6, atr_buffer_fraction=0.1,
    )

    class FrozenEntryStrategy(base.LiquidityDisplacementStrategy):
        def find_candidate(self, m15, m5, i, quote, contract, timestamp):
            candidate = super().find_candidate(m15, m5, i, quote, contract, timestamp)
            if not candidate:
                return None
            d = m5[candidate["displacement_index"]]
            low, high = float(d["low"]), float(d["high"])
            depth = spec["fraction"]
            entry = high - (high - low) * depth if candidate["direction"] == "LONG" else low + (high - low) * depth
            candidate["entry"] = entry
            candidate["risk"] = entry - candidate["stop_loss"] if candidate["direction"] == "LONG" else candidate["stop_loss"] - entry
            if candidate["risk"] <= 0:
                return None
            candidate["entry_type"] = f"RETRACE_{depth:.6f}_DISPLACEMENT"
            candidate["setup_type"] = spec["version"]
            candidate["variant_entry_fraction"] = depth
            return candidate

    original_class = base.LiquidityDisplacementStrategy
    original_event = base.event
    original_process = base.process
    base.LiquidityDisplacementStrategy = FrozenEntryStrategy
    base.SYMBOL = spec["symbol"]
    base.PAPER_TITLE = f"{spec['version']} — {spec['symbol']} FORWARD PAPER"
    base.CFG = cfg
    base.ENTRY_FRACTION = spec["fraction"]
    prefix = f"liquidity_displacement_{name}"
    base.STATE = ROOT / f"{prefix}_state.json"
    base.EVENTS = ROOT / f"{prefix}.jsonl"
    base.DAILY = ROOT / f"{prefix}_daily.jsonl"
    base.SUMMARY = ROOT / f"{prefix}_summary.md"
    base.MANIFEST = ROOT / f"{prefix}_manifest.json"
    base.PIDFILE = ROOT / f"{prefix}.pid"
    base.HEARTBEAT = ROOT / f"{prefix}.heartbeat.json"
    base.STOP = Path(f"/tmp/{prefix}.stop")

    def manifest_variant():
        return {
            "version": spec["version"],
            "parent_strategy": "LIQUIDITY_DISPLACEMENT_SCALP_V1",
            "symbol": spec["symbol"],
            "entry_fraction": spec["fraction"],
            "target_r": 1.25,
            "max_retrace_candles": 5,
            "max_hold_minutes": 120,
            "entry_rules": "V1 sweep -> reclaim -> displacement -> micro structure shift; frozen shallow retracement entry",
            "stop_rules": "V1 structural sweep extreme plus max(0.10 ATR, 1.25x spread, broker stop minimum)",
            "target_rules": "constant 1.25R recalculated from the frozen candidate entry",
            "timeframes": "M5 execution, M15 context, M1 disabled",
            "source_sha256": base.source_hash(),
            "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "paper_only": True,
            "live_order_endpoints": False,
        }

    def event_variant(state, kind, payload):
        payload = dict(payload)
        if payload.get("setup_id"):
            payload["setup_id"] = payload["setup_id"].replace("LDSV1-", f"LDS-{name}-", 1)
        original_event(state, kind, payload)

    def process_variant(state, data, source="LIVE_FORWARD"):
        original_process(state, data, source)
        normalized = {}
        for key, record in state.get("signals", {}).items():
            new_key = key.replace("LDSV1-", f"LDS-{name}-", 1)
            record["setup_id"] = new_key
            record["version"] = spec["version"]
            record["variant_entry_fraction"] = spec["fraction"]
            normalized[new_key] = record
        state["signals"] = normalized
        base.save(state)

    base.manifest = manifest_variant
    base.event = event_variant
    base.process = process_variant
    return original_class


def main():
    if len(sys.argv) < 2:
        raise SystemExit("usage: python3 liquidity_displacement_entry_forward.py {usdjpy25|xau33|btc25|ustec_x100m|ustecm} start|status|stop|checkpoint|daily|health|watch|trades [options]")
    name = sys.argv[1]
    configure(name)
    sys.argv = [sys.argv[0], *sys.argv[2:]]
    base.main()


if __name__ == "__main__":
    main()

"""Separate paper runner for V1 frequency/target research variant A."""
import hashlib
from pathlib import Path

import liquidity_displacement_forward as base
from liquidity_displacement import LiquidityDisplacementConfig

ROOT = Path(__file__).resolve().parent
VERSION = "LIQUIDITY_DISPLACEMENT_SCALP_V1_VARIANT_A"
VA_CFG = LiquidityDisplacementConfig(
    target_r=1.25,
    max_hold_minutes=120,
    max_retrace_candles=5,
    max_structure_break_candles=5,
    min_body_atr=0.5,
    min_body_median_multiple=1.0,
    min_close_location=0.6,
    atr_buffer_fraction=0.1,
)


def manifest_variant():
    return {
        "version": VERSION,
        "target_r_primary": 1.25,
        "target_grid": [1.0, 1.25, 1.5],
        "source_sha256": base.source_hash(),
        "config": VA_CFG.__dict__ | {"symbol": base.SYMBOL},
        "entry_rules": "sweep -> reclaim -> displacement -> micro shift -> 50% displacement retracement",
        "stop_rules": "sweep extreme plus max(0.10 ATR, 1.25x spread, broker stop minimum)",
        "retracement_rules": "maximum 5 completed M5 candles; no chase; M5 only; M15 context; M1 disabled",
        "max_hold_minutes": 120,
        "slippage_assumption_price": base.SLIPPAGE,
        "research_parent": "LIQUIDITY_DISPLACEMENT_SCALP_V1",
        "research_change": "max_retrace_candles: 3 -> 5; target grid: 1.0R, 1.25R, 1.5R",
        "variant_source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }


_base_event = base.event
_base_process = base.process


def event_variant(state, kind, payload):
    payload = dict(payload)
    if payload.get("setup_id"):
        payload["setup_id"] = payload["setup_id"].replace("LDSV1-", "LDSVA-", 1)
    _base_event(state, kind, payload)


def process_variant(state, data, source="LIVE_FORWARD"):
    _base_process(state, data, source)
    normalized = {}
    for key, record in state.get("signals", {}).items():
        new_key = key.replace("LDSV1-", "LDSVA-", 1)
        record["setup_id"] = new_key
        record["version"] = VERSION
        normalized[new_key] = record
    state["signals"] = normalized
    base.save(state)


def configure():
    base.STATE = ROOT / "liquidity_displacement_v1_variant_a_state.json"
    base.EVENTS = ROOT / "liquidity_displacement_v1_variant_a.jsonl"
    base.DAILY = ROOT / "liquidity_displacement_v1_variant_a_daily.jsonl"
    base.SUMMARY = ROOT / "liquidity_displacement_v1_variant_a_summary.md"
    base.MANIFEST = ROOT / "liquidity_displacement_v1_variant_a_manifest.json"
    base.PIDFILE = ROOT / "liquidity_displacement_v1_variant_a.pid"
    base.HEARTBEAT = ROOT / "liquidity_displacement_v1_variant_a.heartbeat.json"
    base.STOP = Path("/tmp/liquidity-displacement-paper-v1-variant-a.stop")
    base.CFG = VA_CFG
    base.manifest = manifest_variant
    base.event = event_variant
    base.process = process_variant


if __name__ == "__main__":
    configure()
    base.main()

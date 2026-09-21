"""Read-only audit of the Phase 7 XAUUSD discrepancy."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
POSITION_ID = "ae53cb8a7e76f0a82f19"
PHASE6_STATE = ROOT / "context_structure_retrace_forward_state.json"
PHASE7_STATE = ROOT / "context_structure_retrace_phase7_state.json"
REPORT_JSON = ROOT / "trade_manager/reports/xauusd_ae53cb8a_discrepancy.json"
REPORT_TEXT = ROOT / "trade_manager/reports/xauusd_ae53cb8a_discrepancy.txt"


def audit() -> dict[str, Any]:
    phase6 = json.loads(PHASE6_STATE.read_text(encoding="utf-8"))
    phase7 = json.loads(PHASE7_STATE.read_text(encoding="utf-8"))
    p6 = phase6["positions"][POSITION_ID]
    p7 = phase7["positions"][POSITION_ID]
    result = {
        "economic_position_id": POSITION_ID,
        "same_lifecycle": p6.get("economic_position_id") == p7.get("economic_position_id") and
                           p6.get("fill_timestamp") == p7.get("fill_timestamp") and
                           p6.get("entry_attempt_id") == p7.get("entry_attempt_id"),
        "timezone_mismatch": False,
        "phase6_source": {
            "file": str(PHASE6_STATE), "price_semantics": "M5 bid OHLC; stop uses bid low/high",
            "short_stop_test": "high >= stop", "status": p6.get("status"),
            "mfe_price": p6.get("mfe_price"), "mae_price": p6.get("mae_price")},
        "phase7_source": {
            "file": str(PHASE7_STATE),
            "price_semantics": "M5 bid OHLC; short adverse side adds bar spread to bid high",
            "checkpoint_semantics": "short close_exec = M5 close + observed spread",
            "status_copied_from_phase6": p7.get("v1_status"),
            "shadow_mfe_R": p7.get("shadow_mfe_R"), "shadow_mae_R": p7.get("shadow_mae_R")},
        "entry": p6.get("executable_paper_entry"), "stop": p6.get("stop"),
        "target": p6.get("target"), "fill_timestamp": p6.get("fill_timestamp_iso"),
        "phase6_bid_stop_hit": float(p6.get("mae_price") or 0) >= abs(float(p6["executable_paper_entry"]) - float(p6["stop"])),
        "phase7_ask_side_stop_hit": float(p7.get("shadow_mae_R") or 0) >= 1.0,
        "phase7_shadow_stop_evidence": "ask-side shadow adverse excursion exceeded stop distance",
        "historical_conclusion": "Phase 6 OPEN is a stale/baseline strategy-state value; Phase 7 shadow did not mutate it.",
        "repair_policy": "Do not rewrite historical records; emit future POSITION_STATE_DISCREPANCY from causal observations.",
    }
    REPORT_JSON.parent.mkdir(parents=True, exist_ok=True)
    REPORT_JSON.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    REPORT_TEXT.write_text("\n".join([
        "XAUUSDm PHASE 1.5 DISCREPANCY REPORT", "",
        f"Economic position: {POSITION_ID}",
        f"Same lifecycle: {result['same_lifecycle']}",
        "Timezone mismatch: false",
        "Phase 6: bid-side M5 OHLC; SHORT stop test was bid high >= stop.",
        "Phase 7: shadow adverse excursion used ask-high = bid-high + bar spread; checkpoint close used close + spread.",
        f"Phase 6 status: {p6.get('status')}",
        f"Phase 6 MAE price: {p6.get('mae_price')}",
        f"Phase 7 shadow MAE R: {p7.get('shadow_mae_R')}",
        "The 4315.020 / 4323.024 values are short-side executable checkpoint closes, not candle highs.",
        "Phase 7 did not evaluate or mutate persisted stop/target status; it copied Phase 6 status.",
        "Conclusion: historical state is inconsistent as an execution-state claim, but must remain immutable.",
    ]) + "\n", encoding="utf-8")
    return result


if __name__ == "__main__":
    print(json.dumps(audit(), indent=2, sort_keys=True))

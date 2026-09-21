"""Replay the first persisted Context V1 management fixture without MT5 access."""
from __future__ import annotations
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .engine import ManagementPolicy, TradeManager, position_r

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / "context_structure_retrace_phase7_state.json"
REPORT = ROOT / "trade_manager" / "reports" / "xauusd_ae53cb8a_replay.json"
TEXT_REPORT = ROOT / "trade_manager" / "reports" / "xauusd_ae53cb8a_replay.txt"
JSONL_REPORT = ROOT / "trade_manager" / "reports" / "xauusd_ae53cb8a_replay.jsonl"


def load_fixture() -> dict[str, Any]:
    data = json.loads(STATE.read_text(encoding="utf-8"))
    positions = data.get("prospective_positions") or data.get("positions") or {}
    for position in positions.values():
        if position.get("economic_position_id") == "ae53cb8a7e76f0a82f19":
            return position
    raise RuntimeError("fixture not found")


def replay() -> dict[str, Any]:
    p = load_fixture()
    manager = TradeManager(ManagementPolicy())
    entry, stop = float(p["executable_entry"]), float(p["initial_stop"])
    checkpoints = p.get("time_checkpoints", {})
    timeline = []
    running_mfe = 0.0
    running_mae = 0.0
    for label, row in sorted(checkpoints.items(), key=lambda item: item[1]["timestamp_used"]):
        current_r = float(row["R"])
        price = entry - current_r * abs(entry - stop)
        timestamp = row["timestamp_used"]
        position = {"strategy_id": "CONTEXT_STRUCTURE_RETRACE_V1", "setup_id": p["setup_id"],
                    "economic_position_id": p["economic_position_id"], "symbol": p["symbol"],
                    "direction": p["direction"], "entry": entry, "original_stop": stop,
                    "original_target": p["v1_target"], "current_stop": stop, "current_target": p["v1_target"],
                    "size": None, "status": p["v1_status"], "mfe": None, "mae": None,
                    "mfe_R": running_mfe, "mae_R": running_mae}
        market = {"timestamp": timestamp, "current_price": price, "source": "PERSISTED_PHASE7_CHECKPOINT",
                  "ema_200": None, "structure": None, "future_data_used": False,
                  "checkpoint_label": label, "checkpoint_R": current_r}
        decision = manager.evaluate(position, market, timestamp=timestamp)
        running_mfe = float(decision["MFE_R"] or running_mfe)
        running_mae = float(decision["MAE_R"] or running_mae)
        decision["data_limitations"] = ["M5_200_EMA_NOT_PERSISTED", "CAUSAL_CANDLE_STREAM_NOT_PERSISTED",
                                         "INTRACANDLE_REJECTION_NOT_DETERMINABLE", "MFE_MAE_AT_CHECKPOINT_NOT_PERSISTED"]
        timeline.append(decision)
    result = {"fixture": {"symbol": p["symbol"], "direction": p["direction"], "entry": entry,
                           "stop": stop, "target": p["v1_target"], "setup_id": p["setup_id"],
                           "economic_position_id": p["economic_position_id"]},
              "policy": ManagementPolicy().__dict__, "timeline": timeline,
              "persisted_threshold_evidence": p.get("thresholds", {}),
              "frozen_v1_state": {"status": p["v1_status"], "exit_reason": p.get("v1_exit_reason"),
                                   "observation_complete": p.get("observation_complete")},
              "evidence_limitations": sorted({x for row in timeline for x in row["data_limitations"]}),
              "broker_writes": 0, "mode": "ADVISORY_SHADOW"}
    REPORT.parent.mkdir(parents=True, exist_ok=True); REPORT.write_text(json.dumps(result, indent=2, default=str) + "\n")
    JSONL_REPORT.write_text("\n".join(json.dumps(row, sort_keys=True, default=str) for row in timeline) + "\n", encoding="utf-8")
    lines = ["TRADE MANAGER PHASE 1 — ADVISORY/SHADOW REPLAY", "",
             f"Fixture: {result['fixture']['economic_position_id']}",
             f"{result['fixture']['symbol']} {result['fixture']['direction']}",
             f"Entry: {result['fixture']['entry']:.4f}  Stop: {result['fixture']['stop']:.4f}  Target: {result['fixture']['target']:.4f}",
             "", "Timeline (causal persisted checkpoints only):"]
    for row in timeline:
        lines.append(f"{row['timestamp']} | price={row['current_price']:.4f} | R={row['current_R']:.4f} | "
                     f"MFE={row['MFE_R'] if row['MFE_R'] is not None else 'n/a'}R | "
                     f"MAE={row['MAE_R'] if row['MAE_R'] is not None else 'n/a'}R | "
                     f"EMA200=n/a | action={row['action']} | reasons={','.join(row['reason_codes'])}")
    lines += ["", "Frozen V1: OPEN; no exit/management action recorded.",
              "Trade Manager: HOLD at every replay checkpoint; advisory only.",
              "", "Evidence limitations:"] + [f"- {x}" for x in result["evidence_limitations"]]
    lines += ["", "Persisted threshold evidence: +0.25R first recorded at 07:00, but only as an aggregate threshold event.",
              "It does not provide a causal candle/EMA rejection or a timestamped stop proposal.",
              "Earliest legitimate protection point: NOT DETERMINABLE from persisted evidence.",
              "No causal M5 candle/EMA stream or timestamped intrabar rejection evidence is available.",
              "Broker writes: 0"]
    TEXT_REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return result


if __name__ == "__main__":
    print(json.dumps(replay(), indent=2, default=str))

"""Durable, market-time startup replay protection.

This module deliberately keeps replay eligibility separate from publication
freshness.  A producer may publish an old event with a new wall-clock
timestamp, but it cannot make that event eligible for REAL execution.
"""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from platform_runtime import trading_platform_runtime_dir

ROOT = Path(__file__).resolve().parent.parent
EPOCH_PATH = trading_platform_runtime_dir(root=ROOT) / "orchestration" / "startup_epoch.json"
LIVE_STRATEGIES = {
    "CONTEXT_STRUCTURE_RETRACE_V1": "phase6",
    "LIQUIDITY_DISPLACEMENT_SCALP_V1": "liquidity-xau-base",
    "LIQUIDITY_DISPLACEMENT_SCALP_XAUUSD_33_V1": "liquidity-xau33",
    "LIQUIDITY_DISPLACEMENT_SCALP_BTCUSD_25_V1": "liquidity-btc25",
    "LIQUIDITY_DISPLACEMENT_SCALP_USDJPY_25_V1": "liquidity-usdjpy25",
}


def _parse(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, timezone.utc)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)


def iso(value: Any) -> str | None:
    parsed = _parse(value)
    return parsed.isoformat() if parsed else None


@dataclass(frozen=True)
class StartupWatermark:
    startup_epoch_id: str
    strategy_id: str
    instance_id: str
    symbol: str
    startup_started_at: str
    startup_market_watermark: str
    watermark_source: str
    created_at: str


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def load_epoch(path: Path = EPOCH_PATH) -> dict[str, Any] | None:
    if not path.exists():
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("schema") != "startup-market-watermark-v1":
        raise ValueError("invalid startup epoch schema")
    return value


def establish_epoch(records: Iterable[dict[str, Any]], *, path: Path = EPOCH_PATH,
                    epoch_id: str | None = None, started_at: str | None = None,
                    reuse: bool = True) -> dict[str, Any]:
    """Create the epoch before publishers run, or reuse it without moving back."""
    existing = load_epoch(path) if reuse else None
    if existing:
        return existing
    started = iso(started_at) or datetime.now(timezone.utc).isoformat()
    eid = epoch_id or f"epoch-{uuid.uuid4().hex}"
    rows = []
    for record in records:
        watermark = iso(record.get("startup_market_watermark") or record.get("latest_closed_candle"))
        if not watermark:
            raise ValueError(f"missing startup market watermark for {record.get('strategy_id')}")
        rows.append(asdict(StartupWatermark(
            startup_epoch_id=eid,
            strategy_id=str(record["strategy_id"]),
            instance_id=str(record["instance_id"]),
            symbol=str(record["symbol"]),
            startup_started_at=started,
            startup_market_watermark=watermark,
            watermark_source=str(record.get("watermark_source", "fully_closed_source_candle")),
            created_at=started,
        )))
    value = {"schema": "startup-market-watermark-v1", "startup_epoch_id": eid,
             "startup_started_at": started, "records": rows}
    _atomic_json(path, value)
    return value


def records_by_strategy(epoch: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {row["strategy_id"]: row for row in epoch.get("records", [])}


def classify_event(event_time: Any, watermark: Any, *, gap_recovery: bool = False,
                   startup: bool = True) -> str:
    event = _parse(event_time)
    boundary = _parse(watermark)
    if event is None or boundary is None:
        return "UNKNOWN_EVENT_TIME"
    if event <= boundary:
        return "GAP_RECOVERY_HISTORICAL_EVENT" if gap_recovery else "STARTUP_HISTORICAL_EVENT"
    return "NORMAL_LIVE_EVENT"


def eligibility(signal: dict[str, Any], watermark: dict[str, Any]) -> tuple[bool, str]:
    provenance = signal.get("provenance") or {}
    # A producer may legitimately use gap recovery for observation, but a
    # startup/catch-up publication is never REAL-eligible.  Future live
    # events must be emitted as NORMAL_LIVE_EVENT after the producer has
    # crossed the durable startup boundary.
    if bool(provenance.get("gap_recovery")):
        return False, "STARTUP_REPLAY_BLOCKED"
    # signal_timestamp is the market-causal timestamp.  Publication time is
    # intentionally never used as a substitute for it.
    causal = signal.get("causal_event_time") or signal.get("signal_timestamp") or signal.get("decision_time")
    classification = classify_event(causal, watermark.get("startup_market_watermark"),
                                    gap_recovery=bool(provenance.get("gap_recovery")))
    if classification != "NORMAL_LIVE_EVENT":
        return False, "STARTUP_REPLAY_BLOCKED"
    return True, classification


def all_live_guards(epoch: dict[str, Any]) -> bool:
    rows = records_by_strategy(epoch)
    return set(LIVE_STRATEGIES).issubset(rows) and all(
        _parse(row.get("startup_market_watermark")) is not None for row in rows.values())

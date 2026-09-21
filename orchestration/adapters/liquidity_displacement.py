from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from orchestration.models import StrategySignal, stable_id


class LiquidityDisplacementAdapter:
    """Canonicalize filled observations from the isolated paper runner."""

    strategy_id = "LIQUIDITY_DISPLACEMENT_SCALP_V1"
    strategy_version = "V1"

    def __init__(self, root: Path, freeze_timestamp: str):
        self.root = root
        self.freeze_timestamp = freeze_timestamp
        self.state_path = root / "liquidity_displacement_forward_state.json"

    @staticmethod
    def _float(value: Any, default: float = 0.0) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _epoch(value: Any) -> int:
        if isinstance(value, (int, float)):
            return int(value)
        return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp())

    def discover_new_signals(self, seen_signal_ids: set[str]) -> list[StrategySignal]:
        if not self.state_path.exists():
            return []
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        boundary = self._epoch(self.freeze_timestamp)
        result: list[StrategySignal] = []
        for record in state.get("signals", {}).values():
            fill_timestamp = record.get("simulated_fill_timestamp")
            if not fill_timestamp or self._epoch(fill_timestamp) < boundary:
                continue
            identity = {
                "strategy_id": self.strategy_id,
                "strategy_version": self.strategy_version,
                "strategy_instance_id": "forward-paper",
                "source_event_id": f"liquidity-displacement:setup:{record.get('setup_id')}",
            }
            signal_id = stable_id("SIG", identity)
            if signal_id in seen_signal_ids:
                continue
            entry = self._float(record.get("entry_realistic"), self._float(record.get("entry_theoretical")))
            stop = self._float(record.get("stop_loss"))
            target = self._float(record.get("target_theoretical"))
            risk_distance = abs(entry - stop)
            target_distance = abs(target - entry)
            symbol = str(record.get("symbol") or "XAUUSDm")
            result.append(StrategySignal(
                signal_id=signal_id, schema_version="strategy-signal-v1",
                strategy_id=self.strategy_id, strategy_version=self.strategy_version,
                strategy_instance_id="forward-paper", source_event_id=identity["source_event_id"],
                market_event_id=None, setup_id=record.get("setup_id"),
                entry_opportunity_id=record.get("setup_id"), economic_position_id=None,
                created_at=str(record.get("detected_at") or fill_timestamp),
                signal_timestamp=str(fill_timestamp), symbol=symbol,
                canonical_symbol=symbol.rstrip("m"), broker_symbol_hint=symbol,
                direction=str(record.get("direction") or ""), entry_type="MARKET_PAPER_OBSERVATION",
                entry_price=entry, stop_price=stop, target_price=target,
                risk_distance=risk_distance, target_distance=target_distance,
                target_r=(target_distance / risk_distance) if risk_distance else None,
                timeframe="M5", lower_timeframe=None, higher_timeframes=("M15",),
                entry_mechanism=(str(record.get("fill_confirmation") or "COMPLETED_CANDLE_RETRACEMENT"),),
                strategy_metadata={
                    "source": record.get("source"), "classification": record.get("classification"),
                    "sweep_timestamp": record.get("sweep_timestamp"),
                    "reclaim_timestamp": record.get("reclaim_timestamp"),
                    "displacement_timestamp": record.get("displacement_timestamp"),
                    "structure_break_timestamp": record.get("structure_break_timestamp"),
                    "spread_at_entry_price": record.get("spread_at_entry_price"),
                },
                provenance={"source_process": "liquidity_displacement_forward.py",
                            "source_state_reference": str(self.state_path),
                            "source_strategy_fingerprint": record.get("version"),
                            "classification": "PROSPECTIVE_ORCHESTRATOR_SIGNAL",
                            "orchestrator_freeze_timestamp": self.freeze_timestamp,
                            "paper_only": True},
            ))
        return result

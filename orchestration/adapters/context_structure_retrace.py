from __future__ import annotations

import json
from datetime import datetime, timezone
import os
from pathlib import Path
from typing import Any

from orchestration.models import StrategySignal, stable_id
from outcome_attribution import target_distance
from orchestration.replay_guard import EPOCH_PATH, eligibility, load_epoch, records_by_strategy


class ContextStructureRetraceAdapter:
    strategy_id = "CONTEXT_STRUCTURE_RETRACE_V1"
    strategy_version = "V1"

    def __init__(self, root: Path, freeze_timestamp: str, instance: dict[str, Any] | None = None):
        self.root = root
        self.freeze_timestamp = freeze_timestamp
        self.instance = instance or {}
        self.policy = self.instance.get("instance_policy") or {}
        # Membership rows are canonical (BTCUSD), while the forward producer's state uses
        # provider symbols (BTCUSDm). Store the membership set canonically and normalize the
        # producer symbol before applying the runtime filter.
        self.active_instruments = {self._canonical_instrument(symbol)
                                   for symbol in self.instance.get("active_instruments") or []}
        # Phase6's active producer writes the compact runtime.  Do not fall
        # back to the removed legacy full-state file: doing so hides a live
        # source failure as an empty/old pipeline.
        # The runner's artifacts live in CONTEXT_RUNNER_STATE_DIR when it runs from a release image.
        state_dir = Path(os.environ.get("CONTEXT_RUNNER_STATE_DIR") or root)
        self.state_path = state_dir / "context_structure_retrace_forward_state_compact.json"
        self.manifest_path = state_dir / "context_structure_retrace_forward_manifest.json"

    @staticmethod
    def _epoch(value: Any) -> int:
        if isinstance(value, (int, float)):
            return int(value)
        return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp())

    @staticmethod
    def _canonical_instrument(symbol: Any) -> Any:
        if isinstance(symbol, str) and symbol.endswith("m"):
            return symbol[:-1]
        return symbol

    def discover_new_signals(self, seen_signal_ids: set[str]) -> list[StrategySignal]:
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        result = []
        boundary = self._epoch(self.freeze_timestamp)
        for setup in state.get("setups", {}).values():
            for position in setup.get("opportunities", []):
                # A completed Phase6 opportunity is historical evidence, not
                # a new StrategySignal candidate.  In particular, this keeps
                # a missed opportunity from being replayed after a repair.
                if (setup.get("target_completed") is True or
                        position.get("status") in {"TARGET_HIT", "STOPPED", "CLOSED"} or
                        position.get("exit_reason") in {"TARGET_HIT", "STOPPED"}):
                    continue
                fill_ts = self._epoch(position.get("fill_timestamp") or 0)
                if fill_ts < boundary:
                    continue
                source_event_id = f"phase6:economic_position:{position['economic_position_id']}"
                identity = {"strategy_id": self.strategy_id, "strategy_version": self.strategy_version,
                            "strategy_instance_id": "phase6", "economic_position_id": position.get("economic_position_id"),
                            "entry_opportunity_id": position.get("entry_opportunity_id"), "source_event_id": source_event_id}
                signal_id = stable_id("SIG", identity)
                if signal_id in seen_signal_ids:
                    continue
                geometry = position.get("geometry") or {}
                direction = setup.get("direction") or position.get("direction")
                symbol = setup.get("symbol") or position.get("symbol")
                canonical_symbol = self._canonical_instrument(symbol)
                pattern = setup.get("pattern")
                enabled_patterns = self.policy.get("enabled_setup_events")
                if enabled_patterns and pattern not in set(enabled_patterns):
                    continue
                if self.active_instruments and canonical_symbol not in self.active_instruments:
                    continue
                if self.policy.get("reentry_enabled") is False and position.get("reentry_type") not in (None, "INITIAL"):
                    continue
                created = datetime.now(timezone.utc).isoformat()
                source_provenance = setup.get("provenance") or {}
                position_provenance = position.get("provenance") or {}
                gap_recovery = position_provenance.get("gap_recovery", source_provenance.get("gap_recovery"))
                result.append(StrategySignal(signal_id=signal_id, schema_version="strategy-signal-v1",
                    strategy_id=self.strategy_id, strategy_version=self.strategy_version, strategy_instance_id="phase6",
                    source_event_id=source_event_id, market_event_id=setup.get("market_event_id"), setup_id=setup.get("setup_id"),
                    entry_opportunity_id=position.get("entry_opportunity_id"), economic_position_id=position.get("economic_position_id"),
                    created_at=created, signal_timestamp=position.get("fill_timestamp_iso") or str(position.get("fill_timestamp")),
                    symbol=symbol, canonical_symbol=canonical_symbol, broker_symbol_hint=symbol,
                    direction=direction, entry_type="MARKET_PAPER_OBSERVATION",
                    entry_price=float(position.get("executable_paper_entry")), stop_price=float(position.get("stop")),
                    target_price=float(position.get("target")), risk_distance=float(geometry.get("stop_distance") or 0),
                    target_distance=target_distance(position.get("executable_paper_entry"), position.get("target")), target_r=geometry.get("target_R"),
                    timeframe="M15", lower_timeframe="M5", higher_timeframes=("H1", "H4"),
                    entry_mechanism=tuple(position.get("entry_mechanisms", [])),
                    strategy_metadata={"pattern": pattern, "reentry_type": position.get("reentry_type"),
                                       "v1_status": position.get("status"),
                                       "instance_policy_revision": self.policy.get("revision", 0),
                                       "time_exit_minutes": self.policy.get("time_exit_minutes"),
                                       "net_profit_target_usd": self.policy.get("net_profit_target_usd"),
                                       "profit_target_pips": self.policy.get("profit_target_pips"),
                                       "profit_target_r": self.policy.get("profit_target_r"),
                                       # Optional explicit broker pip size.  When absent, the
                                       # execution boundary must obtain symbol metadata; no
                                       # decimal-place heuristic is inferred here.
                                       "pip_size": self.policy.get("pip_size")},
                    decision_time=position.get("fill_timestamp_iso") or str(position.get("fill_timestamp")),
                    signal_emitted_at=created,
                    provenance={"source_process": "context_structure_retrace_forward.py", "source_pid": None,
                                "source_state_reference": str(self.state_path), "source_strategy_fingerprint": "6dda2523e15edbc0e2d123878367f21ffaec70219272aa409193c2fc45b7c9bc",
                                "source_config_hash": "1f1da2a63d69ac79e4aca21d0de33c860e76f4c33d9bd321cb50b20353114e1e",
                                "instance_policy_revision": self.policy.get("revision", 0),
                                "instance_policy_fingerprint": self.policy.get("fingerprint"),
                                "classification": "PROSPECTIVE_ORCHESTRATOR_SIGNAL",
                                "orchestrator_freeze_timestamp": self.freeze_timestamp,
                                # These fields are copied when supplied by a
                                # producer/outbox event; absence is unsafe for
                                # REAL eligibility and is never inferred from
                                # runner liveness.
                                "source_market_data_timestamp": position_provenance.get("source_market_data_timestamp", source_provenance.get("source_market_data_timestamp")),
                                "source_data_age": position_provenance.get("source_data_age", source_provenance.get("source_data_age")),
                                "source_read_health": position_provenance.get("source_read_health", source_provenance.get("source_read_health")),
                                # Preserve absent safety provenance as absent;
                                # the execution consumer rejects it closed.
                                "gap_recovery": gap_recovery }))
                candidate = result[-1]
                epoch = load_epoch(EPOCH_PATH)
                watermark = records_by_strategy(epoch or {}).get(self.strategy_id)
                if watermark and not eligibility(candidate.to_dict(), watermark)[0]:
                    result.pop()
        return result

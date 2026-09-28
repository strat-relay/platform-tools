"""Research-only optimized Context raw replay adapter.

The only change from the committed adapter is evaluation order: detect the
same completed M15 patterns first, then build the feature snapshot only when
there is a pattern to materialize.  No output-bearing path or strategy rule is
changed.
"""
from __future__ import annotations

from typing import Any

from context_structure_retrace.replay import feature_snapshot
from research.context_fast_features import fast_feature_snapshot
from context_structure_retrace.patterns import detect_patterns
from context_structure_retrace_forward import _geometry, _spread, make_setup
from strategy_backtest.models import EntrySignal, SetupLifecycleEvent
import strategy_backtest.raw_ohlc_adapters as raw_adapters
from strategy_backtest.raw_ohlc_adapters import ContextRawOhlcEvaluator, _quote_contract
from research.context_feature_tape import CausalFeatureTape


class OptimizedContextRawOhlcEvaluator(ContextRawOhlcEvaluator):
    """Same evaluator semantics with the no-pattern fast path."""

    VERSION = "CONTEXT_STRUCTURE_RETRACE_INTRADAY_V1_RAW_OHLC_ADAPTER_RESEARCH_OPTIMIZED"
    FEATURE_TAPE: CausalFeatureTape | None = None

    @staticmethod
    def _retain_waiting_setup_state(setup: dict[str, Any]) -> dict[str, Any]:
        """Keep only fields consumed after setup creation.

        The parent geometry/retrace path needs the originating bar, entry
        level, direction/timestamp, and the structure zones plus M15 ATR.
        All other snapshot branches are observational evidence that is not
        read again by this adapter.  Retaining them per waiting setup caused
        memory to grow with the number of historical candidates.
        """
        snapshot = setup["context_snapshot"]
        structure_timeframe = snapshot["provenance"]["structure_timeframe"]
        structure_context = snapshot["timeframes"][structure_timeframe]
        m15_context = snapshot["timeframes"].get("M15", {})
        retained_timeframes = {
            "M15": {
                "ema_context": {"atr": m15_context.get("ema_context", {}).get("atr")},
                "sr_context": structure_context.get("sr_context", {}) if structure_timeframe == "M15" else {},
            },
        }
        if structure_timeframe != "M15":
            retained_timeframes[structure_timeframe] = {"sr_context": structure_context.get("sr_context", {})}
        return {
            "setup_id": setup["setup_id"],
            "symbol": setup["symbol"],
            "direction": setup["direction"],
            "pattern": setup["pattern"],
            "setup_timestamp": setup["setup_timestamp"],
            "event_bar": setup["event_bar"],
            "entry_level": setup["entry_level"],
            "status": setup["status"],
            "context_snapshot": {
                "timeframes": retained_timeframes,
                "provenance": {"structure_timeframe": structure_timeframe},
            },
        }

    def _compact_terminal_setups(self) -> None:
        """Release large immutable feature payloads after terminal lifecycle."""
        terminal = {"FILLED", "INVALIDATED_NO_REENTRY", "NO_REMAINING_TARGET_UNDER_CURRENT_SETUP_GEOMETRY"}
        for setup_id, setup in list(self.setups.items()):
            if setup.get("status") in terminal and "context_snapshot" in setup:
                self.setups[setup_id] = {
                    "setup_id": setup_id,
                    "setup_timestamp": setup.get("setup_timestamp"),
                    "status": setup.get("status"),
                    "direction": setup.get("direction"),
                    "pattern": setup.get("pattern"),
                }

    def consume_market_event(self, event: Any) -> tuple[SetupLifecycleEvent | EntrySignal, ...]:
        outputs = super().consume_market_event(event)
        self._compact_terminal_setups()
        return outputs

    def _new_setups(self, event: Any) -> list[SetupLifecycleEvent]:
        replay = raw_adapters._replay(self.state, event.close_timestamp)
        m15 = replay.bars_by_timeframe.get("M15", [])
        if not m15:
            return []
        patterns = [pattern for pattern in detect_patterns(m15, "M15", event.close_timestamp, event.canonical_instrument)
                    if pattern["event_id"] not in self.pattern_ids]
        if not patterns:
            return []
        snapshot = self.FEATURE_TAPE.get(event.close_timestamp) if self.FEATURE_TAPE else None
        if snapshot is None:
            snapshot = fast_feature_snapshot(replay, event.canonical_instrument, event.close_timestamp,
                                             timeframes=self.timeframes)
        quote, contract = _quote_contract(event)
        outputs = []
        for pattern in patterns:
            self.pattern_ids.add(pattern["event_id"])
            setup = make_setup(event.canonical_instrument, pattern, m15[-1], snapshot, quote, contract)
            setup["intraday_variant"] = self.strategy_version.strategy_version_id
            setup["status"] = "WAITING_FOR_RETRACE"
            self.setups[setup["setup_id"]] = self._retain_waiting_setup_state(setup)
            outputs.append(SetupLifecycleEvent(
                setup["setup_id"], self.strategy_version.strategy_version_id,
                event.canonical_instrument, "SETUP_DETECTED", int(pattern["timestamp"]),
                {"parent_pattern": pattern["pattern"], "completed_candle_only": True,
                 "available_through": event.close_timestamp},
            ))
        return outputs

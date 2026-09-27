"""Research-only optimized Context raw replay adapter.

The only change from the committed adapter is evaluation order: detect the
same completed M15 patterns first, then build the feature snapshot only when
there is a pattern to materialize.  No output-bearing path or strategy rule is
changed.
"""
from __future__ import annotations

from typing import Any

from context_structure_retrace.replay import feature_snapshot
from context_structure_retrace.patterns import detect_patterns
from context_structure_retrace_forward import _geometry, _spread, make_setup
from strategy_backtest.models import EntrySignal, SetupLifecycleEvent
import strategy_backtest.raw_ohlc_adapters as raw_adapters
from strategy_backtest.raw_ohlc_adapters import ContextRawOhlcEvaluator, _quote_contract


class OptimizedContextRawOhlcEvaluator(ContextRawOhlcEvaluator):
    """Same evaluator semantics with the no-pattern fast path."""

    VERSION = "CONTEXT_STRUCTURE_RETRACE_INTRADAY_V1_RAW_OHLC_ADAPTER_RESEARCH_OPTIMIZED"

    def _new_setups(self, event: Any) -> list[SetupLifecycleEvent]:
        replay = raw_adapters._replay(self.state, event.close_timestamp)
        m15 = replay.bars_by_timeframe.get("M15", [])
        if not m15:
            return []
        patterns = [pattern for pattern in detect_patterns(m15, "M15", event.close_timestamp, event.canonical_instrument)
                    if pattern["event_id"] not in self.pattern_ids]
        if not patterns:
            return []
        snapshot = feature_snapshot(replay, event.canonical_instrument, event.close_timestamp,
                                     timeframes=self.timeframes)
        quote, contract = _quote_contract(event)
        outputs = []
        for pattern in patterns:
            self.pattern_ids.add(pattern["event_id"])
            setup = make_setup(event.canonical_instrument, pattern, m15[-1], snapshot, quote, contract)
            setup["intraday_variant"] = self.strategy_version.strategy_version_id
            setup["status"] = "WAITING_FOR_RETRACE"
            self.setups[setup["setup_id"]] = setup
            outputs.append(SetupLifecycleEvent(
                setup["setup_id"], self.strategy_version.strategy_version_id,
                event.canonical_instrument, "SETUP_DETECTED", int(pattern["timestamp"]),
                {"parent_pattern": pattern["pattern"], "completed_candle_only": True,
                 "available_through": event.close_timestamp},
            ))
        return outputs

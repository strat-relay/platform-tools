"""Canonical orchestrator adapter for accepted Price Action research entries.

The frozen evaluators remain unchanged.  Only an explicit
``ENTRY_OBSERVED_RESEARCH_ONLY`` evaluation is promoted to a research signal;
proposals, cancellations, and rejected candidates stay in the research ledger.
The orchestrator publishes the signal and the Unified Outcome Resolver owns
the theoretical outcome lifecycle.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from orchestration.models import StrategySignal, stable_id

SR_RETEST_STRATEGY_ID = "PRICE_ACTION_SUPPORT_RESISTANCE_RETEST"
SR_RETEST_INSTANCE_ID = "7f1d6e5a-5ab3-4b59-8d2f-0c6f9e3d0104"
SR_RETEST_STRATEGY_KEY = "sr_retest"

ENGULFING_STRATEGY_ID = "PRICE_ACTION_ENGULFING_CONTINUATION"
ENGULFING_INSTANCE_ID = "7f1d6e5a-5ab3-4b59-8d2f-0c6f9e3d0204"
ENGULFING_STRATEGY_KEY = "engulfing_continuation"

ACCEPTED_REASON = "ENTRY_OBSERVED_RESEARCH_ONLY"
# Outcome contract max hold — no built-in exit policy in the frozen V1 evaluators, so
# the resolver uses this bound to time-exit unresolved signals rather than leaving them open.
MAX_HOLD_MINUTES = 1440


def _iso_epoch(value: Any) -> str:
    return datetime.fromtimestamp(int(value), tz=timezone.utc).isoformat().replace("+00:00", "Z")


class PriceActionObserverAdapter:
    strategy_version = "V1"

    def __init__(self, conn: Any, *,
                 strategy_id: str = SR_RETEST_STRATEGY_ID,
                 instance_id: str = SR_RETEST_INSTANCE_ID,
                 strategy_key: str = SR_RETEST_STRATEGY_KEY) -> None:
        self.conn = conn
        self.strategy_id = strategy_id
        self.instance_id = instance_id
        self.strategy_key = strategy_key

    def discover_new_signals(self, seen_signal_ids: set[str]) -> list[StrategySignal]:
        with self.conn.cursor() as cur:
            cur.execute(
                """SELECT event_id, instrument, payload, observed_at
                     FROM research.price_action_observer_evaluation
                    WHERE instance_id=%s
                      AND strategy_key=%s
                      AND payload->'event'->'provenance'->>'rejection_reason'=%s
                    ORDER BY observed_at, event_id""",
                (self.instance_id, self.strategy_key, ACCEPTED_REASON),
            )
            rows = cur.fetchall()
        result: list[StrategySignal] = []
        for event_id, instrument, payload, observed_at in rows:
            event = payload.get("event") or {}
            provenance = event.get("provenance") or {}
            if provenance.get("entry_cancelled") is True:
                continue
            signal_id = stable_id("SIG", {"strategy_id": self.strategy_id, "strategy_version": self.strategy_version,
                                           "strategy_instance_id": self.instance_id, "source_event_id": event_id})
            if signal_id in seen_signal_ids:
                continue
            entry = float(provenance["entry_open"])
            stop = float(provenance["stop"])
            target = float(provenance["target"])
            direction = str(provenance["direction"])
            decision_time = _iso_epoch(provenance["information_cutoff"])
            emitted_at = observed_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
            risk = abs(entry - stop)
            reward = abs(target - entry)
            result.append(StrategySignal(
                signal_id=signal_id, schema_version="strategy-signal-v1",
                strategy_id=self.strategy_id, strategy_version=self.strategy_version,
                strategy_instance_id=self.instance_id,
                source_event_id=f"price-action-observer:{event_id}", market_event_id=None,
                setup_id=event.get("candidate_id"), entry_opportunity_id=event.get("candidate_id"),
                economic_position_id=None, created_at=emitted_at,
                signal_timestamp=decision_time, symbol=str(instrument),
                canonical_symbol=str(instrument), broker_symbol_hint=f"{instrument}m",
                direction=direction, entry_type="MARKET_PAPER_OBSERVATION",
                entry_price=entry, stop_price=stop, target_price=target,
                risk_distance=risk, target_distance=reward,
                target_r=(reward / risk) if risk else None, timeframe="M15",
                lower_timeframe=None, higher_timeframes=("H1",),
                entry_mechanism=("RESEARCH_ENTRY_OBSERVED",),
                strategy_metadata={
                    "research_only": True, "execution_eligible": False,
                    "parameter_hash": provenance.get("parameter_hash"),
                    "data_snapshot_id": provenance.get("data_snapshot_id"),
                    "outcome_contract": {
                        "version": "entry-outcome.v2", "timeframe_minutes": 15,
                        "activation": "SIGNAL_TIMESTAMP", "time_exit_price": "CLOSE",
                        "price_basis": "THEORETICAL_TOUCH",
                        "same_candle_priority": "AMBIGUOUS_INTRABAR",
                        "time_exit_priority": "AFTER_PRICE",
                        "max_hold_minutes": MAX_HOLD_MINUTES,
                    },
                },
                provenance={
                    "source_process": "research.price_action_forward_runtime",
                    "source_strategy_fingerprint": provenance.get("parameter_hash"),
                    "source_market_data_timestamp": emitted_at, "source_data_age": 0,
                    "source_read_health": True, "gap_recovery": False,
                    "classification": "PROSPECTIVE_ORCHESTRATOR_SIGNAL",
                    "orchestrator_freeze_timestamp": decision_time,
                    # Broker symbol for resolver candle lookup (m-suffix Exness convention).
                    "provider_symbol": f"{instrument}m",
                }, decision_time=decision_time, signal_emitted_at=emitted_at,
            ))
        return result

    def after_publish_hook(self, signal_id: str, conn: Any) -> None:
        from observability.strategy_audit import audit
        audit("signal_published_to_orchestrator", runner="signal-orchestrator",
              strategy_id=self.strategy_id, signal_id=signal_id,
              outcome_writer="UNIFIED_OUTCOME_RESOLVER", research_only=True)

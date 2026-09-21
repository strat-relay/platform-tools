from __future__ import annotations

from typing import Any, Mapping

from core.strategies.evaluation import (
    Decision, DecisionTrace, Evaluation, ReasonCode, StageResult, StageStatus,
    TraceFidelity, default_reason_codes,
)


_CODES = default_reason_codes()


def _reason(code: str) -> ReasonCode:
    return _CODES.get(code)


class LiquidityLegacyRuntime:
    """Wrap an existing LiquidityDisplacementStrategy result without modifying it.

    L2 is honest here: a returned candidate exposes the detector's observed
    fields, while a ``None`` result cannot be reverse-engineered into failed
    stages and is represented as an unobserved legacy outcome.
    """

    runtime_version = "liquidity-legacy-adapter.v1"

    def __init__(self, strategy: Any, *, strategy_id: str = "LIQUIDITY_DISPLACEMENT_SCALP_V1",
                 strategy_version: str = "V1", parameter_set_id: str | None = None):
        self.strategy = strategy
        self.strategy_id = strategy_id
        self.strategy_version = strategy_version
        self.parameter_set_id = parameter_set_id

    def evaluate(self, inputs: Mapping[str, Any], *, decision_time: str,
                 as_of: str | None = None) -> Evaluation:
        result = self.strategy.evaluate(inputs["m15"], inputs["m5"], inputs["quote"],
                                        inputs["contract"], decision_time, inputs["i"])
        instrument = str(inputs.get("instrument") or getattr(self.strategy.config, "symbol", "UNKNOWN"))
        provenance = {"adapter": self.runtime_version, "trace_limit": "legacy detector fields only",
                      "as_of": as_of, "data_digest": inputs.get("data_digest"),
                      "market_source": inputs.get("market_source")}
        if not result:
            trace = DecisionTrace(
                stages=(StageResult("legacy_detector", StageStatus.NOT_EVALUATED,
                                    reason_code=_reason("LEGACY_TRACE_LIMITED")),),
                decision=Decision.NO_CANDIDATE, reason_codes=(_reason("NO_CANDIDATE"), _reason("LEGACY_TRACE_LIMITED")),
                fidelity=TraceFidelity.L2)
            return Evaluation(self.strategy_id, instrument, decision_time, Decision.NO_CANDIDATE, trace,
                               strategy_version=self.strategy_version, parameter_set_id=self.parameter_set_id,
                               reason_codes=trace.reason_codes, trace_fidelity=TraceFidelity.L2,
                               runtime_version=self.runtime_version, provenance=provenance)
        stages = (
            StageResult("liquidity_sweep", StageStatus.PASS, observed={"level": result.get("sweep_level"), "distance": result.get("sweep_distance")}),
            StageResult("displacement", StageStatus.PASS, observed={"body_atr": result.get("body_atr"), "range": result.get("displacement_range")}),
            StageResult("micro_structure_shift", StageStatus.PASS, observed={"break_distance": result.get("break_distance")}),
        )
        filled = result.get("status") == "FILLED"
        retrace = StageResult("retracement_fill", StageStatus.PASS if filled else StageStatus.FAIL,
                              observed={"status": result.get("status"), "entry_delay_candles": result.get("entry_delay_candles")},
                              reason_code=None if filled else _reason("RETRACEMENT_NOT_FILLED"))
        stages += (retrace,)
        decision = Decision.SIGNAL if filled else Decision.REJECT
        reasons = () if filled else (_reason("RETRACEMENT_NOT_FILLED"),)
        trace = DecisionTrace(stages=stages, decision=decision, reason_codes=reasons, fidelity=TraceFidelity.L2)
        return Evaluation(self.strategy_id, instrument, decision_time, decision, trace,
                           direction=result.get("direction"), strategy_version=self.strategy_version,
                           parameter_set_id=self.parameter_set_id, candidate_id=result.get("setup_id"),
                           reason_codes=reasons, trace_fidelity=TraceFidelity.L2,
                           runtime_version=self.runtime_version, provenance=provenance)


class ContextLegacyRuntime:
    """Translate an existing Context lifecycle record at coarse L1 fidelity."""

    strategy_id = "CONTEXT_STRUCTURE_RETRACE_V1"
    strategy_version = "V1"
    runtime_version = "context-legacy-adapter.v1"

    def evaluate(self, inputs: Mapping[str, Any], *, decision_time: str,
                 as_of: str | None = None) -> Evaluation:
        record = inputs.get("record") or {}
        instrument = str(inputs.get("instrument") or record.get("symbol") or "UNKNOWN")
        status = str(record.get("status") or record.get("retrace_state") or "")
        signal = status in {"FILLED", "SIGNAL", "OPEN"} or bool(record.get("economic_position_id"))
        decision = Decision.SIGNAL if signal else Decision.NO_CANDIDATE
        reasons = () if signal else (_reason("NO_CANDIDATE"), _reason("LEGACY_TRACE_LIMITED"))
        trace = DecisionTrace(
            stages=(StageResult("legacy_lifecycle_outcome", StageStatus.PASS if signal else StageStatus.NOT_EVALUATED,
                                observed={"status": status}, reason_code=None if signal else _reason("LEGACY_TRACE_LIMITED")),),
            decision=decision, reason_codes=reasons, fidelity=TraceFidelity.L1)
        return Evaluation(self.strategy_id, instrument, decision_time, decision, trace,
                           direction=record.get("direction"), strategy_version=self.strategy_version,
                           candidate_id=record.get("setup_id") or record.get("economic_position_id"),
                           reason_codes=reasons, trace_fidelity=TraceFidelity.L1,
                           runtime_version=self.runtime_version,
                           provenance={"adapter": self.runtime_version, "as_of": as_of,
                                       "source_state_reference": inputs.get("source_state_reference"),
                                       "trace_limit": "coarse lifecycle evidence"})

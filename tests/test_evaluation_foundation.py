from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import unittest

from core.strategies.evaluation import (
    Decision, DecisionTrace, Evaluation, StageResult, StageStatus,
    TraceFidelity, canonical_bytes, canonical_hash, default_reason_codes,
)
from core.strategies.legacy import ContextLegacyRuntime, LiquidityLegacyRuntime


class FakeLiquidity:
    class Config:
        symbol = "XAUUSDm"

    config = Config()

    def __init__(self, result):
        self.result = result

    def evaluate(self, *args):
        return self.result


def result(status="FILLED"):
    return {"setup_id": "setup-1", "direction": "LONG", "status": status,
            "sweep_level": 100, "sweep_distance": 1, "body_atr": 1.2,
            "displacement_range": 3, "break_distance": 2,
            "entry_delay_candles": 2}


class EvaluationFoundationTests(unittest.TestCase):
    def test_immutable_models_and_order(self):
        stage = StageResult("first", StageStatus.PASS)
        trace = DecisionTrace((stage, StageResult("second", StageStatus.FAIL)), Decision.REJECT,
                              fidelity=TraceFidelity.L2)
        evaluation = Evaluation("S", "XAUUSDm", "2026-09-21T00:00:00Z", Decision.REJECT,
                                trace, trace_fidelity=TraceFidelity.L2)
        with self.assertRaises(FrozenInstanceError):
            stage.status = StageStatus.FAIL
        with self.assertRaises(FrozenInstanceError):
            evaluation.decision = Decision.SIGNAL
        self.assertEqual([x.stage_id for x in trace.stages], ["first", "second"])

    def test_deterministic_serialization_and_hash(self):
        trace = DecisionTrace((StageResult("gate", StageStatus.PASS, observed={"b": 2, "a": 1}),),
                              Decision.SIGNAL, fidelity=TraceFidelity.L3)
        left = Evaluation("S", "EURUSDm", datetime(2026, 9, 21, tzinfo=timezone.utc),
                          Decision.SIGNAL, trace, trace_fidelity=TraceFidelity.L3)
        right = Evaluation("S", "EURUSDm", "2026-09-21T00:00:00.000000Z", Decision.SIGNAL,
                           trace, trace_fidelity=TraceFidelity.L3)
        self.assertEqual(left.canonical_bytes(), right.canonical_bytes())
        self.assertEqual(left.evaluation_hash, right.evaluation_hash)
        changed = Evaluation("S", "EURUSDm", left.decision_time, Decision.REVIEW_REQUIRED,
                             trace, trace_fidelity=TraceFidelity.L3)
        self.assertNotEqual(left.evaluation_hash, changed.evaluation_hash)
        self.assertEqual(canonical_hash({"a": 1, "b": 2}), canonical_hash({"b": 2, "a": 1}))
        self.assertNotIn("0x", left.canonical_bytes().decode())

    def test_decisions_and_reason_registry(self):
        registry = default_reason_codes()
        self.assertEqual(registry.get("RETRACEMENT_NOT_FILLED").version, "v1")
        self.assertTrue(registry.get("RETRACEMENT_NOT_FILLED").terminal)
        self.assertFalse(registry.get("REVIEW_REQUIRED").terminal)
        with self.assertRaises(KeyError):
            registry.get("NOT_A_REAL_REASON")
        trace = DecisionTrace((), Decision.NO_CANDIDATE,
                              reason_codes=(registry.get("NO_CANDIDATE"),), fidelity=TraceFidelity.L0)
        review = Evaluation("S", "XAUUSDm", "2026-09-21T00:00:00Z", Decision.REVIEW_REQUIRED,
                            trace, trace_fidelity=TraceFidelity.L0)
        self.assertEqual(review.decision, Decision.REVIEW_REQUIRED)

    def test_decision_time_is_explicit(self):
        with self.assertRaises(ValueError):
            Evaluation("S", "XAUUSDm", "", Decision.NO_CANDIDATE,
                        DecisionTrace((), Decision.NO_CANDIDATE))
        with self.assertRaises(ValueError):
            Evaluation("S", "XAUUSDm", datetime(2026, 9, 21), Decision.NO_CANDIDATE,
                        DecisionTrace((), Decision.NO_CANDIDATE))

    def test_liquidity_adapter_emits_signal_and_reject_without_fabrication(self):
        runtime = LiquidityLegacyRuntime(FakeLiquidity(result("FILLED")))
        evaluation = runtime.evaluate({"m15": [], "m5": [], "quote": {}, "contract": {}, "i": 0},
                                      decision_time="2026-09-21T00:00:00Z", as_of="2026-09-21T00:00:00Z")
        self.assertEqual(evaluation.decision, Decision.SIGNAL)
        self.assertEqual(evaluation.trace_fidelity, TraceFidelity.L2)
        self.assertEqual(len(evaluation.trace.stages), 4)
        rejected = LiquidityLegacyRuntime(FakeLiquidity(result("UNFILLED"))).evaluate(
            {"m15": [], "m5": [], "quote": {}, "contract": {}, "i": 0}, decision_time="2026-09-21T00:00:00Z")
        self.assertEqual(rejected.decision, Decision.REJECT)
        self.assertEqual(rejected.reason_codes[0].code, "RETRACEMENT_NOT_FILLED")
        missing = LiquidityLegacyRuntime(FakeLiquidity(None)).evaluate(
            {"m15": [], "m5": [], "quote": {}, "contract": {}, "i": 0}, decision_time="2026-09-21T00:00:00Z")
        self.assertEqual(missing.decision, Decision.NO_CANDIDATE)
        self.assertEqual(missing.trace.stages[0].status, StageStatus.NOT_EVALUATED)

    def test_context_adapter_is_coarse_and_read_only(self):
        evaluation = ContextLegacyRuntime().evaluate(
            {"instrument": "XAUUSDm", "record": {"setup_id": "s", "status": "FILLED"}},
            decision_time="2026-09-21T00:00:00Z")
        self.assertEqual(evaluation.decision, Decision.SIGNAL)
        self.assertEqual(evaluation.trace_fidelity, TraceFidelity.L1)
        self.assertEqual(len(evaluation.trace.stages), 1)
        self.assertEqual(ContextLegacyRuntime().evaluate(
            {"instrument": "XAUUSDm", "record": {}}, decision_time="2026-09-21T00:00:00Z").decision,
                         Decision.NO_CANDIDATE)

    def test_frozen_legacy_hashes_unchanged(self):
        expected = {
            "liquidity_displacement.py": "4f22747b5654e123fd6be49dc820aa58f2bad5c166f42f8c9e445fc6debe88ea",
            "liquidity_displacement_forward.py": "2a188165c457f37c47eacfc4a283fb22f8fa9976ba46d0c1661b4584c211c6c6",
            "liquidity_displacement_entry_forward.py": "f35a2c71a85e9a46f8e66e3acedcf7dfbbf8a5214d0c41f82f5c3c1bd2d90ffc",
            "liquidity_displacement_v1_variant_a.py": "9864e5be59161ce0328998a71083f55b107f56ee9f5b71a6116cfa05c41cb5e1",
            # Re-pinned (was b52893c9...) after "Add canonical Context entry outcome projection":
            # context_structure_retrace_forward.py's OWN runtime self-check
            # (FROZEN_DECISION_CODE_HASH, verified separately at import time) confirms its five
            # frozen decision functions (_geometry/make_setup/_fill/_process_bar/process_symbol)
            # are byte-identical to before - decision_code_hash() == FROZEN_DECISION_CODE_HASH
            # still holds. Only ancillary code changed, matching this file's own documented
            # invariant ("provenance metadata can evolve without changing V1 decisions" - see
            # assert_frozen's docstring). This whole-file hash is intentionally stricter than that
            # guard (it catches any byte change, not just to the five fingerprinted functions);
            # re-pinning it is a deliberate, evidenced decision, not a blind hash refresh.
            # Re-pinned again (was b3ee0c1f...) by the orphaned-OPEN-position lifecycle fix. Unlike
            # the previous re-pin, the decision code DID change on purpose: the decision fingerprint
            # moved 70dba71d... -> 0a990dd5... (prior kept in PRIOR_DECISION_CODE_HASHES) while
            # config_hash (the trading rules) is unchanged. Evidence:
            # tests/test_context_runner_position_lifecycle.py and a 20k-path old-vs-new differential
            # replay in which outcomes differ only where the old code orphaned an OPEN position.
            # Re-pinned (was 5ee2cdbe...) when load_active_membership moved provider symbols from
            # platform.json to platform.instrument_provider_mapping (migration 027). Ancillary only: the
            # decision fingerprint (FROZEN_DECISION_CODE_HASH) is unchanged.
            # Re-pinned (was 34ec0e9c...) when runtime artifact paths became configurable via
            # CONTEXT_RUNNER_STATE_DIR (run from the release image, state on the volume). Module-level
            # paths only; the decision fingerprint is unchanged.
            # Re-pinned (was 05664e33...) when read_symbol gained the MARKET_DATA_SOURCE=REDIS branch
            # (market_data_cache/). Data source only, default BRIDGE unchanged; decision fingerprint
            # unchanged (asserted in tests/test_market_data_cache.py) and cached reads are proven
            # identical to fresh bridge snapshots there.
            # Re-pinned for timestamp-based retrace recovery.  The strategy config hash is
            # unchanged; the decision fingerprint is updated in the runner and manifests.
            "context_structure_retrace_forward.py": "d6940dea4607d2cf811ffb3e4226fc8678b0403b48fd002493bf9351c0872a07",
        }
        for name, digest in expected.items():
            self.assertEqual(hashlib.sha256(Path(name).read_bytes()).hexdigest(), digest, name)


if __name__ == "__main__":
    unittest.main()

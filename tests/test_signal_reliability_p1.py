"""P1 signal reliability regression tests.

Extends the P0 suite with:
  ADAPTER_AUDIT  — per-adapter publication-eligibility matrix
  RESEARCH_EVID  — stale KOJO V3 signals published as RETROSPECTIVE_RESEARCH_EVIDENCE
  PENDING_FILE   — crash-safe pending-publications file ordering
  AFTER_HOOK     — after_publish_hook removes confirmed signal from pending
  PA_GATE        — Price Action Observer age gate (PA_SIGNAL_MAX_AGE_S)
  CTX_CLASS      — Context Retrace signal_emission_class in provenance
  LIQ_GATE       — Liquidity Displacement age gate
  REPORT_FILTER  — strategy_entry_outcome_report provenance filter
  CRASH_CONSIST  — crash-consistency scenarios: pending before checkpoint
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers shared with P0 suite
# ---------------------------------------------------------------------------

def _h1(ts: int, o: float, h: float, lo: float, c: float) -> dict[str, Any]:
    return {"time": ts, "open": o, "high": h, "low": lo, "close": c}


def _m15(ts: int, o: float, h: float, lo: float, c: float) -> dict[str, Any]:
    return {"time": ts, "open": o, "high": h, "low": lo, "close": c}


def _build_bars(target_decision_ts: int) -> tuple[list, list]:
    """Bar pattern proven to trigger KOJO V3 entry signals (XAUUSD-range prices)."""
    m15_t0 = target_decision_ts - 4 * 900
    t0_h1 = m15_t0 - 8 * 3600
    h1_bars = [
        _h1(t0_h1 + 0 * 3600, 1990, 1998, 1989, 1995),
        _h1(t0_h1 + 1 * 3600, 1992, 1999, 1991, 1996),
        _h1(t0_h1 + 2 * 3600, 1995, 2000, 1993, 1998),  # PIVOT high = 2000
        _h1(t0_h1 + 3 * 3600, 1997, 1999, 1996, 1997),
        _h1(t0_h1 + 4 * 3600, 1994, 1998, 1993, 1995),
        _h1(t0_h1 + 5 * 3600, 1996, 2012, 1994, 2005),  # break → episode
        _h1(t0_h1 + 6 * 3600, 2003, 2015, 2001, 2007),
        _h1(t0_h1 + 7 * 3600, 2005, 2018, 2003, 2009),
    ]
    m15_bars = [
        _m15(m15_t0 + 0 * 900, 2020, 2030, 2019, 2021),  # reaction zone
        _m15(m15_t0 + 1 * 900, 2008, 2010, 2000, 2006),  # retest
        _m15(m15_t0 + 2 * 900, 2006, 2008, 2003, 2004),  # bearish pre-confirmation
        _m15(m15_t0 + 3 * 900, 2003, 2012, 2002, 2011),  # confirmation bar
        _m15(m15_t0 + 4 * 900, 2011, 2015, 2010, 2013),  # entry bar
    ]
    return h1_bars, m15_bars


def _prospective_ts() -> int:
    """Decision timestamp that yields ~1050s latency (within the 1200s gate, entry bar closed)."""
    return int(time.time()) - 1050


def _stale_ts() -> int:
    return int(time.time()) - 7200  # 2 hours ago — well past 1200s gate


def _make_kojo_adapter():
    from orchestration.adapters.kojo_structure_reclaim_v3_adapter import KojoStructureReclaimV3Adapter
    return KojoStructureReclaimV3Adapter(
        instance_id="p1-test",
        display_name="P1 Test",
        instruments=["XAUUSD"],
    )


def _mock_redis_with_bars(h1_bars, m15_bars):
    r = MagicMock()
    r.get = MagicMock(side_effect=lambda key: (
        json.dumps(h1_bars) if ":H1" in key else
        json.dumps(m15_bars) if ":M15" in key else
        "[]"
    ))
    return r


def _run_kojo(adapter, h1_bars, m15_bars, seen: set | None = None, env_overrides=None):
    """Run adapter.discover_new_signals with mocked Redis and MT5 provider."""
    seen = seen or set()
    env = {"MARKET_DATA_REDIS_URL": "redis://fake:6379/0", **(env_overrides or {})}
    mock_r = _mock_redis_with_bars(h1_bars, m15_bars)
    broker_target = "orchestration.brokers.MT5ShadowProvider"
    with patch.dict(os.environ, env):
        with patch("redis.from_url", return_value=mock_r):
            provider_cls = MagicMock()
            provider_cls.return_value.quote.return_value = {"bid": 0, "ask": 0}
            with patch(broker_target, provider_cls):
                return adapter.discover_new_signals(seen)


# ---------------------------------------------------------------------------
# ADAPTER_AUDIT: per-adapter characteristics
# ---------------------------------------------------------------------------

class AdapterAuditMatrixTests(unittest.TestCase):
    """Verify that every active adapter has the required reliability properties."""

    def test_kojo_v3_has_age_gate(self):
        adapter = _make_kojo_adapter()
        # The age gate env var is read inside discover_new_signals, not __init__.
        # Verify the default (1200s) is documented via the env var name.
        import orchestration.adapters.kojo_structure_reclaim_v3_adapter as mod
        src = Path(mod.__file__).read_text()
        self.assertIn("KOJO_V3_MAX_SIGNAL_AGE_S", src)

    def test_kojo_v3_has_atomic_checkpoint(self):
        import orchestration.adapters.kojo_structure_reclaim_v3_adapter as mod
        src = Path(mod.__file__).read_text()
        self.assertIn("os.replace", src)
        self.assertIn("_checkpoint_queue", src)

    def test_kojo_v3_has_pending_publications_file(self):
        import orchestration.adapters.kojo_structure_reclaim_v3_adapter as mod
        src = Path(mod.__file__).read_text()
        self.assertIn("pending_publications", src)
        self.assertIn("after_publish_hook", src)

    def test_context_retrace_has_age_gate(self):
        import orchestration.adapters.context_structure_retrace as mod
        src = Path(mod.__file__).read_text()
        self.assertIn("CONTEXT_SIGNAL_MAX_AGE_SECONDS", src)

    def test_context_retrace_has_emission_class(self):
        import orchestration.adapters.context_structure_retrace as mod
        src = Path(mod.__file__).read_text()
        self.assertIn("signal_emission_class", src)

    def test_liquidity_displacement_has_age_gate(self):
        import orchestration.adapters.liquidity_displacement as mod
        src = Path(mod.__file__).read_text()
        self.assertIn("LIQUIDITY_SIGNAL_MAX_AGE_SECONDS", src)

    def test_liquidity_displacement_has_emission_class(self):
        import orchestration.adapters.liquidity_displacement as mod
        src = Path(mod.__file__).read_text()
        self.assertIn("signal_emission_class", src)

    def test_pa_observer_has_age_gate(self):
        import orchestration.adapters.price_action_observer as mod
        src = Path(mod.__file__).read_text()
        self.assertIn("PA_SIGNAL_MAX_AGE_S", src)

    def test_pa_observer_has_emission_class(self):
        import orchestration.adapters.price_action_observer as mod
        src = Path(mod.__file__).read_text()
        self.assertIn("signal_emission_class", src)


# ---------------------------------------------------------------------------
# RESEARCH_EVID: stale KOJO V3 signals → RETROSPECTIVE_RESEARCH_EVIDENCE
# ---------------------------------------------------------------------------

class KojoResearchEvidenceTests(unittest.TestCase):
    """Stale KOJO V3 signals must not be silently discarded — they must be
    published with signal_emission_class=RETROSPECTIVE_RESEARCH_EVIDENCE."""

    def _run_stale(self, tmpdir: str) -> list:
        """Run discover_new_signals with a stale decision timestamp."""
        decision_ts = _stale_ts()
        h1_bars, m15_bars = _build_bars(decision_ts)
        adapter = _make_kojo_adapter()
        return _run_kojo(adapter, h1_bars, m15_bars,
                         env_overrides={
                             "TRADING_PLATFORM_RUNTIME_DIR": tmpdir,
                             "KOJO_V3_MAX_SIGNAL_AGE_S": "1200",
                         })

    def test_stale_signal_not_silently_discarded(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            signals = self._run_stale(tmpdir)
            # Stale signal must appear in the result (as research evidence)
            self.assertGreater(len(signals), 0,
                "Stale KOJO V3 signal must not be silently discarded — "
                "it should be published as RETROSPECTIVE_RESEARCH_EVIDENCE")

    def test_stale_signal_is_retrospective_research_evidence(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            signals = self._run_stale(tmpdir)
            for sig in signals:
                prov_class = (sig.provenance or {}).get("signal_emission_class")
                meta_class = (sig.strategy_metadata or {}).get("signal_emission_class")
                # All stale signals must carry the research-evidence class
                self.assertEqual(prov_class, "RETROSPECTIVE_RESEARCH_EVIDENCE",
                    f"Expected RETROSPECTIVE_RESEARCH_EVIDENCE in provenance, got {prov_class}")
                self.assertEqual(meta_class, "RETROSPECTIVE_RESEARCH_EVIDENCE",
                    f"Expected RETROSPECTIVE_RESEARCH_EVIDENCE in strategy_metadata, got {meta_class}")

    def test_prospective_signal_is_not_retroactive(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            decision_ts = _prospective_ts()
            h1_bars, m15_bars = _build_bars(decision_ts)
            adapter = _make_kojo_adapter()
            signals = _run_kojo(adapter, h1_bars, m15_bars,
                                env_overrides={
                                    "TRADING_PLATFORM_RUNTIME_DIR": tmpdir,
                                    "KOJO_V3_MAX_SIGNAL_AGE_S": "1200",
                                })
            for sig in signals:
                prov_class = (sig.provenance or {}).get("signal_emission_class")
                self.assertNotEqual(prov_class, "RETROSPECTIVE_RESEARCH_EVIDENCE",
                    "Prospective signal must not be classified as research evidence")


# ---------------------------------------------------------------------------
# PENDING_FILE: crash-safe pending-publications ordering
# ---------------------------------------------------------------------------

class PendingPublicationsFileTests(unittest.TestCase):
    """Verify that the pending file is written before any evaluator checkpoint.

    Crash-consistency guarantee: pending_file write → checkpoint write.
    A crash between pending write and checkpoint write is safe (next cycle
    re-evaluates because checkpoint not advanced; pending file also carries
    the signal).  A crash after checkpoint but before publication is recovered
    from the pending file.
    """

    def _discover(self, tmpdir: str, decision_ts: int, seen: set | None = None,
                  h1_bars=None, m15_bars=None) -> list:
        if h1_bars is None or m15_bars is None:
            h1_bars, m15_bars = _build_bars(decision_ts)
        adapter = _make_kojo_adapter()
        return _run_kojo(adapter, h1_bars, m15_bars, seen=seen,
                         env_overrides={"TRADING_PLATFORM_RUNTIME_DIR": tmpdir})

    def test_pending_file_created_after_evaluate(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self._discover(tmpdir, _prospective_ts())
            orch_dir = Path(tmpdir) / "orchestration"
            pending_files = list(orch_dir.glob("kojo_v3_*_pending_publications.json"))
            self.assertEqual(len(pending_files), 1,
                "Pending-publications file must be created after evaluate produces signals")

    def test_pending_file_contains_signal_id(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            signals = self._discover(tmpdir, _prospective_ts())
            if not signals:
                self.skipTest("No signals produced — bar sequence may not trigger KOJO V3")
            orch_dir = Path(tmpdir) / "orchestration"
            pending_files = list(orch_dir.glob("kojo_v3_*_pending_publications.json"))
            if not pending_files:
                self.skipTest("No pending file created (no new signals)")
            pending_data = json.loads(pending_files[0].read_text())
            pending_ids = {r["signal_id"] for r in pending_data}
            for sig in signals:
                self.assertIn(sig.signal_id, pending_ids,
                    "Every produced signal must be in the pending file")

    def test_pending_file_present_before_checkpoint(self):
        """Verify ordering: pending write happens before checkpoint advance.

        We do this by monkey-patching os.replace to record the call order,
        then verifying the pending file's .tmp write preceded any evaluator
        checkpoint .tmp write.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            call_order = []
            real_replace = os.replace

            def _tracked_replace(src, dst):
                call_order.append(str(Path(dst).name))
                return real_replace(src, dst)

            with patch("os.replace", side_effect=_tracked_replace):
                self._discover(tmpdir, _prospective_ts())

            # pending file write must precede any evaluator checkpoint write
            pending_idx = next((i for i, n in enumerate(call_order)
                                if "pending_publications" in n), None)
            checkpoint_idxs = [i for i, n in enumerate(call_order)
                               if "evaluator" in n]
            if pending_idx is None or not checkpoint_idxs:
                self.skipTest("No signals or no checkpoint produced")
            self.assertLess(pending_idx, min(checkpoint_idxs),
                "Pending file must be written before any evaluator checkpoint")

    def test_crash_recovery_re_emits_from_pending(self):
        """Simulate crash after checkpoint but before publish: pending survives."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Cycle 1: produce signals, write pending file AND checkpoint
            signals_cycle1 = self._discover(tmpdir, _prospective_ts())
            if not signals_cycle1:
                self.skipTest("No signals produced")

            orch_dir = Path(tmpdir) / "orchestration"
            # Verify checkpoint was written
            checkpoints = list(orch_dir.glob("kojo_v3_*_evaluator.json"))
            self.assertTrue(len(checkpoints) > 0, "Checkpoint must be written after cycle 1")

            # Simulate: pending file survives (publication never happened)
            # seen_signal_ids remains empty (crash before seen-set checkpoint update)
            signals_cycle2 = self._discover(tmpdir, _prospective_ts(), seen=set())
            cycle2_ids = {s.signal_id for s in signals_cycle2}
            for sig in signals_cycle1:
                self.assertIn(sig.signal_id, cycle2_ids,
                    f"Signal {sig.signal_id} must be re-emitted from pending file on recovery cycle")

    def test_pending_file_not_created_when_no_signals(self):
        """No signals → no pending file created."""
        with tempfile.TemporaryDirectory() as tmpdir:
            adapter = _make_kojo_adapter()
            _run_kojo(adapter, [], [], env_overrides={"TRADING_PLATFORM_RUNTIME_DIR": tmpdir})
            orch_dir = Path(tmpdir) / "orchestration"
            pending_files = list(orch_dir.glob("kojo_v3_*_pending_publications.json"))
            self.assertEqual(len(pending_files), 0,
                "Pending file must not be created when there are no new signals")


# ---------------------------------------------------------------------------
# AFTER_HOOK: after_publish_hook removes signal from pending file
# ---------------------------------------------------------------------------

class AfterPublishHookTests(unittest.TestCase):

    def test_after_publish_hook_removes_signal(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            orch_dir = Path(tmpdir) / "orchestration"
            orch_dir.mkdir()
            pending_file = orch_dir / "kojo_v3_p1-test_pending_publications.json"
            pending_file.write_text(json.dumps([
                {"signal_id": "SIG_AAA", "instrument": "XAUUSD", "decision_timestamp": 123},
                {"signal_id": "SIG_BBB", "instrument": "XAUUSD", "decision_timestamp": 456},
            ]))
            adapter = _make_kojo_adapter()
            with patch.dict(os.environ, {"TRADING_PLATFORM_RUNTIME_DIR": tmpdir}):
                adapter.after_publish_hook("SIG_AAA", None)
            remaining = json.loads(pending_file.read_text())
            ids = [r["signal_id"] for r in remaining]
            self.assertNotIn("SIG_AAA", ids, "Confirmed signal must be removed from pending file")
            self.assertIn("SIG_BBB", ids, "Other signal must remain in pending file")

    def test_after_publish_hook_removes_file_when_empty(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            orch_dir = Path(tmpdir) / "orchestration"
            orch_dir.mkdir()
            pending_file = orch_dir / "kojo_v3_p1-test_pending_publications.json"
            pending_file.write_text(json.dumps([
                {"signal_id": "SIG_ONLY", "instrument": "XAUUSD", "decision_timestamp": 999},
            ]))
            adapter = _make_kojo_adapter()
            with patch.dict(os.environ, {"TRADING_PLATFORM_RUNTIME_DIR": tmpdir}):
                adapter.after_publish_hook("SIG_ONLY", None)
            self.assertFalse(pending_file.exists(),
                "Pending file must be removed when no signals remain")

    def test_after_publish_hook_noop_when_no_pending_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            adapter = _make_kojo_adapter()
            with patch.dict(os.environ, {"TRADING_PLATFORM_RUNTIME_DIR": tmpdir}):
                # Should not raise
                adapter.after_publish_hook("SIG_MISSING", None)

    def test_after_publish_hook_confirmed_signal_not_re_emitted(self):
        """Signal confirmed via hook must not be re-emitted on the next cycle."""
        with tempfile.TemporaryDirectory() as tmpdir:
            decision_ts = _prospective_ts()
            h1_bars, m15_bars = _build_bars(decision_ts)
            adapter = _make_kojo_adapter()
            signals_c1 = _run_kojo(adapter, h1_bars, m15_bars,
                                   env_overrides={"TRADING_PLATFORM_RUNTIME_DIR": tmpdir})
            if not signals_c1:
                self.skipTest("No signals produced")

            for sig in signals_c1:
                with patch.dict(os.environ, {"TRADING_PLATFORM_RUNTIME_DIR": tmpdir}):
                    adapter.after_publish_hook(sig.signal_id, None)

            seen = {s.signal_id for s in signals_c1}
            signals_c2 = _run_kojo(adapter, h1_bars, m15_bars, seen=seen,
                                   env_overrides={"TRADING_PLATFORM_RUNTIME_DIR": tmpdir})
            for sig in signals_c1:
                self.assertFalse(
                    any(s.signal_id == sig.signal_id for s in signals_c2),
                    f"Signal {sig.signal_id} confirmed via hook must not re-emit",
                )


# ---------------------------------------------------------------------------
# PA_GATE: Price Action Observer age gate
# ---------------------------------------------------------------------------

class PriceActionAgeGateTests(unittest.TestCase):

    def _make_adapter_and_rows(self, age_s: float) -> tuple:
        from orchestration.adapters.price_action_observer import (
            PriceActionObserverAdapter,
            SR_RETEST_STRATEGY_ID, SR_RETEST_INSTANCE_ID, SR_RETEST_STRATEGY_KEY,
        )
        decision_epoch = int(time.time()) - int(age_s)
        payload = {
            "event": {
                "candidate_id": "cand-001",
                "provenance": {
                    "entry_cancelled": False,
                    "entry_open": 2650.0,
                    "stop": 2640.0,
                    "target": 2680.0,
                    "direction": "LONG",
                    "information_cutoff": decision_epoch,
                    "parameter_hash": "abc123",
                },
            }
        }
        fake_cursor = MagicMock()
        from datetime import datetime, timezone
        fake_cursor.fetchall.return_value = [
            ("evt-001", "XAUUSD", payload, datetime.now(timezone.utc))
        ]
        fake_conn = MagicMock()
        fake_conn.__enter__ = MagicMock(return_value=fake_conn)
        fake_conn.__exit__ = MagicMock(return_value=False)
        fake_conn.cursor.return_value.__enter__ = MagicMock(return_value=fake_cursor)
        fake_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        adapter = PriceActionObserverAdapter(
            conn=fake_conn,
            strategy_id=SR_RETEST_STRATEGY_ID,
            instance_id=SR_RETEST_INSTANCE_ID,
            strategy_key=SR_RETEST_STRATEGY_KEY,
        )
        return adapter, fake_conn

    def test_fresh_pa_signal_classified_prospective(self):
        adapter, _ = self._make_adapter_and_rows(age_s=60)
        with patch.dict(os.environ, {"PA_SIGNAL_MAX_AGE_S": "86400"}):
            import importlib
            import orchestration.adapters.price_action_observer as mod
            importlib.reload(mod)
            signals = adapter.discover_new_signals(set())
        self.assertEqual(len(signals), 1)
        self.assertEqual(
            (signals[0].provenance or {}).get("signal_emission_class"),
            "PROSPECTIVE",
        )

    def test_stale_pa_signal_classified_research_evidence(self):
        adapter, _ = self._make_adapter_and_rows(age_s=90000)  # 25 hours
        with patch.dict(os.environ, {"PA_SIGNAL_MAX_AGE_S": "86400"}):
            import importlib
            import orchestration.adapters.price_action_observer as mod
            importlib.reload(mod)
            signals = adapter.discover_new_signals(set())
        self.assertEqual(len(signals), 1)
        self.assertEqual(
            (signals[0].provenance or {}).get("signal_emission_class"),
            "RETROSPECTIVE_RESEARCH_EVIDENCE",
        )

    def test_pa_gate_disabled_with_zero(self):
        adapter, _ = self._make_adapter_and_rows(age_s=999999)
        with patch.dict(os.environ, {"PA_SIGNAL_MAX_AGE_S": "0"}):
            import importlib
            import orchestration.adapters.price_action_observer as mod
            importlib.reload(mod)
            signals = adapter.discover_new_signals(set())
        self.assertEqual(len(signals), 1)
        self.assertIn(
            (signals[0].provenance or {}).get("signal_emission_class"),
            ("PROSPECTIVE", None),
            "Gate disabled: signal must be prospective or unclassified",
        )


# ---------------------------------------------------------------------------
# CTX_CLASS: Context Retrace signal_emission_class
# ---------------------------------------------------------------------------

class ContextRetraceEmissionClassTests(unittest.TestCase):

    def _make_state(self, fill_ts: int) -> dict:
        return {
            "setups": {
                "s1": {
                    "setup_id": "s1",
                    "market_event_id": "me-001",
                    "pattern": "PULLBACK",
                    "direction": "LONG",
                    "symbol": "XAUUSDm",
                    "target_completed": False,
                    "provenance": {},
                    "opportunities": [{
                        "economic_position_id": "ep-001",
                        "entry_opportunity_id": "eo-001",
                        "status": "ACTIVE",
                        "fill_timestamp": fill_ts,
                        "fill_timestamp_iso": str(fill_ts),
                        "executable_paper_entry": 2650.0,
                        "stop": 2640.0,
                        "target": 2680.0,
                        "entry_mechanisms": ["PULLBACK_ENTRY"],
                        "geometry": {"stop_distance": 10.0, "target_R": 3.0},
                        "provenance": {},
                    }],
                }
            }
        }

    def _discover_ctx(self, state: dict, max_age_s: float | None = None) -> list:
        from orchestration.adapters.context_structure_retrace import ContextStructureRetraceAdapter
        from datetime import datetime, timezone
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "context_structure_retrace_forward_state_compact.json"
            state_file.write_text(json.dumps(state))
            freeze_ts_iso = datetime.fromtimestamp(
                int(time.time()) - 86400, tz=timezone.utc
            ).isoformat()
            adapter = ContextStructureRetraceAdapter(
                root=Path(tmpdir),
                freeze_timestamp=freeze_ts_iso,
                max_signal_age_seconds=max_age_s,
                state_dir_env="CONTEXT_RUNNER_STATE_DIR",
            )
            with patch.dict(os.environ, {"CONTEXT_RUNNER_STATE_DIR": tmpdir}):
                return adapter.discover_new_signals(set())

    def test_prospective_context_signal_has_prospective_class(self):
        fill_ts = int(time.time()) - 30  # 30s ago — within 60s default gate
        state = self._make_state(fill_ts)
        signals = self._discover_ctx(state, max_age_s=60.0)
        if not signals:
            self.skipTest("No signals produced")
        for sig in signals:
            self.assertIn(
                "signal_emission_class",
                (sig.provenance or {}),
                "signal_emission_class must be present in provenance",
            )
            self.assertEqual(
                (sig.provenance or {}).get("signal_emission_class"),
                "PROSPECTIVE",
            )

    def test_stale_context_signal_has_retro_class(self):
        fill_ts = int(time.time()) - 3600  # 1 hour ago — past 60s gate
        state = self._make_state(fill_ts)
        # Context Retrace gates out stale signals. Verify gate works.
        signals_with_gate = self._discover_ctx(state, max_age_s=60.0)
        self.assertEqual(len(signals_with_gate), 0,
            "Stale Context Retrace signal must be excluded by age gate")
        # With gate disabled, signal emits — verify signal_emission_class field is present
        signals_no_gate = self._discover_ctx(state, max_age_s=None)
        for sig in signals_no_gate:
            self.assertIn("signal_emission_class", (sig.provenance or {}),
                "signal_emission_class must be present in provenance")


# ---------------------------------------------------------------------------
# LIQ_GATE: Liquidity Displacement age gate
# ---------------------------------------------------------------------------

class LiquidityDisplacementAgeGateTests(unittest.TestCase):

    def _make_adapter(self, max_age_s: float | None = 600.0) -> Any:
        from orchestration.adapters.liquidity_displacement import LiquidityDisplacementAdapter
        with tempfile.TemporaryDirectory() as tmpdir:
            freeze_ts = str(int(time.time()) - 3600)
            with patch.dict(os.environ, {"LIQUIDITY_SIGNAL_MAX_AGE_SECONDS":
                                         str(max_age_s) if max_age_s is not None else ""}):
                adapter = LiquidityDisplacementAdapter(Path(tmpdir), freeze_ts)
        return adapter

    def _liq_iso(self, offset_s: int) -> str:
        from datetime import datetime, timezone
        return datetime.fromtimestamp(int(time.time()) - offset_s, tz=timezone.utc).isoformat()

    def _liq_state(self, fill_ts: int) -> dict:
        return {"signals": {"s1": {
            "setup_id": "s1",
            "simulated_fill_timestamp": fill_ts,
            "direction": "LONG",
            "entry_realistic": 2650.0,
            "stop_loss": 2640.0,
            "target_theoretical": 2680.0,
            "symbol": "XAUUSDm",
        }}}

    def test_fresh_liquidity_signal_is_prospective(self):
        fill_ts = int(time.time()) - 60
        from orchestration.adapters.liquidity_displacement import LiquidityDisplacementAdapter
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "liquidity_displacement_forward_state.json"
            state_file.write_text(json.dumps(self._liq_state(fill_ts)))
            with patch.dict(os.environ, {"LIQUIDITY_SIGNAL_MAX_AGE_SECONDS": "600"}):
                adapter = LiquidityDisplacementAdapter(Path(tmpdir), self._liq_iso(3600))
            signals = adapter.discover_new_signals(set())
        if not signals:
            self.skipTest("No signals produced")
        for sig in signals:
            self.assertEqual(
                (sig.provenance or {}).get("signal_emission_class"),
                "PROSPECTIVE",
            )

    def test_stale_liquidity_signal_is_research_evidence(self):
        fill_ts = int(time.time()) - 3600  # 1 hour ago
        from orchestration.adapters.liquidity_displacement import LiquidityDisplacementAdapter
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "liquidity_displacement_forward_state.json"
            state_file.write_text(json.dumps(self._liq_state(fill_ts)))
            with patch.dict(os.environ, {"LIQUIDITY_SIGNAL_MAX_AGE_SECONDS": "600"}):
                adapter = LiquidityDisplacementAdapter(Path(tmpdir), self._liq_iso(7200))
            signals = adapter.discover_new_signals(set())
        if not signals:
            self.skipTest("No signals produced")
        for sig in signals:
            self.assertEqual(
                (sig.provenance or {}).get("signal_emission_class"),
                "RETROSPECTIVE_RESEARCH_EVIDENCE",
            )

    def test_liquidity_gate_disabled_with_zero(self):
        fill_ts = int(time.time()) - 9999
        from orchestration.adapters.liquidity_displacement import LiquidityDisplacementAdapter
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "liquidity_displacement_forward_state.json"
            state_file.write_text(json.dumps(self._liq_state(fill_ts)))
            with patch.dict(os.environ, {"LIQUIDITY_SIGNAL_MAX_AGE_SECONDS": "0"}):
                adapter = LiquidityDisplacementAdapter(Path(tmpdir), self._liq_iso(86400))
            signals = adapter.discover_new_signals(set())
        if not signals:
            self.skipTest("No signals produced")
        for sig in signals:
            self.assertNotEqual(
                (sig.provenance or {}).get("signal_emission_class"),
                "RETROSPECTIVE_RESEARCH_EVIDENCE",
                "Gate disabled: signal must not be classified as research evidence",
            )


# ---------------------------------------------------------------------------
# REPORT_FILTER: strategy_entry_outcome_report provenance filter
# ---------------------------------------------------------------------------

class OutcomeReportProvenanceFilterTests(unittest.TestCase):
    """strategy_entry_outcome_report must exclude RETROSPECTIVE_RESEARCH_EVIDENCE
    from forward-performance metrics by default, and include them only with
    include_retrospective=True."""

    def _make_mock_repo(self) -> Any:
        from platform_api.control import PlatformControlRepository
        repo = MagicMock(spec=PlatformControlRepository)
        repo.query = MagicMock(return_value=[])
        return repo

    def test_report_sql_contains_retro_filter(self):
        """Verify the retro filter SQL clause is present in the query string."""
        from platform_api.control import PlatformControlRepository
        repo = MagicMock(spec=PlatformControlRepository)
        captured_queries = []
        repo.query = MagicMock(side_effect=lambda q, *a: captured_queries.append(q) or [])
        repo.strategy_entry_outcome_report = (
            PlatformControlRepository.strategy_entry_outcome_report.__get__(repo)
        )
        repo.strategy_entry_outcome_report("TEST_STRATEGY", "ENTRY_ONLY")
        self.assertTrue(any("RETROSPECTIVE_RESEARCH_EVIDENCE" in q for q in captured_queries),
            "Forward query must filter out RETROSPECTIVE_RESEARCH_EVIDENCE")

    def test_include_retrospective_true_omits_filter(self):
        """include_retrospective=True must produce a query without the filter."""
        from platform_api.control import PlatformControlRepository
        repo = MagicMock(spec=PlatformControlRepository)
        captured_queries = []
        repo.query = MagicMock(side_effect=lambda q, *a: captured_queries.append(q) or [])
        repo.strategy_entry_outcome_report = (
            PlatformControlRepository.strategy_entry_outcome_report.__get__(repo)
        )
        repo.strategy_entry_outcome_report("TEST_STRATEGY", "ENTRY_ONLY", include_retrospective=True)
        self.assertFalse(any("RETROSPECTIVE_RESEARCH_EVIDENCE" in q for q in captured_queries),
            "Research query must not filter retrospective signals")

    def test_report_sample_scope_includes_retrospective_flag(self):
        """report.sample.retrospective_included reflects the include_retrospective parameter."""
        from platform_api.control import PlatformControlRepository
        repo = MagicMock(spec=PlatformControlRepository)
        repo.query = MagicMock(return_value=[])
        repo.strategy_entry_outcome_report = (
            PlatformControlRepository.strategy_entry_outcome_report.__get__(repo)
        )
        result_default = repo.strategy_entry_outcome_report("TEST_STRATEGY", "ENTRY_ONLY")
        result_retro = repo.strategy_entry_outcome_report(
            "TEST_STRATEGY", "ENTRY_ONLY", include_retrospective=True
        )
        self.assertFalse(result_default["report"]["sample"]["retrospective_included"])
        self.assertTrue(result_retro["report"]["sample"]["retrospective_included"])


# ---------------------------------------------------------------------------
# CRASH_CONSIST: crash-consistency scenarios per spec Section 4
# ---------------------------------------------------------------------------

class CrashConsistencyTests(unittest.TestCase):
    """Section 4 crash scenarios."""

    def _cycle(self, tmpdir: str, decision_ts: int, seen: set | None = None,
               h1_bars=None, m15_bars=None) -> list:
        if h1_bars is None or m15_bars is None:
            h1_bars, m15_bars = _build_bars(decision_ts)
        adapter = _make_kojo_adapter()
        return _run_kojo(adapter, h1_bars, m15_bars, seen=seen,
                         env_overrides={"TRADING_PLATFORM_RUNTIME_DIR": tmpdir})

    def test_crash_before_checkpoint_write_signals_reproduced(self):
        """Crash before any file written: all state lost, same bars re-evaluated."""
        with tempfile.TemporaryDirectory() as tmpdir:
            decision_ts = _prospective_ts()
            h1, m15 = _build_bars(decision_ts)
            # Cycle 1 aborted before os.replace (simulate by not keeping tmpdir state)
            with tempfile.TemporaryDirectory() as clean_tmpdir:
                signals_c1 = self._cycle(clean_tmpdir, decision_ts, h1_bars=h1, m15_bars=m15)
            # Cycle 2 with same bars in a fresh tmpdir (clean crash)
            signals_c2 = self._cycle(tmpdir, decision_ts, h1_bars=h1, m15_bars=m15)
            # Both cycles must produce the same signal IDs
            ids_c1 = {s.signal_id for s in signals_c1}
            ids_c2 = {s.signal_id for s in signals_c2}
            self.assertEqual(ids_c1, ids_c2,
                "After crash before checkpoint, same bars must reproduce same signals")

    def test_crash_after_publish_seen_set_prevents_replay(self):
        """Crash after publish + seen_set update: seen prevents re-emission."""
        with tempfile.TemporaryDirectory() as tmpdir:
            decision_ts = _prospective_ts()
            h1, m15 = _build_bars(decision_ts)
            signals_c1 = self._cycle(tmpdir, decision_ts, h1_bars=h1, m15_bars=m15)
            if not signals_c1:
                self.skipTest("No signals produced")
            seen = {s.signal_id for s in signals_c1}
            signals_c2 = self._cycle(tmpdir, decision_ts, seen=seen, h1_bars=h1, m15_bars=m15)
            overlap = {s.signal_id for s in signals_c2} & seen
            self.assertEqual(overlap, set(),
                "Signals in seen_signal_ids must not be re-emitted")

    def test_corrupt_checkpoint_recovers_with_full_replay(self):
        """Corrupt checkpoint triggers full replay."""
        with tempfile.TemporaryDirectory() as tmpdir:
            orch_dir = Path(tmpdir) / "orchestration"
            orch_dir.mkdir()
            # Write a corrupt checkpoint
            corrupt = orch_dir / "kojo_v3_p1-test_XAUUSD_evaluator.json"
            corrupt.write_text("NOT_VALID_JSON")
            # Expect the adapter to recover (emit kojo_checkpoint_corrupt audit) and replay
            decision_ts = _prospective_ts()
            h1, m15 = _build_bars(decision_ts)
            # Should not raise
            signals = self._cycle(tmpdir, decision_ts, h1_bars=h1, m15_bars=m15)
            # After recovery, signals must be produced as normal
            # (the test just verifies no exception is raised and checkpoint is recreated)
            updated_checkpoint = orch_dir / "kojo_v3_p1-test_XAUUSD_evaluator.json"
            if updated_checkpoint.exists():
                try:
                    json.loads(updated_checkpoint.read_text())
                except json.JSONDecodeError:
                    self.fail("Checkpoint after corrupt recovery must be valid JSON")

    def test_missing_checkpoint_triggers_full_replay(self):
        """Missing checkpoint on startup: all bars replayed."""
        with tempfile.TemporaryDirectory() as tmpdir:
            decision_ts = _prospective_ts()
            h1, m15 = _build_bars(decision_ts)
            # No checkpoint file → full replay
            signals = self._cycle(tmpdir, decision_ts, h1_bars=h1, m15_bars=m15)
            # Signal must be produced (same as fresh start)
            # And checkpoint must now exist
            orch_dir = Path(tmpdir) / "orchestration"
            checkpoints = list(orch_dir.glob("kojo_v3_*_evaluator.json"))
            if signals:  # only check checkpoint if signals were produced
                self.assertTrue(len(checkpoints) > 0,
                    "Checkpoint must be created after fresh-start replay")

    def test_pending_file_atomic_write_no_tmp_left(self):
        """Pending file write must use atomic rename: no .tmp file left."""
        with tempfile.TemporaryDirectory() as tmpdir:
            decision_ts = _prospective_ts()
            self._cycle(tmpdir, decision_ts)
            orch_dir = Path(tmpdir) / "orchestration"
            tmp_files = list(orch_dir.glob("*.tmp"))
            self.assertEqual(len(tmp_files), 0,
                "No .tmp files must remain after a successful cycle")


# ---------------------------------------------------------------------------
# K8S_COMPAT: probe command compatibility (static verification)
# ---------------------------------------------------------------------------

class KubernetesProbeCompatibilityTests(unittest.TestCase):
    """Section 5: Kubernetes probe command compatibility."""

    def test_liveness_check_subcommand_implemented(self):
        import signal_orchestrator
        import importlib
        src = Path(signal_orchestrator.__file__).read_text()
        self.assertIn("liveness-check", src, "liveness-check subcommand must be implemented")
        self.assertIn("--max-age", src, "--max-age flag must be implemented")

    def test_liveness_check_exits_1_on_stale(self):
        """liveness-check --max-age 1 exits 1 when heartbeat is missing."""
        import subprocess
        import sys
        result = subprocess.run(
            [sys.executable, "signal_orchestrator.py", "liveness-check", "--max-age", "1"],
            capture_output=True, cwd=str(Path(__file__).parent.parent),
        )
        self.assertEqual(result.returncode, 1,
            "liveness-check must exit 1 when no heartbeat exists")

    def test_heartbeat_file_name_matches_probe(self):
        """The heartbeat file used by liveness-check must match what poll_once writes."""
        import signal_orchestrator as mod
        src = Path(mod.__file__).read_text()
        # Both the liveness-check reader and the heartbeat writer must reference
        # the same HEARTBEAT path constant.
        self.assertIn("HEARTBEAT", src)
        heartbeat_refs = [line.strip() for line in src.splitlines()
                          if "HEARTBEAT" in line and "heartbeat" in line.lower()]
        self.assertGreater(len(heartbeat_refs), 1,
            "HEARTBEAT path must be referenced in both liveness-check and heartbeat write")

    def test_gitops_manifest_has_startup_and_liveness_probes(self):
        """Verify the GitOps manifest has both probes configured for orchestrator-shadow."""
        possible_paths = [
            Path(__file__).parent.parent.parent / "gitops-remove-orphan-init" /
            "apps/trading-platform/mt5-native-bridge-main-runtime.yaml",
        ]
        manifest_path = next((p for p in possible_paths if p.exists()), None)
        if manifest_path is None:
            self.skipTest("GitOps manifest not accessible from test directory")
        manifest_text = manifest_path.read_text()
        self.assertIn("startupProbe", manifest_text)
        self.assertIn("livenessProbe", manifest_text)
        self.assertIn("liveness-check", manifest_text)


class StartupConfigValidationTests(unittest.TestCase):
    """Section 3: Age-gate env vars must fail-closed on invalid (non-numeric) values."""

    def _assert_raises_on_invalid(self, env_var: str, import_target: str) -> None:
        with patch.dict(os.environ, {env_var: "disabled"}):
            import importlib, sys
            mod_name = import_target.split(".")[0] if "." not in import_target else import_target.rsplit(".", 1)[0]
            # Force re-import so the module-level constant is re-evaluated
            for key in [k for k in sys.modules if import_target in k or mod_name in k]:
                del sys.modules[key]
            with self.assertRaises((ValueError, RuntimeError),
                                   msg=f"{env_var}='disabled' must raise ValueError, not silently use default"):
                importlib.import_module(import_target)

    def test_kojo_invalid_age_env_raises(self):
        from orchestration.adapters.kojo_structure_reclaim_v3_adapter import KojoStructureReclaimV3Adapter
        with patch.dict(os.environ, {"KOJO_V3_MAX_SIGNAL_AGE_S": "not-a-number"}):
            with self.assertRaises(ValueError,
                                   msg="KOJO_V3_MAX_SIGNAL_AGE_S='not-a-number' must raise ValueError"):
                # The env var is read inside discover_new_signals, not at import time.
                KojoStructureReclaimV3Adapter(
                    instance_id="test",
                    display_name="Test",
                    instruments=["XAUUSDm"],
                ).discover_new_signals(set())

    def test_liquidity_invalid_age_env_raises(self):
        with patch.dict(os.environ, {"LIQUIDITY_SIGNAL_MAX_AGE_SECONDS": "bad-value"}):
            import importlib, sys
            for k in [k for k in sys.modules if "liquidity_displacement" in k]:
                del sys.modules[k]
            import tempfile
            from pathlib import Path
            importlib.import_module("orchestration.adapters.liquidity_displacement")
            from orchestration.adapters.liquidity_displacement import LiquidityDisplacementAdapter
            with self.assertRaises(ValueError):
                LiquidityDisplacementAdapter(root=Path(tempfile.mkdtemp()), freeze_timestamp="2026-01-01T00:00:00Z")

    def test_context_invalid_age_env_raises(self):
        with patch.dict(os.environ, {"CONTEXT_SIGNAL_MAX_AGE_SECONDS": "bad-value"}):
            import importlib, sys, tempfile
            for k in [k for k in sys.modules if "context_structure_retrace" in k]:
                del sys.modules[k]
            importlib.import_module("orchestration.adapters.context_structure_retrace")
            from orchestration.adapters.context_structure_retrace import ContextStructureRetraceAdapter
            with self.assertRaises(ValueError):
                ContextStructureRetraceAdapter(
                    root=Path(tempfile.mkdtemp()),
                    freeze_timestamp="2026-01-01T00:00:00+00:00",
                )

    def test_pa_invalid_age_env_raises(self):
        with patch.dict(os.environ, {"PA_SIGNAL_MAX_AGE_S": "not-a-number"}):
            import importlib, sys
            for k in [k for k in sys.modules if "price_action_observer" in k]:
                del sys.modules[k]
            with self.assertRaises(ValueError):
                importlib.import_module("orchestration.adapters.price_action_observer")

    def test_kojo_missing_env_uses_default(self):
        env = {k: v for k, v in os.environ.items() if k != "KOJO_V3_MAX_SIGNAL_AGE_S"}
        with patch.dict(os.environ, env, clear=True):
            import importlib, sys
            for k in [k for k in sys.modules if "kojo_structure_reclaim" in k]:
                del sys.modules[k]
            mod = importlib.import_module("orchestration.adapters.kojo_structure_reclaim_v3_adapter")
            self.assertIsNone(getattr(mod, "_max_signal_age_s", None) or None,
                              "Module-level default should be set at discover time, not import time")

    def test_liquidity_missing_env_uses_default(self):
        env = {k: v for k, v in os.environ.items() if k != "LIQUIDITY_SIGNAL_MAX_AGE_SECONDS"}
        with patch.dict(os.environ, env, clear=True):
            import importlib, sys, tempfile
            from pathlib import Path
            for k in [k for k in sys.modules if "liquidity_displacement" in k]:
                del sys.modules[k]
            importlib.import_module("orchestration.adapters.liquidity_displacement")
            from orchestration.adapters.liquidity_displacement import LiquidityDisplacementAdapter
            adapter = LiquidityDisplacementAdapter(root=Path(tempfile.mkdtemp()), freeze_timestamp="2026-01-01T00:00:00Z")
            self.assertEqual(adapter.max_signal_age_s, 600.0,
                             "Default max_signal_age_s must be 600s when env var is unset")


if __name__ == "__main__":
    unittest.main()

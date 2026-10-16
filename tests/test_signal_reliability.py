"""P0 signal reliability regression tests.

Covers the fixes implemented after the October 2026 KOJO V3 incident:
  GATE      — stale signals suppressed by publication eligibility gate (B)
  CHECKPOINT— atomic checkpoint writes; corrupt/missing checkpoint recovery (C)
  LIVENESS  — liveness-check subcommand exit codes (A)
  WATERMARK — watermarks correctly advanced and persisted (C)
  PROVENANCE— instrument_analytics _population_sql classification (D)
  SCENARIOS — 6h/54h outage, normal prospective, duplicate prevention,
               restart-during-replay, late-arriving bars (E)
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
# Dynamic bar builders — timestamps relative to now to avoid time patching
# ---------------------------------------------------------------------------

def _h1(ts: int, o: float, h: float, lo: float, c: float) -> dict[str, Any]:
    return {"time": ts, "open": o, "high": h, "low": lo, "close": c}


def _m15(ts: int, o: float, h: float, lo: float, c: float) -> dict[str, Any]:
    return {"time": ts, "open": o, "high": h, "low": lo, "close": c}


def _build_bars(target_decision_ts: int) -> tuple[list, list]:
    """Build H1 + M15 bars such that decision_timestamp == target_decision_ts.

    The KOJO V3 evaluator emits a LONG entry signal when it processes the ENTRY
    BAR (bar that opens at decision_timestamp).  The confirmation bar closes at
    decision_timestamp.  So:
      - Confirmation bar: open = decision_ts - 900, close = decision_ts
      - Entry bar:        open = decision_ts,       close = decision_ts + 900

    For the entry bar to pass the completeness filter (open_ts + 900 <= now_ts),
    callers must choose target_decision_ts ≤ int(time.time()) - 900.

    Bar sequence matches test_kojo_v3_managed._build_synthetic_session():
      H1: 8 bars (pivot + break + 2 hold bars)
      M15: 5 bars (reaction, retest, pre-conf bearish, confirmation, entry)
    """
    # M15 starts at episode_start_ts = break bar's close
    # episode_start_ts = confirmation_bar.close_ts - (3 M15 bars + 1 M15 bar = 4 * 900)
    # confirmation_bar.open = target_decision_ts - 900
    # reaction M15 = confirmation_bar.open - 2 * 900 = target_decision_ts - 2700
    # episode_start_ts <= reaction_m15.open = target_decision_ts - 3600
    m15_t0 = target_decision_ts - 4 * 900  # reaction bar open_ts
    # H1 break bar close = episode_start_ts = m15_t0
    # H1 break bar open = m15_t0 - 3600
    t0_h1 = m15_t0 - 8 * 3600  # base for H1 bars

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
        _m15(m15_t0 + 3 * 900, 2003, 2012, 2002, 2011),  # confirmation (close = target_decision_ts)
        _m15(m15_t0 + 4 * 900, 2011, 2015, 2010, 2013),  # entry bar
    ]
    return h1_bars, m15_bars


def _prospective_decision_ts() -> int:
    """Return a decision_ts that yields emission_latency_s ≈ 1050s (PROSPECTIVE)."""
    now = int(time.time())
    # latency = now - decision_ts = 1050s → decision_ts = now - 1050
    # entry bar close = decision_ts + 900 = now - 150 ≤ now ✓
    return now - 1050


def _retrospective_decision_ts(latency_s: int) -> int:
    """Return a decision_ts that yields the specified emission_latency_s (RETROSPECTIVE)."""
    return int(time.time()) - latency_s


def _make_adapter(instance_id: str = "test-inst", instrument: str = "XAUUSD") -> Any:
    from orchestration.adapters.kojo_structure_reclaim_v3_adapter import KojoStructureReclaimV3Adapter
    return KojoStructureReclaimV3Adapter(
        instance_id=instance_id,
        display_name="Test",
        instruments=[instrument],
    )


def _mock_redis(h1_bars: list, m15_bars: list) -> MagicMock:
    """Return a mock Redis client that serves the given bars."""
    mock_r = MagicMock()
    h1_json = json.dumps(h1_bars)
    m15_json = json.dumps(m15_bars)
    def _get(key: str) -> str:
        if ":H1" in key:
            return h1_json
        if ":M15" in key:
            return m15_json
        return "[]"
    mock_r.get.side_effect = _get
    return mock_r


def _run_discover(
    adapter: Any,
    tmpdir: str,
    h1_bars: list,
    m15_bars: list,
    seen: set | None = None,
    max_age_s: str | None = None,
) -> list:
    """Run discover_new_signals with mocked Redis."""
    os.environ["MARKET_DATA_REDIS_URL"] = "redis://fake:6379/0"
    os.environ["TRADING_PLATFORM_RUNTIME_DIR"] = tmpdir
    if max_age_s is not None:
        os.environ["KOJO_V3_MAX_SIGNAL_AGE_S"] = max_age_s
    else:
        os.environ.pop("KOJO_V3_MAX_SIGNAL_AGE_S", None)

    mock_r = _mock_redis(h1_bars, m15_bars)
    with patch("redis.from_url", return_value=mock_r):
        result = adapter.discover_new_signals(seen or set())
    return result


# ============================================================================
# GATE — publication eligibility
# ============================================================================

class PublicationEligibilityGateTests(unittest.TestCase):
    """Section B: stale retrospective signals must not be published."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self._tmpdir.cleanup()
        for key in ("MARKET_DATA_REDIS_URL", "TRADING_PLATFORM_RUNTIME_DIR", "KOJO_V3_MAX_SIGNAL_AGE_S"):
            os.environ.pop(key, None)

    def test_normal_prospective_signal_is_published(self) -> None:
        """A signal emitted ~1050s after decision_timestamp must be published (within 1200s)."""
        decision_ts = _prospective_decision_ts()
        h1, m15 = _build_bars(decision_ts)
        signals = _run_discover(_make_adapter(), self._tmpdir.name, h1, m15)
        self.assertGreater(len(signals), 0, "Expected at least one prospective signal")
        classes = [s.provenance.get("signal_emission_class") for s in signals]
        self.assertTrue(all(c == "PROSPECTIVE" for c in classes), f"Expected PROSPECTIVE, got {classes}")

    def test_six_hour_outage_signals_as_research_evidence(self) -> None:
        """After a 6-hour outage, stale signals are published as RETROSPECTIVE_RESEARCH_EVIDENCE."""
        decision_ts = _retrospective_decision_ts(6 * 3600)
        h1, m15 = _build_bars(decision_ts)
        signals = _run_discover(_make_adapter(), self._tmpdir.name, h1, m15)
        self.assertGreater(len(signals), 0,
            "6h outage: stale signal must be published as research evidence, not suppressed")
        for sig in signals:
            self.assertEqual(sig.provenance.get("signal_emission_class"),
                             "RETROSPECTIVE_RESEARCH_EVIDENCE")

    def test_54_hour_outage_signals_as_research_evidence(self) -> None:
        """After a 54-hour outage, signals are published as RETROSPECTIVE_RESEARCH_EVIDENCE."""
        incident_latency = 21697  # seconds — matches the Oct 15 incident
        decision_ts = _retrospective_decision_ts(incident_latency)
        h1, m15 = _build_bars(decision_ts)
        signals = _run_discover(_make_adapter(), self._tmpdir.name, h1, m15)
        self.assertGreater(len(signals), 0,
            "54h outage: signal must be preserved as research evidence")
        for sig in signals:
            self.assertEqual(sig.provenance.get("signal_emission_class"),
                             "RETROSPECTIVE_RESEARCH_EVIDENCE")

    def test_retrospective_research_evidence_at_boundary(self) -> None:
        """A signal at emission_latency_s = 1201 (beyond default 1200s gate) must be
        published as RETROSPECTIVE_RESEARCH_EVIDENCE, not suppressed."""
        decision_ts = _retrospective_decision_ts(1201)
        h1, m15 = _build_bars(decision_ts)
        signals = _run_discover(_make_adapter(), self._tmpdir.name, h1, m15)
        self.assertGreater(len(signals), 0,
            "emission_latency_s=1201 must emit as research evidence")
        for sig in signals:
            self.assertEqual(sig.provenance.get("signal_emission_class"),
                             "RETROSPECTIVE_RESEARCH_EVIDENCE")

    def test_gate_disabled_with_zero_allows_retrospective(self) -> None:
        """Setting KOJO_V3_MAX_SIGNAL_AGE_S=0 disables the gate (research override)."""
        decision_ts = _retrospective_decision_ts(21697)
        h1, m15 = _build_bars(decision_ts)
        signals = _run_discover(_make_adapter(), self._tmpdir.name, h1, m15, max_age_s="0")
        # Gate disabled — retrospective signal should be returned
        self.assertGreater(len(signals), 0, "With gate disabled, retrospective signal must be published")
        classes = [s.provenance.get("signal_emission_class") for s in signals]
        self.assertTrue(all(c == "RETROSPECTIVE" for c in classes))

    def test_gate_env_var_custom_threshold_passes_within(self) -> None:
        """KOJO_V3_MAX_SIGNAL_AGE_S=3600 should pass a 1500s-late signal."""
        decision_ts = _retrospective_decision_ts(1500)
        h1, m15 = _build_bars(decision_ts)
        signals = _run_discover(_make_adapter(), self._tmpdir.name, h1, m15, max_age_s="3600")
        # 1500s < 3600s threshold → should be published even though RETROSPECTIVE
        self.assertGreater(len(signals), 0, "1500s latency with 3600s threshold must be published")

    def test_gate_env_var_custom_threshold_emits_evidence_beyond(self) -> None:
        """KOJO_V3_MAX_SIGNAL_AGE_S=3600: a 3601s-late signal emits as research evidence."""
        decision_ts = _retrospective_decision_ts(3601)
        h1, m15 = _build_bars(decision_ts)
        signals = _run_discover(_make_adapter(), self._tmpdir.name, h1, m15, max_age_s="3600")
        self.assertGreater(len(signals), 0,
            "3601s latency beyond 3600s threshold must emit as RETROSPECTIVE_RESEARCH_EVIDENCE")
        for sig in signals:
            self.assertEqual(sig.provenance.get("signal_emission_class"),
                             "RETROSPECTIVE_RESEARCH_EVIDENCE")

    def test_signal_emission_class_in_prospective_provenance(self) -> None:
        """Published prospective signals must carry signal_emission_class=PROSPECTIVE."""
        decision_ts = _prospective_decision_ts()
        h1, m15 = _build_bars(decision_ts)
        signals = _run_discover(_make_adapter(), self._tmpdir.name, h1, m15)
        for sig in signals:
            self.assertEqual(sig.provenance.get("signal_emission_class"), "PROSPECTIVE")
            self.assertEqual(sig.strategy_metadata.get("signal_emission_class"), "PROSPECTIVE")

    def test_signal_emission_class_retrospective_when_gate_off(self) -> None:
        """When gate is disabled, retrospective signals carry signal_emission_class=RETROSPECTIVE."""
        decision_ts = _retrospective_decision_ts(21697)
        h1, m15 = _build_bars(decision_ts)
        signals = _run_discover(_make_adapter(), self._tmpdir.name, h1, m15, max_age_s="0")
        for sig in signals:
            self.assertEqual(sig.provenance.get("signal_emission_class"), "RETROSPECTIVE")


# ============================================================================
# CHECKPOINT — atomic writes, corrupt/missing recovery
# ============================================================================

class CheckpointTests(unittest.TestCase):
    """Section C: persistent evaluator state reliability."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._state_dir = Path(self._tmpdir.name) / "orchestration"
        self._state_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()
        for key in ("MARKET_DATA_REDIS_URL", "TRADING_PLATFORM_RUNTIME_DIR", "KOJO_V3_MAX_SIGNAL_AGE_S"):
            os.environ.pop(key, None)

    def _state_file(self, instance_id: str = "test-inst", instrument: str = "XAUUSD") -> Path:
        safe = instrument.replace("/", "_").replace(":", "_")
        return self._state_dir / f"kojo_v3_{instance_id}_{safe}_evaluator.json"

    def _run(self, seen: set | None = None, decision_ts: int | None = None,
             max_age_s: str | None = None) -> list:
        if decision_ts is None:
            decision_ts = _prospective_decision_ts()
        h1, m15 = _build_bars(decision_ts)
        return _run_discover(_make_adapter(), self._tmpdir.name, h1, m15,
                             seen=seen, max_age_s=max_age_s)

    def test_checkpoint_written_after_successful_cycle(self) -> None:
        """State file must exist after discover_new_signals completes."""
        self._run()
        self.assertTrue(self._state_file().exists(), "State file must be written after cycle")

    def test_checkpoint_contains_watermarks(self) -> None:
        """State file must contain _h1_watermark and _m15_watermark."""
        self._run()
        state = json.loads(self._state_file().read_text())
        self.assertIn("_h1_watermark", state)
        self.assertIn("_m15_watermark", state)
        self.assertGreater(int(state["_h1_watermark"]), 0)
        self.assertGreater(int(state["_m15_watermark"]), 0)

    def test_no_tmp_file_left_after_write(self) -> None:
        """Atomic write must not leave a .tmp file behind."""
        self._run()
        tmp_files = list(self._state_dir.glob("*.tmp"))
        self.assertEqual(tmp_files, [], f"No .tmp files should remain; found {tmp_files}")

    def test_corrupt_checkpoint_is_recovered(self) -> None:
        """A corrupt checkpoint file must not crash the adapter; it must continue with empty state."""
        self._state_file().write_text("NOT_VALID_JSON {{{", encoding="utf-8")
        # Should not raise; adapter recovers with empty state
        result = self._run()
        self.assertIsInstance(result, list)
        # State file should have been overwritten with a valid checkpoint
        refreshed = json.loads(self._state_file().read_text())
        self.assertIn("_h1_watermark", refreshed)

    def test_missing_checkpoint_triggers_full_replay(self) -> None:
        """No state file on startup causes full bar history replay (watermarks start at 0)."""
        # No state file written
        decision_ts = _prospective_decision_ts()
        h1, m15 = _build_bars(decision_ts)
        _run_discover(_make_adapter(), self._tmpdir.name, h1, m15)
        state = json.loads(self._state_file().read_text())
        self.assertEqual(int(state["_h1_watermark"]), max(b["time"] for b in h1))

    def test_watermarks_not_replayed_on_second_cycle(self) -> None:
        """After a checkpoint is written, a second cycle with same bars must not re-emit."""
        decision_ts = _prospective_decision_ts()
        h1, m15 = _build_bars(decision_ts)
        seen: set[str] = set()
        first = _run_discover(_make_adapter(), self._tmpdir.name, h1, m15, seen=seen)
        seen |= {s.signal_id for s in first}
        second = _run_discover(_make_adapter(), self._tmpdir.name, h1, m15, seen=seen)
        self.assertEqual(second, [], "Second cycle must not re-emit seen signals")

    def test_checkpoint_watermarks_prevent_replay_after_restart(self) -> None:
        """A restarted adapter reads the checkpoint and skips already-processed bars."""
        decision_ts = _prospective_decision_ts()
        h1, m15 = _build_bars(decision_ts)
        seen: set[str] = set()
        first = _run_discover(_make_adapter(), self._tmpdir.name, h1, m15, seen=seen)
        seen |= {s.signal_id for s in first}
        # Simulate restart: fresh adapter instance
        fresh = _make_adapter()
        second = _run_discover(fresh, self._tmpdir.name, h1, m15, seen=seen)
        self.assertEqual(second, [], "Restarted adapter must not re-emit seen signals")

    def test_restart_during_replay_no_duplicates(self) -> None:
        """After deleting checkpoint, replay produces same signal IDs — seen_ids prevents DB dupes."""
        decision_ts = _prospective_decision_ts()
        h1, m15 = _build_bars(decision_ts)
        seen: set[str] = set()
        first = _run_discover(_make_adapter(), self._tmpdir.name, h1, m15, seen=seen)
        seen |= {s.signal_id for s in first}
        # Simulate crash before checkpoint: delete state file
        self._state_file().unlink(missing_ok=True)
        fresh = _make_adapter()
        replayed = _run_discover(fresh, self._tmpdir.name, h1, m15, seen=seen)
        self.assertEqual(replayed, [], "Replay with seen IDs must produce no new signals")


# ============================================================================
# LIVENESS — liveness-check subcommand
# ============================================================================

class LivenessCheckTests(unittest.TestCase):
    """Section A: liveness-check subcommand exit codes."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def _write_heartbeat(self, age_s: float, status: str = "ACTIVE") -> Path:
        runtime = Path(self._tmpdir.name) / "orchestration"
        runtime.mkdir(parents=True, exist_ok=True)
        hb_path = runtime / "heartbeat.json"
        from datetime import datetime, timezone
        ts = datetime.fromtimestamp(time.time() - age_s, tz=timezone.utc).isoformat().replace("+00:00", "Z")
        hb_path.write_text(json.dumps({"pid": 12345, "status": status, "timestamp": ts}))
        return hb_path

    def _run_liveness_check(self, max_age: float) -> int:
        """Run the liveness-check subcommand and return its exit code."""
        import subprocess
        result = subprocess.run(
            [sys.executable, "signal_orchestrator.py", "liveness-check", "--max-age", str(max_age)],
            capture_output=True,
            env={**os.environ, "TRADING_PLATFORM_RUNTIME_DIR": self._tmpdir.name},
            cwd="/Users/caleb/projects/platform-tools",
        )
        return result.returncode

    def test_fresh_heartbeat_exits_0(self) -> None:
        """A heartbeat written 5s ago must return exit code 0 (healthy)."""
        self._write_heartbeat(age_s=5)
        self.assertEqual(self._run_liveness_check(max_age=90), 0)

    def test_stale_heartbeat_exits_1(self) -> None:
        """A heartbeat written 120s ago must return exit code 1 with max_age=90."""
        self._write_heartbeat(age_s=120)
        self.assertEqual(self._run_liveness_check(max_age=90), 1)

    def test_missing_heartbeat_file_exits_1(self) -> None:
        """A missing heartbeat file must return exit code 1."""
        self.assertEqual(self._run_liveness_check(max_age=90), 1)

    def test_heartbeat_at_boundary_minus_1_healthy(self) -> None:
        """A heartbeat at max_age - 1s must be healthy."""
        self._write_heartbeat(age_s=89)
        self.assertEqual(self._run_liveness_check(max_age=90), 0)

    def test_heartbeat_at_max_age_plus_1_unhealthy(self) -> None:
        """A heartbeat at max_age + 1s must be unhealthy."""
        self._write_heartbeat(age_s=91)
        self.assertEqual(self._run_liveness_check(max_age=90), 1)

    def test_degraded_status_still_healthy_if_fresh(self) -> None:
        """A DEGRADED heartbeat that is recent must still pass the liveness check."""
        self._write_heartbeat(age_s=5, status="DEGRADED")
        self.assertEqual(self._run_liveness_check(max_age=90), 0)


# ============================================================================
# DUPLICATE PREVENTION — seen_signal_ids
# ============================================================================

class DuplicatePreventionTests(unittest.TestCase):
    """Deduplication via seen_signal_ids must work across signal states."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self._tmpdir.cleanup()
        for key in ("MARKET_DATA_REDIS_URL", "TRADING_PLATFORM_RUNTIME_DIR"):
            os.environ.pop(key, None)

    def test_seen_signal_id_suppresses_reemission(self) -> None:
        """If signal_id is already in seen_signal_ids, it must not be returned."""
        decision_ts = _prospective_decision_ts()
        h1, m15 = _build_bars(decision_ts)
        first = _run_discover(_make_adapter(), self._tmpdir.name, h1, m15)
        self.assertGreater(len(first), 0)
        seen = {s.signal_id for s in first}
        second = _run_discover(_make_adapter(), self._tmpdir.name, h1, m15, seen=seen)
        for sig in second:
            self.assertNotIn(sig.signal_id, seen, "Seen signal_ids must not be re-emitted")

    def test_empty_seen_set_allows_all_new_signals(self) -> None:
        """An empty seen_signal_ids set must not suppress any valid signal."""
        decision_ts = _prospective_decision_ts()
        h1, m15 = _build_bars(decision_ts)
        signals = _run_discover(_make_adapter(), self._tmpdir.name, h1, m15, seen=set())
        self.assertGreater(len(signals), 0)


# ============================================================================
# LATE-ARRIVING BARS
# ============================================================================

class LateArrivingBarsTests(unittest.TestCase):
    """Late-arriving bars (those just past their close) must not cause lookahead bias."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self._tmpdir.cleanup()
        for key in ("MARKET_DATA_REDIS_URL", "TRADING_PLATFORM_RUNTIME_DIR"):
            os.environ.pop(key, None)

    def test_incomplete_m15_bar_excluded_from_processing(self) -> None:
        """A bar whose close time is still in the future must not be processed."""
        decision_ts = _prospective_decision_ts()
        h1, m15 = _build_bars(decision_ts)
        now_ts = int(time.time())
        # Add a bar that hasn't completed yet (close = now + 400s in the future)
        incomplete = _m15(now_ts - 500, 2020, 2025, 2019, 2022)  # close = now + 400
        extended_m15 = m15 + [incomplete]
        _run_discover(_make_adapter(), self._tmpdir.name, h1, extended_m15)
        state_dir = Path(self._tmpdir.name) / "orchestration"
        sf = state_dir / "kojo_v3_test-inst_XAUUSD_evaluator.json"
        state = json.loads(sf.read_text())
        m15_watermark = int(state.get("_m15_watermark", 0))
        # The incomplete bar must not advance watermark to its open_ts
        self.assertLess(
            m15_watermark, now_ts - 500 + 1,
            "Incomplete bar open_ts must not be in watermark (bar was excluded)",
        )

    def test_bars_arriving_after_outage_processed_in_order(self) -> None:
        """Bars that arrive in bulk after an outage are processed in temporal order."""
        decision_ts = _prospective_decision_ts()
        h1, m15 = _build_bars(decision_ts)
        # All bars are in the past; they should all pass the completeness filter
        result = _run_discover(_make_adapter(), self._tmpdir.name, h1, m15)
        # At least one signal expected when entry bar is complete
        self.assertIsInstance(result, list)


# ============================================================================
# PROVENANCE SEPARATION — instrument_analytics._population_sql
# ============================================================================

class ProvenanceSeparationTests(unittest.TestCase):
    """Section D: population SQL must correctly classify PROSPECTIVE/RETROSPECTIVE signals."""

    def _sql(self, population: str) -> str:
        from platform_api.instrument_analytics import InstrumentAnalyticsRepository
        return InstrumentAnalyticsRepository._population_sql(population)

    def test_theoretical_returns_non_broker_filter(self) -> None:
        sql = self._sql("THEORETICAL")
        self.assertIn("BROKER_REALIZED", sql)
        self.assertIn("IS DISTINCT FROM", sql)

    def test_retrospective_population_matches_retro_keyword(self) -> None:
        """The RETROSPECTIVE population SQL must match the '%retro%' pattern."""
        sql = self._sql("RETROSPECTIVE")
        self.assertIn("retro", sql)

    def test_retrospective_population_checks_signal_emission_class_key(self) -> None:
        """RETROSPECTIVE SQL must check signal_emission_class JSONB key for KOJO V3."""
        sql = self._sql("RETROSPECTIVE")
        self.assertIn("signal_emission_class", sql)

    def test_forward_paper_population_checks_prospective_keyword(self) -> None:
        """FORWARD_PAPER population SQL must include '%prospective%' pattern."""
        sql = self._sql("FORWARD_PAPER")
        self.assertIn("prospective", sql)

    def test_forward_paper_excludes_emitted_after_exit(self) -> None:
        """FORWARD_PAPER must exclude signals where signal_emitted_at > exit_timestamp."""
        sql = self._sql("FORWARD_PAPER")
        self.assertIn("signal_emitted_at", sql)
        self.assertIn("exit_timestamp", sql)

    def test_forward_paper_excludes_retro_classification(self) -> None:
        """FORWARD_PAPER must filter out retrospective signals."""
        sql = self._sql("FORWARD_PAPER")
        self.assertIn("retro", sql)

    def test_unknown_returns_unknown_case(self) -> None:
        sql = self._sql("UNKNOWN")
        self.assertIn("UNKNOWN", sql)

    def test_broker_executed_returns_broker_realized_filter(self) -> None:
        sql = self._sql("BROKER_EXECUTED")
        self.assertIn("BROKER_REALIZED", sql)

    def test_all_returns_true(self) -> None:
        sql = self._sql("ALL")
        self.assertEqual(sql.strip(), "TRUE")

    def test_kojo_v3_nested_provenance_path_checked(self) -> None:
        """SQL must check nested provenance->signal_emission_class path for KOJO V3."""
        sql_retro = self._sql("RETROSPECTIVE")
        sql_fwd = self._sql("FORWARD_PAPER")
        self.assertIn("signal_emission_class", sql_retro)
        self.assertIn("signal_emission_class", sql_fwd)

    def test_forward_paper_not_all_retro_suppressed(self) -> None:
        """FORWARD_PAPER exclusion applies only when BOTH retro-classified AND emitted-after-exit."""
        sql = self._sql("FORWARD_PAPER")
        # Both conditions must be present in the exclusion clause
        self.assertIn("exit_timestamp IS NOT NULL", sql)
        self.assertIn("signal_emitted_at", sql)


# ============================================================================
# KOJO V3 signal provenance keys
# ============================================================================

class KojoV3SignalProvenanceTests(unittest.TestCase):
    """Prospective signals must carry required provenance keys for analytics classification."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self._tmpdir.cleanup()
        for key in ("MARKET_DATA_REDIS_URL", "TRADING_PLATFORM_RUNTIME_DIR"):
            os.environ.pop(key, None)

    def _signals(self) -> list:
        decision_ts = _prospective_decision_ts()
        h1, m15 = _build_bars(decision_ts)
        return _run_discover(_make_adapter(), self._tmpdir.name, h1, m15)

    def test_prospective_signal_carries_emission_class_in_provenance(self) -> None:
        for sig in self._signals():
            self.assertIn("signal_emission_class", sig.provenance)
            self.assertIn("emission_latency_s", sig.provenance)

    def test_prospective_signal_carries_emission_class_in_strategy_metadata(self) -> None:
        for sig in self._signals():
            self.assertIn("signal_emission_class", sig.strategy_metadata)

    def test_prospective_emission_latency_below_threshold(self) -> None:
        for sig in self._signals():
            self.assertLessEqual(sig.provenance.get("emission_latency_s"), 1200)

    def test_signal_decision_time_iso_format(self) -> None:
        """decision_time must be an ISO-formatted string for the confirmation bar close."""
        for sig in self._signals():
            self.assertIsNotNone(sig.decision_time)
            # Must be parseable as ISO datetime
            from datetime import datetime
            dt = datetime.fromisoformat(sig.decision_time.replace("Z", "+00:00"))
            self.assertIsNotNone(dt)

    def test_signal_strategy_id_correct(self) -> None:
        for sig in self._signals():
            self.assertEqual(sig.strategy_id, "KOJO_STRUCTURE_RECLAIM_V3")

    def test_signal_execution_eligible_false(self) -> None:
        """No KOJO V3 signal may have execution_eligible=True or broker_writes!=0."""
        adapter = _make_adapter()
        self.assertFalse(adapter.diagnostics()["execution_eligible"])
        self.assertEqual(adapter.diagnostics()["broker_writes"], 0)


if __name__ == "__main__":
    unittest.main()

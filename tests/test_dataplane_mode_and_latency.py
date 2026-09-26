from __future__ import annotations

import os
import unittest

from dataplane.latency import LatencyRecorder, LatencyTimestamps
from dataplane.mode import SignalDataPlaneFlags, SignalDataPlaneMode


class DataPlaneModeTests(unittest.TestCase):
    ENV_KEYS = ("SIGNAL_DATA_PLANE_MODE", "SIGNAL_DATA_PLANE_NATS_FIRST_ENABLED")

    def setUp(self):
        self._saved = {k: os.environ.pop(k, None) for k in self.ENV_KEYS}

    def tearDown(self):
        for k, v in self._saved.items():
            if v is not None:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)

    def test_default_mode_is_db_first(self):
        self.assertEqual(SignalDataPlaneFlags.from_env().mode, SignalDataPlaneMode.DB_FIRST)

    def test_nats_first_requires_the_explicit_enable_flag(self):
        os.environ["SIGNAL_DATA_PLANE_MODE"] = "NATS_FIRST"
        with self.assertRaises(ValueError):
            SignalDataPlaneFlags.from_env()
        os.environ["SIGNAL_DATA_PLANE_NATS_FIRST_ENABLED"] = "true"
        flags = SignalDataPlaneFlags.from_env()
        self.assertEqual(flags.mode, SignalDataPlaneMode.NATS_FIRST)

    def test_enable_flag_without_mode_selects_nats_first(self):
        os.environ["SIGNAL_DATA_PLANE_NATS_FIRST_ENABLED"] = "true"
        self.assertEqual(SignalDataPlaneFlags.from_env().mode, SignalDataPlaneMode.NATS_FIRST)

    def test_enable_flag_true_with_explicit_db_first_mode_is_rejected(self):
        os.environ["SIGNAL_DATA_PLANE_MODE"] = "DB_FIRST"
        os.environ["SIGNAL_DATA_PLANE_NATS_FIRST_ENABLED"] = "true"
        with self.assertRaises(ValueError):
            SignalDataPlaneFlags.from_env()

    def test_validate_fails_closed_without_confirmed_nats_availability(self):
        flags = SignalDataPlaneFlags(SignalDataPlaneMode.NATS_FIRST, True)
        with self.assertRaises(RuntimeError):
            flags.validate(nats_available=False)
        flags.validate(nats_available=True)  # does not raise

    def test_invalid_mode_string_rejected(self):
        os.environ["SIGNAL_DATA_PLANE_MODE"] = "SOMETHING_ELSE"
        with self.assertRaises(ValueError):
            SignalDataPlaneFlags.from_env()


class LatencyInstrumentationTests(unittest.TestCase):
    def test_metrics_computed_only_for_present_timestamp_pairs(self):
        t = LatencyTimestamps(path="NATS_FIRST", t0_decision=0.0, t1_orchestrator_accept=0.001,
                              t2_publish_initiated=0.002, t3_puback=0.004, t4_distribution_receive=0.0045,
                              t5_projector_begin=0.010, t6_projector_commit=0.015)
        metrics = t.metrics_ms()
        self.assertAlmostEqual(metrics["strategy_to_bus_ms"], 2.0, places=3)
        self.assertAlmostEqual(metrics["bus_publish_ack_ms"], 2.0, places=3)
        self.assertAlmostEqual(metrics["bus_to_distribution_ms"], 0.5, places=3)
        self.assertAlmostEqual(metrics["projector_processing_ms"], 5.0, places=3)
        self.assertAlmostEqual(metrics["end_to_end_projection_ms"], 15.0, places=3)
        self.assertAlmostEqual(metrics["end_to_end_acceptance_ms"], 4.0, places=3)

    def test_missing_timestamps_omit_their_metrics_rather_than_erroring(self):
        t = LatencyTimestamps(path="DB_FIRST", t0_decision=0.0, t2_publish_initiated=0.001, t3_puback=0.002)
        metrics = t.metrics_ms()
        self.assertIn("bus_publish_ack_ms", metrics)
        self.assertNotIn("bus_to_distribution_ms", metrics)
        self.assertNotIn("projector_processing_ms", metrics)

    def test_recorder_computes_percentiles_and_separates_paths(self):
        recorder = LatencyRecorder()
        for ack_ms in (1.0, 2.0, 3.0, 4.0, 100.0):
            recorder.record(LatencyTimestamps(path="NATS_FIRST", t2_publish_initiated=0.0, t3_puback=ack_ms / 1000.0))
        for ack_ms in (10.0, 12.0):
            recorder.record(LatencyTimestamps(path="DB_FIRST", t2_publish_initiated=0.0, t3_puback=ack_ms / 1000.0))
        nats_dist = recorder.distribution("NATS_FIRST:bus_publish_ack_ms")
        db_dist = recorder.distribution("DB_FIRST:bus_publish_ack_ms")
        self.assertEqual(nats_dist["n"], 5)
        self.assertEqual(nats_dist["min"], 1.0)
        self.assertEqual(nats_dist["max"], 100.0)
        self.assertEqual(db_dist["n"], 2)
        self.assertIsNone(recorder.distribution("NO_SUCH_KEY"))
        report = recorder.report()
        self.assertIn("NATS_FIRST:bus_publish_ack_ms", report)
        self.assertIn("DB_FIRST:bus_publish_ack_ms", report)


if __name__ == "__main__":
    unittest.main()

import logging
import unittest
from unittest import mock

from execution_v2.risk_state.collector import RiskStateCollector


class RiskReferenceTimingTests(unittest.TestCase):
    def test_reference_batch_duration_uses_monotonic_start_time(self):
        store = mock.Mock()
        store.read_snapshot.return_value = None
        store.read_health.return_value = {}
        store.read_market_metadata.return_value = {
            "tick_size": 0.01, "tick_value": 0.01,
            "min_lot": 0.01, "max_lot": 200.0, "lot_step": 0.01,
            "observed_at": 1000.0,
        }
        collector = RiskStateCollector(
            store, mock.Mock(), canonical_for=lambda symbol: symbol,
            reference_symbols=lambda: ["BTCUSDm"], clock=lambda: 1000.0,
        )

        with self.assertLogs("execution_v2.risk_state.collector", level=logging.INFO) as logs:
            with mock.patch("execution_v2.risk_state.collector.time.monotonic",
                            side_effect=[100.0, 101.0, 101.25, 101.25]):
                self.assertTrue(collector.collect_reference())

        batch_log = next(line for line in logs.output if "risk_reference_batch_succeeded" in line)
        self.assertIn("elapsed_ms=1250.0", batch_log)
        self.assertNotIn("-", batch_log.split("elapsed_ms=", 1)[1])


if __name__ == "__main__":
    unittest.main()

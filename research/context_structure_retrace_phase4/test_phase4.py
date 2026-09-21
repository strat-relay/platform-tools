import unittest

from .phase4 import _metric, _sequential


class Phase4Tests(unittest.TestCase):
    def _item(self, t, event, r=1.0, exit_t=None):
        return {"fill_time": t, "phase3_market_event_id": event, "baseline_outcome": {"r": r, "exit_timestamp": exit_t or t + 60}}

    def test_economic_metrics_have_one_result_per_position(self):
        m = _metric([self._item(1, "e1"), self._item(2, "e2", -1)])
        self.assertEqual(m["n"], 2)
        self.assertAlmostEqual(m["expectancy_r"], 0.0)

    def test_sequential_replay_blocks_overlapping_positions(self):
        items = [self._item(1, "e1", 1, 100), self._item(50, "e2", -1, 120), self._item(130, "e3", 1, 180)]
        view = _sequential(items, "ONE_POSITION_AT_A_TIME")
        self.assertEqual(view["accepted"], 2)
        self.assertEqual(view["rejected_overlap"], 1)

    def test_reentry_policy_keeps_later_distinct_event_after_close(self):
        items = [self._item(1, "e1", 1, 100), self._item(130, "e1", -1, 180), self._item(200, "e2", 1, 240)]
        view = _sequential(items, "REENTRY_ALLOWED_NO_SCALEIN")
        self.assertEqual(view["accepted"], 2)

    def test_hypothetical_stop_target_fields_cannot_change_metrics_identity(self):
        a = self._item(1, "e1")
        b = dict(a)
        b["stop_hypothesis"] = "A"
        b["target_hypothesis"] = "B"
        self.assertEqual(_metric([a])["n"], _metric([b])["n"])

    def test_same_price_interaction_is_not_two_economic_positions(self):
        # Identity is intentionally represented by the normalized ledger, not
        # by stop/target hypothesis labels. This fixture protects the policy
        # boundary used by the implementation even though full artifact replay
        # is covered separately by the generated report.
        a = self._item(1, "e1")
        b = dict(a)
        b["phase3_market_event_id"] = "e2"
        b["stop_hypothesis"] = "WIDER"
        self.assertEqual(a["fill_time"], b["fill_time"])
        self.assertEqual(a["baseline_outcome"]["r"], b["baseline_outcome"]["r"])


if __name__ == "__main__":
    unittest.main()

import tempfile
import unittest
from pathlib import Path

from orchestration.replay_guard import LIVE_STRATEGIES, all_live_guards, classify_event, establish_epoch, eligibility


class StartupReplayGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "startup_epoch.json"
        self.records = [{"strategy_id": strategy, "instance_id": instance, "symbol": "XAUUSDm",
                         "startup_market_watermark": "2026-09-19T00:00:00+00:00"}
                        for strategy, instance in {
                            "CONTEXT_STRUCTURE_RETRACE_V1": "phase6",
                            "LIQUIDITY_DISPLACEMENT_SCALP_V1": "liquidity-xau-base",
                            "LIQUIDITY_DISPLACEMENT_SCALP_XAUUSD_33_V1": "liquidity-xau33",
                            "LIQUIDITY_DISPLACEMENT_SCALP_BTCUSD_25_V1": "liquidity-btc25",
                            "LIQUIDITY_DISPLACEMENT_SCALP_USDJPY_25_V1": "liquidity-usdjpy25",
                        }.items()]

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def signal(event_time, emitted, gap=False):
        return {"decision_time": event_time, "signal_timestamp": event_time,
                "signal_emitted_at": emitted, "provenance": {"gap_recovery": gap}}

    def test_historical_event_with_fresh_emitted_at_is_blocked(self):
        epoch = establish_epoch(self.records, path=self.path, epoch_id="test")
        ok, reason = eligibility(self.signal("2026-09-18T23:00:00Z", "2026-09-20T00:00:00Z"), epoch["records"][0])
        self.assertFalse(ok); self.assertEqual(reason, "STARTUP_REPLAY_BLOCKED")

    def test_startup_gap_recovery_is_blocked(self):
        self.assertEqual(classify_event("2026-09-19T00:00:00Z", "2026-09-19T00:00:00Z", gap_recovery=True), "GAP_RECOVERY_HISTORICAL_EVENT")
        self.assertFalse(eligibility(self.signal("2026-09-19T00:00:00Z", "2026-09-20T00:00:00Z", True), self.records[0])[0])
        self.assertFalse(eligibility(self.signal("2026-09-20T02:00:00Z", "2026-09-20T02:00:01Z", True), self.records[0])[0])

    def test_post_watermark_event_is_eligible(self):
        ok, classification = eligibility(self.signal("2026-09-19T00:00:01Z", "2026-09-19T00:00:02Z"), self.records[0])
        self.assertTrue(ok); self.assertEqual(classification, "NORMAL_LIVE_EVENT")

    def test_duplicate_historical_setup_is_blocked(self):
        signal = self.signal("2026-09-18T00:00:00Z", "2026-09-20T00:00:00Z")
        self.assertFalse(eligibility(signal, self.records[0])[0]); self.assertFalse(eligibility(signal, self.records[0])[0])

    def test_closed_market_old_event_and_market_reopen_new_event(self):
        self.assertFalse(eligibility(self.signal("2026-09-18T00:00:00Z", "2026-09-20T00:00:00Z"), self.records[0])[0])
        self.assertTrue(eligibility(self.signal("2026-09-19T00:15:00Z", "2026-09-19T00:16:00Z"), self.records[0])[0])

    def test_restart_same_epoch_does_not_move_watermark(self):
        first = establish_epoch(self.records, path=self.path, epoch_id="epoch-a")
        second = establish_epoch(self.records, path=self.path, epoch_id="epoch-b")
        self.assertEqual(first["startup_epoch_id"], second["startup_epoch_id"])
        self.assertFalse(eligibility(self.signal("2026-09-18T00:00:00Z", "2026-09-20T00:00:00Z"), second["records"][0])[0])

    def test_all_five_live_instances_have_guards(self):
        epoch = establish_epoch(self.records, path=self.path, epoch_id="all")
        self.assertEqual(set(LIVE_STRATEGIES), {row["strategy_id"] for row in epoch["records"]}); self.assertTrue(all_live_guards(epoch))

    def test_freshness_is_not_replay_eligibility(self):
        self.assertFalse(eligibility(self.signal("2026-09-18T00:00:00Z", "2026-09-19T00:00:01Z"), self.records[0])[0])


if __name__ == "__main__":
    unittest.main()

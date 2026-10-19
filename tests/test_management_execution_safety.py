"""Execution-boundary regressions using broker reads and recording writes, without a database."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest

from execution_v2.management import ManagementWorker, ManagementOutcome, stop_is_safe


class RecordingManager(ManagementWorker):
    def __init__(self):
        self.mode = "LIVE"
        self.position = {"ticket": 42, "symbol": "XAUUSDm", "type": 0,
                         "sl": 90.0, "tp": 120.0}
        self.rows = []
        self.submissions = []
        worker = SimpleNamespace(conn=None, account_id="test", authority_provider=lambda: "ENABLED",
                                 bridge=SimpleNamespace(read_positions=lambda: [self.position]))
        super().__init__(worker, tm_mode_reader=lambda: self.mode)

    def _platform_link(self, trade_id):
        return {"direction": "LONG", "execution_intent_id": "E1", "ticket": "42", "symbol": "XAUUSDm"}

    def _existing(self, decision_id):
        return None

    def _existing_close(self, trade_id):
        return None

    def _record(self, row):
        self.rows.append(row)
        return True

    def _update(self, intent_id, **fields):
        self.rows.append(fields)

    def _execute(self, intent_id, tool, args, broker_action, link, **kwargs):
        self.submissions.append(args)
        return ManagementOutcome("APPLIED", intent_id)


class ManagementSafetyTests(unittest.TestCase):
    def setUp(self):
        self.manager = RecordingManager()
        self.now = datetime(2026, 10, 19, tzinfo=timezone.utc)

    def decide(self, *, age=5, action="MOVE_STOP", **params):
        return self.manager.process_decision({
            "decision_id": "D1", "managed_trade_id": "T1", "action": action,
            "decision_time": (self.now - timedelta(seconds=age)).isoformat(),
            "parameters": params}, now_utc=self.now)

    def rejected(self, reason, **kwargs):
        result = self.decide(**kwargs)
        self.assertEqual((result.status, result.reason), ("REJECTED", reason))
        self.assertEqual(self.manager.submissions, [])

    def test_future_decision_cannot_write(self):
        self.rejected("DECISION_TIME_IN_FUTURE", age=-1, new_stop=100)

    def test_freshness_boundary(self):
        self.assertEqual(self.decide(age=120, new_stop=100).status, "APPLIED")
        self.manager.submissions.clear()
        self.rejected("STALE_DECISION", age=120.001, new_stop=100)

    def test_nonfinite_or_malformed_prices_cannot_write(self):
        for field in ("new_stop", "new_target"):
            for value in (float("nan"), float("inf"), -float("inf"), "bad"):
                with self.subTest(field=field, value=value):
                    self.rejected("INVALID_PRICE_LEVEL", **{field: value})

    def test_invalid_broker_levels_cannot_write(self):
        for field in ("sl", "tp"):
            with self.subTest(field=field):
                self.manager.position[field] = float("nan")
                self.rejected("INVALID_BROKER_PRICE_LEVEL", new_stop=100)
                self.manager.position[field] = 90 if field == "sl" else 120

    def test_wrong_symbol_cannot_close(self):
        self.manager.position["symbol"] = "BTCUSDm"
        self.rejected("POSITION_SYMBOL_MISMATCH", action="EXIT")

    def test_invalid_position_types_cannot_close(self):
        for value in (None, -1, 2, 0.5, True, "bad", float("inf")):
            with self.subTest(value=value):
                self.manager.position["type"] = value
                self.rejected("POSITION_TYPE_INVALID", action="EXIT")

    def test_safe_moves_preserve_target_and_never_loosen_stop(self):
        self.rejected("RISK_INCREASING_STOP_CHANGE", new_stop=89)
        self.assertEqual(self.decide(new_stop=100).status, "APPLIED")
        self.assertEqual(self.manager.submissions[0]["take_profit"], 120)

    def test_stop_safety_rejects_nonfinite_and_invalid_direction(self):
        for direction, stop, requested in (("SHORT", 0, float("nan")),
                                            ("LONG", 90, float("inf")),
                                            ("LONG", float("nan"), 100),
                                            ("UNKNOWN", 90, 100)):
            self.assertFalse(stop_is_safe(direction, stop, requested))
        self.assertTrue(stop_is_safe("SHORT", 110, 105))

    def test_mode_change_during_ownership_acquisition_fences_submission(self):
        def acquire():
            self.manager.mode = "SHADOW"
            return 7
        self.manager.w._acquire_generation = acquire
        result = ManagementWorker._execute(self.manager, "I1", "mt5_close_position",
                                           {"ticket": 42}, "CLOSE", {})
        self.assertEqual((result.status, result.reason),
                         ("FENCED", "TRADE_MANAGER_NOT_LIVE_BEFORE_SUBMISSION"))
        self.assertEqual(self.manager.submissions, [])


if __name__ == "__main__":
    unittest.main()

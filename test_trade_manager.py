import unittest
from trade_manager.engine import (ManagementPolicy, TradeManager, ema, ema_interaction,
                                  ema_rejection, excursion, management_intent, position_r,
                                  structural_stop)


class TradeManagerTests(unittest.TestCase):
    def setUp(self):
        self.position = {"strategy_id":"S", "setup_id":"U", "economic_position_id":"P", "symbol":"X",
                         "direction":"SHORT", "entry":100.0, "original_stop":110.0, "original_target":80.0,
                         "current_stop":110.0, "current_target":80.0, "size":1, "status":"OPEN"}

    def test_r_long_short(self):
        self.assertEqual(position_r("SHORT",100,110,90), 1.0)
        self.assertEqual(position_r("LONG",100,90,110), 1.0)

    def test_no_future_data_and_deterministic_hold(self):
        m = TradeManager()
        market = {"timestamp":"t1", "current_price":90.0, "ema_200":None, "future_data_used":False}
        a = m.evaluate(self.position, market, timestamp="t1")
        self.assertEqual(a["action"], "HOLD"); self.assertEqual(a["current_R"], 1.0)
        self.assertTrue(a["advisory_only"]); self.assertIn("NO_MANAGEMENT_CHANGE", a["reason_codes"])

    def test_duplicate_key_is_recorded(self):
        m = TradeManager(); market={"timestamp":"t1","current_price":90,"future_data_used":False}
        self.assertFalse(m.evaluate(self.position,market,timestamp="t1")["duplicate_suppressed"])
        self.assertTrue(m.evaluate(self.position,market,timestamp="t1")["duplicate_suppressed"])

    def test_closed_position(self):
        p=dict(self.position,status="TARGET_HIT")
        self.assertIn("POSITION_CLOSED", TradeManager().evaluate(p,{"timestamp":"t","current_price":90},timestamp="t")["reason_codes"])

    def test_policy_versioning_and_intent_contract(self):
        d=TradeManager(ManagementPolicy(policy_id="P",version="2")).evaluate(self.position,{"timestamp":"t","current_price":90},timestamp="t")
        i=management_intent(d); self.assertEqual(i["action"],"HOLD"); self.assertEqual(i["authorization_mode"],"ADVISORY_SHADOW")

    def test_ema_is_causal(self):
        self.assertIsNone(ema([1,2],3)); self.assertEqual(ema([1,2,3],3),2)

    def test_mfe_mae_long_and_short(self):
        short = excursion("SHORT", 100, 110, 90)
        self.assertEqual(short["mfe_R"], 1.0); self.assertEqual(short["mae_R"], 0.0)
        long = excursion("LONG", 100, 90, 110)
        self.assertEqual(long["mfe_R"], 1.0); self.assertEqual(long["mae_R"], 0.0)

    def test_ema_interaction_and_rejection_are_causal(self):
        self.assertEqual(ema_interaction(100.1, 100, .2), "EMA_TOUCH")
        self.assertTrue(ema_rejection({"high": 101, "low": 99, "close": 99.5}, 100, "SHORT"))
        self.assertTrue(ema_rejection({"high": 101, "low": 99, "close": 100.5}, 100, "LONG"))

    def test_structural_stop(self):
        self.assertEqual(structural_stop("SHORT", 100, .5), 100.5)
        self.assertEqual(structural_stop("LONG", 100, .5), 99.5)

    def test_stale_market_fails_closed(self):
        p = ManagementPolicy(max_market_age_ms=2000)
        d = TradeManager(p).evaluate(self.position, {"timestamp":"t", "current_price":90, "age_ms":2001}, timestamp="t")
        self.assertEqual(d["reason_codes"], ["STALE_MARKET_DATA"])

    def test_no_broker_surface(self):
        import trade_manager.engine as e
        self.assertFalse(any(name in dir(e) for name in ("OrderSend","CTrade","mt5_market_order")))


if __name__ == "__main__": unittest.main()

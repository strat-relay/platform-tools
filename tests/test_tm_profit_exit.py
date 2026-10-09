from datetime import datetime, timedelta, timezone
import unittest

from trade_management.tm_profit_exit import ExitPolicy, evaluate_exit_policy


UTC = timezone.utc
NOW = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)


class ProfitExitPolicyTests(unittest.TestCase):
    def test_buy_pips_uses_bid_and_instrument_pip_size(self):
        action, reasons, params = evaluate_exit_policy(
            trade_state="OPEN", direction="LONG", entry_price=1.1000,
            bid=1.1010, ask=1.1012, risk_distance=0.0020, pip_size=0.0001,
            as_of=NOW, policy=ExitPolicy(profit_target_pips=10))
        self.assertEqual(action, "EXIT")
        self.assertIn("PROFIT_PIPS_TARGET_REACHED", reasons)
        self.assertAlmostEqual(params["observed_profit_pips"], 10)

    def test_sell_r_uses_ask_and_immutable_risk_distance(self):
        action, reasons, params = evaluate_exit_policy(
            trade_state="OPEN", direction="SHORT", entry_price=100.0,
            bid=98.0, ask=98.5, risk_distance=2.0, pip_size=0.1,
            as_of=NOW, policy=ExitPolicy(profit_target_r=0.75))
        self.assertEqual(action, "EXIT")
        self.assertIn("PROFIT_R_TARGET_REACHED", reasons)
        self.assertAlmostEqual(params["observed_profit_r"], 0.75)

    def test_time_exit_precedes_profit_and_records_all_triggered_rules(self):
        action, reasons, params = evaluate_exit_policy(
            trade_state="OPEN", direction="LONG", entry_price=100.0,
            bid=101.0, ask=101.2, risk_distance=1.0, pip_size=0.1,
            as_of=NOW, policy=ExitPolicy(time_exit_at=NOW - timedelta(seconds=1),
                                         profit_target_r=0.5, profit_target_pips=5))
        self.assertEqual(action, "EXIT")
        self.assertEqual(params["exit_reason"], "TIME_EXIT")
        self.assertEqual(params["triggered_rules"], ["TIME_EXIT", "PROFIT_R", "PROFIT_PIPS"])
        self.assertEqual(reasons[0], "TIME_EXIT_DUE")

    def test_net_usd_is_a_safe_candidate_until_execution_boundary_values_it(self):
        action, reasons, params = evaluate_exit_policy(
            trade_state="OPEN", direction="LONG", entry_price=100.0,
            bid=100.1, ask=100.2, risk_distance=1.0, pip_size=0.1,
            as_of=NOW, policy=ExitPolicy(net_profit_target_usd=5))
        self.assertEqual(action, "EXIT")
        self.assertEqual(params["exit_reason"], "NET_PROFIT_USD")
        self.assertTrue(params["net_profit_evaluation_required"])
        self.assertEqual(reasons, ("NET_PROFIT_EVALUATION_REQUIRED",))

    def test_stale_quote_blocks_price_rules_but_not_time_exit(self):
        action, reasons, _ = evaluate_exit_policy(
            trade_state="OPEN", direction="LONG", entry_price=100.0,
            bid=101.0, ask=101.2, risk_distance=1.0, pip_size=0.1,
            as_of=NOW, quote_age_seconds=61,
            policy=ExitPolicy(profit_target_r=0.5))
        self.assertEqual((action, reasons), ("HOLD", ("QUOTE_STALE",)))


if __name__ == "__main__":
    unittest.main()

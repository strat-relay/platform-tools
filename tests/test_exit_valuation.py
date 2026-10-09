import unittest

from execution_v2.exit_valuation import (ExitValuationError, estimate_initial_monetary_risk,
                                         estimate_net_liquidation_profit)


class ExitValuationTests(unittest.TestCase):
    SPEC = {"tick_size": 0.01, "tick_value": 1.0, "point": 0.01, "commission_per_lot": 0.5}
    ACCOUNT = {"currency": "USD"}

    def test_buy_uses_bid_once_and_includes_costs(self):
        value = estimate_net_liquidation_profit(
            direction="LONG", position={"price_open": 100.0, "volume": 2, "swap": -0.25},
            symbol_info=self.SPEC, account_info=self.ACCOUNT, bid=101.0, ask=101.2,
            charged_commission=-1.0)
        self.assertEqual(value.close_price, 101.0)
        self.assertEqual(value.gross_profit, 200.0)
        self.assertEqual(value.estimated_close_commission, 1.0)
        self.assertAlmostEqual(value.estimated_net_profit, 197.75)

    def test_sell_uses_ask(self):
        value = estimate_net_liquidation_profit(
            direction="SHORT", position={"price_open": 100.0, "volume": 1},
            symbol_info={**self.SPEC, "commission_per_lot": 0}, account_info=self.ACCOUNT,
            bid=98.0, ask=98.5, charged_commission=0)
        self.assertEqual(value.close_price, 98.5)
        self.assertEqual(value.gross_profit, 150.0)

    def test_non_usd_account_fails_closed(self):
        with self.assertRaisesRegex(ExitValuationError, "ACCOUNT_CURRENCY_NOT_USD"):
            estimate_net_liquidation_profit(direction="LONG", position={"price_open": 1, "volume": 1},
                                            symbol_info=self.SPEC, account_info={"currency": "EUR"},
                                            bid=2, ask=2.1)

    def test_initial_risk_uses_original_stop_and_costs(self):
        risk = estimate_initial_monetary_risk(
            position={"price_open": 100.0, "volume": 2}, symbol_info=self.SPEC,
            initial_stop=99.0, charged_commission=-1.0, estimated_close_commission=1.0)
        self.assertAlmostEqual(risk, 202.0)


if __name__ == "__main__":
    unittest.main()

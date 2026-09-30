import unittest

from execution_v2.pricing import (REFERENCE_PRICE_SIDE, ExecutionPricingError,
                                  broker_protection_levels, executable_exit_reached)


def metadata(tick=0.01, stops=0, freeze=0, digits=2):
    return {"tick_size": tick, "point": tick, "stops_level": stops,
            "freeze_level": freeze, "raw": {"digits": digits}}


class BrokerPricingTests(unittest.TestCase):
    def test_reference_and_mt5_sides(self):
        self.assertEqual(REFERENCE_PRICE_SIDE, "BID")
        self.assertTrue(executable_exit_reached(direction="LONG", exit_kind="TARGET", canonical_level=100, bid=100, ask=101))
        self.assertTrue(executable_exit_reached(direction="SHORT", exit_kind="TARGET", canonical_level=100, bid=99, ask=100))

    def test_sell_tp_and_sl_add_submission_spread(self):
        p = broker_protection_levels(direction="SHORT", canonical_stop_price=101.0,
                                     canonical_target_price=98.0, broker_symbol="BTCUSDm",
                                     bid=99.0, ask=100.0, metadata=metadata())
        self.assertEqual((p.broker_stop_price, p.broker_target_price), (102.0, 99.0))
        self.assertEqual(p.spread_at_submission, 1.0)

    def test_buy_tp_and_sl_keep_bid_reference_levels(self):
        p = broker_protection_levels(direction="LONG", canonical_stop_price=98.0,
                                     canonical_target_price=102.0, broker_symbol="XAUUSDm",
                                     bid=100.0, ask=101.0, metadata=metadata())
        self.assertEqual((p.broker_stop_price, p.broker_target_price), (98.0, 102.0))

    def test_zero_spread_and_asset_spreads(self):
        zero = broker_protection_levels(direction="LONG", canonical_stop_price=98,
                                        canonical_target_price=102, broker_symbol="EURUSDm",
                                        bid=100, ask=100, metadata=metadata())
        self.assertEqual((zero.broker_stop_price, zero.broker_target_price), (98, 102))
        for symbol, spread in (("BTCUSDm", 12.0), ("XAUUSDm", 0.8), ("EURUSDm", 0.0002)):
            p = broker_protection_levels(direction="SHORT", canonical_stop_price=110,
                                         canonical_target_price=90, broker_symbol=symbol,
                                         bid=100, ask=100 + spread,
                                         metadata=metadata(tick=0.0001 if symbol == "EURUSDm" else 0.01,
                                                          digits=4 if symbol == "EURUSDm" else 2))
            self.assertAlmostEqual(p.broker_stop_price - p.canonical_stop_price, spread, places=4)

    def test_tick_rounding_is_conservative(self):
        p = broker_protection_levels(direction="SHORT", canonical_stop_price=101.001,
                                     canonical_target_price=98.001, broker_symbol="EURUSDm",
                                     bid=99.0, ask=99.0007,
                                     metadata=metadata(tick=0.001, digits=3))
        self.assertEqual(p.broker_stop_price, 101.002)
        self.assertEqual(p.broker_target_price, 98.001)

    def test_stops_level_and_freeze_level_fail_closed(self):
        with self.assertRaisesRegex(ExecutionPricingError, "BROKER_STOP_DISTANCE"):
            broker_protection_levels(direction="LONG", canonical_stop_price=99.5,
                                     canonical_target_price=100.5, broker_symbol="XAUUSDm",
                                     bid=100.0, ask=100.1,
                                     metadata=metadata(tick=0.01, stops=60, freeze=0))

    def test_canonical_values_are_retained(self):
        p = broker_protection_levels(direction="SHORT", canonical_stop_price=101,
                                     canonical_target_price=98, broker_symbol="BTCUSDm",
                                     bid=99, ask=100, metadata=metadata())
        self.assertEqual((p.canonical_stop_price, p.canonical_target_price), (101, 98))
        self.assertNotEqual(p.broker_target_price, p.canonical_target_price)

    def test_dynamic_spread_is_submission_evidence_only(self):
        first = broker_protection_levels(direction="SHORT", canonical_stop_price=101,
                                         canonical_target_price=98, broker_symbol="EURUSDm",
                                         bid=99.9, ask=100.0, metadata=metadata())
        second = broker_protection_levels(direction="SHORT", canonical_stop_price=101,
                                          canonical_target_price=98, broker_symbol="EURUSDm",
                                          bid=99.0, ask=100.5, metadata=metadata())
        self.assertEqual(first.canonical_target_price, second.canonical_target_price)
        self.assertNotEqual(first.broker_target_price, second.broker_target_price)

    def test_btc_short_regression_reference_touch_is_not_broker_tp(self):
        target = 82985.31
        # Bid touched the canonical target, but the Ask is still above the
        # SELL TP trigger after a 12-dollar BTC spread.
        self.assertFalse(executable_exit_reached(direction="SHORT", exit_kind="TARGET",
                                                 canonical_level=target, bid=target, ask=target + 12.0))
        self.assertTrue(executable_exit_reached(direction="SHORT", exit_kind="TARGET",
                                                canonical_level=target, bid=target - 12.1,
                                                ask=target - 0.1))


if __name__ == "__main__":
    unittest.main()

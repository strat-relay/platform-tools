import unittest
from orchestration.tradeability import evaluate


def sig(direction="LONG", entry=100.0, stop=99.0, target=102.0):
    return {"direction": direction, "entry_price": entry, "stop_price": stop, "target_price": target}


META = {"point": 0.01, "tick_size": 0.01, "stops_level": 10, "freeze_level": 0,
        "volume_min": 0.01, "volume_step": 0.01}


class TradeabilityTests(unittest.TestCase):
    def test_long_and_short_valid_geometry(self):
        self.assertEqual(evaluate(sig(), {"bid": 100.0, "ask": 100.01}, META).status, "TRADEABLE")
        self.assertEqual(evaluate(sig("SHORT", 100, 101, 98), {"bid": 99.99, "ask": 100.0}, META).status, "TRADEABLE")

    def test_consumed_and_wrong_sides(self):
        self.assertEqual(evaluate(sig(target=100.05), {"bid": 100.0, "ask": 100.06}, META).rejection_reason, "TARGET_ALREADY_CONSUMED")
        self.assertEqual(evaluate(sig(stop=100.1), {"bid": 100.0, "ask": 100.01}, META).rejection_reason, "STOP_WRONG_SIDE_OF_MARKET")
        self.assertEqual(evaluate(sig(target=98.0), {"bid": 99.0, "ask": 99.01}, META).rejection_reason, "TARGET_WRONG_SIDE_OF_MARKET")

    def test_broker_distance_and_alignment(self):
        self.assertEqual(evaluate(sig(stop=100.02), {"bid": 100, "ask": 100.01}, META).rejection_reason, "STOP_WRONG_SIDE_OF_MARKET")
        self.assertEqual(evaluate(sig(target=100.05), {"bid": 100, "ask": 100.01}, META).rejection_reason, "BROKER_TARGET_TOO_CLOSE")
        self.assertEqual(evaluate(sig(target=102.005), {"bid": 100, "ask": 100.01}, META).rejection_reason, "INVALID_TICK_ALIGNMENT")

    def test_spread_dominates_remaining_target(self):
        result = evaluate(sig(target=100.5), {"bid": 99.0, "ask": 100.01}, META)
        self.assertEqual(result.rejection_reason, "SPREAD_DOMINATES_TARGET")

    def test_original_rr_is_independent_of_current_quote(self):
        result = evaluate(sig(entry=81261.11, stop=81167.49, target=81271.94), {"bid": 81260, "ask": 81260.5}, META)
        self.assertAlmostEqual(result.original_rr, 10.83 / 93.62, places=3)

    def test_interim_effective_rr_floor_boundaries(self):
        for target, expected in ((100.24, False), (100.25, True), (100.26, True)):
            result = evaluate(sig(target=target), {"bid": 99.99, "ask": 100.0}, META, policy_min_rr=0.25)
            self.assertEqual(result.status == "TRADEABLE", expected)

    def test_signal_rr_and_effective_rr_are_separate(self):
        # The signal's theoretical RR is above the floor, but executable ask
        # worsens it below the floor.
        result = evaluate(sig(entry=100.0, stop=98.0, target=100.50),
                          {"bid": 100.0, "ask": 100.01}, META, policy_min_rr=0.25)
        self.assertEqual(result.rejection_reason, "EFFECTIVE_RR_BELOW_MINIMUM")

    def test_low_signal_rr_does_not_override_effective_rr(self):
        result = evaluate(sig(entry=100.0, stop=99.0, target=100.20),
                          {"bid": 99.99, "ask": 100.01}, META, policy_min_rr=0.25)
        self.assertEqual(result.status, "RR_BELOW_POLICY")


if __name__ == "__main__":
    unittest.main()

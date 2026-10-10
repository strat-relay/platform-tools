import unittest

from scripts.audit_outcome_parity import contract_gap


class ProspectiveParityTests(unittest.TestCase):
    def test_prospective_requires_complete_versioned_contract(self):
        self.assertEqual(contract_gap({"strategy_metadata": {}}),
                         ["strategy_metadata.outcome_contract"])
        self.assertIn("price_basis", contract_gap({"strategy_metadata": {
            "outcome_contract": {"version": "entry-outcome.v2"}}}))

    def test_supported_contract_is_admitted(self):
        self.assertEqual(contract_gap({"strategy_metadata": {"outcome_contract": {
            "version": "entry-outcome.v2", "timeframe_minutes": 15,
            "activation": "SIGNAL_TIMESTAMP", "time_exit_price": "CLOSE",
            "price_basis": "THEORETICAL_TOUCH"}}}), [])


if __name__ == "__main__":
    unittest.main()

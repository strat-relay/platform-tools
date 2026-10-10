import unittest

from postgres.db import checksum_is_accepted


class MigrationChecksumCompatibilityTests(unittest.TestCase):
    def test_known_035_legacy_checksum_is_accepted_without_rewriting_history(self):
        self.assertTrue(checksum_is_accepted(
            "035_manual_signal_invalidation.sql",
            "d647e5c4c3683fb36c233a762cb7ccf6c2eaef6dd797f601e068cb2de0ca5c86",
            "289cc3f6eac208480bb962cabdd2ffea079db00049bea217dba83c47a4498bcc",
        ))

    def test_known_047_legacy_checksum_is_accepted_without_rewriting_history(self):
        self.assertTrue(checksum_is_accepted(
            "047_kojo_v3_strategy_registration.sql",
            "f9457146fffe72b4083411558e5210f01b99046dc6c9b04faafe6807fdbb72ce",
            "4822aaf56cc7ff2ccff9a73bb8296989399751e6212839cbe146b042a34d2f82",
        ))

    def test_unknown_checksum_drift_is_still_rejected(self):
        self.assertFalse(checksum_is_accepted(
            "035_manual_signal_invalidation.sql", "unknown", "289cc3f6eac208480bb962cabdd2ffea079db00049bea217dba83c47a4498bcc"))
        self.assertFalse(checksum_is_accepted("034_strategy_parameter_manifest.sql", "legacy", "current"))


if __name__ == "__main__":
    unittest.main()

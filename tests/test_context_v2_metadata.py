import os
import unittest
from unittest.mock import patch

from context_structure_retrace_v2 import research_metadata


class ContextV2MetadataTests(unittest.TestCase):
    def test_research_metadata_pins_identity_and_never_routes_to_broker(self):
        with patch.dict(os.environ, {"SOURCE_COMMIT": "test-commit"}, clear=False):
            self.assertEqual(research_metadata(), {
                "strategy_id": "CONTEXT_STRUCTURE_RETRACE_V2",
                "strategy_version": "V2",
                "contract_hash": "4430542fb8d249d6338ead1fb745664a2e44836c4123e16f069b0c48bd69e107",
                "parameter_hash": "ad897dff76e5b119d52fe7f05203d25d214b61c1dff69be18b9e8f784284335f",
                "source_commit": "test-commit",
                "lifecycle": "RESEARCH_ONLY",
                "broker_writes": False,
            })


if __name__ == "__main__":
    unittest.main()

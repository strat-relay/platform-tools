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
                "parameter_hash": "dc72c5d03e547fc02e1c80c91fb244e2b153a0bd32a8b9a8f3df200c71a61394",
                "source_commit": "test-commit",
                "lifecycle": "RESEARCH_ONLY",
                "broker_writes": False,
            })


if __name__ == "__main__":
    unittest.main()

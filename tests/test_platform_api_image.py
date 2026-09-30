from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class PlatformApiImageTests(unittest.TestCase):
    def test_api_image_carries_import_time_outcome_attribution_dependency(self):
        dockerfile = (ROOT / "deploy/platform_api/Dockerfile").read_text(encoding="utf-8")
        self.assertIn("COPY outcome_attribution.py ./outcome_attribution.py", dockerfile)


if __name__ == "__main__":
    unittest.main()

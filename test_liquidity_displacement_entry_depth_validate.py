import unittest
from pathlib import Path


class EntryDepthValidationTests(unittest.TestCase):
    def test_validator_exists_and_is_separate(self):
        self.assertTrue(Path("liquidity_displacement_entry_depth_validate.py").exists())

    def test_spec_is_present(self):
        text=Path("liquidity_displacement_entry_depth_validate.py").read_text()
        self.assertIn('DEPTHS={"25":.25,"33":1/3,"50":.5}', text)
        self.assertIn('max_retrace_candles=5', text)
        self.assertIn('TARGET=1.25', text)


if __name__=="__main__": unittest.main()

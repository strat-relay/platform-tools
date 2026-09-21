import unittest
from pathlib import Path


class OOSAuditTests(unittest.TestCase):
    def test_oos_validator_uses_fixed_variants(self):
        text=Path("liquidity_displacement_oos_validate.py").read_text()
        self.assertIn('"USDJPYm_25":("USDJPYm",.25)', text)
        self.assertIn('"XAUUSDm_33":("XAUUSDm",1/3)', text)
        self.assertIn('split=start+((end-start)*2)//3', text)
        self.assertIn('TARGET=1.25', text)


if __name__=="__main__": unittest.main()

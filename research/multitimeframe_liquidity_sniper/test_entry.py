import unittest
from .entry import evaluate_m5_entry, UnsupportedResearchFamily, FAMILIES

def b(t,o,h,l,c,s=8): return {"time":t,"open":o,"high":h,"low":l,"close":c,"spread":s}

class EntryTests(unittest.TestCase):
    def setup(self, direction="LONG"):
        return {"direction":direction,"bos_choch_timestamp":0,"m15_displacement_high":110,"m15_displacement_low":100}

    def test_close_hold_long_short(self):
        for d in ("LONG","SHORT"):
            level=108 if d=="LONG" else 102
            rows=[b(300,109,110,level-1,level+1),b(600,level,level+1,level-1,level+1)] if d=="LONG" else [b(300,101,103,100,level-1),b(600,level,level+1,level-1,level-1)]
            x=evaluate_m5_entry("EURUSDm",rows,self.setup(d),{"family":"TOUCH_CLOSE_HOLD","depth":.2,"expiration":2,"stop_anchor":"M5_SWEEP_EXTREME"})
            self.assertIsNotNone(x)

    def test_next_candle_does_not_skip_failed_touch(self):
        s=self.setup("LONG"); rows=[b(300,109,110,107,109),b(600,108,109,108.5,107),b(900,107,109,108.5,109)]
        x=evaluate_m5_entry("EURUSDm",rows,s,{"family":"TOUCH_NEXT_CANDLE_HOLD","depth":.2,"expiration":3,"stop_anchor":"M5_SWEEP_EXTREME"})
        self.assertIsNone(x)

    def test_unsupported_family_is_explicit(self):
        with self.assertRaises(UnsupportedResearchFamily): evaluate_m5_entry("EURUSDm",[],self.setup(),{"family":"NOT_A_FAMILY"})

    def test_every_declared_family_has_explicit_long_and_short_path(self):
        for family in FAMILIES:
            for direction in ("LONG", "SHORT"):
                s=self.setup(direction); s["bos_choch_timestamp"]=300
                if family == "PSYCHOLOGICAL_LEVEL_RECLAIM": s["psychological_level"] = 108 if direction == "LONG" else 102
                if family == "MICRO_BOS_CHOCH":
                    rows=[b(300,100,101,99,100), b(600,100,110,99,110) if direction=="LONG" else b(600,100,101,90,90)]
                elif family == "M5_LIQUIDITY_SWEEP_RECLAIM":
                    rows=[b(300,100,101,99,100), b(600,100,109,98,109) if direction=="LONG" else b(600,101,103,99,99)]
                else:
                    level=108 if direction=="LONG" else 102
                    rows=[b(300,100,101,99,100), b(600,level,level+1,level-1,level+1) if direction=="LONG" else b(600,level,level+1,level-1,level-1)]
                    if family == "TOUCH_NEXT_CANDLE_HOLD":
                        rows.append(b(900,level,level+1,level-1,level+1) if direction=="LONG" else b(900,level,level+1,level-1,level-1))
                params={"family":family,"depth":.2,"expiration":2,"stop_anchor":"M5_SWEEP_EXTREME"}
                result=evaluate_m5_entry("EURUSDm",rows,s,params)
                self.assertIsNotNone(result, family + " " + direction)

if __name__=="__main__": unittest.main()

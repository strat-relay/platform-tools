import unittest
from .state_machine import detect_m15_setups

def bar(t,o,h,l,c): return {"time":t,"open":o,"high":h,"low":l,"close":c}

class M15StateMachineTests(unittest.TestCase):
    def base(self, direction="LONG", bos_delay=2):
        rows=[]
        for i in range(12): rows.append(bar(i*900,100+i*.1,100+i*.1+.2,100+i*.1-.2,100+i*.1+.1))
        # Sweep/reclaim candle, then independent displacement and structure confirmation.
        if direction=="LONG":
            rows += [bar(10800,101,101.1,98,100.8), bar(11700,100.8,102.0,100.7,101.8), bar(12600,101.8,102.1,101.7,102.05)]
        else:
            rows += [bar(10800,101,104,100.9,101.2), bar(11700,101.2,101.3,99.0,99.2), bar(12600,99.2,99.3,98.8,98.95)]
        return rows

    def test_same_candle_displacement_bos(self):
        out=detect_m15_setups(self.base(),{"liquidity_lookback":5,"sweep_depth_atr":0,"reclaim_delay":1,"displacement_body_atr":.2,"displacement_window":2,"bos_delay":0,"structure_lookback":5})
        self.assertTrue(any(x["status"]=="SETUP_ACTIVE" for x in out))

    def test_later_bos_need_not_be_displacement(self):
        rows=self.base(); rows[-1]=bar(12600,101.9,102.2,101.8,102.1)
        out=detect_m15_setups(rows,{"liquidity_lookback":5,"sweep_depth_atr":0,"reclaim_delay":1,"displacement_body_atr":.2,"displacement_window":2,"bos_delay":2,"structure_lookback":5})
        self.assertTrue(any(x["status"]=="SETUP_ACTIVE" for x in out))

    def test_short_mirror(self):
        out=detect_m15_setups(self.base("SHORT"),{"liquidity_lookback":5,"sweep_depth_atr":0,"reclaim_delay":1,"displacement_body_atr":.2,"displacement_window":2,"bos_delay":2,"structure_lookback":5})
        self.assertTrue(any(x["status"]=="SETUP_ACTIVE" and x["direction"]=="SHORT" for x in out))

    def test_expiration_is_explicit(self):
        rows=self.base(); rows[12]=bar(10800,101,101.1,98,99)
        out=detect_m15_setups(rows,{"liquidity_lookback":5,"sweep_depth_atr":0,"reclaim_delay":1,"displacement_body_atr":.5,"displacement_window":1,"bos_delay":1,"structure_lookback":5})
        self.assertTrue(any(x["status"] in {"EXPIRED","INVALIDATED"} for x in out))

if __name__=="__main__": unittest.main()

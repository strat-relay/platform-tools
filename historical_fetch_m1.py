import json, sys
import MetaTrader5 as mt5

symbol = sys.argv[1] if len(sys.argv) > 1 else "XAUUSDm"
if not mt5.initialize(path=r"C:\Program Files\MetaTrader 5\terminal64.exe"):
    raise SystemExit(f"MT5 initialize failed: {mt5.last_error()}")
if not mt5.symbol_select(symbol, True):
    raise SystemExit(f"symbol_select failed: {mt5.last_error()}")
rates = []
for pos in (0, 50000, 100000, 150000, 200000, 250000, 300000):
    chunk = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M1, pos, 50000)
    if chunk is None:
        if pos == 0: raise SystemExit(f"copy_rates failed: {mt5.last_error()}")
        break
    rates.extend(chunk)
    if len(chunk) < 50000: break
rows = {int(x["time"]): {"time": int(x["time"]), "open": float(x["open"]), "high": float(x["high"]), "low": float(x["low"]), "close": float(x["close"]), "spread": int(x["spread"])} for x in rates}
print(json.dumps(list(rows.values()), separators=(",", ":")))

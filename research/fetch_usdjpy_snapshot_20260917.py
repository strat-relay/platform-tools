"""Research-only historical snapshot fetch; no broker writes."""
import json
from datetime import datetime, timezone
import MetaTrader5 as mt5

SYMBOL = "USDJPYm"
END = datetime(2026, 9, 17, 8, 55, tzinfo=timezone.utc)
if not mt5.initialize(path=r"C:\Program Files\MetaTrader 5\terminal64.exe"):
    raise SystemExit(f"MT5 initialize failed: {mt5.last_error()}")
if not mt5.symbol_select(SYMBOL, True):
    raise SystemExit(f"symbol_select failed: {mt5.last_error()}")
info = mt5.symbol_info(SYMBOL)
out = {"symbol": SYMBOL, "snapshot_end": END.isoformat(), "contract": {
    "tick_size": float(info.trade_tick_size), "tick_value": float(info.trade_tick_value),
    "min_lot": float(info.volume_min), "max_lot": float(info.volume_max), "lot_step": float(info.volume_step),
    "point": float(info.point), "digits": int(info.digits), "stops_level": int(info.trade_stops_level),
    "contract_size": float(info.trade_contract_size)}}
for name, tf, count in [("M5", mt5.TIMEFRAME_M5, 60000), ("M15", mt5.TIMEFRAME_M15, 30000), ("H1", mt5.TIMEFRAME_H1, 15000)]:
    rates = mt5.copy_rates_from(SYMBOL, tf, END, count)
    if rates is None: raise SystemExit(f"copy_rates failed {name}: {mt5.last_error()}")
    out[name] = [{"time": int(x["time"]), "open": float(x["open"]), "high": float(x["high"]), "low": float(x["low"]),
                  "close": float(x["close"]), "tick_volume": int(x["tick_volume"]), "spread": int(x["spread"]),
                  "real_volume": int(x["real_volume"])} for x in rates]
print(json.dumps(out, separators=(",", ":")))

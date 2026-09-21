"""Read-only MT5 snapshot fetch for pair-agnostic FX research."""
import json, sys
from datetime import datetime, timezone
import MetaTrader5 as mt5

SYMBOLS = ["EURUSDm","GBPUSDm","USDJPYm","USDCHFm","USDCADm","AUDUSDm","NZDUSDm",
           "EURJPYm","GBPJPYm","EURGBPm","AUDJPYm","CADJPYm","CHFJPYm","GBPAUDm","GBPCADm"]
END = datetime(2026, 9, 17, 8, 55, tzinfo=timezone.utc)
if not mt5.initialize(path=r"C:\Program Files\MetaTrader 5\terminal64.exe"):
    raise SystemExit(f"MT5 initialize failed: {mt5.last_error()}")
out = {"snapshot_end": END.isoformat(), "requested_symbols": SYMBOLS, "symbols": {}}
for symbol in SYMBOLS:
    if not mt5.symbol_select(symbol, True):
        out["symbols"][symbol] = {"available": False, "reason": f"symbol_select: {mt5.last_error()}"}; continue
    info = mt5.symbol_info(symbol)
    if info is None:
        out["symbols"][symbol] = {"available": False, "reason": "symbol_info unavailable"}; continue
    item = {"available": True, "symbol": symbol, "contract": {
        "tick_size": float(info.trade_tick_size), "tick_value": float(info.trade_tick_value),
        "point": float(info.point), "digits": int(info.digits), "stops_level": int(info.trade_stops_level),
        "contract_size": float(info.trade_contract_size), "volume_min": float(info.volume_min), "volume_step": float(info.volume_step)}}
    for name, tf, count in [("M5", mt5.TIMEFRAME_M5, 60000), ("M15", mt5.TIMEFRAME_M15, 30000), ("H1", mt5.TIMEFRAME_H1, 15000)]:
        rates = mt5.copy_rates_from(symbol, tf, END, count)
        if rates is None or len(rates) == 0:
            item["available"] = False; item["reason"] = f"{name}: {mt5.last_error()}"; break
        item[name] = [{"time": int(x["time"]), "open": float(x["open"]), "high": float(x["high"]), "low": float(x["low"]),
                       "close": float(x["close"]), "tick_volume": int(x["tick_volume"]), "spread": int(x["spread"]),
                       "real_volume": int(x["real_volume"])} for x in rates]
    out["symbols"][symbol] = item
print(json.dumps(out, separators=(",", ":")))

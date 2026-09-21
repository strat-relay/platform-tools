"""Read-only MT5 history fetcher for Phase 1 context research.

This is intentionally separate from the existing strategy fetchers and adds
H4 while retaining M1/M5/M15/H1 data for causal reconstruction.
"""
import json
import sys
from datetime import datetime, timedelta, timezone

import MetaTrader5 as mt5


symbol = sys.argv[1] if len(sys.argv) > 1 else "XAUUSDm"
if not mt5.initialize(path=r"C:\Program Files\MetaTrader 5\terminal64.exe"):
    raise SystemExit(f"MT5 initialize failed: {mt5.last_error()}")
if not mt5.symbol_select(symbol, True):
    raise SystemExit(f"symbol_select failed for {symbol}: {mt5.last_error()}")
info = mt5.symbol_info(symbol)
out = {
    "symbol": symbol,
    "contract": {
        "tick_size": float(info.trade_tick_size), "tick_value": float(info.trade_tick_value),
        "min_lot": float(info.volume_min), "max_lot": float(info.volume_max), "lot_step": float(info.volume_step),
        "point": float(info.point), "digits": int(info.digits), "stops_level": int(info.trade_stops_level),
        "contract_size": float(info.trade_contract_size),
    },
}
for tf_name, tf, count in [("M5", mt5.TIMEFRAME_M5, 60000), ("M15", mt5.TIMEFRAME_M15, 30000), ("H1", mt5.TIMEFRAME_H1, 15000), ("H4", mt5.TIMEFRAME_H4, 5000)]:
    rates = mt5.copy_rates_from_pos(symbol, tf, 0, count)
    if rates is None:
        raise SystemExit(f"copy_rates failed for {symbol} {tf_name}: {mt5.last_error()}")
    out[tf_name] = [{"time": int(x["time"]), "open": float(x["open"]), "high": float(x["high"]), "low": float(x["low"]), "close": float(x["close"]), "spread": int(x["spread"]), "tick_volume": int(x["tick_volume"])} for x in rates]

now = datetime.now(timezone.utc)
m1_chunks = []
for offset in range(0, 190, 30):
    end = now - timedelta(days=offset)
    start = now - timedelta(days=min(offset + 30, 190))
    rates = mt5.copy_rates_range(symbol, mt5.TIMEFRAME_M1, start, end)
    if rates is not None:
        m1_chunks.extend(rates.tolist())
seen = {int(x[0]): x for x in m1_chunks}
out["M1"] = [{"time": int(x[0]), "open": float(x[1]), "high": float(x[2]), "low": float(x[3]), "close": float(x[4]), "spread": int(x[6]), "tick_volume": int(x[5])} for x in sorted(seen.values(), key=lambda x: int(x[0]))]
print(json.dumps(out, separators=(",", ":")))

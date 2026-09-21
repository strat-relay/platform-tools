import json
import MetaTrader5 as mt5

symbol = "BTCUSDm"
if not mt5.initialize(path=r"C:\Program Files\MetaTrader 5\terminal64.exe"):
    raise SystemExit(f"MT5 initialize failed: {mt5.last_error()}")
if not mt5.symbol_select(symbol, True):
    raise SystemExit(f"symbol_select failed: {mt5.last_error()}")
info = mt5.symbol_info(symbol)
account = mt5.account_info()
tick = mt5.symbol_info_tick(symbol)
out = {
    "symbol": symbol,
    "contract": {"tick_size": float(info.trade_tick_size), "tick_value": float(info.trade_tick_value), "min_lot": float(info.volume_min), "max_lot": float(info.volume_max), "lot_step": float(info.volume_step), "point": float(info.point), "digits": int(info.digits), "stops_level": int(info.trade_stops_level), "contract_size": float(info.trade_contract_size)},
    "quote": {"bid": float(tick.bid), "ask": float(tick.ask), "time": int(tick.time)} if tick else None,
    "account": {"equity": float(account.equity) if account else None, "balance": float(account.balance) if account else None},
}
for tf_name, tf, count in [("M5", mt5.TIMEFRAME_M5, 60000), ("M15", mt5.TIMEFRAME_M15, 30000), ("H1", mt5.TIMEFRAME_H1, 15000)]:
    rates = mt5.copy_rates_from_pos(symbol, tf, 0, count)
    if rates is None:
        raise SystemExit(f"copy_rates failed for {tf_name}: {mt5.last_error()}")
    out[tf_name] = [{"time": int(x["time"]), "open": float(x["open"]), "high": float(x["high"]), "low": float(x["low"]), "close": float(x["close"]), "tick_volume": int(x["tick_volume"]), "spread": int(x["spread"]), "real_volume": int(x["real_volume"])} for x in rates]
print(json.dumps(out, separators=(",", ":")))

"""Run the paper-only strategy against the local MT5 bridge."""
from __future__ import annotations

import argparse
import time

from contracts.mt5_bridge import Mt5ReadClient
from paper_engine import PaperEngine, StrategyConfig, StrategyEngine


def call_bridge(url: str, name: str, arguments: dict) -> dict:
    return Mt5ReadClient(url, timeout_s=40).call(name, arguments)


def run_once(args: argparse.Namespace, strategy: StrategyEngine, paper: PaperEngine, last_candle: int | None) -> int:
    base = args.mcp_url
    account = call_bridge(base, "mt5_account_info", {})
    contract = call_bridge(base, "mt5_symbol_info", {"symbol": args.symbol})
    quote = call_bridge(base, "mt5_quote", {"symbol": args.symbol})
    m5 = call_bridge(base, "mt5_rates", {"symbol": args.symbol, "timeframe": "M5", "limit": args.limit})
    m15 = call_bridge(base, "mt5_rates", {"symbol": args.symbol, "timeframe": "M15", "limit": args.limit})
    h1 = call_bridge(base, "mt5_rates", {"symbol": args.symbol, "timeframe": "H1", "limit": args.limit})
    completed_m5 = m5["rates"][:-1]
    completed_m15 = m15["rates"][:-1]
    completed_h1 = h1["rates"][:-1]
    if not completed_m5:
        print(json.dumps({"decision": "NO_TRADE", "reason": "no_completed_candle"}))
        return last_candle or 0
    candle_time = int(completed_m5[-1]["time"])
    if candle_time == last_candle:
        return last_candle
    paper.process_candle(completed_m5[-1], time.time())
    signal = strategy.evaluate(completed_h1, completed_m15, completed_m5, quote)
    result = paper.evaluate(signal, float(account["equity"]), {"tick_size": contract["tick_size"], "tick_value": contract["tick_value"], "min_lot": contract["min_lot"], "max_lot": contract["max_lot"], "lot_step": contract["lot_step"]}, quote)
    result["candle_time"] = candle_time
    print(json.dumps(result, separators=(",", ":")))
    return candle_time


def main() -> None:
    parser = argparse.ArgumentParser(description="MT5 paper-only strategy runner")
    parser.add_argument("--mcp-url", default="http://127.0.0.1:22347/mcp")
    parser.add_argument("--symbol", default="XAUUSDm")
    parser.add_argument("--limit", type=int, default=120)
    parser.add_argument("--interval", type=int, default=15)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--enable-paper", action="store_true", help="enable simulated entries; never enables live trading")
    parser.add_argument("--audit", default="paper_audit.jsonl")
    args = parser.parse_args()
    config = StrategyConfig(symbol=args.symbol, engine_enabled=args.enable_paper)
    strategy = StrategyEngine(config)
    paper = PaperEngine(config, args.audit)
    last_candle = None
    while True:
        try:
            last_candle = run_once(args, strategy, paper, last_candle)
        except Exception as exc:
            print(json.dumps({"decision": "NO_TRADE", "reason": "runner_error", "error": str(exc)}))
        if args.once:
            break
        time.sleep(max(1, args.interval))


if __name__ == "__main__":
    main()

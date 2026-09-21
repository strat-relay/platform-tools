# Context V1 Demo Execution

`DEMO_EXECUTION` is an explicit, fail-closed mode for
`CONTEXT_STRUCTURE_RETRACE_V1`. It is bound to
`5056045369@MetaQuotes-Demo`; there is no generic real-execution mode.

The execution endpoint is configured separately from the research bridge at
`127.0.0.1:22348`. Until that dedicated transport is verified and cross-feed
geometry is approved, `demo-arm` refuses to arm.

The execution layer uses a virtual `$200` strategy bankroll and 1% risk,
independent of the broker's approximately `$100,000` equity. Volume is floored
to the broker step and trades below the broker minimum are skipped.

Commands:

```text
python3 live_execution_consumer.py demo-status
python3 live_execution_consumer.py demo-audit
python3 live_execution_consumer.py demo-arm
python3 live_execution_consumer.py demo-disarm
python3 live_execution_consumer.py trades
```

The current mappings are intentionally `UNSAFE` pending cross-feed validation:

| Strategy symbol | Demo symbol | Status |
|---|---|---|
| XAUUSDm | XAUUSD | UNSAFE |
| USDJPYm | USDJPY | UNSAFE |
| EURUSDm | EURUSD | UNSAFE |
| BTCUSDm | — | UNAVAILABLE |

No existing signal is replayed. Only a new signal after the durable arm
timestamp can create a demo execution intent.

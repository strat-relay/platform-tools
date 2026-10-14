# Price Action V1 forward observation

This artifact is frozen but not activated. It defines a read-only, paper/theoretical observer for the frozen Support/Resistance Retest V1 and Engulfing Continuation V1 evaluators.

## Commands

```sh
PYTHONPATH=. python3 -m research.price_action_forward_observer freeze
PYTHONPATH=. python3 -m research.price_action_forward_observer audit-order-isolation
PYTHONPATH=. python3 -m research.price_action_forward_observer health
PYTHONPATH=. python3 -m research.price_action_forward_observer report
PYTHONPATH=. python3 -m research.price_action_forward_observer stop
```

The observer may only be started after the read-only bridge health check reports a fixed healthy universe and a human verifies the resulting manifest. This checkout did not start it because the bridge health probe failed closed.

The first snapshot is warm-up/reconstructed state only. Subsequent completed H1/M15 snapshots are processed causally. Entries are theoretical next-open observations and are stored locally under the state directory; no signal publication, outbox, account, order, position, or execution path is called.

## Current readiness

- Manifest: `forward_manifest.json`
- Status: `FROZEN_NOT_ACTIVATED`
- T0: `2026-10-14T04:38:01.680419Z`
- Healthy universe at preflight: none
- Bridge evidence: `BridgeTransportError: [Errno 61] Connection refused` from `http://127.0.0.1:22347/mcp`
- Activation: blocked; no first live candle or live evaluation exists
- Historical baselines remain unchanged and are not mixed with this observer

If a future run is activated after a successful health gate, stop only the dedicated observer process and issue the scoped `stop --state-dir docs/research/price_action_forward/state` command. Do not use any existing production runner stop command.

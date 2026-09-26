"""The REAL, non-simulated bridge-side OD-06 fence boundary (mission
`CLAUDE-STRATRELAY-V2-EXECUTION-AUDIT-REMEDIATION`, remediating the prior audit finding that
production execution wiring used `execution_v2.bridge_fence_sim.BridgeFenceSimulator` - an
in-process, in-memory, per-process test double - as if it were bridge-side enforcement).

## What "real" means here, precisely

`boundary.py::RealBridgeFenceBoundary` implements the exact same independent-verification
contract `bridge_fence_sim.py` proved in isolation (signature, account binding, monotonic
generation, expiry, request-fingerprint, idempotency), but backed by `store.py`'s durable SQLite
persistence instead of an in-memory dict - so ownership-generation state and the idempotency
ledger both survive a process restart, which is exactly what the audit's section 5/6 required
("do not rely solely on PostgreSQL lookup at request time... prove restart behavior... do not use
in-memory dictionaries as authoritative protection"). `http_server.py` exposes this boundary over
a real HTTP transport (stdlib `http.server` only, no new dependency) so the platform-side runtime
can reach it as a genuinely separate, network-addressable service - the same shape the actual
`mt5-native-bridge` process would expose once this code is ported there.

## What is still NOT here

The actual MT5 order-send primitive (the `broker_call` parameter every `submit()` still takes) is
never implemented in this package - mission constraints forbid starting port 22348 or sending any
live MT5 order. Every isolated proof in `tests/test_mt5_bridge_fence*.py` and
`tests/test_execution_v2_real_bridge.py` injects a fake/counting `broker_call`; only that one
final call is ever mocked. Everything before it - signature verification, account binding,
generation/epoch enforcement, expiry, fingerprint binding, and durable idempotency - is real,
production-intended code, exercised through the real HTTP transport in the E2E proof, not a
simulator.

This package has never been deployed, never binds to port 22348, and is never reachable from
`execution_v2/runtime/*` other than through `execution_v2/runtime/bridge_client.py`'s HTTP client,
which itself fails closed if no bridge endpoint is configured
(`tests/test_execution_v2_runtime.py`).
"""

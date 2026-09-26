"""V2 personal execution (architecture/v2-execution, remediated by
CLAUDE-STRATRELAY-V2-EXECUTION-AUDIT-REMEDIATION): the first production-capable path from a
canonical EntrySignal to one broker-held-fenced execution attempt. Caleb's personal execution
only - no customer auto-trading, no Trade Manager execution effects.

Lifecycle: EntrySignal -> ExecutionEligibility -> ExecutionIntent -> risk validation ->
ownership/generation acquisition (PostgreSQL, reused unmodified from platform.acquire_ownership)
-> broker-held fence issuance (fence.py) and INDEPENDENT bridge-side validation -> submission ->
ExecutionResult -> canonical PostgreSQL persistence + JetStream event
(execution.intent.created.v1 / execution.result.recorded.v1, both pre-existing subjects on the
EXECUTION stream - nothing added to the messaging contract).

Fails closed by default: nothing in this package can produce a broker effect unless
EXECUTION_AUTHORITY_MODE=ENABLED is read explicitly from the environment (execution_v2.runtime.config).
No code path infers permission to trade from an EntrySignal's existence, a ManagedTrade's
existence, an ExecutionIntent's existence, a PostgreSQL lease, or bridge reachability alone.

This package imports nothing from trade_management/ (P4.2) and does not require or read any
TradeManagerDecision/ManagementSignal/HOLD/WITHHELD state - EntrySignal -> ExecutionIntent is
independent of EntrySignal -> ManagedTrade -> Observation -> TM-NONE (mission section 11).
It imports nothing from the legacy `execution` package (file-IPC based) or `trade_manager`
(legacy). Enforced by tests/test_execution_v2_isolation.py.

## Three distinct levels of bridge-fence proof (do not conflate these - the prior audit found
## documentation that implied the first was equivalent to the third, which it is not)

1. **Unit/simulator proof** (`bridge_fence_sim.py`, `tests/test_execution_v2_fence.py`):
   an in-process, in-memory, TEST-ONLY re-implementation of the OD-06 protocol, useful for fast,
   dependency-free tests of the *protocol logic* itself. It is never imported by any production
   module (`execution_v2/runtime/*.py`, `mt5_bridge_fence/*.py`) - enforced by
   `tests/test_execution_v2_isolation.py::test_no_production_module_imports_the_test_only_simulator`.
   It cannot prove durability, restart behavior, or a real network boundary, and never claimed to.

2. **Real bridge code, isolated proof** (`mt5_bridge_fence/` - `boundary.py` + `store.py` +
   `http_server.py`, exercised over a real HTTP transport in
   `tests/test_execution_v2_real_bridge.py`): the actual independent-verification and durable-
   idempotency logic this mission implements, with SQLite-backed persistence that survives a
   real process restart (a new `RealBridgeFenceBoundary` instance, same on-disk file), reached by
   the platform side through a real HTTP client
   (`execution_v2.runtime.bridge_client.HttpBridgeFenceClient`) over a real (loopback,
   ephemeral-port) socket. Only the FINAL MT5 order-send primitive is a fake/counting callable;
   everything else - signature, account binding, generation/epoch, expiry, fingerprint,
   idempotency, reconciliation - is the real, production-intended code. This is what production
   runtime wiring (`execution_v2/runtime/service.py`) actually constructs and talks to.

3. **Live broker proof** (NOT performed by any code in this repository, and not performed by
   this mission): actually sending a request to the real `mt5-native-bridge` process on port
   22347/22348 and having it reach MT5. `mt5_bridge_fence/` is designed to be portable to that
   process (same validation/persistence logic, same wire shape) but has never been deployed
   there. `BROKER_WRITES=0` for every test and every proof in this repository, always.
"""

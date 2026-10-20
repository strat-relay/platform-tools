# Donchian V2 forward observation

This adds a separate observer and managed V2 instance. It never upgrades the V1
observer in place. The library parent strategy ID is shared; V1's definition,
version, parameters, runtime state, instance and image stay intact.

## Prepared path

- Selector: `donchian_breakout@V2`; the unqualified selector remains V1.
- Workload: `donchian-breakout-v2-observer`, namespace trading.
- Universe: BTCUSD -> BTCUSDm, XAUUSD -> XAUUSDm, USDJPY -> USDJPYm.
- Independent run, immutable manifest, activation epoch, runtime/dependency hashes,
  PostgreSQL research checkpoint and append-only event history.
- H1/M15 320-bar bootstrap; explicit positive cache provider tick sizes required.
  Canonical Redis cache only. No direct bridge requests or broker operations.
- The same shared cache observer dispatches the V2 recipe, preserving warm-up,
  H1 watermark buffering, atomic checkpoint/event writes and per-run DB locking.
  Bootstrap and pre-T0 reconstruction clear V2 episodes as well as pending entries.
- Entry mode prefers verified contemporaneous provider opening observations. When
  only completed bars arrive, entries remain retrospective reconstruction. Missing
  opening evidence is not replaced with a current quote or labeled prospective.
- StrategyLibraryAdapter accepts V2 only with the explicit V2 instance/parameter
  identity, RESEARCH status, OFF execution mode and enabled research publication.
  Signal metadata explicitly carries strategy_version V2 for the shared resolver.
- Canonical publication and signal outbox remain exclusively orchestrator-owned.
  The observer only writes research tables. Execution eligibility stays false.

## Prepare only after the runtime image has been built from this source

Do not insert a guessed digest or a now-expired T0 into GitOps. Use a verified
runtime image containing both this V2 recipe and its forward/orchestrator wiring.
The current source PR must be merged/built first. The control API image must
contain the V2 managed-catalog evaluator mapping so the new instance appears
under the existing Breakout Donchian strategy with V2 statistics. Pick a fresh DNS-safe run ID
and freeze a T0 at least ten minutes in the future with adequate rollout time.

```bash
PYTHONPATH=. python -m research.donchian_v2_forward plan \
  --run-id "$RUN_ID" --activation-time "$ACTIVATION_EPOCH" \
  --image "$RUNTIME_IMAGE" --output "$HOME/Downloads/$RUN_ID"
```

The command reads local source and writes a new local directory only. It refuses
an existing output directory, latest tags, missing inputs and insufficient future
notice. It generates run.json, identity.json, registration.sql, activation.sql,
quiesce.sql, workload.json and orchestrator-image.patch.json. No live changes occur.

## Registration and activation sequence

1. Verify the release image imports research.donchian_v2_forward and dispatches
   donchian_breakout@V2. Confirm Redis native H1/M15 streams and actual provider ticks.
   Check existing strategy-library-observer-db, trading-redis-auth and ghcr-pull
   Secret **names/existence**; never print values or copy credentials.
2. Apply registration.sql through the already approved database migration/admin
   path. It transactionally inserts only the new offline V2 version/parameter/instance
   and instrument memberships, with strict conflict checks. It does not rewrite
   the existing platform strategy parent or grant permissions. The reviewed SQL
   is not a numbered migration and does not run automatically at image startup.
3. Prepare GitOps for workload.json plus a patch that changes only the
   orchestrator-shadow container image to the same reviewed digest. That patch is
   necessary for the orchestrator to understand the V2 selector. Never bulk-repin
   frozen research observers. Updating this container rolls its shared pod, so
   other containers in that pod restart with their existing image/configuration.
4. Before T0, apply activation.sql through the approved database path. It verifies
   the exact frozen managed identities, OFF/ineligible status, existing enabled
   library parent and a previously unset or identical run binding. It updates
   only the new V2 instance online/research-publication attributes, and atomically
   seeds its independent research state and activation attestation. Existing state
   is not overwritten. A conflicting binding or elapsed T0 aborts the transaction.
5. Merge/reconcile the prepared GitOps workload and orchestrator image patch.
   No imperative restart or reconciliation trigger is required. The init container
   checks the activation attestation and source/config binding using the existing
   restricted research role; it requires no SELECT privileges on strategy_mgmt.
   The SQL path verified managed registration at activation. This is not continuous
   verification of later operator edits to managed policy. Readiness requires
   fresh successful observation cycles, not merely a running process.

The orchestrator reads active instances from PostgreSQL on lifecycle refresh.
A V2 registration alone is offline; source code or image availability alone is not
activation. If T0 elapses before the activation transaction, generate a fresh run
bundle while the instance is still unbound; do not backdate or edit frozen history.
If SQL succeeded and the workload starts later, preserve that T0 and report the
actual first healthy cycle separately. Bootstrap remains history reconstruction,
not fabricated evidence of observation during downtime.

## Verify live and report

```bash
kubectl --kubeconfig "$HOME/Downloads/local.yaml" --context local -n trading \
  get deploy donchian-breakout-v2-observer -o wide
kubectl --kubeconfig "$HOME/Downloads/local.yaml" --context local -n trading \
  logs deploy/donchian-breakout-v2-observer -c observer --tail=60
kubectl --kubeconfig "$HOME/Downloads/local.yaml" --context local -n trading \
  exec deploy/donchian-breakout-v2-observer -c observer -- \
  python -m research.strategy_library_forward health --manifest /config/run.json
```

Record the desired/running immutable digest, instance ID, configured T0, actual
first healthy cycle, finalized H1/M15 boundaries, successful instruments, tick
metadata and zero bridge/broker writes. Repeated cycles/restarts must not add
new identities for the same evaluation/entry. Unchanged theoretical outcome
observations may be recomputed with identical event IDs; the durable ledger
uses ON CONFLICT DO NOTHING. Inspect an accepted entry end-to-end through its
V2 parameter reference, emission class, canonical signal/outbox and theoretical
outcome. An absence of qualifying setups is not itself an unhealthy observer.

Use the version/instance parameter scope for statistics; never pool V1/V2 as a
validated Donchian result. This is forward **research**, not permission to enable
execution or claim profitable performance. Historical ablations/holdout work
remain outstanding.

## Stop and preserve evidence

Disable only the V2 managed instance using the run-scoped quiesce.sql and set only
this observer's GitOps replicas to zero. Keep its manifest, parameter identities,
state and events. Quiescing publication alone does not stop the observer process;
the GitOps replica change stops data collection. Do not disable the shared parent,
stop V1, delete data, modify credentials, enable broker execution or mutate old runs.

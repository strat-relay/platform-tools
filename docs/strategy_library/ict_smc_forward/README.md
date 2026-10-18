# ICT / SMC forward research rollout

This rollout is authorized for research observation and canonical orchestrator publication only. No broker execution is authorized. The older three-strategy canary, its pinned image, run, state, signals and outcomes remain unchanged.

## Scope and identities

| Family | Selector | Managed instance ID |
| --- | --- | --- |
| ICT | judas_swing@V2 | 4a411452-1440-5f91-bb95-eaf6dabbccc2 |
| ICT | power_of_three@V1 | 3441a68f-6209-56fa-b3ad-c0e8d6c3cb86 |
| SMC | order_block_retracement@V2 | b2af32aa-e42d-5ff6-9a42-2aece970459f |

Universe: XAUUSD→XAUUSDm, BTCUSD→BTCUSDm, USDJPY→USDJPYm. Complete M15 execution/H1 context; 320-bar bootstrap and retained history bounded at 10,000 bars. The cache is shared; the observer never polls the bridge. A separate run/workload prevents old checkpoint/config/artifact drift.

Migration `066_ict_smc_forward_registration.sql` creates the frozen managed versions/parameter sets, three offline observation instances, inactive canonical parents and canonical instrument membership. It does not enable publication, grant roles, change existing identities or create workloads. Definition identity is shared with any existing V1 definition; version, parameter and instance identities differ. Conflicting frozen identity, parent or instrument scope aborts. Verify slot 066 and its checksum on the actual database before applying it using the established controlled migration path.

## Timing and publication contract

The generated run explicitly uses CONTEMPORANEOUS_CANDLE_OPEN with a 10-second cache-only cycle. Confirmation remains completed-candle causal. A valid cached opening observation may produce a research entry before its entry candle completes, within the existing platform age window. No future high/low/close is consumed by entry checks. Delayed/missing open evidence must never be relabelled prospective; any later next-open reconstruction remains RETROSPECTIVE_RESEARCH_EVIDENCE and excluded from prospective performance by the existing metrics boundary.

The frozen offline definition retains its NEXT_OPEN_RECONSTRUCTED replay policy. The separately bound forward run declares its observation mode; it does not revise the frozen pattern/geometry parameters. Run ID, dataset/run binding, entry timing, emission class and source artifact distinguish these evidence classes. Compare results within the same timing/evidence protocol, not indiscriminately across reconstructed and contemporaneous cohorts.

The observer writes only its durable research ledger/checkpoint. The orchestrator adapter alone converts accepted entries to canonical paper-observation signals and the shared publisher/outbox. New selector/version checks bind canonical signals to their actual V1/V2 identities. Their outcomes use the shared theoretical resolver; broker outcomes are separate. Metadata always marks research-only/execution-ineligible, instance execution_mode remains OFF, and the canonical execution intent factory hard-blocks these three strategy IDs. Old canary publication payloads remain byte-identical (golden hashes tested).

## Prerequisites and plan generation

1. Merge the implementation and forward-wiring changes through normal PR checks. Produce an immutable **main-built** runtime image via the repository release workflow. Never deploy a branch image or an existing digest lacking these modules.
2. From the release-manifest.json artifact select the trading-platform-runtime reference and verify its commit is on origin/main and includes migration 066 plus the version selector/adapter changes. The orchestrator, API and unified resolver must run a release containing this wiring before activation.
3. Verify Kubernetes context `local`, namespace `trading`, the actual database target, migration ledger, existing registrations, and the existing `strategy-library-observer-db` Secret/login's research-only privileges. Reuse that credential reference; do not dump it or broaden grants. Verify cache symbol mappings/tick sizes, completed H1/M15 data, provider opening observations, and current process health. Expected FX/metal closure is not evidence of a process failure; no readiness or freshness gate is relaxed.
4. Choose a unique immutable DNS-safe run ID and explicit planned activation epoch. Do not reuse any prior run or alter T0 after evidence exists. Choose a future boundary allowing time for GitOps reconciliation.

Generate a concrete rollout bundle (the variables must contain verified real values):

```bash
PYTHONPATH=. python3 -m research.ict_smc_forward_plan plan \
  --run-id "$ICT_RUN_ID" \
  --activation-time "$ICT_ACTIVATION_EPOCH" \
  --image "$ICT_VERIFIED_RUNTIME_IMAGE" \
  --output "$HOME/Downloads/ict-smc-forward-plan"
```

Outputs: run.json, activation.sql, quiesce.sql, workload.json. No command above changes the cluster or database. No dummy image/T0 is bundled in source. Workload JSON is valid Kubernetes input and can be included as a resource in the actual GitOps kustomization.

## Apply and verify

- Through the established migration path apply 066 only after checking ledger/target/checksum. Verify all three instances appear offline with the expected version/parameter bindings and execution eligibility false.
- Add the generated workload.json to `apps/trading-platform/` in the **blissmen/stratrelay-gitops** repository and include it in kustomization.yaml. It contains a separate immutable ConfigMap, Recreate deployment and research-only network policy. Do not change the original canary resource. Use the tested digest in API/orchestrator/resolver as needed through normal GitOps release repinning.
- Apply the generated activation.sql as a single transaction through the existing authorized database connection/admin path after verifying the new runtime is deployed. It checks exact migration checksum and frozen identities, rejects a different run/T0/image, then enables only the three observation instances and their canonical parents. Repeating the same activation is safe. It never changes execution_eligible or execution_mode.
- Merge the reviewed GitOps desired state and allow Flux to reconcile. Do not use kubectl edit, set image, restart, or a manual observer Pod. Actual evidence start is the first successful persisted post-bootstrap cycle, not necessarily the planned T0 or Pod start. Record these timestamps separately.
- Verify image digest, run binding, all intended streams, successful/blocked instruments, readiness and cycle freshness. Confirm zero direct bridge requests and no overlapping observer replicas. Readiness may remain blocked while required FX/metal data are unavailable during closure; do not fabricate readiness.
- Follow the first post-activation proposal and accepted entry through durable ledger → exact managed instance → canonical signal/outbox → theoretical resolver. No setup is a valid result; do not change thresholds to manufacture a signal. Verify execution attempts and execution intents for these IDs remain zero. Verify both prospective and retrospective labels against actual entry-timing evidence.

## Pause and rollback

Use quiesce.sql through the same controlled DB path to set only these run-bound instances offline, then scale only the new workload to zero through GitOps. Preserve immutable ConfigMap, research state, ledgers and canonical evidence. Leave existing theoretical outcomes available to the resolver. Do not delete rows, reset checkpoints, repoint an existing run to another artifact, or disable unrelated parent strategies.

## Current verification boundary

Software and isolated PostgreSQL-compatible engine tests are complete. Live cluster access is unavailable in this workspace (no kubectl/kubeconfig). No live migration, activation SQL or GitOps workload change has been applied here. A real main release digest and live preflight/reconciliation evidence are still required before reporting RUNNING.

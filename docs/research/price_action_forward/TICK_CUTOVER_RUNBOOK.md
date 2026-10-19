# Repair both Price Action observer deployments

The release image implements `pa-provider-tick.v1` and rejects both original
instance UUIDs. `price-action-observers.yaml` still supplies those original UUIDs.
A restart or removal of the guard cannot repair this incompatible configuration.
The original manifests, checkpoints, append-only evaluations and theoretical
outcomes must retain their attribution.

`research.price_action_tick_cutover` prepares the complete replacement for BOTH
S/R Retest and Engulfing Continuation. It performs no database or cluster writes.
It produces registration, activation and quiesce SQL, two separate observer
Deployments with restrictive network policies, immutable manifests, and a copy of
the original deployment file with only `replicas: 0` changed. Do not use a new
UUID with the old default manifest. Do not resume legacy state under corrected
metadata. The original evaluator versions and exact frozen parameter rows are
reused; their parameters are not revised.

## Cluster-connected execution

Use the authorized `local` context and namespace `trading`. This entire repair is
research-only. Do not change account, execution policy, bridge, EA, or broker state.

1. Read the actual original managed instance/version/parameter joins, original
   checkpoint and ledger counts, parent enablement, current deployment image IDs,
   Flux revision and migration ledger. Save sanitized before-evidence. Verify the
   live target before any SQL. Confirm these original instance IDs are still the
   sources, not replacements already activated by another operator.
2. Verify a main-built immutable runtime digest containing the corrected runtime.
   Generate from the SAME source revision used in that image: activation verifies
   the checkpoint's exact source/manifest and entry-timing bindings. Do not repin
   an existing corrected run to another image/source revision.
3. Discover or provision through the existing controlled admin path a dedicated
   research-only PostgreSQL login/Secret with USAGE on research and only SELECT,
   INSERT and UPDATE on `research.price_action_observer_state` and
   `research.price_action_observer_evaluation`. Verify its target and that it lacks
   execution/outbox/account/broker write privileges. Do not reuse a library-only
   credential without checking PA table access; do not dump Secret values.
4. Export the current GitOps original deployment file as JSON, supplying verified
   values below. Choose a unique run ID and a future UTC M15 boundary with enough
   rollout time. Do not backdate T0. A future T0 is the intended publication
   boundary; actual first durable evidence is recorded separately.

```bash
kubectl --context local create --dry-run=client --validate=false \
  -f "$GITOPS_REPO/apps/trading-platform/price-action-observers.yaml" \
  -o json > "$HOME/Downloads/pa-original-deployments.json"

PYTHONPATH=. python3 -m research.price_action_tick_cutover \
  --gitops-source "$HOME/Downloads/pa-original-deployments.json" \
  --run-id "$PA_CORRECTED_RUN_ID" \
  --activation-time "$PA_CORRECTED_T0_EPOCH" \
  --image "$PA_VERIFIED_RUNTIME_IMAGE" \
  --db-secret "$PA_VERIFIED_RESEARCH_SECRET_NAME" \
  --output "$HOME/Downloads/$PA_CORRECTED_RUN_ID"
```

5. Apply `register.sql` transactionally through the verified established admin
   path. It verifies both original frozen bindings (including full parameter
   values), creates both new instances OFFLINE with execution OFF/ineligible,
   and registers the exact XAUUSD/BTCUSD/USDJPY membership. A repeated identical
   registration is idempotent; conflicting identities abort.
6. Through a GitOps PR replace the original deployment resource contents with
   `legacy-observers.json` (Kubernetes List JSON is valid YAML input), and add
   `workload.json` under a distinct run-specific filename to the active
   kustomization. Preserve the original images/env/manifests; the original
   workload replicas become zero. Review the diff to ensure no unrelated resources
   or existing library canaries change. Merge and let Flux reconcile.
7. Verify actual image IDs, Secret permissions, network-policy selectors against
   actual Postgres/Redis Pod labels, correct provider tick metadata, initial
   bootstrap with zero entry emission, and fresh persisted cycles for all three
   instruments in both new instances. Confirm source bindings, exact scopes and
   no legacy IDs in the new workloads. Expected market closures are not errors to
   bypass; wait if required data readiness is unavailable.
8. Only then apply `activate.sql` transactionally through the same admin path.
   It requires fresh RUNNING checkpoints with matching source/manifest/T0/timing
   bindings and all three instrument ticks, checks parents are already active,
   and flips only the two old instances offline and two new instances online with
   research publication enabled. It never enables execution or touches a ledger.
   Publication filters entries before T0. Pre-activation evaluations must remain
   identified separately from prospective published evidence.
9. Verify orchestrator refresh discovers both exact UUIDs and parameter refs.
   Follow a post-T0 accepted entry through canonical publication and theoretical
   outcome tracking when one occurs. Zero signals alone is not a failed rollout.
   Verify zero execution intents, broker attempts and broker writes for the new
   identities. Preserve legacy outcome resolution and old evidence access.

If another operator already completed a corrected cutover, do not generate a
second run blindly. Inventory the active identities and checkpoint bindings first.
Pause only the new run via `quiesce.sql` and scale its workloads to zero through
GitOps if rollback is needed. Preserve all ledgers. Do not restore legacy state
into corrected instances or automatically resume the affected legacy evaluators.

## Local verification boundary

The bundle generator and checkpoint/bootstrap tests run without a cluster. A
rollback-only PostgreSQL integration test is included in normal CI when
`OUTCOME_RESOLVER_TEST_DSN` points to an explicitly named isolated test database.
No live DB registration, credential grant, GitOps activation, or post-T0 observation
is implied by generating or testing a bundle.

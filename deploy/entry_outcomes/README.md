# Context ENTRY_ONLY outcome cutover

The API image uses `deploy/platform_api/Dockerfile` and includes migration 015.
The Context runner overlay is a separate, deliberately small image built with
this Dockerfile. The overlay image installs psycopg and carries the updated
Context runner, post-checkpoint projector, and the runner's existing
`strategy_report_format.py` import dependency. Its init container copies only
these files to the existing runtime PVC; it must not copy a full platform
source tree over the live shared runtime.

At rollout, configure `ENTRY_OUTCOME_SIGNAL_CUTOFF_ID` to the verified
post-T0 canonical EntrySignal cutoff. The runner projects only EntrySignals
under that exact cutoff whose economic-position and opportunity IDs match the
current Context ledger. PostgreSQL EntrySignals are the allowlist; unrelated
ledger rows are never imported.

The deployment uses the existing `trading-runtime-endpoints` secret for the
PostgreSQL connection. Existing main-runtime egress and PostgreSQL ingress
NetworkPolicies already allow this workload to reach PostgreSQL on TCP 5432.
No broker or execution endpoint is added.

`patch_context_runtime.py` updates only the `initialize-runtime-tree` and
`context-paper` containers in the shared Deployment. Its init command copies
only the required runner files to the PVC; it deliberately leaves
the orchestrator, Context ledger, and other files untouched. Use a digest-pinned
image and the verified EntrySignal cutoff ID when invoking it.

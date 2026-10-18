# Local Codex activation handoff

Continue the already-authorized ICT/SMC forward research rollout in platform-tools. Do the work; do not request another confirmation for the observation-only scope.

Use `~/Downloads/local (2).yaml`, context `local`, namespace `trading`. Discover actual workload/container names; do not assume labels. Read `docs/strategy_library/ict_smc_forward/README.md` and the current PR/release state first.

Complete normal CI/merge and main-only immutable image release for the strategy implementation and forward wiring. Verify the release manifest/source ancestry and actual image contents. Verify migration slot 066 and checksum against the actual DB; do not overwrite any conflicting migration. Apply `066_ict_smc_forward_registration.sql` through the established migration/admin path. Verify the exact three version/parameter/instance bindings and offline/execution-ineligible state.

Use `research.ict_smc_forward_plan` to generate a unique concrete run, future planned T0 and workload pinned to that verified main-built digest. Keep XAUUSD/BTCUSD/USDJPY canonical mappings, 320 H1/M15 bars, cache-only reads, contemporaneous open mode, 10-second poll and OFF execution modes. Verify the shared research-only observer credential and network policy; never expose credentials or grant execution writes.

Deploy the necessary orchestrator/API/resolver release and add only the new generated workload and network policy through `blissmen/stratrelay-gitops`. Do not mutate the existing canary's manifest, run, image pin or state. Apply generated activation.sql transactionally through the authorized DB path, merge the GitOps desired state, and verify Flux reconciliation plus durable post-bootstrap cycles. Follow accepted entries to canonical orchestrator publication and theoretical outcome tracking when they occur; absence of a setup is not a failure. No broker writes are authorized. Verify no execution intents/order attempts exist for the three new IDs.

Do not fabricate a digest, historical validation, evidence start, ready state, signal, or outcome. Do not loosen rule, freshness, timing, permission or readiness gates. Stop only on a concrete identity, permission, release, data-quality or safety conflict that cannot be resolved within this scope, preserving evidence and reporting the exact blocker. Do not change unrelated strategies, execution policy, accounts, bridge/EA or firewall.

Save sanitized timestamped diagnostics to Downloads, including actual image/source revision, migration checksum/state, instance IDs, run ID, planned T0, Pod start, first durable evidence time, per-stream status, publication links if available, and zero-execution checks. Report separately: code merged, migration applied, workload deployed, forward evidence verified, canonical publication verified, execution disabled. Do not mark any unverified stage complete.

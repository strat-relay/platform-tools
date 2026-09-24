# execution_v2 resource-quota headroom

`trade_management`'s equivalent file (`deploy/trade_management/resource-quota.yaml`) hard-codes a
specific `ResourceQuota` computed from the live cluster's observed state *at the time that mission
inspected it*. This mission (`CLAUDE-STRATRELAY-V2-EXECUTION-IMPLEMENTATION`) did not inspect live
Kubernetes state and must not fabricate numbers as if it had (mission constraints: DO NOT MODIFY
LIVE KUBERNETES, and no live cluster read was performed here).

Before scaling `deploy/execution_v2/workload.yaml` above `replicas: 0`, the operator (Codex, per
this mission's audit handoff) must:

1. Read the current live `ResourceQuota` for the `trading` namespace (`kubectl get resourcequota
   trading-conservative -n trading -o yaml`, or the equivalent for whatever quota object is live
   at that time - `trade_management`'s patch may already have changed it since this mission
   started).
2. Add exactly this one pod's own configured `resources.limits` from `workload.yaml` (currently
   `cpu: 150m`, `memory: 256Mi`) and one pod slot, following the identical arithmetic pattern
   `deploy/trade_management/resource-quota.yaml` documents in its own header comment.
3. Publish the resulting single-file patch as `deploy/execution_v2/resource-quota.yaml`, in the
   same additive, fully-computed-from-observed-state style - never a guessed or rounded-up value.

This file intentionally contains no YAML to apply; it exists so the requirement is not silently
skipped.

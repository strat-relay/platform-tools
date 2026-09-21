# Senior-engineer handoff

The repository contains several independent historical research branches and
one active frozen prospective paper experiment: `CONTEXT_STRUCTURE_RETRACE_V1`.
PID 49978 is the known runner. It reads MT5 data through the local bridge and
records simulated setup/position events; it cannot place broker orders.

Trusted identity is the manifest, not a filename or a remembered result:

- freeze: `2026-09-16T05:00:08.854816+00:00`
- source identity: `f931fe449d1bee78fde768374ad7ce88ded3f19f9afdf349e9acb47602772a0f`
- configuration: `1f1da2a63d69ac79e4aca21d0de33c860e76f4c33d9bd321cb50b20353114e1e`
- decision fingerprint: `70dba71d28fe8a5c09f9033b80eeb4c27a733c6c342537e03c631f41e2a1cdda`
- Phase 2 representation: `923d0d2762b6b78515a96e96dba17e42e34818aa82c406dc9ebc6f43b1a54c41`

Do not edit V1 decision functions, configuration, manifest, state or ledger
during collection. Develop a V2 in a new namespace and manifest. Use the
runbook for restart/recovery, the registry for strategy boundaries, and the
test evidence for integrity.

What is not proven: profitability, commission impact, sufficient sample size,
runner-exit policy, and fully auditable gap recovery. Historical March–
September results are exposed development data, not untouched validation.

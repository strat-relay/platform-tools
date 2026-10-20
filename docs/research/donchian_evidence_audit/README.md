# Donchian V1 evidence investigation

This change adds an explicitly invoked read-only audit, not a new strategy or
publication policy. Frozen Donchian V1 and its layered-entry behavior are unchanged.

## Findings from the 2026-10-20 14:31 UTC user-supplied report

All 53 signals are execution-ineligible. Among the 22 records classified prospective,
21 have resolved theoretical outcomes: 2 wins, 19 losses, -14.992R. BTC contributes
19 resolved entries: 1 win, 18 losses, -16.000R. Eight resolved BTC entries without
an earlier same-direction position still open include 1 win/7 losses (-5R); the
11 overlapping resolved entries all lost (-11R). These are a small, dependent
sample, not a permanent conclusion about every Donchian implementation.

The 31 retrospective records must remain a separate population. Eleven total
records remain open: eight have no recorded barrier touch, three have data gaps.
The source report does not independently validate the candle path.

All prospective-classified entries were emitted 916-972 seconds after their
recorded entry. SIG_8f9fe2c7374e7da76d18d097 has a recorded stop before emission.
This supports a timing/classification conflict, not a proven explanation for
all losses. Market price at emission has not been supplied.

The initial report queried camelCase run IDs, missing snake_case database keys.
It queried publication time from the signal row, whereas it lives in the outbox.
The deeper audit corrects both omissions. The initial metrics are still useful
for their explicitly recorded classification, but those labels are not proof
of contemporaneous observation.

## Source trace at main eb76ade

- The observer's `advance()` reconstructs the next open only after the entry
  candle completes. Accepted entries are marked `FORWARD_COMPLETED_CANDLE_RECONSTRUCTION`.
- The library adapter marks this path `RETROSPECTIVE_RESEARCH_EVIDENCE`.
- `CanonicalSignalPublisher.publish()` calls the common publication timing guard;
  it also demotes reconstructed entries or entries emitted over 60 seconds late.
- Canonical insertion copies source provenance and uses ON CONFLICT DO NOTHING.
  A later correct deployment does not rewrite older canonical classifications.
- The API projects stored source provenance; it does not recompute the class.

Thus current source has protections that contradict the old labels. An older
runtime, another ingestion path, or historical rows preserved across a rollout
are hypotheses. Exact live image history and original observer events are needed
to locate the responsible path. No root cause is claimed yet.

## Run

From this checkout on the operator Mac:

```bash
bash scripts/run_donchian_evidence_audit.sh
```

The script uses `~/Downloads/local.yaml`, context `local`, namespace `trading`,
and `deploy/unified-outcome-resolver` container `resolver`. It writes local reports
under Downloads. Database operations run in a read-only repeatable-read transaction,
with a 20-second statement timeout, 5-second connection/Redis timeouts, 500 signal
and 20,000 candle bounds per stream. Redis uses GET only. No broker access.

The source audit reads original accepted-entry and setup proposal events, their
parameter/artifact identities and tick size. Source permission errors are explicit;
no credentials, grants or fallback roles are used. Full source event payloads contain
research evidence; reports should be shared privately.

Reference paths combine MT5 archive and cache bars only if overlapping prices agree
within archive precision (1e-8). Forming bars are excluded. No provider is guessed
when canonical provider_symbol is absent, and no other archive provider is stitched
in. Missing cache/DB access or conflicts block the stream rather than fabricate a
complete path. An available stream can still contain gaps, which remain unresolved.

Geometry uses the frozen parameter hash and proposal tick. Without either, stop and
target verification is explicitly unavailable. A complete 40-bar causal window is
required for confirmation checks. Outcome consistency uses the **existing** shared
research resolver and retains raw reference candles and a hash for inspection.
This is not an independent resolver, live-fill simulation, cost-adjusted result,
or proof that the candle values were available at the original decision.

Compare source-to-canonical fields, class conflicts, parameter identity, geometry,
reference open, gap/ambiguity state, replay/stored terminal status and exact exit
valuation/time before proposing changes. A matching terminal label alone is not
full outcome equivalence. Any changed hypothesis belongs in a separately identified
research version/run. Do not rewrite V1's evidence or merge broker P/L into it.

## First deep-audit transfer (2026-10-20 ~14:47 UTC)

The supplied Kubernetes stderr reports WebSocket close 1006/unexpected EOF while
copying stdout. The 831,885-byte report ends inside a candle object, so the full
audit did not transfer successfully. Only 11 complete leading signal objects
could be recovered; they are an entry-ordered subset, not a representative sample.

For these 11 BTC records, source-to-canonical entry time/direction/prices match,
causal channel direction/stop/target and next-open prices match, and the shared
resolver reproduces all 11 stop losses, including stored exit timestamps and R.
Each source event is completed-candle reconstruction. Metadata already says
retrospective, while canonical source provenance says prospective. This proves
inconsistent stored classifications in this subset, not the responsible historical
binary, an independent price-feed verification, or a universal failure cause.

The updated launcher transports compressed JSON, retains candles once per stream,
checks gzip integrity and parses the full JSON locally before finalizing report.json.
Failed transfers retain .part output and diagnostics rather than a valid-looking
report filename. No workload or network configuration is changed.

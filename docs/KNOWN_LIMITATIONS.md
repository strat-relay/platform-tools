# Known limitations and open questions

- The prospective V1 sample is currently tiny: 4 setups, 0 opportunities and
  0 economic positions at the latest report.
- Commission is `UNKNOWN_UNRESOLVED`; no invented commission is included.
- Slippage is telemetry/shadow sensitivity only.
- Leg B runner exit remains a research hypothesis, not a V1 decision.
- March–September 2026 historical data is exposed development data, not an
  untouched validation set.
- The EURUSD gap at 2026-09-16T05:09:23.946714Z records 44 missing completed
  M5 candles, but the ledger does not conclusively prove every recovered bar
  was individually processed.
- No production live execution or personalized account risk engine exists.
- MT5/EA availability is required for collection; sleep, restart and network
  gaps can create missing observations.
- `AGENT_STATUS.md` is a convenience handoff and can become stale; verify
  health/status commands before acting.
- Existing historical research contains many superseded and contaminated
  artifacts; use manifests and strategy-specific runbooks to select evidence.
- This workspace currently has no Git metadata/remote; `.gitignore` now states
  hygiene policy, but source-control setup and review workflow remain deferred.

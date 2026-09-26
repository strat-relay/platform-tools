# V2 Risk & Execution Console — backend handoff

Mission `CLAUDE-V2-RISK-EXECUTION-CONSOLE`. **Not deployed** — code and tests only, per explicit
instruction. The full audit/merge/deploy handoff (covering both this backend and the Console
frontend together — review order, deployment steps, contract decisions, what's proven vs. not,
deferred work) lives in the frontend repo:
`trading-ops-console` worktree `trading-ops-console-v2-risk`, branch
`v2-risk-execution-console`, `docs/v2_risk_execution_console_handoff.md`.

Backend-specific summary for a reviewer working only in this repo:

- **Base**: `17b4a36` (this mission's given reference commit), merged with `c718862` (the Console
  realtime backend, per mission section 16's "reuse the realtime architecture already
  implemented" — the two were independent lines of history before this branch). Merge commit
  `f415da1` resolved two conflicts (`platform_api/control.py`,
  `tests/test_platform_control_api.py`), both genuinely independent additions on each side,
  full detail in that commit's own message.
- **New**: `postgres/migrations/018_execution_v2_risk_policy_override.sql` (additive singleton
  table), `execution_v2/risk_policy_store.py`, `platform_api/v2_risk.py`.
- **Changed**: `execution_v2/risk.py` (mechanical refactor only —
  `load_risk_policy()`'s behavior for its existing callers, including the live execution_v2
  runtime, is unchanged), `platform_api/control.py` (the one new POST route, still 405
  `READ_ONLY_API` for everything else), `platform_api/signals.py` (request-body plumbing + CORS
  method allowlist), `deploy/platform_api/Dockerfile` (copies `execution_v2/{__init__,risk,
  risk_policy_store}.py` + the baseline policy JSON — no execution_v2 runtime/worker code).
- **Final commit**: `a42bb8b` (also see the handoff-doc commit on top, `a42bb8b`'s message has
  the full technical detail).
- Tests: `python3 -m unittest discover -s tests -p 'test_*.py'` — 474 tests, 3 pre-existing
  failures + 8 pre-existing environmental errors (all confirmed to reproduce identically at
  `17b4a36` alone, unrelated to this work).

`EXECUTION_AUTHORITY_MODE` unchanged (`DISABLED`, read-only end to end — no code path anywhere
in this diff writes it). `BROKER_WRITES=0`. No production policy value changed by this branch —
only new code paths were added; the deployed policy file/values are untouched.

# Project inventory

Inventory date: 2026-09-16. This is an operational inventory of the flat
repository as found; no existing runtime files were moved or deleted.

## Active runtime

| Path | Purpose | Owner | State | Source/generated | Move now? | Recovery-critical? |
|---|---|---|---|---|---|---|
| `context_structure_retrace_forward.py` | Frozen V1 paper collector and read-only CLI | `CONTEXT_STRUCTURE_RETRACE_V1` | active | source | no | yes |
| `context_structure_retrace_forward_state.json` | Cursor, setup, position and counters | V1 runner | active | generated | no | yes |
| `context_structure_retrace_forward.jsonl` | Append-only prospective event ledger | V1 runner | active | generated | no | yes |
| `context_structure_retrace_forward.heartbeat.json` | Liveness/cursor heartbeat | V1 runner | active | generated | no | useful |
| `context_structure_retrace_forward.pid` | Single-runner lock | V1 runner | active | generated | no | no; stale locks may be removed only after process check |
| `context_structure_retrace_forward_manifest.json` | Immutable V1 identity and configuration | V1 freeze | immutable | generated/frozen artifact | no | yes |
| `context_structure_retrace_forward_summary.md` | Last runner summary | V1 runner | generated | generated | no | no |
| `context_structure_retrace_forward.py` | Active process cwd dependency | Python | open by PID 49978 | source | no | yes |

PID 49978 currently has the repository as its cwd and an established TCP
connection to the bridge. Do not rename, move, truncate, or rewrite the
runtime files above while it is active.

## Bridge and execution boundary

| Path | Purpose | State | Notes |
|---|---|---|---|
| `bridge.py` | Local HTTP/MCP queue and EA bridge | active service | Exposes both read tools and explicitly-confirmed live-capable tools; V1 allow-list blocks the latter |
| `MT5TradingBridge.mq5` | MT5 Expert Advisor transport/execution adapter | deployed source | Must be compiled/attached in MT5 for reads |
| `paper_runner.py`, `paper_engine.py` | Legacy/general paper engine | research/reference | Never call live tools |
| `com.caleb.liquidity-displacement-forward.plist` | Optional legacy supervisor artifact | archival/operational | Applies to liquidity-displacement, not V1 |

## Strategy and research source

- `context_structure_retrace/`: instrument-agnostic causal replay, schemas,
  indicators, S/R, attention, patterns, structures and retracement features.
- `research/context_structure_retrace_phase3` through `phase5`:
  development-phase candidate, integrity, target, review and correction work.
- `research/liquidity_displacement_normalization`: overlap/entry-opportunity
  normalization research.
- Root `liquidity_displacement*.py`, `micro_scalp*.py`,
  `simple_sr_candle*.py`, `engulfing_*.py`, `raw_sequence_*.py` and related
  JSON/CSV/Markdown files: historical research and prior forward variants.
- `strategies/*`: additive runbooks and output maps; root runtime paths remain
  canonical until a maintenance migration.

## Generated evidence and archives

Root-level JSON, JSONL, CSV and Markdown result files are historical evidence
or runner artifacts. `archive/` contains superseded V2 material. No files were
deleted. Files with names containing `contaminated-xau-20260915` are preserved
for audit and must not be mistaken for clean current state.

## Tests and fixtures

`test_*.py`, `research/*/test_*.py`, `context_structure_retrace/*` and the
legacy validation scripts are test/research code. Python `__pycache__/` and
`.DS_Store` are disposable generated noise and should not be treated as
research evidence.

## Migration disposition

No existing files were moved in this task. The desired tree is documented in
`PROJECT_STRUCTURE.md`; physical migration of active V1 paths is deferred to a
controlled maintenance window after stopping and snapshotting PID 49978.

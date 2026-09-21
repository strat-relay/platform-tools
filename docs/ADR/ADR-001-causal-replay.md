# ADR-001 — Causal completed-candle replay / no lookahead

Status: Accepted. Date: 2026-09-16.

Decision: completed bars are available only after bar end; forming HTF bars are
rebuilt from lower-timeframe bars strictly before `as_of`. Future outcome
labels are generated separately.

Evidence: `context_structure_retrace/data.py` and Phase 1/3 causality tests.

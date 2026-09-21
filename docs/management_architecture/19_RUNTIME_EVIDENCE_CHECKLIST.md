# 19 - Minimal READ-ONLY runtime evidence checklist (for Caleb / Codex, later)

Purpose: close A5 `OD-A5-14` and the A6 unknowns that **only the running system can answer**. Claude did not inspect any runtime, and this document performs nothing. Every item is read-only.

**Ground rules for whoever runs it**

* Use only: `ps`, `lsof -nP -i`, `launchctl print`/`list`, `stat`, `ls -l`, `tail -n`, `wc -l`, `head`, `cat`/`jq` on files, `git rev-parse`/`git log -1` in the deployed checkout. **Never** run the services' own CLIs (`start`, `stop`, `freeze`, `arm`, `demo-*`, `real-*`, `establish-*`): several create lock/stop files or state.
* Copy any file you need to analyse into a scratch directory **before** opening it with tools; do not open runtime state files for write; do not `touch` anything.
* Record the **UTC time of observation** with every answer; runtime facts expire.
* Paths are relative to the runtime root (`TRADING_PLATFORM_RUNTIME_DIR`, default `<checkout>/runtime`) unless a repository-root file is named.

## A. Process reality (what is running)

| # | Question | How (read-only) | Decides |
|---|---|---|---|
| A1 | Is the **Phase 7 observer** running? which PID/start time/cmdline? | `ps -eo pid,lstart,command \| grep phase7`; `context_structure_retrace_phase7.pid` (root) and `.heartbeat.json` `timestamp`/`status` | `OD-A5-13`; whether TM has any producer now |
| A2 | Is the **Trade Manager consumer** running? mode (`ADVISORY_SHADOW`/`REAL_MANAGEMENT`), start time? | `ps` for `trade_manager.stream_consumer`; `runtime/trade_manager/observation_stream/collector_health.json` (`status`, `mode`, `timestamp`, `collector_running`) | whether the legacy chain is live |
| A3 | Which **process versions/commits** are deployed (orchestrator, consumer, TM, runners, bridge)? | `git -C <deployed checkout> rev-parse HEAD` and `git status --short`; process start times vs `git log -1 --format=%cI` for each module | whether running code predates/postdates known changes (A5 OD-01: commit `c6482c3`) |
| A4 | Is **demo mode** running or armed? | `runtime/execution/demo_state.json` `armed`; consumer heartbeat `mode`; `real_state.json` `armed` | OD-04 deployment status |
| A5 | Which stack services are actually up vs the registry? | compare `launchctl list` and `ps` against `scripts/mt5_stack_services.json` (bridge repo) service names | intended vs actual |

## B. The observation stream

| # | Question | How | Decides |
|---|---|---|---|
| B1 | Which observation stream files exist and are they growing? | `ls -l runtime/trade_manager/observation_stream/`; `stat` mtimes; `wc -l events.jsonl`; a **second** `wc -l` a minute later | is anything publishing |
| B2 | Does the stream contain `MARKET_OBSERVATION` rows with **non-null `bid`/`ask`**? | on a **copy** of `events.jsonl`: `jq -c 'select(.event_type=="MARKET_OBSERVATION") \| {bid,ask}' \| sort \| uniq -c \| head` | confirms `M1` at runtime |
| B3 | Current **checkpoint values** and gaps | `checkpoint.trade_manager.json` (`sequence`, `gaps`); `publisher_health.json` (`publisher_sequence`, `publisher_last_event_at`, `publisher_failures`) | lag, loss evidence |
| B4 | How many `published_ids` does `publisher_state.json` hold? file size | `stat`; `jq '.published_ids \| length'` on a copy | growth risk, dedupe scale |
| B5 | Are there rotated archives (`events.<epoch>.jsonl`)? | `ls -l` | retention/volume baseline |
| B6 | Is `activation.json` present and what does it say? | copy + `jq` (`mode`, `strategies`, `instruments`, `trade_manager_activation_cutoff`) | product vs REAL_MANAGEMENT activation |
| B7 | `collector_health.json`: `incomplete_intervals`, `observation_count`, `position_event_count` | `jq` on a copy | did any observation ever evaluate (`observation_count`) and how many were `MISSING_QUOTE` |

## C. Management chain outputs

| # | Question | How | Decides |
|---|---|---|---|
| C1 | Do `runtime/management/management_proposals.jsonl`, `management_intents.jsonl`, `management_decisions.jsonl` exist and how many rows? statuses/reasons? | `wc -l`; `jq -r .reason` counts on copies | whether any proposal was ever made/authorised (expected: none or all `POSITION_NOT_FOUND`) |
| C2 | Does `runtime/execution/management/execution_results.jsonl` exist; statuses (`REJECTED`/`COMPLETED`), reasons? | copy + `jq` | REAL close/trail reality, retry pattern |
| C3 | Ownership ledger contents (count, states) | copy `runtime/management/ownership_registry.jsonl`; `jq -r .ownership_state` counts | linkage to `signal_id` |
| C4 | Who writes `broker_state.json`? last `captured_at`, `snapshot_version`, `read_only_source` | `stat`; `jq` | confirms the single writer (A5 `P8`) |

## D. Inputs and feeds

| # | Question | How | Decides |
|---|---|---|---|
| D1 | Does the **legacy full-state file** `context_structure_retrace_forward_state.json` exist; mtime vs the compact state? | `ls -l` both files (repo root); compare mtimes | whether Phase 7 sees a frozen position set |
| D2 | Which research listener do Context runner and Phase 7 use (`22347` vs `22350`)? | `ps` command lines (`--mcp-url`), `lsof -nP -iTCP -sTCP:LISTEN` for the bridge PIDs | feed identity (`05`) |
| D3 | Liquidity runners' data path: `MT5_WINEPREFIX`/`MT5_WINE_BIN`/`MT5_READ_ONCE_PATH` values | `ps eww` of the runner (environment of the process) | reference feed per stream |
| D4 | EA poll interval (`MinimumPollMilliseconds`) on the research and execution EAs | terminal EA inputs (read-only screen/`MQL5/Logs`) | provider read budget (A5) |
| D5 | Is the Phase 7 manifest present and its `freeze_timestamp`? | `context_structure_retrace_phase7_manifest.json` (tracked, but check the deployed copy) | eligibility vocabulary baseline |

## E. Golden-corpus inputs (copy only)

| # | Item | How | Use |
|---|---|---|---|
| E1 | `context_structure_retrace_phase7_state.json` (input of `trade_manager/replay.py`) | copy to scratch | golden fixture for the XAUUSD case; **not tracked in git** |
| E2 | Phase 7 `context_structure_retrace_phase7.jsonl` (events) | copy | edge cases (gaps, checkpoints) |
| E3 | Any `runtime/trade_manager/observations/*.jsonl` (research store) | copy | counterfactual experiment outputs, classification RESEARCH |

## F. Related A5 unknowns (same session, same rules)

| # | Question | How |
|---|---|---|
| F1 | Do `runtime/orchestration/sizing_decisions.jsonl` rows with `error == "'tradeability_decisions'"` exist? does `tradeability_decisions.jsonl` exist? | `jq`/`ls` on copies (A5 OD-01) |
| F2 | Does `runtime/execution/real_execution_resume_generations/` exist? | `ls -ld` (A5 OD-02) |
| F3 | `orchestration/state.json` `live_execution_resume_generation` value; resume file `real_execution_resume_generation` | `jq` on copies |

## G. How results are used

Answers go into a single dated evidence note (no repo edits required): for each item `answered / not answered`, value, UTC time. Expected outcomes under the A6 analysis: A1 no or stale heartbeat; B2 all `null`; B7 `observation_count` 0 with growing `incomplete_intervals` (or the consumer not running); C1 empty or `POSITION_NOT_FOUND`; C2 empty or rejected. **If any expectation is contradicted the analysis must be revisited** - in particular an unlisted producer (A1/A5), non-null quotes (B2), or authorised proposals (C1).

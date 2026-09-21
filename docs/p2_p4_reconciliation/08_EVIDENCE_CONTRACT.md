# 08 - Performance / evidence contract: ENTRY_ONLY vs ENTRY_PLUS_TM and BACKTEST / FORWARD / LIVE

Confirms and tightens A6 `15` and A2 `06_PERFORMANCE_EVIDENCE.md` (A7 > A6). Design only; no calculation is implemented.

## 1. The separation (unchanged, confirmed)

`ENTRY_ONLY` and `ENTRY_PLUS_TM` are **different series and are never blended**. Series key = `(stream_id | legacy_stream_key, series_kind, provenance, strategy_ref, trade_manager_version_id | null, window)`.

| | `ENTRY_ONLY` | `ENTRY_PLUS_TM` |
|---|---|---|
| Outcome source | the **strategy's frozen ledger** for the reference trade (never recomputed by the TM) | the **managed reference simulation**: only decisions actually emitted in real time by the bound frozen `TradeManagerVersion`, applied in order |
| Needs a TM version? | no | yes (`FROZEN`, bound at open) |
| Independent of publication/execution? | yes | yes |

An `ENTRY_PLUS_TM` outcome for `TM-NONE-1` is, by construction, identical in *decisions* (all `HOLD`) to `ENTRY_ONLY`, but it is still a **separate series with its own clock**; it must never be presented as evidence for a management policy.

## 2. Provenance interaction (`evidence_mode` on ManagedTrade, `data_status` on observations and decisions)

| Provenance | When it applies to a ManagedTrade | Product series it may join | Never |
|---|---|---|---|
| **BACKTEST** | signals or observations come from a historical provider/dataset, or the trade was created by a replay/backfill | `ENTRY_ONLY`/`ENTRY_PLUS_TM` labelled BACKTEST | may not be relabelled FORWARD; may not use a TM version frozen *after* the data it evaluates as if it were prospective |
| **FORWARD** | **all** of: (a) the EntrySignal is a live prospective signal (`provenance_class = PROSPECTIVE_ORCHESTRATOR_SIGNAL`, not `GAP_RECOVERY`/`PRE_ORCHESTRATOR_REFERENCE`); (b) the strategy version's freeze precedes the signal; (c) the bound TM version's `frozen_at` <= `tm_bound_at` <= `created_at`; (d) creation lag `created_at - decision_time` within the policy bound `L_create`; (e) every observation used has `data_status = FORWARD` (live provider, not historical) and `observed_at` in real time; (f) every decision was persisted (`persisted_at`) before the next observation was recorded | `ENTRY_ONLY` (FORWARD) and, if `ENTRY_PLUS_TM` conditions hold, `ENTRY_PLUS_TM` (FORWARD) | a trade failing any of (a)-(f) is `FORWARD_INELIGIBLE(reason)` and excluded from FORWARD series |
| **LIVE** | belongs to **Personal Execution**: realised results of real broker execution, reconciled to broker deals (`live.trade.closed`) | its own `LIVE` series | is not produced by Trade Management; a ManagedTrade **never becomes LIVE**; unexecuted trades cannot have LIVE outcomes |

**An unexecuted EntrySignal can legitimately produce FORWARD `ENTRY_PLUS_TM` evidence**: the reference trade needs a strategy ledger (entry-only), a live provider (observations), a frozen TM version, and real-time persisted decisions - no broker, no account, no execution success (`03` section 7). This is exactly the A2 principle "FORWARD evidence with the reference execution model (no broker order)".

## 3. Research replay must not masquerade as FORWARD

Guards (each testable):

1. `data_status` is set by the **producer's provider capability**, not by the consumer: a `historical` provider can only emit `REPLAY`/`BACKTEST`.
2. The observation/decision id namespaces include `provider_id`/`feed_id`; a replayed observation cannot collide with a FORWARD one.
3. A ManagedTrade created by backfill/reconciliation of old shadow signals gets `eligibility = FORWARD_INELIGIBLE(LATE_CREATION)` (`03` section 6, `10`).
4. `experiments.py` hypothetical outcomes and Phase 7 research thresholds are `RESEARCH` and can never be attached to a series.
5. Series assembly reads `evidence_mode` **and** the per-observation `data_status`; any FORWARD series containing a non-FORWARD observation is invalid.

## 4. A specific hazard found in the actual P2: observation start lag

Both legacy adapters emit an EntrySignal only **after** the strategy's reference fill (`decision_time` = fill time; `signal_emitted_at` = discovery). A ManagedTrade created from it cannot observe the market between the fill and its creation. Therefore:

* record `observation_start_lag_seconds = first_observation.observed_at - decision_time` (and `creation_lag`),
* a decision may only be based on observations at or after creation,
* the unobserved interval is a first-class flag (`LEFT_TRUNCATED_LIKE`, analogous to Phase 7's `LEFT_TRUNCATED_EXISTING_POSITION`), and a bound `L_create`/`L_start` (owner sign-off, `OD-A7-4`) decides FORWARD eligibility.

## 5. Data that must exist for the series (recorded by P4, listed in A6 `15` section 3)

EntrySignal geometry + reference-fill semantics (copied into ManagedTrade), bound versions and `frozen_at`, eligibility class, strategy exit event, every observation used (id, seq, `observed_at`, quote, `bars_ref.digest`, `data_status`), every decision (id, action, parameters, reason codes, `decision_time`, **`persisted_at`**, trace ref), the managed-track applied-decision ledger, cost-model version, `publication_status` as an orthogonal attribute. **The real-time admissibility rule** of A6 `15` section 4 stands.

## 6. Derived TM version = new clock

Any derived `TradeManagerVersion` starts its own FORWARD clock at its `frozen_at`; it never inherits or back-fills the parent's series; challenger tracks are separate series.

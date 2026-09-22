# 04 - FORWARD-timing observability and OD-A7-4

## 1. What P2-A1 now persists

`strategy.entry_signals` carries, for every ingested signal:

| Timestamp | Meaning | Column |
|---|---|---|
| `decision_time` | strategy reference fill time | `decision_time` (normalised UTC) |
| `signal_emitted_at` | orchestrator discovery time | `signal_emitted_at` |
| `ingested_at` | when P2-A1's ingest transaction committed the row | `ingested_at timestamptz NOT NULL DEFAULT now()` |

This is sufficient to compute, later (not now - no calculation is implemented, none should be):

* **creation/ingestion lag** = `ingested_at - decision_time` (and, separately, `ingested_at - signal_emitted_at` for tailer/relay latency specifically)
* **discovery lag** = `signal_emitted_at - decision_time` (already existed pre-A1 as a derivable quantity; now durably queryable)
* **observation-start lag** = not yet computable from P2-A1 alone; it needs the **first observation's `observed_at`**, which is a P4.3 (Trade Observation Service) deliverable, correctly out of scope here

No ingestion/discovery/creation lag field was silently defaulted, bounded, or hidden behind a computed eligibility flag - the raw timestamps are stored and nothing derives a pass/fail judgement from them yet.

## 2. OD-A7-4 status

**OD-A7-4 remains genuinely OPEN.** No bound (`L_create`, `L_start`) was chosen anywhere in the diff; no code reads a configured threshold and rejects/accepts a signal by lag. This was correctly left to the owner, as instructed.

## 3. What OD-A7-4 blocks and what it does not

| Question | Answer | Why |
|---|---|---|
| `OD_A7_4_BLOCKS_P2_1_COLLECTION` | **false** | P2.1 collection only needs the raw timestamps to exist and be queryable so evidence reports can *display* lag distributions; it does not need a pass/fail bound. They exist (section 1) |
| `OD_A7_4_BLOCKS_P4_2` | **false** | P4.2 (`docs/p2_p4_reconciliation/10`) creates a ManagedTrade with `eligibility` classified by `provenance_class`/creation lag, with an explicit **`ELIGIBILITY_UNEVALUATED`** state when no bound is configured (per that contract's own wording: "default unset = ELIGIBILITY_UNEVALUATED, never a guessed number"). ManagedTrade creation itself does not require the bound to be set |
| `OD_A7_4_BLOCKS_FORWARD_QUALIFICATION` | **true** | A trade cannot be *certified* `FORWARD`-eligible (A7 `08` section 2, conditions a-f) until a bound exists to test condition (d) `creation lag within L_create`; until then such trades are correctly `ELIGIBILITY_UNEVALUATED`, not silently `FORWARD` |

This matches the distinction the task expected: **collecting timing data may proceed; claiming FORWARD eligibility remains blocked** - and it is verified, not assumed, by reading exactly which code paths exist (timestamp columns: yes; threshold logic: no; eligibility classifier: not yet built, correctly deferred to P4.2).

## 4. Nothing new to reconcile against OD-A7-4

P2-A1 introduces no new blocker and resolves none of OD-A7-4's substance (it was never P2-A1's job to). The open decision is unchanged in scope; it is simply now **answerable with real data** once P2.1 produces a lag distribution.

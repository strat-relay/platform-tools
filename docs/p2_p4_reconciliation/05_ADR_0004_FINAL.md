# 05 - ADR-0004 final review against the actual P2

A6 `21_ADR_CANONICAL_OBSERVATION_PRODUCER.md` is left untouched (historical). This document is the **A7 approval record** and the list of adjustments.

## 1. Status

> **ADR-0004: APPROVED.** Approved by the architect's decision recorded in the A7 mission (2026-09-21) for: canonical producer = **Trade Observation Service**; owner = **Trading Core / Trade Management**; dependency = **MarketDataProvider only**; Phase 7 observer = legacy, not canonical; strategy runners and Personal Execution are not canonical producers; broker-state authority remains P5; P3 shadow only; P5 blocked by OD-06. **OD-A6-1 is resolved.**

## 2. Can it be approved *without qualification* based on the actual P2? **Yes.**

The ADR decides *who produces observations, from what, with which event, order and dedupe*. None of those depends on a P2 contract that P2 got wrong:

| ADR-0004 element | P2 interaction | Incompatibility? |
|---|---|---|
| producer / owner / dependency | none (P2 has no observation, market data, or Trade Manager code) | none |
| `trade.observation.recorded.v1`, ordering `managed_trade_id` + gapless `observation_seq`, dedupe `observation_id` = `Nats-Msg-Id`, stream `TRADING_OBSERVATION` | needs V1.2 envelope + publisher + stream config | **two implementation gaps in V1.2 infrastructure, not in P2** (section 3) |
| ManagedTrade as observation subject | needs the EntrySignal -> ManagedTrade input | **P2 provides an insufficient input** (`03`): this is a prerequisite of `P4.2`, tracked in `12`, and is *outside* what the ADR decides (A6 `06` owns the ManagedTrade model) |

No concrete incompatibility with the decision exists. The P2 findings gate implementation stages (`P4.2`, `P2.1`), not the decision.

## 3. Adjustments (implementation-level; recorded, not qualifications)

| # | A6 text | Adjustment | Reason (evidence) |
|---|---|---|---|
| A-1 | subjects `trade.observation.recorded.v1.<instrument>` (A6 `09`) | **exact subject `trade.observation.recorded.v1`**; instrument is a payload field (and may become a header) | `contracts.validate_subject` accepts only exact members of `SUBJECTS`; a tokenised subject is refused (`R17`). A token can be introduced later together with validator support, but ordering never depended on it |
| A-2 | dedupe `observation_id` also used as `Nats-Msg-Id` | keep, and require **P4.1 to add `Nats-Msg-Id` support to `JetStreamPublisher`** (headers pass-through) | the adapter sends no headers (`R17`) |
| A-3 | producer publishes from PostgreSQL via outbox | keep, and require a **minimal outbox relay** (leased, ordered per aggregate, publishes with `Nats-Msg-Id`, marks published) before any observation is published (P4.3) | none exists (`R18`); P2.1 needs the same relay |
| A-4 | `TRADING_CORE` carries `trade.opened/decision.made/closed` and `signal.management.published.v1` | keep, but `STREAMS["TRADING_CORE"]` is currently derived by **prefix** (`strategy.`, `signal.`); P4.1 must extend it with the explicit `trade.opened.v1`, `trade.decision.made.v1`, `trade.closed.v1` and **must not** use a `trade.>` wildcard that overlaps `TRADING_OBSERVATION` | JetStream forbids overlapping stream subjects; `contracts.py` prefix rule |
| A-5 | `strategy_version_id` in the observation/decision payloads | payloads carry **`strategy_ref = "<strategy_id>@<strategy_version>"`** and `trade_manager_version_id`; `strategy_version_id` is omitted/NULL until a unique StrategyVersion registry exists | P2 registers a shared `'V1'` (`R7`, `R8`) |
| A-6 | `parameter_set_id` in payloads | carried as nullable `parameter_set_ref` with status `LEGACY_IMPLICIT_IN_STRATEGY_ID` | P2 always NULL (`R9`) |
| A-7 | OD-A6-2 bar-window transport | **resolved: `bars_ref` digest reference** (not inline delta) | keeps the payload small (A4 rule); required for identical facts across TM versions |
| A-8 | OD-A6-3 challenger versions | **resolved: evaluation tracks** on the same ManagedTrade | keeps `MT_` 1:1 with the EntrySignal (A2 ER); see `03` |
| A-9 | OD-A6-4 first version | **resolved: `TM-NONE-1`** (`04`) | |
| A-10 | OD-A6-6 management event name | **resolved: `signal.management.published.v1`** | A2 catalogue + V1.2 `.v1` convention; Commerce's `signal.*` filter |

Unchanged from ADR-0004: no account, ticket, lot, R-multiple, MFE/MAE, EMA/structure values, decision or full bar arrays in the product observation; `observation_id` deterministic from `(managed_trade_id, provider_id, feed_id, instrument, source_timestamp, quote hash)`; `observation_seq` assigned by the producer inside its transaction; `TRADING_OBSERVATION` bounded and separate from domain events; sizing/retention remain OD-08.

## 4. Canonical statements (final)

| | |
|---|---|
| Canonical producer | Trade Observation Service |
| Canonical owner | Trading Core / Trade Management |
| Market dependency | `MarketDataProvider` only (research-listener MT5 adapter today) |
| Canonical event | `trade.observation.recorded.v1` |
| Ordering | `managed_trade_id` + gapless per-trade `observation_seq` |
| Deduplication | deterministic `observation_id` (also `Nats-Msg-Id`) |
| Stream | `TRADING_OBSERVATION` |
| Product observation content | market facts + `bars_ref` only; no account/ticket/lot |
| Phase 7 observer | legacy; not canonical (`06`) |
| Strategy runners / Personal Execution | not producers |

## 5. What this approval does **not** do

It does not approve implementation of P4.2 (blocked by the P2 amendment), does not decide any management policy, does not authorise any P5 work, and does not change OD-06.

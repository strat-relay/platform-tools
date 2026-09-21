# 08 - Canonical observation event contract (`trade.observation.recorded.v1`)

The current JSONL record (`trade-manager-shared-observation-v1`) is **not** the final schema (`01`, `02`). This contract maps onto the V1.2 `EventEnvelope` (`event_id, event_type, aggregate_type, aggregate_id, aggregate_version, occurred_at, payload, correlation_id, causation_id, producer, schema_version`) and A4's envelope rules (small facts + stable references, no large payloads).

## 1. Envelope mapping

| Envelope field | Value |
|---|---|
| `event_id` | `observation_id` (deterministic, section 3) - also `Nats-Msg-Id` |
| `event_type` | `trade.observation.recorded.v1` |
| `aggregate_type` / `aggregate_id` | `managed_trade` / `managed_trade_id` |
| `aggregate_version` | `observation_seq` (per-trade, gapless) |
| `occurred_at` | `observed_at` |
| `causation_id` | `market_snapshot_id` (the instrument-level snapshot it derives from) |
| `correlation_id` | `signal_id` |
| `producer` | `trade-observation-service@<version>` (+ `runtime_instance_id`) |

## 2. Payload

```json
{
  "schema": "trade-observation.v1",
  "observation_id": "TOBS_<24 hex>",
  "observation_seq": 1287,
  "managed_trade_id": "MT_<24 hex>",
  "signal_id": "SIG_<24 hex>",
  "stream_id": "…",
  "strategy_version_id": "…",
  "parameter_set_id": "…",
  "trade_manager_version_id": "TMV_<24 hex>",
  "instrument": "XAUUSD",
  "direction": "LONG",
  "observed_at": "2026-09-21T10:15:00.120Z",
  "effective_at": "2026-09-21T10:15:00Z",
  "market_snapshot_id": "MSN_<24 hex>",
  "quote": {"bid": 4311.42, "ask": 4311.62, "spread": 0.20, "source_timestamp": "2026-09-21T10:14:59.900Z",
            "provider_id": "mt5-research", "feed_id": "…", "price_semantics_version": "ps.v1"},
  "bars_ref": {"timeframe": "M5", "completed_through": "2026-09-21T10:10:00Z", "count": 205, "digest": "sha256:…"},
  "data_status": "FORWARD",
  "provenance": {"producer_version": "…", "provider_capabilities": ["EXECUTABLE_QUOTES", "BARS"], "future_data_used": false}
}
```

**No** account, ticket, lot, R-multiple, MFE/MAE, EMA/structure values, decision, or full bar arrays.

### 2.1 Bars: reference, not payload (`OD-A6-2`)

Today every event inlines 205 M5 bars (`m5_history`). The contract carries `bars_ref` = `(timeframe, completed_through, count, digest)`; the evaluator resolves the window through the `MarketDataProvider` bar store and **verifies the digest** (mismatch => quarantine, `18`). Rationale: payload stays small (the A4 rule); many trades on one instrument share one window; replay substitutes a historical provider with the same digest. The alternative - inlining a bounded delta of newly closed bars - is documented in `OD-A6-2`; the choice does not change any other field.

## 3. Keys

| Key | Definition | Purpose |
|---|---|---|
| **Ordering key** | `managed_trade_id`; order defined by `observation_seq` | one trade's observations are applied strictly in order; different trades are independent |
| **Deduplication key** | `observation_id = stable_id("TOBS", {managed_trade_id, provider_id, feed_id, instrument, source_timestamp, sha256(bid, ask)})` | identical fact for the same trade cannot exist twice, regardless of retries or producer restarts; no publisher sequence inside the hash (today's envelope hashes a `source` that later gets mutated) |
| Inbox key | `(consumer_name, event_id)` | duplicate delivery |
| `market_snapshot_id` | `stable_id("MSN", {provider_id, feed_id, instrument, source_timestamp, sha256(bid, ask)})` | share one market fact across trades |
| Decision idempotency (see `11`) | `decision_id = stable_id("TMD", {managed_trade_id, observation_id, tm_version_id})` | one decision per (trade, observation, version) |

## 4. Sequence, late, stale, replay

| Situation | Rule | Recorded as |
|---|---|---|
| **In order** (`seq == last_applied + 1`) | evaluate | decision |
| **Duplicate** (`observation_id` seen, or `seq <= last_applied`) | acknowledge, no effect | inbox hit |
| **Gap** (`seq > last_applied + 1`) | do **not** evaluate past the gap; redeliver with backoff up to `max_deliver`; if the missing seq exists in PostgreSQL (`trade_observation` rows), the evaluator reads it and proceeds in order; if it does not exist after the bound, park the trade `OBSERVATION_GAP` (no decision, alert) | evidence: gap interval |
| **Late** (`observed_at` earlier than the last applied observation but `seq` unseen) | cannot occur with a single sequencing producer; if it does (producer bug/replay), persist as evidence, **never evaluate** | `LATE_OBSERVATION` |
| **Stale** (`now - observed_at > max_market_age_ms` of the bound version, or provider flag) | evaluate to `HOLD(STALE_MARKET_DATA)` persisted for audit - the existing reason code (`engine.REASONS`) and rule (`max_market_age_ms`), which no production caller ever supplied (`M12`) | decision `HOLD` with reason |
| **Replay** (`data_status = REPLAY/BACKTEST`) | same evaluator, same ids namespace with `data_status` in the key derivation via `provider_id`/`feed_id`; never mixed with FORWARD series | decisions carry the status |
| **Redelivery after crash** | recompute; same `decision_id` => unique constraint absorbs | no duplicate |
| **Trade closed / not found** | ack after recording `OBSERVATION_FOR_CLOSED_TRADE` / quarantine `MANAGED_TRADE_MISSING` | evidence |

## 5. Who assigns `observation_seq`

The **producer, inside its own transaction**: it locks the ManagedTrade row (or a per-trade counter), assigns `seq = last_seq + 1`, inserts `trade_observation` and the outbox row, commits. The stream is therefore a *view* of PostgreSQL; a lost or truncated stream is rebuilt from `trade_observation` (A4: JetStream is not the system of record). Volume is bounded by (open trades x poll rate); retention of `trade_observation` rows is `OD-08`.

## 6. Versioning

`schema = "trade-observation.v1"`, subject suffix `.v1`; a breaking change is a new subject/version with a dual-publication window. Unknown `schema_version` => park + alert (A4 failure mode 18).

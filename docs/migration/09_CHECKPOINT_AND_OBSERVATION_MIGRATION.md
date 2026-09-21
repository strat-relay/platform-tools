# 09 - Checkpoint migration and observation fan-out migration

## Part 1 - Checkpoints

### Inventory (from `04`)

| Artifact | What it stores | Is correctness dependent on it? |
|---|---|---|
| `ORC-10 orchestration/state.json` | `processed_signal_ids` (unbounded) | **Yes** (with `signals.jsonl` unique key as backstop) |
| `ORC-11 startup_epoch.json` | replay watermark | Yes - safety boundary (classed AUTHORITATIVE) |
| `ORC-12 <instance>-startup-baseline.json` | liquidity pre-existing signals | Yes - prevents re-emit of history |
| `EXE-06 execution/state.json` | consumer derived state | No (derivable) |
| `TMG-02 publisher_state.json` | all published ids (unbounded) | Yes (dedupe) |
| `TMG-03 fanout consumer checkpoint` | read position; **saved before processing** | Yes (and at-most-once) |
| `TMG-04 collector_state / health` | collector progress / health | Partly |
| Runner `state` files | strategy state | owned by frozen runners; not a platform checkpoint |

**Rule (`PRODUCTION_FILE_IPC=0`): no checkpoint file may be required for correctness.**

### Target

Correctness moves from *"remember what I processed"* to *"make reprocessing harmless"*:

| Today | Target |
|---|---|
| `processed_signal_ids` | `UNIQUE(signal_id)` on `signal` + inbox `(consumer, event_id)`; the set is seeded once from the file so history is not reprocessed |
| replay watermark | `replay_watermark(stream, environment, generation, cutoff)` row - authority (`DIRECT_CUTOVER`, imported not regenerated) |
| liquidity baselines | `stream_baseline(stream_instance_id, baseline_hash, established_at)` row |
| fanout consumer checkpoint | **JetStream durable consumer** position; ack after processing; inbox for idempotency |
| publisher `published_ids` | `Nats-Msg-Id` + inbox; unbounded set disappears |
| execution `state.json` | none |
| collector state/health | `trade_manager_checkpoint` row + heartbeat |

### Migration steps

1. **Seed**: import the checkpoint contents as *idempotency seeds* (signal ids, baselines, watermark). Over-seeding makes the system skip an item it should have processed (detected by reconciliation: file-present/DB-absent); under-seeding is harmless because unique constraints block duplicates.
2. Start consumers in shadow at a position derived from the imported checkpoint (`DeliverByStartSequence` or time), **not** "all" and not "new" - reconciled in the shadow phase.
3. Gate `CHECKPOINT_INDEPENDENT` (part of `DB_AUTHORITY_READY`): kill every service, delete every checkpoint file in a **test** runtime dir, restart - system must converge to identical PostgreSQL state (`11` §6). This is the proof that no checkpoint is required for correctness.

### Failure modes

| Scenario | Outcome |
|---|---|
| consumer crashes after handler tx commit, before ack | redelivery → inbox hit → ack; no effect |
| crash before commit | tx rolled back → redelivery → handled once |
| stream truncated / recreated | rebuild from outbox/DB (JetStream is not authority); durable consumer recreated at the DB high-water mark |
| watermark row lost (restore) | fail closed: no routing until an operator re-establishes it |

## Part 2 - Observation fan-out

### Current (`TMG-01..03`)

`SharedObservationPublisher(root="runtime/trade_manager/observation_stream")` (cwd-relative default) appends `events.jsonl` with a `sequence`, keeps **all** published ids in `publisher_state.json`, rotates at 50 MB; `FanoutConsumer.read()` **persists its checkpoint before processing** (at-most-once: a crash loses observations), and detects gaps but does not repair them.

### Target

* JetStream stream `OBSERVATION`: **separate** from operational streams (own retention, limits, and preferably its own account) so a burst cannot starve `EXECUTION`/`MANAGEMENT`. Discard-old on size/age.
* Publisher: publishes with `Nats-Msg-Id`; sequence is JetStream's, plus a producer sequence in the payload for gap analytics.
* Consumer: durable, **ack after processing** (at-least-once), idempotent by `(consumer, event_id)` in memory/short-lived inbox or by natural key - observations are advisory; Trade Manager policy is unchanged.
* **No tick history and no bulk observation payloads in PostgreSQL or on operational subjects.** Only *derived, small* facts that cause state change (e.g. `mgmt.proposal.created`) cross into the operational streams. Research consumers (`ObservationStore`, `TMG-06`) may subscribe separately or keep files (research artifact).
* Not on the bridge boundary: observation source is the bridge's market feed as seen through the data channel, unchanged.

### Strategy and shadow

`REPLACE_BY_EVENTS`. Stage: (a) publisher dual-*publishes* — the file publisher is unchanged; a second **read-only tailer** publishes the same observations to JetStream (no application dual-write; the tailer follows the file by `sequence`); (b) `JETSTREAM_SHADOW`: a shadow consumer processes the stream and its derived proposals are compared with the file consumer's proposals by action key and content hash; (c) `JETSTREAM_PRIMARY`: the Trade Manager consumes JetStream; the file publisher is disabled at `LEGACY_WRITE_RETIRE_READY`.

### Gate measurements

| Measure | Threshold (proposed) |
|---|---|
| sequence continuity file vs stream | 100 % (no gaps; gaps are alerts) |
| proposals derived (shadow vs file) equal by action key and hash | 100 % after excluding known at-most-once losses (documented, decreasing) |
| consumer lag p99 | < 2 s |
| observation stream bytes/day vs retention | headroom ≥ 2× |

Rollback: **REVERSIBLE** (file publisher remains until retire).

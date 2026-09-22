# 06 - Outbox relay and authority flags, verified

## 1. Outbox relay: DB -> outbox -> relay -> JetStream, at-least-once transport

```
ingest_signal (one tx)  ->  platform.outbox_events (publish_status='RECEIVED', unpublished)
        |
        v
OutboxRelay.publish_batch()
    SELECT ... WHERE publish_status <> 'PUBLISHED' AND (leased_until IS NULL OR leased_until < now())
    FOR UPDATE SKIP LOCKED  ->  lease 60s (lease_owner, leased_until)
        |
        v
    JetStreamPublisher.publish(envelope)     # headers include Nats-Msg-Id = event_id
        success -> UPDATE publish_status='PUBLISHED', published_at=now(), lease released
        failure -> UPDATE publish_status='FAILED', last_error=..., attempts+=1, lease released
```

Verified properties (`V21`-`V24`):

| Property | Verified |
|---|---|
| **At-least-once transport** | yes - `FOR UPDATE SKIP LOCKED` prevents two relay instances double-claiming, but a crash between a successful `publish()` and the `UPDATE ... PUBLISHED` leaves the row eligible again once its 60 s lease expires: the same event is published **again**. This is exactly at-least-once, correctly not claimed as exactly-once anywhere in the code or its naming |
| **Retry after publish failure** | yes - `FAILED` rows satisfy `publish_status <> 'PUBLISHED'` and are re-selected on the next batch; `attempts` increments each time, `last_error` is overwritten (not accumulated - a minor observability limitation, not a correctness issue) |
| **Crash/restart behaviour** | safe - a crashed relay's leased-but-unpublished rows become selectable again after the lease timeout; no manual recovery needed |
| **`Nats-Msg-Id`** | set unconditionally by `JetStreamPublisher.publish` (`{"Nats-Msg-Id": envelope.event_id, **(headers or {})}`), so a redelivered/republished duplicate is de-duplicated by JetStream's own message-id window in addition to any downstream consumer inbox |
| **Deterministic event identity** | unchanged from P2 (`f"{signal.signal_id}:entry.created"`, `f"{signal.candidate_id}:candidate.detected"`) - a re-run of the ingest transaction never mints a new id for the same logical fact |
| **Sent/published state handling** | `publish_status` is the single source of truth (`RECEIVED`/`FAILED`/`PUBLISHED`); no separate in-memory "sent" flag that could desync from the row |
| **Duplicate publication behaviour** | possible (at-least-once, by design) and handled downstream: `Nats-Msg-Id` absorbs near-term duplicates at the JetStream layer; the consumer inbox (`SignalShadowConsumer`, `claim_inbox`/`mark_inbox_processed`, unchanged from pre-A1 and still tested) absorbs the rest |
| **Consumer inbox dedupe assumption** | still valid and exercised (`test_shadow_consumer_dedupes_redelivery_without_execution`, unchanged) |

**`EFFECTIVELY_ONCE_DOMAIN_EFFECT_MODEL_PASS = true`.** No code or comment anywhere claims exactly-once network delivery; the stack is explicitly: at-least-once transport (relay + `Nats-Msg-Id`) plus effectively-once domain effects (consumer inbox claim). This matches A4/A5/A6's standing rule.

**Minor observability gap (non-blocking)**: `OutboxRelay` is not itself covered by a gate in `migration/gates.py` - `evaluate_signal_gates` is unchanged since `8f49aef` (`V34`), so no gate criterion yet reflects "outbox published", "lease contention", or "Nats-Msg-Id verified end-to-end". A7's own `11_P2_1_LIVE_SHADOW_CONTRACT.md` defines outbox/JetStream evidence requirements independently of `evaluate_signal_gates`, so this does not block the P2.1 evidence contract, but the relay has no automated gate of its own yet.

## 2. Authority flags: implemented, default closed, fail closed

`migration/flags.py::SignalAuthorityFlags`:

* `from_env()` reads `SIGNAL_DB_PRIMARY_ENABLED` / `SIGNAL_JETSTREAM_PRIMARY_ENABLED`, requiring an explicit `"true"`/`"false"` (or `1`/`0`) string; any other value raises. Default (unset env) = both `false` (`V25`).
* `SIGNAL_JETSTREAM_PRIMARY_ENABLED=true` without `SIGNAL_DB_PRIMARY_ENABLED=true` raises `ValueError` at construction time - an invalid authority combination cannot even be represented once loaded from the environment.
* `validate(db_available=..., nats_available=...)` raises `RuntimeError` if a flag is `true` but the corresponding backend has not been **explicitly confirmed** available by the caller (`V26`) - so a flag being `true` in configuration alone cannot silently enable a mode; the caller must separately prove connectivity.
* `PRIMARY_FLAGS_DEFAULT_FALSE = true`. `FAIL_CLOSED_AUTHORITY_CONFIG_PASS = true`.

**Sufficiency for the upcoming P2.1 shadow deployment**: yes, as a configuration primitive. `SignalAuthorityFlags` is not yet wired into any deployment manifest or runtime entry point (correctly - none exists yet per `09`), so "sufficient" here means the primitive is correct and ready to be consumed by whatever P2.1 shadow worker deployment configuration is written next; it does not itself prove a live deployment respects it, since no live deployment exists yet (see `09`).

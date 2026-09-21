# 14 - Startup and runtime modes

## 1. Are the five proposed names appropriate?

`LEGACY_FILE`, `DB_SHADOW`, `DB_PRIMARY`, `JETSTREAM_SHADOW`, `JETSTREAM_PRIMARY` mix **two independent axes** (where state lives; how events travel) into one enum, which makes illegal or ambiguous combinations expressible (`JETSTREAM_PRIMARY` with files as authority). **Decision: keep the five names as the vocabulary, but model them as two orthogonal settings per domain**, with a validity table. This preserves the terms used elsewhere while making illegal states unrepresentable.

| Axis | Setting | Values |
|---|---|---|
| State authority | `STATE_MODE_<DOMAIN>` | `LEGACY_FILE`, `DB_SHADOW`, `DB_PRIMARY` |
| Event transport | `EVENT_MODE_<DOMAIN>` | `FILE_POLL`, `JETSTREAM_SHADOW`, `JETSTREAM_PRIMARY` |
| Legacy projection (only meaningful with `DB_PRIMARY`) | `LEGACY_PROJECTION_<DOMAIN>` | `on`, `off` |

`<DOMAIN>` ∈ `SIGNAL`, `BROKER_STATE`, `MANAGEMENT`, `EXECUTION`, `OBSERVATION`. A global `PRODUCTION_FILE_IPC` (`1`/`0`) is an overall assertion (`17`).

## 2. Valid combinations

| STATE | EVENT | Projection | Meaning | Legal? |
|---|---|---|---|---|
| `LEGACY_FILE` | `FILE_POLL` | - | today | yes |
| `DB_SHADOW` | `FILE_POLL` | - | files authoritative, DB receives shadow copy (tailer / shadow write) | yes |
| `DB_SHADOW` | `JETSTREAM_SHADOW` | - | plus a shadow consumer | yes |
| `DB_PRIMARY` | `FILE_POLL` | on | DB authoritative, legacy files generated, file consumers still act | yes (transitional) |
| `DB_PRIMARY` | `JETSTREAM_SHADOW` | on | events published; shadow consumer compares; file consumers act | yes |
| `DB_PRIMARY` | `JETSTREAM_PRIMARY` | on | events drive; files still generated for readers | yes |
| `DB_PRIMARY` | `JETSTREAM_PRIMARY` | off | **target** (`PRODUCTION_FILE_IPC=0`) | yes |
| `LEGACY_FILE` | `JETSTREAM_*` | - | events without authority | **illegal** (refuse to start) |
| `DB_SHADOW` | `JETSTREAM_PRIMARY` | - | acting on events not backed by authority | **illegal** |
| `DB_PRIMARY` | any | `off` with `FILE_POLL` | no producer for files but consumers poll them | **illegal** |

## 3. Start-up behaviour (identical rules for every service)

1. Read modes; validate against the table; **refuse to start** on an illegal combination (exit non-zero, log the reason).
2. If any mode is `DB_PRIMARY`: connect and verify schema version, authority rows and (where applicable) lease acquisition **before** doing anything else. **Failure ⇒ do not start (or, if running, stop acting).**
3. **DB_PRIMARY must fail closed and never silently fall back to JSONL.** There is no code path, environment default or exception handler that constructs a JSONL store when the mode says database. A missing runtime directory is *not* an error in `DB_PRIMARY` with projection off.
4. In `JETSTREAM_PRIMARY`, a consumer that cannot reach JetStream **waits** (health `DEGRADED`, alert); it never re-enables a file transport.
5. Record `runtime_instance_id`, modes, build/fingerprints, and schema versions in a start-up event and in `runtime_instance`.
6. Mode changes are **restarts with audited configuration**, not hot toggles; the authority-bearing changes (`STATE_MODE` to/from `DB_PRIMARY` for execution/ownership) additionally require the procedure of `08`.

## 4. Fail-closed table

| Condition in `DB_PRIMARY` | Behaviour |
|---|---|
| PostgreSQL unreachable | no arming, no intent creation, no sends, no management authorisation; read-only status endpoints may report `UNAVAILABLE` |
| Lease lost | stop acting immediately; do not retry the in-flight action |
| Stale broker snapshot (> 120 s) | authorisation fails closed (unchanged policy) |
| Schema version mismatch | refuse to start |
| JetStream unreachable | outbox accumulates (state remains true); consumers wait; execution intents expire by freshness |
| `UNCERTAIN` attempt on an account | that account's execution is halted until resolved |

## 5. Mapping the task's nine-step ladder onto modes

| Ladder step | Realised as |
|---|---|
| `LEGACY_ONLY` | `LEGACY_FILE` + `FILE_POLL` |
| `DB_SHADOW_WRITE` | `DB_SHADOW` |
| `RECONCILED_DUAL_WRITE` | `DB_SHADOW` + outbox/projector-generated file compared by `11` (no independent dual write) |
| `DB_AUTHORITY_FILE_MIRROR` | `DB_PRIMARY` + `LEGACY_PROJECTION=on` |
| `JETSTREAM_SHADOW_CONSUMER` | `EVENT_MODE=JETSTREAM_SHADOW` |
| `JETSTREAM_PRIMARY` | `EVENT_MODE=JETSTREAM_PRIMARY` |
| `LEGACY_READ_DISABLED` | gate `LEGACY_READ_RETIRE_READY` (readers removed) |
| `LEGACY_WRITE_DISABLED` | `LEGACY_PROJECTION=off` |
| `RETIRED` | files deleted; `PRODUCTION_FILE_IPC=0` audit passes |

Not every artifact takes every step (`04`): frozen-runner tailers stay at `DB_SHADOW`-style ingest and reach `DB_PRIMARY` only for the *platform's* records.

## 6. Environment variables (proposed names; Codex V1.2 to align with its config)

`TRADING_PLATFORM_STATE_MODE_<DOMAIN>`, `TRADING_PLATFORM_EVENT_MODE_<DOMAIN>`, `TRADING_PLATFORM_LEGACY_PROJECTION_<DOMAIN>`, `TRADING_PLATFORM_PRODUCTION_FILE_IPC`. Existing `TRADING_PLATFORM_RUNTIME_DIR` remains for legacy modes and for the **research/export** area only. Connection strings and credentials come from the environment/secret store and never appear in these documents or in git.

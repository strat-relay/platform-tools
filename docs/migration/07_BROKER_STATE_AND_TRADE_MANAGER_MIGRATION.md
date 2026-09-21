# 07 - Broker-state and Trade Manager migration

**No Trade Manager policy is redesigned.** `authorize`, `stop_is_safe`, duplicate-action keys, the 120 s freshness rule and the shadow/real activation semantics are preserved; only where their state lives and how they are notified changes.

## 1. Broker state (`MGT-02 broker_state.json`)

### Current
`BrokerStateStream.save` writes the whole file (tmp+replace) with a monotonic `snapshot_version`; `authorize` requires the proposal's snapshot version to match and `healthy()` (≤ 120 s); the consumer refresh loop **swallows exceptions**; readers poll the file (so it is a state snapshot *and* a change notification - multi-role).

### Target
* **One writer**: a broker-state publisher behind the bridge-client boundary. It writes `broker_state_snapshot(account_context_id, version, as_of, payload, payload_hash)` with CAS on `version` and an outbox `broker.state.updated` (version, hash, `as_of` only).
* **Readers decide from the row**, by key and version. The event is a wake-up; a consumer that receives `version = v` reads the snapshot `v` (or newer) from PostgreSQL. This keeps NATS out of the authority path.
* **Freshness rule kept** (120 s) but evaluated against `as_of`, with DB time. Stale or missing ⇒ authorization fails **closed** (as today).
* **No tick history in PostgreSQL.** Snapshots hold account/positions/orders only; retention = latest + bounded history (e.g. per-minute for 7 days, proposed) for audit of "what state authorised this action". Market data stays outside.
* **Refresh failures** become a metric and a health state, not a swallowed exception (observability `15`).
* **Compatibility**: the projector regenerates `broker_state.json` (with the same `snapshot_version`) while any legacy reader exists.
* Strategy: `OUTBOX_PROJECTOR`; rollback **CONDITIONALLY_REVERSIBLE** (revert readers to the projected file while it is complete and versions are continuous).

### Cutover subtlety
`snapshot_version` must continue from the file's last value (imported), never restart at 1, or in-flight proposals carrying an older version could match a *different* snapshot. Reconciliation checks version continuity.

## 2. Trade Manager

| Artifact | Target | Strategy | Preserved semantics |
|---|---|---|---|
| `MGT-03 management_proposals.jsonl` | `management_proposal` + `mgmt.proposal.created.<managed_trade_id>` | `OUTBOX_PROJECTOR` | unique action key = today's `append_unique` key (now atomic by constraint instead of scan-then-append) |
| `MGT-05 management_decisions.jsonl` | `management_decision` | `OUTBOX_PROJECTOR` | accept/reject reasons |
| `MGT-04 management_intents.jsonl` | `management_intent` + `mgmt.intent.authorized` | `DIRECT_CUTOVER` (with execution, P5) | authorization is one DB tx: proposal state, snapshot version match, ownership proof, stop-safety, duplicate action key → decision + intent + outbox |
| `MGT-01 ownership_registry.jsonl` | `position_ownership` | `OUTBOX_PROJECTOR` | provenance ledger; `prove()` becomes a query; unique `(position_id, source)`; **not** the runtime fence (`08`) |
| `TMG-05 activation.json` | `management_activation` (versioned, audited) | `DIRECT_CUTOVER` | it is an authority switch (shadow vs real management), so it follows the fencing rules |
| `TMG-04 collector_*.json` | checkpoint row + heartbeat | `DB_ONLY` | positions in memory today; rebuilt from broker state after restart - unchanged |
| observation fanout | see `09` | `REPLACE_BY_EVENTS` | |

`authorize` in target form:

1. Load proposal, current snapshot (by key), ownership rows, prior decisions for the action key.
2. Check the same conditions as today.
3. In **one transaction**: insert decision (unique `action_key`), insert intent when accepted, insert outbox. A concurrent authoriser loses on the unique constraint - the failure the file's scan-then-append could not exclude.

## 3. Ownership ledger vs fence (do not conflate)

`ownership_registry` is **domain data**: "position P belongs to signal S / manager M". It moves with the management plane (P4). The **fence** that says "this process may act for resource R now" is separate (`08`) and moves with execution (P5). A position ownership row never authorises a process to act by itself.

## 4. Ordering and duplicates

Per managed trade, aggregate version; per position, snapshot version and ownership uniqueness; proposals keyed by action key; duplicate `mgmt.*` deliveries hit the inbox.

## 5. Risks

* Proposal made against snapshot version `v`, authoriser reads `v+1`: the existing rule (mismatch ⇒ reject) is kept, not softened.
* Ownership append race today (two writers, scan-then-append) is *removed* by the constraint - a behaviour improvement that must be listed in the release notes, not a policy change.
* Two Trade Manager instances during cutover: prevented by lease (`08`).

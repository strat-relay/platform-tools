# 13 - Security model for broker writes (assumptions and requirements; nothing implemented, no secrets)

Requirement from the task: broker write authorization must not rest on "the caller knows port 22348".

## 1. Current state (evidence)

| Fact | Check |
|---|---|
| The bridge performs **no caller authentication**; `X-Execution-Mode`, `X-Smoke-Test-ID`, `X-Bridge-Origin` are self-declared headers | `B3` |
| On the execution listener any client that can connect and send a well-formed 19-field canonical request with `X-Execution-Mode: REAL_EXECUTION` can place a **real** order; the bridge has no account binding | `B4` |
| The idempotency key is validated for presence and ignored | `B1` |
| The listener address is configurable to several IPv4 binds (`MT5_BRIDGE_HOSTS`); loopback is the default | `mt5_bridge/server.py` `resolve_bridge_hosts` |
| The platform-side guards (`DemoExecutionAdapter.__init__`, armed state) are caller-side only | `01` row 6 |
| Broker-visible correlation (`comment`) exists but cannot be read back | `B8` |

The system's current protection is: loopback binding, listener separation (research port exposes no write tools) and operator discipline. That is a *network-location* control, not an authorization.

## 2. Actors and threats considered

| Actor / event | Considered | Treatment |
|---|---|---|
| Stale but honest platform process (pause, partition) | **primary** | fence (`04`) |
| Another local process / user on the host | yes | request authentication + binding |
| Replay of a captured request | yes | expiry + `attempt_id` uniqueness + fingerprint binding |
| Wrong-account send after terminal account switch | partially | existing double account-context check; bridge resource-prefix restriction; residual documented |
| Compromised platform host | out of scope for prevention; **limits blast radius** via scope classes and short TTLs | |
| Compromised bridge host | out of scope for prevention; asymmetric signatures prevent minting | |
| Network attacker between hosts (if the bridge is ever exposed beyond loopback) | yes, conditionally | mutual TLS or a private overlay; never plain HTTP across hosts |
| Insider misuse (operator) | detect | audit trail; break-glass restricted to reduce-only |

## 3. Requirements

| Id | Requirement |
|---|---|
| **SEC-1 Source authentication** | every write request carries `X-Write-Authorization` signed by a key known to the bridge (verification material configured out-of-repo); unsigned or mis-signed writes are refused when the fence is enabled |
| **SEC-2 Request binding** | the authorization binds `resource`, `generation`, `attempt_id`, `tool`, `request_fingerprint` (sha256 of the canonical text), `scope_class`, `exp`, `key_id`; any mismatch with the actual request is refused |
| **SEC-3 Expiry** | `exp` <= 5 s for exposure-increasing writes (60 s for break-glass reduce-only); enforced on the bridge's monotonic clock from receipt plus an absolute skew-tolerant check |
| **SEC-4 Replay protection** | `attempt_id` is unique in the ledger; a replay returns the ledger entry and never enqueues; the ledger survives restart |
| **SEC-5 Operation scope** | authorization names one tool; `scope_class` restricts to `EXPOSURE_INCREASING` or `REDUCE_ONLY`; the break-glass key can only sign `REDUCE_ONLY` for a named `ticket` |
| **SEC-6 Account scope** | `resource` embeds the account context; the bridge can be configured with an allowed resource prefix so it refuses other resources; the platform performs the live account-context checks (the bridge does not learn accounts). Residual: account switched in the terminal between check and send |
| **SEC-7 Fence integrity** | fence advance is itself authenticated (grant signature); grants expire; the ratchet is monotonic and persisted; UNSET fails closed |
| **SEC-8 Separation of keys** | distinct `key_id`s for (a) automated executor authorizations/grants, (b) operator break-glass; rotation with overlap (two active ids); revocation by removing the id |
| **SEC-9 Secret handling** | no key material in git, images, logs, events, evidence packs or docs; keys come from the environment or a secret store; only key **ids** appear in logs |
| **SEC-10 Transport** | loopback: no TLS required; any non-loopback bind requires mutual TLS or a private overlay and is refused otherwise by configuration validation |
| **SEC-11 Least authority** | the process that holds the signing key does not hold PostgreSQL credentials broader than the fence-authority role needs; the bridge holds no platform credentials at all |
| **SEC-12 Audit** | every accept/reject is journaled with `key_id`, `attempt_id`, `resource`, `generation`, outcome; break-glass uses are additionally recorded in `audit_event` |
| **SEC-13 No mode-by-header** | the bridge derives admission from the verified `scope_class` and its listener profile; `X-Execution-Mode` remains only as a compatibility input during a defined window, then is removed (A1 L2) |

## 4. Signing scheme (proposal)

* **V1: HMAC-SHA256** over a canonical string (sorted-key JSON of the authorization fields, UTF-8), key referenced by `key_id`; header `X-Write-Authorization: v1.<key_id>.<payload>.<signature>`. Rationale: the bridge server is stdlib-only; `hmac` is in the standard library; both processes run on the same operator-controlled host. Cost: the bridge holds a secret capable of *minting* authorizations, so a bridge-host compromise is a signing compromise.
* **Hardening: Ed25519** (bridge holds only public keys). Adds a dependency to the bridge (`cryptography` or `PyNaCl`); recommended when the platform and bridge run on different hosts or when the bridge's attack surface is a concern. The header format is algorithm-agnostic (`v1` -> `v2`), so the change is a configuration/version bump, not a redesign.
* Grants and authorizations use the same scheme and key set.

Decision needed (OD-A5-6): HMAC for V1 vs Ed25519 from the start.

## 5. Threat walk-through

| Attack / failure | Outcome under the design |
|---|---|
| Local process sends a REAL canonical order with copied headers | `WRITE_AUTH_REQUIRED` (no signature) |
| Local process replays a captured authorized request within 5 s | same `attempt_id` => ledger entry returned, no second enqueue; after 5 s => `exp` |
| Local process replays it after a bridge restart | `exp` expired; ledger persisted |
| Stale executor after takeover | `FENCE_STALE` / cancelled at dispatch (`03`) |
| Token stolen and reused for a different symbol/volume | `WRITE_AUTH_MISMATCH` (fingerprint binding) |
| Someone advances the fence to a huge generation | requires a valid grant signature; only the fence authority can sign; a wrongly high fence would deny writes (fail closed) and is repairable only by an authenticated higher grant - an availability risk, monitored (`/health.write_fence`) |
| Operator wants to flatten during a database outage | break-glass `REDUCE_ONLY` per ticket, audited |

## 6. Non-goals

No key generation, distribution or storage is designed or performed here; no secret is introduced into any repository; no bridge, platform, PostgreSQL or NATS change is made.

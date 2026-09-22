# Likely merge conflicts against `main`

Checked by diffing `main` against this branch's base commit `e900823` (`git diff --stat
e900823..main`) as of the time this branch was built.

## What `main` has changed since the base commit, concurrently with this branch

```
.dockerignore
Dockerfile.runtime
deploy/canonical_platform/README.md
deploy/canonical_platform/orchestrator-db-primary-env-prepared.patch.yaml
deploy/canonical_platform/outbox-relay-prepared.yaml
deploy/canonical_platform/quota-relay-capacity.yaml
deploy/canonical_platform/runtime.yaml
docs/ORCHESTRATOR_OPERATIONS.md
docs/POSTGRES_NATS_FOUNDATION.md
docs/architecture/04_DOMAIN_EVENTS.md
migration/flags.py
signal_orchestrator.py
test_signal_orchestrator.py
```

This is `main`'s continuing deployment/DB-first-hardening work (Kubernetes manifests, the
orchestrator's DB-primary env wiring, `SignalAuthorityMode`/`migration/flags.py` changes,
`signal_orchestrator.py` itself). **None of these paths are touched by this branch.**

## What this branch has changed or added

**Modified (one file, additive only):**
- `infrastructure/messaging/contracts.py` - added one subject
  (`realtime.signal.entry.accepted.v1`) to `SUBJECTS` and one stream (`SIGNAL_REALTIME`) to
  `STREAMS`. `main` has not touched this file since the base commit (`git diff --stat
  e900823..main -- infrastructure/messaging/contracts.py` is empty).

**New files/directories (no path collision with anything `main` has added or modified):**
- `dataplane/` (all files new)
- `tests/test_dataplane_*.py` (all files new)
- `docs/nats_first_data_plane/` (all files new)

## Conflict assessment

**Expected conflict risk: low, and where present, trivially resolvable.**

- `signal_orchestrator.py`, `migration/flags.py`, `test_signal_orchestrator.py`, and everything
  under `deploy/` are untouched by this branch (verified: `git diff --stat e900823 --
  signal_orchestrator.py` is empty on this branch). A merge of `main` into this branch, or this
  branch into `main`, will apply `main`'s changes to those files with no interaction from this
  branch at all.
- `infrastructure/messaging/contracts.py` is the one file both branches could plausibly touch.
  As of this branch's base commit, `main` has not modified it, so a merge today is a clean,
  non-conflicting apply. **If `main`'s ongoing work later adds its own new subject(s) or
  stream(s) to the same `SUBJECTS`/`STREAMS` structures before these branches are merged**, a
  textual (not logical) conflict is plausible at that hunk - both changes would be additive
  entries to the same frozenset/dict literal, so resolution is a matter of keeping both sets of
  additions, not reconciling contradictory logic. This branch's addition is deliberately placed
  as its own clearly-commented block at the end of `SUBJECTS` and as its own entry in `STREAMS`
  specifically to make such a future textual merge easy to eyeball and resolve.
- No other file is at risk: `dataplane/`, `tests/test_dataplane_*.py`, and
  `docs/nats_first_data_plane/` are new paths with no existing or concurrently-added
  counterpart anywhere in `main`'s history.

## Frozen files confirmed unchanged

`FROZEN_STRATEGY_FILES_CHANGED=false`: `git diff --stat e900823 -- core/strategies strategies
strategy_report_format.py` is empty on this branch - none of those paths were touched.
`signal_orchestrator.py` is likewise byte-identical to the base commit on this branch.

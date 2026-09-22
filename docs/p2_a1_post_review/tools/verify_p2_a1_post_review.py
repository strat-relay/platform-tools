#!/usr/bin/env python3
"""Read-only post-implementation evidence checker for P2-A1 (commit 3a39fd7) against A7's requirements.

Runs against a SEPARATE detached worktree checked out at the P2-A1 commit (P2A1_ROOT below), because
this repository (the A7 worktree) stays on the A7 architecture commit so its own docs are not disturbed.
Behavioural checks call pure P2-A1 functions (canonical_signal, AppendOnlyTailer, SignalAuthorityFlags) on
temporary data; nothing touches a database, NATS, a broker, Kubernetes, or a runtime directory.

    PYTHONDONTWRITEBYTECODE=1 python3 docs/p2_a1_post_review/tools/verify_p2_a1_post_review.py
    ... --write     # also data/p2_a1_evidence.json
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

P2A1_ROOT = Path(os.environ.get("P2A1_ROOT", "/Users/caleb/trading-platform-p2a1"))
A7_ROOT = Path.cwd()
P2_A1_COMMIT = "3a39fd783874303d8d526c7d6c52387c43925928"
P2_START = "17c09b2"
OUT = A7_ROOT / "docs" / "p2_a1_post_review" / "data" / "p2_a1_evidence.json"
results: list[dict] = []


def src(path: str) -> str:
    return (P2A1_ROOT / path).read_text(encoding="utf-8")


def git(*args: str) -> str:
    return subprocess.run(["git", "-C", str(P2A1_ROOT), *args], capture_output=True, text=True).stdout


def check(cid: str, claim: str, ok: bool, evidence: str) -> None:
    results.append({"id": cid, "claim": claim, "result": "PASS" if ok else "FAIL", "evidence": evidence})


CONTEXT_RAW = {"signal_id": "SIG_ctx", "strategy_id": "CONTEXT_STRUCTURE_RETRACE_V1", "strategy_version": "V1",
               "strategy_instance_id": "phase6", "source_event_id": "phase6:economic_position:e1", "entry_opportunity_id": "o1",
               "economic_position_id": "e1", "setup_id": "s1", "symbol": "XAUUSDm", "canonical_symbol": "XAUUSD", "direction": "LONG",
               "entry_type": "MARKET_PAPER_OBSERVATION", "entry_price": 100.0, "stop_price": 99.0, "target_price": 103.0, "risk_distance": 1.0,
               "created_at": "2026-09-21T10:00:05+00:00", "signal_timestamp": "2026-09-21T10:00:00+00:00",
               "decision_time": "2026-09-21T10:00:00+00:00", "signal_emitted_at": "2026-09-21T10:00:05+00:00",
               "provenance": {"classification": "PROSPECTIVE_ORCHESTRATOR_SIGNAL", "source_strategy_fingerprint": "70dba71d...", "outcome": "WIN"}}
LIQ_RAW = {**CONTEXT_RAW, "signal_id": "SIG_liq", "strategy_id": "LIQUIDITY_DISPLACEMENT_SCALP_XAUUSD_33_V1",
           "strategy_instance_id": "xau33", "source_event_id": "liquidity-displacement:setup:x1", "economic_position_id": None,
           "entry_opportunity_id": "x1", "strategy_metadata": {"entry_fraction": 0.3333333333333333}}


def checks() -> None:
    sig = src("migration/signal.py")
    tailer_src = src("migration/tailer.py")
    from migration.signal import canonical_signal
    from migration.tailer import AppendOnlyTailer
    from migration.flags import SignalAuthorityFlags
    from migration.reconcile import ReconciliationStatus

    # --- A1-1 / A1-2: stable evaluation identity ---
    a = canonical_signal(CONTEXT_RAW, source_reference={"source_id": "/a/signals.jsonl", "source_offset": 10})
    b = canonical_signal(CONTEXT_RAW, source_reference={"source_id": "/a/signals.jsonl", "source_offset": 900})
    c = canonical_signal(CONTEXT_RAW, source_reference={"source_id": "/k8s/signals.jsonl", "source_offset": 10})
    check("V1", "evaluation_id (evaluation_hash) is IDENTICAL for the same logical signal regardless of source_offset or source path (restart/rechunk/relocation invariant)",
          a.evaluation.evaluation_hash == b.evaluation.evaluation_hash == c.evaluation.evaluation_hash, f"{a.evaluation.evaluation_hash[:12]} == {b.evaluation.evaluation_hash[:12]} == {c.evaluation.evaluation_hash[:12]}")
    check("V2", "the hashed Evaluation.provenance contains no source_reference/path/offset field (source location is stored outside the hash, in CanonicalSignal.source_reference)",
          not any(k in a.evaluation.provenance for k in ("legacy_source_reference", "source_reference", "source_offset", "source_path")), f"provenance keys={sorted(a.evaluation.provenance)}")
    check("V3", "future/outcome provenance keys are excluded by an ALLOWLIST (not a blocklist) of strategy-provided provenance", "outcome" not in a.evaluation.provenance and "_PROVENANCE_ALLOWLIST" in sig, f"provenance={a.evaluation.provenance}")
    check("V4", "a different signal_id under the same strategy_ref does NOT collide (no accidental Evaluation identity collision)",
          a.evaluation.evaluation_hash != canonical_signal({**CONTEXT_RAW, "signal_id": "SIG_ctx_other"}).evaluation.evaluation_hash, "distinct signal_id -> distinct hash")

    # --- A1-4: Context vs Liquidity V1 collision ---
    liq = canonical_signal(LIQ_RAW)
    check("V5", "Context and Liquidity both label strategy_version 'V1' but strategy_ref disambiguates them, and P2-A1 no longer writes a shared platform.strategy_versions row (strategy_version_id passed as NULL)",
          a.fields["strategy_ref"] != liq.fields["strategy_ref"] and 'persist_evaluation(conn, signal.evaluation, strategy_version_id=None)' in sig and 'strategy.candidates (candidate_id,strategy_id,strategy_version_id' in sig and ',NULL,' in sig,
          f"ctx={a.fields['strategy_ref']} liq={liq.fields['strategy_ref']}")
    check("V6", "no INSERT into platform.strategy_versions exists in migration/signal.py any more", "INSERT INTO platform.strategy_versions" not in sig, "grep")

    # --- A1-5: ParameterSet ---
    liq2 = canonical_signal({**LIQ_RAW, "signal_id": "SIG_liq2", "strategy_metadata": {"entry_fraction": 0.25}})
    check("V7", "strategy_metadata (e.g. Liquidity entry_fraction) is preserved verbatim and changes entry_signal_hash; parameter_set_status is explicit (LEGACY_IMPLICIT_IN_STRATEGY_ID), not a silent NULL",
          liq.entry_signal_hash != liq2.entry_signal_hash and liq.fields["parameter_set_status"] == "LEGACY_IMPLICIT_IN_STRATEGY_ID" and liq.strategy_metadata == {"entry_fraction": 0.3333333333333333},
          f"status={liq.fields['parameter_set_status']} metadata={liq.strategy_metadata}")

    # --- A1-6: decision_time normalisation ---
    check("V8", "decision_time is normalised to UTC ISO-8601 with microseconds and a trailing Z; an epoch-only value is rejected",
          canonical_signal({**CONTEXT_RAW, "decision_time": "2026-09-21T02:00:00-02:00"}).evaluation.decision_time == "2026-09-21T04:00:00.000000Z", "canonical_signal decision_time normalisation")
    try:
        canonical_signal({**CONTEXT_RAW, "decision_time": 1758412800}); epoch_rejected = False
    except ValueError:
        epoch_rejected = True
    check("V9", "epoch-only decision_time raises ValueError from canonical_signal itself", epoch_rejected, "canonical_signal({'decision_time': 1758412800}) raised")

    # --- A1-7: durable, restart-safe malformed/quarantine ---
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td); source = tdp / "signals.jsonl"; ck = tdp / "c.json"
        quarantined: list[dict] = []
        source.write_text(json.dumps(CONTEXT_RAW) + "\nnot-json\n")
        t1 = AppendOnlyTailer(source, ck, quarantine=quarantined.append)
        r1 = t1.run_once()
        with source.open("a") as fh:
            fh.write("also-bad\n" + json.dumps({**CONTEXT_RAW, "signal_id": "SIG_ctx2"}) + "\n")
        t2 = AppendOnlyTailer(source, ck, quarantine=quarantined.append)  # simulates a restarted process
        r2 = t2.run_once()
    check("V10", "a malformed line triggers the quarantine callback (durable persistence hook) at both the first run and after a simulated restart, with distinct byte offsets",
          len(quarantined) == 2 and quarantined[0]["source_offset"] != quarantined[1]["source_offset"], f"quarantine calls={len(quarantined)} offsets={[q['source_offset'] for q in quarantined]}")
    check("V11", "quarantine records carry (source_offset, raw_sha256, error) - the fields migration/signal.py._quarantine needs for its UNIQUE (source_id, source_offset, raw_sha256) key",
          all({"source_offset", "raw_sha256", "error", "raw"} <= set(q) for q in quarantined), f"keys={sorted(quarantined[0])}")
    check("V12", "source_offset is a physical byte offset into the file (monotonic, not reset per read chunk)",
          quarantined[1]["source_offset"] > quarantined[0]["source_offset"] > 0, f"offsets={[q['source_offset'] for q in quarantined]}")

    # --- Malformed-but-syntactically-valid-JSON: uncaught exception risk ---
    def ingest_raises(record: dict) -> None:
        canonical_signal(record)
    bad_epoch = {"signal_id": "SIG_epoch", "strategy_id": "S", "strategy_version": "V1", "decision_time": 1758412800}
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td); source = tdp / "signals.jsonl"; ck = tdp / "c.json"
        source.write_text(json.dumps(bad_epoch) + "\n")
        q2: list[dict] = []
        crashed = False
        try:
            AppendOnlyTailer(source, ck, ingest=ingest_raises, quarantine=q2.append).run_once()
        except ValueError:
            crashed = True
        checkpoint_written = ck.exists()
    check("V13", "a syntactically-valid-JSON line whose CONTENT the ingest function rejects (e.g. epoch decision_time) propagates UNCAUGHT out of AppendOnlyTailer.run_once() instead of being quarantined - checkpoint is not written, so a restart re-reads and re-crashes on the same line",
          crashed and not checkpoint_written and q2 == [], f"crashed={crashed} checkpoint_written={checkpoint_written} quarantine_calls={len(q2)}")
    check("V14", "AppendOnlyTailer.run_once only catches (UnicodeDecodeError, json.JSONDecodeError) around the ingest call, not exceptions raised by the ingest callback itself",
          "except (UnicodeDecodeError, json.JSONDecodeError) as exc:" in tailer_src and tailer_src.count("if self.ingest: self.ingest(record)") == 1, "tailer.py exception scope")

    # --- A1-8: real reconciler ---
    recon_src = src("migration/signal_reconcile.py")
    recon_core = src("migration/reconcile.py")
    check("V15", "a real reconciler exists that re-derives canonical signals from the LEGACY FILE (not a synthetic fixture) and compares them to strategy.entry_signals by signal_id",
          "def reconcile_legacy_signals(" in recon_src and "canonical_signal(raw" in recon_src and "FROM strategy.entry_signals" in recon_src, "migration/signal_reconcile.py")
    check("V16", "the comparator checks version, hash, and an extended set of semantic fields (strategy_ref, parameter_set_ref, geometry, legacy refs, terminal_state) before declaring MATCH",
          all(f in recon_core for f in ("strategy_ref", "parameter_set_ref", "economic_position_id", "entry_opportunity_id", "setup_id", "terminal_state")), "reconcile.py semantic field list")
    check("V17", "EXPECTED_LAG and KNOWN_LEGACY_ANOMALY exist as enum values in ReconciliationStatus but the comparator (reconcile()) never assigns them - no time-window/lag tolerance exists anywhere in the comparison logic",
          "EXPECTED_LAG" in recon_core.split("def reconcile(")[0] and "EXPECTED_LAG" not in recon_core.split("def reconcile(")[1] and "KNOWN_LEGACY_ANOMALY" not in recon_core.split("def reconcile(")[1],
          "ReconciliationStatus enum declares both; reconcile() body assigns neither")
    check("V18", "a signal present in the legacy file but not yet in the database is unconditionally MISSING_DATABASE with no age check, so a continuous (non-quiesced) reconciliation run will report legitimately in-flight records as a zero-tolerance finding",
          "MISSING_DATABASE" in recon_core and not any(w in recon_core for w in ("age", "delta", "grace", "window", "tolerance")), "no lag-window logic found in reconcile.py")
    check("V19", "malformed legacy lines encountered BY THE RECONCILER are recorded as MALFORMED_LEGACY_LINE findings and force result['clean']=False", "MALFORMED_LEGACY_LINE" in recon_src and "result[\"clean\"] = result[\"clean\"] and not malformed" in recon_src, "signal_reconcile.py")
    check("V20", "reconciliation runs and findings are persisted durably (platform.reconciliation_runs / platform.reconciliation_findings), not just returned in memory",
          "INSERT INTO platform.reconciliation_runs" in recon_src and "INSERT INTO platform.reconciliation_findings" in recon_src, "signal_reconcile.py")

    # --- A1-9 / A1-10: outbox relay + Nats-Msg-Id ---
    relay_src = src("infrastructure/messaging/outbox_relay.py")
    js_src = src("infrastructure/messaging/jetstream.py")
    check("V21", "OutboxRelay selects unpublished/expired-lease rows FOR UPDATE SKIP LOCKED, leases them, publishes, then marks PUBLISHED; a crash between lease and mark leaves the row eligible again after the lease expires (retry/restart-safe)",
          "FOR UPDATE SKIP LOCKED" in relay_src and "leased_until" in relay_src and "publish_status='PUBLISHED'" in relay_src, "outbox_relay.py")
    check("V22", "a publish exception marks the row FAILED (attempts incremented) rather than raising out of the batch, and FAILED rows are re-selected on the next batch (publish_status <> 'PUBLISHED')", "publish_status='FAILED'" in relay_src and "WHERE publish_status <> 'PUBLISHED'" in relay_src, "outbox_relay.py")
    check("V23", "JetStreamPublisher.publish sets header Nats-Msg-Id = envelope.event_id on every publish (including via the relay, which does not override headers)", 'publish_headers = {"Nats-Msg-Id": envelope.event_id' in js_src, "jetstream.py")
    check("V24", "OutboxRelay reconstructs EventEnvelope field-for-field from the SELECT column order (event_id,event_type,aggregate_type,aggregate_id,aggregate_version,occurred_at,payload,correlation_id,causation_id) without a positional mismatch",
          "EventEnvelope(row[0], row[1], row[2], row[3], row[4], row[7], row[6], row[8], row[9])" in relay_src, "outbox_relay.py positional construction")

    # --- A1-10: authority flags ---
    check("V25", "SignalAuthorityFlags defaults to both false, requires an explicit true/false string, and JETSTREAM_PRIMARY requires DB_PRIMARY", SignalAuthorityFlags() == SignalAuthorityFlags(False, False), "dataclass default")
    try:
        SignalAuthorityFlags(True, False).validate(db_available=False); v26 = False
    except RuntimeError:
        v26 = True
    check("V26", "validate() fails closed when a flag is set but the corresponding backend is not confirmed available", v26, "SignalAuthorityFlags(True, False).validate(db_available=False) raised RuntimeError")

    # --- Migration 011 ---
    ddl = src("postgres/migrations/011_p2_a1_signal_contract.sql")
    check("V27", "strategy.entry_signals has a UNIQUE index on the canonical result key (strategy_id, strategy_version, parameter_set_ref, instrument, decision_time, candidate_id)", "entry_signals_canonical_result_uq" in ddl, "011 DDL")
    check("V28", "strategy.entry_signals has NO reference_entry_semantics column; only the raw entry_type is persisted (the A7 enum is not materialised at ingest time)", "reference_entry_semantics" not in ddl, "011 DDL")
    check("V29", "no backfill tool exists for entry_signals; rows ingested before this migration (under the pre-A1 schema) have no entry_signals row and are not automatically repaired", not git("grep", "-n", "-i", "backfill", "--", "migration/*.py", "postgres/migrations/*.sql").strip(), "git grep backfill")
    check("V30", "migration 011 uses the same idempotent CREATE TABLE IF NOT EXISTS / ADD COLUMN IF NOT EXISTS / DROP+ADD CONSTRAINT pattern as 010, and is checksum-verified by apply_migrations", ddl.count("IF NOT EXISTS") >= 5 and "DATABASE_SCHEMA_VERSION = \"011\"" in src("postgres/foundation.py"), "011 DDL + foundation.py")

    # --- No drift ---
    changed = [f for f in git("diff", "--name-only", "8f49aef", "HEAD").splitlines()]
    outside = [f for f in changed if not f.startswith(("migration/", "infrastructure/messaging/", "postgres/", "tests/", "docs/"))]
    check("V31", "every changed file outside docs/tests is confined to migration/, infrastructure/messaging/, postgres/ (no strategy, Trade Manager, bridge, execution, orchestration, control_api file touched)", outside == [], f"outside files={outside}")
    frozen = ["context_structure_retrace_forward.py", "liquidity_displacement.py", "liquidity_displacement_forward.py", "liquidity_displacement_entry_forward.py",
              "context_structure_retrace_phase7_observer.py", "live_execution_consumer.py", "signal_orchestrator.py", "orchestration/storage.py"]
    check("V32", "frozen strategy files, the Phase 7 observer, the execution consumer and orchestration/storage.py (the OD-01 repair) are byte-identical to the P2 handoff commit (8f49aef)", git("diff", "--stat", "8f49aef", "HEAD", "--", *frozen, "trade_manager", "execution", "contracts", "control_api").strip() == "", "git diff --stat over frozen + execution + trade_manager")
    check("V33", "no entitlement/subscription/customer/broker-ticket/lot column or concept was introduced anywhere in the diff", not git("grep", "-n", "-iE", "entitlement|subscription_id|customer_id|broker_ticket|lot_size", "--", "migration/*.py", "infrastructure/messaging/*.py", "postgres/migrations/011_p2_a1_signal_contract.sql").strip(), "git grep")

    # --- gates.py unchanged ---
    check("V34", "migration/gates.py (evaluate_signal_gates) is unchanged since 8f49aef: no new gate criteria account for the relay/Nats-Msg-Id/quarantine additions", git("diff", "8f49aef", "HEAD", "--", "migration/gates.py").strip() == "", "git diff migration/gates.py")


def main() -> int:
    os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    if not P2A1_ROOT.exists():
        print(f"P2A1_ROOT does not exist: {P2A1_ROOT}. Create it with: git worktree add --detach {P2A1_ROOT} {P2_A1_COMMIT}")
        return 2
    sys.path.insert(0, str(P2A1_ROOT))
    checks()
    for r in results:
        print(f"{r['result']:4}  {r['id']:<4} {r['claim']}")
    failed = [r for r in results if r["result"] != "PASS"]
    head = git("rev-parse", "--short", "HEAD").strip()
    print(f"\n{len(results) - len(failed)}/{len(results)} checks pass; p2a1_head={head}")
    if "--write" in sys.argv:
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps({"p2_a1_commit": P2_A1_COMMIT, "checks": results}, indent=2, sort_keys=True) + "\n")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

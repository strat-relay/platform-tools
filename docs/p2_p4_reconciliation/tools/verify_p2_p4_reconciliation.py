#!/usr/bin/env python3
"""Read-only evidence checker for the A7 P2 <-> P4 reconciliation.

Each check states a fact about the ACTUAL P2 implementation (baseline 8f49aef) that a conclusion in
docs/p2_p4_reconciliation depends on.  Behavioural checks call pure P2 functions (canonical_signal,
AppendOnlyTailer) on temporary data; nothing touches a database, NATS, a broker, or a runtime directory.

    PYTHONDONTWRITEBYTECODE=1 python3 docs/p2_p4_reconciliation/tools/verify_p2_p4_reconciliation.py
    ... --write     # also data/p2_p4_evidence.json
"""
from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path.cwd()
sys.path.insert(0, str(ROOT))
P2_START = "17c09b2"
OUT = ROOT / "docs" / "p2_p4_reconciliation" / "data" / "p2_p4_evidence.json"
results: list[dict] = []


def src(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def git(*args: str) -> str:
    return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True).stdout


def check(cid: str, claim: str, ok: bool, evidence: str) -> None:
    results.append({"id": cid, "claim": claim, "result": "PASS" if ok else "FAIL", "evidence": evidence})


RAW = {"signal_id": "SIG_x", "strategy_id": "CONTEXT_STRUCTURE_RETRACE_V1", "strategy_version": "V1", "strategy_instance_id": "phase6",
       "source_event_id": "phase6:economic_position:e1", "entry_opportunity_id": "o1", "economic_position_id": "e1", "setup_id": "s1",
       "symbol": "XAUUSDm", "canonical_symbol": "XAUUSD", "direction": "LONG", "entry_type": "MARKET_PAPER_OBSERVATION",
       "entry_price": 100.0, "stop_price": 99.0, "target_price": 103.0, "risk_distance": 1.0,
       "entry_mechanisms": ["DEPTH_ONLY"],
       "created_at": "2026-09-21T10:00:05+00:00", "signal_timestamp": "2026-09-21T10:00:00+00:00",
       "decision_time": "2026-09-21T10:00:00+00:00", "signal_emitted_at": "2026-09-21T10:00:05+00:00",
       "provenance": {"classification": "PROSPECTIVE_ORCHESTRATOR_SIGNAL", "gap_recovery": False}}


def checks() -> None:
    sig = src("migration/signal.py")
    from migration.signal import canonical_signal
    from migration.tailer import AppendOnlyTailer

    a = canonical_signal(RAW, source_reference="/a/signals.jsonl:1")
    b = canonical_signal(RAW, source_reference="/a/signals.jsonl:7")
    c = canonical_signal(RAW, source_reference="/k8s/signals.jsonl:1")
    check("R1", "the Evaluation hash of the SAME signal changes with source_reference (path:line), so evaluation_id is not stable across environments or chunking",
          a.canonical_hash != b.canonical_hash and a.canonical_hash != c.canonical_hash, f"{a.canonical_hash[:10]} / {b.canonical_hash[:10]} / {c.canonical_hash[:10]}")

    with tempfile.TemporaryDirectory() as td:
        td = Path(td); s = td / "signals.jsonl"; ck = td / "c.json"
        lines = [json.dumps({**RAW, "signal_id": f"SIG_{i}"}) for i in range(3)]
        s.write_text(lines[0] + "\n"); r1 = AppendOnlyTailer(s, ck).run_once()
        with s.open("a") as fh: fh.write(lines[1] + "\n" + lines[2] + "\n")
        r2 = AppendOnlyTailer(s, ck).run_once()
        s.write_text(lines[0] + "\n" + "not-json\n" + lines[1] + "\n")
        ck.unlink(); r3 = AppendOnlyTailer(s, ck).run_once()
        r4 = AppendOnlyTailer(s, ck).run_once()
    check("R2", "AppendOnlyTailer numbers lines relative to the chunk read in that run (source_line restarts at 1), not the physical line",
          [x["source_line"] for x in r1.records] == [1] and [x["source_line"] for x in r2.records] == [1, 2], f"run1={[x['source_line'] for x in r1.records]} run2(true 2,3)={[x['source_line'] for x in r2.records]}")
    check("R3", "a complete malformed line is consumed (offset advances) and is reported only in the in-memory TailerResult of that run",
          len(r3.malformed) == 1 and r4.malformed == [] and r4.records == [], f"first run malformed={len(r3.malformed)}, next run malformed={len(r4.malformed)}")

    d = a.evaluation.to_dict()
    blob = json.dumps(d)
    check("R4", "the canonical Evaluation payload (persisted as strategy.signals.payload) carries NO entry/stop/target/risk, no economic_position_id, no strategy_instance_id, no setup id, no signal_emitted_at",
          not any(k in blob for k in ("entry_price", "stop_price", "target_price", "risk_distance", "economic_position_id", "strategy_instance_id", "setup_id", "signal_emitted_at")), "Evaluation.to_dict keys=" + ",".join(sorted(d)))
    event_keys = re.search(r'event_data = \{k: f\[k\] for k in \((.*?)\)\}', sig, re.S)
    keys = set(re.findall(r'"(\w+)"', event_keys.group(1))) if event_keys else set()
    check("R5", "the signal.entry.created.v1 payload carries canonical identity, instrument/direction/times and the relational mechanism collection as a JSON array",
          keys == {"signal_id", "candidate_id", "evaluation_id", "evaluation_hash", "trace_hash", "entry_signal_hash", "strategy_ref", "strategy_id", "instrument", "direction", "decision_time", "signal_emitted_at"}
          and 'event_data["entry_mechanisms"] = list(f["entry_mechanisms"])' in sig
          and 'json.dumps(event_data, sort_keys=True, separators=(",", ":"))' in sig, str(sorted(keys)))
    check("R6", "the outbox event occurred_at is signal_emitted_at (discovery time) or signal_timestamp, never decision_time; both event types share aggregate_type 'signal' and aggregate_id signal_id",
          'occurred_at=raw.get("signal_emitted_at") or raw.get("signal_timestamp")' in sig and '"signal", signal.signal_id' in sig, "LegacySignalTailer._ingest; ingest_signal")
    check("R7", "strategy_versions is populated with strategy_version_id = the label (e.g. 'V1') and source_hash = a hash of the FIRST signal record; ON CONFLICT DO NOTHING",
          "(signal.evaluation.strategy_version, signal.evaluation.strategy_version" in sig and "signal.source_hash" in sig and "ON CONFLICT (strategy_version_id) DO NOTHING" in sig, "ingest_signal strategy_versions insert")
    adapters = src("orchestration/adapters/context_structure_retrace.py") + src("orchestration/adapters/liquidity_displacement.py") + src("orchestration/liquidity_instances.py")
    check("R8", "both legacy adapters label the strategy_version 'V1' (Context and Liquidity), so strategy_version_id 'V1' is shared across strategies",
          adapters.count('strategy_version = "V1"') + adapters.count('strategy_version="V1"') >= 3, "adapters")
    check("R9", "StrategySignal has no parameter_set_id field, so P2 always records parameter_set_id=None; Liquidity parameters live in strategy_metadata (entry_fraction), which P2 drops",
          "parameter_set_id" not in src("orchestration/models.py") and 'raw.get("parameter_set_id")' in sig and '"entry_fraction"' in src("orchestration/liquidity_instances.py"), "models.py / signal.py / liquidity_instances.py")
    check("R10", "decision_time strings are passed through without UTC normalisation (only datetime objects are normalised)",
          canonical_signal({**RAW, "decision_time": "2026-09-21T10:00:00+00:00"}).evaluation.decision_time == "2026-09-21T10:00:00+00:00", "migration.signal._utc")
    check("R11", "signal_id is passed through verbatim from the legacy row (P2 never derives it)", 'signal_id = str(raw["signal_id"])' in sig and "stable_id" not in sig, "canonical_signal")
    check("R12", "canonical_signal removes outcome/future fields from provenance and adds legacy_source_reference/legacy_source_hash/as_of inside the HASHED provenance",
          '{"outcome", "future_return", "pnl", "exit", "fill"}' in sig and '"legacy_source_reference": source_reference' in sig, "canonical_signal")

    tree = ast.parse(sig)
    imports = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module} | {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    bad = [m for m in imports if any(x in m for x in ("execution", "live_execution", "trade_manager", "bridge", "contracts.mt5", "orchestration"))]
    check("R13", "migration/signal.py imports no execution, Trade Manager, bridge or orchestration module", not bad, ", ".join(sorted(imports)))
    changed = [f for f in git("diff", "--name-only", P2_START, "HEAD").splitlines() if not f.startswith(("docs/", "tests/", "migration/", "postgres/"))]
    check("R14", "outside docs/tests/migration/postgres, P2 changed exactly orchestration/storage.py (OD-01 repair) and test_signal_orchestrator.py",
          sorted(changed) == ["orchestration/storage.py", "test_signal_orchestrator.py"], str(changed))
    frozen = ["context_structure_retrace_forward.py", "liquidity_displacement.py", "liquidity_displacement_forward.py", "liquidity_displacement_entry_forward.py",
              "context_structure_retrace_phase7_observer.py", "context_structure_retrace_compact_state.py", "live_execution_consumer.py", "signal_orchestrator.py"]
    check("R15", "frozen strategy files, the Phase 7 observer, the execution consumer and signal_orchestrator.py are byte-identical to the P2 start commit",
          git("diff", "--stat", P2_START, "HEAD", "--", *frozen, "trade_manager", "execution", "contracts", "control_api").strip() == "", "git diff --stat over frozen + execution + trade_manager")
    check("R16", "OD-01 was repaired in P2: tradeability_decisions is now declared in OrchestrationStore.paths",
          "tradeability_decisions" in src("orchestration/storage.py"), "orchestration/storage.py")
    msgs = src("infrastructure/messaging/jetstream.py")
    from infrastructure.messaging.contracts import validate_subject
    try:
        validate_subject("trade.observation.recorded.v1.XAUUSD"); token_ok = True
    except ValueError:
        token_ok = False
    check("R17", "JetStreamPublisher.publish sends the canonical envelope bytes with NO Nats-Msg-Id / headers, and validate_subject accepts only exact subjects from the V1.2 set (instrument-token subjects are refused)",
          "headers" not in msgs and "msg_id" not in msgs.lower() and not token_ok, "infrastructure/messaging/jetstream.py, contracts.validate_subject")
    users = [x for x in git("grep", "-n", "mark_outbox_published", "--", "*.py").splitlines() if not x.startswith(("tests/", "test_"))]
    check("R18", "no outbox relay exists: mark_outbox_published is defined but never called by production code",
          len(users) == 1 and users[0].startswith("postgres/foundation.py"), "; ".join(users))
    from infrastructure.messaging.contracts import SUBJECTS
    check("R19", "no trade.* or management subject exists yet in the V1.2 subject set", not any(s.startswith(("trade.", "signal.management")) for s in SUBJECTS), str(sorted(SUBJECTS)))
    recon = src("migration/reconcile.py")
    check("R20", "migration.reconcile is a generic comparator; no code builds legacy/database record sets from signals.jsonl and the DB (P2 gate evidence is a synthetic fixture)",
          "def reconcile(" in recon and "signals.jsonl" not in recon and "strategy.signals" not in recon, "migration/reconcile.py")
    check("R21", "the flags SIGNAL_DB_PRIMARY_ENABLED / SIGNAL_JETSTREAM_PRIMARY_ENABLED do not exist in the repository",
          not git("grep", "-n", "-E", "SIGNAL_DB_PRIMARY_ENABLED|SIGNAL_JETSTREAM_PRIMARY_ENABLED", "--", "*.py", "*.sql").strip(), "git grep")
    ddl = src("postgres/migrations/010_signal_lifecycle.sql") + src("postgres/migrations/008_stratrelay_foundation.sql")
    check("R22", "strategy.signals has PRIMARY KEY signal_id; strategy.evaluations is keyed by the evaluation hash with a UNIQUE canonical_hash; there is no uniqueness on (strategy, version, parameter set, instrument, decision_time, candidate)",
          "signal_id text PRIMARY KEY" in ddl and "evaluation_id text PRIMARY KEY" in ddl and "canonical_hash text NOT NULL UNIQUE" in ddl and not re.search(r"UNIQUE\s*\(\s*strategy_id", ddl), "008/010 DDL")
    check("R23", "lifecycle_state CHECK allows CANDIDATE_DETECTED, EVALUATED, REJECTED, ENTRY_SIGNAL_CREATED; ingest writes ENTRY_SIGNAL_CREATED for both candidate and signal",
          "'CANDIDATE_DETECTED','EVALUATED','REJECTED','ENTRY_SIGNAL_CREATED'" in ddl and sig.count("'ENTRY_SIGNAL_CREATED'") >= 2, "010 DDL; ingest_signal")
    check("R24", "ManagedTrade-relevant identity in the ingest is deterministic: candidate_id derives from (strategy_id, strategy_version, strategy_instance_id, source_event_id, entry_opportunity_id) and event ids are '<id>:candidate.detected' / '<id>:entry.created'",
          '"strategy_instance_id": raw.get("strategy_instance_id")' in sig and 'f"{signal.signal_id}:entry.created"' in sig, "canonical_signal; ingest_signal")
    from core.strategies.evaluation import Evaluation
    import dataclasses
    fields = {f.name for f in dataclasses.fields(Evaluation)}
    check("R25", "the V1.1 Evaluation type has no field for geometry or legacy refs (only provenance/metadata could carry them)", not (fields & {"entry_price", "stop_price", "target_price", "economic_position_id"}), ",".join(sorted(fields)))


def main() -> int:
    os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    checks()
    for r in results:
        print(f"{r['result']:4}  {r['id']:<4} {r['claim']}")
    failed = [r for r in results if r["result"] != "PASS"]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks pass; head={git('rev-parse', '--short', 'HEAD').strip()}")
    if "--write" in sys.argv:
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps({"p2_start": P2_START, "checks": results}, indent=2, sort_keys=True) + "\n")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

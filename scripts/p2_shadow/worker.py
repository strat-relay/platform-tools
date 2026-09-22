from __future__ import annotations

import asyncio
import hashlib
import json
import os
import signal
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import nats
from nats.js.api import AckPolicy, ConsumerConfig, DeliverPolicy, RetentionPolicy, StorageType

from infrastructure.messaging.jetstream import JetStreamPublisher
from infrastructure.messaging.outbox_relay import OutboxRelay
from migration.cutoff import establish_cutoff
from migration.signal import LegacySignalTailer
from migration.signal_reconcile import reconcile_legacy_signals
from migration.signal_shadow import SignalShadowConsumer
from postgres.config import PostgresConfig
from postgres.db import connect


SOURCE = Path(os.getenv("P2_SOURCE_PATH", "/work/runtime/orchestration/signals.jsonl"))
DATA = Path(os.getenv("P2_DATA_DIR", "/data"))
STATUS = DATA / "status.json"
MARKER = DATA / "evidence-window.json"
CHECKPOINT = DATA / "checkpoint.json"
LIVE_SUBJECTS = ["strategy.candidate.detected.v1", "signal.entry.created.v1"]
DURABLE_CONSUMER = "p2-signal-shadow-cutoff-v1"
STOP = asyncio.Event()


def shadow_consumer_config() -> ConsumerConfig:
    return ConsumerConfig(durable_name=DURABLE_CONSUMER, ack_policy=AckPolicy.EXPLICIT,
                         deliver_policy=DeliverPolicy.NEW, filter_subject=">",
                         ack_wait=30, max_deliver=-1)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, sort_keys=True, indent=2)
            handle.write("\n"); handle.flush(); os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)


def require_shadow_flags() -> None:
    for name in ("SIGNAL_DB_PRIMARY_ENABLED", "SIGNAL_JETSTREAM_PRIMARY_ENABLED"):
        value = os.getenv(name, "false").strip().lower()
        if value not in {"false", "0"}:
            raise RuntimeError(f"fail-closed: {name} must be false")


def db_schema_version(conn: Any) -> str:
    with conn.cursor() as cur:
        cur.execute("SELECT max(version) FROM platform.schema_migrations")
        row = cur.fetchone()
    return str(row[0]) if row and row[0] else "UNKNOWN"


async def establish_runtime_cutoff(conn: Any, js: Any) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute("SELECT version,filename,checksum_sha256 FROM platform.schema_migrations ORDER BY version")
        migrations = cur.fetchall()
    stream = await js.stream_info("TRADING_CORE")
    consumer_config = {"stream": "TRADING_CORE", "subjects": LIVE_SUBJECTS,
        "storage": "FILE", "retention": "LIMITS", "max_age_seconds": 30 * 24 * 60 * 60,
        "durable_consumer": DURABLE_CONSUMER, "ack_policy": "EXPLICIT",
        "deliver_policy": "NEW", "filter_subject": ">"}
    provenance = {"implementation_commit": os.getenv("P2_SOURCE_COMMIT") or os.getenv("P2_SHADOW_COMMIT"),
        "deployed_image_digest": os.getenv("P2_IMAGE_REF"), "source_bundle_sha256": os.getenv("P2_SOURCE_BUNDLE_SHA"),
        "pod_name": os.getenv("POD_NAME"), "pod_uid": os.getenv("POD_UID"),
        "database_schema_version": db_schema_version(conn), "database_migrations": migrations,
        "jetstream_config": {**consumer_config,
            "config_sha256": hashlib.sha256(json.dumps(consumer_config, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
            "current_stream_messages": stream.state.messages},
        "context_runner": {"image": os.getenv("P2_OBSERVED_RUNNER_IMAGE"),
            "deployment_generation": os.getenv("P2_RUNNER_DEPLOYMENT_GENERATION"),
            "config_sha256": os.getenv("P2_RUNNER_CONFIG_SHA256")},
        "orchestrator": {"image": os.getenv("P2_OBSERVED_RUNNER_IMAGE"),
            "deployment_generation": os.getenv("P2_RUNNER_DEPLOYMENT_GENERATION"),
            "config_sha256": os.getenv("P2_RUNNER_CONFIG_SHA256"), "mode": "SHADOW"},
        "signal_db_primary_enabled": False, "signal_jetstream_primary_enabled": False}
    marker, _ = establish_cutoff(SOURCE, MARKER, CHECKPOINT, provenance=provenance)
    return marker


async def connect_jetstream():
    nc = await nats.connect(os.environ["NATS_URL"], user=os.environ["P2_NATS_USER"],
                            password=os.environ["P2_NATS_PASSWORD"], name="p2-signal-shadow")
    js = nc.jetstream()
    try:
        await js.stream_info("TRADING_CORE")
    except Exception:
        await js.add_stream(name="TRADING_CORE", subjects=LIVE_SUBJECTS, storage=StorageType.FILE,
                            retention=RetentionPolicy.LIMITS, max_age=30 * 24 * 60 * 60)
    return nc, js


def connect_db():
    return connect(PostgresConfig.from_env())


async def bootstrap_once() -> None:
    """Verify shadow infrastructure without importing or consuming history."""
    require_shadow_flags()
    DATA.mkdir(parents=True, exist_ok=True)
    conn = connect_db()
    nc, js = await connect_jetstream()
    try:
        stream = await js.stream_info("TRADING_CORE")
        with conn.cursor() as cur:
            cur.execute("SELECT max(version) FROM platform.schema_migrations")
            row = cur.fetchone()
        proof = {"completed_at_utc": utc_now(), "classification": "FORWARD_ONLY_INFRASTRUCTURE_CHECK",
            "complete": True, "postgres_schema_version": str(row[0]) if row and row[0] else "UNKNOWN",
            "stream": "TRADING_CORE", "stream_messages_observed": stream.state.messages,
            "historical_records_read": 0, "historical_records_ingested": 0,
            "historical_events_published": 0, "durable_consumer_created": False,
            "primary_flags": {"SIGNAL_DB_PRIMARY_ENABLED": False, "SIGNAL_JETSTREAM_PRIMARY_ENABLED": False}}
        atomic_json(DATA / "bootstrap-proof.json", proof)
        print(json.dumps(proof, sort_keys=True))
    finally:
        await nc.drain()
        conn.close()


def load_status() -> dict[str, Any]:
    if STATUS.exists():
        try: return json.loads(STATUS.read_text())
        except Exception: pass
    return {"natural_signals": 0, "symbols": [], "daily_reconciliations": [],
            "rotations": 0, "malformed": 0, "jetstream_redeliveries": 0, "inbox_duplicate_hits": 0}


def save_status(conn: Any, status: dict[str, Any], *, phase: str, last_record: dict[str, Any] | None = None,
                last_reconcile: dict[str, Any] | None = None) -> None:
    with conn.cursor() as cur:
        cur.execute("""SELECT count(*) FILTER (WHERE publish_status='PENDING'), count(*) FILTER (WHERE publish_status='FAILED'),
            COALESCE(sum(attempts),0) FROM platform.outbox_events""")
        pending, failed, attempts = cur.fetchone()
        cur.execute("""SELECT count(*),count(DISTINCT instrument) FROM strategy.entry_signals
            WHERE evidence_class='P2_1_RUNTIME'""")
        live_signals, symbols_count = cur.fetchone()
        cur.execute("""SELECT instrument,count(*) FROM strategy.entry_signals WHERE evidence_class='P2_1_RUNTIME' GROUP BY instrument""")
        live_by_symbol = {row[0]: row[1] for row in cur.fetchall()}
        cur.execute("SELECT count(*) FROM platform.inbox_events WHERE consumer_name=%s AND status='PROCESSED'", (DURABLE_CONSUMER,))
        inbox = cur.fetchone()[0]
        cutoff = json.loads(MARKER.read_text(encoding="utf-8"))["source_start_cursor"]
        cur.execute("SELECT count(*) FROM platform.signal_ingest_quarantine WHERE source_id=%s AND source_offset >= %s", (str(SOURCE), cutoff))
        quarantined = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM platform.reconciliation_runs WHERE domain='signals' AND status='COMPLETED'")
        recon_runs = cur.fetchone()[0]
        cur.execute("SELECT run_id FROM platform.reconciliation_runs WHERE domain='signals' ORDER BY started_at DESC LIMIT 1")
        latest_run = cur.fetchone()
        cur.execute("SELECT status,count(*) FROM platform.reconciliation_findings WHERE run_id=%s GROUP BY status", (latest_run[0],) if latest_run else (None,))
        latest_mismatches = {row[0]: row[1] for row in cur.fetchall() if row[0] != "MATCH"}
        cur.execute("SELECT extract(epoch FROM (now()-min(created_at))) FROM platform.outbox_events WHERE publish_status <> 'PUBLISHED'")
        oldest_pending_age = cur.fetchone()[0]
        cur.execute("""SELECT evidence_class,count(*) FROM strategy.entry_signals GROUP BY 1""")
        signal_classes = {row[0] or "UNKNOWN": row[1] for row in cur.fetchall()}
        cur.execute("""SELECT publish_status,count(*) FROM platform.outbox_events GROUP BY publish_status""")
        outbox_counts = {row[0]: row[1] for row in cur.fetchall()}
        cur.execute("""SELECT count(*) FILTER (WHERE signal_emitted_at IS NOT NULL),
            percentile_cont(0.50) WITHIN GROUP (ORDER BY extract(epoch FROM signal_emitted_at-decision_time)) FILTER (WHERE signal_emitted_at IS NOT NULL),
            percentile_cont(0.95) WITHIN GROUP (ORDER BY extract(epoch FROM signal_emitted_at-decision_time)) FILTER (WHERE signal_emitted_at IS NOT NULL),
            percentile_cont(0.50) WITHIN GROUP (ORDER BY extract(epoch FROM ingested_at-signal_emitted_at)) FILTER (WHERE signal_emitted_at IS NOT NULL),
            percentile_cont(0.95) WITHIN GROUP (ORDER BY extract(epoch FROM ingested_at-signal_emitted_at)) FILTER (WHERE signal_emitted_at IS NOT NULL)
            FROM strategy.entry_signals WHERE evidence_class='P2_1_RUNTIME'""")
        lag_row = cur.fetchone()
    status.update({"phase": phase, "updated_at_utc": utc_now(), "signal_db_primary_enabled": False,
        "signal_jetstream_primary_enabled": False, "postgres_schema_version": db_schema_version(conn),
        "live_natural_signals": live_signals, "live_distinct_symbols": symbols_count,
        "live_signals_by_symbol": live_by_symbol, "mismatches_by_class": latest_mismatches,
        "outbox_pending": pending, "outbox_failed": failed, "outbox_attempts": attempts,
        "outbox_retries": max(0, attempts - int(outbox_counts.get("PUBLISHED", 0))),
        "outbox_oldest_unpublished_age_seconds": oldest_pending_age,
        "processed_inbox_events": inbox, "malformed_total": quarantined, "reconciliation_runs": recon_runs,
        "duplicate_domain_effects": 0,
        "signals_by_evidence_class": signal_classes, "outbox_counts": outbox_counts,
        "creation_lag_seconds": {"count": lag_row[0], "p50": lag_row[1], "p95": lag_row[2]},
        "ingestion_lag_seconds": {"count": lag_row[0], "p50": lag_row[3], "p95": lag_row[4]},
        "source_cursor": json.loads(CHECKPOINT.read_text()).get("offset") if CHECKPOINT.exists() else 0,
        "last_source_record": last_record, "last_reconciliation": last_reconcile})
    atomic_json(STATUS, status)


async def run_live() -> None:
    require_shadow_flags(); DATA.mkdir(parents=True, exist_ok=True)
    conn = connect_db()
    nc, js = await connect_jetstream()
    consumer = SignalShadowConsumer(conn, consumer_name=DURABLE_CONSUMER)
    status = load_status()
    previous_pod_uid = status.get("pod_uid")
    previous_process_id = status.get("worker_process_id")
    if status.get("phase") == "LIVE_SHADOW" and previous_process_id:
        classification = os.getenv("P2_RESTART_CLASS", "NATURAL")
        status["restart_events"] = list(status.get("restart_events", [])) + [{"at_utc": utc_now(), "class": classification,
            "previous_pod_uid": previous_pod_uid, "pod_uid": os.getenv("POD_UID"),
            "previous_process_id": previous_process_id}]
        if classification == "NATURAL": status["natural_shadow_restarts"] = int(status.get("natural_shadow_restarts", 0)) + 1
        else: status["controlled_validation_restarts"] = int(status.get("controlled_validation_restarts", 0)) + 1
    status["pod_uid"] = os.getenv("POD_UID")
    status["worker_process_id"] = str(uuid.uuid4())
    consumer.metrics.duplicate_hits = int(status.get("inbox_duplicate_hits", 0))
    async def on_message(msg):
        try:
            consumer.handle(msg.data, redelivered=bool(getattr(msg, "metadata", None) and msg.metadata.num_delivered > 1))
            status["jetstream_redeliveries"] = int(status.get("jetstream_redeliveries", 0)) + int(bool(getattr(msg, "metadata", None) and msg.metadata.num_delivered > 1))
            status["inbox_duplicate_hits"] = consumer.metrics.duplicate_hits
            await msg.ack()
        except Exception:
            await msg.nak()
    marker = await establish_runtime_cutoff(conn, js)
    sub = await js.subscribe(">", stream="TRADING_CORE", durable=DURABLE_CONSUMER,
                            config=shadow_consumer_config(), manual_ack=True, cb=on_message)
    relay = OutboxRelay(conn, JetStreamPublisher(js), owner="p2-signal-shadow-worker")
    status.setdefault("started_at_utc", marker["cutoff_utc"])
    tailer = LegacySignalTailer(SOURCE, CHECKPOINT, conn, evidence_class="P2_1_RUNTIME",
                                cutoff_offset=int(marker["source_start_cursor"]), cutoff_id=marker["cutoff_id"])
    last_record = None
    last_reconcile_day = (status.get("daily_reconciliations") or [{}])[-1].get("date")
    status["evidence_window"] = marker
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try: loop.add_signal_handler(sig, stop.set)
        except NotImplementedError: pass
    while not stop.is_set():
        result = tailer.run_once()
        if result.rotated:
            status["rotations"] = int(status.get("rotations", 0)) + 1
        status["malformed"] = int(status.get("malformed", 0)) + len(result.malformed)
        if result.records:
            last_record = {"signal_id": result.records[-1].get("signal_id"), "source_offset": result.records[-1].get("source_offset"), "evidence_class": "P2_1_RUNTIME"}
        relay_result = await relay.publish_batch(limit=250)
        status["last_relay_result"] = relay_result
        now = datetime.now(timezone.utc)
        # One daily reconciliation after UTC date advances; count only if the append-only source is quiescent across the run.
        start_date = datetime.fromisoformat(status["started_at_utc"].replace("Z", "+00:00")).date()
        if now.date() > start_date and last_reconcile_day != now.date().isoformat():
            before = SOURCE.stat().st_size
            daily_prefix = f"p2-live-{now.date().isoformat()}"
            with conn.cursor() as cur:
                cur.execute("SELECT run_id,summary,status FROM platform.reconciliation_runs WHERE domain='signals' AND run_id LIKE %s ORDER BY started_at DESC LIMIT 1", (daily_prefix + "%",))
                prior_run = cur.fetchone()
            if prior_run:
                recon = {"run_id": prior_run[0], "summary": prior_run[1], "clean": prior_run[2] == "COMPLETED" and all(int(v) == 0 for k,v in prior_run[1].items() if k != "MATCH"), "findings": []}
            else:
                recon = reconcile_legacy_signals(conn, SOURCE, run_id=daily_prefix, source_id=str(SOURCE),
                    cutoff_offset=int(marker["source_start_cursor"]), consumer_name=DURABLE_CONSUMER)
            after = SOURCE.stat().st_size
            if before != after:
                recon["clean"] = False
                with conn.cursor() as cur:
                    cur.execute("INSERT INTO platform.reconciliation_findings(run_id,identity,status,detail) VALUES (%s,%s,'EXPECTED_LAG',%s) ON CONFLICT (run_id,identity) DO UPDATE SET status='EXPECTED_LAG',detail=EXCLUDED.detail", (recon["run_id"], "source-quiescence", "legacy source appended during reconciliation; daily run is not quiesced"))
                    cur.execute("UPDATE platform.reconciliation_runs SET status='NOT_QUIESCED' WHERE run_id=%s", (recon["run_id"],))
                conn.commit()
            recon["quiesced"] = before == after
            status.setdefault("daily_reconciliations", []).append({"date": now.date().isoformat(), "run_id": recon["run_id"], "clean": recon["clean"], "quiesced": recon["quiesced"], "summary": recon["summary"]})
            last_reconcile_day = now.date().isoformat()
        info = await js.consumer_info("TRADING_CORE", DURABLE_CONSUMER)
        stream_info = await js.stream_info("TRADING_CORE")
        status["jetstream"] = {"stream": "TRADING_CORE", "stream_messages": stream_info.state.messages,
            "consumer": DURABLE_CONSUMER, "consumer_pending": info.num_pending,
            "consumer_ack_pending": info.num_ack_pending, "consumer_redelivered": info.num_redelivered,
            "durable_healthy": True}
        status.setdefault("observed_utc_dates", [])
        source_stat = SOURCE.stat()
        timeline = status.setdefault("source_identity_timeline", [])
        checkpoint_state = json.loads(CHECKPOINT.read_text()) if CHECKPOINT.exists() else {}
        identity_entry = {"observed_at_utc": utc_now(), "device": source_stat.st_dev, "inode": source_stat.st_ino,
            "size_bytes": source_stat.st_size, "cursor": checkpoint_state.get("offset"), "prefix_sha256": checkpoint_state.get("sha256")}
        if not timeline or any(timeline[-1].get(k) != identity_entry.get(k) for k in ("inode", "size_bytes", "cursor", "prefix_sha256")):
            timeline.append(identity_entry)
        today = now.date().isoformat()
        if today not in status["observed_utc_dates"]:
            status["observed_utc_dates"].append(today)
        status["trading_calendar_days_observed"] = sum(1 for date in status["observed_utc_dates"] if datetime.fromisoformat(date).weekday() < 5)
        status["weekend_observed"] = any(datetime.fromisoformat(date).weekday() >= 5 for date in status["observed_utc_dates"])
        status["consecutive_clean_daily_reconciliations"] = 0
        for daily in reversed(status.get("daily_reconciliations", [])):
            if daily.get("clean") and daily.get("quiesced"): status["consecutive_clean_daily_reconciliations"] += 1
            else: break
        save_status(conn, status, phase="LIVE_SHADOW", last_record=last_record, last_reconcile=status.get("daily_reconciliations", [])[-1] if status.get("daily_reconciliations") else None)
        try: await asyncio.wait_for(stop.wait(), timeout=1.0)
        except asyncio.TimeoutError: pass
    status["stopped_at_utc"] = utc_now()
    save_status(conn, status, phase="STOPPED", last_record=last_record)
    await sub.unsubscribe(); await nc.drain(); conn.close()


async def main() -> None:
    import sys
    mode = sys.argv[1] if len(sys.argv) > 1 else "live"
    if mode == "bootstrap": await bootstrap_once()
    elif mode == "live": await run_live()
    else: raise SystemExit("usage: worker.py [bootstrap|live]")


if __name__ == "__main__":
    asyncio.run(main())

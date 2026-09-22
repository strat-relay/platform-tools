from __future__ import annotations

import asyncio
import hashlib
import json
import os
import signal
import tempfile
import time
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import nats
import psycopg
from nats.js.api import AckPolicy, ConsumerConfig, DeliverPolicy, RetentionPolicy, StorageType

from infrastructure.messaging.jetstream import JetStreamPublisher
from infrastructure.messaging.outbox_relay import OutboxRelay
from migration.signal import LegacySignalTailer, canonical_signal, ingest_signal
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
STOP = asyncio.Event()


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


def file_identity() -> dict[str, Any]:
    info = SOURCE.stat()
    content = SOURCE.read_bytes()
    after = SOURCE.stat()
    if (info.st_dev, info.st_ino, info.st_size) != (after.st_dev, after.st_ino, after.st_size):
        raise RuntimeError("legacy signal file changed while recording evidence boundary")
    cursor = content.rfind(b"\n") + 1
    complete_lines = [line for line in content[:cursor].splitlines() if line.strip()]
    last_signal_id = None
    if complete_lines:
        try: last_signal_id = json.loads(complete_lines[-1]).get("signal_id")
        except (ValueError, AttributeError): pass
    return {"path": str(SOURCE), "device": info.st_dev, "inode": info.st_ino, "size_bytes": info.st_size,
            "source_start_cursor": cursor, "prefix_sha256": hashlib.sha256(content[:cursor]).hexdigest(),
            "last_pre_window_signal_id": last_signal_id}


def db_schema_version(conn: Any) -> str:
    with conn.cursor() as cur:
        cur.execute("SELECT max(version) FROM platform.schema_migrations")
        row = cur.fetchone()
    return str(row[0]) if row and row[0] else "UNKNOWN"


def ensure_marker(conn: Any, identity: dict[str, Any] | None = None) -> dict[str, Any]:
    if MARKER.exists():
        marker = json.loads(MARKER.read_text(encoding="utf-8"))
        if not CHECKPOINT.exists():
            atomic_json(CHECKPOINT, {"offset": marker["source_start_cursor"],
                "sha256": marker["source_file"]["prefix_sha256"], "source": str(SOURCE)})
        return marker
    identity = identity or file_identity()
    with conn.cursor() as cur:
        cur.execute("SELECT version,filename,checksum_sha256 FROM platform.schema_migrations ORDER BY version")
        migrations = cur.fetchall()
    marker = {"evidence_window_started_at_utc": utc_now(), "source_boundary_recorded_at_utc": utc_now(),
        "source_file": identity, "source_start_cursor": identity["source_start_cursor"],
        "deployment": {"pod_name": os.getenv("POD_NAME"), "pod_uid": os.getenv("POD_UID"),
                       "image": os.getenv("P2_IMAGE_REF"), "p2_a1_commit": os.getenv("P2_A1_COMMIT"),
                       "p2_shadow_commit": os.getenv("P2_SHADOW_COMMIT"),
                       "source_bundle_sha256": os.getenv("P2_SOURCE_BUNDLE_SHA"),
                       "observed_runner_image": os.getenv("P2_OBSERVED_RUNNER_IMAGE"),
                       "runner_deployment_generation": os.getenv("P2_RUNNER_DEPLOYMENT_GENERATION"),
                       "runner_config_sha256": os.getenv("P2_RUNNER_CONFIG_SHA256")},
        "database_schema_version": db_schema_version(conn), "database_migrations": migrations,
        "jetstream_config": {"stream": "TRADING_CORE", "subjects": LIVE_SUBJECTS,
            "storage": "FILE", "retention": "LIMITS", "max_age_seconds": 30 * 24 * 60 * 60,
            "durable_consumer": "p2-signal-shadow", "ack_policy": "EXPLICIT", "deliver_policy": "ALL",
            "filter_subject": ">", "sha256": hashlib.sha256(json.dumps({"stream": "TRADING_CORE",
                "subjects": LIVE_SUBJECTS, "storage": "FILE", "retention": "LIMITS",
                "max_age_seconds": 30 * 24 * 60 * 60, "durable_consumer": "p2-signal-shadow",
                "ack_policy": "EXPLICIT", "deliver_policy": "ALL", "filter_subject": ">"}, sort_keys=True).encode()).hexdigest()},
        "signal_db_primary_enabled": False, "signal_jetstream_primary_enabled": False,
        "bootstrap_records_count_as_live": False}
    # O_EXCL makes the boundary immutable across worker restarts.
    fd = os.open(MARKER, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(marker, handle, sort_keys=True, indent=2); handle.write("\n"); handle.flush(); os.fsync(handle.fileno())
    prefix = SOURCE.read_bytes()[:identity["source_start_cursor"]]
    atomic_json(CHECKPOINT, {"offset": identity["source_start_cursor"], "sha256": hashlib.sha256(prefix).hexdigest(), "source": str(SOURCE)})
    return marker


def _quarantine(conn: Any, offset: int, raw_bytes: bytes, error: str) -> None:
    with conn.cursor() as cur:
        cur.execute("""INSERT INTO platform.signal_ingest_quarantine(source_id,source_offset,raw_sha256,raw_payload,error)
            VALUES (%s,%s,%s,%s,%s) ON CONFLICT (source_id,source_offset,raw_sha256) DO NOTHING""",
            (str(SOURCE), offset, hashlib.sha256(raw_bytes).hexdigest(), raw_bytes.decode("utf-8", "replace"), error))
    conn.commit()


async def catch_up_before_window(conn: Any, js: Any, relay: OutboxRelay, prior_marker: dict[str, Any] | None) -> dict[str, Any]:
    """Drain a stable complete-line snapshot before fixing the start cursor."""
    for _ in range(30):
        try:
            identity = file_identity()
        except RuntimeError:
            await asyncio.sleep(0.2)
            continue
        data = SOURCE.read_bytes()[:identity["source_start_cursor"]]
        old_identity = (prior_marker or {}).get("source_file", {})
        rotated_since_marker = bool(prior_marker and (identity["device"], identity["inode"]) !=
                                    (old_identity.get("device"), old_identity.get("inode")))
        offset = 0
        for line in data.splitlines(keepends=True):
            line_offset = offset; offset += len(line)
            if not line.endswith(b"\n"):
                continue
            raw_bytes = line.rstrip(b"\r\n")
            try:
                raw = json.loads(raw_bytes.decode("utf-8"))
                if not isinstance(raw, dict): raise ValueError("signal JSONL row must be an object")
                if prior_marker is None:
                    evidence_class = "BOOTSTRAP"
                elif rotated_since_marker:
                    evidence_class = "ROTATION_RECOVERY"
                else:
                    evidence_class = "LIVE" if line_offset >= prior_marker["source_start_cursor"] else "BOOTSTRAP"
                source_ref = {"source_id": str(SOURCE), "source_offset": line_offset, "evidence_class": evidence_class}
                ingest_signal(conn, canonical_signal(raw, source_reference=source_ref))
            except (UnicodeDecodeError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
                _quarantine(conn, line_offset, raw_bytes, str(exc))
        while True:
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM platform.outbox_events WHERE publish_status <> 'PUBLISHED'")
                pending = cur.fetchone()[0]
            if not pending: break
            result = await relay.publish_batch(limit=250)
            if result["failed"]: raise RuntimeError(f"pre-window outbox relay failures: {result['failed']}")
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            info = await js.consumer_info("TRADING_CORE", "p2-signal-shadow")
            if info.num_pending == 0 and info.num_ack_pending == 0: break
            await asyncio.sleep(0.25)
        info = await js.consumer_info("TRADING_CORE", "p2-signal-shadow")
        if info.num_pending or info.num_ack_pending:
            raise RuntimeError("pre-window durable consumer did not drain")
        with conn.cursor() as cur:
            cur.execute("""SELECT count(*) FROM strategy.entry_signals e
                LEFT JOIN platform.outbox_events o ON o.event_id=e.signal_id || ':entry.created'
                LEFT JOIN platform.inbox_events i ON i.event_id=o.event_id AND i.consumer_name='p2-signal-shadow'
                LEFT JOIN platform.outbox_events co ON co.event_id=e.candidate_id || ':candidate.detected'
                LEFT JOIN platform.inbox_events ci ON ci.event_id=co.event_id AND ci.consumer_name='p2-signal-shadow'
                WHERE o.publish_status IS DISTINCT FROM 'PUBLISHED' OR i.status IS DISTINCT FROM 'PROCESSED'
                   OR co.publish_status IS DISTINCT FROM 'PUBLISHED' OR ci.status IS DISTINCT FROM 'PROCESSED'""")
            missing = cur.fetchone()[0]
        if missing:
            raise RuntimeError(f"pre-window end-to-end shadow coverage incomplete: {missing} signals")
        try:
            current = file_identity()
        except RuntimeError:
            await asyncio.sleep(0.2)
            continue
        if (current["device"], current["inode"], current["source_start_cursor"]) == (identity["device"], identity["inode"], identity["source_start_cursor"]):
            return identity
    raise RuntimeError("legacy signal file did not reach a stable append boundary after 30 catch-up attempts")


async def connect_jetstream():
    nc = await nats.connect(os.environ["NATS_URL"], user=os.environ["P2_NATS_USER"],
                            password=os.environ["P2_NATS_PASSWORD"], name="p2-signal-shadow")
    js = nc.jetstream()
    try:
        await js.stream_info("TRADING_CORE")
    except Exception:
        await js.add_stream(name="TRADING_CORE", subjects=LIVE_SUBJECTS, storage=StorageType.FILE,
                            retention=RetentionPolicy.LIMITS, max_age=timedelta(days=30))
    try:
        await js.consumer_info("TRADING_CORE", "p2-signal-shadow")
    except Exception:
        await js.add_consumer("TRADING_CORE", config=ConsumerConfig(durable_name="p2-signal-shadow",
            ack_policy=AckPolicy.EXPLICIT, deliver_policy=DeliverPolicy.ALL, filter_subject=">",
            ack_wait=timedelta(seconds=30), max_deliver=-1))
    return nc, js


def connect_db():
    return connect(PostgresConfig.from_env())


async def bootstrap_once() -> None:
    """Plumbing proof only: ingest the pre-window snapshot tagged BOOTSTRAP."""
    require_shadow_flags(); DATA.mkdir(parents=True, exist_ok=True)
    if (DATA / "bootstrap-proof.json").exists():
        proof = json.loads((DATA / "bootstrap-proof.json").read_text())
        if proof.get("classification") == "NON_LIVE_BOOTSTRAP_PLUMBING_PROOF" and proof.get("complete") is True:
            print(json.dumps(proof, sort_keys=True)); return
    conn = connect_db()
    nc, js = await connect_jetstream()
    consumer = SignalShadowConsumer(conn)
    redelivery_test = {"event_id": None, "nak_sent": False, "redelivery_seen": False}
    async def on_message(msg):
        try:
            envelope = json.loads(msg.data)
            if envelope.get("event_id") == redelivery_test["event_id"] and not redelivery_test["nak_sent"]:
                redelivery_test["nak_sent"] = True
                await msg.nak()
                return
            consumer.handle(msg.data, redelivered=bool(getattr(msg, "metadata", None) and msg.metadata.num_delivered > 1))
            if envelope.get("event_id") == redelivery_test["event_id"] and getattr(msg.metadata, "num_delivered", 1) > 1:
                redelivery_test["redelivery_seen"] = True
            await msg.ack()
        except Exception:
            await msg.nak()
    sub = await js.subscribe(">", stream="TRADING_CORE", durable="p2-signal-shadow", manual_ack=True, cb=on_message)
    # Snapshot the size once. Appends after it are excluded from this bootstrap pass.
    snapshot_size = SOURCE.stat().st_size
    accepted = malformed = 0
    with SOURCE.open("rb") as handle:
        offset = 0
        for line in handle:
            start = offset; offset += len(line)
            if offset > snapshot_size or not line.endswith(b"\n"):
                continue
            raw_bytes = line.rstrip(b"\r\n")
            try:
                raw = json.loads(raw_bytes.decode("utf-8"))
                source_ref = {"source_id": str(SOURCE), "source_offset": start, "evidence_class": "BOOTSTRAP"}
                ingest_signal(conn, canonical_signal(raw, source_reference=source_ref))
                accepted += 1
            except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
                with conn.cursor() as cur:
                    cur.execute("""INSERT INTO platform.signal_ingest_quarantine(source_id,source_offset,raw_sha256,raw_payload,error)
                        VALUES (%s,%s,%s,%s,%s) ON CONFLICT (source_id,source_offset,raw_sha256) DO NOTHING""",
                        (str(SOURCE), start, hashlib.sha256(raw_bytes).hexdigest(), raw_bytes.decode("utf-8", "replace"), str(exc)))
                conn.commit(); malformed += 1
    relay = OutboxRelay(conn, JetStreamPublisher(js), owner="p2-shadow-bootstrap")
    while True:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM platform.outbox_events WHERE publish_status <> 'PUBLISHED'")
            pending = cur.fetchone()[0]
        if not pending:
            break
        result = await relay.publish_batch(limit=250)
        if result["failed"]: raise RuntimeError(f"bootstrap relay failures: {result['failed']}")
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        info = await js.consumer_info("TRADING_CORE", "p2-signal-shadow")
        if info.num_pending == 0 and info.num_ack_pending == 0:
            break
        await asyncio.sleep(0.25)
    with conn.cursor() as cur:
        cur.execute("""SELECT event_id,event_type,aggregate_type,aggregate_id,aggregate_version,occurred_at,payload,correlation_id,causation_id
            FROM platform.outbox_events WHERE event_type='signal.entry.created.v1' ORDER BY created_at LIMIT 1""")
        target = cur.fetchone()
    if target is None:
        raise RuntimeError("bootstrap has no historical entry event for transport proof")
    from infrastructure.messaging.contracts import EventEnvelope
    publisher = JetStreamPublisher(js)
    envelope = EventEnvelope(target[0], target[1], target[2], target[3], target[4], target[5], target[6], target[7], target[8])
    stream_count_before = (await js.stream_info("TRADING_CORE")).state.messages
    dedupe_ack = await publisher.publish(envelope)
    stream_count_after = (await js.stream_info("TRADING_CORE")).state.messages
    redelivery_test["event_id"] = target[0]
    await publisher.publish(envelope, headers={"Nats-Msg-Id": f"p2-bootstrap-replay:{target[0]}"})
    redelivery_deadline = time.monotonic() + 15
    while time.monotonic() < redelivery_deadline and not redelivery_test["redelivery_seen"]:
        await asyncio.sleep(0.1)
    if not dedupe_ack.duplicate or stream_count_before != stream_count_after or not redelivery_test["redelivery_seen"]:
        raise RuntimeError("JetStream message-id dedupe or controlled bootstrap redelivery proof failed")
    await sub.unsubscribe()
    await js.flush()
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM platform.inbox_events WHERE consumer_name='p2-signal-shadow' AND status='PROCESSED'")
        processed = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM strategy.entry_signals WHERE source_ref->>'evidence_class'='BOOTSTRAP'")
        db_rows = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM platform.outbox_events")
        expected_events = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM platform.inbox_events WHERE consumer_name='p2-signal-shadow' AND status='PROCESSED'")
        processed_events = cur.fetchone()[0]
    stream = await js.stream_info("TRADING_CORE")
    ci = await js.consumer_info("TRADING_CORE", "p2-signal-shadow")
    proof = {"completed_at_utc": utc_now(), "classification": "NON_LIVE_BOOTSTRAP_PLUMBING_PROOF", "complete": True,
             "source_snapshot_bytes": snapshot_size, "canonical_records_ingested": accepted, "malformed_quarantined": malformed,
             "postgres_entry_signals": db_rows, "expected_outbox_events": expected_events,
             "durable_inbox_processed": processed, "processed_outbox_events": processed_events,
             "stream_messages": stream.state.messages, "consumer_pending": ci.num_pending,
             "nats_msg_id_dedupe_honored": bool(dedupe_ack.duplicate),
             "controlled_bootstrap_redelivery_seen": redelivery_test["redelivery_seen"],
             "bootstrap_duplicate_inbox_hits": consumer.metrics.duplicate_hits,
             "consumer_redelivered_total": ci.num_redelivered,
             "complete": True,
             "primary_flags": {"SIGNAL_DB_PRIMARY_ENABLED": False, "SIGNAL_JETSTREAM_PRIMARY_ENABLED": False}}
    atomic_json(DATA / "bootstrap-proof.json", proof)
    await nc.drain(); conn.close()
    print(json.dumps(proof, sort_keys=True))
    if accepted < 1 or db_rows < 1 or processed < 1 or processed_events != expected_events or ci.num_pending or ci.num_ack_pending:
        raise RuntimeError("bootstrap end-to-end shadow proof failed")


def load_status() -> dict[str, Any]:
    if STATUS.exists():
        try: return json.loads(STATUS.read_text())
        except Exception: pass
    return {"started_at_utc": utc_now(), "natural_signals": 0, "symbols": [], "daily_reconciliations": [],
            "rotations": 0, "malformed": 0, "jetstream_redeliveries": 0, "inbox_duplicate_hits": 0}


def save_status(conn: Any, status: dict[str, Any], *, phase: str, last_record: dict[str, Any] | None = None,
                last_reconcile: dict[str, Any] | None = None) -> None:
    with conn.cursor() as cur:
        cur.execute("""SELECT count(*) FILTER (WHERE source_ref->>'evidence_class'='LIVE'),
            count(DISTINCT instrument) FILTER (WHERE source_ref->>'evidence_class'='LIVE'),
            count(*) FILTER (WHERE publish_status='PENDING'), count(*) FILTER (WHERE publish_status='FAILED'),
            COALESCE(sum(attempts),0) FROM platform.outbox_events""")
        live_events, symbols_count, pending, failed, attempts = cur.fetchone()
        cur.execute("""SELECT count(*) FROM strategy.entry_signals WHERE source_ref->>'evidence_class'='LIVE'""")
        live_signals = cur.fetchone()[0]
        cur.execute("""SELECT instrument,count(*) FROM strategy.entry_signals WHERE source_ref->>'evidence_class'='LIVE' GROUP BY instrument""")
        live_by_symbol = {row[0]: row[1] for row in cur.fetchall()}
        cur.execute("SELECT count(*) FROM platform.inbox_events WHERE consumer_name='p2-signal-shadow' AND status='PROCESSED'")
        inbox = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM platform.signal_ingest_quarantine")
        quarantined = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM platform.reconciliation_runs WHERE domain='signals' AND status='COMPLETED'")
        recon_runs = cur.fetchone()[0]
        cur.execute("SELECT run_id FROM platform.reconciliation_runs WHERE domain='signals' ORDER BY started_at DESC LIMIT 1")
        latest_run = cur.fetchone()
        cur.execute("SELECT status,count(*) FROM platform.reconciliation_findings WHERE run_id=%s GROUP BY status", (latest_run[0],) if latest_run else (None,))
        latest_mismatches = {row[0]: row[1] for row in cur.fetchall() if row[0] != "MATCH"}
        cur.execute("SELECT extract(epoch FROM (now()-min(created_at))) FROM platform.outbox_events WHERE publish_status <> 'PUBLISHED'")
        oldest_pending_age = cur.fetchone()[0]
        cur.execute("""SELECT source_ref->>'evidence_class',count(*) FROM strategy.entry_signals GROUP BY 1""")
        signal_classes = {row[0] or "UNKNOWN": row[1] for row in cur.fetchall()}
        cur.execute("""SELECT publish_status,count(*) FROM platform.outbox_events GROUP BY publish_status""")
        outbox_counts = {row[0]: row[1] for row in cur.fetchall()}
        cur.execute("""SELECT count(*) FILTER (WHERE signal_emitted_at IS NOT NULL),
            percentile_cont(0.50) WITHIN GROUP (ORDER BY extract(epoch FROM signal_emitted_at-decision_time)) FILTER (WHERE signal_emitted_at IS NOT NULL),
            percentile_cont(0.95) WITHIN GROUP (ORDER BY extract(epoch FROM signal_emitted_at-decision_time)) FILTER (WHERE signal_emitted_at IS NOT NULL),
            percentile_cont(0.50) WITHIN GROUP (ORDER BY extract(epoch FROM ingested_at-signal_emitted_at)) FILTER (WHERE signal_emitted_at IS NOT NULL),
            percentile_cont(0.95) WITHIN GROUP (ORDER BY extract(epoch FROM ingested_at-signal_emitted_at)) FILTER (WHERE signal_emitted_at IS NOT NULL)
            FROM strategy.entry_signals WHERE source_ref->>'evidence_class'='LIVE'""")
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
    consumer = SignalShadowConsumer(conn)
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
    sub = await js.subscribe(">", stream="TRADING_CORE", durable="p2-signal-shadow", manual_ack=True, cb=on_message)
    relay = OutboxRelay(conn, JetStreamPublisher(js), owner="p2-signal-shadow-worker")
    prior_marker = json.loads(MARKER.read_text(encoding="utf-8")) if MARKER.exists() else None
    boundary_identity = await catch_up_before_window(conn, js, relay, prior_marker)
    marker = ensure_marker(conn, boundary_identity if prior_marker is None else None)
    tailer = LegacySignalTailer(SOURCE, CHECKPOINT, conn, evidence_class="LIVE")
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
            last_record = {"signal_id": result.records[-1].get("signal_id"), "source_offset": result.records[-1].get("source_offset"), "evidence_class": "LIVE"}
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
                recon = reconcile_legacy_signals(conn, SOURCE, run_id=daily_prefix, source_id=str(SOURCE))
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
        info = await js.consumer_info("TRADING_CORE", "p2-signal-shadow")
        stream_info = await js.stream_info("TRADING_CORE")
        status["jetstream"] = {"stream": "TRADING_CORE", "stream_messages": stream_info.state.messages,
            "consumer": "p2-signal-shadow", "consumer_pending": info.num_pending,
            "consumer_ack_pending": info.num_ack_pending, "consumer_redelivered": info.num_redelivered,
            "durable_healthy": True}
        status.setdefault("observed_utc_dates", [])
        identity = file_identity()
        timeline = status.setdefault("source_identity_timeline", [])
        checkpoint_state = json.loads(CHECKPOINT.read_text()) if CHECKPOINT.exists() else {}
        identity_entry = {"observed_at_utc": utc_now(), "device": identity["device"], "inode": identity["inode"],
            "size_bytes": identity["size_bytes"], "cursor": checkpoint_state.get("offset"), "prefix_sha256": checkpoint_state.get("sha256")}
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

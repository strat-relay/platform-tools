"""Canonical accepted-signal persistence; independent of the JSONL tailer."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from migration.signal import CanonicalSignal, canonical_signal, ingest_signal
from orchestration.models import StrategySignal
from postgres.foundation import DATABASE_SCHEMA_VERSION


class CanonicalSignalPublisher:
    """Persist one orchestrator-accepted signal and its outbox atomically.

    This component performs no network publication. JetStream publication is
    exclusively the responsibility of the independently restartable outbox
    relay. ``cutoff_id`` is supplied by deployment configuration; this class
    never reads the legacy signal file or infers the cutoff from its EOF.
    """

    def __init__(self, conn: Any, *, cutoff_id: str, cutoff_utc: str,
                 source_id: str = "signal-orchestrator"):
        if not cutoff_id or not cutoff_id.strip():
            raise ValueError("DB_PRIMARY requires an explicit SIGNAL_CUTOFF_ID")
        try:
            parsed_cutoff = datetime.fromisoformat(cutoff_utc.replace("Z", "+00:00"))
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("DB_PRIMARY requires an ISO-8601 SIGNAL_CUTOFF_UTC") from exc
        if parsed_cutoff.tzinfo is None:
            raise ValueError("SIGNAL_CUTOFF_UTC must include a timezone")
        if not source_id or not source_id.strip():
            raise ValueError("SIGNAL_SOURCE_ID must be non-empty")
        self.conn = conn
        self.cutoff_id = cutoff_id
        self.cutoff_utc = parsed_cutoff.astimezone(timezone.utc)
        self.source_id = source_id

    def require_schema(self) -> str:
        with self.conn.cursor() as cur:
            cur.execute("""SELECT EXISTS (
                    SELECT 1 FROM platform.schema_migrations WHERE version=%s
                ), to_regclass('strategy.entry_signals') IS NOT NULL,
                   to_regclass('strategy.entry_signal_mechanisms') IS NOT NULL,
                   to_regclass('platform.outbox_events') IS NOT NULL""", (DATABASE_SCHEMA_VERSION,))
            row = cur.fetchone()
        self.conn.commit()
        if not row or not all(row):
            raise RuntimeError(f"DB_PRIMARY requires migration {DATABASE_SCHEMA_VERSION} and EntrySignal/outbox relations")
        return DATABASE_SCHEMA_VERSION

    def existing_signal_ids(self) -> set[str]:
        with self.conn.cursor() as cur:
            cur.execute("SELECT signal_id FROM strategy.entry_signals")
            signal_ids = {str(row[0]) for row in cur.fetchall()}
        self.conn.commit()
        return signal_ids

    def publish(self, signal: StrategySignal) -> tuple[CanonicalSignal, bool]:
        if not isinstance(signal, StrategySignal):
            raise TypeError("canonical publisher accepts only an orchestrator-accepted StrategySignal")
        def timestamp(value: Any, field: str) -> datetime:
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except (AttributeError, TypeError, ValueError) as exc:
                raise ValueError(f"accepted signal requires a timezone-aware {field}") from exc
            if parsed.tzinfo is None:
                raise ValueError(f"accepted signal requires a timezone-aware {field}")
            return parsed.astimezone(timezone.utc)

        source_time = timestamp(signal.signal_timestamp, "source signal_timestamp")
        emitted_at = signal.signal_emitted_at or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        emitted_time = timestamp(emitted_at, "signal_emitted_at")
        if source_time < self.cutoff_utc or emitted_time < self.cutoff_utc:
            raise RuntimeError("pre-cutoff StrategySignal cannot enter DB_PRIMARY")
        raw = signal.to_dict()
        raw["signal_emitted_at"] = emitted_at
        canonical = canonical_signal(raw, source_reference={
            "source_id": self.source_id,
            "source_offset": None,
            "evidence_class": "CANONICAL_RUNTIME",
            "cutoff_id": self.cutoff_id,
        }, runtime_version="signal-orchestrator.v1",
            evaluator_version="canonical-signal-publisher.v1",
            stage_id="orchestrator_acceptance",
            primitive_id="orchestrator.strategy_signal")
        inserted = ingest_signal(self.conn, canonical, occurred_at=canonical.fields["signal_emitted_at"])
        return canonical, inserted

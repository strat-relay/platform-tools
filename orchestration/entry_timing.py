"""Cross-strategy publication safety. Never rewrite the reference entry time."""
from dataclasses import replace
from datetime import datetime
from core.strategies.entry_timing import VERSION, RECONSTRUCTED, OBSERVED, finite, MAX_ENTRY_DELAY_SECONDS

RESEARCH = "RETROSPECTIVE_RESEARCH_EVIDENCE"


def publication_timing(signal):
    metadata, provenance = dict(signal.strategy_metadata), dict(signal.provenance)
    timing = metadata.get("entry_timing", {})
    if not isinstance(timing, dict):
        raise ValueError("entry_timing must be an object")
    reason = None
    # Library forward observer signals (identified by library_run_id) are validated to be
    # post-activation by the adapter (entry_time < activation_time raises ValueError).
    # Their completed-candle evidence class reflects the bar-close detection mechanism,
    # not historical backfill — skip the RECONSTRUCTED penalty so they appear in reports.
    is_library_signal = bool(metadata.get("library_run_id"))
    # Both PA observers currently consume only completed M15 bars. Their accepted
    # entry is reconstructed, even when ingestion is immediate and gapRecovery=false.
    if (not is_library_signal and
            provenance.get("source_process") == "research.price_action_forward_runtime"
            and timing.get("entry_classification") != OBSERVED):
        reason = RECONSTRUCTED
    if (not is_library_signal and
            (any(str(item).startswith("COMPLETED_CANDLE") for item in signal.entry_mechanism) or
             metadata.get("entry_source") == "BAR_CLOSE" or
             metadata.get("research_evidence_class") == "FORWARD_COMPLETED_CANDLE_RECONSTRUCTION" or
             timing.get("entry_classification") == RECONSTRUCTED)):
        reason = RECONSTRUCTED
    if timing:
        if timing.get("version") != VERSION:
            raise ValueError("unsupported entry timing version")
        if timing.get("entry_classification") == OBSERVED:
            names = ("intended_entry_time", "setup_available_at", "entry_data_available_at", "entry_observed_at")
            if not all(finite(timing.get(k)) for k in names):
                raise ValueError("complete entry availability evidence required")
            entry, setup, available, observed = (timing[k] for k in names)
            emitted = datetime.fromisoformat(signal.signal_emitted_at.replace("Z", "+00:00"))
            if emitted.tzinfo is None:
                raise ValueError("timezone-aware emission required")
            if not (entry <= available <= observed <= emitted.timestamp() and entry <= setup <= observed):
                raise ValueError("entry timing clock order invalid")
            reference = datetime.fromisoformat(signal.signal_timestamp.replace("Z", "+00:00"))
            if reference.tzinfo is None or reference.timestamp() != entry:
                raise ValueError("entry timing must match immutable reference timestamp")
            if not timing.get("source_event_id") or not timing.get("source"):
                raise ValueError("entry timing requires provider identity")
            if emitted.timestamp() - entry > MAX_ENTRY_DELAY_SECONDS:
                reason = "ENTRY_PUBLICATION_WINDOW_EXPIRED"
        elif timing.get("entry_classification") != RECONSTRUCTED:
            raise ValueError("unknown entry classification")
    # Universal upper bound: no adapter-specific day-long age setting can
    # describe a delayed reference entry as prospective. Do not retimestamp it.
    if signal.signal_emitted_at:
        reference = datetime.fromisoformat(signal.signal_timestamp.replace("Z", "+00:00"))
        emitted = datetime.fromisoformat(signal.signal_emitted_at.replace("Z", "+00:00"))
        if reference.tzinfo is None or emitted.tzinfo is None or emitted < reference:
            raise ValueError("invalid reference/emission clock order")
        if (emitted-reference).total_seconds() > MAX_ENTRY_DELAY_SECONDS:
            reason = reason or "ENTRY_PUBLICATION_WINDOW_EXPIRED"
    if reason:
        metadata.update({"research_only": True, "execution_eligible": False,
                         "signal_emission_class": RESEARCH,
                         "timing_rejection_reason": reason})
        provenance.update({"classification": RESEARCH, "signal_emission_class": RESEARCH})
    if provenance.get("signal_emission_class") == RESEARCH or metadata.get("signal_emission_class") == RESEARCH:
        metadata.update({"research_only": True, "execution_eligible": False})
    return replace(signal, strategy_metadata=metadata, provenance=provenance)

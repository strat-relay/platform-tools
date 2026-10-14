from research.price_action_outcome_audit import (
    _cancellation_diagnostics,
    build_ledger,
    resolver_audit,
)


def _wrapped(reason, **provenance):
    return {"evaluation": {}, "provenance": {"rejection_reason": reason, **provenance},
            "decision_time": provenance.get("information_cutoff", 100),
            "event_open": provenance.get("information_cutoff", 100), "event_close": 115,
            "tick_size": 1.0}


def test_ledger_reconciles_proposal_acceptance_and_cancellation():
    result = {"strategy": "sr_retest", "instrument": "TEST", "unresolved_pending": 0,
              "rejection_counts": {}, "evaluations": [
                  _wrapped("RESEARCH_PROPOSAL_ONLY", direction="LONG", entry_reference=101,
                           stop=99, target=105, planned_r=2, level={"price": 100}, information_cutoff=100),
                  _wrapped("ENTRY_OBSERVED_RESEARCH_ONLY", direction="LONG", entry_open=101,
                           stop=99, target=105, planned_r=2, information_cutoff=115,
                           entry_cancelled=False),
              ]}
    ledger = build_ledger(result)
    assert ledger["counts"] == {"proposals": 1, "accepted_research_entries": 1,
                                 "cancellations": 0, "unresolved_dataset_end": 0,
                                 "duplicate_or_unmatched_observations": 0}


def test_cancellation_diagnostics_distinguish_gap_from_geometry():
    gap = {"direction": "SHORT", "entry_open": 112, "stop": 111, "target": 100,
           "proposal": {"level": {"price": 100}}}
    geometry = {"direction": "SHORT", "entry_open": 105, "stop": 104, "target": 100,
                "proposal": {"level": {"price": 100}}}
    assert _cancellation_diagnostics(gap, tick=1)["gap_breached"] is True
    assert _cancellation_diagnostics(geometry, tick=1)["invalid_entry_geometry"] is True


def test_existing_execution_contract_is_not_claimed_compatible():
    audit = resolver_audit()
    assert audit["compatible"] is False
    assert audit["production_writes"] == 0

"""Dormant, fail-closed migration substrate for P0/P1.

These primitives are intentionally not imported by production runners.  Later
cutover work can adopt them domain by domain without changing legacy authority.
"""

from .gates import CutoverGate, GateEvidence, evaluate_gate
from .modes import EventTransportMode, LegacyProjectionMode, RuntimeModes, StateAuthorityMode
from .reconcile import ReconciliationFinding, ReconciliationStatus, reconcile
from .state import MigrationState, MigrationStateStore, transition

__all__ = [
    "CutoverGate", "GateEvidence", "evaluate_gate", "EventTransportMode",
    "LegacyProjectionMode", "RuntimeModes", "StateAuthorityMode",
    "ReconciliationFinding", "ReconciliationStatus", "reconcile",
    "MigrationState", "MigrationStateStore", "transition",
]

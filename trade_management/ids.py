"""Deterministic identity for every P4.2 domain object.

Same `stable_id(prefix, value)` construction already used across this codebase
(`orchestration/models.py`, `trade_manager/central.py`, `migration/signal.py`): sha256 of
canonical (sorted-key, compact-separator) JSON, truncated to 24 hex chars, prefixed. Defined
locally rather than imported cross-domain, matching the existing convention - each domain owns
its own copy of this pure helper.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping


def stable_id(prefix: str, value: Mapping[str, Any]) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    return f"{prefix}_{hashlib.sha256(raw).hexdigest()[:24]}"


def managed_trade_id(signal_id: str) -> str:
    """`stable_id("MT", {signal_id})` - the PRIMARY binding's identity (A6 06 section 3).
    Depends on nothing but the EntrySignal's own stable id: never the process, path, wall
    clock, economic_position_id, ticket, or account."""
    return stable_id("MT", {"signal_id": signal_id})


def market_snapshot_id(*, provider_id: str, feed_id: str | None, instrument: str,
                       source_timestamp: str, bid: float, ask: float) -> str:
    """`stable_id("MSN", {provider_id, feed_id, instrument, source_timestamp, sha256(bid, ask)})`
    (A6 08 section 3): shares one market fact across every trade on that instrument."""
    quote_hash = hashlib.sha256(json.dumps({"bid": bid, "ask": ask}, sort_keys=True,
                                           separators=(",", ":"), default=str).encode()).hexdigest()
    return stable_id("MSN", {"provider_id": provider_id, "feed_id": feed_id, "instrument": instrument,
                             "source_timestamp": source_timestamp, "quote_hash": quote_hash})


def observation_id(*, managed_trade_id: str, provider_id: str, feed_id: str | None, instrument: str,
                   source_timestamp: str, bid: float, ask: float) -> str:
    """`stable_id("TOBS", {managed_trade_id, provider_id, feed_id, instrument, source_timestamp,
    sha256(bid, ask)})` (A6 08 section 3): identical fact for the same trade cannot exist twice
    regardless of retries or producer restarts. Deliberately excludes `observation_seq` - the
    sequence is assigned separately (this id is a *content* key; the sequence is an *ordering*
    key) so a redelivered/duplicate observation for the same trade+fact always maps to the same
    id even if the producer briefly disagrees with itself about the sequence number."""
    quote_hash = hashlib.sha256(json.dumps({"bid": bid, "ask": ask}, sort_keys=True,
                                           separators=(",", ":"), default=str).encode()).hexdigest()
    return stable_id("TOBS", {"managed_trade_id": managed_trade_id, "provider_id": provider_id,
                              "feed_id": feed_id, "instrument": instrument,
                              "source_timestamp": source_timestamp, "quote_hash": quote_hash})


def decision_id(*, managed_trade_id: str, observation_id: str, tm_version_id: str) -> str:
    """`stable_id("TMD", {managed_trade_id, observation_id, tm_version_id})` (A6 11 section 1):
    one decision per (trade, observation, version); a redelivered observation recomputes the
    same id and is absorbed by the unique constraint."""
    return stable_id("TMD", {"managed_trade_id": managed_trade_id, "observation_id": observation_id,
                             "tm_version_id": tm_version_id})


def tm_version_id(manifest_hash: str) -> str:
    """`"TMV_" + sha256(canonical_bytes(identity_manifest))[:24]` (A6 07 / A7 04 section 1).
    `manifest_hash` is the full sha256 hex digest; only its first 24 chars become the id, but
    the full hash is stored separately (`trade_manager_version.manifest_hash`, UNIQUE) so the
    truncation is never relied on for uniqueness."""
    return f"TMV_{manifest_hash[:24]}"


def binding_id(*, strategy_id: str, strategy_instance_id: str | None, instrument: str | None,
               tm_version_id: str, resolution: str, valid_from: str) -> str:
    return stable_id("BIND", {"strategy_id": strategy_id, "strategy_instance_id": strategy_instance_id,
                              "instrument": instrument, "tm_version_id": tm_version_id,
                              "resolution": resolution, "valid_from": valid_from})

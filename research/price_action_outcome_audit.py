"""Additive episode reconciliation and outcome-resolver compatibility audit.

This module replays the frozen evaluators, builds an episode ledger, and maps
accepted research observations to an in-memory ``EntrySignal`` shape.  It does
not submit signals, write databases, or invoke a resolver whose semantics do
not match the frozen protocol.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from strategy_backtest.models import EntrySignal, MarketEvent, canonical_json, fingerprint
from strategy_backtest.price_action_engulfing_continuation import (
    STRATEGY_VERSION_ID as ENGULF_STRATEGY,
)
from strategy_backtest.price_action_sr_retest import STRATEGY_VERSION_ID as SR_STRATEGY

from research.price_action_historical_replay import (
    DEFAULT_OUTPUT, DEFAULT_SOURCE, event_stream, init_evaluator, load_source,
)


OUTPUT = DEFAULT_OUTPUT / "outcome_audit_v1"
DATASET_MANIFEST_PATH = DEFAULT_OUTPUT / "dataset_manifest.json"
OUTCOME_POLICY = {
    "schema": "price-action-theoretical-outcome-protocol-v1",
    "status": "FROZEN_ADDITIVE_RESEARCH_ONLY",
    "entry_policy": "accepted research observation at the next M15 candle open; no fill slippage inferred",
    "path_start": "first completed M15 candle after the accepted entry open; entry candle is included for touch audit",
    "same_candle_policy": "AMBIGUOUS when both stop and target touch and no lower-timeframe evidence exists",
    "missing_path_policy": "DATA_GAP_UNRESOLVED; never bridge gaps",
    "dataset_end_policy": "OPEN_AT_DATASET_END unless a supported terminal touch occurs before the last available candle",
    "maximum_hold_policy": "UNSPECIFIED_BY_FROZEN_V1; no time exit is invented",
    "cost_policy": "GROSS_REFERENCE_PRICE_ONLY; no bid/ask, commission, or slippage adjustment",
    "overlap_policy": "per-episode diagnostic only; no account-level portfolio drawdown",
    "resolver_selection": "blocked pending compatible resolver semantics",
}


@dataclass(frozen=True)
class ResearchEntry:
    episode_id: str
    strategy_version: str
    instrument: str
    direction: str
    entry_timestamp: int
    entry_price: float
    stop_price: float
    target_price: float
    planned_r: float
    actual_entry_r: float | None
    provenance: dict[str, Any]


def _episode_id(strategy: str, proposal: dict[str, Any]) -> str:
    payload = {key: proposal.get(key) for key in (
        "strategy", "instrument", "confirmation_time", "direction", "stop", "target", "entry_reference")}
    return hashlib.sha256(canonical_json({"strategy": strategy, **payload}).encode()).hexdigest()[:24]


def _level_payload(proposal: dict[str, Any]) -> dict[str, Any] | None:
    return proposal.get("zone") or proposal.get("level")


def _representative(entry: dict[str, Any], *, tick: float) -> dict[str, Any]:
    level = _level_payload(entry) or _level_payload(entry.get("proposal", {})) or {}
    level_price = level.get("price")
    actual = entry.get("entry_open")
    direction = entry.get("direction")
    measured_gap = None
    if level_price is not None and actual is not None:
        measured_gap = (float(level_price) - float(actual)) if direction == "LONG" else (float(actual) - float(level_price))
    return {"episode_id": entry["episode_id"], "confirmation_time": entry.get("confirmation_time"),
            "proposal_entry_reference": entry.get("entry_reference"),
            "next_open_time": entry.get("entry_time"), "next_open_price": actual,
            "direction": direction, "level_price": level_price,
            "gap_threshold": tick * 8, "measured_gap_against_level": measured_gap,
            "stop": entry.get("stop"), "target": entry.get("target"),
            "planned_r": entry.get("planned_r"), "actual_entry_r": entry.get("actual_entry_r"),
            "reason": entry.get("reason"), "cancellation_diagnostics": entry.get("cancellation_diagnostics")}


def _cancellation_diagnostics(episode: dict[str, Any], *, tick: float) -> dict[str, Any]:
    direction = episode.get("direction")
    entry = episode.get("entry_open")
    stop = episode.get("stop")
    target = episode.get("target")
    proposal = episode.get("proposal", {})
    level = _level_payload(proposal) or {}
    level_price = level.get("price")
    gap_threshold = tick * 8
    gap_breached = False
    if entry is not None and level_price is not None:
        gap_breached = (float(entry) < float(level_price) - gap_threshold if direction == "LONG"
                        else float(entry) > float(level_price) + gap_threshold)
    valid_geometry = ((float(stop) < float(entry) < float(target)) if direction == "LONG"
                      else (float(target) < float(entry) < float(stop))) if None not in (entry, stop, target) else None
    actual_r = episode.get("actual_entry_r")
    return {"gap_breached": gap_breached, "invalid_entry_geometry": valid_geometry is False,
            "actual_entry_r_below_minimum": actual_r is not None and actual_r < 1.5,
            "level_price": level_price, "gap_threshold": gap_threshold,
            "measured_gap_against_level": ((float(level_price) - float(entry)) if direction == "LONG" else
                                            (float(entry) - float(level_price))) if None not in (level_price, entry) else None}


def _replay_job(job: tuple[str, str, list[MarketEvent], int]) -> dict[str, Any]:
    kind, symbol, events, evaluation_start = job
    evaluator = init_evaluator(kind)
    warmup = [event for event in events if event.close_timestamp < evaluation_start]
    measured = [event for event in events if event.close_timestamp >= evaluation_start]
    for event in warmup:
        evaluator.consume_market_event(event)
    state = evaluator.snapshot_state()
    state["pending"] = {}
    evaluator.restore_state(state)
    evaluations: list[dict[str, Any]] = []
    for event in measured:
        for item in evaluator.consume_market_event(event):
            provenance = dict(item.provenance)
            provenance["data_manifest_hash"] = hashlib.sha256(DATASET_MANIFEST_PATH.read_bytes()).hexdigest()
            provenance["outcome_policy_hash"] = fingerprint(OUTCOME_POLICY)
            evaluations.append({"evaluation": item.to_dict(), "provenance": provenance,
                                "decision_time": int(item.decision_time), "event_open": event.open_timestamp,
                                "event_close": event.close_timestamp, "tick_size": event.provenance.get("tick_size")})
    return {"strategy": kind, "instrument": symbol, "evaluations": evaluations,
            "unresolved_pending": len(getattr(evaluator, "pending", {})),
            "rejection_counts": evaluator.diagnostics().get("rejection_counts", {})}


def build_ledger(result: dict[str, Any]) -> dict[str, Any]:
    strategy = ENGULF_STRATEGY if result["strategy"] == "engulfing_continuation" else SR_STRATEGY
    pending: dict[str, dict[str, Any]] = {}
    episodes: list[dict[str, Any]] = []
    duplicate_observations = 0
    for wrapped in result["evaluations"]:
        p = wrapped["provenance"]
        reason = p.get("rejection_reason")
        if reason == "RESEARCH_PROPOSAL_ONLY":
            proposal = {**p, "strategy": strategy, "instrument": result["instrument"],
                        "confirmation_time": wrapped["decision_time"]}
            episode_id = _episode_id(strategy, proposal)
            proposal["episode_id"] = episode_id
            if result["instrument"] in pending:
                pending[result["instrument"]]["superseded_by_new_proposal"] = True
                episodes.append(pending.pop(result["instrument"]))
            pending[result["instrument"]] = {"episode_id": episode_id, "strategy_version": strategy,
                "instrument": result["instrument"], "direction": p.get("direction"),
                "confirmation_time": wrapped["decision_time"], "entry_reference": p.get("entry_reference"),
                "stop": p.get("stop"), "target": p.get("target"), "planned_r": p.get("planned_r"),
                "proposal": proposal, "status": "PENDING_NEXT_OPEN"}
            continue
        if reason not in {"ENTRY_OBSERVED_RESEARCH_ONLY", "ENTRY_GAP_CANCELLED", "HTF_TREND_INVALIDATED", "INSUFFICIENT_ENTRY_R"}:
            continue
        if result["instrument"] not in pending:
            duplicate_observations += 1
            continue
        episode = pending.pop(result["instrument"])
        episode.update({"entry_time": p.get("information_cutoff", wrapped["event_open"]),
                        "entry_open": p.get("entry_open"), "actual_entry_r": p.get("actual_entry_r"),
                        "reason": reason, "entry_cancelled": bool(p.get("entry_cancelled")),
                        "status": "CANCELLED" if p.get("entry_cancelled") else "ACCEPTED_RESEARCH_ENTRY"})
        if p.get("entry_cancelled"):
            episode["cancellation_diagnostics"] = _cancellation_diagnostics(episode, tick=float(wrapped.get("tick_size") or 0.00001))
        episodes.append(episode)
    for episode in pending.values():
        episode.update({"status": "UNRESOLVED_DATASET_END", "reason": "NO_NEXT_OPEN_OBSERVATION"})
        episodes.append(episode)
    episodes.sort(key=lambda item: (item.get("confirmation_time", 0), item["episode_id"]))
    accepted = [episode for episode in episodes if episode["status"] == "ACCEPTED_RESEARCH_ENTRY"]
    cancellations = [episode for episode in episodes if episode["status"] == "CANCELLED"]
    unresolved = [episode for episode in episodes if episode["status"] == "UNRESOLVED_DATASET_END"]
    return {"strategy": result["strategy"], "instrument": result["instrument"],
            "episodes": episodes, "counts": {"proposals": len(episodes), "accepted_research_entries": len(accepted),
                "cancellations": len(cancellations), "unresolved_dataset_end": len(unresolved),
                "duplicate_or_unmatched_observations": duplicate_observations},
            "cancellation_reasons": dict(Counter(episode.get("reason") for episode in cancellations)),
            "accepted_entries": [asdict(ResearchEntry(episode["episode_id"], strategy, episode["instrument"],
                episode["direction"], int(episode["entry_time"]), float(episode["entry_open"]), float(episode["stop"]),
                float(episode["target"]), float(episode["planned_r"]), episode.get("actual_entry_r"),
                {"episode_id": episode["episode_id"],
                 "data_snapshot_id": episode["proposal"].get("data_snapshot_id"),
                 "data_manifest_hash": episode["proposal"].get("data_manifest_hash"),
                 "parameter_hash": episode["proposal"].get("parameter_hash"),
                 "dependency_manifest_hash": episode["proposal"].get("dependency_manifest_hash"),
                 "primitive_manifest": episode["proposal"].get("primitive_manifest"),
                 "outcome_policy_hash": fingerprint(OUTCOME_POLICY)}))
                for episode in accepted]}


def resolver_audit() -> dict[str, Any]:
    return {"selected_existing_contract": "strategy_backtest.execution.SimulatedExecution",
            "adapter_identity": "price-action-research-entry-adapter-v1",
            "adapter_contract": "accepted episodes map to in-memory EntrySignal geometry only; no submit/consume/persistence call",
            "compatible": False,
            "reasons": [
                "EntrySignal is an executable-shaped contract and must not be published; adapter keeps it in memory only.",
                "SimulatedExecution applies CONSERVATIVE_STOP_FIRST when stop and target both touch one candle; frozen protocol requires AMBIGUOUS without lower-timeframe evidence.",
                "SimulatedExecution has no explicit missing-path/data-gap classification.",
                "Frozen V1 specifies no maximum hold; SimulatedExecution requires an invented expiry or leaves trades open.",
                "CostModel cannot represent unavailable bid/ask/commission/slippage evidence without assumptions.",
            ],
            "minimal_extension_needed": "A research-only resolver API accepting initial-risk entries, configurable same-candle ambiguity, explicit no-max-hold, gap classification, and dataset-end censoring; it must remain outside production publication and broker reconciliation.",
            "production_writes": 0, "production_publication": False}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT)
    args = parser.parse_args()
    symbols, audits, bars, resolutions = load_source(args.source_root)
    ends = [audits[f"{symbol}/{tf}"]["last_close_timestamp"] for symbol in symbols for tf in ("H1", "M15")]
    common_end = min(ends)
    evaluation_start = common_end - 30 * 86400
    warmup_start = evaluation_start - 120 * 3600
    jobs = []
    for kind in ("sr_retest", "engulfing_continuation"):
        for symbol in symbols:
            tick = float(resolutions.get(symbol, {}).get("symbol_info", {}).get("tick_size") or 0.00001)
            jobs.append((kind, symbol, event_stream(symbol, bars, warmup_start, common_end,
                                                       "price-action-native-mt5-historical-freeze-v1", tick), evaluation_start))
    with ProcessPoolExecutor(max_workers=min(8, len(jobs))) as pool:
        raw_results = list(pool.map(_replay_job, jobs))
    ledgers = [build_ledger(result) for result in raw_results]
    all_episodes = [episode for ledger in ledgers for episode in ledger["episodes"]]
    cancellation_counts = Counter((episode["strategy_version"], episode["instrument"], episode.get("reason"))
                                  for episode in all_episodes if episode["status"] == "CANCELLED")
    cancellation_subtypes = Counter()
    for episode in all_episodes:
        if episode["status"] != "CANCELLED":
            continue
        diagnostics = episode.get("cancellation_diagnostics", {})
        for name in ("gap_breached", "invalid_entry_geometry", "actual_entry_r_below_minimum"):
            if diagnostics.get(name):
                cancellation_subtypes[(episode["strategy_version"], episode["instrument"], name)] += 1
    representative = {}
    for episode in all_episodes:
        if episode["status"] != "CANCELLED":
            continue
        key = (episode["strategy_version"], episode.get("reason"))
        if key not in representative:
            representative[key] = _representative(episode, tick=float(resolutions.get(episode["instrument"], {}).get("symbol_info", {}).get("tick_size") or 0.00001))
    protocol = {**OUTCOME_POLICY, "dataset_snapshot_id": "price-action-native-mt5-historical-freeze-v1",
                "evaluation_interval": {"start": evaluation_start, "end": common_end},
                "strategy_versions": [SR_STRATEGY, ENGULF_STRATEGY],
                "source_protocol_fingerprint": "see ../protocol_manifest.json",
                "policy_hash": fingerprint(OUTCOME_POLICY)}
    report = {"schema": "price-action-outcome-audit-report-v1", "protocol_hash": fingerprint(protocol),
              "episode_ledger_fingerprint": fingerprint(all_episodes),
              "scope": {"evaluation_start": evaluation_start, "evaluation_end": common_end,
                        "warmup_start": warmup_start, "retrospective": True},
              "strategy_totals": {}, "cancellations_by_strategy_instrument_reason": {
                  f"{strategy}|{instrument}|{reason}": count for (strategy, instrument, reason), count in sorted(cancellation_counts.items())},
              "cancellation_subtypes_by_strategy_instrument": {
                  f"{strategy}|{instrument}|{subtype}": count for (strategy, instrument, subtype), count in sorted(cancellation_subtypes.items())},
              "representative_cancellations": {f"{strategy}|{reason}": value for (strategy, reason), value in representative.items()},
              "resolver": resolver_audit(), "outcome_metrics": {"status": "BLOCKED_RESOLVER_SEMANTICS",
                  "accepted_entries": sum(ledger["counts"]["accepted_research_entries"] for ledger in ledgers),
                  "tp": 0, "sl": 0, "time_exit": 0, "ambiguous": 0, "data_gap": 0,
                  "open_at_dataset_end": sum(ledger["counts"]["unresolved_dataset_end"] for ledger in ledgers),
                  "resolved_denominator": 0, "unresolved_denominator": sum(ledger["counts"]["accepted_research_entries"] for ledger in ledgers),
                  "win_rate": None, "expectancy_r": None, "profit_factor": None, "maximum_drawdown_r": None}}
    for kind in ("sr_retest", "engulfing_continuation"):
        selected = [ledger for ledger in ledgers if ledger["strategy"] == kind]
        report["strategy_totals"][kind] = {key: sum(ledger["counts"][key] for ledger in selected)
            for key in ("proposals", "accepted_research_entries", "cancellations", "unresolved_dataset_end", "duplicate_or_unmatched_observations")}
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "outcome_protocol_manifest.json").write_text(json.dumps(protocol, indent=2, sort_keys=True) + "\n")
    (args.output_dir / "resolver_compatibility.json").write_text(json.dumps(resolver_audit(), indent=2, sort_keys=True) + "\n")
    (args.output_dir / "outcome_audit_report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    with (args.output_dir / "episode_ledger.jsonl").open("w") as handle:
        for episode in all_episodes:
            handle.write(json.dumps(episode, sort_keys=True) + "\n")
    with (args.output_dir / "accepted_research_entries.jsonl").open("w") as handle:
        for ledger in ledgers:
            for entry in ledger["accepted_entries"]:
                handle.write(json.dumps(entry, sort_keys=True) + "\n")
    print(json.dumps({"output_dir": str(args.output_dir), "protocol_hash": report["protocol_hash"],
                      "strategy_totals": report["strategy_totals"], "resolver_compatible": False}, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

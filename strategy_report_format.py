"""Shared CLI text formatter for the standard strategy observability report.

Every strategy's `report` subcommand renders through `format_standard_report()`
so the CLI and the Control API present the same numbers in the same section
order — this module has no strategy-specific knowledge and performs no
computation of its own; it only formats the dict each strategy's
`build_standard_report()` already computed.
"""
from __future__ import annotations

from typing import Any


def _fmt(value: Any, digits: int = 3) -> str:
    if value is None:
        return "N/A"
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return str(value)


def _line(label: str, value: Any) -> str:
    return f"{label}: {value if value is not None else 'N/A'}"


def _position_line(p: dict[str, Any]) -> str:
    realized = p.get("realized_R")
    realized_text = "OPEN" if realized is None else f"{_fmt(realized)}R"
    return (f"{p.get('symbol', 'N/A')} | {p.get('direction', 'N/A')} | "
            f"{p.get('entry_time', 'N/A')} | entry={p.get('entry_price', 'N/A')} | "
            f"result={realized_text} | status={p.get('status', 'N/A')}")


def format_standard_report(report: dict[str, Any]) -> str:
    identity = report.get("identity", {})
    status = report.get("status", {})
    sample = report.get("sample", {})
    performance = report.get("performance", {})
    lines: list[str] = []

    strategy_id = identity.get("strategy_id", "UNKNOWN_STRATEGY")
    lines.append(f"{strategy_id} — STANDARD STRATEGY REPORT (READ-ONLY)")
    lines.append("=" * 72)

    lines.append("\nSTATUS")
    lines.append(_line("Runner status", status.get("runner_status")))
    lines.append(_line("Runner heartbeat", status.get("last_runner_heartbeat")))
    lines.append(_line("Heartbeat age (s)", status.get("runner_heartbeat_age")))
    lines.append(_line("Bridge status", status.get("bridge_status")))
    lines.append(_line("Data status", status.get("data_status")))
    lines.append(_line("Data age (s)", status.get("data_age")))
    lines.append(_line("Kill switch", status.get("kill_switch")))

    lines.append("\nSAMPLE")
    lines.append(_line("Scope", sample.get("scope")))
    lines.append(_line("Boundary", sample.get("boundary")))

    lines.append("\nPERFORMANCE")
    lines.append(f"Trades: {performance.get('trades', 'N/A')} | Wins: {performance.get('wins', 'N/A')} | "
                 f"Losses: {performance.get('losses', 'N/A')} | Breakevens: {performance.get('breakevens', 'N/A')} | "
                 f"Open: {performance.get('open', 'N/A')}")
    lines.append(f"Realized R: {_fmt(performance.get('realized_R'))} | "
                 f"Expectancy R: {_fmt(performance.get('expectancy_R'))} | "
                 f"Profit factor: {_fmt(performance.get('profit_factor'))} | "
                 f"Max DD: {_fmt(performance.get('max_drawdown_R'))}")
    if performance.get("mfe_R") is not None or performance.get("mae_R") is not None:
        lines.append(f"MFE R: {_fmt(performance.get('mfe_R'))} | MAE R: {_fmt(performance.get('mae_R'))}")

    lines.append("\nBY SYMBOL")
    symbols = report.get("symbols") or []
    if symbols:
        for row in symbols:
            lines.append(f"{row.get('symbol', 'N/A')}: " + " ".join(
                f"{k}={v}" for k, v in row.items() if k != "symbol"))
    else:
        lines.append("None")

    lines.append("\nOPEN STRATEGY POSITIONS")
    open_positions = report.get("open_positions") or []
    if open_positions:
        for p in open_positions:
            lines.append(_position_line(p))
    else:
        lines.append("None")

    lines.append("\nCLOSED STRATEGY POSITIONS")
    closed_positions = report.get("closed_positions") or []
    if closed_positions:
        for p in closed_positions:
            lines.append(_position_line(p))
    else:
        lines.append("None")

    lines.append("\nREJECTIONS / NON-TRADES")
    rejections = report.get("rejection_reasons") or []
    if rejections:
        for row in rejections:
            lines.append(f"{row.get('reason_code', 'N/A')}: {row.get('count', 0)}")
    else:
        lines.append("None")

    lines.append("\nDATA QUALITY")
    dq = report.get("data_quality") or {}
    lines.append(_line("Status", dq.get("data_status")))
    lines.append(_line("Gap status", dq.get("gap_status")))
    lines.append(_line("Missing observations", dq.get("missing_observations")))
    lines.append(_line("Recovered", dq.get("recovered")))
    lines.append(_line("Recovery source", dq.get("recovery_source")))

    lines.append("\nRECENT ACTIVITY")
    activity = report.get("recent_activity") or []
    if activity:
        for row in activity:
            lines.append(f"{row.get('timestamp', 'N/A')} | {row.get('display_event', row.get('event_type', 'N/A'))} | "
                         f"{row.get('symbol', '')}")
    else:
        lines.append("None")

    ref = report.get("reference_performance")
    if ref:
        lines.append("\nHISTORICAL REFERENCE (not current/forward performance)")
        for k, v in ref.items():
            if k == "provenance":
                continue
            lines.append(_line(k, v))
        if ref.get("provenance"):
            lines.append(f"Provenance: {ref['provenance']}")

    lines.append("\nCONFIGURATION")
    lines.append(_line("Strategy version", identity.get("strategy_version")))
    lines.append(_line("Configuration version", identity.get("configuration_version")))
    lines.append(_line("Source identity", identity.get("source_identity")))
    lines.append(_line("Decision fingerprint", identity.get("decision_fingerprint")))
    lines.append(_line("Freeze timestamp", identity.get("freeze_timestamp")))
    lines.append(_line("Observability version", identity.get("observability_version")))

    extensions = report.get("extensions") or {}
    extra_keys = [k for k in extensions if k not in ("strategy_id",)]
    if extra_keys:
        lines.append(f"\n{strategy_id} — STRATEGY-SPECIFIC EXTENSIONS")
        for key in extra_keys:
            lines.append(f"\n[{key}]")
            value = extensions[key]
            if isinstance(value, dict):
                for k, v in value.items():
                    if isinstance(v, (list, dict)):
                        lines.append(f"{k}: {len(v)} item(s) — see JSON report for detail")
                    else:
                        lines.append(_line(k, v))
            elif isinstance(value, list):
                lines.append(f"{len(value)} item(s) — see JSON report for detail")
            else:
                lines.append(str(value))

    return "\n".join(lines)

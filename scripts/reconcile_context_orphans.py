"""One-shot historical reconciliation of orphaned OPEN Context positions.

Before the orphaned-position fix, an OPEN position stopped receiving exit evaluation once its
setup left FILLED (INVALIDATED_NO_REENTRY) or was compacted away. The corrected runner evaluates
such positions again, but only on *new* bars; the bars it skipped while they were orphaned would
never be seen. This replays exactly those skipped bars - authoritative completed M5 bars from the
fill up to the symbol's `last_m5` cursor (bars the runner has already passed) - through the
runner's own `_evaluate_open_position`. Exits therefore get their true historical bar time and R,
recorded through the runner's normal event ledger and state, and reach
strategy.entry_signal_outcomes through the unchanged outcome projection.

Rules:
- Only positions the runner no longer evaluates (`_unevaluated_open_positions`) are touched.
- A position without a frozen symbol/direction is reported as insufficient state, never guessed.
- Bars after `last_m5` are left to the runner itself, so nothing is evaluated twice.
- Market data comes only from the read-only bridge client (Mt5ReadClient.rates_range).
- Dry run by default. Run with --apply ONLY while the runner is stopped (e.g. as an init
  container). Idempotent: closed positions are no longer OPEN; still-open ones see the same bars.

    CONTEXT_RUNNER_STATE_DIR=/work python -m scripts.reconcile_context_orphans --mcp-url URL [--apply]
"""
from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path
from typing import Any, Callable
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import context_structure_retrace_forward as fwd  # noqa: E402

SOURCE = "HISTORICAL_RECONCILIATION"
PAGE_SIZE = 500


def fetch_m5(client: Any, symbol: str, start: int, end: int) -> list[dict[str, Any]]:
    """Completed M5 bars with start < time <= end, paged, via the read-only client."""
    bars: dict[int, dict[str, Any]] = {}
    cursor = start + 1
    while cursor <= end:
        page = client.rates_range(symbol, "M5", cursor, end + 1, page_size=PAGE_SIZE, completed_only=True)
        rows = page.get("rates", []) if isinstance(page, dict) else (page or [])
        rows = [r for r in rows if cursor <= int(r["time"]) <= end]
        if not rows:
            break
        for r in rows:
            bars[int(r["time"])] = r
        cursor = max(int(r["time"]) for r in rows) + 1
        if len(rows) < PAGE_SIZE:
            break
    return [bars[t] for t in sorted(bars)]


def orphans(state: dict[str, Any]) -> list[tuple[str | None, dict[str, Any] | None, dict[str, Any]]]:
    symbols = {s.get("symbol") for s in state.get("setups", {}).values()}
    symbols |= {p.get("symbol") for p in state.get("positions", {}).values()}
    found: list[tuple[str | None, dict[str, Any] | None, dict[str, Any]]] = []
    for symbol in sorted(s for s in symbols if s):
        found.extend((symbol, setup, pos) for setup, pos in fwd._unevaluated_open_positions(state, symbol))
    # Setup-less records without a frozen symbol cannot be attributed to any symbol.
    held = {str(o.get("economic_position_id")) for s in state.get("setups", {}).values() for o in s.get("opportunities", [])}
    for pid, pos in state.get("positions", {}).items():
        if pid not in held and pos.get("status") == "OPEN" and not pos.get("symbol"):
            found.append((None, None, pos))
    return found


def reconcile(state: dict[str, Any], client: Any, *, record_event: Callable[[dict[str, Any], dict[str, Any]], None]) -> dict[str, Any]:
    report: dict[str, list[dict[str, Any]]] = {"closed": [], "still_open": [], "insufficient_state": [], "no_bars": []}
    for symbol, setup, pos in orphans(state):
        pid = pos["economic_position_id"]
        direction = setup["direction"] if setup is not None else pos.get("direction")
        cursor = (state.get("symbols", {}).get(symbol or "", {}) or {}).get("last_m5")
        if symbol is None or direction not in ("LONG", "SHORT") or cursor is None:
            report["insufficient_state"].append({"position": pid, "symbol": symbol, "direction": direction})
            continue
        bars = fetch_m5(client, symbol, int(pos["fill_timestamp"]), int(cursor))
        if not bars:
            report["no_bars"].append({"position": pid, "symbol": symbol})
            continue
        with patch.object(fwd, "append_event", record_event):
            for bar in bars:
                fwd._evaluate_open_position(state, symbol, setup, pos, direction, bar, SOURCE)
                if pos["status"] != "OPEN":
                    break
        entry = {"position": pid, "symbol": symbol, "direction": direction, "bars_replayed": len(bars)}
        if pos["status"] == "OPEN":
            report["still_open"].append(entry)
        else:
            report["closed"].append({**entry, "status": pos["status"], "exit_timestamp": pos["exit_timestamp"],
                                     "realized_R": pos["realized_R"]})
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mcp-url", required=True, help="read-only research bridge (e.g. http://host:22347/mcp)")
    parser.add_argument("--apply", action="store_true", help="write state + event ledger (runner must be stopped)")
    parser.add_argument("--once-marker", type=Path, default=None,
                        help="with --apply: skip if this file exists; create it after a successful run "
                             "(lets it run as an init container without repeating on every restart)")
    args = parser.parse_args(argv)
    if args.apply and args.once_marker is not None and args.once_marker.exists():
        print(json.dumps({"skipped": True, "reason": f"already completed ({args.once_marker})"}))
        return 0
    from contracts.mt5_bridge import Mt5ReadClient
    client = Mt5ReadClient(args.mcp_url, timeout_s=60)
    live = fwd.load_state()
    if args.apply:
        state = live
        record_event = fwd.append_event  # runner's own ledger + checkpoint
    else:
        state = copy.deepcopy(live)
        record_event = lambda event, _state: None  # noqa: E731
    report = reconcile(state, client, record_event=record_event)
    if args.apply:
        fwd.save_state(state)
    summary = {k: len(v) for k, v in report.items()}
    if args.apply and args.once_marker is not None:
        args.once_marker.write_text(json.dumps({"summary": summary}, default=str) + "\n", encoding="utf-8")
    print(json.dumps({"dry_run": not args.apply, "summary": summary, "state_dir": str(fwd.STATE_DIR), **report},
                     indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Seed canonical instrument -> provider symbol mappings into platform.instrument_provider_mapping
(migration 027), optionally enabling them for a strategy instance.

Input is either the broker probe output ({"found": {"FX": {...}, "CRYPTO": {...}}}, entries with
provider_symbol / trade_enabled / trade_mode) or a flat list of
{"canonical": ..., "provider_symbol": ..., "asset_class": ..., "display_name": ...}.
Only symbols the broker reported as tradable are taken from probe output.

Dry-run by default; pass --apply to write. Idempotent: unchanged mappings keep their revision,
existing memberships are left untouched (a DISABLED instrument is never re-enabled). Does not touch
V2 execution symbol resolution or the V2 risk policy (real-money scope stays as configured there).

    python -m scripts.seed_instrument_mappings probe.json                      # dry run
    python -m scripts.seed_instrument_mappings probe.json --apply \
        --enable CONTEXT_STRUCTURE_RETRACE_V1:phase6                           # map + enable
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from platform_api.instrument_membership import ASSET_CLASSES, InstrumentMembershipRepository  # noqa: E402

NAMES = {"XAUUSD": "Gold", "XAGUSD": "Silver", "BTCUSD": "Bitcoin", "ETHUSD": "Ethereum", "LTCUSD": "Litecoin",
         "XRPUSD": "XRP", "BCHUSD": "Bitcoin Cash", "SOLUSD": "Solana", "DOGEUSD": "Dogecoin", "ADAUSD": "Cardano"}


def _tradable(entry: dict[str, Any]) -> bool:
    return entry.get("trade_enabled") not in (False, 0) and entry.get("trade_mode") not in (0,)


def load_mappings(payload: Any) -> list[dict[str, str]]:
    if isinstance(payload, dict) and "found" in payload:
        rows = []
        for group, entries in payload["found"].items():
            for canonical, entry in sorted(entries.items()):
                if _tradable(entry):
                    rows.append({"canonical": canonical, "provider_symbol": entry["provider_symbol"],
                                 "asset_class": group.upper()})
        return rows
    if isinstance(payload, list):
        return [dict(item) for item in payload]
    raise ValueError("expected probe output ({'found': ...}) or a list of mappings")


def seed(repo: Any, mappings: list[dict[str, str]], *, apply: bool, enable: tuple[str, str] | None,
         actor: str = "seed_instrument_mappings") -> dict[str, Any]:
    summary: dict[str, Any] = {"mapped": [], "unchanged": [], "enabled": [], "membership_kept": [], "dry_run": not apply}
    existing = {}
    if enable and apply:
        existing = {r["canonical_instrument"]: r for r in repo.list_membership(*enable)}
    for m in mappings:
        canonical = str(m["canonical"]).strip().upper()
        asset_class = str(m.get("asset_class") or "OTHER").upper()
        if asset_class not in ASSET_CLASSES:
            raise ValueError(f"{canonical}: unknown asset_class {asset_class}")
        if not apply:
            summary["mapped"].append(canonical)
            continue
        row = repo.save_mapping(canonical, str(m["provider_symbol"]), asset_class,
                                display_name=m.get("display_name") or NAMES.get(canonical, canonical), actor=actor)
        summary["mapped" if row["changed"] else "unchanged"].append(canonical)
        if enable:
            if canonical in existing:
                summary["membership_kept"].append(f"{canonical}={existing[canonical]['state']}")
            else:
                repo.save_membership(enable[0], enable[1], canonical, "ACTIVE", None, actor)
                summary["enabled"].append(canonical)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("input", type=Path)
    parser.add_argument("--apply", action="store_true", help="write (default is a dry run)")
    parser.add_argument("--enable", metavar="STRATEGY_ID:INSTANCE_ID",
                        help="also add each newly mapped instrument as an ACTIVE member of this instance")
    args = parser.parse_args(argv)
    enable = tuple(args.enable.split(":", 1)) if args.enable else None
    if enable is not None and len(enable) != 2:
        parser.error("--enable must be STRATEGY_ID:INSTANCE_ID")
    mappings = load_mappings(json.loads(args.input.read_text(encoding="utf-8")))
    print(json.dumps(seed(InstrumentMembershipRepository(), mappings, apply=args.apply, enable=enable), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

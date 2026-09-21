"""Historical paper validation for the frozen 25% USTEC entry variants."""
from __future__ import annotations

import json
from pathlib import Path

from liquidity_displacement_oos_validate import run

ROOT = Path(__file__).resolve().parent
VARIANTS = {
    "USTEC_x100m_25": ("USTEC_x100m", 0.25),
    "USTECm_25": ("USTECm", 0.25),
}


def main() -> None:
    results = {}
    for name, (symbol, fraction) in VARIANTS.items():
        try:
            results[name] = run(name, symbol, fraction)
        except Exception as exc:
            results[name] = {"variant": name, "symbol": symbol, "error": str(exc)}
        print(json.dumps(results[name], indent=2), flush=True)

    (ROOT / "liquidity_displacement_ustec25_results.json").write_text(
        json.dumps(results, indent=2), encoding="utf-8"
    )
    lines = [
        "# USTEC shallow-entry historical paper validation",
        "",
        "READ-ONLY / PAPER-ONLY. Frozen V1 logic with 25% retracement, 5-candle expiry, and 1.25R target.",
        "",
        "| Variant | Symbol | Discovery fills | Discovery PF | Discovery Exp R | Validation fills | Validation PF | Validation Exp R | Validation DD |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name, result in results.items():
        if "error" in result:
            lines.append(f"| {name} | {result['symbol']} | error | error | error | error | error | error | error |")
            continue
        discovery = result["discovery"]
        validation = result["validation"]
        lines.append(
            f"| {name} | {result['symbol']} | {discovery['filled']} | {discovery['profit_factor']} | "
            f"{discovery['expectancy_r']:.3f} | {validation['filled']} | {validation['profit_factor']} | "
            f"{validation['expectancy_r']:.3f} | {validation['max_drawdown_r']:.2f}R |"
        )
    (ROOT / "liquidity_displacement_ustec25_summary.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()

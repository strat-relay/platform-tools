from __future__ import annotations

from pathlib import Path


def audit_repository_provenance(root: str | Path) -> dict:
    root = Path(root)
    validation_mentions = []
    for path in sorted(root.glob("*.md")):
        text = path.read_text(encoding="utf-8", errors="ignore")
        if "validation" in text.lower() and ("2026-07" in text or "2026-08" in text or "2026-09" in text or "July" in text or "September" in text):
            validation_mentions.append(path.name)
    return {
        "repository": str(root),
        "known_prior_use": {
            "july_september_2026": True,
            "evidence": validation_mentions,
            "used_for_previous_oos_results_and_strategy_decisions": True,
        },
        "eligibility": {
            "march_june_2026": "internal discovery/development only; previously used in research",
            "july_september_2026": "not untouched; previously labeled validation and discussed for parameter decisions",
            "future_data_after_phase1_freeze": "eligible final holdout if collected without inspection-driven rule changes",
        },
        "recommendation": "Do not call July-September 2026 an untouched validation period for this branch. Reserve a future post-freeze period as the final holdout.",
    }

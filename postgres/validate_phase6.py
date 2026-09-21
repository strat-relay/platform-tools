from __future__ import annotations

import argparse
import json
import hashlib
from pathlib import Path

from .config import PostgresConfig
from .db import connect

ROOT = Path(__file__).resolve().parents[1]


def validate(conn, compact_path: Path) -> dict[str, object]:
    compact = json.loads(compact_path.read_text(encoding="utf-8"))
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM strategy.phase6_setups")
        setup_count = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM strategy.phase6_entry_opportunities")
        opportunity_count = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM strategy.phase6_economic_positions")
        position_count = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM strategy.phase6_setup_lifecycle")
        setup_events = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM strategy.phase6_position_lifecycle")
        position_events = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM research.phase6_observations")
        observations = cur.fetchone()[0]
        cur.execute("SELECT setup_id FROM strategy.phase6_setups ORDER BY setup_id")
        actual_setup_ids = [row[0] for row in cur.fetchall()]
        cur.execute("SELECT entry_opportunity_id FROM strategy.phase6_entry_opportunities ORDER BY entry_opportunity_id")
        actual_opportunity_ids = [row[0] for row in cur.fetchall()]
        cur.execute("SELECT economic_position_id FROM strategy.phase6_economic_positions ORDER BY economic_position_id")
        actual_position_ids = [row[0] for row in cur.fetchall()]
    expected = {"setups": len(compact.get("setups", {})), "opportunities": sum(len(s.get("opportunities", [])) for s in compact.get("setups", {}).values()), "positions": len(compact.get("positions", {}))}
    def digest(values):
        return hashlib.sha256("\n".join(values).encode()).hexdigest()
    expected_setup_ids = sorted(compact.get("setups", {}))
    expected_opportunity_ids = sorted(o["entry_opportunity_id"] for s in compact.get("setups", {}).values() for o in s.get("opportunities", []) if o.get("entry_opportunity_id"))
    expected_position_ids = sorted(compact.get("positions", {}))
    id_hashes = {
        "setups": {"actual": digest(actual_setup_ids), "expected": digest(expected_setup_ids)},
        "opportunities": {"actual": digest(actual_opportunity_ids), "expected": digest(expected_opportunity_ids)},
        "positions": {"actual": digest(actual_position_ids), "expected": digest(expected_position_ids)},
    }
    return {"counts": {"setups": setup_count, "opportunities": opportunity_count, "positions": position_count, "setup_lifecycle_events": setup_events, "position_lifecycle_events": position_events, "observations": observations}, "expected": expected, "counts_equal": {k: {"actual": {"setups": setup_count, "opportunities": opportunity_count, "positions": position_count}[k], "expected": expected[k]} for k in expected}, "id_hashes": id_hashes, "id_hashes_equal": all(v["actual"] == v["expected"] for v in id_hashes.values())}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--compact", type=Path, default=ROOT / "context_structure_retrace_forward_state_compact.json")
    args = parser.parse_args()
    with connect(PostgresConfig.from_env()) as conn:
        print(json.dumps(validate(conn, args.compact), indent=2))


if __name__ == "__main__": main()

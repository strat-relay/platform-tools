from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

path = Path("/data/status.json")
try:
    state = json.loads(path.read_text())
    updated = datetime.fromisoformat(state["updated_at_utc"].replace("Z", "+00:00"))
    age = (datetime.now(timezone.utc) - updated).total_seconds()
    good = state.get("phase") == "LIVE_SHADOW" and age <= 30 and not state.get("signal_db_primary_enabled") and not state.get("signal_jetstream_primary_enabled")
except Exception:
    good = False
if not good:
    sys.exit(1)

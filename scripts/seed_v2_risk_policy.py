"""Explicit one-time bootstrap of the relational V2 policy from the reference file."""
from __future__ import annotations

import json

from execution_v2.risk_policy_store import persist_policy
from postgres.db import connect


def main() -> None:
    with open("orchestration/config/v2_execution_risk_policy.json", encoding="utf-8") as fh:
        raw = json.load(fh)
    policy, meta = persist_policy(raw, updated_by="bootstrap:file:legacy", connect_fn=connect)
    print(json.dumps({"policy_id": meta["policy_id"], "revision": meta["revision"],
                      "source": meta["source"], "risk_per_trade": policy.risk_per_trade}))


if __name__ == "__main__":
    main()

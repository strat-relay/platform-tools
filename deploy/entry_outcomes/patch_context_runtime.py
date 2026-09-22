#!/usr/bin/env python3
"""Patch only the Context runner/init images and PostgreSQL projection env."""
from __future__ import annotations

import argparse
import json
import subprocess


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--context", default="local")
    parser.add_argument("--namespace", default="trading")
    parser.add_argument("--image", required=True, help="immutable registry image reference")
    parser.add_argument("--cutoff-id", required=True, help="verified canonical post-T0 EntrySignal cutoff")
    parser.add_argument("--dry-run", choices=("none", "client", "server"), default="none")
    args = parser.parse_args()

    overlay_command = [
        "sh", "-c",
        "set -eu; "
        "cp /outcome-overlay/context_structure_retrace_forward.py /work/; "
        "cp /outcome-overlay/context_structure_retrace_outcome_projector.py /work/; "
        "cp /outcome-overlay/strategy_report_format.py /work/",
    ]
    patch = {
        "spec": {
            "template": {
                "spec": {
                    "initContainers": [{
                        "name": "initialize-runtime-tree",
                        "image": args.image,
                        "imagePullPolicy": "IfNotPresent",
                        "command": overlay_command,
                    }],
                    "containers": [{
                        "name": "context-paper",
                        "image": args.image,
                        "imagePullPolicy": "IfNotPresent",
                        "env": [
                            {
                                "name": "TRADING_POSTGRES_DSN",
                                "valueFrom": {"secretKeyRef": {
                                    "name": "trading-runtime-endpoints",
                                    "key": "TRADING_POSTGRES_DSN",
                                }},
                            },
                            {"name": "ENTRY_OUTCOME_SIGNAL_CUTOFF_ID", "value": args.cutoff_id},
                        ],
                    }],
                },
            },
        },
    }
    command = [
        "kubectl", "--kubeconfig", args.kubeconfig, "--context", args.context,
        "-n", args.namespace, "patch", "deployment", "mt5-native-bridge-main-runtime",
        "--type=strategic", "--patch", json.dumps(patch, separators=(",", ":")),
    ]
    if args.dry_run != "none":
        command.append(f"--dry-run={args.dry_run}")
    subprocess.run(command, check=True)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Builds the small, machine-readable release manifest (mission Phase 5): given a commit and the
per-image digests collected by release.yml's build-and-push job, answer "image -> digest ->
source repository -> exact Git SHA" without needing to query the registry again.

Deliberately minimal - one JSON file, no database, no service. Each *.env file in --images-dir
holds exactly one `name=digest` line, written by release.yml's "Record image/digest" step (one
file per matrix leg, to avoid artifact-merge filename collisions).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True, help='e.g. "strat-relay/platform-tools"')
    parser.add_argument("--commit", required=True, help="full commit SHA the images were built from")
    parser.add_argument("--images-dir", required=True, type=Path, help="directory of downloaded *.env files")
    parser.add_argument("--registry", required=True, help='e.g. "ghcr.io/strat-relay"')
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()

    images = []
    env_files = sorted(args.images_dir.glob("*.env"))
    if not env_files:
        print(f"no *.env files found in {args.images_dir}", file=sys.stderr)
        return 1

    for env_file in env_files:
        line = env_file.read_text(encoding="utf-8").strip()
        if not line or "=" not in line:
            print(f"malformed image record: {env_file}", file=sys.stderr)
            return 1
        name, digest = line.split("=", 1)
        images.append({
            "name": name,
            "tag": f"sha-{args.commit}",
            "digest": digest,
            "reference": f"{args.registry}/{name}@{digest}",
        })

    manifest = {
        "repository": args.repository,
        "commit": args.commit,
        "images": images,
    }
    args.out.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

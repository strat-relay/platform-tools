#!/bin/sh
set -eu

# Records this release's image/digest provenance as a PR against the separately-owned GitOps
# repository, for Flux to reconcile. This script never talks to Kubernetes and never receives
# cluster credentials - it only opens a PR with a JSON file, the same as a human would.
#
# Usage: open_gitops_update.sh <release-manifest.json> <owner/repo> <app-name>
# Requires: GH_TOKEN in the environment, scoped to <owner/repo> (a fine-scoped token or a GitHub
# App installation token - never a broad personal access token; see
# docs/engineering/PRODUCTION_RELEASE_POLICY.md).

manifest=$1
gitops_repo=$2
app_name=$3

# Resolve before changing directory. Actions artifacts may be downloaded into
# an explicit directory, and every subsequent read must use the same file.
manifest_path=$(python3 -c 'import os,sys; print(os.path.abspath(sys.argv[1]))' "$manifest")
test -f "$manifest_path"

commit=$(python3 -c "import json,sys; print(json.load(open('$manifest_path'))['commit'])")
short_sha=$(echo "$commit" | cut -c1-12)
branch="release/${app_name}/${short_sha}"
target_path="apps/${app_name}/release.json"

workdir=$(mktemp -d)
trap 'rm -rf "$workdir"' EXIT

gh repo clone "$gitops_repo" "$workdir" -- --depth 1
cd "$workdir"

git checkout -b "$branch"
mkdir -p "apps/${app_name}"
cp "$manifest_path" "$target_path"

# Keep every declared workload on the exact immutable digest published by
# platform-tools. These workloads are owned by the GitOps repository, while
# their image provenance is owned by the release manifest. The update remains
# a desired-state PR; this script never contacts Kubernetes.
#
# A release can contain images that are not adopted by a particular GitOps
# checkout yet, so absent manifest paths are intentionally skipped. This lets
# the handoff work during topology migrations without silently leaving an
# existing workload on an old digest.
changed_paths=$(python3 - "$manifest_path" <<'PY'
import json
import pathlib
import re
import sys

manifest_path = pathlib.Path(sys.argv[1])
manifest = json.loads(manifest_path.read_text())
references = {image["name"]: image["reference"] for image in manifest["images"]}
paths_by_image = {
    "trading-platform-control-api": ["apps/trading-platform/platform-api.yaml"],
    "trading-platform-api-router": ["apps/trading-platform/platform-api-router.yaml"],
    "trading-platform-runtime": [
        "apps/trading-platform/runtimes.yaml",
        "apps/trading-platform/execution-v2-workload.yaml",
    ],
    "trading-platform-realtime-api": ["apps/trading-platform/platform-realtime-api.yaml"],
}

image_pattern = re.compile(r"^(?P<indent>\s*)image:\s+ghcr\.io/strat-relay/(?P<name>[a-z0-9-]+)@sha256:[0-9a-f]+\s*$")
changed = []
for image_name, paths in paths_by_image.items():
    reference = references.get(image_name)
    if reference is None:
        continue
    for path_string in paths:
        path = pathlib.Path(path_string)
        if not path.is_file():
            continue
        original = path.read_text()
        lines = original.splitlines(keepends=True)
        updated = []
        touched = False
        for line in lines:
            match = image_pattern.match(line.rstrip("\n"))
            if match and match.group("name") == image_name:
                newline = "\n" if line.endswith("\n") else ""
                line = f"{match.group('indent')}image: {reference}{newline}"
                touched = True
            updated.append(line)
        result = "".join(updated)
        if touched and result != original:
            path.write_text(result)
            changed.append(path_string)

for path in changed:
    print(path)
PY
)

# `target_path` is absent in the bootstrap GitOps repository.  A copied new file is
# untracked, and `git diff --quiet` intentionally ignores untracked files; checking
# only the diff therefore falsely treats the first release as identical.  Include
# untracked/added content in the comparison while keeping the operation read-only
# until the explicit add/commit below.
if test -z "$(git status --short -- "$target_path" $changed_paths)"; then
  echo "no change to ${target_path} - skipping (this release produced an identical manifest)"
  exit 0
fi

git config user.name "stratrelay-release-bot"
git config user.email "release-bot@stratrelay.app"
git add "$target_path" $changed_paths
git commit -m "Update ${app_name} desired state to ${short_sha}

Application repository: ${gitops_repo%%/*} (see release.json's own "repository" field for the
exact app repo)
Application commit: ${commit}

This PR only records desired state for Flux to reconcile. It does not touch Kubernetes directly
and was opened with no cluster credentials."
# `gh auth setup-git` does not consume GH_TOKEN as an active login in the
# Actions runner. Supply the scoped token only through Git's HTTP header; it
# is never printed and the temporary clone is discarded after this script.
auth_header=$(printf 'x-access-token:%s' "$GH_TOKEN" | base64 | tr -d '\n')
git -c "http.extraHeader=Authorization: basic ${auth_header}" push origin "$branch"

gh pr create --repo "$gitops_repo" --head "$branch" \
  --title "Update ${app_name} desired state to ${short_sha}" \
  --body "Automated release update. Application commit: ${commit}. See ${target_path} for the full image/digest manifest. Flux reconciles this once merged - this PR does not deploy anything by itself."

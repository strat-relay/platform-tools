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

commit=$(python3 -c "import json,sys; print(json.load(open('$manifest'))['commit'])")
short_sha=$(echo "$commit" | cut -c1-12)
branch="release/${app_name}/${short_sha}"
target_path="apps/${app_name}/release.json"

workdir=$(mktemp -d)
trap 'rm -rf "$workdir"' EXIT

gh repo clone "$gitops_repo" "$workdir" -- --depth 1
cd "$workdir"

git checkout -b "$branch"
mkdir -p "apps/${app_name}"
cp "$OLDPWD/$manifest" "$target_path"

# `target_path` is absent in the bootstrap GitOps repository.  A copied new file is
# untracked, and `git diff --quiet` intentionally ignores untracked files; checking
# only the diff therefore falsely treats the first release as identical.  Include
# untracked/added content in the comparison while keeping the operation read-only
# until the explicit add/commit below.
if test -z "$(git status --short -- "$target_path")"; then
  echo "no change to ${target_path} - skipping (this release produced an identical manifest)"
  exit 0
fi

git config user.name "stratrelay-release-bot"
git config user.email "release-bot@stratrelay.app"
git add "$target_path"
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

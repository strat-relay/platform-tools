#!/bin/sh
set -eu

# Refuse production release work from an agent branch or detached commit.
# Usage: verify_main_release.sh [source-commit]
remote=${RELEASE_REMOTE:-origin}
branch=${RELEASE_BRANCH:-main}
source_commit=${1:-HEAD}

git fetch "$remote" "$branch" >/dev/null
if ! git merge-base --is-ancestor "$source_commit" "$remote/$branch"; then
  echo "production releases must originate from $remote/$branch: $source_commit is not reachable" >&2
  exit 1
fi

echo "$source_commit is reachable from $remote/$branch"

# Production release policy

Operational, not theoretical - this is what actually happens when code moves from a branch to a
running production container.

## The pipeline

```
feature branch / agent worktree
        v
       PR                    <- .github/workflows/pr.yml: tests, typecheck/build validation,
        v                       docker build validation. No push, no deploy.
      main
        v
GitHub Actions               <- .github/workflows/release.yml, main-only (Phase 7 guard;
        v                       scripts/verify_main_release.sh)
immutable image               <- ghcr.io/<owner>/<image>:sha-<full-commit-sha>, digest-addressed
        v
GitOps repository update      <- a PR against the separate GitOps repo, image+digest only
        v
       Flux                  <- reconciles the cluster from the GitOps repo's desired state
```

## Rules

- **Branches and agent worktrees are development only.** Nothing built from a branch other than
  `main` is a production artifact, no matter how it was tested. `scripts/verify_main_release.sh`
  enforces this mechanically before any release build runs.
- **PR -> main is required** for anything that will ever run in production. There is no direct
  path from a branch to a deployed container.
- **`main` is the canonical application source.** Every image this repository produces is
  buildable, byte-for-byte, from a commit reachable from `origin/main` - see the release
  manifest (below) for how to check any running image against that claim.
- **GitHub Actions builds and releases.** It never deploys. `release.yml` stops at pushing an
  image to GHCR and opening a PR against the GitOps repository; it has no Kubernetes credentials
  and never calls `kubectl`.
- **The GitOps repository is the canonical deployment desired state.** What is actually running
  is whatever Flux has reconciled from that repository, not whatever this repository's own
  `deploy/*.yaml` manifests say in isolation (those are the manifest *source*, mirrored into the
  GitOps repo's desired state by the release workflow - they are not applied directly).
- **Flux deploys.** Nothing in this repository's CI ever mutates the cluster. Routine
  `kubectl apply`/`kubectl set image` against production is prohibited outside an explicitly
  authorized, manual, one-off operation - not something CI does routinely.
- **Application workflows have no cluster credentials.** No kubeconfig, no cluster-scoped token,
  anywhere in `.github/workflows/`. The only credential `release.yml` uses beyond the built-in
  `GITHUB_TOKEN` (scoped to `contents: read, packages: write`) is a fine-scoped GitOps-repo token
  (`GITOPS_DEPLOY_TOKEN`, see below) - and that token's only capability needed is opening a PR
  against one specific repository.
- **Rollback means selecting an older immutable main-built image**, by SHA, in the GitOps
  repository - never re-deploying a branch build, never `kubectl edit`-ing a running Deployment.
- **Deployment does not imply execution authorization.** A container running in production is
  never, by itself, permission to place a broker order. `EXECUTION_AUTHORITY_MODE` and the risk
  policy are canonical PostgreSQL runtime state, not something a container image, a Kubernetes
  manifest, or this pipeline can set. `release.yml`'s `safety-guard` job fails the release outright
  if any manifest in this repository's own `deploy/` statically arms execution
  (`EXECUTION_AUTHORITY_MODE: ENABLED` / `REAL_EXECUTION: true` as a literal value, not a
  `valueFrom` cluster reference) - see that job for the exact check.
- **The bridge (`mt5-native-bridge`) has a separate release path.** It is not a Kubernetes
  workload and Flux does not deploy it; see that repository's own
  `docs/engineering/PRODUCTION_RELEASE_POLICY.md`.

## Required GitOps configuration (not yet provisioned by this repository)

`release.yml`'s `gitops-update` job needs, once the GitOps repository exists:

- **Repository variable** `GITOPS_REPOSITORY` - e.g. `strat-relay/stratrelay-gitops`.
- **Repository secret** `GITOPS_DEPLOY_TOKEN` - a fine-scoped token or GitHub App installation
  token whose only capability is opening a PR against that one repository. Never a broad personal
  access token.

Until both exist, the job logs a notice and skips - it does not block PR CI or the image publish
above, and the release is still fully produced and published; only the GitOps-repo PR is deferred.

## Proving provenance for a running image

Given an image reference (`ghcr.io/<owner>/<image>@sha256:<digest>`):

1. The image carries `org.opencontainers.image.revision` (the exact commit SHA) and
   `org.opencontainers.image.source` (this repository's URL) as OCI labels - inspectable with
   `docker inspect` or `crane config`, no registry-side lookup required.
2. `release-manifest.json` (built by `scripts/build_release_manifest.py`, uploaded as a workflow
   artifact and mirrored into the GitOps repository's `apps/trading-platform/release.json` by
   `gitops-update`) lists every image this release produced, all built from the same commit, with
   their digests.
3. That commit is, by construction (the `guard` job), reachable from `origin/main` at release
   time - `git log --oneline <commit>..origin/main` (empty if it's still the tip) shows exactly
   what has shipped since.

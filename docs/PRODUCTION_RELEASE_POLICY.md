# Production release policy

Production artifacts must be built from a commit reachable from `origin/main`.
Agent branches and detached worktrees are development-only and must not be
deployed directly. Use `scripts/verify_main_release.sh [commit]` before
building or deploying. Release tags should include the short `main` commit;
rollback must select an earlier main-built image, not an agent branch.

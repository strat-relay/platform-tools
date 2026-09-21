#!/bin/sh
# Start ONLY the Trading Control API with the production environment.
# Does not touch the orchestrator, execution consumer, channels, or MT5.
set -eu
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
ENV_FILE="${CONTROL_API_ENV_FILE:-$ROOT/deploy/prod/control_api.env}"
[ -f "$ENV_FILE" ] || { echo "missing $ENV_FILE (copy control_api.env.example)" >&2; exit 1; }
set -a; . "$ENV_FILE"; set +a
cd "$ROOT"
exec python3 -m control_api

# Docker deployment foundation

Docker Compose is the normal deployment model for the eventual platform.
PostgreSQL is the first infrastructure service established under that model.
There is no native-host PostgreSQL deployment path in the plan.

## Commands

```sh
cp .env.example .env                 # replace the local password
docker compose up -d postgres
docker compose run --rm migrate
docker compose run --rm postgres-tests
docker compose up -d                 # future complete platform
```

`postgres` uses the official PostgreSQL 16.4 Alpine image, a named persistent
volume, and a `pg_isready` healthcheck. `docker compose down` preserves the
database volume. Data is removed only with an explicit volume-removal command,
such as `docker compose down -v`.

The migration service applies the versioned migrations already present in
`postgres/migrations`; it does not use `/docker-entrypoint-initdb.d` and does
not import Phase 6 data. Application containers connect to `postgres:5432`,
never `localhost`. PostgreSQL is not published to the host by default because
the migration and test tooling run inside Compose. A host port can be added
later only when a concrete development workflow requires it.

## Backup and restore

Use a disposable or operationally approved target and keep the dump outside the
database volume:

```sh
docker compose exec -T postgres sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > backup.dump
cat backup.dump | docker compose exec -T postgres sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists'
```

The normal backup mechanism is logical `pg_dump`/`pg_restore`, not copying the
raw PostgreSQL volume directory.

## Current host-boundary audit

| Component/dependency | Current endpoint | Classification | Future Docker note |
|---|---|---|---|
| MT5 Data Channel | `127.0.0.1:22347` | HOST_BOUNDARY_REQUIRED | If MT5 remains on macOS, the containerized client will need an explicitly designed host gateway or routed boundary; do not assume this is portable. |
| MT5 Execution Channel | `127.0.0.1:22348` | HOST_BOUNDARY_REQUIRED | Same external MT5 boundary; no live networking change is made here. |
| Control API | `127.0.0.1:22349` | NETWORKING_CHANGE_REQUIRED | Container bind/listen and frontend origin policy must be designed before containerization. |
| Phase 6 runner | local files plus Data Channel | NETWORKING_CHANGE_REQUIRED | File/state mounts and MT5 access require an explicit container design. |
| Signal orchestrator | Execution endpoint `127.0.0.1:22348` | NETWORKING_CHANGE_REQUIRED | Must use Compose DNS for container peers and an explicit host boundary for MT5. |
| Trade Manager / execution consumer | local runtime plus `22348` | NETWORKING_CHANGE_REQUIRED | Not containerized or switched in this task. |
| PostgreSQL | new Compose service | CONTAINER_READY | Use `postgres:5432` from Compose services. |

On Mac development, `host.docker.internal` is a possible Docker Desktop host
gateway for a future explicitly configured MT5 boundary. It is not assumed here
or treated as universal: Linux and production deployments need a deliberate
routing, firewall, and service-discovery design.

MT5 itself remains host-side and is not containerized by this change.

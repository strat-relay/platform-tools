# Trade Manager monitoring follow-up

## Changes

- Market-data symbol resolution accepts exact provider symbols already present in the
  ACTIVE mapping (for example XAUUSDm), as well as canonical keys. It never strips or
  guesses broker suffixes and does not alter execution symbol configuration.
- Decision processing locks the managed trade and detects an existing TIME_EXIT decision.
  Subsequent market observations cannot create additional terminal decisions for that exit.
- An independent five-second clock worker uses its own database connection, statement/lock
  timeouts and readiness status. Synchronous market-data bridge calls cannot occupy that
  worker. This removes a source of delay; it does not prove the precise cause of the
  reported 144-second live delay or guarantee delivery latency during database outages.
- The canonical outcome resolver retains activation evidence for an open limit entry.
  Pending entries remain HOLD with THEORETICAL_PENDING_ENTRY until activation is evidenced.
  Expired, invalidated and ambiguous entries are not price-managed as active positions.
  The holding-period arithmetic still follows the existing outcome contract.
- GET /api/v1/trade-manager/signals lists canonical signals, including those without any
  ManagedTrade or execution intent. It reports theoretical state, registration status,
  latest management decision and captured deadline, with pagination. Broker position
  truth remains on the existing live projection.
- The companion console PR renders this source independently of broker availability,
  labels the broker section separately and removes the misleading NO BROKER EFFECTS badge.

## Deployment and remaining work

Deploy the backend before the console. Roll out both Trade Manager and the unified outcome
resolver so newly processed pending entries retain canonical activation evidence.
No new database migration is required beyond existing migration 068.

There is still no StratRelay subscriber delivery consumer in the inspected repositories.
The discovered bliss-systems/notification-service is a ProCV RabbitMQ placeholder and is
not an established StratRelay delivery service. It was left untouched. Wiring actual
subscriber delivery requires the correct existing service repository or endpoint and its
subscription/entitlement contract. No historical subscriber messages were replayed.

Validation: 313 backend tests passed, 29 integration tests skipped, 20 subtests passed.
The console production build and 38 focused API/UI tests passed. Cluster rollout,
PostgreSQL concurrency and live notification delivery have not been verified here.

# portcast-assignment

## One-command demo runtime

Requires a running Docker daemon and Docker Compose v2. From the repository root:

```sh
docker compose up --build -d --wait
```

If Compose v2 is installed as the standalone `docker-compose` command rather
than the Docker plugin, use `docker-compose` instead of `docker compose` in the
commands below (standalone v2.32.2 was verified available). The Docker daemon
must be running first; for a Colima-based setup, start it with `colima start`.

Compose starts PostgreSQL, a one-shot `setup` job, the reporting `server`, the
schedule `consumer`, and a separate `recovery` worker. No `.env` file or host
Python installation is needed for this demo: containers receive fixed,
development-only database credentials matching PostgreSQL.

Setup waits for healthy PostgreSQL, then runs `init_db`,
`migrate_operation_context`, `migrate_recovery_index`, and `seed_demo` sequentially
using the consumer image. Both APIs and the worker wait for successful setup and
healthy PostgreSQL. Each API has its own database-aware HTTP health check.
The worker has no dedicated health check: `--wait` checks that its process is
running; the runtime smoke check below verifies actual recovery.

Default host ports are PostgreSQL **5433**, reporting **8000**, and schedule
**8001**, all bound to localhost. The worker publishes no port, reuses the
consumer image with a separate command, and has `restart: unless-stopped`,
`init: true`, and a 15-second stop grace period. Images still contain only shared
source plus their respective application.

Find the seeded demo organization ID and check both APIs:

```sh
docker compose logs setup
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8001/health
```

Use that organization ID with the schedule request on port 8001 in
[Metered demo consumer](#metered-demo-consumer) and the reporting request below.
Setup is serial development provisioning, not a general production migration
system. Seeding does not refill an existing allowance. Starting the default
project can reuse an older named volume; this command does not reset data.

### Runtime smoke check (optional host tooling)

This check requires host `uv` dependencies (`uv sync --locked`) and database
configuration from a copied `.env.example` → `.env` or exported `DB_*` values.
The configured database must be the same one used by both API URLs (defaults
`http://127.0.0.1:8000` and `http://127.0.0.1:8001`).

```sh
uv run python -m scripts.runtime_smoke --allow-demo-writes
```

The script creates a unique organization fixture and cleans up its rows. It checks
real HTTP health, schedule requests, saved-response replay, input conflict, and
usage reporting, then commits an expired hold and waits for the actual polling
worker to resolve it. This simulates abandonment; it does not crash a process.

Fresh startup and this smoke check passed on an isolated Compose project using:

```sh
POSTGRES_HOST_PORT=55433 SERVER_HOST_PORT=18000 CONSUMER_HOST_PORT=18001 \
  docker-compose -p portcast-runtime-check up --build -d --wait
DB_HOST=127.0.0.1 DB_PORT=55433 DB_NAME=portcast \
  DB_USERNAME=portcast DB_PASSWORD=portcast_dev \
  uv run python -m scripts.runtime_smoke --allow-demo-writes \
  --server-url http://127.0.0.1:18000 --consumer-url http://127.0.0.1:18001
```

Full `down`/`up` with the volume retained was also verified: setup reran without
an `.env` file, the demo balance remained 499/500, and the saved response replayed
without another charge. The isolated verification stack/volume were then removed;
the original development database was left running and its volume preserved.
The full suite passed **43 tests** on a separate `portcast_test` database while
the demo worker ran against `portcast`. These checks are not load benchmarks.

## Development database

For host-based development, start just PostgreSQL instead of the full runtime:

```sh
docker compose up -d --wait postgres
```

The database is available at `127.0.0.1:5433` (container port remains `5432`):

```text
postgresql://portcast:portcast_dev@127.0.0.1:5433/portcast
```

These credentials are for local development only. The port is bound to localhost,
and database files persist in the `postgres_data` named volume.

Open a SQL shell without installing PostgreSQL locally:

```sh
docker compose exec postgres psql -U portcast -d portcast
```

Check service status:

```sh
docker compose ps
```

Stop the Compose runtime while preserving database data:

```sh
docker compose down
```

**Destructive:** only use the following to intentionally delete all local database
data as well:

```sh
docker compose down -v
```

## Separate FastAPI applications

For host-based applications, use the PostgreSQL-only command above, copy
`.env.example` to `.env` if you do not already have one (or export `DB_*` values),
and run the explicit schema/seed commands below. Install the locked dependencies:

```sh
uv sync --locked
```

Start each application in its own terminal:

```sh
# Reporting server: usage + health
uv run uvicorn src.server.main:app --host 127.0.0.1 --port 8000

# Schedule consumer: schedule API + health
uv run uvicorn src.consumer.main:app --host 127.0.0.1 --port 8001
```

Neither application imports the other. Both directly use the shared library and
the same PostgreSQL database; quota accounting is not an HTTP service.

### Source layout

```text
src/server/main.py             Reporting API and health
src/consumer/main.py           Schedule API and health
src/consumer/service.py        Schedule logic
src/consumer/schemas.py        Schedule request/response models
src/consumer/seed_demo.py       Demo provisioning
src/shared/config.py           Configuration
src/shared/database.py         Database connection/session setup
src/shared/models.py           SQLAlchemy tables
src/shared/quota.py            Quota accounting and reporting
src/shared/schemas.py          Shared response models
src/shared/init_db.py          Explicit schema initialization
src/shared/migrate_operation_context.py  Additive migration
```

Both apps use SQLAlchemy with the psycopg driver. The example `.env` contains:

```dotenv
DB_HOST=127.0.0.1
DB_PORT=5433
DB_NAME=portcast
DB_USERNAME=portcast
DB_PASSWORD=portcast_dev
```

`src/shared/config.py` loads the root `.env` using `python-dotenv` and validates these
required values with Pydantic. Existing environment variables take precedence.
The connection URL is built centrally with SQLAlchemy's URL builder, which handles
special characters in credentials. Passwords are masked in the configuration's
representation. `.env` is ignored by Git; `.env.example` contains only development
credentials and is safe to track.

Change the `DB_*` values for another database. Deployment credentials should not use
the development values. Startup verifies
database connectivity; shutdown disposes of the connection pool. Each database
request gets a session, but transaction commits are explicit, not automatic.

Check connectivity at `http://127.0.0.1:8000/health` (server) and
`http://127.0.0.1:8001/health` (consumer). Each returns `{"status":"ok"}`
when the database is reachable and HTTP 503 if a running API loses database access.
If PostgreSQL is unavailable at startup, the API fails startup instead of reporting
readiness. FastAPI startup does not create tables or run migrations.

## Development schema

Create the five tables from the current logical schema explicitly:

```sh
uv run python -m src.shared.init_db
```

`src/shared/models.py` defines organizations, feature costs/lease durations, per-request
allocations, purchased-credit balances, and current-month balances. The initial
feature enum contains `container-tracking` and `sailing-schedule`. Operation states
are `RESERVED`, `DONE`, and `RELEASED`.

Both balance tables count reservations in `units_consumed`; finalizing success
must not deduct again. Database constraints enforce nonnegative balances,
`total_allocated = units_consumed + units_remaining`, and scoped uniqueness.
These constraints alone do not implement concurrent admission or safe recovery.
The code must preserve `reserved_at` and use claim/version checks for transitions.

This command creates missing tables; it does **not** migrate existing tables or
update existing enum types. Schema evolution needs explicit migrations; the
operation-context extension has a small additive migration described below.
There is no contract model yet. Recovery polling uses an additional partial index
with its own explicit migration below.
Pydantic validates configuration and API request/response schemas; these table
definitions use SQLAlchemy, not Pydantic API models.

## Dedicated test database

Use a separate database so a running recovery worker cannot claim test fixtures.
Never point these tests at a production database. With the default Compose
PostgreSQL running and host dependencies/configuration ready:

```sh
# Create once; skip this command if portcast_test already exists.
docker compose exec -T postgres createdb -U portcast portcast_test

DB_NAME=portcast_test uv run python -m src.shared.init_db
DB_NAME=portcast_test uv run python -m src.shared.migrate_operation_context
DB_NAME=portcast_test uv run python -m src.shared.migrate_recovery_index
DB_NAME=portcast_test uv run pytest -q
```

Keep the API and polling worker on `portcast`, not `portcast_test`. Tests clean up
their own rows; no blanket database reset is needed. `DB_NAME` overrides the root
`.env` value. If using custom host ports, also set `DB_PORT` to that project's
published PostgreSQL port. Schema commands do not drop existing data.

## Quota reservation and settlement

With PostgreSQL running and the development schema initialized:

```sh
uv sync --locked
DB_NAME=portcast_test uv run pytest
```

`src/shared/quota.py` claims an operation key using PostgreSQL `INSERT ... ON CONFLICT
DO NOTHING`. `reserve_quota(...)` then locks the monthly balance to calculate
the allocation, consumes included units first, and conditionally acquires any
remaining units from unexpired credits. Lock order is operation, monthly balance,
then credit balance. Admission/commit failure rolls back both sources and the
operation claim; no partial batch is admitted. A
concurrent duplicate waits for the original transaction and returns its existing
operation without another deduction, even if the balance is now exhausted.
Reservation timestamps use the PostgreSQL clock; returned operations are detached
snapshots. Callers must supply a session without an active transaction. This flow
uses PostgreSQL's default READ COMMITTED isolation.

Keys are scoped to `(org_id, feature, idempotency_key)`. Reusing a key with a
different unit count raises `IdempotencyConflict`. Replays preserve the original
reservation time, lease, and claim version; they do not renew ownership or restart
released work. Callers must inspect the returned status.

The operation may store normalized `request_payload`; replay checks both units
and that payload. The demo consumer supplies it, so equal-cost input changes are
rejected too. Bare quota callers that omit it only get unit-level matching.
Results can be persisted atomically with `DONE` for response replay. Retaining the
operation/result is necessary for replay; automatic retention/cleanup is not yet
implemented. Previously stored rows have null input/result fields and cannot be
treated as completed demo requests with recoverable results.

The basic tests cover reservation, rejection without side effects, replay after
exhaustion, and conflicting unit counts. This slice assumes configured balances;
expired monthly rows are refreshed lazily. Mixed-allocation tests verify the recorded source split,
uncharged replay, and rollback when credits are insufficient or expired.
Lease-based recovery is described below; cache behavior and contract eligibility
are not implemented yet. In particular, credit expiry is not a
replacement for checking active-contract/grace eligibility.

### Calendar-month reset

The first new admission or release touching an expired monthly aggregate refreshes
it under its row lock. Reset sets consumed units to zero and remaining units to
the configured allowance, then moves `resets_on` to the first of the next month
at 00:00 UTC. Skipped months grant only the current allowance, with no rollover.
Purchased credits are not reset. No scheduled reset job or historical aggregate
table is required.

PostgreSQL supplies the authoritative time after monthly-lock acquisition;
`dateutil` handles calendar arithmetic. A new operation's original reservation
timestamp/lease are assigned at that admission point before commit, not before
lock waits. Committed timestamps remain unchanged during replay or recovery.
Known-key replay returns the existing operation and does not grant fresh capacity.

Reset and admission share a transaction; rejected admission rolls its changes
back too. A later admission/reporting access can refresh the row again. The usage
endpoint uses the same locked refresh policy, even before any new-month admission.

### Usage endpoint

```text
GET http://127.0.0.1:8000/orgs/{org_id}/features/{feature}/usage
```

```sh
curl http://127.0.0.1:8000/orgs/1/features/sailing-schedule/usage
```

For example, `/orgs/1/features/container-tracking/usage` reports:

```json
{
  "org_id": 1,
  "feature": "container-tracking",
  "period_start": "2026-10-01T00:00:00Z",
  "next_reset": "2026-11-01T00:00:00Z",
  "monthly": {"limit": 10, "used": 3, "reserved": 7, "available": 0},
  "credits": {"reserved": 2, "available": 5, "expires_on": "2027-01-01T00:00:00Z"}
}
```

This is an illustrative response, not seeded data. `monthly.used` is completed
included usage; pending allocations are separate in `monthly.reserved`.
`credits.reserved` includes outstanding credit holds across all periods. Missing
or expired credits report zero available; missing credits have a null expiry.
This does not yet account for contract/grace eligibility.

Reporting locks balance rows in monthly/credit order and reads both reservation
totals in one statement snapshot. It does not lock individual operations or change
their states. Reads can wait on active accounting transactions; benchmark that
cost rather than assume reporting is contention-free. HTTP 404 means no configured
quota, 422 means an invalid feature/input, and 503 means a database access failure.
This demo endpoint is not authenticated; production must verify organization access.

### Finalization and source-specific release

`finalize_reservation(session, operation_id=..., claim_version=...)` marks confirmed
success as `DONE` without another deduction. `release_reservation(...)` marks a
confirmed failure `RELEASED` and refunds its monthly and credit allocation once. Both own a
short transaction and lock the operation row while checking its claim version
and state. Repeating the same transition is harmless; an opposite terminal
transition or stale claim raises `OperationConflict`.

Release locks the current monthly balance row too, refunding only when its
`resets_on` boundary identifies the reservation's original UTC month. An old-month
operation is closed without adding capacity to a newer month. The monthly reset
implementation must update that boundary atomically with its counters.
Release itself uses the locked refresh policy before deciding whether its monthly
allocation still belongs to the current aggregate.

Purchased credits are restored to their own balance, including after a monthly
reset. Release never extends `expires_on`; an expired balance may show refunded
units but those units remain unavailable for new admissions. Credit balance rows
must be retained while unresolved reservations refer to them. The upcoming
contract/credit-lifecycle code must coordinate expiration, replacement, and renewal
with outstanding holds; this slice does not implement those lifecycle transitions.

Settlement takes a confirmed outcome from the consumer; it does not decide that
a timeout or expired lease means failure. Only claim version, not elapsed lease
time alone, identifies changed ownership. Finalization supports monthly-only,
mixed, and credit-only allocations without changing either balance again.
Internal operation IDs are not authorization: a future
API layer must verify organization access before calling settlement.

The basic tests use real PostgreSQL from `.env`, with an outer transaction and
savepoints that roll back test rows even when the function commits. Run all tests
against a local development database only.

The contention test uses four spawned processes, each with independent database
connections and actual commits. It holds the quota row until all four processes
are observed waiting on a PostgreSQL lock, then releases them. Forty one-unit
requests compete for 25 units: exactly 25 must be accepted, 15 rejected, zero
remain, and persisted reservations must match the accounting. Unexpected database
errors fail the test rather than being counted as quota rejections. A second
cross-process test sends 40 copies of one key requiring all available units:
every caller must receive the same operation, with only one deduction and one
persisted reservation. It runs for monthly-only and mixed allocations. Committed
fixture rows are explicitly cleaned up afterward.

A third cross-process test releases one reservation from four processes at once,
after observing lock contention. All receive `RELEASED`, but the balance is
refunded exactly once for monthly-only and mixed allocations. Basic transition
tests cover repeat calls, incompatible terminal transitions, stale claims, release
after a simulated monthly reset, and credit-only refunds after expiry.

A fourth cross-process test runs 40 batches of seven against 25 included units
and 10 purchased credits: exactly five complete batches consume all 35 units,
35 batches are rejected, and allocation records match both balances.

```sh
DB_NAME=portcast_test uv run pytest tests/test_quota_concurrency.py -q
```

This requires visibility of the test connections in `pg_stat_activity` (the local
Compose database user supports it). It exercises quota admission under real
cross-process contention, not completed downstream work, failover, or load-test
latency. Shared development feature configuration should not be changed while
running this test; it is not intended for a production database.

Reset tests replace only the clock seam while still using real Postgres. They
cover the exact UTC boundary, leap February/year transitions, skipped months,
unchanged credits, and old-period release. Cross-process exhaustion also runs
against an expired monthly aggregate: concurrent first requests must collectively
receive one allowance, not one allowance per instance.

## Metered demo consumer

For an existing database, add the nullable operation-context columns explicitly.
For a fresh database, `init_db` already includes them. Both commands are repeatable:

```sh
uv run python -m src.shared.init_db
uv run python -m src.shared.migrate_operation_context
uv run python -m src.consumer.seed_demo
uv run uvicorn src.consumer.main:app --host 127.0.0.1 --port 8001
```

The seed command prints the demo organization ID. It provisions 500 monthly
schedule units but does not refill an existing balance. Run seeding serially as
a local setup command, not concurrently in every API instance.

Use the printed organization ID in place of `1` below:

```sh
curl -X POST http://127.0.0.1:8001/orgs/1/schedule-searches \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: 16fc2c3e-e3d6-4e49-ad32-98bda3917c83' \
  -d '{"routes":["SGSIN-NLRTM","SGSIN-GBFXT"]}'
```

Each route costs the configured schedule unit price (one for the demo). The batch
is admitted all-or-nothing. The result contains explicitly fictitious sailings.
Repeat the same key/body to recover the same saved response without another
charge, even if the configured unit price has since changed. New operations use
the current price. Changing relevant inputs with the same key returns 409. A new key is a
new operation; fresh-result reuse across keys is not implemented yet.

Set `demo_behavior` to `fail` or `timeout` to inject confirmed failures. These
return 502/504 and release the hold. The timeout is an injected, safely stopped
failure—not a real remote call whose outcome is uncertain. Replaying a released
operation returns 409; starting new work requires a new key. Insufficient capacity
returns 429, validation errors 422, and database errors 503. Unknown accounting
outcomes never automatically trigger a refund.

The feature computation is pure and runs outside quota transactions. A retry of an
active unfinished operation returns 409 without running the lookup. After lease
expiry, a retry may claim and recover it. An expired original executor might still
be running, so repeated pure computation remains possible; only the current claim
version can settle. Completed retries do not compute again.
This is not an exactly-once guarantee for external mutations. The completed result
and `DONE` status commit together, making a lost HTTP response recoverable.

**Charge boundary for this demo:** a valid result durably recorded with `DONE`,
not proof the LB transmitted it. This is an explicit implementation limitation
relative to the desired delivery-boundary policy; network delivery cannot be
atomically coordinated with the database here. The endpoint is demo-only with
no production organization authentication or contract enforcement.

```sh
DB_NAME=portcast_test uv run pytest tests/test_consumer.py -q
```

Consumer tests cover successful outcome/replay, equal-cost input conflict,
confirmed failure/refund, no execution after quota/validation rejection, and
preservation of the hold during uncertain final accounting. The migration adds
columns without deleting rows; it is not a general migration framework.

## Docker applications

Build the separate images from the repository root:

```sh
docker build -f Dockerfile.server -t portcast-server .
docker build -f Dockerfile.consumer -t portcast-consumer .
```

Both install locked dependencies with `uv`. `Dockerfile.server` copies only
shared + server source; `Dockerfile.consumer` copies only shared + consumer source.
Supply database credentials at runtime with `--env-file .env`; do not embed
secrets in images.

As a manual-container alternative, start only the Compose `postgres` service and
complete the schema/seed commands above, then attach both applications to its network:

```sh
docker network ls
docker run --network portcast-assignment_default --env-file .env -e DB_HOST=postgres -e DB_PORT=5432 -p 127.0.0.1:8000:8000 portcast-server
docker run --network portcast-assignment_default --env-file .env -e DB_HOST=postgres -e DB_PORT=5432 -p 127.0.0.1:8001:8000 portcast-consumer
```

Run the two containers in separate terminals. The Compose network name can vary;
use the actual name from `docker network ls`. Container-to-container database
traffic uses `postgres:5432`, while host commands use `127.0.0.1:5433`.

For Docker Desktop, containers can alternatively reach the host-published database:

```sh
docker run --env-file .env -e DB_HOST=host.docker.internal -p 127.0.0.1:8000:8000 portcast-server
docker run --env-file .env -e DB_HOST=host.docker.internal -p 127.0.0.1:8001:8000 portcast-consumer
```

These commands use the `.env` host database port, normally 5433. With Colima,
host routing depends on the environment; prefer the shared Compose network above.
The full Compose runtime automates setup separately; application startup itself
does not run schema migrations. The schedule API and recovery worker are distinct
processes in the consumer package. Compose starts the worker as its own service;
manual host/container deployments use the explicit commands below.

## Lease recovery and separate polling worker

For an existing database, add the recovery index explicitly (repeatable):

```sh
uv run python -m src.shared.migrate_recovery_index
```

Fresh schema creation includes this partial index. The development migration uses
ordinary `CREATE INDEX`, which can block writes while building. Large production
tables would need a separately planned concurrent index migration.

Start the worker in a separate terminal/task:

```sh
uv run python -m src.consumer.recovery_worker

# One bounded poll, useful for smoke checks
uv run python -m src.consumer.recovery_worker --once

# Optional tuning
uv run python -m src.consumer.recovery_worker --poll-interval 5 --batch-size 20
```

The demo feature's lease is configured in `api_quota_map` (30 seconds in the seed).
Polling defaults to five seconds and at most 20 claims per iteration. Polling and
lease durations are independent; the worker waits between iterations. SIGTERM and
SIGINT stop it after its current bounded batch and dispose the connection pool.
Restart scanning is simply the next poll, not a per-API background thread.

`claim_expired_reservations()` locks eligible `RESERVED` operations with
`FOR UPDATE SKIP LOCKED`, increments `claim_version`, and renews the ownership
deadline using the PostgreSQL clock. It preserves `reserved_at`, allocation, and
balances. A bounded partial index supports polling. Claims commit before any
feature work. Contending workers skip locked operations rather than waiting.

Recognized expired client retries use the same claim primitive, scoped to their
operation. Both retry and polling paths call `execute_schedule_operation()` with
persisted inputs and never reserve capacity again. The consumer passes
`reject_pending_replay=True` during admission, so concurrent first requests that
both missed the replay lookup do not both execute an active reservation.

The worker handles only `sailing-schedule`, whose fictitious lookup is pure and
safe to repeat. It does **not** automatically replay tracking writes or arbitrary
third-party mutations. Confirmed injected failure/timeout releases the hold;
missing/malformed context, unknown errors, or ambiguous database commits are
logged by operation ID, claim version, and error type without blind refunds.
Database errors during polling are retried on a later iteration; `--once` returns
exit status 1 if the database poll fails.

**Limits:** no heartbeat, hard execution deadline, or permanent retry cap is
implemented. Unresolved eligible demo operations can be retried after the renewed
lease expires; legacy rows without valid inputs require operator attention. Batch
leases start together and execution is sequential, so long work could outlive a
lease before/during execution. That is acceptable only for this cheap pure demo;
production features need appropriate deadlines, smaller batches/lease renewal,
and feature-specific outcome lookup or downstream idempotency. Claim fencing
prevents stale database settlement, not duplicate external side effects. A crash
after computing but before persisting `DONE` may require safe recomputation.

The separately deployed worker can reuse the consumer image without starting its
HTTP app or including the server package:

```sh
docker build -f Dockerfile.consumer -t portcast-consumer .
docker run --network portcast-assignment_default --env-file .env -e DB_HOST=postgres -e DB_PORT=5432 portcast-consumer /app/.venv/bin/python -m src.consumer.recovery_worker
```

Use your actual Compose network name. No published port is needed for the worker.
This does not add a third source package or merge the server into the consumer.

```sh
DB_NAME=portcast_test uv run pytest tests/test_recovery.py tests/test_consumer.py tests/test_quota_concurrency.py -q
```

Tests cover abandoned durable inputs, bounded recovery, confirmed refunds,
unknown outcomes retaining holds, active/expired retries, original allocation and
timestamp preservation, and stale-owner rejection. Four independent processes
verify a held operation lock is skipped and a subsequent racing takeover commits
once. That claim test synchronizes process readiness; unlike admission tests,
`SKIP LOCKED` does not wait for an observed row-lock contention gate. A simulated
lost acknowledgement after finalization commits verifies saved-result replay with
no refund or second charge; it is not an actual database/network failover test.

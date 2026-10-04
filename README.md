# portcast-assignment

## Development database

Requires Docker with Docker Compose v2. Start PostgreSQL 17 and wait for readiness:

```sh
docker compose up -d --wait
```

If Compose v2 is installed as the standalone `docker-compose` command rather
than the Docker plugin, use `docker-compose` instead of `docker compose` in the
commands below (for example, `docker-compose up -d --wait`). The Docker daemon
must be running first; for a Colima-based setup, start it with `colima start`.

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

Stop the database while preserving its data:

```sh
docker compose down
```

To intentionally delete all local database data as well:

```sh
docker compose down -v
```

This setup starts only the development database, not the application or recovery
worker. No application schema is created yet.

## FastAPI database connection

With PostgreSQL running, copy `.env.example` to `.env` if you do not already have
one. Install the locked Python dependencies and start the API:

```sh
uv sync --locked
uv run fastapi dev src/main.py
```

The API uses SQLAlchemy with the psycopg driver. The example `.env` contains:

```dotenv
DB_HOST=127.0.0.1
DB_PORT=5433
DB_NAME=portcast
DB_USERNAME=portcast
DB_PASSWORD=portcast_dev
```

`src/config.py` loads the root `.env` using `python-dotenv` and validates these
required values with Pydantic. Existing environment variables take precedence.
The connection URL is built centrally with SQLAlchemy's URL builder, which handles
special characters in credentials. Passwords are masked in the configuration's
representation. `.env` is ignored by Git; `.env.example` contains only development
credentials and is safe to track.

Change the `DB_*` values for another database. Deployment credentials should not use
the development values. Startup verifies
database connectivity; shutdown disposes of the connection pool. Each database
request gets a session, but transaction commits are explicit, not automatic.

Check connectivity at `http://127.0.0.1:8000/health`. It returns `{"status":"ok"}`
when the database is reachable and HTTP 503 if a running API loses database access.
If PostgreSQL is unavailable at startup, the API fails startup instead of reporting
readiness. FastAPI startup does not create tables or run migrations.

## Development schema

Create the five tables from the current logical schema explicitly:

```sh
uv run python -m src.init_db
```

`src/models.py` defines organizations, feature costs/lease durations, per-request
allocations, purchased-credit balances, and current-month balances. The initial
feature enum contains `container-tracking` and `sailing-schedule`. Operation states
are `RESERVED`, `DONE`, and `RELEASED`.

Both balance tables count reservations in `units_consumed`; finalizing success
must not deduct again. Database constraints enforce nonnegative balances,
`total_allocated = units_consumed + units_remaining`, and scoped uniqueness.
These constraints alone do not implement concurrent admission or safe recovery.
The code must preserve `reserved_at` and use claim/version checks for transitions.

This command creates missing tables; it does **not** migrate existing tables or
update existing enum types. Schema evolution will need migrations. There is no
seed data, contract model, or recovery worker implementation yet.
Pydantic currently validates configuration and the health response; these table
definitions use SQLAlchemy, not Pydantic API models.

## First monthly reservation slice

With PostgreSQL running and the development schema initialized:

```sh
uv sync --locked
uv run pytest
```

`src/quota.py` claims an operation key using PostgreSQL `INSERT ... ON CONFLICT
DO NOTHING`, then reserves monthly capacity using a conditional SQL update in
the same transaction. Admission/commit failure rolls back both changes. A
concurrent duplicate waits for the original transaction and returns its existing
operation without another deduction, even if the balance is now exhausted.
Reservation timestamps use the PostgreSQL clock; returned operations are detached
snapshots. Callers must supply a session without an active transaction. This flow
uses PostgreSQL's default READ COMMITTED isolation.

Keys are scoped to `(org_id, feature, idempotency_key)`. Reusing a key with a
different unit count raises `IdempotencyConflict`. Replays preserve the original
reservation time, lease, and claim version; they do not renew ownership or restart
released work. Callers must inspect the returned status.

**Current limitation:** the schema does not yet store request inputs or a
fingerprint. Different business inputs with the same unit count cannot be detected
here; the consumer will need that check before this is full request-level
idempotency. Replay also depends on retaining the operation record. No key-retention
or response-replay policy is implemented yet.

The basic tests cover reservation, rejection without side effects, replay after
exhaustion, and conflicting unit counts. This slice assumes configured,
current-period balances. Credits, automatic period resets, ownership recovery,
cache behavior, and contract eligibility are not implemented yet.

### Monthly finalization and release

`finalize_reservation(session, operation_id=..., claim_version=...)` marks confirmed
success as `DONE` without another deduction. `release_reservation(...)` marks a
confirmed failure `RELEASED` and refunds its monthly allocation once. Both own a
short transaction and lock the operation row while checking its claim version
and state. Repeating the same transition is harmless; an opposite terminal
transition or stale claim raises `OperationConflict`.

Release locks the current monthly balance row too, refunding only when its
`resets_on` boundary identifies the reservation's original UTC month. An old-month
operation is closed without adding capacity to a newer month. The monthly reset
implementation must update that boundary atomically with its counters.

Settlement takes a confirmed outcome from the consumer; it does not decide that
a timeout or expired lease means failure. Only claim version, not elapsed lease
time alone, identifies changed ownership. Credit-funded settlement is explicitly
unsupported in this slice. Internal operation IDs are not authorization: a future
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
cross-process test sends 40 copies of one key requiring all 25 available units:
every caller must receive the same operation, with only one deduction and one
persisted reservation. Committed fixture rows are explicitly cleaned up afterward.

A third cross-process test releases one reservation from four processes at once,
after observing lock contention. All receive `RELEASED`, but the balance is
refunded exactly once. Basic transition tests cover repeat calls, incompatible
terminal transitions, stale claims, and release after a simulated monthly reset.

```sh
uv run pytest tests/test_quota_concurrency.py -q
```

This requires visibility of the test connections in `pg_stat_activity` (the local
Compose database user supports it). It exercises monthly admission under real
cross-process contention, not completed downstream work, failover, or load-test
latency. Shared development feature configuration should not be changed while
running this test; it is not intended for a production database.

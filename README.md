# Per-customer quota metering

A Python/PostgreSQL component for monthly quotas per organization and feature.
It supports concurrent admission across instances, all-or-nothing batches,
retry-safe accounting, failure refunds, and usage reporting. A fake schedule-search
API demonstrates the integration; a separate worker recovers abandoned reservations.

## Quick start

Requires a running **Docker daemon and Docker Compose v2**. From the repository root:

```sh
docker compose up --build -d --wait
docker compose logs setup
```

Use `docker-compose` if your v2 installation is standalone. No host Python or `.env`
is needed for the container demo. Setup initializes/migrates the database and seeds
a demo organization with 500 monthly schedule units; its ID appears in the setup logs.
Seeding does **not** refill an existing balance.

| Component | Default address |
|---|---|
| Schedule consumer / API docs | <http://127.0.0.1:8001/docs> |
| Usage reporting / API docs | <http://127.0.0.1:8000/docs> |
| PostgreSQL | `127.0.0.1:5433` |
| Recovery worker | Separate process; no exposed port |

Both APIs expose `/health`. Ports bind to localhost; credentials are development-only.
The stack uses a persistent named volume. Stop without deleting its data:

```sh
docker compose down
```

**Do not add `-v` unless you intend to delete the database.** Optional host-port
overrides are listed in [`.env.example`](.env.example).

## Try it

Replace `1` with the organization ID printed by setup:

```sh
ORG_ID=1
KEY=16fc2c3e-e3d6-4e49-ad32-98bda3917c83

curl -X POST "http://127.0.0.1:8001/orgs/$ORG_ID/schedule-searches" \
  -H 'Content-Type: application/json' \
  -H "Idempotency-Key: $KEY" \
  -d '{"routes":["SGSIN-NLRTM","SGSIN-GBFXT"]}'

curl "http://127.0.0.1:8000/orgs/$ORG_ID/features/sailing-schedule/usage"
```

Each route costs one unit with the seeded configuration. Results contain fictitious
sailings. Repeat the **same key and body** to replay the saved result without another
charge; changed inputs with that key return 409. New work requires a new UUID key.
With a new key, set `demo_behavior` to `fail` or `timeout` to exercise confirmed failure/refund handling.

## Run tests

Host tooling requires **`uv` and Python 3.13+**. With the Compose database running,
use the isolated `portcast_test` database, never the demo or a production database:

```sh
uv sync --locked

# Create once; skip if portcast_test already exists.
docker compose exec -T postgres createdb -U portcast portcast_test

export DB_HOST=127.0.0.1 DB_PORT=5433 DB_NAME=portcast_test
export DB_USERNAME=portcast DB_PASSWORD=portcast_dev

uv run python -m src.shared.init_db
uv run python -m src.shared.migrate_operation_context
uv run python -m src.shared.migrate_recovery_index
uv run pytest -q
```

Latest retained suite: **70 tests passed**, including real PostgreSQL contention,
rollback, retries, refunds, monthly boundaries, reporting, and recovery. Run the
suite sequentially; fixtures share feature configuration. Containers remain on
`portcast`, independent of these host environment variables. Adjust `DB_PORT` if
you changed the published database port.

Optional runtime smoke check, using the host settings above but the demo database:

```sh
DB_NAME=portcast uv run python -m scripts.runtime_smoke --allow-demo-writes
```

It exercises HTTP behavior and the actual recovery worker with its own synthetic
fixture, then cleans up its rows. It simulates abandonment, not an actual process crash.

## Architecture at a glance

- **`src/consumer/`** — schedule API, pure demo feature, separate recovery process.
- **`src/server/`** — usage reporting and health.
- **`src/shared/`** — PostgreSQL models, configuration, transactional quota library.

Both apps import the library directly; neither calls a separate quota HTTP service.
PostgreSQL is authoritative. Admission reserves capacity before work; settlement
records success or refunds confirmed failure. The design, author-drawn diagrams,
concurrency reasoning, reset policy, and deferred extensions are in
**[DESIGN.md](DESIGN.md)**.

## Performance and scope

We retained the tested SQLAlchemy implementation and consolidated fresh admission
into one transaction. Small comparisons demonstrated promising reductions through
direct asyncpg and server-side admission; these remain **experiments, not integrated
performance guarantees**. The design records the actual load-test limits and the
deliberate decision to avoid a late partial refactor.

This is a local demo with a pure feature and demo identity assumptions—not production
authentication, carrier integration, or an exactly-once external-write guarantee.

## Further reading

- [Design decisions, measurements, scaling and limitations](DESIGN.md)
- [k6 HTTP workloads and accounting verification](benchmarks/README.md) — isolated `portcast_load` database.
- [Manual transaction experiments](tests/experiments/README.md) — optional tools, not automatic tests or application dependencies.
- [Human–AI collaboration and attribution](llm_conversation/COLLABORATION.md)
- [Architecture discussion](discussion/ARCHITECTURE_DISCUSSION.md) — historical proposals, not the implementation contract.

The previous detailed README is temporarily preserved verbatim in `temp.md` for
review before final documentation cleanup.

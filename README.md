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
seed data, quota execution, contract model, or recovery worker implementation yet.
Pydantic currently validates configuration and the health response; these table
definitions use SQLAlchemy, not Pydantic API models.

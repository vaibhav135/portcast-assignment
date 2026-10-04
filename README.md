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

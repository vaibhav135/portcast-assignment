# Load-test guide

k6 sends HTTP requests to the **SQLAlchemy-based schedule consumer**. The Python
fixture tool prepares isolated data and checks the resulting database accounting.
Neither tool is part of the application runtime.

Our historical HTTP load tests exposed limits in this implementation. Direct
asyncpg and PostgreSQL functions were tested **only through small transaction
comparisons, not under HTTP load**. Results and the deliberate implementation
tradeoff are in [DESIGN.md](../DESIGN.md); alternatives live in
[manual experiments](../tests/experiments/README.md).

## Terms

| Term | Meaning |
|---|---|
| VU (virtual user) | One simulated client; not an organization or API server |
| Rate | Requested starts per second; the example sends one HTTP request per start |
| p95 | 95% of measured samples took this long or less |
| Dropped iterations | Scheduled work k6 never started; not quota denials or successful requests |

## One small verification run

Requires Docker/Compose and `uv`. Run from the repository root; use `docker-compose`
instead if that is your Compose v2 command. Never target production or run alongside pytest.

### 1. Prepare an isolated database

```sh
uv sync --locked
docker compose up -d --wait postgres

# Create once; skip if portcast_load already exists. Do not drop/reset it.
docker compose exec -T postgres createdb -U portcast portcast_load

export DB_HOST=127.0.0.1 DB_PORT=5433 DB_NAME=portcast_load
export DB_USERNAME=portcast DB_PASSWORD=portcast_dev
uv run python -m src.shared.init_db
uv run python -m src.shared.migrate_operation_context
uv run python -m src.shared.migrate_recovery_index
uv run python -m src.consumer.seed_demo

mkdir -p benchmarks/results
RUN=quick-check  # Choose an unused name for every run.
uv run python -m scripts.quota_load prepare --allow-demo-writes --orgs 100 \
  --manifest "benchmarks/results/$RUN-fixture.json"
```

These are local development credentials. Adjust `DB_PORT` if your database uses a
different published port. Fixtures are separate from the seeded demo organization.

### 2. Start the benchmark consumer and send requests

Use the actual Compose network name from `docker network ls`. This separate
consumer has four workers and timing enabled; no recovery worker targets its database.

```sh
NETWORK=portcast-assignment_default
docker build -f Dockerfile.consumer -t portcast-quota-bench .
docker run -d --name quota-bench-api --network "$NETWORK" --init \
  -e DB_HOST=postgres -e DB_PORT=5432 -e DB_NAME=portcast_load \
  -e DB_USERNAME=portcast -e DB_PASSWORD=portcast_dev \
  -e QUOTA_BENCHMARK_TIMING=1 portcast-quota-bench \
  /app/.venv/bin/uvicorn src.consumer.main:app \
  --host 0.0.0.0 --port 8000 --workers 4 --no-access-log
docker logs quota-bench-api
```

Wait for application startup to complete before running k6. The following starts
five requests/sec for ten seconds, using five simulated clients:

```sh
docker run --rm --user "$(id -u):$(id -g)" --network "$NETWORK" \
  -v "$PWD/benchmarks:/benchmarks" -w /benchmarks \
  -e FIXTURE="/benchmarks/results/$RUN-fixture.json" \
  -e SUMMARY="/benchmarks/results/$RUN-summary.json" \
  -e BASE_URL=http://quota-bench-api:8000 \
  -e WORKLOAD=distributed -e RATE=5 -e DURATION=10s \
  -e VUS=5 -e MAX_VUS=5 -e K6_VERSION=1.0.0 \
  grafana/k6:1.0.0 run quota_load.js
```

This is a small verification, not a capacity claim or the historical 2,000/sec run.
k6 may exit nonzero when its latency threshold is missed; **still verify accounting**.

### 3. Verify, then clean up

```sh
uv run python -m scripts.quota_load verify \
  --manifest "benchmarks/results/$RUN-fixture.json" \
  --summary "benchmarks/results/$RUN-summary.json" \
  --output "benchmarks/results/$RUN-accounting.json"
```

Review the report, stop the consumer, and clean only the owned fixture:

```sh
docker stop quota-bench-api
docker rm quota-bench-api
uv run python -m scripts.quota_load cleanup --allow-demo-writes \
  --manifest "benchmarks/results/$RUN-fixture.json"
```

Cleanup refuses unresolved holds. Do not delete databases/volumes or refund uncertain
work to make a check pass. Results/manifests remain local and Git-ignored; never reuse
a fixture for another measured run.

## Reading the output

HTTP latency and quota time are different. `quota_total_ms` adds admission and
finalization for each new paid request; its default threshold is p95 <10ms, our
measurement interpretation—not a percentile specified by the PDF. Timing is opt-in
and has overhead. Offered rate is not achieved throughput; completion rates can include drain.

Accounting consistency, response checks, dropped starts, and performance thresholds
are reported separately. Other script modes are `hot`, `batch`, `retry`, `failure`
and `exhaustion`; batches charge multiple units and retry mode can send two requests
per start. Each requires a fresh fixture and an explicitly chosen workload.

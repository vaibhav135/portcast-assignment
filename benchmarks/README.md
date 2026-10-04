# Local quota load tests (k6)

This tests the real schedule HTTP API and PostgreSQL accounting, not a bare
counter. The fake feature is cheap, pure computation: no shipping provider is
called. Multiple API workers share accounting, but every component is local.
This is not a multi-machine or production-capacity claim.

## What these files do

These are **test tools**, not application services or frontend code.

### `benchmarks/quota_load.js`: simulated client requests

This JavaScript runs inside **k6**, not in a browser. It sends real HTTP requests
to the schedule consumer, like a frontend or another API client would: an
organization ID in the URL, an `Idempotency-Key` header, and a JSON body containing
routes. It measures HTTP latency and validates responses. It does not simulate
browser rendering, user clicks, or execution of our frontend code.

```text
quota_load.js running in k6
          |
          | HTTP requests, like an external client
          v
     Consumer API (four worker processes)
          |
          | Shared quota library / SQL transactions
          v
     PostgreSQL (portcast_load)
```

The JavaScript does not access PostgreSQL directly or implement quota accounting.
The consumer still performs admission, feature execution, and settlement through
the real application path.

### `scripts/quota_load.py`: test data and accounting checks

This Python script is **not another load generator**. It connects directly to the
isolated benchmark database to support the test:

| Command | Responsibility |
| --- | --- |
| `prepare` | Creates test organizations and quota balances; writes their IDs and configuration into a fixture JSON file that k6 reads |
| `verify` | After traffic stops, checks durable operations, source allocations, charges, refunds, and balances against the k6 summary |
| `cleanup` | Deletes only that fixture's owned records; refuses if unresolved reservations remain |

The workflow is **Python prepares data → k6 sends client requests → Python checks
accounting → explicit cleanup**. Neither tool runs as part of normal application
operation. Verification is separate because HTTP responses alone cannot prove
that database accounting is correct.

### Retired detailed tracing (historical evidence)

The investigation expanded from k6 into serial profiling and detailed nested
request/ORM/SQL traces. A bounded run used 20 requests with five users; its CPU
clock-order bug was corrected before a second run. Both runs reconciled 20 paid
operations, but cold-start effects, instrumentation overhead, and sampling limits
made the results harder to interpret than a small transaction comparison.

We returned to simpler per-step timings and native driver/PostgreSQL diagnostics.
Those identified a real WAL-sync wait in one slow commit, without claiming it
explained every delay or the sustained-load failure. The subsequent transaction
comparisons are preserved under [manual experiments](../tests/experiments/README.md).

Detailed tracing middleware, ORM/engine wrappers, the serial profiler, and the
trace analyzer were removed during cleanup. k6 no longer requests trace IDs or
prints per-request trace records. Only the opt-in `Server-Timing` quota measurement
remains in the application. Old `atomic20-*` / `atomic20-v2-*` records and reports
remain local historical evidence in `benchmarks/results/`, not instructions to
rerun removed tooling or production-capacity claims.

## Isolated setup

Requires Docker/Compose and host `uv` dependencies (`uv sync --locked`). Use
`docker-compose` instead of `docker compose` if only the standalone binary is
available. These commands assume the default development PostgreSQL on 5433,
development DB credentials from `.env`/exported `DB_*`, and the default Compose
network. Substitute the actual network/DB port for another Compose project.
Do not run load tests on production or concurrently with the pytest suite.

```sh
docker compose up -d --wait postgres
# Create once. Do not recreate/drop an existing database.
docker compose exec -T postgres createdb -U portcast portcast_load

DB_NAME=portcast_load uv run python -m src.shared.init_db
DB_NAME=portcast_load uv run python -m src.shared.migrate_operation_context
DB_NAME=portcast_load uv run python -m src.shared.migrate_recovery_index
DB_NAME=portcast_load uv run python -m src.consumer.seed_demo

docker pull grafana/k6:1.0.0
docker run --rm grafana/k6:1.0.0 version
docker build -f Dockerfile.consumer -t portcast-consumer-load .
docker run -d --name portcast-load-consumer \
  --network portcast-assignment_default \
  -p 127.0.0.1:18001:8000 \
  -e DB_HOST=postgres -e DB_PORT=5432 -e DB_NAME=portcast_load \
  -e DB_USERNAME=portcast -e DB_PASSWORD=portcast_dev \
  -e QUOTA_BENCHMARK_TIMING=1 \
  --init portcast-consumer-load \
  /app/.venv/bin/uvicorn src.consumer.main:app \
  --host 0.0.0.0 --port 8000 --workers 4 --no-access-log

curl --fail http://127.0.0.1:18001/health
docker logs portcast-load-consumer
mkdir -p benchmarks/results
```

The named benchmark API container is intentionally separate from the demo API.
No recovery worker targets `portcast_load` in this baseline: that would change
observed outcomes during the measurement. Pending holds fail reconciliation and
are retained, not refunded/deleted. The original demo worker, if running, uses
`portcast`, not this database. Stop benchmark traffic before verification/cleanup.

The API uses four Uvicorn workers, each with its own SQLAlchemy pool: defaults
`pool_size=5`, `max_overflow=10`, `pool_timeout=30s`, `pool_pre_ping=True`.
Up to 60 consumer connections may be opened. Pool/lock waits count in quota
timings. FastAPI's sync handlers also have a per-process thread limiter (normally
40 tokens under AnyIO defaults); record the actual installed versions/settings.
No pool/thread/accounting optimization has been made for the benchmark.

## Run one workload per fresh fixture

Start with a low rate. Fixture provisioning is outside the measurement. The
default is 5,000 organizations, one feature, 1,000,000 included units each, no
credits. High allowances prevent a denial-heavy run being mistaken for capacity.
Data files and raw output in `benchmarks/results/` are ignored by Git.

```sh
DB_NAME=portcast_load uv run python -m scripts.quota_load prepare \
  --allow-demo-writes --manifest benchmarks/results/sanity-fixture.json

docker run --rm --user "$(id -u):$(id -g)" \
  --network portcast-assignment_default \
  -v "$PWD/benchmarks:/benchmarks" -w /benchmarks \
  -e FIXTURE=/benchmarks/results/sanity-fixture.json \
  -e SUMMARY=/benchmarks/results/sanity-summary.json \
  -e BASE_URL=http://portcast-load-consumer:8000 \
  -e WORKLOAD=distributed -e RATE=20 -e DURATION=10s \
  -e VUS=20 -e MAX_VUS=200 -e K6_VERSION=1.0.0 \
  grafana/k6:1.0.0 run quota_load.js

DB_NAME=portcast_load uv run python -m scripts.quota_load verify \
  --manifest benchmarks/results/sanity-fixture.json \
  --summary benchmarks/results/sanity-summary.json \
  --output benchmarks/results/sanity-accounting.json
```

The fixture manifest stores its run UUID, owned org IDs/names, allowance/price,
database host/port/name (no credentials), UTC period, PostgreSQL version, host
platform/Python version, Git revision/dirty state, and source SHA-256. A pre-commit
run must be described as a dirty working-tree measurement with that source hash,
not as a measurement of the unchanged base commit. The manifest may be large;
retain metadata and selected results separately for a final measured report.

**Never reuse a fixture for another measured run.** Reconciliation compares all
its durable operations against one k6 summary. Give every warmup, scenario,
rate/duration change, or repeat a new manifest path and fresh owned organizations.
Low-rate runs do not touch all 5,000 orgs; report provisioned and actually used
organization counts separately. A full cycle requires at least 5,000 iterations.

### Workloads

| `WORKLOAD` | Requests and intent |
| --- | --- |
| `distributed` | Round-robin orgs, one fresh paid request per iteration |
| `hot` | One fresh paid request per iteration, all competing for the first org |
| `batch` | Distributed orgs, seven routes per fresh request by default (`BATCH_SIZE`) |
| `retry` | Fresh one-route request, then identical saved-response replay if it succeeded; up to two HTTP requests per iteration |
| `failure` | Distributed orgs, alternating confirmed injected fail/timeout; expected 502/504 and refunds |
| `exhaustion` | Hot org, seven-route batches; 200/429 expected, complete allocation only |

For a focused mixed-credit sanity run, prepare a fresh `batch` fixture with
`--orgs 100 --monthly-units 3 --credit-units 1000000`, then run at 20 iterations/sec
for 5s. For exhaustion, prepare `--orgs 1 --monthly-units 25 --credit-units 10`,
then seven-unit batches can complete exactly five times. These are accounting
checks, not successful-throughput claims.

`constant-arrival-rate` specifies **iterations/sec**, not universally HTTP
requests/sec or route units/sec. Only the one-request workloads map that directly
to request starts. `retry` makes up to two requests; `batch` charges several units
per successful request. Report these separately. Random UUIDv4 keys prevent
accidental replay from inflating new paid throughput.

### Larger measurements (after the sanity check)

Suggested initial experiments, each with a new fixture:
- Separate low-rate warmup, then `distributed`, `RATE=2000`, `DURATION=60s`.
- Short `distributed` peak, `RATE=10000`, `DURATION=10s`.
- Lower-rate `hot`, `batch`, `retry`, `failure`, and exhaustion checks to isolate
  contention and correctness behavior.

These durations are proposed experiment choices, not durations specified by the
assignment or results already achieved. Increase `VUS`/`MAX_VUS` based on observed
response times and generator resources. At 2,000/sec with 100ms iterations,
roughly 200 simultaneous virtual users are needed. Sufficient VUs do not guarantee
the generator, API, or database has sufficient capacity. The k6 generator shares
Colima CPU/memory with PostgreSQL and the API in this setup; record resource
contention and do not infer multi-machine capacity.

If no VU is free to start an iteration, k6 records `dropped_iterations`. Those
scheduled iterations never became HTTP requests. Report configured rate, actual
request starts/completions, drops, new paid successes, replay successes, units,
denials, expected failures, and unexpected errors. Do not call configured rate
achieved throughput. Counts over the overall run can include the graceful drain;
inspect time-series output for a sustained plateau rather than relying only on
one aggregate mean. Optional k6 `--out json=/benchmarks/results/<run>-series.json`
captures time-series metrics but adds output overhead; record whether enabled.

## Timing and pass/fail semantics

`QUOTA_BENCHMARK_TIMING=1` enables request-scoped `Server-Timing` headers:
- `quota_admission`: normalized-payload serialization, replay/pricing lookup,
  reservation or expired claim, including database pool/lock waits and commits.
- `quota_finalize`: full successful result/accounting settlement call.
- `quota_release`: full confirmed-failure refund call.

Feature computation and HTTP/server scheduling/serialization outside these spans
are excluded. Recovery calls have no HTTP collector. Disabled requests read no
timing clock and emit no header. Descriptions `ok`, `error`, and `db_error` describe
call outcomes, **not proof that an ambiguous commit did/didn't occur**.

k6 reports `new_paid_http_ms` separately from `replay_http_ms` (native
`http_req_duration` mixes all request types in a workload). These HTTP durations
measure sending + waiting + receiving, excluding connection establishment;
native connection/blocked metrics and `iteration_duration` give additional context.
k6 also reports admission/finalization/release distributions and `quota_total_ms`, a
per-new-paid-request sum of admission + finalization. It does not sum separate
percentiles. For an explicit conservative interpretation of the assignment's
<10ms overhead target, the default threshold is **p95 of that total <10ms**;
the brief does not specify a percentile. Also report p50/p95/p99 and the separate
phases rather than hiding slow tails or equating total HTTP latency with quota
overhead. `QUOTA_P95_MS` can change the experiment threshold; document any override.
Instrumentation/header processing has cost; measurements are instrumented, not
a zero-overhead production profile.

Checks validate HTTP status, result shape/routes, timing phases, and exact saved
response replay. Expected injected 502/504 and exhaustion 429 are labeled
separately, not counted as paid successes. Failed checks, unexpected responses,
dropped iterations, or missed latency thresholds make k6 exit nonzero. **Still
run reconciliation and preserve output after a failed performance threshold.**

Verification reads a repeatable, read-only snapshot after traffic has stopped.
It checks both balances against source-specific `DONE` + `RESERVED` allocations,
full batch cost, durable results, releases, period, and unchanged pricing. It
compares durable counts/units to acknowledged new successes and confirmed failures.
Any unresolved holds, acknowledgement discrepancy, or unexpected responses fail
the run; they are not blindly refunded. Completed replay count is HTTP evidence;
the database has only one row per operation. Performance threshold misses/drops
are distinct from accounting consistency and reported separately.

## Cleanup and evidence

Only after verification and after stopping load:

```sh
DB_NAME=portcast_load uv run python -m scripts.quota_load cleanup \
  --allow-demo-writes --manifest benchmarks/results/sanity-fixture.json
docker stop portcast-load-consumer
docker rm portcast-load-consumer
```

Cleanup validates exact organization ownership and refuses to delete unresolved
holds. It removes only fixture-owned rows, retaining files as evidence. It does
not drop/reset databases, refill the demo allowance, or delete Docker volumes.
Neither k6 nor application code is installed globally; the pinned k6 Docker image
is used. The separate `portcast_test` database remains the target for pytest.

Record commands, date, source identity, Docker image IDs, k6 version/digest,
PostgreSQL settings/version, CPU/memory allocated to Colima, host hardware,
API process count, pools, dataset size/active orgs, warmup, duration, VUs,
latency, throughput, drops, errors, and accounting output. Final curated measured
results are a separate review step; this file does not claim targets are met.

## First admission optimization (under review)

Fresh consumer admission now commits replay lookup, a combined price/lease read,
and reservation together. The public `reserve_quota()` still owns its transaction;
the consumer uses the same accounting through an internal transaction-scoped
helper. Existing replay pricing, atomic key claims, locks, refunds, and recovery
remain intact. Normal monthly-only admission has seven SQL statements and one
commit, versus eight statements and two commits in the earlier evidence.

The isolated `tests/experiments/simple_quota_timing.py` diagnostic prints elapsed times for
admission only. `QUOTA_SIMPLE_COMMIT_TRACE=1` optionally enables public Psycopg
protocol tracing and commit-only cProfile; `QUOTA_SIMPLE_SERVER_TRACE=1` enables
duration logging only on its synthetic diagnostic DB connection. These are not
production settings. Its reserve-body timer now excludes the outer commit; use
the whole admission timer for before/after comparisons, not nested sums.

After 92 passing tests, exactly three curls on one fresh single-worker container
observed admission 59.142/23.538/23.495ms versus 63.456/27.432/29.061ms previously.
The two warm-sample mean was about 16.7% lower, but still above 10ms. This small,
logging-enabled sequential comparison is not a production percentile or a new
sustained-capacity result. Raw local notes are in
`benchmarks/results/simple-commit-investigation.md`; containers/logs were retained
and diagnostic servers stopped. No cache, queue, durability change, or ORM
replacement was introduced.

Manual transaction comparisons have been separated into
[`tests/experiments/`](../tests/experiments/README.md). They preserve the asyncpg,
prepared-statement, async-ORM, and PostgreSQL-function investigation without
changing the production implementation or running during ordinary pytest.

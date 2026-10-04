# Manual quota experiments

These probes preserve the performance investigation. **They are not application
implementations, production recommendations, or automatic pytest tests.** The
application continues to use its tested SQLAlchemy admission path.

## Standalone transaction probe

`asyncpg_quota_probe.py` compares the current admission function with alternatives
for **fresh, single-route requests paid entirely from current monthly quota**:

| Option | Comparison |
|---|---|
| Default | Current SQLAlchemy ORM/Psycopg versus direct asyncpg |
| `--prepared-comparison` | Automatic asyncpg caching versus explicit preparation |
| `--async-orm-comparison` | Direct asyncpg versus existing ORM through `AsyncSession.run_sync()` |
| `--function-comparison` | Seven client statements versus a session-local PostgreSQL function |

The options are mutually exclusive. Each invocation performs three calls per
variant, with fresh keys and reused connections. Admission timing includes commit
but **excludes connection establishment/checkout, finalization, and printing**.
Finalization runs outside the timer so successful fixture work is not abandoned.
The PostgreSQL function is created in `pg_temp`, not installed by a migration.

This is not a complete alternative for retries, credits, monthly resets, recovery,
or failure handling. It is not a load generator or HTTP endpoint benchmark. Two
warm samples per variant and fixed execution order cannot establish production
percentiles, throughput, or the assignment's total quota-overhead target.

## Run manually only

First set up the project's normal dependencies and isolated benchmark database as
described in [the benchmark instructions](../../benchmarks/README.md). Create a
fresh benchmark-owned fixture in `portcast_load`, with sufficient monthly capacity
for six paid operations. Read its organization ID from the fixture manifest.
Do not use the original demo database or an arbitrary organization.

From the repository root, replacing `ORG_ID` with that synthetic ID:

```sh
PYTHONPATH=. DB_NAME=portcast_load uv run --frozen --no-sync \
  --with asyncpg==0.31.0 python tests/experiments/asyncpg_quota_probe.py \
  --org-id ORG_ID --allow-demo-writes
```

Add `--prepared-comparison` or `--function-comparison` for those comparisons.
The async-ORM comparison also needs SQLAlchemy's optional asyncio dependency:

```sh
PYTHONPATH=. DB_NAME=portcast_load uv run --frozen --no-sync \
  --with asyncpg==0.31.0 --with 'sqlalchemy[asyncio]==2.1.3' \
  python tests/experiments/asyncpg_quota_probe.py \
  --org-id ORG_ID --allow-demo-writes --async-orm-comparison
```

Optional packages belong to the temporary experiment environment, not the
application's dependencies or lockfile. The script requires explicit write opt-in
and checks that the organization has a benchmark-owned name. Verify accounting
afterward using `scripts.quota_load verify` with the same fixture manifest.

For a Docker comparison, explicitly mount `tests/experiments` read-only and run
the file with `PYTHONPATH=/app` in the consumer image. Tests/experiments are excluded
from normal image builds. Historical container commands used the old mounted
module name; the script was subsequently relocated here without adopting its
alternative implementations into the application.

## Preserved measurements

Raw reports, manifests, and reconciliation outputs remain in local, Git-ignored
`benchmarks/results/`. The small comparisons observed:

| Comparison | Observed warm means |
|---|---|
| ORM/Psycopg vs direct asyncpg | 19.491 vs 9.421 ms |
| Automatic caching vs explicit preparation | 8.872 vs 9.656 ms |
| Direct asyncpg vs Async ORM/asyncpg bridge | 9.273 vs 20.020 ms |
| Direct asyncpg vs PostgreSQL function | 11.269 vs 5.320 ms |

These are separate runs, not one league table. Different timing scope from the
HTTP diagnostics, run-to-run variation, cold setup, and fixed order matter. The
results are evidence of possible directions, **not claims that production targets
were achieved**. No alternative was integrated into the application.

## Simple endpoint timing diagnostic

`simple_quota_timing.py` launches the existing SQLAlchemy schedule endpoint with
per-step prints. It is a manual diagnostic, not the normal API entry point.
From the repository root, after configuring `DB_*` for `portcast_load`:

```sh
PYTHONPATH=. DB_NAME=portcast_load uv run --frozen --no-sync uvicorn \
  --app-dir tests/experiments simple_quota_timing:app \
  --host 127.0.0.1 --port 18004
```

Only send synthetic benchmark-owned requests. Optional
`QUOTA_SIMPLE_COMMIT_TRACE=1` enables public Psycopg protocol tracing and commit-only
profiling; `QUOTA_SIMPLE_SERVER_TRACE=1` enables connection-level PostgreSQL duration
logging. These outputs can contain synthetic SQL/data. Prints/profile reports are
emitted after the measured admission, except native protocol output while enabled.
Stop the diagnostic server when finished; do not launch it with normal deployment.

Importing this launcher deliberately patches the consumer app/service **in that
process** and disables timing headers. Do not import it from application code,
tests, or package initializers. Its filename avoids automatic pytest collection.

The unfinished Core probe/tests, detailed in-app tracing, older serial profiler,
and trace analyzer were removed. k6, fixture/reconciliation tooling, and minimal
opt-in `Server-Timing` remain. This folder does not implicitly execute either probe.

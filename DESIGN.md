# Per-customer quota metering — design

## 1. Summary and scope

SQLAlchemy/Psycopg and PostgreSQL reserve quota before work, then finalize/refund
it. The shared library provides concurrent enforcement, batches, retries, UTC
reset, reporting and recovery for one pure schedule consumer.

The brief targets 2,000 quota operations/sec sustained, 10,000/sec peak and under
10ms request-path check/deduction overhead.

**70 retained tests passed against real PostgreSQL.**

The retained implementation prioritizes verified accounting correctness within
the take-home timebox and does not meet the latency target in the recorded
measurements. The transaction comparisons showed meaningful optimization headroom
without replacing PostgreSQL: direct asyncpg measured approximately 9.4ms for
fresh admission, while a separate PostgreSQL-function comparison measured
approximately 5.3ms. These were limited fresh-monthly transaction measurements,
not integrated request-path or throughput guarantees. I chose not to integrate
either approach late in the exercise because doing so would require revalidating
the full concurrency, idempotency, refund, reset, credit, and recovery contract.
Commands are in [README.md](README.md).

## 2. Architecture and data model

### Implemented architecture

`src/consumer` serves searches; `src/server` serves reporting. Both import
`src/shared`; separate recovery reuses its accounting and feature execution.
Processes own pools/sessions, not authoritative balances.

A library avoids another service hop; PostgreSQL provides shared transactions,
uniqueness, locks and durability. Compose starts PostgreSQL, serial setup, both
APIs and recovery. Separate images enforce package boundaries; repeatable seeding
does not refill quotas. No broker or quota HTTP service is needed.

![Author's original architecture sketch showing shared PostgreSQL, API instances and recovery](assets/Screenshot%202026-10-05%20at%204.25.02%E2%80%AFAM.png)

*Figure 1 — Early architecture sketch drawn by the author and used during design,
included unchanged. The final implementation uses the shared-library/PostgreSQL
shape described in this section. Cache and external-service paths are conceptual
and are not part of quota correctness; the demo does not deploy a load balancer.
Recovery eligibility uses ownership leases, not an execution-timeout guarantee.*

| Table | Responsibility |
|---|---|
| `org` | Organization identity |
| `api_quota_map` | Feature unit cost and ownership-lease duration |
| `feature_quota_monthly` | One mutable included-quota aggregate per organization/feature |
| `feature_quota_extra` | Optional prepaid balance and expiry per organization/feature |
| `quota_usage_per_request` | Unique request identity, source allocation, state, lease/version, normalized inputs and saved result |

Routes form a lookup batch: required units = route count × configured cost (seeded
as one). Configuration lives in PostgreSQL, not per-instance counters.

![Author's original quota schema sketch with per-organization feature balances and reservation states](assets/Screenshot%202026-10-05%20at%204.24.56%E2%80%AFAM.png)

*Figure 2 — Early logical schema sketch drawn by the author, included unchanged.
The implemented tables retain this accounting shape and add request/result JSONB
for retry identity and saved outcomes. Contract-linked credit retention is a
designed extension, not an implemented eligibility/lifecycle policy. Success is
charged at durable result finalization, not verified request/response delivery.*

### Designed extensions, deferred

Included-first prepaid allocation and expiry checks are implemented. The broader
[discussion](discussion/ARCHITECTURE_DISCUSSION.md) also considered contracts,
seven-day metered grace, uninterrupted renewal, contract-end + 90-day credit
retention, deduplicated grants, notices, and org-scoped fresh paid-result reuse
under new keys. These are **deferred, not PDF requirements**; some transition
details remain open. Same-key replay is implemented, cross-key free reuse is not.
No cache owns balances; write-behind needs durability design, and views cannot
replace atomic admission.

## 3. Concurrent correctness and batch policy

Fresh admission uses **one transaction**. Replay lookup is only a fast path:
`INSERT … ON CONFLICT DO NOTHING` against unique `(org_id, feature, idempotency_key)`
coordinates racing claims under READ COMMITTED isolation.

Lock order is **operation → monthly → credits**. The monthly lock protects reset
and allocation; conditional updates require sufficient units and unexpired credits.
Insufficient capacity rolls back everything. All writers must respect this protocol.

Included units come first. Batches are **all-or-nothing**: partial work would need
selection/order semantics and complicate retries.

For each balance, database constraints preserve nonnegative counters and
`allocated = consumed + remaining`. Here, **consumed includes holds and completed
work**. Stored operation allocations explain where each reservation came from.
Feature execution runs outside the transaction, keeping accounting locks short.

Contention tests hold a database lock until four independent processes are observed
waiting. Tests also cover racing admission, duplicate keys, rollback and refunds.
The invariant and observed contention support the claim; this is not exhaustive
proof of production behavior or an accidentally sequential test.

## 4. Failures, retries, and recovery

| State/event | Behavior |
|---|---|
| `RESERVED` | Capacity held; feature work unfinished or uncertain |
| Confirmed success → `DONE` | Save result and terminal state together; no second deduction |
| Confirmed failure → `RELEASED` | Refund original sources once when release commits |
| Same key, changed inputs | Conflict; no work/charge |
| Recognized `DONE` retry | Replay saved result/charge despite repricing |
| Recognized `RELEASED` retry | Conflict; new attempt needs a new key |
| Active-lease retry | Conflict; no takeover |

Storage failure closes admission. A timeout, crash or lost commit acknowledgement
does **not** prove failure; failed release can leave a hold. The charge boundary is
a durable result with `DONE`, not client/LB receipt. Retry identity resolves lost responses.

Expiry permits takeover, not refund. Retry/worker claims use `FOR UPDATE SKIP LOCKED`,
increment `claim_version` and preserve `reserved_at`; locked settlement rejects stale
versions. A partial index serves recovery. Defaults: lease 30s, polling 5s, batch 20.
Expired executors may overlap takeover; fencing protects settlement, not external execution.

Recovery is safe because the demo is pure/repeatable. Unknown outcomes remain held.
No heartbeat, real deadline, retry cap or external-write exactly-once guarantee is
provided. Injected timeout means stopped demo failure, not remote cancellation.

## 5. Monthly reset and reporting

Periods are UTC calendar months, start-inclusive/end-exclusive. Database time is
read after the monthly lock. Admission, release and reporting refresh in place;
skipped months do not accumulate allowances. Initial allowance is full, without
proration or rollover; contract activation is not implemented.

Original admission time/allocation survives reset. Old-period refunds cannot refill
the new month; late success changes only state. Credit refunds never extend expiry.

`GET /orgs/{org_id}/features/{feature}/usage` locks aggregates and derives holds,
avoiding a second mutable reserved counter. It returns UTC boundaries, monthly
limit/completed/reserved/available units, and credit reserved/available/expiry.
Included holds use the current month; old credit holds remain accounted for.

## 6. Measurements and the investigation

### Historical HTTP load tests

k6 → four Uvicorn processes → PostgreSQL 17.11 ran locally in Docker/Colima on
macOS: one feature, 5,000 provisioned orgs. Pools: 5 connections + 10 overflow per
worker, 30s timeout, pre-ping. Fixture reconciliation checked accounting afterward;
no recovery worker masked holds.

Both runs scheduled **2,000 new paid requests/sec for 60 seconds**:

| VUs | Paid completions | Achieved/sec¹ | Dropped iterations | HTTP p95 | Quota p95² |
|---:|---:|---:|---:|---:|---:|
| 500 | 3,493 | 52.92 | 116,507 | 14,346 ms | 5,407 ms |
| 50 | 2,773 | 44.14 | 117,256 | 2,159 ms | 1,683 ms |

¹ Rates include drain: 66.00 and 62.82 seconds total, not a sustained plateau.
² Per-request admission + finalization, then percentile aggregation. This p95
interpretation was our benchmark choice; the PDF does not specify a percentile.

Accounting reconciled; no unexpected responses or unresolved holds. Drops were
**never sent**, not denials/successes. Both runs failed performance thresholds and
reached only 3,493/2,773 orgs. Thirty features, 50,000 orgs, eight replicas and
10,000/sec peak were not demonstrated.

These **dirty-build** results used base `9f4b160`, not cleaned checkpoint `86dcbd4`.
Local hashes/versions lack full hardware allocation; exact reconstruction is incomplete.
Raw artifacts remain local/ignored.
[Benchmark commands](benchmarks/README.md) reproduce the workload, not these exact numbers.

### Smaller comparisons, not application capacity

Profiling/tracing expanded beyond useful scope; cold/instrumentation effects made
interpretation harder. Smaller measurements identified `WalSync` in one slow commit,
but did not establish the whole high-load root cause.

Consolidation observed ~23.5ms warm versus 27–29ms with logging. Monthly admission
now has seven statements and one commit.

Separate standalone runs compared two warm samples per variant:

| Comparison | Warm means |
|---|---|
| ORM/Psycopg vs direct asyncpg | 19.49 vs 9.42 ms |
| Automatic caching vs explicit preparation | 8.87 vs 9.66 ms |
| Direct asyncpg vs Async ORM/asyncpg bridge | 9.27 vs 20.02 ms |
| Seven asyncpg client statements vs PostgreSQL function | 11.27 vs 5.32 ms |

Commit is included; checkout, finalization and setup are excluded. Fixed order and
tiny samples are not cross-run rankings or full-operation p95. Alternatives cover
fresh monthly-funded work, not the complete credit/retry/recovery contract.

The comparisons showed that putting asyncpg underneath the existing ORM alone
did not reproduce the direct-path gain; reducing client-side processing and
round trips was the more promising direction. We deliberately retained the
tested implementation within the timebox rather than integrate a faster happy
path without preserving the full accounting contract. Adopting an alternative
would require those cases to pass and the actual request path to be measured.
[Manual probes](tests/experiments/README.md) preserve that evidence; detailed
tracing/older profilers were removed, minimal opt-in quota timing remains.

**Verification choice:** After consolidating admission, I checked correctness
with the retained test suite and used bounded curl/transaction comparisons to
measure the change and investigate latency reductions. Within the remaining
timebox, I prioritized verified accounting and targeted performance evidence
over repeating a sustained/peak campaign or integrating incomplete alternatives.
The earlier k6 runs therefore do not measure the consolidated, cleaned version;
its sustained/peak capacity remains unmeasured. A future integrated optimization
would need both contract revalidation and a new full-path load test.

## 7. Scaling and alternatives

50,000 organizations × roughly 30 features means about 1.5 million active quota
buckets. That alone is not a reason to replace an indexed PostgreSQL design. I
would focus on operation rate, contention within individual buckets, connection
pressure, and growth of request history. The following is my evolution plan,
not functionality already implemented or capacity already demonstrated.

- **Optimize the existing path first.** Reduce ORM processing and database round
  trips through explicit SQL/asyncpg, potentially collapsing admission into a
  PostgreSQL function. Our experiments support this direction without replacing
  the shared-state correctness model; the complete accounting contract still
  needs revalidation before integration.
- **Bound connections as replicas grow.** PgBouncer can let many application
  connections share a bounded server pool. It addresses connection pressure, not
  WAL, CPU, I/O or lock contention. Pooling mode and prepared-statement compatibility
  need checking when using asyncpg.
- **Offload only appropriate reads.** Replicas can serve historical, admin/dashboard
  and other stale-tolerant reads, leaving primary capacity for writes. Admission
  must use primary state. Our current usage endpoint also performs locked lazy
  reset, so it cannot move unchanged to a read-only replica.
- **Cache data with explicit freshness rules.** Configuration, metadata and
  stale-tolerant reports are candidates; pricing/limit changes need invalidation
  or versioning. Remaining quota stays authoritative in PostgreSQL. Redis-authoritative
  consumption would introduce different durability/failover semantics. Replicate
  or cluster a cache only when its CPU, memory, network, connections or availability
  requirements justify it—not because the organization count reaches 50,000.
- **Keep request history bounded.** Aggregates grow with organizations × features;
  operations/idempotency grow with requests × time. Define terminal-record retention
  and the retry window, archive old history, and consider time partitioning as needed.
  Never silently erase retry protection or purge unresolved reservations.
- **Separate total throughput from hot-key limits.** Independent buckets can run
  concurrently; thousands of requests for one organization/feature/period still
  contend on one logical value. More API instances, pooling, replicas and ordinary
  sharding do not remove that serialization. If an optimized primary eventually
  becomes the aggregate-write bottleneck, `org_id` is a natural sharding boundary;
  spreading organizations does not split a single hot bucket.

Measurements should choose the next step: quota p95/p99, database CPU/I/O, pool
waits, active connections, row-lock waits, WAL/write pressure, table/index size
and per-key contention. Those distinguish SQL overhead, connection limits, read
load, history growth and write contention instead of assuming one scaling remedy.

## 8. Limits and authorship

Org IDs are not authentication. No carrier integration, delivery guarantee, purge
or HA deployment is supplied. Earlier Compose startup, restart and runtime smoke
checks passed, but were not repeated after instrumentation cleanup. That final
container smoke check is release verification, separate from a new load-test campaign.

The author drew the original architecture/schema sketches and chose the product
policies and scope. Implementation, tests, experiments and this draft received
substantial AI assistance; decisions were reviewed collaboratively. Attribution
is summarized in [COLLABORATION.md](llm_conversation/COLLABORATION.md).

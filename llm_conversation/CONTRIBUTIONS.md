# Human and AI Contributions — Working Record

This document summarizes contributions from the conversation so far. It is an
attribution record, **not a verbatim transcript** or a claim that every detail was
independently designed by one party. Update it as implementation progresses.

- **Author:** Vaibhav Bisht, the human developer directing this assignment.
- **Assistant:** an AI coding/research assistant used through OpenCode.
- **Source:** the ongoing conversation, the author's schema/architecture images,
  and code/documentation changes made during that session.
- **Checkpoint:** `feat/demo-consumer`, after implementing the demo consumer and
  running 32 tests, then an author-approved replay/pricing fix with 33 passing
  tests historically, followed by the author-requested server/consumer split.
  The split and two new boundary tests passed the full suite (**35 tests**).
  The author approved the consumer and split for commit and local merge, then
  requested a new recovery branch. No push was requested.
  Subsequently the author explicitly requested pushing `main` and starting
  recovery; `main` was pushed. The author then reviewed and approved recovery,
  explicitly authorizing its commit/local merge and the next runtime slice.
  Attribution documentation was explicitly requested by the author.
  Recovery commit `1fbdb16` was merged into local `main`, and the approved next
  branch is `chore/demo-runtime`. The author reviewed and approved the one-command
  runtime slice, including the optional integration smoke script. No push was
  authorized for this transition.

## 1. Collaboration agreement

The author owns the project and final design decisions. The author explicitly
requested time to explain ideas before receiving suggestions, incremental code
review, simple architecture, and focused correctness tests rather than exhaustive
scenario coverage. Full implementations were produced by the assistant when
requested or approved; they were not all written by the author.

An early reservation function was intentionally scaffolded for the author to
complete. The author clarified that implementation was wanted, and the assistant
then wrote it. This distinction matters: approving or reviewing generated code
is not the same as independently writing that code.

## 2. Author's contributions

### Problem understanding and priorities

- Identified quota enforcement, correctness, and request-path latency as the core
  concerns, then explored building the quota part independently with a fake consumer.
- Proposed PostgreSQL and Docker for shared persistence and development setup.
- Repeatedly emphasized simplicity, implementation progress, and the cost of
  adding complexity across the whole application.

### Product and policy decisions

- Proposed expandable capacity rather than a fixed allowance with no extension.
- Explored subscription + prepaid and subscription + pay-as-you-go models, clarified
  that the initial window idea was a true rolling window, then chose prepaid packs.
- Chose all-or-nothing batches because partial fulfillment would require arbitrary
  selection semantics for which items to process.
- Accepted fixed monthly periods, then simplified the anchor to the first of each
  month at midnight UTC instead of contract-anniversary arithmetic.
- Chose a full initial allowance without proration to keep scope small, while
  requesting documentation of proration as a preferred future improvement.
- Raised the customer-fairness concerns around credit expiration near renewal and
  short contract lapses; accepted full carry-forward on uninterrupted renewal and
  a 90-day retention window after contract end.
- Proposed a short service grace period, selected seven days, and specified normal
  metered usage plus a consumer UI warning during that grace.
- Defined the responsibility principle: failures inside our serving system should
  not charge for unsuccessful work; failures beyond our boundary are not controlled
  by the service. Highlighted differences between feature reads and feature writes.
- Challenged the assumption that idempotency keys alone solve refresh/retry behavior,
  and requested research into real implementations rather than relying on assertions.
- Proposed combining feature-specific deduplication, idempotency, and caching,
  with freshness informed by upstream data-update behavior.
- Chose freshness-based free result reuse rather than a free-hit counter, rejecting
  the counter's extra concurrency/accounting complexity as poor value.

### Recovery and accounting reasoning

- Connected recovery to the transactional-outbox/durable-log idea: persist enough
  state so a restarted or different instance can find unfinished work. Kafka was
  an analogy, not a request to add Kafka.
- Proposed retry-driven recovery from persisted operation state and recognized
  that polling has the same uncertainty about slow versus failed owners.
- Considered CDC, then explicitly chose polling for simplicity.
- Proposed feature-dependent timing informed by SLO/SLA expectations, and bounded
  request timeouts rather than indefinite synchronous execution.
- Proposed the minimal lifecycle; accepted the branching names `RESERVED`, `DONE`,
  and `RELEASED` after discussion, without a separate processing-state write.
- Pointed out that units are already removed from availability at reservation,
  so successful completion must not deduct them again.
- Chose mutable current-period aggregate rows instead of mandatory historical
  aggregate tables, and challenged treating historical reporting as a requirement.
- Explicitly defined `units_consumed` as reserved + finalized units, with failure
  handled by a compensating refund.

### Schema and workflow ownership

- Created and shared the architecture and logical schema diagrams. The initial
  five-table shape came from the author: organization, feature configuration,
  per-request accounting, extra credits, and monthly allowance.
- Iteratively refined those diagrams after feedback: allocation split,
  `idempotency_key`, scoped uniqueness, immutable `reserved_at`, `claim_version`,
  and separate `lease_expires_at`.
- Clarified the distinction between logical architecture/schema and physical
  implementation details, rather than requiring timestamp-type decisions in diagrams.
- Requested `src/` layout, split `.env` values, central URL construction, and host
  port 5433 after discovering a conflict on 5432.
- Requested branch checks, atomic commits, review checkpoints, and feature branches
  based on merged `main`; corrected the assistant's dependent-branch workflow.
- Reviewed and approved successive infrastructure, accounting, reset, reporting,
  and consumer slices, including the separate application layout.
- Reported committing/pushing earlier setup work and performed local runs/review.
  Later commits, merges, and pushes were also explicitly delegated to the assistant.
- Approved a focused replay correction after assistant review identified that
  completed retries were incorrectly compared against the current feature price.
- Directed the separation into a reporting server and schedule-consumer app,
  with independent images, no cross-app imports, and a direct shared quota library
  backed by the same PostgreSQL database. This supersedes the earlier combined
  API/consumer deployment topology; it does not request an HTTP quota service.
- Asked for the application/deployment boundary to be explicit, then approved
  committing and locally merging recovery, creating the next branch, and fully
  implementing the one-command demo runtime.

## 3. Assistant's contributions

### Explanation, critique, and research

- Read the assignment and explained feature units versus HTTP-request counts,
  atomic admission, transaction boundaries, reservations, and compensating refunds.
- Compared module versus service integration, PostgreSQL conditional updates versus
  row locks, and the performance implications of hot rows and long transactions.
- Suggested distinguishing included allowance from purchased credits, consuming
  included units first, and separating payment/contract duration from usage periods.
- Suggested renewal carry-forward, the 90-day retention option, and renewal reminders
  starting approximately 60 days before expiry. The reminder schedule remains a
  proposal, not implemented or an industry-standard claim.
- Researched public Portcast terms and examples from OpenAI, Stripe, ElevenLabs,
  Algolia, Mapbox, Microsoft, and Atlassian. Researched open-source Lago/LiteLLM
  behavior using a delegated research agent and summarized bounded findings.
- Explained the limits of request IDs, idempotency, caching, TCP acknowledgements,
  LB failover, response transmission evidence, and read replicas.
- Suggested a separate periodic recovery worker, ownership leases, atomic claims,
  and claim-version fencing. Explained why lease expiry does not prove failure.
- Reviewed the author's schema and proposed retaining operation-specific allocation
  facts, scoped identity checks, separate ownership deadline, and immutable period
  attribution without requiring an indefinite history table.

### Code written by the assistant

The following application/infrastructure implementation was AI-written during the
session, with author direction and review. This is not a claim that all code already
has final approval or that all planned behavior is implemented.

| Area | Assistant implementation |
| --- | --- |
| Development database | `compose.yaml`, PostgreSQL health check, persistent volume, localhost port configuration |
| Configuration/connection | `src/shared/config.py`, `src/shared/database.py`, `.env.example`, FastAPI lifecycle and health routes |
| Tables | `src/shared/models.py`, translating/refining the author's diagram into SQLAlchemy columns and constraints |
| Schema setup | `src/shared/init_db.py`, explicit creation rather than automatic app-startup table changes |
| Quota accounting | `src/shared/quota.py`: reservation, key claiming/replay, mixed allocation, finalization, release, state/version checks |
| Reset | Locked lazy calendar-month refresh, database-clock admission timestamp, `dateutil` arithmetic |
| Reporting | `get_quota_usage`, `src/shared/schemas.py` reporting models, org/feature usage endpoint in `src/server/main.py` |
| Metered consumer | `src/consumer/main.py`, `service.py`, `schemas.py`: schedule API, normalized input matching, durable result finalization |
| Schema extension | `src/shared/migrate_operation_context.py`, adding nullable request/result JSONB fields |
| Demo setup | `src/consumer/seed_demo.py`, repeatable serial provisioning without allowance refill |
| Author-directed app split | Assistant reorganized shared/server/consumer code with no cross-app imports and separate health routes |
| Separate app images | `Dockerfile.server` and `Dockerfile.consumer`, locked `uv` dependencies and only shared + respective app source |
| Recovery slice | Shared expired-lease claims, `src/consumer/recovery_worker.py`, common persisted-input execution for retry/polling, and explicit partial-index migration |
| One-command runtime | Compose configuration for PostgreSQL, serial one-shot setup, independently health-checked APIs, and separate recovery service; `scripts/runtime_smoke.py` and runtime documentation |

The assistant selected implementation details such as synchronous SQLAlchemy
sessions, psycopg, SQLAlchemy URL construction, database constraints, explicit
transaction ownership, and Postgres `ON CONFLICT` key claiming. These details were
surfaced in the discussion; they should not be described as wholly independent
human implementation decisions.

### Tests and verification

- Added pytest through `uv` and wrote all current test files/fixtures in this session.
- Implemented rollback/savepoint fixtures for basic real-Postgres tests and committed
  fixtures with explicit cleanup for independent-process contention.
- Implemented the observed-lock test gate and four spawned worker processes so
  contention is verified rather than assumed from simultaneous task launch.
- Tested exhaustion, duplicate deductions, double refunds, mixed credit batches,
  stale claims, period boundaries, reporting, and pending consumer behavior.
- Ran tests and repeated selected concurrency tests; last full suite at this
  checkpoint: **32 passed against real PostgreSQL**, with one test-client deprecation warning.
- Verified schema initialization/additive migration reruns, connectivity, and
  test-row cleanup. Provisioned the intentionally retained local demo organization.
- On the author's approval, added an existing-operation/input lookup before
  pricing new consumer work and a regression test: replay retains the original
  charge after repricing, changed inputs still conflict, and new keys use the
  current price. The updated full suite passed **33 tests** against real PostgreSQL.
- After the app split, the full suite passed **35 tests**, including two new
  route-boundary tests. Built both Docker images and started each app with Uvicorn
  inside its container against shared PostgreSQL; real HTTP health/OpenAPI checks
  passed. Verified each image lacks the other application's Python package.
- Added recovery tests covering abandoned operations, bounded batches, active
  leases, confirmed refunds, unknown/malformed context, preserved allocation,
  stale owners, and independently racing claims. Corrected a test that incorrectly
  compared the host clock with the authoritative PostgreSQL clock.
- Added a simulated lost acknowledgement after successful finalization and
  checked saved-result replay. Verified the worker entry point locally/in Docker
  and clean SIGTERM shutdown after its first poll. The full suite including
  recovery and the acknowledgement test passed **43 tests** against real PostgreSQL;
  the independent-process claim test passed again in a separate run.
- Verified standalone `docker-compose` v2.32.2 availability, fresh full-runtime
  startup, and `scripts/runtime_smoke.py` on isolated project
  `portcast-runtime-check`, using host ports 55433/18000/18001 and matching host
  database configuration/API URLs. The script checks actual HTTP schedule work,
  replay, conflict, reporting, and polling recovery of an expired committed hold,
  with unique-organization fixture cleanup. It simulates abandonment without an
  actual process crash. Verified full down/up with the volume retained and no
  `.env` file: setup preserved a 499/500 balance and the saved response replayed
  without another charge. The full **43-test** suite passed on `portcast_test`,
  separate from the demo worker's `portcast` database. Removed only the isolated
  verification stack/volume afterward, preserving the original development database.

Test runtimes are **not load-test results**. No end-to-end throughput/latency
benchmark has been completed yet. Passing tests do not establish every production
failure guarantee.

### Documentation and Git operations

- Drafted and updated `discussion/ARCHITECTURE_DISCUSSION.md`, README instructions,
  the temporary `TODO.md`, and this attribution document.
- Created branches, committed/merged approved slices, and pushed `main` when explicitly
  requested. Some earlier commits/pushes were performed by the author instead.
- The assistant initially created a dependent reset branch before merging credits.
  The author corrected this; work was stashed, credits merged into `main`, and the
  reset branch restored against that integrated baseline.
- `TODO.md` was committed despite the author's later preference to keep it temporary.
  It remains a temporary working checklist; retain it until the author requests
  final cleanup.
- Updated these four documents for the author-directed split, local app commands,
  separate images, and runtime database networking. No Git operations were
  performed for this documentation update.
- The author noted the layout refactor should have been on a separate branch,
  accepted keeping it on `feat/demo-consumer`, and explicitly requested committing
  the approved work, merging to local `main`, and creating the next feature branch.
- Created `fed2fc7`, fast-forwarded local `main`, and created `feat/quota-recovery`.
  On the author's subsequent explicit request, pushed `main` to `origin`, including
  reporting commit `6c8f299` and consumer/split commit `fed2fc7`. The author later
  approved committing/merging recovery and creating `chore/demo-runtime`; no push
  was authorized for that transition. The assistant performed the delegated Git
  operations: created recovery commit `1fbdb16`, merged it into local `main`, and
  created `chore/demo-runtime`. The author subsequently reviewed and approved the
  runtime implementation/docs for the usual commit/local-merge workflow.
  Temporary `TODO.md` edits remain uncommitted.

## 4. Jointly refined decisions

These are collaborative outcomes, not exclusively human- or AI-originated:

- Monthly included allowance plus separate prepaid credits, with renewal/retention rules.
- Fixed UTC calendar months instead of the originally explored rolling window.
- Reservation before execution, no second success deduction, source-specific failure refunds.
- A single reusable quota library, now imported directly by the separate reporting
  server and schedule consumer, plus the implemented separate recovery worker.
- Original-period settlement using per-operation identity and one mutable monthly aggregate.
- Feature-specific deduplication/freshness, free paid-result reuse while fresh, and
  explicit operation identity for retries/recovery.
- Feature-specific execution deadlines and leases, without equating silence with failure.
- Minimal real-Postgres tests that focus on accounting invariants and actual contention.

## 5. Limits, pending review, and work not done

- The desired service/LB delivery boundary was proposed by the author. The
  demo consumer currently charges when a valid result is durably recorded with
  `DONE`; it does **not** prove LB transmission. This narrower implementation must
  remain documented, not presented as the original delivery policy solved.
- Active unfinished retries are rejected; an expired original executor may still
  overlap recovery computation. This is not exactly-once external execution.
- A saved result and accounting state support recognized replay, but do not solve
  refresh with a new key, indefinite result retention, or external uncertain outcomes.
- The one-command Compose runtime is implemented and approved. It uses fixed
  local-demo credentials without requiring `.env` or host Python, preserves the
  PostgreSQL named volume, and runs serial development setup without refilling an
  existing allowance. The optional smoke script requires host `uv` dependencies
  and DB environment/`.env` targeting the same database as both API URLs.
  Worker process readiness in `--wait` is not a dedicated recovery health check;
  actual recovery was verified by the smoke script. Full restart/persistence and
  dedicated test-database isolation were verified too; none are load benchmarks.
- Contract/grace enforcement, fresh-result reuse across keys, benchmarks, and
  final `DESIGN.md` remain unfinished. Recovery is limited
  to the pure demo schedule lookup: no external-write exactly-once execution,
  heartbeat, actual execution timeout, or permanent retry cap is implemented.
- No real payment system, notification delivery, UI, or carrier integration was built.
- The public company's commercial terms are context only; the exercise explicitly
  says it is not related to Portcast's actual product.

## 6. Preserving evidence and preparing submission disclosure

- Export actual OpenCode sessions with `/export` if a verbatim record is desired.
  Save them separately here; do not mistake this summary for original messages.
- Review exports for credentials, private data, and irrelevant tool output before
  committing or sharing them. Raw exports are optional, not a submission requirement.
- Continue updating this record for implementation/review changes and remaining work.
- For submission, produce a concise AI-assistance disclosure based on this record:
  identify AI-written code/tests/docs, the author's policy/schema contributions,
  joint decisions, and verification/limitations honestly.

## 7. Benchmark investigation, experiments, and cleanup

- Recovery `1fbdb16` and runtime `9f4b160` were merged and subsequently pushed to
  `origin/main` on the author's explicit request. The benchmark work began on
  `test/quota-load`; the chronological checkpoints below precede the later
  author-requested checkpoint commit/push.
- The author requested an Internet-backed comparison of load tools and approved
  k6. The assistant wrote k6 workloads, isolated fixture/reconciliation tooling,
  opt-in timing headers, tests, and documentation. Test fixtures use `portcast_load`
  and the suite uses `portcast_test`; the original demo database was preserved.
- Preliminary serial profiling and compilation-cache checks did not establish a
  complete root cause. The local 2,000/sec, 60s experiment missed the targets:
  3,493 completed requests, approximately 52.9/sec including drain, and 116,507
  scheduled iterations not started. A 50-VU comparison also missed the targets.
  Accounting reconciled, but these are not successful performance demonstrations.
- The author explicitly challenged the broad investigation and directed atomic
  measurements of one actual endpoint with few users. The assistant implemented
  `src/consumer/tracing.py` and `scripts/trace_quota.py`: per-request nested wall/
  valid sync-thread CPU spans, client/server IDs, and read-only PostgreSQL waits.
  This did not change the quota SQL, transaction policy, or accounting algorithm.
- The bounded diagnostic was exactly 20 requests with five concurrent users.
  Review caught a CPU end-clock ordering bug in the instrumentation; the assistant
  corrected it, added a regression test, and repeated the same bounded workload
  with fresh fixtures. Use `atomic20-v2-*` as the corrected evidence, not the first
  CPU breakdown. Both runs settled and charged exactly 20 units each.
- Corrected traces reconstruct every request's wall time using exclusive intervals,
  without double-counting children. They locate ORM/SQL-client work, request
  dispatch/response time, connection/ping, and commits; snapshots can miss short
  DB waits and client SQL wall time is not server execution time. Instrumentation
  and cold-start effects prevent treating these as uninstrumented production
  latency or proof about the earlier high-concurrency failure.
- Latest full suite: **86 passed** against PostgreSQL, with the existing TestClient
  deprecation warning. No performance optimization has been applied. Contract/grace
  enforcement, fresh-result reuse, final design/results curation, and cleanup
  remain pending. Temporary `TODO.md` and raw local result files are not staged.
- After the author requested a simpler diagnostic, the assistant added isolated
  `scripts/simple_quota_timing.py` and sent one curl request, then (with separate
  approval) one more request using public Psycopg protocol tracing and commit-only
  standard-library profiling. No library files or quota implementation were edited.
  The first write commit took 43.093 ms; the profiled repeat took 29.528 ms, with
  29.418 ms between COMMIT and final-response protocol entries. This locates the
  interval but does not diagnose server WAL, network, or scheduling as its cause.
  Diagnostic containers were stopped and their logs retained. The later standalone
  diagnostic was smoke-checked by its HTTP requests, not included in the earlier
  86-test result. External research and measurement caveats are preserved locally
  in `benchmarks/results/simple-commit-investigation.md`; no tuning was performed.
- On separate approval to go deeper, the assistant enabled session-only server
  duration logging and sampled the named diagnostic backend during one additional
  request (operation 7193, PID 38514). The client reservation commit was 39.945 ms,
  PostgreSQL reported 38.533 ms, and six active-COMMIT samples reported IO/WalSync.
  This establishes a server WAL durability wait for that slow commit, not the
  cause of every API delay or the earlier throughput failure. Host versus VM
  clock-offset and sampling caveats are recorded in the local evidence. No global
  PostgreSQL settings, accounting policy, durability, or library files were changed.
  The diagnostic server was stopped after the request; no further tests were run.
- The author then approved PostgreSQL's official `pg_test_fsync` storage diagnostic
  (one second per test, unique scratch file on the WAL filesystem). Current
  fdatasync averaged 0.543ms for one 8kB write and 0.605ms for two, fastest among
  tested methods in those cases. The utility completed and scratch-file removal
  was verified. This does not explain earlier slow-tail commits or establish API
  throughput; no configuration or durability change was made.
- The author approved exactly three sequential fresh-key requests on one newly
  started diagnostic server. With commit profiling off, first admission took
  63.456ms and the next two 27.432/29.061ms. Server reservation commits were
  3.484/1.418/1.518ms; the earlier 30–40ms stall did not recur, including on the
  first request. Warm admission still exceeded 10ms, with cumulative SQL/client
  work larger than commit. The assistant recorded this limited comparison and
  stopped the server; no optimization or broader test followed.
- The author then explicitly approved consolidating preliminary checks and fresh
  reservation into one transaction and reading price/lease once, without changing
  architecture. The assistant extracted a private transaction-scoped reservation
  helper while retaining the public transaction-owning API, updated the consumer,
  diagnostic labels, and existing tests, and added six PostgreSQL regressions for
  one commit/config read, rollback, and independent-session admission races.
  **92 tests passed**. Three approved curl comparisons observed warm admission
  23.538/23.495ms versus 27.432/29.061ms previously (~16.7% lower two-sample mean),
  with logging on; not a production percentile or proof of meeting 10ms. Fixture
  accounting reconciled nine total DONE units and no holds. The server was stopped;
  no further optimization, sustained test, commit, or push was performed.
- A Core endpoint probe was subsequently started, then paused during scope
  clarification. `scripts/core_quota_probe.py`, its tests, and an optional diagnostic
  selection flag remain unverified; no Core curl comparison or tests were run.
- After explicit clarification to think small and compare transactions in one
  standalone file, the assistant added `scripts/asyncpg_quota_probe.py`: three
  current ORM calls and three direct asyncpg calls for only fresh monthly-funded
  work, on reused connections, with commit included and settlement outside timing.
  asyncpg 0.31.0 was installed only in the disposable diagnostic environment;
  application/dependency files were unchanged. Warm two-call means were 19.491ms
  ORM versus 9.421ms asyncpg (~52% observed reduction), not a driver-only attribution
  or production SLO claim. Six DONE units/no holds reconciled. Scope, fixed-order,
  logging/checkout exclusions, and raw results are recorded in local
  `benchmarks/results/asyncpg-probe-report.md`. The probe exited; no endpoint switch,
  further experiment, commit, or push followed.
- On separate approval, the assistant made the standalone probe compare automatic
  asyncpg caching against explicit handles for the identical seven statements.
  Three calls each observed warm means 8.872ms automatic versus 9.656ms explicit;
  preparation cost 5.968ms outside the timer. This tiny fixed-order comparison
  showed no benefit and does not establish a regression or production percentile.
  All six fixture operations settled/reconciled. Application and schema unchanged;
  probe exited. Local evidence: `benchmarks/results/asyncpg-prepared-report.md`.
- The author separately approved measuring SQLAlchemy Async ORM with asyncpg using
  `AsyncSession.run_sync()` to reuse existing admission unchanged. The assistant
  added only a standalone-probe branch and temporarily installed SQLAlchemy's
  asyncio extra in its test container. Three calls each: direct asyncpg warm mean
  9.273ms; ORM/asyncpg bridge 20.020ms. All six operations reconciled as DONE with
  no holds, and the probe exited. This does not assign all overhead to ORM objects
  or establish native-async/production performance. No endpoint/schema/dependency
  files changed. Local evidence: `benchmarks/results/async-orm-report.md`.
- The author approved a PostgreSQL-function comparison. The assistant added a
  session-local pg_temp PL/pgSQL function for the same limited fresh monthly path,
  with no production migration. In one three-call comparison, direct asyncpg warm
  mean was 11.269ms versus 5.320ms via one function call; creation was 4.034ms outside
  timing. Fixed order, warmed connection and limited scope prevent production/cold
  latency claims. Six DONE units reconciled, no holds; a catalog check after close
  confirmed the temporary function was gone. The optional async-ORM imports were
  made lazy after a pre-transaction launch failure. No application integration or
  further test was performed. Local evidence: `benchmarks/results/postgres-function-report.md`.
- On explicit cleanup approval, the standalone transaction probe was moved from
  `scripts/asyncpg_quota_probe.py` to `tests/experiments/asyncpg_quota_probe.py`.
  The unfinished Core probe/test and its diagnostic selection flag were removed.
  Manual experiment instructions preserve invocation, scope and measured results;
  historical raw records were not rewritten. k6, fixture tooling, older profiling/
  tracing, database fixtures and retained results were left unchanged for separate
  review. No alternative admission path was adopted into the application.
  Verification after cleanup: **92 tests passed** against `portcast_test`, with
  only the existing TestClient deprecation warning; `git diff --check` passed.
  No probe transactions, HTTP calls, load tests, commits, or pushes were run.
- On separate approval, the assistant removed detailed consumer tracing middleware,
  lifecycle/ORM/SQL hooks and markers, retired the serial profiler and trace analyzer,
  and removed their 22 feature/tool-specific tests. Business accounting and its
  correctness tests were retained. Minimal opt-in Server-Timing now calls quota
  functions directly without a tracing dependency; k6's retired trace-ID feature
  was removed while quota timing/checks remain. The simple timing launcher moved
  to `tests/experiments/`; docs preserve findings but no longer instruct running
  deleted tools. **70 retained tests passed** with the existing TestClient warning;
  `git diff --check` and a network-disabled `k6 inspect` check passed. No performance
  experiment, application integration, durability change, commit, or push followed.
- After reviewing the original PDF, the author chose submission-focused scope:
  retain the tested SQLAlchemy implementation, document unmet targets and possible
  improvements, and defer optional contract/fresh-result-reuse extensions. The
  temporary TODO was corrected accordingly. The author then explicitly requested
  a checkpoint commit and push of the retained application, benchmark tooling,
  isolated experiments, tests and documentation before deciding on further work.
  Raw local results, temporary planning, secrets and browser artifacts are excluded.
  DESIGN.md and final runtime/load verification remain outstanding; this checkpoint
  is not a completed submission or a claim of meeting the performance objectives.

# Implementation TODO

Working checklist for completing the assignment. Policies live in
[`discussion/ARCHITECTURE_DISCUSSION.md`](discussion/ARCHITECTURE_DISCUSSION.md).
Branches and commit messages below are proposals, not commands already executed.
The author remains in control of scope and implementation decisions.

## Current checkpoint

- Current branch: `feat/quota-reservation` when this checklist was created.
- [x] `uv` project, FastAPI connection lifecycle, root `.env`, central configuration.
- [x] PostgreSQL 17 development Compose setup on host port 5433.
- [x] Five SQLAlchemy tables and explicit development schema initialization.
- [x] Monthly-only reservation with atomic conditional capacity acquisition.
- [x] Scoped operation-key replay, including exhausted-quota and concurrent retries.
- [x] Same-key/different-unit rejection.
- [x] Monthly success finalization without another deduction.
- [x] Monthly release once, guarded by operation state and claim version.
- [x] Old-period release does not refund the current month.
- [x] Three cross-process tests: overspending, duplicate deductions, double refunds.
- [x] Last full verification: **11 tests passed against real PostgreSQL**.
- [x] Commits: `b5edbe3` reservation baseline; `06f5b72` retries/settlement/contention.

Not complete yet: the API currently exposes health only. There is no running
metered consumer, automatic reset, credit-funded execution, contract enforcement,
fresh-result cache, recovery worker, or end-to-end benchmark.

## Development / branch / commit rules

- [ ] Before each implementation slice, inspect `git branch --show-current` and
  `git status --short`. Never edit application code on `main`; switch/create a branch first.
- [ ] Branch from the agreed integrated baseline. If a prior feature branch is not
  merged, explicitly decide whether to continue it or create a dependent branch;
  do not silently assume its changes are already on `main`.
- [ ] Preserve unrelated edits and the author's staging. Stage intended paths only.
- [ ] Keep commits behavior-focused and independently coherent: implementation,
  relevant tests, and accurate documentation together. Avoid committing a broken
  half of a multi-table transaction.
- [ ] Before committing, inspect status, staged/unstaged diffs, and recent log;
  run relevant tests and `git diff --check`. Match the repo's `added:` / `fixed:` style.
- [ ] Never commit `.env`, real credentials, generated database data, or private transcripts.
- [ ] Commit, push, and create PRs only when requested. No force-push/amend by default.
- [ ] After each slice, summarize behavior, verification, limits, and remaining work.

## Test policy

Test **invariants and meaningful failure boundaries**, not every implementation
detail or every combination of inputs.

- Use real Postgres for transactional and concurrency correctness.
- Basic tests may use rollback/savepoint isolation; concurrency tests require
  independent connections/processes and actual committed state.
- Preserve the observed-lock contention gate: test success must not depend on
  requests accidentally running sequentially.
- Unexpected database errors must fail tests, not count as ordinary quota denial.
- Keep test cases sequential while their fixtures share feature configuration.
  Requests inside contention tests remain concurrent. Parallel test-suite execution
  needs better fixture isolation first; it is not an assignment requirement.
- Clean up only test-owned records. Prefer a dedicated test database as setup matures.
- Re-run relevant contention checks for stability. Repeated runs are not benchmarks
  or proof covering all possible executions.

## 1. Purchased-credit fallback and settlement

**Proposed branch:** `feat/quota-credits`

### Implementation

- [ ] Extend reservation to consume included capacity first, then usable credits.
- [ ] Persist the exact monthly/extra split on the operation.
- [ ] Reject the entire batch if combined capacity is insufficient; neither source
  nor the operation record may be partially committed.
- [ ] Keep operation identity, both balance changes, and allocation atomic. Review
  a consistent lock order before combining this with settlement/reset/recovery.
- [ ] Finalize credit-funded/mixed operations without another deduction.
- [ ] Release each source exactly once, preserving original-period monthly behavior.
- [ ] Define expired-credit release behavior explicitly; do not revive expired credits accidentally.
- [ ] Review the name/interface of `reserve_monthly` when it becomes combined admission;
  avoid maintaining two divergent accounting implementations.

### Focused tests

- [ ] One mixed allocation + repeated release scenario verifies correct source restoration.
- [ ] Insufficient combined capacity leaves both balances and operation records unchanged.
- [ ] Cross-process exhaustion with batch requests verifies accepted units never
  exceed included + credits and batch remainders are not partially served.
- [ ] Reuse duplicate-key and concurrent-release coverage for credit-funded operations.

**Commit checkpoint:** `added: prepaid credit allocation and settlement`

## 2. UTC monthly reset in place

**Proposed branch:** `feat/quota-reset`

### Implementation

- [ ] Refresh included allowance on the first of each month at 00:00 UTC.
- [ ] Use the existing mutable aggregate row and synchronize reset with admission
  and release. Multiple instances must not grant the allowance repeatedly.
- [ ] Handle skipped inactive months without granting accumulated allowances.
- [ ] Keep original `reserved_at` immutable and purchased balances independent of reset.
- [ ] Select an authoritative time source and a simple controllable-time interface
  for boundary tests. Do not change production accounting solely to make tests pass.
- [ ] Make reporting reflect the current period even before its first new admission.

### Focused tests

- [ ] Boundary refresh grants one allowance, does not roll over old units, and
  does not reset purchased credits; include one short-month/year transition.
- [ ] Cross-process first admissions after a boundary trigger only one refresh.
- [ ] Retain the existing old-period release test and add late-success coverage
  if reset integration changes that path.

**Commit checkpoint:** `added: synchronized calendar-month quota reset`

## 3. Complete operation identity and recoverable feature state

**Proposed branch:** `feat/operation-context`

### Implementation

- [ ] Persist normalized relevant inputs/fingerprint or reference a durable feature
  operation. Unit-count matching alone is not full request idempotency.
- [ ] Store enough context to resolve/retry the selected demo feature after process loss.
- [ ] Define what a duplicate sees for `RESERVED`, `DONE`, and `RELEASED`; replay
  must not silently execute released work as a new chargeable operation.
- [ ] Define operation/key retention and result availability separately from quota
  periods/cache freshness. Cleanup must not forget unresolved holds.
- [ ] If schema changes, use an explicit migration/update approach. `create_all`
  does not alter existing tables or enum types; choose the smallest suitable tooling.

### Focused tests

- [ ] Same key with different inputs but equal cost is rejected without balance changes.
- [ ] A recognized completed retry returns/references the prior outcome without
  executing and charging again. Recovery can locate its feature operation.

**Commit checkpoint:** `added: durable operation identity and recovery context`

## 4. Minimal contract and credit lifecycle

**Proposed branch:** `feat/entitlements`

This implements our agreed extension, not payments, procurement, or a billing platform.

### Implementation

- [ ] Give contract/access state a minimal home in the data model.
- [ ] Admit new work while active or in eligible seven-day grace; reject it after access ends.
- [ ] Preserve permission for already-admitted work to finish/settle after expiry.
- [ ] Full first-period allowance, no proration/no included rollover. Repeated
  activation must not grant duplicate included capacity.
- [ ] Carry credits across uninterrupted renewal. Retain them until contract end
  + 90 days on lapse; grace is inside that window, not an added seven days.
- [ ] Define exact renewal/reactivation and expiration transitions before mutating balances.
- [ ] Provide trusted, deduplicated credit grants; repeated billing/admin events
  must not add the same pack twice. Keep the initial integration small/local.
- [ ] Expose grace/deadline information for a possible UI notice. Do not build UI
  or notification delivery for this assignment by default.

### Focused tests

- [ ] One boundary-focused eligibility test covers active/grace/expired admission
  and settlement of previously admitted work.
- [ ] Renewal/reactivation/retention expiry preserves or expires purchased units correctly.
- [ ] Duplicate grant delivery adds capacity once; use concurrent delivery if the
  implementation relies on an atomic grant identity.

**Commit checkpoints:** separate eligibility/lifecycle from trusted grants if each
is independently complete, e.g. `added: contract eligibility and credit retention`
and `added: idempotent prepaid credit grants`.

## 5. Reporting and a runnable feature consumer

**Proposed branch:** `feat/quota-api`

### Implementation

- [ ] Add Pydantic request/response models and minimal FastAPI routes.
- [ ] Add usage reporting scoped to organization + feature: monthly limit,
  completed usage, reserved units, included availability, purchased-credit
  availability, and next reset. Make source breakdowns unambiguous.
- [ ] Derive reservations from operation records; do not subtract old-period
  included holds from current-month usage. Read a consistent accounting snapshot.
- [ ] Implement one small fake tracking/search consumer calling the real quota
  module: reserve, perform bounded work, finalize/release.
- [ ] Add controlled failure/timeout behavior for integration verification, not
  a real carrier integration. Confirm failure before releasing uncertain work.
- [ ] Map validation, insufficient quota, key conflicts, in-progress operations,
  missing configuration, and unavailable storage to clear responses.
- [ ] Verify organization scope at the API boundary; internal operation IDs alone
  are not authorization. Keep demo identity assumptions explicit, not production auth claims.
- [ ] Resolve/document the observable read-success boundary. LB response delivery
  cannot be atomically committed with accounting; do not claim that issue is solved.
- [ ] Account for a lost database commit response: a retry resolves by operation
  identity rather than treating the error as proof no deduction occurred.

### Focused tests

- [ ] Successful and confirmed-failed consumer calls produce matching operation/balance state.
- [ ] Reporting separates completed usage from holds and isolates organizations/features.
- [ ] Database unavailable before admission starts no work and returns an appropriate error.
- [ ] Exercise a lost-response/retry case without another charge; distinguish
  recoverable outcome from an unimplemented delivery guarantee.

**Commit checkpoints:** `added: per-feature quota usage reporting`, then
`added: metered demo consumer and API error handling` if independently useful.

## 6. Fresh-result reuse and business deduplication

**Proposed branch:** `feat/consumer-reuse`

### Implementation

- [ ] Implement org-scoped reuse of a result that organization already paid for,
  until its feature-specific freshness deadline. No free-hit counter.
- [ ] Match normalized feature inputs and prevent simultaneous fresh requests from
  duplicating work/charges. Coordinate this with operation identity.
- [ ] Cache eviction must not silently erase paid-reuse eligibility. Choose the
  smallest storage scheme; a separate cache service is not automatically required.
- [ ] Define demo-feature business uniqueness (shipment identity, not container
  number forever) if the selected consumer has write behavior.
- [ ] Document that free fresh-result reuse is a deliberate refinement of the
  assignment's per-search example. Discounts/fractional units are not implemented.
- [ ] Document traffic/rate-limit needs separately; no production rate limiter by default.

### Focused tests

- [ ] Fresh reuse consumes no additional units; expired reuse/new inputs are charged.
- [ ] A different organization does not inherit another organization's paid entitlement.
- [ ] Concurrent same-input requests result in the agreed single paid lookup/resource.

**Commit checkpoint:** `added: org-scoped fresh result reuse`

## 7. Retry-driven recovery and periodic worker

**Proposed branch:** `feat/quota-recovery`

### Implementation

- [ ] Atomically claim eligible `RESERVED` operations by expired lease; increment
  claim version and update lease deadline, never original reservation time.
- [ ] Use the same recovery function for recognized retries and the background worker.
- [ ] Persist/inspect feature outcomes before deciding to rerun, finalize, or release.
  Lease expiry alone is not proof of failure or permission to duplicate side effects.
- [ ] Choose concrete demo execution timeout, lease duration/renewal, polling interval,
  and bounded batch size. Five minutes and 30 seconds were discussed; neither was finalized.
- [ ] Run recovery as a separate process/task importing the same quota module.
  No Kafka, CDC, or dispatcher service.
- [ ] Handle worker restart/shutdown, retry limits, and unresolved outcomes without
  inventing automatic safe refunds. Log identifiers/status, not secrets.
- [ ] Review lease timing under row-lock waits: an operation's lease must not become
  misleading before its admission transaction finishes.

### Focused tests

- [ ] Crash after committed reservation, then worker recovery; capacity/result settle correctly.
- [ ] Crash after feature success but before accounting; recover without duplicate work/charge.
- [ ] Competing recovery workers/retry claim once; expired ownership cannot overwrite the winner.
- [ ] An active lease is not taken over. Reuse stale-owner settlement coverage.

**Commit checkpoint:** `added: shared lease recovery and periodic worker`

## 8. Reproducible startup and test setup

**Proposed branch:** `chore/demo-runtime`

- [ ] Add a minimal app image and Compose API/worker services alongside Postgres.
- [ ] Separate host database port 5433 from container-to-container `postgres:5432`.
- [ ] Supply `.env.example` values and explicit startup/schema/seed sequencing.
- [ ] Create just enough repeatable demo data; startup/restart must not duplicate grants.
- [ ] One documented command brings up the runnable consumer and its dependencies.
- [ ] Define a dedicated test database/reset workflow that never destroys development data.
- [ ] Verify health checks, startup ordering, worker operation, and graceful shutdown.
- [ ] If demonstrating multiple API replicas, ensure connections and state are shared
  correctly; use an LB only as needed for the demo, not as a new billing coordinator.

**Verification:** fresh-environment startup, restart with persisted data, test
command, one successful demo operation, and one recovery exercise.

**Commit checkpoint:** `added: one-command quota demo runtime`

## 9. Real load measurements and scaling limits

**Proposed branch:** `test/quota-load`

- [ ] Add one reproducible load script hitting the consumer, not only a bare counter.
- [ ] Include distributed organization/feature traffic, hot-bucket contention,
  batches, and retries in a small number of clearly described workloads.
- [ ] Report offered/achieved throughput, accepted/denied/error counts, duration,
  concurrency, and latency distributions (p50/p95/p99).
- [ ] Distinguish HTTP end-to-end latency from quota admission overhead; record
  finalization/release measurements separately where relevant.
- [ ] Measure toward the stated 2,000 operations/sec sustained and 10,000/sec peak
  workload; report honestly if local resources/implementation do not meet targets.
- [ ] Record hardware, process count, pool settings, database version, dataset,
  commands, warmup, and exact code revision for reproducibility.
- [ ] Assert accounting invariants after load. Denial-heavy traffic or cache hits
  must not be presented as successful paid-operation throughput.
- [ ] Explain bottlenecks: hot rows, pool saturation, database writes, operation
  retention, and recovery lag; propose changes for 50,000 orgs/30 features.

**Commit checkpoints:** load tooling first, then recorded results tied to that
revision. Do not confuse test durations with performance measurements.

## 10. Final documentation and submission

**Proposed branch:** `docs/submission`

- [ ] Write `DESIGN.md` about implemented decisions, rejected alternatives,
  concurrency/failure semantics, reset/reporting, and integration—not a feature list.
- [ ] Include real load numbers, reproduction commands, scaling changes, and where
  the implementation falls over. Never copy proposed behavior as implemented fact.
- [ ] Add an AI-assistance record distinguishing the author's design/input from
  assistant research, code, tests, and documentation. Raw conversation exports are
  optional evidence; inspect them for private data before sharing.
- [ ] Make README sufficient for a reviewer to start, exercise, and test the repo.
- [ ] Run the focused full suite, startup smoke check, and documented load command.
- [ ] Review diffs, secrets, dead scaffolding, known unsupported paths, and doc/code consistency.
- [ ] Verify all required changes are committed; push/PR only when requested.
- [ ] Prepare repo link or zip. Do not send the submission email without explicit authorization.

**Commit checkpoint:** `added: design rationale and reproducible submission guide`

## Scope guardrails

Do not add by default: payment processing, notification delivery/UI, Kafka/CDC,
historical reporting tables, unlimited overage billing, a separate quota service,
an enterprise auth system, or a production HA deployment.

If time becomes tight, discuss simplifying our extensions rather than silently
dropping agreed behavior. Prioritize the brief: concurrent enforcement, batch
policy, failure/retry correctness, monthly reset/reporting, a runnable consumer,
and real reproducible measurements.

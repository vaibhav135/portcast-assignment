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
  Attribution documentation was explicitly requested by the author.

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

## 4. Jointly refined decisions

These are collaborative outcomes, not exclusively human- or AI-originated:

- Monthly included allowance plus separate prepaid credits, with renewal/retention rules.
- Fixed UTC calendar months instead of the originally explored rolling window.
- Reservation before execution, no second success deduction, source-specific failure refunds.
- A single reusable quota library, now imported directly by the separate reporting
  server and schedule consumer; a separately deployed recovery worker remains planned.
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
- Concurrent in-progress demo retries may repeat cheap pure computation; this is
  not exactly-once execution for a real third-party operation.
- A saved result and accounting state support recognized replay, but do not solve
  refresh with a new key, indefinite result retention, or external uncertain outcomes.
- Contract/grace enforcement, fresh-result reuse across keys, recovery worker,
  one-command complete runtime, benchmarks, and final `DESIGN.md` remain unfinished.
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

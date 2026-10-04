# Quota Metering — Architecture Discussion

> Working record of our discussion, not a completed design or an implementation claim.
> Architecture exploration is complete enough to begin author-led implementation.
> This records agreed policies and the author's logical schema, not verified code.
> Review remaining implementation choices as code is written rather than blocking
> progress with an exhaustive series of design questions.
> **Agreed** means an explicit decision; **proposed** means a direction still needing confirmation.

## Latest author-requested deployment update

The author subsequently requested separate **reporting server** and **schedule
consumer** applications. This overrides the combined API/consumer deployment
topology recorded below; the earlier discussion is preserved as history.

- `src/server/main.py`: usage reporting + health, local port 8000.
- `src/consumer/main.py`: schedule API + health, local port 8001. Schedule logic,
  API models, and seeding live in `service.py`, `schemas.py`, and `seed_demo.py`.
- `src/shared`: configuration, database, tables, quota logic, shared response
  models, schema initialization, and the operation-context migration.
- Neither app imports the other. Both import the shared library directly and
  use the same PostgreSQL database. Quota is not a separate HTTP service.
- Separate server/consumer images contain only shared source + their own app and
  install locked dependencies with `uv`; credentials are supplied at runtime.
- Compose remains database-only. Schema setup/migrations are explicit commands.
  The recovery worker is a separate process at `src/consumer/recovery_worker.py`,
  not the schedule HTTP app. It reuses the consumer image with a different command
  and imports shared accounting, without including or importing server code.

**Recovery implementation checkpoint:** the demo now has expired-lease claims
with `SKIP LOCKED`, claim-version fencing, retry-driven recovery, and periodic
polling. Polling defaults to five seconds and batches of 20; the seeded feature
lease is 30 seconds. Only the pure schedule lookup is automatically repeatable.
Malformed/unknown outcomes retain their holds; expiry alone never causes refunds.
There is no heartbeat, actual execution timeout, or permanent retry cap yet.
These are demo defaults/limits, not fulfillment of arbitrary remote-write recovery.

Runnable commands and Docker networking are documented in the README. This update
does not turn historical proposals below into claims of implemented recovery or
verified performance.

## 1. The assignment problem

Build a Python 3 component that tracks and enforces monthly usage quotas for each
customer organization and API feature.

We count **units of feature usage**, not simply HTTP requests. For example:

- Tracking one container consumes one container-tracking unit.
- A schedule search consumes one sailing-schedule unit.
- A single batch containing 100 containers consumes 100 units.

Each organization has an independent allowance for each feature. The component
must decide whether an operation can proceed, account for its units safely, and
expose usage for the current period and the next reset time.

### Essential guarantees

- Concurrent requests across multiple application instances must not spend the
  same remaining capacity, over-serve, or produce negative balances.
- A separate application-side read followed by a write is insufficient.
- Downstream failures and retries must not silently lose quota or double-charge.
- Monthly boundaries must have precise, consistent semantics.

### Workload and constraints from the brief

| Area | Requirement |
| --- | --- |
| Language | Python 3 |
| Organizations | Approximately 5,000 initially, growing toward 50,000 |
| Features | Approximately 30 metered features |
| Traffic | Approximately 2,000 quota operations/sec sustained; peaks near 10,000/sec |
| Deployment | Horizontally scaled fleet, currently 8 instances |
| Traffic shape | Bursty per organization, including large batches |
| Latency | Under 10 ms overhead per quota operation in the request path |

Correctness is non-negotiable. Latency and throughput must be measured rather than
assumed from the technology choice or organization count.

### Submission requirements

- A repository that starts with one command.
- At least one consumer of the quota component running under load.
- Tests, including genuine contention against nearly exhausted quota.
- A final `DESIGN.md` explaining decisions, rejected approaches, shared state,
  batch semantics, failures/retries, reset/reporting, real reproducible load-test
  numbers, scaling changes, and limits.
- Disclosure of AI assistance and which design decisions were the author's own.

The exercise is intended to take approximately four hours. That makes scope
control important even while we explore product behavior.

## 2. What Portcast does — business context

Portcast provides supply-chain visibility and freight-intelligence software.
Its public offerings include container tracking, predictive arrival/departure
information, port congestion and terminal data, air-cargo tracking, sailing
schedules, analytics, and freight auditing. Customers can integrate data through
APIs into their logistics systems.

Its public terms describe a default 12-month contract, committed annual usage
volume, and additional-volume charges. Unless an order form specifies otherwise,
usage is billed in Bookmark IDs representing tracked shipment identifiers.
Commercial terms can vary by customer order form.

**Important distinction:** the assignment explicitly says its feature names are
illustrative and the exercise is not related to Portcast's actual product. We
use this context to understand enterprise usage, not to infer hidden requirements
or replace the brief's monthly-quota requirement with annual-volume accounting.

## 3. Our product model

### 3.1 Contract, payment, and usage period are separate

**Agreed:** an active contract determines service eligibility. A contract can be
annual even though included allowance refreshes monthly. Payment frequency is
external to the quota component.

For example, an annual contract can grant 500 container-tracking units in each
monthly period, with additional capacity available through prepaid credit packs.

We are not building checkout, invoicing, pricing, or payment processing. A trusted
billing/admin integration would tell the quota component when capacity has been
granted and whether a contract is active. Its exact interface remains open.

### Contract renewal reminders and service grace

**Agreed duration:** seven calendar days of service grace after contract expiry.
This is our chosen courtesy policy, not a universal industry standard.

**Agreed behavior:** during those seven days, customers continue normal metered
usage under their existing plan: the usual monthly allowance rules, purchased
credits, and batch limits still apply. Grace extends service eligibility; it does
not mean unlimited usage. Purchased credits remain usable during grace.

The author also requested a UI card/banner warning that renewal is pending and
showing the access deadline. UI implementation is outside this component, but
its integration should make the grace state/deadline available to the consumer.

**Still open:** which customers qualify (for example, renewal pending versus
explicit non-renewal). Normal calendar-month rules apply during grace. Credit
retention is measured from contract end, not from the end of grace.
Grace must not be interpreted as an automatically renewed contract.

**Proposed notification schedule:** begin annual renewal outreach 60 days before
expiry, follow up before any contractual cancellation deadline, and send a final
reminder approximately 14 days before renewal. The schedule is not finalized;
notifications and commercial follow-up remain outside the quota component.

Research context: [Microsoft 365](https://learn.microsoft.com/en-us/microsoft-365/commerce/subscriptions/what-if-my-subscription-expires?view=o365-worldwide)
documents continued-access periods of 30 days for some subscription/enterprise
arrangements, with exceptions by program and agreement.
[Stripe](https://docs.stripe.com/billing/revenue-recovery/smart-retries) recommends
eight payment retries over two weeks, but that is a payment-recovery schedule,
not a service-access grace guarantee.

### 3.2 Included allowance

**Agreed:**

- Configured per organization and feature.
- Refreshes on the first day of every calendar month at 00:00 UTC, independent
  of the contract start date.
- Grant the full configured allowance on activation, even for a partial first month;
  no initial-period proration in the assignment implementation.
- Unused included allowance does **not** roll over.

This replaces the earlier idea of a trailing 30-day rolling window. We no longer
need usage to age out continuously to determine available included capacity.

Rationale: a defined monthly entitlement is easier to explain, report, and enforce
than an exact trailing window. Customers can still extend capacity using credits.
This policy does not depend on an assumption that every customer uses all of its
monthly allowance.

### 3.3 Purchased credits

**Agreed:**

- Purchased credits are a separate, finite balance for an organization/feature.
- They do not increase the recurring included allowance.
- Unused purchased credits carry forward in full while the contract is active.
- Unused purchased credits also carry forward in full across uninterrupted
  contract renewal; renewal itself does not expire or reduce the balance.
- Credits remain usable through seven-day service grace. After grace they are
  unusable without renewed access, but retained until 90 days after contract end.
  Reactivation within that window restores access; otherwise they expire.
  The seven days are inside the 90-day window, not added before it starts.
- This retention window does not grant service access or additional allowance.
- No percentage-based monthly decay, such as retaining only 50%.
- Consume included allowance first, then purchased credits.

Purchased credits provide extra units, not permission to over-serve beyond the
combined entitlement. Unrestricted pay-as-you-go overage is not our chosen model.

The author accepted 90 days as a customer-friendly retention policy, not as a
claimed industry standard. Expiration should preserve accounting history rather
than erase it. Its deadline should be disclosed; notification delivery remains
outside the quota component's initial scope.

**Still open:** exact retention-boundary semantics, cancellation versus natural
expiry, and interaction with any approved access extension. We have not adopted
a separate 12-month-from-purchase credit-expiry rule.

### 3.4 Batch behavior

**Agreed: all-or-nothing.**

If a request requires more units than the available included allowance plus usable
purchased credits, reject the entire request. Perform no partial work and deduct
nothing. Return a clear response showing required and available capacity; its
exact HTTP status and response shape are undecided.

Rationale: choosing an arbitrary subset of containers or searches would require
additional selection/order semantics and complicate the consumer's behavior.

Example:

```text
Remaining included allowance:  20 units
Purchased credits:            100 units
Request requires:              50 units

On successful charge:
  Included allowance consumed: 20 units
  Purchased credits consumed:  30 units
  Purchased credits left:      70 units
```

The allocation is persisted when capacity is reserved. Success does not deduct
it again; confirmed failure releases it through a compensating database update.
Exact transactions still need implementation and verification.

## 4. Time and monthly boundaries

**Agreed direction:** define enforcement boundaries and return timestamps in UTC.
Clients may convert timestamps into local time for presentation without changing
the actual reset instant.

Every instance must assign requests to the same period. A period should be
start-inclusive and end-exclusive: at the exact boundary, the new period applies.
That interval convention is proposed, not yet formally confirmed.

### Agreed calendar-month policy

The later discussion replaced contract-anniversary anchors with calendar months.
Access begins only when the contract activates; the included allowance refreshes
at 00:00 UTC on the first of each month. Use a suitable date/time library for
calendar boundaries rather than hand-written month arithmetic.

Example:

```text
Contract effective timestamp: 2026-10-15T14:30:00Z
First access/allowance span:   Oct 15 14:30 UTC → Nov 1 00:00 UTC
Next monthly period:          Nov 1 00:00 UTC → Dec 1 00:00 UTC
```

### Deliberate simplification: no first-month proration

The author prefers prorating a partial first period as a future product policy,
but explicitly chose a full first-month allowance to keep this assignment simple.
This applies to each configured feature. Purchased credits remain separate.

Known consequence: a customer activating five days before month-end receives a
full allowance for those five days, then another full allowance on the first.
This boundary opportunity is accepted for the initial scope, not overlooked.
If activation can be repeated, it must not grant the same allowance repeatedly;
the activation/reactivation rules still need design.

A future proration policy should explicitly define the remaining-time fraction,
integer-unit rounding, and application to each feature's limit. Per-feature limits
can share one calculation policy; separate algorithms per API are not necessary.

The authoritative clock remains an implementation choice. Period assignment is
based on the original reservation; recovery does not change that timestamp.

## 5. Technical architecture direction

### Quota reservation lifecycle

**Agreed direction:** reserve required capacity atomically before starting
chargeable work. Reserved units are unavailable to competing requests but are
not yet final charges. Finalize the charge on confirmed success; release the
reservation on confirmed failure.

Reservations must respect the combined included-allowance/credit entitlement and
the all-or-nothing batch policy. The author's operation record stores the
monthly/extra allocation split; exact database transactions remain to implement.

**Agreed operation states:** `RESERVED` branches to `DONE` on confirmed success
or `RELEASED` on confirmed failure. No separate processing-state write is needed;
`RESERVED` covers held capacity and unfinished/uncertain work. Lease expiry alone
does not change the operation to `RELEASED`.

Reservation removes units from available capacity once. Success marks the
operation `DONE`; it must not deduct those units a second time.

**Author's chosen aggregate semantics:** `units_consumed` includes both reserved
and successfully finalized units. Reservation increments it and reduces
`units_remaining`; success changes operation status only. Confirmed failure
reduces consumption and restores eligible capacity atomically with `RELEASED`.
This is a compensating update after commit, not a database transaction rollback.
Unresolved operations remain counted as consumed/held; polling does not guarantee
that every uncertain outcome can be resolved automatically.

**Agreed monthly-boundary behavior:** an admitted operation retains its original
period/allocation across reset. New operations use the new period. Releasing an
old-period reservation must not increase the new month's included allowance.
The author chose one mutable current-month aggregate, not a historical bucket
table. Immutable per-operation `reserved_at` preserves original period identity.
An old-month success changes its operation status without altering new-month
balances; releasing old included units does not credit the new month. Historical
aggregate tables/reporting or indefinite archives are not requirements.

**Agreed contract-boundary behavior:** work admitted while access is valid,
including grace, may finish and settle its existing reservation after access ends.
Reject new chargeable operations after the access deadline. Do not cancel
already-admitted work solely because the contract/grace deadline passes.

Do not assume that timeout or process death proves failure. Handling unknown
outcomes and safe re-execution still require consumer-specific handling.
Monthly and contract boundary policies are agreed. The definition of confirmed success, particularly for read
response delivery, remains open.

### Request deadlines and reservation release

**Agreed:** synchronous feature work has a bounded, feature-specific execution
timeout informed by its SLO, expected runtime, and dependency behavior. Requests
must not wait indefinitely. The ownership lease should be consistent with that
timeout and allow time for cleanup/final accounting; exact values remain open.

- Confirmed success finalizes the charge.
- Confirmed failure, including a timeout whose execution was safely stopped,
  releases the reservation back to available capacity. Preserve operation history.
- A timeout of our wait does not necessarily cancel remote work. If downstream
  work may still complete, its outcome remains uncertain and requires recovery
  rather than an automatic release.
- Once an operation is failed and its reservation released, a late completion
  must not finalize a charge against that released hold.
- A crashed process cannot clean up its committed reservation; persisted operation
  state enables retry-driven or background recovery.

**Agreed timing direction:** short operations use feature-specific bounded
execution/ownership deadlines. Long-running tasks have durable job records and
ongoing execution tracking. A started record alone does not prove current
ownership; lease renewal or execution-status checks still need specification.
Crossing an ownership deadline permits recovery, not an inference that work failed.

Lease durations/renewal and request timeouts will be selected for the demo consumer;
we do not need arbitrary values for every hypothetical feature now. The assignment's
quota-overhead target is distinct from feature execution time.

### Read-result reuse and charging

**Agreed:** after an organization pays for a read lookup, reuse of the same fresh
result by that organization consumes no additional quota units until the result's
freshness deadline. Do not introduce a counter limiting the number of free hits.
The author rejected that counter as additional concurrent accounting complexity
without sufficient benefit.

- Match the feature and relevant request parameters; unrelated lookups are not free.
- Free reuse applies to the organization that paid for the result. A global cache
  hit does not automatically make another organization's first lookup free.
- Freshness eligibility follows the logical result/deadline, not incidental cache
  eviction or how long an entry remains in memory.
- TTL/freshness is feature-specific and informed by source updates and product
  requirements, not one universal timeout.
- This permits intentional repeat reads as well as recovery after a lost response.
- This avoids an additional charge; it does not refund the original charge or
  prove that the original response reached the client.
- Zero quota consumption is not unlimited traffic; rate limiting is a separate
  protection, whose implementation scope remains undecided.

This deliberately refines the assignment's illustrative "one unit per schedule
search" rule into a paid lookup with free fresh-result reuse. Explain this extension
explicitly in the final design. Exact success/charging boundaries remain open.

**Implementation still open:** how to preserve paid-reuse eligibility across
instances and cache eviction, handle simultaneous initial lookups, retain/recover
results, and coordinate eligibility with quota accounting. No free-hit counter
does not mean that shared cache/accounting coordination disappears.

### Abandoned-operation recovery

**Agreed direction:** persist pending operation state in shared PostgreSQL and
use polling for recovery. Do not introduce CDC or a broker for this purpose.
The author's Kafka reference was an analogy for persistence/replay, not a proposal
to add Kafka.

**Historical deployment discussion (superseded by the update above):**
Normal requests execute directly in API processes. A separate recovery worker
periodically checks for pending operations with expired ownership leases, not
every pending operation. In ECS terms, this is a separate worker service/task,
not one recovery worker inside every API task. The worker can share the API image
and codebase with a different startup command. Concrete deployment configuration
remains open; the separate API/worker deployment boundary is finalized.

**Agreed:** a recognized client retry can also recover the existing operation
without reserving units a second time. Retries and the polling worker use the same
ownership/lease checks and atomic recovery-claim rules. Polling is the fallback
for abandoned operations that nobody retries. A still-active owner is not taken
over merely because a duplicate request arrived. Refresh recovery still depends
on preserving operation identity or a feature-specific deduplication mapping.

**Agreed schema direction:** an operation has immutable `reserved_at`, mutable
`lease_expires_at`, and `claim_version` (initially zero). Recovery takeover updates
the deadline and increments the version, not the original reservation time.
Claim and completion/release checks must be atomic so stale versions cannot
finalize or release the operation. Exact claim queries remain an implementation
choice; bounded batches with `FOR UPDATE SKIP LOCKED` were proposed.
Work runs outside the claim transaction. Expired leases do not prove that prior workers
stopped or downstream work failed; safe re-execution still needs feature-level
idempotency/deduplication or outcome lookup.

**Still open:** polling interval, lease duration/renewal, claim fencing details,
recovery behavior for reads versus writes, and unresolved-outcome handling.
Five-minute polling was considered; 30 seconds was recommended but not selected.
Polling frequency and lease duration are independent settings. Startup scanning
can supplement periodic polling but is not sufficient as the sole mechanism.

### Usage reporting contract

**Agreed:** usage reporting is scoped to one organization and feature. Return:

- Monthly included limit.
- Completed units used in the current period.
- Units reserved by unfinished operations.
- Available included units.
- Available purchased credits.
- Next monthly reset timestamp in UTC.

Pending reservations are not final reported usage, although stored aggregate
`units_consumed` includes them. Derive current-period included reservations from
`RESERVED` operations and `used_from_monthly`; completed included usage equals
aggregate consumption minus those reservations. Availability is `units_remaining`.
Derive held credits separately using `used_from_extra`; unresolved old-period
credit holds must also remain unavailable. No separate aggregate reserved counter
is required. Reporting still needs a consistent snapshot under concurrent updates.

Purchased-credit reservations must reduce available purchased credits too.
The exact breakdown of included versus credit-funded used/reserved quantities
remains to be specified so totals are unambiguous. Endpoint path, field names,
consistency guarantees, and HTTP errors remain open.

### 5.1 Storage and deployment

**Agreed:** use shared PostgreSQL, running in Docker for local reproducibility.
Application instances do not maintain authoritative per-instance balances.

**Historical integration shape (deployment topology superseded above):**
one reusable quota module imported by the
API/consumer and the recovery worker. It is not a separate HTTP service, process,
or third ECS task type. There are two deployable application process types:

- API service with one or more API tasks.
- Recovery-worker service, initially one worker task, scaling independently.

```text
API service/tasks                  Recovery-worker service/task
  API + feature consumer             Polling + feature recovery
  Quota module                       Same quota module
         |                                  |
         +--------- Shared PostgreSQL ------+
```

Shared PostgreSQL supports both processes: quota accounting, operation/recovery
state, and any persisted demo-feature data. It is not exclusively storage for the
quota module. Quota-state changes go through the shared module so request and
recovery paths enforce the same rules.

**Agreed simplicity principle:** these are responsibilities, not a mandate for
many packages/classes or abstraction layers. Keep contract/credit handling in the
quota module initially; validation and response mapping in the API; business
deduplication and freshness with the feature consumer. Separate further only for
a concrete need. The author has provided and refined their logical schema and
will lead implementation. Review code and transactions incrementally rather than
requiring every edge case to be discussed before coding.

The quota module should return structured results; the API layer translates them
to HTTP responses. Quota accounting should not be coupled to response rendering.
Framework, exact interfaces, runtime configuration, and packaging are still
undecided at this discussion checkpoint. The earlier two-process topology is
superseded by the latest author-requested server/consumer split; the recovery
worker remains future work.

A separate service can offer independent scaling and a shared integration
boundary, but adds a network hop and additional failure handling. Server load
alone does not establish that moving the component into a service improves latency.

### 5.2 Concurrent correctness

**Discussed candidate approaches:** PostgreSQL atomic conditional updates and/or
short transactions with appropriate row locking.

The database must coordinate admission and accounting. If a request consumes both
included allowance and credits, changes to both sources must be atomic. Credit
grants must also be deduplicated so a retried grant cannot add capacity twice.

The author's logical schema is recorded below. Lock targets/order, isolation,
reset synchronization, and exact queries remain implementation work. Reservation
must commit the operation and allocation changes together; release must commit
state and compensating balance changes together. State/version checks must
prevent double finalization, double release, and stale-owner updates.
No correctness guarantee is implemented or tested yet.

### Author's logical schema snapshot

This transcribes the author's latest diagram; it is not SQL or a claim that all
required execution data is represented. Concrete types, indexes, and libraries
will be handled during implementation. UTC semantics are settled; physical
timestamp-type choices need not block the logical architecture discussion.

| Record | Fields in the author's current proposal | Identity/constraints |
| --- | --- | --- |
| `org` | `id`, `name` | Primary key `id` |
| `api_quota_map` | `id`, `feature`, `lease_duration_sec`, unit-cost value | Primary key; unique feature |
| `quota_usage_per_request` | `id`, `org_id`, `feature`, `status`, `used_from_monthly`, `used_from_extra`, `idempotency_key`, immutable `reserved_at`, `claim_version`, `lease_expires_at` | Primary key; unique `(org_id, feature, idempotency_key)` |
| `feature_quota_extra` | `id`, `org_id`, `feature`, `total_allocated`, `units_consumed`, `units_remaining`, `expires_on`, `last_added` | Primary key; unique `(org_id, feature)` |
| `feature_quota_monthly` | `id`, `org_id`, `feature`, `total_allocated`, `units_consumed`, `units_remaining`, `resets_on`, `last_renewed` | Primary key; unique `(org_id, feature)` |

- States: `RESERVED`, `DONE`, `RELEASED`; no separate processing state.
- Allocation amounts are retained operation facts, not reconstructed from current
  balances. Their sum gives the reserved quantity.
- Monthly aggregates reset in place; reset markers/current-period identity and
  admission/release must be synchronized to avoid races.
- Extra-credit `expires_on` means contract end plus 90 days, not monthly reset or
  access deadline. Access ends after seven-day grace.
- Feature costs may instead live in static configuration; that alternative is not
  finalized. Batch quantity still determines required units; existing operations
  retain their allocation despite configuration changes.

**Known gaps to resolve while implementing:** contract/access state needs a home;
original inputs/fingerprint or a feature-operation reference are needed for
same-key/different-input detection and safe recovery. Result/cache storage and
paid-freshness eligibility belong with the feature consumer. These do not require
a separate service or a full history/ledger subsystem by default.

### 5.3 Performance considerations

- A frequently accessed organization/feature can become a hot row: competing
  admissions must serialize somewhere to enforce its shared limit.
- Independent buckets should avoid unnecessary global contention.
- Connection pooling, short transactions, and indexes will matter.
- Do not hold a database transaction open while downstream work executes.
- PostgreSQL is a starting choice, not proof that we meet the stated load/latency.

We have no benchmark results and will not claim any until measured.

## 6. Proposed verification strategy — after architecture agreement

Use a small fake consumer to represent downstream work, but use **real PostgreSQL**
for integration, concurrency, and load testing. We do not need a real shipment
tracking product or an elaborate simulation framework.

Planned categories to agree on:

1. **Basic accounting:** accept, reject, allocate allowance/credits, and report usage.
2. **Contention:** synchronized competing requests for the same nearly exhausted
   bucket; verify accepted units, balance accounting, and rejection behavior.
3. **Cross-process behavior:** exercise multiple application processes so a local
   lock cannot accidentally stand in for distributed correctness.
4. **Failures and retries:** inject downstream failures, duplicates, and uncertain
   outcomes according to the failure protocol we eventually choose.
5. **Time boundaries:** controlled time for reset, short-month, and contract-end cases.
6. **Load:** exercise the runnable consumer, record throughput, latency distributions,
   errors, workload shape, and environment, with reproducible commands.

Tests demonstrate implementation behavior under the exercised conditions; the
transaction design must also explain why the invariant holds. A no-contention
test or a successful performance benchmark alone does not establish correctness.

No tests or load scripts have been written as part of this discussion.

## 7. Remaining implementation work and honest limits

This is a review checklist for implementation, not another prerequisite sequence
of architecture questions. The product model, deployment shape, operation states,
allocation split, and aggregate counter meanings are settled.

1. **Transactions and constraints:** implement atomic reservation/release,
   idempotency uniqueness, state/version guards, consistent lock order, and
   current-month reset synchronization. Specify nonnegative accounting invariants.
2. **Consumer/recovery contract:** persist enough identity/inputs to reject key
   misuse and resolve work. Choose concrete demo timeouts, lease behavior, polling
   interval, retry-record retention, and in-progress duplicate responses.
3. **Access and credit lifecycle:** represent contract/grace eligibility, trusted
   renewal/grant updates, and exact expiry/refund boundaries without introducing
   payment processing. Prevent repeated activation from granting duplicate allowance.
4. **Cache/reporting integration:** implement org-scoped fresh-result reuse and
   choose result storage; prevent simultaneous fresh lookups from duplicating work
   or charges. Map agreed reporting fields consistently to included/credit counters.
5. **Runtime and verification:** choose framework/database tooling and minimal
   startup configuration; implement contention, failure, retry, boundary, and load
   tests. Record real measurements, scale limits, and AI contributions.

### Important unresolved limit: response delivery

The author wants read charges to respect successful transmission through our
outer serving boundary, including the LB. Ordinary HTTP does not provide an
atomic durable receipt tying that event to PostgreSQL accounting. Fresh-result
reuse avoids a second charge after some lost responses, but does not prove delivery
or refund the first charge. Do not describe LB-delivery correctness as solved.
Choose and explicitly document the consumer's observable success boundary during
implementation; do not silently replace the author's policy.

### Other limits to preserve

- Leases/polling provide recovery opportunities, not proof of failure or exactly-once
  downstream execution. Unknown outcomes may remain reserved.
- Fail closed for new work when safe database admission is unavailable. A lost
  commit response is an unknown outcome; a read replica alone does not establish
  safe writable failover. Document durability assumptions.
- No completed code, passing tests, latency results, or production-scale claims
  are implied by this document.

## 8. Scope guardrails and next discussion

We have chosen a product direction, not permission to build a billing platform.
Credit packs and contract eligibility are deliberate extensions; they must not
crowd out the brief's core correctness, retry, reset, and measurement requirements.

The author has supplied and refined the schema through diagrams and now wants
to move to coding rather than discussing every atomic detail in advance.
The requested action at this stage is documentation cleanup only, not permission
to implement the application automatically.

Next: proceed with author-led code/skeleton and tests at the level requested.
Review concrete transactions against these policies as they are written. Explain
correctness issues and let the author attempt fixes; do not rewrite or add layers
without asking. Pause only for genuine blocking ambiguity, not every minor choice.

This document will evolve as we decide. The final `DESIGN.md` should describe the
actual implementation and measured behavior, not copy unverified proposals as facts.

## 9. Research references

Public pages consulted during the discussion:

- [Portcast overview](https://www.portcast.io/)
- [Portcast container-tracking API](https://www.portcast.io/container-tracking-api)
- [Portcast Terms of Use](https://www.portcast.io/terms-of-use), especially clauses
  3.2, 3.3, and 5.3.
- [Algolia pricing](https://www.algolia.com/pricing): monthly allowances and annual
  enterprise contracts.
- [Mapbox pricing](https://www.mapbox.com/pricing): monthly usage pricing and annual
  commitment discounts.
- [ElevenLabs pricing](https://elevenlabs.io/pricing): monthly credit refresh,
  bounded included-credit rollover, and separate top-up credits.
- [OpenAI prepaid billing](https://help.openai.com/en/articles/8264644-how-can-i-set-up-prepaid-billing):
  purchased-credit expiration after one year.
- [Stripe billing credits](https://docs.stripe.com/billing/subscriptions/usage-based/billing-credits):
  credit grants, optional expiration, and ledger-based accounting.
- [Stripe billing-cycle anchors](https://docs.stripe.com/billing/subscriptions/billing-cycle):
  anchored periods and short-month handling.

These are examples of published policies, not a statistical survey of industry
practice. Customer contracts and published policies may differ or change. Stripe's
billing credits are invoice-settlement infrastructure, not synchronous quota enforcement.

# Human–AI collaboration

**Author:** Vaibhav Bisht. **Assistant:** an AI coding/research assistant used through
OpenCode. This is an attribution summary, not a verbatim transcript or a claim that
either party independently produced every decision. The author directed the project;
substantial implementation, tests, tooling and documentation were AI-written.

## Author's contributions

- Created and refined the original architecture and five-table schema diagrams,
  including per-request allocation, idempotency, immutable admission time and
  ownership/version concepts. The original drawings are included in
  [DESIGN.md](../DESIGN.md) unchanged.
- Proposed shared PostgreSQL/Docker and selected all-or-nothing batches, UTC
  calendar months, full initial allowance, prepaid capacity, and the meaning of
  aggregate consumption as held plus finalized units—without a second success deduction.
- Directed the library integration, separate reporting/consumer applications,
  independent images and recovery process, simple scope, and incremental review.
- Challenged retry, delivery-boundary and performance assumptions; redirected an
  overextended investigation toward small, explicit transaction comparisons.
- Chose to retain the tested SQLAlchemy implementation and document measured
  optimization directions rather than undertake a late partial migration.
- Supplied the measurement-driven scaling direction: optimize PostgreSQL first,
  then address connections, safe read offload, caching, retention and hot-bucket
  limits before considering sharding.
- Selected broader contract/grace/credit-retention and fresh-result-reuse policies;
  these remain deferred extensions, not claims of implemented assignment functionality.

## AI assistance

- Read the brief, researched alternatives, reviewed the schema, and explained
  concurrency, transaction, idempotency, lease and durability tradeoffs.
- Wrote application/infrastructure code: SQLAlchemy models and configuration,
  quota admission/settlement/reset/reporting, API models/routes, migrations,
  recovery, repeatable seed, container setup and runtime smoke tooling.
- Proposed and implemented details such as conditional updates, `ON CONFLICT`
  identity claiming, explicit transaction ownership and claim-version fencing.
- Wrote the pytest suite and fixtures, including real PostgreSQL rollback tests,
  independent-process contention gates and regression coverage; ran verification.
- Wrote k6/fixture-reconciliation tooling, isolated diagnostic probes and the
  documentation drafts. Executed approved Git operations; the author also performed
  earlier setup/review and repository operations.

## How we worked

The author supplied ideas and constraints, reviewed changes, and approved implementation
slices. Storage, lifecycle and failure policies were refined jointly; accepting or
reviewing generated code is distinct from independently writing it. The assistant
also made scope/interpretation mistakes, corrected through the author's feedback.
The agreed workflow is to clarify uncertainty, keep experiments small, preserve
evidence, and obtain approval before changes or publication.

## Lessons and deliberate choices

Load testing expanded into excessive profiling/tracing, complicating interpretation.
Smaller measurements and native database diagnostics clarified individual delays;
controlled transaction comparisons showed promising reductions through lighter
database access and fewer round trips. They did not establish production capacity
or fully explain the earlier high-load failure. Detailed tracing was retired and
manual probes isolated rather than adopted into the application.

Technical decisions/results and their limits are in [DESIGN.md](../DESIGN.md),
[benchmarks](../benchmarks/README.md) and [manual experiments](../tests/experiments/README.md).
The previous full working record is temporarily preserved verbatim in root
`temp-contributions.md` for review; it is not required reading or a raw chat export.

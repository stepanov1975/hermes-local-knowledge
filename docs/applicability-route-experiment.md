# Offline applicability-aware promotion experiment

## Decision

The prepared applicability veto showed a useful semantic signal but **did not pass
acceptance**. Retain the private experiment; do not deploy this candidate.

The earlier [revision veto](revision-route-experiment.md) rejected endorsements
on any byte change. That harmed harmless edits and historical requests. This
successor rejects a learned promotion only when a current source review says it
is inapplicable to the exact immediate lookup. Recency is not the predicate.

## Small candidate, explicit boundary

The private runner reuses the pinned v0.5.3 managed retrieval, feedback routing,
native adapter, disposable telemetry and authoritative predecessor evaluator.
There is no production plugin change, new ranker, graph or database schema.

A prepared review binds the exact source root, explicit feedback/artifact IDs,
query, type filter, limit and caller-supplied lookup intent/target/operation/context.
Preparation validates the exact current primary source, its SHA-256, a verbatim
quotation, rationale, AI author and original verification time. A frozen one-hour
maximum review age is used for this run. Missing, invalid, expired, future-dated,
duplicate or mismatched evidence preserves incumbent behavior; so do applicable
and uncertain verdicts. A new feedback ID cannot inherit an old veto. Historical
endorsement hashes are neither required nor used as semantic evidence.

An exact matched `inapplicable` review restores the captured unassisted result
page after incumbent routing. The artifact remains ordinarily retrievable. This
post-routing experiment does not avoid an incumbent typed retry. It does not
select an alternative or inspect scoring labels to decide the veto.

A private adapter injects the same real native `lookup` arguments into both lanes
and bridges that context to the experimental service. Production does not yet
consume this applicability signal. Native telemetry retains the incumbent route
trace alongside the experimental final IDs in disposable state; it is not a
proposed production provenance schema. Preparation checks and timing are fixed-run
evidence, not a live file-drift detector or autonomous verification service.

## Executed evidence

- Three source-reviewed semantic discriminators: **one gain, no regressions**;
  harmless-edit and historical-intent complete response pages unchanged.
- Full packet: **11 completed, zero case errors, two gains, zero primary or harmful
  regressions, nine unchanged first results**.
- Eight constructed development/control cases and three previously selected
  current-corpus real-query cases; not a holdout or representative traffic sample.
- Eleven source-only AI reviews: nine applicable and two inapplicable. One review
  was intentionally omitted as a missing-record control. Nine reviews matched the
  actual selected promotion; two vetoes fired.
- Gains: one explicit incompatibility with current-format recovery; one existing
  wrong-primary promotion of an execution receipt instead of a workflow source.
  The latter corrects a bad endorsement, not proven historical source staleness.
- Incumbent-vs-itself controls, complete non-triggered response pages, frozen input
  state and the selected source-file hashes remained stable.
- Focused private harness: **35 passed**. Ruff and mypy passed for both authored
  Python files. Relevant public contracts/plugin/portable-regression suite:
  **120 passed**; production source was unchanged.

The relevance expectations were frozen AI-authored judgments with previous
baseline exposure. The applicability reviewer read full frozen sources with
ranking labels and candidate outputs withheld; those reviews are inputs, not
independent ranking gold or human judgments. Real lookup contexts were
reconstructed from exact queries and same-session task evidence, then summarized
within native field limits. They are not exact historical per-call context.
Original and corrected-context review receipts are retained privately.

## Performance gate and limits

Both lanes ran 30 alternating warm rounds through real managed search, native
projection/serialization and disposable telemetry, without concurrent parent
tests or reviews. Three full-packet cases exceeded the frozen **2 ms**
candidate-minus-incumbent median response budget: approximately **2.6, 12.1 and
8.0 ms**. The candidate remained rejected; the threshold was not relaxed and the
packet was not rerun to obtain approval. Earlier noisy synthetic measurements
are also retained, not counted as performance acceptance.

Gate-only median times were below 0.2 ms, but that does not establish negligible
end-to-end cost. Response timing includes harness instrumentation and ordinary
local I/O, not provider investigation, host transport or scheduling. The observed
differences are not proof that the applicability rule caused each slowdown.

This establishes feasibility of a prepared, narrowly scoped semantic veto,
not unseen-task benefit, autonomous investigator reliability, discovery of new
replacement documents or overall operational savings. Integration and a bounded
prospective comparison remain separate authorization decisions.

## Retention

The one-off runner, tests, frozen sources, tasks, reviews, packet and detailed
reports remain private, outside this repository. Public retention is this
conclusion plus the predecessor's existing portable relevance regressions. No
runtime ranking/configuration change, deployment, push or restart occurred.

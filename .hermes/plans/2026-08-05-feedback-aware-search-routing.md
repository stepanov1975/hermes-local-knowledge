# Feedback-Aware Search Routing Plan

## Goal

Improve first-result routing for `knowledge_search` by learning only from explicit local feedback and by retrying one concise, type-scoped accepted route when the ordinary mixed search misses it. Preserve the lexical index as the source of truth, avoid Hindsight/network dependencies, and prove that historical recall and negatively labeled queries do not regress.

## Root cause

The live lexical index has strong recall but moderate Hit@1. Broad mixed-type queries can omit a specific support document from the returned page even when a shorter query plus an artifact-type filter finds it. The exact query→artifact feedback needed to recover those cases already exists in `usage.sqlite`, but search currently uses feedback only for reporting/evaluation.

## Constraints

- Learn positive routes only from explicit feedback containing both a query and artifact ID; a query-level negative may veto without an artifact ID.
- A mere successful tool call must never train the router.
- Keep `usage.sqlite` canonical; do not call or retain into Hindsight.
- Scope feedback to the source root stored in the current index and fail open if telemetry is missing, corrupt, or locked.
- A remembered artifact must still be found in the current live index before it can be returned.
- Apply at most one bounded learned route per search.
- Respect explicit `artifact_type` filters and do not apply managed feedback to caller-owned index databases.
- Keep the dependency set standard-library-only.

## Design

1. Add a small feedback-routing module that:
   - reads explicit feedback for the current index source root;
   - uses only the latest rating for each normalized query/artifact pair;
   - activates only positive (`useful`/legacy `great`) pairs;
   - suppresses pairs whose latest rating is negative and lets a newer rejection on the current query veto an older overlap route;
   - matches exact or strongly overlapping query terms deterministically.
2. In `LocalKnowledgeService.search`:
   - run the ordinary search unchanged;
   - load the single best active feedback route;
   - if its verified artifact is already present, move it to rank 1;
   - otherwise retry an accepted query no longer than the current query with its mapped artifact type and promote the target only if that retry finds the exact current artifact;
   - otherwise return the ordinary results unchanged.
3. Keep the existing lexical evaluation as the unassisted regression baseline to avoid evaluating a learner on its own training labels.
4. Evaluate separately against live telemetry:
   - unassisted Hit@1/Hit@10 must not regress;
   - assisted historical replay must improve first-result routing;
   - negatively labeled query/artifact pairs must not gain rejected top-rank exposure;
   - measured latency overhead must remain proportionate.

## Implementation stages and gates

### Stage 1 — Contracts and red tests

- Add focused tests for exact promotion, concise/type-scoped retry, newer-negative suppression, filter mismatch, missing/corrupt feedback fail-open, and no-feedback stability.
- Verify the new tests fail before implementation.

### Stage 2 — Minimal implementation

- Implement the feedback route loader and bounded search wrapper.
- Integrate only managed `LocalKnowledgeService.search` calls.
- Run focused tests, Ruff, and mypy.

### Stage 3 — Public contract and release metadata

- Document feedback-aware routing and its privacy/trust boundaries.
- Update the bundled router skill with the internal retry behavior.
- Bump synchronized plugin metadata because runtime behavior changes.

### Stage 4 — Evaluation and review

- Run full pytest, Ruff, mypy, version policy, build/package smoke, and `git diff --check`.
- Compare live lexical metrics, assisted feedback replay, rejected-artifact exposure, and latency with the captured baseline.
- Obtain an independent spec/quality review; fix and re-run affected gates.

### Stage 5 — Delivery

- Commit the reviewed change, push after final verification, refresh editable entrypoint metadata, restart/refresh the live plugin consumer as required, and verify one learned-route query plus one ordinary query through the native tool.

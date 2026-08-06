# Verified Search Feedback, Replay, and Correction Routes — Implementation Plan

## Status

Approved by Alex for checkpointed implementation on 2026-08-06. Implementation proceeds on a local branch; do not push until post-implementation evaluation is reported and Alex approves pushing.

## Primary goal

Improve Hermes Local Knowledge's ability to route a local question to the correct whole artifact by making search-quality telemetry trustworthy enough to:

1. detect real retrieval issues;
2. retain replay-quality evidence for future search/ranking changes; and
3. apply one high-confidence correction learned from explicit, validated feedback when ordinary ranking is noisy.

The implementation must improve the plugin's main function—routing to the artifact the agent should inspect first. Telemetry is supporting infrastructure, not a separate analytics product.

## Rationale and root cause

The current v0.4.2 plugin already has the right broad architecture:

- `usage.sqlite` records lookup inputs, result IDs/types, latency, errors, plugin/config/index provenance, and feedback;
- `knowledge_usage_report` turns that evidence into an improvement queue;
- the evaluator replays positive labels against the unassisted index;
- `routing.py` can promote one artifact learned from explicit useful feedback and can verify it with one typed retry.

Historical evidence is sufficient to identify the broad problem: recall is strong, but top-result selection is weaker. The remaining telemetry weakness is precision of the correction contract:

- one negative feedback row lacks usable query/event linkage and another appears semantically inconsistent with its associated search, even though the current database has no dangling event foreign key;
- `artifact_id` identifies the artifact being judged but does not identify the desired replacement;
- current resolution detection infers resolution from a later positive query rather than storing an explicit relationship;
- search telemetry stores final result IDs but does not say whether feedback routing changed them or what the unassisted baseline was;
- current cross-version evaluation covers unassisted ranking well but does not provide an equivalent production-path lane for feedback routing changes.

The deeper issue is therefore not lack of telemetry volume. It is that current records can say "this was bad" without always proving "this exact current artifact is the accepted replacement." That is enough for human triage but not enough for authoritative rank-one correction.

## Evidence baseline to preserve

Before implementation, recapture and save a private baseline report outside Git using the live v0.4.2 state:

- 1,353 indexed artifacts, including 95 `tool_okf` artifacts;
- 127 total feedback rows;
- 108 search-attributable feedback rows;
- 18 query-linked negative search rows, plus one unlinked negative record;
- 64 positive-label queries / 67 accepted labels in the current evaluator;
- 352 distinct successful historical search operation tuples;
- exact unassisted Hit@1 0.5000, Hit@10 0.9375, and MRR@10 approximately 0.6366 at the time this plan was written;
- current telemetry search p95 approximately 59.475 ms at the time this plan was written.

These numbers are a planning snapshot, not permanent acceptance constants. The implementation baseline must be recaptured immediately before code changes and the exact private inputs/hashes recorded in the implementation checkpoint.

## Comparison with official Hermes Agent

Verified against the locally installed Hermes Agent v0.20.0 (2026.8.3) documentation:

| Official Hermes extension contract | Current/proposed plugin behavior |
| --- | --- |
| Standalone packages may register through `[project.entry-points."hermes_agent.plugins"]`. | Keep the existing `local_knowledge = hermes_local_knowledge.plugin` entry point; no Hermes core patch. |
| Tool handlers accept `args: dict, **kwargs`, return JSON strings, and should convert failures to JSON rather than raise. | Keep all five existing handlers and their JSON contracts; extend only `knowledge_feedback`'s optional parameters. |
| `ctx.register_tool()` is the supported tool surface. | Keep the five existing tools; do not add a new authority-route tool. |
| Plugin hooks are observers and must accept `**kwargs`; `post_tool_call` and `on_session_finalize` are supported. | Keep the two existing OKF hooks unchanged; telemetry/correction work stays in tool/service code and adds no hook. |
| `plugin.yaml` must describe provided tools/hooks; pip metadata and plugin metadata must agree. | Keep the same tool/hook declarations and synchronize the next patch version in `plugin.yaml`, `pyproject.toml`, and `__init__.py`. |

Official Hermes does not provide a built-in local whole-artifact router, replay corpus, or feedback-routing policy. Those remain valid standalone-plugin responsibilities. No proposed functionality requires an undocumented Hermes context field, core patch, gateway event, dashboard API, or external telemetry service.

Documentation references used for viability review:

- `/home/alex/.hermes/hermes-agent/website/docs/developer-guide/plugins/index.md`
- `/home/alex/.hermes/hermes-agent/website/docs/user-guide/features/plugins.md`
- `/home/alex/.hermes/hermes-agent/website/docs/user-guide/features/hooks.md`

## Key KISS design decision

Do **not** add:

- an `authority_route` artifact type;
- source-controlled route JSON/YAML files;
- an authority-route candidate SQLite database;
- a background detector, cron job, or new session hook;
- global `tool_okf` priority;
- LLM-generated routes.

Use the existing feedback and routing mechanism instead:

- a negative feedback row records the rejected result and may optionally identify an expected current artifact;
- a later useful feedback row explicitly links to the negative row and identifies the accepted current artifact;
- that linked correction pair becomes the highest-confidence feedback route;
- the existing deterministic overlap matcher, one-route limit, exact target verification, negative veto, and fail-open behavior remain.

This avoids creating a second policy store and gives one evidence chain:

```text
search event -> negative feedback -> accepted resolution -> routed replay
```

Ordinary useful feedback remains available as the current lower-confidence prior for backward compatibility. Explicitly linked corrections outrank ordinary useful feedback when both match.

## Scope and guardrails

### In scope

- additive `usage.sqlite` schema changes;
- strict event-link validation for new feedback;
- optional expected-target and explicit-resolution fields;
- route provenance and unassisted-result capture for searches;
- higher-confidence routing from explicit correction pairs;
- issue-quality reporting and replay-quality evaluation lanes;
- bounded best-effort redaction only when local telemetry is serialized to an LLM-facing tool result;
- tests, documentation, synchronized versioning, packaging, deployment, and live verification.

### Explicitly out of scope

- embeddings, vector search, rerank models, chunk RAG, or query rewriting;
- automatic creation of missing runbooks or skills;
- automatic route publication from a negative row;
- semantic analysis of feedback notes;
- generalized PII/secret classification, entropy scanning, or recursive scrubbing frameworks;
- storing telemetry outside the configured local state directory;
- Hindsight retention or network telemetry;
- dashboards, alerting, cron, or retention-policy work;
- rewriting existing ranking weights in the same change;
- changing OKF generation, leases, hooks, or privacy projections;
- broad source-root expansion;
- unrelated-query/adversarial corpus generation beyond existing replay plus focused neighboring cases for the corrections being added.

If implementation requires any item above, stop and revise/re-review the plan rather than absorbing it into this scope.

## Data contracts

### `usage_events`: preserve exact replay inputs and distinguish raw from routed output

Add only these search-provenance columns through the existing additive `_ensure_columns` migration pattern:

```text
baseline_top_ids_json  TEXT NOT NULL DEFAULT '[]'
route_feedback_id      INTEGER
route_artifact_id      TEXT
route_outcome          TEXT NOT NULL DEFAULT 'none'
index_jsonl_sha256     TEXT
index_format_version   INTEGER
feedback_max_id        INTEGER DEFAULT -1
```

`top_ids_json` remains the final user-visible order. `baseline_top_ids_json` records the order returned by `search_index` before feedback routing. For new search events, persist the complete returned ID/type order: up to the native tool's existing schema maximum of 30, and up to the caller's explicit `--limit` for the CLI. Do not invent a second telemetry-only truncation that would make a returned CLI artifact unverifiable. Historical rows remain valid with shorter snapshots, and `baseline_top_ids_json` follows the same full returned-page rule. This is required to validate feedback about results below rank five and to replay the actual response page.

Allowed `route_outcome` values:

- `none` — no route selected;
- `already_first` — selected target was already rank one;
- `promoted_existing` — target was present and moved to rank one;
- `promoted_retry` — one typed retry verified and supplied the target;
- `verification_failed` — a route matched but its current target could not be verified, so baseline results were returned unchanged.

Do not store score vectors, artifact bodies, query embeddings, model explanations, or candidates outside the returned bounded page. Exact inputs, baseline/final IDs, target provenance, and current source/index snapshots are sufficient for deterministic behavioral replay.

Expose the `jsonl_sha256` value already stored in index metadata through `index.index_metadata()` and copy it, together with the existing SQLite `PRAGMA user_version`, into each new usage event. These identify which corpus and index format produced an observed order; do not compute a second content hash in the request path. This is the deterministic companion `index.jsonl` content hash, **not** a hash of the SQLite database; never compare rebuilt SQLite file bytes.

`feedback_max_id` records the highest root-scoped feedback ID visible to the same read snapshot used by routing, including when no route matched. Use `-1` for legacy/unbounded rows, `NULL` for a new search where feedback state was unavailable, `0` for a captured empty state, and a positive value for a captured high-water. The additive default must backfill all pre-migration rows to `-1`; rolled-back v0.4.2 writers that omit the column also receive `-1`. This is the one additional event-time boundary needed to prevent later feedback from silently changing a future replay of an earlier search; do not add a second route-state table or serialize feedback bodies into each event.

For new events, preserve the exact query accepted by `knowledge_search` in local SQLite. Do not redact local query, note, path, or artifact ID values. Retain existing practical bounds for non-replay diagnostic strings unless a focused test proves they truncate a supported search input.

### `feedback`: make correction evidence explicit

Add:

```text
expected_artifact_id   TEXT
resolves_feedback_id   INTEGER
linkage_status         TEXT NOT NULL DEFAULT 'legacy'
```

Retain the existing `artifact_id` meaning: the artifact receiving the rating.

New-field semantics:

- `expected_artifact_id` is optional and valid only for a negative search-quality rating. It identifies an already-existing artifact believed to be the better target.
- `resolves_feedback_id` is optional and valid only for `rating="useful"`. It links the accepted `artifact_id` to one earlier negative feedback row.
- `linkage_status` for new writes is `verified_event` when a valid event is used, `direct_query` when explicit query feedback is recorded without an event, `artifact_only` for the existing artifact-rating call with neither event nor query, or `unscoped` for an existing rating-only/note-only call. Existing rows remain `legacy` and are classified during reporting/evaluation without destructive rewriting.

Add indexes only where current query plans need them:

```text
UNIQUE feedback(resolves_feedback_id) WHERE resolves_feedback_id IS NOT NULL
feedback(root, linkage_status, rating, id DESC)
```

Do not enable SQLite foreign-key enforcement or destructively rewrite historical rows. Validate new relationships in the write transaction and keep old readers compatible with the additive columns.

## Feedback write contract

Extend the existing `knowledge_feedback` tool with two optional fields:

```text
expected_artifact_id
resolves_feedback_id
```

Do not add accepted/rejected duplicate fields; `artifact_id` plus rating already expresses which artifact was judged.

### Event-linked feedback validation

When `event_id` is supplied, perform one local transaction that:

1. loads the referenced event;
2. verifies it exists and belongs to the current configured root;
3. verifies its tool is `knowledge_search`, `knowledge_get`, or `knowledge_neighbors`;
4. verifies the referenced lookup event succeeded;
5. for search feedback, copies the event's exact query into the feedback row;
6. if the caller also supplied `query`, requires exact equality after outer-whitespace normalization;
7. when `artifact_id` is supplied for a search rating, requires it to appear in that event's recorded `top_ids_json`; the desired replacement remains separate in `expected_artifact_id` and need not have appeared in the failed result list;
8. records `linkage_status="verified_event"`.

`expected_artifact_id` and `resolves_feedback_id` are search-correction fields. Reject them when the referenced event is `knowledge_get` or `knowledge_neighbors`; feedback on those tools remains supported through the existing rating/artifact/note fields.

If validation fails, the explicit feedback tool returns a JSON error and writes no feedback row. This preserves the existing policy that lookup telemetry fails open but explicit feedback is strict.

Do not attempt to detect semantic disagreement between a free-form note and a query. That would require speculative language processing and would not reliably fix the observed historical mismatch.

### Direct feedback validation

When `event_id` is absent:

- record `linkage_status="direct_query"` when a non-empty query is supplied;
- preserve the existing artifact-only rating call and record it as `linkage_status="artifact_only"` when `artifact_id` is present but event/query are absent;
- preserve currently valid rating-only and note-only calls and record them as `linkage_status="unscoped"` when event/query/artifact are all absent;
- reject `expected_artifact_id` and `resolves_feedback_id` for artifact-only or unscoped feedback because it has no replayable intent;
- keep the current root and caller correlation fields.

Artifact-only and unscoped feedback remain useful as local quality/operator evidence, but they are not eligible for search issue detection, query labels, or correction routes because they have no replayable search intent. Do not tighten the public JSON schema's current `rating`-only minimum in this change.

### Expected-target validation

For `expected_artifact_id`:

- allow only negative ratings (`missing`, `noisy`, `not_useful`, `stale`, `wrong_artifact`);
- require that the artifact currently exists in the managed index;
- treat a negative row without an expected target as an issue requiring coverage/triage, not a route.

If the canonical artifact does not exist, omit `expected_artifact_id`; do not create a placeholder route.

### Resolution validation

For `resolves_feedback_id`:

- require `rating="useful"`, a non-empty `artifact_id`, and a current artifact that exists in the managed index;
- require the new useful feedback itself to link to a successful `knowledge_search` event whose complete recorded result page contains that accepted `artifact_id`;
- require the referenced row to be a negative feedback row for the same root;
- if the negative row has `expected_artifact_id`, require it to equal the accepted `artifact_id`;
- reject references to missing, positive, cross-root, or already explicitly resolved rows;
- write the resolution and its route evidence atomically, with the partial unique index as the final race-safe duplicate guard.

For the `knowledge_feedback` handler's complete write path, use one short `BEGIN IMMEDIATE` transaction to insert the canonical feedback row and its successful `knowledge_feedback` usage event together. Return both IDs internally and preserve the current user-facing feedback ID. If that transaction fails because the database is locked, return one fixed JSON error and **do not open a second connection to record the failure**, because that would double the promised lock-wait budget. Pre-transaction schema/argument validation failures may make one bounded best-effort failed-usage write.

A concise accepted query is still required for target verification. For an explicit correction, keep the two query roles separate: match the user's search against the referenced negative feedback's canonical query, then use the resolving useful feedback's concise query only for the one typed target-verification retry. The route matcher must not infer either query from note prose. Ordinary unlinked useful feedback retains its existing single-query behavior.

## Search execution provenance

Keep `LocalKnowledgeService.search`'s public two-value return shape. Put an internal routing trace inside the fresh metadata dictionary and have `_handle_search` remove it before returning index metadata to the model.

Change `apply_feedback_route` to return a small typed result containing:

- final rows;
- outcome enum;
- matched feedback ID;
- target artifact ID.
- root-scoped feedback high-water ID from the same route-read snapshot.

Own this state in `routing.py`, not in the handler. Add a typed `RouteDecision` returned for every lookup, including no match. In one SQLite read transaction, capture `MAX(id)` for all feedback rows in the current root, then apply routing. Return `0` for an empty root, `NULL` for unavailable state, and the positive max otherwise. The service and handler only propagate this decision.

The handler records baseline IDs and route provenance in the same `knowledge_search` usage event as the final IDs. It must not emit a second telemetry event for the typed retry.

Telemetry recording remains fail-open: if provenance cannot be persisted, search results still return normally.

## Issue detection and report behavior

Extend `knowledge_usage_report` rather than adding a detector process.

Add compact sections/counts for:

- feedback linkage quality: `verified_event`, `direct_query`, `artifact_only`, `unscoped`, `legacy`, orphaned event, root mismatch;
- unresolved verified/direct negative feedback;
- unresolved negatives with a current expected target;
- unresolved negatives without a current expected target;
- explicit resolutions and their accepted targets;
- route outcomes and verification failures;
- replay-ready label counts by quality tier.

Candidate rules:

1. Exclude test roots, stale-root rows, probe queries, tool errors, orphaned links, and root-mismatched links from the active work queue.
2. Re-run the current search before presenting an unresolved negative as a correction candidate. If the accepted/expected artifact is already rank one, report it as behaviorally resolved rather than asking for a route.
3. A verified negative with an existing expected target may be presented as a correction candidate.
4. A verified negative without an expected target remains a coverage/ranking triage item.
5. Never infer a target from note prose or adjacent artifacts.

Keep the current report `days` and `limit` bounds. Do not scan a new arbitrary historical window in the request path; use indexed SQL over the requested time window and the current bounded report limit.

Perform the bounded current-state check in `LocalKnowledgeService.usage_report`, after `_usage_report` has selected candidates. Use the existing unassisted `_search_index_fn` against the current managed `index.sqlite`, with no rebuild and no telemetry write. If the current index is missing, belongs to another source root, or cannot be read, retain the candidate with `current_replay_status="unavailable"`; do not guess that it is resolved. This keeps index access out of `telemetry.py` and avoids circularly validating a candidate through the feedback route it may create.

### LLM boundary privacy

`usage.sqlite`, private evaluator snapshots, and local comparison output retain exact local values, including secrets if they occur. Do not scrub local storage.

The native `knowledge_usage_report` tool is LLM-facing. Keep `_usage_report` and any local CLI/evaluator consumer exact. In `_handle_usage_report`, immediately before JSON serialization to the model, apply one small best-effort helper that masks only obvious credential assignments and URL userinfo. Guardrails:

- preserve the original local value;
- no entropy heuristics, recursive object walker, PII recognizer, or external dependency;
- keep the helper and its tests materially smaller than the telemetry/report implementation;
- if redaction would destroy issue identity, expose a stable local row/event ID so the exact record can be inspected locally without sending it to the model.

## Replay and evaluation design

### Keep the existing unassisted lane

`evaluation.py` continues to evaluate `search_index` without feedback routing. This remains the primary guard against training/evaluation leakage and ranking regressions.

Classify labels into:

- `explicit_resolution` — useful row linked through `resolves_feedback_id`;
- `verified_event` — structurally valid useful event-linked row;
- `direct_or_legacy` — useful query-bearing direct/legacy row that remains informative but weaker. Artifact-only and unscoped rows remain reportable but cannot become query labels or routes.

Report aggregate metrics for all valid current labels and separate metrics/counts for the higher-confidence tiers. Do not discard the historical corpus merely because it predates the new schema.

### Add one production-path replay lane

Extend the existing private historical comparison harness rather than creating a second evaluator:

- use the frozen source/config/OKF/usage snapshot already created by `compare_historical_query_versions.py`;
- run managed `LocalKnowledgeService.search(..., ensure=False)` for historical search cases so the selected code version exercises its real feedback route path without emitting new telemetry;
- record production final IDs and route provenance separately from raw-index IDs;
- preserve raw-index replay as an independent lane;
- keep private snapshot and report files mode `0600` and out of Git.

The production lane must use the same frozen usage database for baseline and candidate refs. It must not read the live database after snapshot creation. For new events with `feedback_max_id >= 0`, both refs must apply only feedback at or below that boundary. New events with `feedback_max_id IS NULL` replay with feedback routing unavailable and unassisted. `-1` legacy events are explicitly labeled **fixed-capture-policy replay**: they evaluate both refs against the same complete frozen feedback snapshot but are not represented as exact event-time route reproduction.

Do not add a replay-only boundary parameter to `LocalKnowledgeService`. For each distinct sentinel/bound, derive a ref-local working usage DB: delete root-scoped feedback above a nonnegative bound, make feedback unavailable for `NULL`, and retain the full snapshot for `-1`. Group cases by bound and reuse each DB. This lets unmodified v0.4.2 and candidate refs receive identical evidence.

Because routing scopes feedback by absolute source root, derive each ref-local working database from that one frozen snapshot and remap only the captured live root to the selected ref's frozen source-root path in one transaction. Never alter the canonical snapshot. Verify row counts and event/feedback links after remapping, and compare canonicalized rows with ref-local root prefixes removed so baseline and candidate are proven to contain the same logical evidence.

### Replay corpus contents

For each replayed search, preserve/use:

- exact query;
- requested limit;
- artifact-type filter;
- rebuild flag as historical evidence, while evaluation uses the frozen already-built index and remains read-only;
- plugin/config/index provenance;
- index JSONL fingerprint and index format version;
- baseline top IDs when present;
- final top IDs;
- route feedback ID, target ID, and outcome when present.
- feedback high-water ID when present.

Do not add raw artifact contents to telemetry. The frozen source snapshot already supplies the content required to rebuild and compare versions.

Do not deduplicate events merely because query, limit, and type filter match. Events with different rebuild semantics, index fingerprints, or route outcomes remain distinct replay cases. Exact observed-order comparison is authoritative only when the frozen replay corpus fingerprint equals the event fingerprint; otherwise report rank/output drift as diagnostic evidence rather than claiming event-time reproduction.

### Acceptance oracles

A candidate change is acceptable only when:

1. unassisted exact and parent-equivalent Hit@1/3/5/10 and MRR do not regress against the immediately captured baseline;
2. higher-confidence label metrics do not regress;
3. every explicit correction case places its accepted current target at rank one in the production lane;
4. a rejected query/artifact pair does not gain top-rank exposure;
5. historical production result-order changes are limited to queries matched by explicit correction evidence; unrelated replay cases remain unchanged;
6. no new search errors, empty-result regressions, get changes, or neighbor changes appear;
7. route verification failures return the unassisted list unchanged;
8. the candidate does not materially worsen measured p95 search latency across the existing three-run comparison method; compare measured distributions rather than inventing a new fixed cutoff.

Do not evaluate a newly created correction only on the exact query used to create it. Add at least one related held-out historical/paraphrased query when available and a small set of adjacent nonmatching cases derived from the same domain. Do not generate a broad synthetic adversarial subsystem.

## Timeout and bounded-work policy

- Preserve the existing feedback-route read-only SQLite busy timeout of 100 ms. It is already covered by a lock-contention test requiring fail-open behavior in under one second and has operated with the current approximately 59 ms historical search p95.
- Replace the general telemetry connection's current 10-second SQLite lock wait with a **1-second per-handler lock-wait budget**. This is not inferred from search p95. Stage 0 found a maximum of five live events sharing one timestamp second, then ran three five-thread bursts against private database copies using the real `_record_usage` and `_record_feedback` paths. All 15 writes succeeded, but worst handler durations were 510.486 ms, 527.321 ms, and 519.334 ms. That evidence rejects the original 100 ms proposal. One second is the smallest conventional whole-second budget that leaves useful margin above the measured 527.321 ms worst case while reducing the current 10-second synchronous stall by 90%. Audit every success and exception path so one handler cannot incur two sequential busy waits; in particular, locked `knowledge_feedback` must not attempt follow-up failure telemetry.
- Re-run the same three-burst real-path benchmark after implementation. It must again produce zero dropped writes/errors and all durations must remain below 750 ms, preserving at least 250 ms of observed headroom inside the one-second budget. An intentionally held lock beyond one second must yield the accepted behavior: lookup result returned with telemetry dropped, or one strict `knowledge_feedback` JSON error without follow-up telemetry. Keep the benchmark output private and record only its aggregate method and distribution.
- Do not increase the route lookup's `FEEDBACK_SCAN_LIMIT`, add retries around SQLite, or add sleeps.
- Preserve the one existing typed retry and `RETRY_LIMIT=10`; do not add multi-attempt search loops.
- Keep existing historical comparison subprocess behavior rather than inventing an unsupported timeout for source builds that can vary by machine and cold cache.
- OKF generation timeouts and leases are outside this plan and remain unchanged.

If implementation discovers a need for a new timeout, stop and obtain either an official Hermes contract or measured historical duration distribution before choosing it; amend and re-review the plan first.

## Implementation stages

Each stage follows: implement -> focused verification -> independent spec/quality review -> fix/re-review -> checkpoint commit. Do not start the next stage with unresolved blocking findings.

### Stage 0 — Freeze contracts and baseline

**Files read/commands only; no runtime changes.**

1. Confirm clean worktree and current synchronized version.
2. Capture `knowledge_usage_report(days=365, limit=50)` to a private `0600` file outside Git.
3. Capture detailed unassisted evaluation JSON.
4. Run the existing historical comparison harness against current HEAD as a self-comparison/smoke and preserve the private report.
5. Record hashes of the usage DB snapshot, source snapshot manifest, current commit, Hermes version, Python version, and resolved plugin config sources.
6. Verify the known unlinked/questionable feedback rows are visible as data-quality caveats; do not edit historical rows.
7. Derive the conservative same-second event burst and measure copied-database transaction/lock behavior required by the timeout policy; confirm or reject the initial 100 ms proposal before code changes.

**Gate:** baseline is reproducible and no current test/build failure is misattributed to later work.

### Stage 1 — Additive schema and strict feedback contract

**Primary files:**

- `hermes_local_knowledge/telemetry.py`
- `hermes_local_knowledge/index.py`
- `hermes_local_knowledge/service.py`
- `hermes_local_knowledge/plugin.py`
- `hermes_local_knowledge/cli.py`
- `tests/test_service.py`
- `tests/test_plugin.py`
- new focused telemetry tests only if existing files become unwieldy

Tasks:

1. Add the ten additive columns and two justified indexes described above.
2. Expose the already-stored index JSONL fingerprint and format version through index metadata and copy them into usage events.
3. Replace the general telemetry connection's 10-second lock wait with the evidence-backed 1-second budget while preserving lookup fail-open and feedback-strict behavior.
4. Make native-tool and CLI search telemetry persist the complete bounded returned ID/type page so new event-linked feedback can validate any returned artifact, not only ranks one through five.
5. Extend `_record_feedback` to validate event links and resolution links and append the successful feedback-tool usage event in the same short `BEGIN IMMEDIATE` transaction.
6. Keep new local query values exact; do not add local secret scrubbing.
7. Extend service validation for current artifact existence without coupling telemetry SQL to index internals.
8. Add the two optional JSON-schema properties to `knowledge_feedback`; keep `additionalProperties: false` and existing calls valid.
9. Return actionable JSON errors for invalid event, root, rating/field combination, missing target, or conflicting resolution.
10. Add migration tests from the exact current schema and reopen with v0.4.2-compatible readers to prove additive rollback compatibility.

Focused tests:

- valid event-linked feedback canonicalizes the event query;
- missing/cross-root/non-lookup/failed event is rejected;
- supplied query conflict is rejected;
- a judged artifact that was not returned by the referenced search is rejected;
- a valid artifact at the maximum returned rank can be linked because the full bounded page was stored;
- direct-query, artifact-only, rating-only, and note-only feedback remain supported; artifact-only/unscoped rows are excluded from correction/replay eligibility;
- expected target rules by rating;
- explicit resolution rules, target existence, expected-target agreement, and partial-unique duplicate resolution rejection;
- explicit feedback remains strict while lookup telemetry remains fail-open;
- a locked feedback transaction performs no second failure-telemetry write and the complete handler returns within the single bounded wait;
- existing v0.4.2 database opens and migrates without row loss, all pre-migration rows receive `feedback_max_id=-1`, and a rolled-back v0.4.2 writer also receives the `-1` default;
- secrets and full local values remain intact in SQLite;
- an exclusive usage-database lock cannot delay search or feedback for multiple seconds.

**Gate:** focused tests, Ruff, and mypy pass; independent review confirms the schema is additive, semantics are unambiguous, and no existing tool call breaks.

### Stage 2 — Search provenance and report-quality issue detection

**Primary files:**

- `hermes_local_knowledge/routing.py`
- `hermes_local_knowledge/service.py`
- `hermes_local_knowledge/plugin.py`
- `hermes_local_knowledge/cli.py`
- `hermes_local_knowledge/telemetry.py`
- `tests/test_routing.py`
- `tests/test_service.py`
- `tests/test_plugin.py`

Tasks:

1. Return a typed route application outcome without changing the user-visible search payload.
2. Persist baseline/final result IDs, index fingerprint/format, feedback high-water ID, and route provenance in one usage event.
   - Update both the native tool handler and standalone CLI telemetry path so they retain parity.
   - Reuse the complete bounded final page introduced in Stage 1 and add the corresponding unassisted baseline page.
3. Extend report linkage classifications and explicit-resolution sections.
4. Replace heuristic resolution with explicit resolution when present; retain and label the old exact-query heuristic only for legacy rows.
5. Re-run current search for the bounded active negative rows before calling them correction candidates.
6. Add the compact LLM-boundary redactor and keep exact SQLite values untouched.

Focused tests:

- each route outcome is recorded correctly;
- route/no-route searches record `0` or a positive root-scoped high-water from the same read snapshot, while an unavailable feedback database records `NULL` and a migrated legacy row remains `-1`;
- no-route lookup still returns a typed decision, and feedback rows irrelevant to today's matcher still advance the root-scoped high-water;
- no-route searches record identical baseline/final IDs;
- failed verification records provenance and returns byte-for-byte-equivalent baseline order;
- report excludes probes/test/stale-root/orphaned rows from the active queue;
- explicit and legacy resolution are distinguished;
- current rank-one target is behaviorally resolved;
- obvious credential assignments and URL userinfo are masked in tool output but unchanged in SQLite;
- non-secret diagnostic text remains useful.

**Gate:** the live report can account for every feedback row by linkage and resolution category, and one malformed row cannot become a correction candidate.

### Stage 3 — High-quality raw and production replay

**Primary files:**

- `hermes_local_knowledge/evaluation.py`
- `hermes_local_knowledge/cli.py`
- `scripts/evaluate_ref.py`
- `scripts/compare_historical_query_versions.py`
- `tests/test_evaluation.py`
- `tests/test_evaluate_ref.py`
- `tests/test_historical_compare.py`

Tasks:

1. Add quality-tiered label loading while preserving existing aggregate outputs for compatibility.
2. Add route provenance to detailed evaluation output without making private queries public.
3. Add the read-only production-path replay lane using the selected ref's `LocalKnowledgeService.search(..., ensure=False)`.
4. Ensure baseline and candidate refs receive immutable copies of the same usage snapshot.
5. Build and reuse ref-local working usage databases for each `0`/positive bound, `NULL` unavailable state, and `-1` fixed-capture state so unmodified v0.4.2 and candidate services receive identical evidence.
6. Keep current raw-index, get, neighbors, synthetic contract, structure, and latency checks.
7. Permit intended production-order changes only for explicit correction cases; reject unrelated changes.
8. Keep comparison reports and snapshots private and out of Git.

Focused tests:

- quality-tier classification and legacy compatibility;
- explicit resolution target contributes the correct accepted label;
- orphaned/root-mismatched rows cannot enter high-confidence labels;
- production lane applies routing but writes no usage rows;
- later feedback cannot affect a bounded `0`/positive event replay; new `NULL` cases remain unassisted; `-1` legacy cases are never reported as event-time exact;
- the unchanged v0.4.2 baseline also excludes post-bound feedback via the prepared working DB, proving no replay-only service API is required;
- exact corpus matching compares canonical companion JSONL hashes and never SQLite file hashes;
- frozen usage DB hash is unchanged after evaluation;
- ref-local root remapping changes only the expected root prefix and preserves row/link counts;
- baseline and candidate use identical evidence;
- intended correction change is accepted while unrelated order change is rejected;
- raw ranking lane remains independent of feedback routing;
- old refs lacking new columns receive defaults rather than crashing.

**Gate:** self-comparison is stable, v0.4.2-to-candidate comparison is executable, and private output proves both raw and routed behavior from one frozen corpus.

### Stage 4 — Prioritize explicit correction routes

**Primary files:**

- `hermes_local_knowledge/routing.py`
- `tests/test_routing.py`
- `tests/test_service.py`
- historical evaluation fixtures only where necessary

Tasks:

1. Load explicitly resolved useful rows as the highest-confidence route source.
2. For explicit corrections, join the referenced negative row and keep `trigger_query` (negative) separate from `verification_query` (resolving useful row); match only on the trigger and use only the verification query for the typed retry.
3. Keep ordinary useful feedback as the existing lower-confidence single-query fallback.
4. Preserve exact-match precedence, strong-overlap constraints, quote behavior, artifact-type compatibility, latest-negative veto, one-route maximum, one typed retry, target verification, and 100 ms fail-open lookup.
5. Use existing matcher constants; do not add manual required/excluded term DSL in this change.
6. Build evaluator fixtures from one or two real historical correction families only after independently verifying each current target. Keep these fixtures in the private frozen evaluation copy or as sanitized deterministic test fixtures; do not mutate live feedback merely to make the evaluation pass. Do not fabricate routes for missing artifacts.

Focused tests:

- explicit correction outranks a competing ordinary useful route;
- explicit correction activation uses the negative trigger query while typed verification uses the distinct accepted query;
- newer negative feedback suppresses the explicit route;
- target deletion/staleness fails open;
- route cannot cross source roots or artifact-type filters;
- related held-out query improves;
- adjacent domain queries do not activate the correction;
- all existing feedback-routing tests remain green.

Historical gate:

- run unassisted and production replay for at least three runs;
- require all acceptance oracles above;
- inspect every changed production case, not just aggregate metrics;
- reject blanket OKF promotion or any unexplained unrelated change.

**Gate:** explicit correction improves intended top-one routing with no raw-ranking regression and no unrelated production replay changes.

### Stage 5 — Documentation, public contract, and package verification

**Primary files:**

- `README.md`
- bundled local-knowledge router skill and telemetry reference
- `plugin.yaml`
- `pyproject.toml`
- `hermes_local_knowledge/__init__.py`
- public-contract/install-smoke/version-policy tests

Tasks:

1. Document the feedback field semantics and correction procedure:
   - record negative feedback;
   - inspect/verify the canonical target;
   - run a concise `knowledge_search` that returns the target and record useful feedback against that event with `resolves_feedback_id`;
   - replay before relying on the correction.
2. Document local-data privacy versus LLM-facing report redaction.
3. Document raw versus production replay lanes and label-quality tiers.
4. State that correction routing does not create missing artifacts and that tool OKFs remain non-authoritative hints.
5. Update synchronized metadata to the next unreleased patch version.
6. Run packaging and editable-entrypoint checks required by `CONTRIBUTING.md`.

Verification commands:

```bash
python -m pytest -q
python -m ruff check .
python -m mypy
python scripts/check_version_policy.py --base-ref origin/main
git diff --check
python -m build
```

Also run the repository's install-smoke/public-contract tests and the historical comparison commands established in Stage 0.

**Gate:** full suite, lint, typing, version policy, package build/install smoke, historical replay, and independent final review all pass.

### Stage 6 — Delivery and live verification

1. Review the complete diff for scope and generated/private-file exclusion.
2. Commit the reviewed implementation; push only after final verification.
3. Refresh editable entry-point metadata:

```bash
python -m pip install -e /home/alex/repos/hermes-local-knowledge
```

4. Confirm `hermes plugins list --plain --no-bundled` reports the new synchronized version.
5. Restart or refresh the long-running Hermes gateway using the documented local operational procedure so it loads the new entry point.
6. In a fresh native plugin manager/session, verify:
   - all five existing tools register and no extra hook/tool appears;
   - the additive `usage_events`/`feedback` columns and both planned feedback indexes exist before assessing assisted routing; a pre-migration read must fail open, and the migration smoke must make subsequent reads assisted;
   - one ordinary search records identical baseline/final IDs;
   - one known resolved correction records its route provenance and returns the verified target first;
   - one unrelated search remains unchanged;
   - `knowledge_usage_report` shows linkage/resolution/replay counts and does not expose an obvious credential fixture;
   - live `usage.sqlite` remains readable and the current index is not rebuilt unnecessarily.
7. Re-run the live detailed evaluator and compare with the Stage 0 private baseline.

## Review requirements

At each stage, the independent reviewer receives:

- this plan and named stage acceptance criteria;
- the exact diff since the prior checkpoint;
- focused and full verification output as applicable;
- private evaluation summary with queries represented by stable IDs when sharing exact text is unnecessary;
- unresolved findings from earlier reviews.

Review must separately cover:

1. **Specification:** every implemented behavior is required by this plan and every required behavior exists.
2. **Quality:** schema migration, strict/fail-open boundaries, query/root scoping, replay immutability, route verification, and rollback compatibility.
3. **KISS/scope:** no parallel route store, detector service, broad scrubber, new hook, global OKF priority, or unrelated ranking change entered the diff.
4. **Operational viability:** official Hermes tool/handler/plugin contracts remain satisfied and package metadata/deployment is complete.

Blocking findings must be fixed and re-reviewed before proceeding. Advisory ideas not required for the primary goal are deferred rather than absorbed.

## Rollback

The migration is additive. A rollback to v0.4.2 should ignore unknown columns and continue using existing `usage_events` and `feedback` fields.

Rollback procedure:

1. restore/reinstall the previously verified plugin commit/package;
2. restart/refresh the gateway;
3. verify the five tools and an ordinary search;
4. leave additive columns and new rows in place unless a proven compatibility issue requires a backed-up database restore.

Do not delete or rewrite telemetry as a routine rollback step. Preserve the local database and private baseline artifacts for diagnosis.

## Final acceptance criteria

The implementation is complete only when all are true:

- new feedback cannot silently bind to a missing, cross-root, or conflicting event;
- an explicit correction identifies both the rejected feedback and the accepted current artifact;
- local telemetry retains exact replay evidence without unnecessary secret scrubbing;
- LLM-facing report text receives small best-effort credential masking;
- issue detection separates routing candidates, missing coverage, malformed evidence, probes/tests, and runtime errors;
- every managed search can distinguish unassisted and final result order plus route provenance;
- frozen raw and production replay run against identical private evidence without writes;
- explicit correction routes improve intended top-one cases and do not alter unrelated historical cases;
- unassisted ranking metrics and rejected-artifact exposure do not regress;
- no new tool, hook, background worker, route file format, model call, dependency, or Hermes core patch is introduced;
- full tests, Ruff, mypy, version policy, package build/install smoke, historical replay, live native-tool verification, and independent final review pass.

# Implicit-Feedback Replay High-Water Implementation Plan

> **For Hermes:** Execute this plan with the simplest approach that fits its size and risk; use delegation only when it materially improves independent review.

**Goal:** Make same-turn implicit-feedback routing deterministically replayable at its original telemetry boundary without exposing future or malformed implicit evidence.

**Architecture:** Extend the existing production-search telemetry and private replay-state materialization. Persist the explicit and implicit root-scoped high-water marks plus the three effective implicit-routing settings. During replay, restore those settings, prune only the root-scoped state actually consulted, validate every relevant retained implicit row against its exact successful same-turn baseline search event, and classify replay as exact only when all applicable inputs and outputs match.

**Tech Stack:** Python standard library, SQLite, pytest, Ruff, mypy.

---

## Scope and acceptance contract

- New managed-search telemetry records `implicit_feedback_max_id`, `implicit_feedback_enabled`, `implicit_min_confirmations`, and `implicit_max_generic_queries` additively.
- Implicit rows used by replay retain an exact `search_event_id` and non-empty `turn_id`; their linked search event must match root, session, task, turn, query, successful tool status, and baseline artifact membership.
- Later implicit rows cannot affect an earlier enabled replay.
- Disabled searches replay with implicit routing disabled and do not depend on an implicit high-water or ambient implicit rows.
- Legacy/partial rows remain non-exact; replay never repairs or infers missing provenance.
- Explicit route precedence, rejection vetoes, route-assisted-only exclusion, and fail-open telemetry remain unchanged.
- No Hermes-core changes, public tool API changes, dependencies, threshold changes, or standalone replay subsystem.

## Task 1: Focused RED contracts

1. Routing retains both high-water marks when implicit routing is enabled and preserves explicit precedence/rejection vetoes.
2. Telemetry migration/writes persist the nullable implicit boundary, settings, and implicit-row turn ID while old writers remain compatible.
3. Replay state excludes rows above the enabled boundary, preserves unrelated roots, and restores recorded settings.
4. Disabled telemetry with no implicit boundary can replay exactly; unknown legacy enabled state cannot.
5. Corrupt implicit provenance—missing event, wrong root/session/task/turn/query, final-only artifact, malformed baseline JSON—makes the implicit boundary unavailable and replay non-exact.
6. A successful plugin search remains successful when the new telemetry writer/migration path raises.

## Task 2: Runtime provenance

1. Read explicit and implicit root-scoped snapshots within one SQLite read transaction.
2. Preserve explicit-route precedence and rejection vetoes.
3. Persist both bounds and all three effective settings best-effort.
4. Add nullable `implicit_feedback.turn_id`; write the exact host turn used for same-turn attribution.
5. Keep legacy `NULL` values untrusted.

## Task 3: Historical materialization

1. Load all four usage-event fields through legacy-safe SQL extraction.
2. Key enabled replay states by explicit bound, implicit bound, and effective settings.
3. Key disabled states by explicit bound and disabled state only; do not require or materialize an implicit boundary.
4. Rewrite and prune exact roots only; descendant and unrelated roots remain independent.
5. For enabled states, validate every retained row in the production scan window against a successful linked baseline search with matching root/session/task/turn/query and artifact membership.
6. If validation fails, mark the implicit boundary unavailable and fail closed rather than reconstructing evidence.
7. Pass both applicability/availability and recorded settings to the evaluator.

## Task 4: Evaluation exactness

1. Instantiate each replay service with the event’s recorded implicit settings.
2. Require explicit-bound availability and matching corpus/plugin/index/output/route provenance.
3. Require implicit-bound availability and matching implicit high-water only when recorded implicit routing was enabled.
4. Require recorded disabled state—but no implicit boundary—when it was disabled.
5. Keep existing `feedback_bound_kind` reporting taxonomy backward-compatible and expose implicit applicability/availability separately.

## Task 5: Verification and delivery

1. Run focused and complete pytest suites.
2. Run Ruff, mypy, version policy, and `git diff --check`.
3. Run managed build/evaluation/doctor smokes where configured.
4. Build and Twine-check wheel/sdist; fresh-install and load the plugin entry point.
5. Inspect the complete diff and verify Hermes core remains untouched.
6. Freeze the candidate, obtain independent runtime and data-contract reviews, address findings, rerun gates, and commit locally without pushing.

## Risks and mitigations

- **Cross-stream race:** one read transaction captures the route decision and applicable root-scoped high-water marks.
- **Malformed telemetry:** exactness requires validation; invalid rows are never repaired or silently ignored as trusted evidence.
- **Disabled-state overconstraint:** implicit state is not a ranking dependency when disabled.
- **Legacy ambiguity:** nullable fields remain unavailable rather than defaulting to zero or current configuration.
- **Root leakage:** all high-water validation, rewriting, and pruning uses exact root equality.
- **Operational impact:** telemetry writes remain best-effort and cannot change successful tool output.

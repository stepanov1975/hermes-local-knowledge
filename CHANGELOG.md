# Changelog

All notable changes to this project are documented in this file.

## [0.4.12] - 2026-08-22

### Added

- Expanded artifact-type intent with query-terminal, single-noun promotion for runbooks, skills, docs/references, and memory documents. At most one fully matching family moves forward, only when the exact target carries a complete configured entity label in its own identity fields and another term supplies the operation/topic. Eligibility is resolved across one complete index snapshot and applied as a stable move; body-only, sibling-derived, ambiguous, topic-only, mixed, underspecified, and conversational noun uses retain baseline ranking exactly. Added a lower-bound implicit consumed-rank diagnostic based on valid linked unassisted search baselines.

### Changed

- Made the bounded discovery hint direct local Hermes/homelab questions to `knowledge_search` before broad file search while reminding callers to verify live state.

[0.4.12]: https://github.com/stepanov1975/hermes-local-knowledge/compare/v0.4.11...v0.4.12

## [0.4.11] - 2026-08-21

### Fixed

- Preferred Hermes' context-local active profile over the process-wide `HERMES_HOME` fallback, keeping profile configuration, indexes, usage telemetry, and learned feedback isolated when multiple profile managers coexist in one process.

[0.4.11]: https://github.com/stepanov1975/hermes-local-knowledge/compare/v0.4.10...v0.4.11

## [0.4.10] - 2026-08-21

### Added

- Added a current-version live native-search quality cohort and explicit operational/history cohorts to `knowledge_usage_report`, keeping aggregate operational totals while separating probes, hourly doctor runs, other CLI/native activity, and historical searches; implicit feedback is also broken down by consuming tool.
- Added strict same-turn implicit-consumption adapters for successful later-request `skill_view` and `read_file` calls whose canonical source path resolves to exactly one caller-visible baseline artifact in the same current index snapshot; same-request parallel calls remain ineligible.

[0.4.10]: https://github.com/stepanov1975/hermes-local-knowledge/compare/v0.4.9...v0.4.10

## [0.4.9] - 2026-08-17

### Changed

- Registered the static `knowledge_search` discovery hint as a bounded, cache-safe system-prompt section on supported Hermes hosts, while retaining the deduplicated `pre_llm_call` injection as a backward-compatible fallback.
- Kept the `pre_llm_call` hook dedicated to implicit-feedback context binding when system-prompt sections are available, so the static hint survives compression and remains present for multimodal turns without repeated user-message injection.

[0.4.9]: https://github.com/stepanov1975/hermes-local-knowledge/compare/v0.4.8...v0.4.9

## [0.4.8] - 2026-08-17

### Added

- Added a concise, availability-gated `pre_llm_call` hint that tells the model to use `knowledge_search` for local tools and information while preserving the existing implicit-feedback turn binding and avoiding duplicate hints in conversation history.

### Fixed

- Tightened the shared tool and hint availability check so configured source and Hermes-home paths must be directories rather than merely existing paths.

[0.4.8]: https://github.com/stepanov1975/hermes-local-knowledge/compare/v0.4.7...v0.4.8

## [0.4.7] - 2026-08-14

### Changed

- Consolidated replay state-key ownership in frozen cases and shared persisted implicit-evidence validation between live routing and historical replay.
- Reused immutable replay index files with hard links, with a private-copy fallback on filesystems without hard-link support.

### Fixed

- Kept unrelated and malformed post-tool hook calls inside the fail-open boundary without resolving implicit-feedback configuration.
- Centralized root-scoped implicit-feedback high-water/schema handling so routing records one consistent replay boundary.

[0.4.7]: https://github.com/stepanov1975/hermes-local-knowledge/compare/v0.4.6...v0.4.7

## [0.4.6] - 2026-08-14

### Added

- Added opt-in implicit routing feedback when an artifact is consumed from a recent same-turn baseline search. Confirmations are deduplicated per search event, require distinct searches, and stop promoting artifacts mature across too many query shapes; route-assisted-only results do not create evidence, explicit feedback remains authoritative, and implicit evidence stays outside evaluation labels. The idea originated with [@xXLODXx](https://github.com/xXLODXx)'s proposal and initial implementation in [PR #27](https://github.com/stepanov1975/hermes-local-knowledge/pull/27), including its confirmation and generic-artifact gate design.
- Added deterministic historical replay for implicit-routing state. Managed search telemetry records a root-scoped implicit high-water and effective settings; exact replay requires bounded same-turn baseline provenance to validate, while legacy or incomplete rows remain non-exact and disabled searches do not depend on unused implicit state.

[0.4.6]: https://github.com/stepanov1975/hermes-local-knowledge/compare/v0.4.5...v0.4.6

## [0.4.5] - 2026-08-07

### Changed

- Expanded the supported Ruff test-tool range through 0.16 while explicitly preserving the pre-0.16 default lint rule set.

[0.4.5]: https://github.com/stepanov1975/hermes-local-knowledge/compare/v0.4.4...v0.4.5

## [0.4.4] - 2026-08-07

### Added

- Added `local_knowledge.router_skill_path` for operators who intentionally deploy a customized proactive router skill instead of the bundled copy. Relative paths resolve from the active Hermes home.

### Changed

- The doctor validates an explicitly configured custom skill's enrolled runtime path and `local-knowledge-router` frontmatter identity without requiring byte equality with the bundled skill.
- `install-router-skill` now treats a valid configured custom skill as authoritative and skips bundled installation even with `--force`.

### Fixed

- Release automation now renders the complete version-specific changelog notes into GitHub releases, detects and repairs missing or changed notes without rebuilding complete assets, and verifies the final body on every run.

[0.4.4]: https://github.com/stepanov1975/hermes-local-knowledge/compare/v0.4.3...v0.4.4

## [0.4.3] - 2026-08-07

### Added

- Added provenance-rich search telemetry that records the complete bounded result page, the unassisted baseline page, feedback-route outcome, root-scoped feedback high-water, index hash and format, plugin version, and resolved execution context needed for replay.
- Added verified event-linked feedback and explicit correction resolutions. Feedback links now validate the source root, successful tool call, query, returned artifact, expected target, parent/child relationship, and unique accepted resolution in one transaction.
- Added a private, read-only historical comparison harness that freezes telemetry and source/runtime/OKF inputs, builds each Git ref independently, and replays both raw index operations and managed production searches without mutating live usage data.
- Added quality-tier metrics and provenance for explicit resolutions, verified events, and direct or legacy feedback, together with per-case replay evidence summaries.

### Changed

- Extended the usage schema additively while retaining migrated rows as fixed-capture diagnostic evidence instead of overstating them as exact historical observations.
- Classified replay evidence as event-time reproduction, exact-input counterfactual, fixed-capture legacy, unavailable, mismatched, or errored. Exact claims now require reconstructable root-scoped feedback state, matching corpus and execution identity, and reproduction of recorded output and route provenance.
- Historical acceptance now distinguishes rejection, supported improvement, and unchanged or insufficient evidence. Direct or legacy-only labels cannot by themselves prove an improvement.
- Kept evaluation leakage-free by measuring unassisted ranking while evaluating feedback-assisted production behavior in a separate replay lane.

### Fixed

- Preserved quality-only queries and their outcomes through case materialization, per-ref execution, metric calculation, and private diagnostics.
- Corrected explicit-resolution handling for omitted parent targets, conflicting declared targets, duplicate resolution edges, empty legacy queries, and root-mismatched or malformed feedback.
- Made replay-bound validation root-scoped and propagated preparation-time availability so missing historical feedback cannot be classified as exact merely because another root owns the same numeric ID.
- Aligned the correction-route acceptance oracle with production query matching, quote handling, scoring, artifact-type eligibility, and later query-wide or target-specific vetoes.

[0.4.3]: https://github.com/stepanov1975/hermes-local-knowledge/compare/v0.4.2...v0.4.3

## [0.4.2] - 2026-08-05

### Added

- Added a bounded deterministic search prior learned only from explicit useful feedback for the configured live source root. A matching accepted artifact is promoted only when it still appears in current index results.
- Added one concise, artifact-type-scoped retry when an accepted route is absent from the initial results; the remembered artifact must be rediscovered by that live retry before promotion.

### Changed

- Kept read-only evaluation on the unassisted index ranking so feedback labels do not train and score the same replay.

### Fixed

- Made newer matching rejections veto older overlap routes and prevented a retry from expanding to a longer accepted query.
- Enforced retry length using non-deduplicated query tokens so repeated words cannot bypass the no-longer-than-current contract.
- Mapped artifact-ID prefixes such as `mcp:` and `cron:` to their actual artifact types before the typed verification retry.
- Scoped feedback assistance to the source root stored in the current index, encoded read-only feedback database URIs, and bounded locked-database fallback with a root/order lookup index and short timeout.

[0.4.2]: https://github.com/stepanov1975/hermes-local-knowledge/compare/v0.4.0...v0.4.2

## [0.4.0] - 2026-07-27

### Changed

- Consolidated configuration, artifact collection, format-4 persistence/search, telemetry, evaluation, service orchestration, OKF generation, plugin registration, and CLI behavior into their final owner modules, with `indexer` retained as a thin eight-export compatibility facade.
- Advanced the persisted index to format 4, retained `index_build.lock` as the v0.3.12-compatible file gate, and added `index_build.sqlite` for new-process transaction locking. SQLite and JSONL are validated and hash-bound so failed publication rolls back and crash-split pairs rebuild.
- Simplified automatic OKF execution to one fixed lease with no heartbeat or renewal, one structured batch call when claims exist, and token/lease-fenced validation and publication.
- Kept current v0.3.12 OKF queue data readable through selected-claim schema normalization without adding a general historical migration ladder.

### Removed

- Deleted all eleven superseded compatibility and layering modules after their documented product behavior moved to the final owners.

[0.4.0]: https://github.com/stepanov1975/hermes-local-knowledge/compare/v0.3.12...v0.4.0

## [0.3.12] - 2026-07-26

### Changed

- Advanced the persisted index format to version 3 so indexes created before MCP argument redaction rebuild automatically instead of retaining old summaries and FTS rows.

### Fixed

- Redacted obvious credential values, including URL userinfo, from structured MCP process arguments before they enter summaries, routing fields, related paths, or the search index, while preserving useful commands, routing-safe URLs, paths, and locator metadata.
- Preserved literal phrase order, duplicates, and stopwords in quoted searches; kept strict phrase matches ahead of phrase-blind identity and relaxed fallback results.
- Recognized portable POSIX, home, Windows drive, and UNC paths without extracting local-looking paths embedded in HTTP URLs, trimmed trailing prose punctuation before relationship resolution, and encoded read-only SQLite URIs for paths containing spaces, `#`, or `?`.
- Prevented normalized OKF output-name collisions from replacing a routing note owned by a different tool while retaining same-tool regeneration.

## [0.3.11] - 2026-07-26

### Fixed

- Moved automatic tool OKF model generation out of the synchronous `on_session_finalize` callback. Session finalization now performs only a queue check and detached worker launch, so `/new`, `/reset`, session expiry, and CLI exit no longer wait for the model call.
- Kept generation on Hermes' host-owned `ctx.llm` path by running the bounded deterministic generator through a fresh `hermes local-knowledge okf-worker` plugin CLI process rather than a general agent with terminal or file tools.
- Added a durable SQLite generation lease that is renewed while the host LLM call runs. Each publication prevalidates a worker-unique temporary file and uses a short SQLite write transaction that revalidates the lease and claim before replacing, validating again, and completing the canonical artifact. Automatic and manual claim paths use the same transaction-fenced stale reconciliation, which completes valid canonical output left by hard process death before applying the retry cap. The synchronous launcher uses a read-only, timeout-bounded queue check, and a worker recursion guard prevents recursive launches.

## [0.3.10] - 2026-07-25

### Added

- Added a native Windows CI job on `windows-latest` with Python 3.12.
- Made automated releases wait for both the existing Linux test matrix and the Windows test job.
- Added cross-platform regression coverage for inter-process index-build locking and SQLite publication while a concurrent reader is active.

### Fixed

- Closed OKF queue SQLite connections deterministically after commit or rollback. Python's SQLite context manager does not close the connection by itself, which left database files locked on Windows.
- Reworked lock regression probes to exercise the plugin's native POSIX or Windows locking implementation instead of assuming `fcntl` is available.
- Explicitly closed SQLite setup connections in race tests so the tests model only the intended concurrent-reader behavior.

## [0.3.9] - 2026-07-24

### Added

- Added an automated release workflow that runs only after CI succeeds on `main`, builds and checks fresh wheel and source distributions, installation-smokes both artifacts, verifies the Hermes plugin entry point, and publishes the matching version tag and GitHub release.
- Added idempotent handling for reruns and repair of incomplete releases while verifying that existing tags point to the exact tested commit.

### Changed

- Advanced the persisted index format to version 2 and classified indexes as missing, corrupt, older, current, or newer.
- Missing, corrupt, and older indexes are rebuilt automatically; newer-format indexes are now rejected with expected and observed version details instead of being overwritten by an older runtime.
- Kept generated OKF body prose positive-purpose-only and excluded negative boundary language from positive routing evidence.

### Fixed

- Prevented `when_not_to_use`-style negative OKF prose from making an unrelated artifact searchable for the excluded domain ([#18]).
- Added bounded retries for SQLite index publication when short-lived Windows readers temporarily prevent `os.replace`, while preserving the previous usable index if retries are exhausted ([#19]).
- Prevented older plugin runtimes from downgrading indexes created by newer index formats across native tools and config-backed CLI lookups ([#20]).

[0.3.12]: https://github.com/stepanov1975/hermes-local-knowledge/compare/v0.3.11...v0.3.12
[0.3.11]: https://github.com/stepanov1975/hermes-local-knowledge/compare/v0.3.10...v0.3.11
[0.3.10]: https://github.com/stepanov1975/hermes-local-knowledge/compare/v0.3.9...v0.3.10
[0.3.9]: https://github.com/stepanov1975/hermes-local-knowledge/compare/v0.3.8...v0.3.9
[#18]: https://github.com/stepanov1975/hermes-local-knowledge/issues/18
[#19]: https://github.com/stepanov1975/hermes-local-knowledge/issues/19
[#20]: https://github.com/stepanov1975/hermes-local-knowledge/issues/20

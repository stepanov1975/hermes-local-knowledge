# Changelog

All notable changes to this project are documented in this file.

## [0.4.1] - 2026-08-05

### Added

- Added a bounded deterministic search prior learned only from explicit useful feedback for the configured live source root. A matching accepted artifact is promoted only when it still appears in current index results.
- Added one concise, artifact-type-scoped retry when an accepted route is absent from the initial results; the remembered artifact must be rediscovered by that live retry before promotion.

### Changed

- Kept read-only evaluation on the unassisted index ranking so feedback labels do not train and score the same replay.

### Fixed

- Made newer matching rejections veto older overlap routes and prevented a retry from expanding to a longer accepted query.
- Mapped artifact-ID prefixes such as `mcp:` and `cron:` to their actual artifact types before the typed verification retry.
- Scoped feedback assistance to the source root stored in the current index, encoded read-only feedback database URIs, and bounded locked-database fallback with a root/order lookup index and short timeout.

[0.4.1]: https://github.com/stepanov1975/hermes-local-knowledge/compare/v0.4.0...v0.4.1

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

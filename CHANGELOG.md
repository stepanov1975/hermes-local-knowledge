# Changelog

All notable changes to this project are documented in this file.

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

[0.3.10]: https://github.com/stepanov1975/hermes-local-knowledge/compare/v0.3.9...v0.3.10
[0.3.9]: https://github.com/stepanov1975/hermes-local-knowledge/compare/v0.3.8...v0.3.9
[#18]: https://github.com/stepanov1975/hermes-local-knowledge/issues/18
[#19]: https://github.com/stepanov1975/hermes-local-knowledge/issues/19
[#20]: https://github.com/stepanov1975/hermes-local-knowledge/issues/20

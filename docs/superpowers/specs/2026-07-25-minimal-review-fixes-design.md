# Minimal Review Fixes Design

## Goal

Fix every confirmed review issue in the local-knowledge plugin with the smallest practical code changes, preserving whole-artifact routing, standard-library-only runtime dependencies, public entry points, and existing configuration behavior.

## Constraints

- This plugin runs on a private host for private use. Corporate-grade threat modeling and generalized secret-detection frameworks are out of scope.
- Credential filtering must cover only obvious credential-bearing forms while preserving useful MCP routing metadata.
- Prefer focused helpers and explicit errors over new abstractions, migrations, background services, or dependency additions.
- Preserve `indexer.py` and `plugin.py` compatibility surfaces and existing monkeypatch seams.
- Every behavioral fix must begin with a focused failing regression test.
- Release-relevant changes require synchronized version updates in `plugin.yaml`, `pyproject.toml`, and `hermes_local_knowledge/__init__.py`.

## Chosen Approach

Use surgical fixes and fail-fast behavior where transparent support would require a compatibility migration.

The rejected alternatives are:

1. A collision-aware artifact-ID migration with persistent mappings and legacy-ID aliases. This would add migration logic and change feedback/history semantics for a rare private-host edge case.
2. A comprehensive credential classifier or policy engine. This would be disproportionate for local private configuration and would risk removing useful routing terms.

## Design

### MCP scanning and graph routing

Add a small scanner-local sanitizer for MCP `command`, `args`, and URL fields:

- redact the value after an option whose name contains `token`, `secret`, `password`, `api-key`, `apikey`, `authorization`, or `credential`;
- redact the right-hand side of an inline secret-like `name=value` argument;
- remove URL user information, query strings, and fragments while retaining scheme, host, port, and path;
- otherwise preserve command names, package/module identifiers, flags, paths, and ordinary arguments.

Do not inspect arbitrary content or add entropy/format-based secret detection.

Extend the standard-library YAML fallback only enough to parse block-list `args` and mapping keys under `env`; environment values remain discarded. Extend path extraction to recognize Windows drive/UNC paths, `~/...`, and general absolute POSIX paths without extracting URL path fragments. Use the platform-appropriate path parser when resolving basenames.

### Artifact and OKF collisions

Replace silent last-write-wins behavior in `collect_artifacts()` with an explicit collision error naming the duplicate artifact ID and both source paths. This preserves all existing IDs when inputs are valid and avoids a broad ID migration.

Before writing a generated OKF, inspect an existing target's frontmatter. If it belongs to a different exact tool name that maps to the same slug, reject the new candidate through the existing bounded error lifecycle instead of overwriting the first tool's file. No hashing, alias map, or automatic migration will be added.

### OKF publication

Render each OKF to a randomized temporary Markdown file in the final directory using exclusive temporary-file creation. Extend validation with an optional expected final path so staged content is checked against the claimed target without first becoming scanner-visible.

After successful validation, atomically replace the final file and mark the candidate done. Any exception after publication restores the previous file or removes the new file. Temporary files are always removed. Existing manual CLI validation keeps its current signature and behavior by default.

### Index publication and SQLite paths

Build and publish SQLite before writing the diagnostic JSONL file. This ensures a failed SQLite replacement cannot expose a newer JSONL generation beside an older runtime database. A fully transactional two-file generation protocol is intentionally out of scope because runtime reads SQLite and JSONL is diagnostic output.

Construct read-only SQLite connections from percent-encoded absolute file URIs. Apply the same one-line behavior to evaluation and the historical comparison helper.

### Search and evaluation semantics

Update `fts_query()` so balanced quoted spans become escaped FTS5 phrase expressions while unquoted terms retain existing prefix behavior. Pure quoted queries remain strict-only; mixed queries keep their existing fallback rules.

For parent-equivalent evaluation, compare the small equivalence family of each result and expected artifact. This allows sibling support documents under the same owning skill while continuing to exclude generic graph neighbors, cron/script edges, and peer skills. Apply identical logic in the historical comparison helper.

### CLI and packaging

Make the doctor check require both `okf.enabled` and `okf.auto_generate`, with a concise recommendation for each disabled setting.

For search, get, neighbors, and evaluate, emit a small JSON error object and return exit status 1 whenever `--json` is active. Human-readable mode retains existing traceback/error behavior. Missing artifacts use the same JSON error contract.

Expand `MANIFEST.in` only with files required by the tests already shipped in the source distribution: version-policy scripts, `plugin.yaml`, root skills, examples, and installer guidance. Do not add new packaging machinery.

### Version and documentation

Bump all synchronized version locations from `0.3.10` to `0.3.11`. Add a compact changelog entry summarizing the routing, OKF, CLI, and packaging corrections. No broader README rewrite is needed.

## Error Handling

- Artifact collisions stop the index build with a clear deterministic error instead of losing data.
- OKF slug collisions use the existing candidate error/retry cap and never overwrite another tool's completed file.
- Staged OKF validation failures leave the previous final file intact.
- JSON CLI mode returns JSON for expected and operational failures; non-JSON mode remains unchanged.
- SQLite and JSONL publication errors continue to propagate so scheduled rebuilds remain observable.

## Testing

Add focused regressions covering:

- obvious MCP secret flags, inline assignments, and credential-bearing URLs without over-redacting ordinary arguments;
- fallback block-list arguments and environment names without values;
- Windows, UNC, macOS, and general POSIX wrapper paths;
- duplicate artifact IDs and colliding tool OKF targets;
- validation exceptions, randomized staging cleanup, and final-file restoration;
- SQLite-first publication after exhausted replacement retries;
- quoted phrase adjacency and mixed quoted queries;
- sibling support-document parent equivalence in both evaluators;
- `doctor`'s two-flag configuration matrix;
- JSON error output for missing databases, missing artifacts, and evaluation failures;
- source-distribution extraction and test collection;
- state paths containing `#` and `%`.

After focused red-green cycles, run the full pytest, Ruff, Linux-targeted mypy, version-policy, and whitespace gates. Build and Twine-check wheel/sdist artifacts, install the wheel in an isolated environment, verify the Hermes entry point, and run tests from the unpacked sdist. Run configured Hermes smokes only if the local Hermes runtime is available.

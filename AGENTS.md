# Agent instructions for `hermes-local-knowledge`

These instructions apply to the whole repository.

## Before editing

1. Read `README.md`, `CONTRIBUTING.md`, and `memory.md`.
2. Check `git status --short --branch`; preserve unrelated dirty work.
3. Keep changes focused. Runtime dependencies remain Python standard library only unless a documented product need justifies otherwise.
4. Never commit generated/local state or secrets.

## Documented public boundaries

- Package version is synchronized in `plugin.yaml`, `pyproject.toml`, and `hermes_local_knowledge/__init__.py`.
- The Python plugin entry point is `local_knowledge = hermes_local_knowledge.plugin`; `plugin.register` is the registration boundary.
- Registration provides exactly five native tools (`knowledge_search`, `knowledge_get`, `knowledge_neighbors`, `knowledge_feedback`, `knowledge_usage_report`) and two hooks (`post_tool_call`, `on_session_finalize`).
- `indexer.__all__` is exactly: `Artifact`, `Edge`, `IndexSettings`, `build_index`, `search_index`, `get_artifact`, `get_neighbors`, `main`.
- `python -m hermes_local_knowledge.cli` is the primary standalone CLI. `python -m hermes_local_knowledge.indexer` is the preserved compatibility entry point. `hermes local-knowledge` is the smaller install/doctor surface; its worker command is host-internal.
- Preserve documented configuration aliases and defaults. Do not preserve undocumented private call shapes merely because a test once patched them.
- The three router-skill copies are an intentional packaging/install exception to DRY and must remain byte-identical with one equality assertion in `tests/test_public_contract.py`.

## Current owners

- `config.py`: configuration models and resolution.
- `artifacts.py`: whole-artifact models, collection, privacy-safe metadata, graph edges.
- `index.py`: format-4 persistence, cross-version/SQLite build locks, rebuild classification, deterministic retrieval.
- `telemetry.py`: usage/feedback persistence and reports.
- `evaluation.py`: read-only feedback replay and metrics.
- `service.py`: managed index and telemetry lifecycle for one resolved config.
- `okf.py`: safe OKF queue, hooks, worker, validation, fenced publication.
- `plugin.py`: Hermes tools/hooks/skill/CLI registration.
- `cli.py`: standalone and Hermes CLI adapters.
- `indexer.py`: thin compatibility facade.
- `__init__.py`: package version.

## Product, ranking, and privacy invariants

- Route to whole artifacts; do not introduce chunk RAG without an explicit design change.
- FTS is the primary broad-recall path. Deterministic identity/metadata retrieval is complementary, not a replacement.
- Operational type promotion is narrow and query-gated. Quoted searches, explicit `artifact_type` filters, skill-parent lifting, and global per-parent support-doc diversity must retain their documented behavior.
- Parent-equivalent evaluation only relates a `skill_support_doc` to its owning skill; graph neighbors are not evaluation equivalence.
- Script search text uses routing-safe metadata, never arbitrary body literals.
- Environment names may be routing signals. Environment values, MCP credential values, raw tool arguments/output, transcripts, OCR/private document text, and secret-like schema values must not enter indexed or generated artifacts.
- `$HERMES_HOME/skills/.archive` is excluded from active routing.
- Feedback/evaluation data stays local. Keep public docs/tests free of raw telemetry and private content.
- Feedback-assisted routing is current-index-root-only and bounded to the latest significant explicit query/artifact rating. Only `useful` is positive; a newer rejection for the route or matching current query vetoes an older overlapping positive. Promote an artifact only when the current index returns it, with at most one no-longer-than-current artifact-type retry. Explicit caller-owned indexes remain unassisted.
- Read-only evaluation measures the unassisted index ranking. Do not train on and score the same feedback replay.

## State and concurrency invariants

Generated state includes `index.sqlite`, `index.jsonl`, `usage.sqlite`, `okf_queue.sqlite`, `okfs/tools/*.md`, `okf_worker.log`, the v0.3.12-compatible file gate `index_build.lock`, the SQLite transaction lock `index_build.sqlite`, and `okf_index_dirty/`. Do not commit it.

- Managed lookups rebuild missing, corrupt, older-format, or OKF-dirty indexes. Ordinary source changes require `rebuild=true`, an explicit CLI build, or an optional operator schedule; no schedule is required.
- Reject newer index formats before publication. Build and validate temporary SQLite/JSONL outputs before publishing them as a recoverable, hash-bound pair under both build locks.
- OKF automatic generation requires explicit model-token consent. The finalizer only checks and launches; the detached worker uses one fixed lease, one structured batch call when claims exist, and claim/lease-fenced validation/publication.
- Version 0.4.0 supports the current v0.3.12 queue shape through selected-claim normalization, not a general migration ladder.

## Release policy

A version bump is required when release-relevant paths differ from the base, including:

- `__init__.py`, `plugin.yaml`, `pyproject.toml`, `after-install.md`;
- `hermes_local_knowledge/**`, `examples/**`, and `skills/**`.

Docs-only edits to `README.md`, `CONTRIBUTING.md`, `memory.md`, or `AGENTS.md` normally do not require a bump unless accompanied by a release-relevant path.

## Verification

Focused public/plugin contract check:

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest \
  tests/test_public_contract.py tests/test_plugin.py \
  -q -p no:cacheprovider
```

Full gate:

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q -p no:cacheprovider
python -m ruff check .
python -m mypy
python scripts/check_version_policy.py --base-ref origin/main
git diff --check
```

For ranking/index changes, also run a configured build, read-only evaluation, and doctor smoke. For release/package changes, build and installation-smoke both wheel and sdist.

## Repository safety

Keep `.env*`, databases, JSONL indexes, logs, caches, builds, virtualenvs, mutation workspaces, and local state out of commits. This is a public repository: never add credentials, private document contents, raw session transcripts, or identifying telemetry rows.

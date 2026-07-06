# Agent instructions for hermes-local-knowledge

These instructions apply to the whole repository.

## Start here

1. Read `README.md`, `CONTRIBUTING.md`, and `memory.md` before changing code.
2. Check `git status --short --branch` and protect any existing dirty work.
3. Keep changes focused. This is a small reusable Hermes Agent plugin; avoid speculative abstractions.
4. Treat generated/local state as off limits for commits.

## Project invariants

- The plugin routes local questions to **whole artifacts**. Do not turn it into chunk RAG without an explicit design change.
- Runtime dependencies should remain Python standard library only unless a strong reason is documented.
- `indexer.py` and `plugin.py` are compatibility/public entry points. Preserve existing exports, CLI compatibility, and monkeypatch seams unless deliberately breaking them.
- Handlers should return JSON-compatible success/error payloads for expected bad input; do not let malformed tool args crash normal plugin calls.
- Config belongs in Hermes `config.yaml`; secrets belong in env/secret stores and must never be indexed or written to docs/tests.

## Files and state to avoid committing

Do not commit generated or local-only artifacts, including:

- `*.sqlite`, `*.sqlite3`, `*.db`, `*.jsonl`;
- `knowledge/`, `state/`, `logs/`, `tmp/`;
- `.coverage`, `htmlcov/`, `.pytest_cache/`, `.ruff_cache/`, `.mypy_cache/`;
- `build/`, `dist/`, `*.egg-info/`;
- `mutants/`, virtualenvs, `.env*`.

## Version policy

For release-relevant changes, bump all three version locations together:

- `plugin.yaml`
- `pyproject.toml`
- `hermes_local_knowledge/__init__.py`

Release-relevant paths include `hermes_local_knowledge/**`, `plugin.yaml`, `pyproject.toml`, `after-install.md`, `examples/**`, and `skills/**`. The policy is enforced by:

```bash
python scripts/check_version_policy.py --base-ref origin/main
```

Docs-only changes such as `README.md`, `CONTRIBUTING.md`, `memory.md`, or `AGENTS.md` normally do not require a version bump.

## Search/indexing rules to preserve

Before touching `scanners.py`, `search.py`, `evaluation.py`, `handlers.py`, or tests, read the detailed rationale in `memory.md`.

Key invariants:

- FTS remains the primary broad-recall path; metadata/identifier retrieval is a deterministic fallback, not a replacement.
- Script search text must use safe routing metadata only. Do not index arbitrary script body literals.
- Env **names** may be routing signals; env **values** must not be persisted or searched.
- Parent-equivalent evaluation is limited to `skill_support_doc <-> owning parent skill` pairs.
- Operational priority must be narrow and domain-gated. Do not globally promote all non-prose artifacts.
- Exact/quoted support-doc behavior is important. Pure quoted searches should not be polluted by loose fallback results.
- `artifact_type` filters must only return the requested type, including `skill_support_doc`.
- Support-doc diversity is per parent and applied globally after strict + fallback candidates are combined.

## Testing and verification

For code changes, run:

```bash
python -m pytest -q
python -m ruff check .
python -m mypy
python scripts/check_version_policy.py --base-ref origin/main
git diff --check
```

For docs-only changes, at minimum run:

```bash
git diff --check
python scripts/check_version_policy.py --base-ref origin/main
```

For search/ranking/scanner/evaluation changes, also run configured runtime smokes when available:

```bash
HERMES_HOME=/home/alex/.hermes python -m hermes_local_knowledge.cli build --from-hermes-config
python -m hermes_local_knowledge.cli evaluate --from-hermes-config --json
python -m hermes_local_knowledge.cli doctor --hermes-home /home/alex/.hermes --rebuild --query 'paperless review'
```

If `doctor --from-hermes-config` is rejected by a revision, use `--hermes-home` as above.

For package/release changes, additionally build and inspect the wheel/sdist:

```bash
python -m build --outdir "$tmpdist"
python -m twine check "$tmpdist"/*
python -m venv "$tmpvenv"
"$tmpvenv/bin/python" -m pip install "$tmpdist"/*.whl
"$tmpvenv/bin/python" - <<'PY'
import importlib.metadata as md
matches = [ep for ep in md.entry_points().select(group='hermes_agent.plugins') if ep.name == 'local_knowledge']
print([(ep.name, ep.value) for ep in matches])
module = matches[0].load()
print('register_callable', callable(getattr(module, 'register', None)))
PY
```

## Independent review discipline

When using subagents/reviewers:

- Tell review agents explicitly whether they may edit files. For review-only tasks, say **do not modify files**.
- A timed-out, cancelled, missing, or partial review is not a PASS.
- If a review times out, split the review into smaller bounded tasks or report that there is no independent approval.
- After subagent work, check `git status` and inspect for unintended files.

## Release/deployment notes

- Before release, CI green is not enough if async reviewers may still post. Read back PR reviews/review threads when a PR exists.
- Verify tag target, GitHub release assets, and GitHub Actions results after publishing.
- For Alex's installed plugin, update and verify both source and installed SHAs:

```bash
HERMES_HOME=/home/alex/.hermes hermes plugins update local_knowledge
git -C /home/alex/repos/hermes-local-knowledge rev-parse --short HEAD
git -C /home/alex/.hermes/plugins/local_knowledge rev-parse --short HEAD
```

- A running Hermes gateway may keep old imported plugin code. Restart from outside the gateway process or use `/restart` from chat. Do not run `hermes gateway restart` from inside a gateway-run terminal session.

## Public-safety rules

This is a public reusable plugin repository. Do not write secrets, private document contents, raw local telemetry, or private session transcripts into repository files. Distill behavior and lessons instead.

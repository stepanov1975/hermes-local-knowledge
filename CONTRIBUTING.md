# Contributing

Thanks for considering a contribution to Hermes Local Knowledge.

This is a small Hermes Agent plugin, so the best contributions are focused, tested, and easy to review.

## Before you open an issue or pull request

- Search existing issues and pull requests first.
- Keep reports free of secrets, tokens, private document contents, full local credential paths, and sensitive logs.
- For security-sensitive reports, follow [`SECURITY.md`](SECURITY.md) instead of opening a detailed public issue.

## Development setup

```bash
python -m pip install -e '.[test]'
python -m pytest -q
python -m ruff check .
python -m mypy
python scripts/check_version_policy.py --base-ref origin/main
git diff --check
```

The package has no runtime dependencies beyond the Python standard library. Test dependencies are intentionally small.

## Pull request expectations

A good pull request includes:

- a short explanation of the user-facing behavior change;
- tests or a clear reason tests are not needed;
- a synchronized plugin version bump when release-relevant files change;
- documentation updates when install, configuration, or public behavior changes;
- local verification output in the PR checklist.

The version policy is checked in CI. Keep `plugin.yaml`, `pyproject.toml`, and
`hermes_local_knowledge/__init__.py` synchronized. If a change touches runtime
or package/plugin surfaces such as `hermes_local_knowledge/**`, `plugin.yaml`,
`pyproject.toml`, `after-install.md`, or `examples/**`, bump the version in all
three locations compared with the target branch.

Generated or local-only files must stay out of commits, including:

- `.env` and `.env.*`;
- `*.sqlite`, `*.sqlite3`, `*.db`, and `*.jsonl`;
- `knowledge/`, `state/`, `logs/`, `tmp/`;
- caches, build outputs, mutation-test workspaces, and virtualenvs.

## Scope guidelines

Prefer behavior-level fixes over speculative abstractions. If a change expands what the index scans by default, explain the privacy impact and add tests for private/local-state exclusion.

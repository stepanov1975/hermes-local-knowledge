## Summary

<!-- What changed and why? -->

## Verification

- [ ] `python -m pytest -q`
- [ ] `python -m ruff check .`
- [ ] `python -m mypy`
- [ ] `python scripts/check_version_policy.py --base-ref origin/main`
- [ ] `git diff --check`
- [ ] Install/register/search smoke tested if plugin packaging changed

## Checklist

- [ ] Generated state (`*.sqlite`, `*.jsonl`, caches, `.env`) is not committed
- [ ] Plugin version bumped in `plugin.yaml`, `pyproject.toml`, and `hermes_local_knowledge/__init__.py` if release-relevant files changed
- [ ] Docs updated if install/config behavior changed
- [ ] Security-sensitive changes reviewed for secret/path leakage

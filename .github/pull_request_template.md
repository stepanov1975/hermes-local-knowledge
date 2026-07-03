## Summary

<!-- What changed and why? -->

## Verification

- [ ] `python -m pytest -q`
- [ ] `python -m ruff check .`
- [ ] `python -m mypy`
- [ ] `git diff --check`
- [ ] Install/register/search smoke tested if plugin packaging changed

## Checklist

- [ ] Generated state (`*.sqlite`, `*.jsonl`, caches, `.env`) is not committed
- [ ] Docs updated if install/config behavior changed
- [ ] Security-sensitive changes reviewed for secret/path leakage

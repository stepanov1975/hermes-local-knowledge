# GitHub security and repository hygiene

This repository uses lightweight GitHub hygiene suitable for a public reusable Hermes plugin.

## Automated checks

| File | Purpose |
| --- | --- |
| `.github/workflows/ci.yml` | Runs tests and Ruff on Python 3.11 and 3.12. |
| `.github/workflows/security.yml` | Runs Gitleaks, actionlint, Semgrep, zizmor, ShellCheck, and gated pip-audit. |
| `.github/workflows/codeql.yml` | Runs GitHub CodeQL for Python after the repository is public. The job is skipped while the repository is private. |
| `.github/dependabot.yml` | Weekly dependency/update checks for GitHub Actions and Python packaging metadata, with a 7-day cooldown. |
| `.gitleaks.toml` | Secret scanner config with narrow placeholder allowlist. |
| `.semgrepignore` | Excludes caches/build output from Semgrep scans. |
| `CONTRIBUTING.md` | Public contribution expectations and local validation commands. |
| `CODE_OF_CONDUCT.md` | Community conduct expectations. |
| `SECURITY.md` | Vulnerability reporting and leak-response notes. |

## Local validation

Run before pushing security-related changes:

```bash
python -m pytest -q
python -m ruff check .
git diff --check

# Workflow syntax/security checks, when Docker and uvx are available:
docker run --rm -v "$PWD:/repo" -w /repo \
  docker.io/rhysd/actionlint@sha256:887a259a5a534f3c4f36cb02dca341673c6089431057242cdc931e9f133147e9 \
  -color

docker run --rm -v "$PWD:/repo" -w /repo \
  ghcr.io/gitleaks/gitleaks@sha256:cdbb7c955abce02001a9f6c9f602fb195b7fadc1e812065883f695d1eeaba854 \
  detect --source=/repo --config=/repo/.gitleaks.toml --redact --verbose

uvx --from zizmor==1.26.1 zizmor .github/workflows
uvx --from semgrep==1.168.0 semgrep scan --config p/ci --config p/secrets --error --metrics=off .
```

## Manual GitHub settings after making the repository public

Some settings are not fully represented by repository files.

Recommended settings:

1. **Settings → Code security and analysis**
   - Enable Dependency graph.
   - Enable Dependabot alerts.
   - Enable Dependabot security updates.
   - Enable Secret scanning.
   - Enable Push protection.
   - Enable Private vulnerability reporting if available.
2. **Settings → Actions → General**
   - Allow GitHub Actions to run.
   - Keep workflow permissions read-only unless a workflow explicitly needs writes. CodeQL needs `security-events: write` in its own workflow.
3. **Settings → Branches / Rulesets**
   - Protect `main` or add a ruleset requiring pull requests and passing CI before merge.
   - Recommended required checks after the first public run: `Python 3.11`, `Python 3.12`, `Gitleaks`, `actionlint`, `Python static/security checks`, `ShellCheck`, and `CodeQL Python` if CodeQL is active.
4. **Settings → General**
   - Delete head branches after merge.
   - Keep wiki and projects disabled unless project documentation actually moves there.

## Notes

- The security workflow intentionally uses pinned action SHAs and pinned Docker image digests. When Dependabot opens an update PR for actions or scanner config, review both the human-readable version and the pinned SHA/digest before merging.
- GitHub-native Secret scanning and Push protection are separate from CI-based Gitleaks/Semgrep scanning. Confirm both after the visibility change.
- Public repositories receive GitHub code security features that may not be available while the repository is private on a free plan.

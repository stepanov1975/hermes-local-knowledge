# GitHub security and repository hygiene

This repository uses lightweight GitHub hygiene suitable for a public reusable Hermes plugin.

## Automated checks

| File | Purpose |
| --- | --- |
| `.github/workflows/ci.yml` | Runs version-policy checks and tests on Linux/Python 3.11 and 3.12, Ruff and mypy on Linux, Windows/Python 3.12 tests, then the release workflow for verified `main` pushes. |
| `.github/workflows/security.yml` | Runs Gitleaks, actionlint, Semgrep, zizmor, ShellCheck, and gated pip-audit. |
| `.github/dependabot.yml` | Weekly dependency/update checks for GitHub Actions and Python packaging metadata, with a 7-day cooldown. |
| `requirements-release.txt` | Pins the write-capable release job's top-level build tools and constrains its isolated build environment. |
| `requirements-security.txt` | Pins the Python security scanners so Dependabot can maintain them. |
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
python -m mypy
git diff --check

# Workflow syntax/security checks, when Docker is available:
# actionlint v1.7.12
docker run --rm -v "$PWD:/repo" -w /repo \
  docker.io/rhysd/actionlint:1.7.12@sha256:b1934ee5f1c509618f2508e6eb47ee0d3520686341fec936f3b79331f9315667 \
  -color

# Gitleaks v8.30.1
docker run --rm -v "$PWD:/repo" -w /repo \
  ghcr.io/gitleaks/gitleaks:v8.30.1@sha256:c00b6bd0aeb3071cbcb79009cb16a60dd9e0a7c60e2be9ab65d25e6bc8abbb7f \
  detect --source=/repo --config=/repo/.gitleaks.toml --redact --verbose

python -m pip install --requirement requirements-security.txt
zizmor .github/workflows
semgrep scan --config p/ci --config p/secrets --error --metrics=off .
```

## Manual GitHub settings

Some settings are not fully represented by repository files.

Recommended settings:

1. **Settings → Code security and analysis**
   - Enable Dependency graph.
   - Enable Dependabot alerts.
   - Enable Dependabot security updates.
   - Enable Secret scanning.
   - Enable Push protection.
   - Keep Private vulnerability reporting enabled.
   - Enable GitHub CodeQL default setup for both Python and GitHub Actions.
2. **Settings → Actions → General**
   - Allow GitHub Actions to run.
   - Require actions to be pinned to a full-length commit SHA.
   - Keep workflow permissions read-only unless a workflow explicitly needs writes.
3. **Settings → Branches / Rulesets**
   - Protect `main` or add a ruleset requiring pull requests and passing CI before merge.
   - Recommended required checks after the first public run: `Python 3.11`, `Python 3.12`, `Windows Python 3.12`, `Gitleaks`, `actionlint`, `Python static/security checks`, `ShellCheck`, `Analyze (python)`, and `Analyze (actions)`.
4. **Settings → General**
   - Delete head branches after merge.
   - Keep wiki and projects disabled unless project documentation actually moves there.
   - Enable release immutability. The release workflow creates a draft, verifies its notes and artifacts, and only then publishes it.

## Dependency-pin maintenance

- Dependabot maintains the full-SHA GitHub Action references and both root `requirements-*.txt` files.
- Docker-based scanners remain pinned by digest because Dependabot does not update image references embedded in workflow shell commands. Their human-readable versions are recorded beside the digest variables in `security.yml`.
- When updating a scanner image, verify the release tag's manifest digest with `docker buildx imagetools inspect`, run the digest-pinned image's version command, then update `security.yml` and the local-validation examples together. A scheduled updater is intentionally not required for this small repository.

## Notes

- The security workflow intentionally uses pinned action SHAs and pinned Docker image digests. Review both the human-readable version and the pinned SHA/digest before merging updates.
- GitHub-native Secret scanning and Push protection are separate from CI-based Gitleaks/Semgrep scanning. Confirm both after the visibility change.
- CodeQL uses GitHub's default setup rather than a checked-in advanced workflow, avoiding duplicate configurations while keeping Python and GitHub Actions analysis active.
- Public repositories receive GitHub code security features that may not be available while the repository is private on a free plan.

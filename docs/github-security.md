# GitHub security and repository hygiene

This repository uses lightweight GitHub hygiene suitable for a private reusable Hermes plugin.

## Automated checks

| File | Purpose |
| --- | --- |
| `.github/workflows/ci.yml` | Runs tests on Python 3.11 and 3.12. |
| `.github/workflows/security.yml` | Runs Gitleaks, actionlint, Semgrep, zizmor, ShellCheck, and gated pip-audit. |
| `.github/dependabot.yml` | Weekly dependency/update checks for GitHub Actions and Python packaging metadata. |
| `.gitleaks.toml` | Secret scanner config with narrow placeholder allowlist. |
| `.semgrepignore` | Excludes caches/build output from Semgrep scans. |
| `SECURITY.md` | Private vulnerability reporting and leak-response notes. |

## Local validation

Run before pushing security-related changes:

```bash
python -m pytest -q
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

## Manual GitHub settings

Some GitHub settings are not fully represented by repository files, especially on private/free repositories.

Recommended settings:

1. **Settings → Code security and analysis**
   - Enable Dependency graph.
   - Enable Dependabot alerts.
   - Enable Dependabot security updates.
   - Enable Secret scanning and Push protection if the account plan allows it.
2. **Settings → Actions → General**
   - Allow GitHub Actions to run.
   - Keep workflow permissions read-only unless a workflow explicitly needs writes.
3. **Settings → Branches / Rulesets**
   - Protect `main` or add a ruleset requiring CI when the plan allows it.
   - For private repos on free plans, classic branch protection may be unavailable.
4. **Settings → General**
   - Delete head branches after merge.
   - Keep wiki disabled unless project documentation actually moves there.

## Notes

The security workflow intentionally uses pinned action SHAs and pinned Docker image digests. When Dependabot opens an update PR for actions or scanner config, review both the human-readable version and the pinned SHA/digest before merging.

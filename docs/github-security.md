# GitHub security and repository hygiene

This repository uses lightweight GitHub hygiene suitable for a public reusable Hermes plugin.

## Automated checks

| File | Purpose |
| --- | --- |
| `.github/workflows/ci.yml` | Runs Linux tests on Python 3.11 and 3.12, one Linux quality lane for version policy, Ruff, and mypy, Windows/Python 3.12 tests, then the release workflow for verified `main` pushes. |
| `.github/workflows/security.yml` | Runs Gitleaks, actionlint with conditional ShellCheck when shell files exist, Semgrep, zizmor, and gated pip-audit. |
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
   - Recommended required checks after the first public run: `Python 3.11`, `Python 3.12`, `Python quality`, `Windows Python 3.12`, `Gitleaks`, `actionlint`, `Python static/security checks`, `Analyze (python)`, and `Analyze (actions)`.
4. **Settings → General**
   - Delete head branches after merge.
   - Keep wiki and projects disabled unless project documentation actually moves there.
   - Enable release immutability. The release workflow creates a draft, verifies its notes and artifacts, and only then publishes it.

## Dependency-pin maintenance

- Dependabot maintains the full-SHA GitHub Action references and both root `requirements-*.txt` files.
- Docker-based scanners remain pinned by digest because Dependabot does not update image references embedded in workflow shell commands. Their human-readable versions are recorded beside the digest variables in `security.yml`.
- When updating a scanner image, verify the release tag's manifest digest with `docker buildx imagetools inspect`, run the digest-pinned image's version command, then update `security.yml` and the local-validation examples together. A scheduled updater is intentionally not required for this small repository.

## Scanner-only audit exceptions

The `Run pip-audit` step excludes exactly **five advisories, still unpatched in
its scanner dependency graph**, only when auditing `requirements-security.txt`.
These are non-applicability exceptions for the fixed CI invocation, not security
updates or general approvals of the affected libraries. Every other advisory in
that manifest, every other `requirements*.txt` manifest (including release
tools), and the generated project/optional-dependency audit remain strict and
fail the job on findings. No ambient-environment audit or global ignore applies.

The owning scanner is pinned to [Semgrep 1.184.0](https://pypi.org/pypi/semgrep/1.184.0/json).
Its published metadata requires `mcp==1.29.0` and the OpenTelemetry API, SDK and
OTLP-proto-HTTP exporter at `~=1.37.0`; the SDK resolves the API to 1.37.0.
[Semgrep 1.185.0 metadata](https://pypi.org/pypi/semgrep/1.185.0/json) retains these
constraints, so simply updating to that version does not fix the findings.
Forcing incompatible transitive upgrades or installing with `--no-deps` is not a
supported remediation.

| Excluded advisory | Affected component and why the CI command does not reach it |
| --- | --- |
| [CVE-2026-59951](https://github.com/advisories/GHSA-84m7-p3x7-pcfv) | `mcp`: unbounded stateful Streamable HTTP server sessions. The scan does not start an MCP server. |
| [CVE-2026-116684](https://github.com/advisories/GHSA-w4fh-qvv9-3v23) | `mcp`: server bearer-token audience validation. The scan does not expose an MCP authentication endpoint. |
| [CVE-2026-116685](https://github.com/advisories/GHSA-5h93-6whr-6q8j) | `mcp`: HTTP/SSE client redirects carrying custom headers/bodies. The scan does not connect to MCP servers or use the MCP OAuth client. |
| [CVE-2026-116686](https://github.com/advisories/GHSA-rwrf-2pqf-9j8j) | `mcp`: client tool-result validation fetching server-supplied schema references. The scan does not create an MCP client or call remote MCP tools. |
| [CVE-2026-114603](https://github.com/advisories/GHSA-xcq7-p3pf-p88h) | `opentelemetry-api`: quadratic inbound `tracestate` parsing. The scan leaves tracing disabled (the CLI default) and does not extract untrusted propagation headers. |

This assessment is limited to
`semgrep scan --config p/ci --config p/secrets --error --metrics=off .`.
The registry configuration downloads are not MCP client sessions. Semgrep's MCP
entry point is separate from this scan command; the presence/import of the SDK
does not start a server or client. The tracing argument defaults to false;
`--metrics=off` is not itself the tracing control. Published-wheel inspection and
an isolated scan probe supported these reachability conclusions. They do not
establish safety for other Semgrep modes, enabled tracing, a production MCP
service, or arbitrary use of these packages.

The repository maintainers own these exceptions. On each Semgrep update,
recheck its dependency metadata, resolve the scanner requirements, and audit
without exclusions. Remove each exception as soon as a compatible scanner
release resolves a patched dependency (`mcp>=1.30.0` for these four MCP
advisories; `opentelemetry-api>=1.45.1` for the tracing advisory), or the advisory
is withdrawn. Reassess before changing the scan invocation, enabling tracing,
or adding MCP use; if the affected path becomes reachable or uncertain, remove
the exception and fix the dependency or block the job rather than widening the
waiver. Keep this table, workflow arguments, and
`tests/test_security_workflow.py` synchronized. That regression test executes
the actual YAML shell recipe with a recording audit executable and checks both
argument isolation and fail-fast behavior; it is not a live vulnerability scan.

## Notes

- The security workflow intentionally uses pinned action SHAs and pinned Docker image digests. Review both the human-readable version and the pinned SHA/digest before merging updates.
- GitHub-native Secret scanning and Push protection are separate from CI-based Gitleaks/Semgrep scanning. Confirm both after the visibility change.
- CodeQL uses GitHub's default setup rather than a checked-in advanced workflow, avoiding duplicate configurations while keeping Python and GitHub Actions analysis active.
- Public repositories receive GitHub code security features that may not be available while the repository is private on a free plan.

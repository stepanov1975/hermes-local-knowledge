# Hermes Local Knowledge

[![CI](https://github.com/stepanov1975/hermes-local-knowledge/actions/workflows/ci.yml/badge.svg)](https://github.com/stepanov1975/hermes-local-knowledge/actions/workflows/ci.yml)
[![Security scans](https://github.com/stepanov1975/hermes-local-knowledge/actions/workflows/security.yml/badge.svg)](https://github.com/stepanov1975/hermes-local-knowledge/actions/workflows/security.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Reusable Hermes Agent plugin for routing local questions to the right local artifact: skills, scripts, runbooks, cron jobs, MCP servers, and supporting docs.

The router indexes **whole artifacts**, not arbitrary RAG chunks. Its job is to answer: *which local artifact should the agent inspect first?*

## What it provides

Native Hermes tools under the `local_knowledge` toolset:

| Tool | Purpose |
| --- | --- |
| `knowledge_search` | Search indexed local artifacts and auto-build the index if missing. |
| `knowledge_get` | Fetch one artifact by id. |
| `knowledge_neighbors` | Traverse conservative graph edges for one artifact. |
| `knowledge_feedback` | Record lookup quality feedback locally. |
| `knowledge_usage_report` | Summarize usage, zero-result queries, errors, and feedback. |

## Install

From the public git repo:

```bash
hermes plugins install https://github.com/stepanov1975/hermes-local-knowledge.git --enable
```

SSH also works when your host has GitHub SSH keys configured:

```bash
hermes plugins install git@github.com:stepanov1975/hermes-local-knowledge.git --enable
```

### Install the routing skill

The plugin registers tools, but a skill tells Hermes **when** to use them. Without the routing skill, Hermes can still call `knowledge_search` when explicitly asked, but it is much less likely to use the router proactively for local runbooks, scripts, cron jobs, MCP wrappers, or custom skills.

Install the bundled skill into the target Hermes home/profile for proactive routing:

```bash
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
mkdir -p "$HERMES_HOME/skills/local-knowledge-router"
cp "$HERMES_HOME/plugins/local_knowledge/skills/local-knowledge-router/SKILL.md" \
  "$HERMES_HOME/skills/local-knowledge-router/SKILL.md"
```

The plugin also registers this same skill as a read-only, namespaced plugin skill for explicit loads:

```text
skill_view("local_knowledge:local-knowledge-router")
```

That namespaced skill is useful as a versioned fallback/reference, but it does **not** appear in Hermes' normal available-skill index. Install the normal skill above when you want Hermes to use local knowledge proactively.

If you are working from a source checkout instead of the installed plugin directory:

```bash
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
mkdir -p "$HERMES_HOME/skills/local-knowledge-router"
cp skills/local-knowledge-router/SKILL.md \
  "$HERMES_HOME/skills/local-knowledge-router/SKILL.md"
```

You can also install the skill directly from GitHub:

```bash
hermes skills install https://raw.githubusercontent.com/stepanov1975/hermes-local-knowledge/main/skills/local-knowledge-router/SKILL.md --name local-knowledge-router
```

After adding the skill, start a fresh Hermes session or run `/reload-skills` and then `/new`/`/reset` so the router instructions enter the prompt.

Configure a high-signal source tree to index. Prefer a local operational/customization repo that contains your runbooks, helper scripts, and custom skills; the plugin still indexes runtime skills, cron jobs, and MCP config from `$HERMES_HOME` separately.

```bash
hermes config set local_knowledge.source_root "$HOME/repos/your-local-docs-or-customizations"
hermes config set local_knowledge.state_dir "$HOME/.hermes/local_knowledge"
hermes config set local_knowledge.custom_skill_dirs custom_skills
hermes config set local_knowledge.script_dirs scripts,hermes_home/scripts
hermes config set local_knowledge.include_markdown_docs true
hermes config set local_knowledge.exclude_dir_names build,dist
```

### Keep the index fresh

The native tools auto-build the index when it is missing, and every lookup accepts `rebuild=true`, but normal searches reuse the existing `index.sqlite`. For a real install, schedule a rebuild cron job. This matters because skills, scripts, runbooks, cron jobs, and MCP config can change outside the current session; without a scheduled rebuild, agents may route from stale metadata unless they remember to request `rebuild=true`.

For a directory plugin install, create a silent no-agent rebuild job:

```bash
export HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
mkdir -p "$HERMES_HOME/scripts"
cat > "$HERMES_HOME/scripts/rebuild_local_knowledge_index.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
cd "$HERMES_HOME/plugins/local_knowledge"
python -m hermes_local_knowledge.cli build --from-hermes-config --hermes-home "$HERMES_HOME" >/dev/null
EOF
chmod +x "$HERMES_HOME/scripts/rebuild_local_knowledge_index.sh"
hermes cron create \
  --name 'local_knowledge index rebuild' \
  --script rebuild_local_knowledge_index.sh \
  --no-agent \
  --deliver local \
  '0 * * * *'
```

Empty stdout keeps successful runs silent; non-zero exits still alert. Hourly is a reasonable default for active setups. Daily is fine for rarely changed docs.

Then restart the gateway from outside the running gateway process, or send
`/restart` from a gateway chat such as Telegram:

```bash
hermes gateway restart
```

For local development:

```bash
cd /path/to/hermes-local-knowledge
hermes plugins install "file://$(pwd)" --enable
hermes gateway restart
```

Directory install with `hermes plugins install` is the recommended path. The package also exposes a `hermes_agent.plugins` entry point for environments that install plugin packages into the same Python environment Hermes uses.

## Configuration

Put non-secret settings in `~/.hermes/config.yaml`:

```yaml
local_knowledge:
  source_root: ~/repos/<your-local-docs-or-customizations>
  state_dir: ~/.hermes/local_knowledge
  exclude_dir_names: [build, dist]
```

`source_root` is the high-signal directory being indexed. `state_dir` is generated local state and should not be committed. `exclude_dir_names` adds extra directory names to the built-in skip list (`.archive`, `worktrees`, `.worktrees`, `.git`, `__pycache__`, `node_modules`, `venv`, `.venv`, `.mypy_cache`, `.pytest_cache`, `htmlcov`, `logs`). Use YAML lists in `config.yaml`; when using `hermes config set` from the shell, comma-separated strings or bracket-list strings are accepted and normalized by the plugin.

Environment variables are supported for development and tests:

| Variable | Meaning |
| --- | --- |
| `LOCAL_KNOWLEDGE_ROOT` | Overrides `local_knowledge.source_root`. |
| `LOCAL_KNOWLEDGE_STATE_DIR` | Overrides `local_knowledge.state_dir`. |
| `HERMES_HOME` | Selects the Hermes profile/runtime home to inspect. |

If no `source_root` is configured, the plugin defaults to `HERMES_HOME` and indexes runtime skills, cron, and MCP config, but it does **not** scan arbitrary root-level Markdown notes unless `include_markdown_docs: true` is set explicitly. If `$HERMES_HOME/hermes-agent` exists, the tools and CLI warn because the broad runtime tree is usually noisier than a curated operational repo.

Recommended pattern:

- set `local_knowledge.source_root` to your high-signal docs/customizations repo, for example `~/repos/hermes-customizations`;
- keep `local_knowledge.state_dir` under `~/.hermes/local_knowledge` or another local-only state directory;
- rely on the plugin's separate runtime scan for `$HERMES_HOME/skills`, `$HERMES_HOME/cron/jobs.json`, and `$HERMES_HOME/config.yaml` MCP entries.

## Preserving existing history

If you are replacing an older deployment, point `state_dir` at the directory
that already contains `usage.sqlite` before restarting Hermes. New instances
should usually use `~/.hermes/local_knowledge` for `state_dir`.

## Configurable source layout

The scanner defaults are intentionally generic, and all source directories are configurable:

```yaml
local_knowledge:
  source_root: ~/repos/<your-local-docs-or-customizations>
  state_dir: ~/.hermes/local_knowledge
  custom_skill_dirs: [custom_skills]
  script_dirs: [scripts, hermes_home/scripts]
  memory_dirs: [memory]
  runbook_dirs: [docs]
  include_markdown_docs: true
  exclude_dir_names: [build, dist]
  known_entities:
    - Hermes
    - AcmeCloud
    - InternalAPI
```

The built-in excluded directory names are: `worktrees`, `.worktrees`, `.git`, `__pycache__`, `node_modules`, `venv`, `.venv`, `.mypy_cache`, `.pytest_cache`, `htmlcov`, `logs`. Use `exclude_dir_names` to add more without patching the code.

Indexed artifact types:

| Type | Default source |
| --- | --- |
| `skill` | `<source_root>/custom_skills/**/SKILL.md` plus runtime `$HERMES_HOME/skills/**/SKILL.md` |
| `script` | `<source_root>/scripts/**`, `<source_root>/hermes_home/scripts/**` |
| `memory_doc` | `<source_root>/memory/*.md` |
| `runbook` | `<source_root>/docs/**`, plus `app_*.md` files under the source root |
| `skill_support_doc` | Markdown support docs under configured custom skill dirs, plus runtime `$HERMES_HOME/skills/**/{references,templates,scripts,assets}` docs that are not already under `source_root` |
| `cron_job` | `$HERMES_HOME/cron/jobs.json` |
| `mcp_server` | `$HERMES_HOME/config.yaml` `mcp_servers` entries, plus legacy `mcp.servers` entries |

## Generated state

The plugin writes:

```text
<state_dir>/index.sqlite
<state_dir>/index.jsonl
<state_dir>/usage.sqlite
```

These are generated or local-only state. Do not commit them.

## Usage-history-informed behavior

This standalone shape keeps the lessons from the initial deployment:

- hyphenated human queries such as `manifest-backed backup` are split into safe SQLite FTS prefix terms;
- search ranking prefers exact/title/trigger hits so specific skills outrank generic helpers;
- operations/update wording is covered by artifact-level runbook search, not just script search;
- feedback and zero-result telemetry stays local and is summarized by `knowledge_usage_report` before changing ranking or source coverage;
- `knowledge_usage_report` separates live-root, pytest/probe, and other telemetry, suppresses resolved zero-result/negative-feedback candidates, and buckets legacy feedback ratings;
- both native tools and standalone CLI lookups write local usage events with plugin version, config source, index age/mtime, artifact counts by type, and build duration when a build occurs.

## CLI use

You can build/query without loading Hermes:

```bash
python -m hermes_local_knowledge.indexer build \
  --root ~/repos/<your-local-docs-or-customizations> \
  --hermes-home ~/.hermes \
  --output-dir ~/.hermes/local_knowledge

python -m hermes_local_knowledge.indexer search 'backup runbook' \
  --db ~/.hermes/local_knowledge/index.sqlite \
  --limit 8

python -m hermes_local_knowledge.indexer get skill:backup-runbook \
  --db ~/.hermes/local_knowledge/index.sqlite

python -m hermes_local_knowledge.indexer neighbors skill:backup-runbook \
  --db ~/.hermes/local_knowledge/index.sqlite
```

To match native plugin behavior, read `local_knowledge` settings from Hermes config instead of repeating flags:

```bash
python -m hermes_local_knowledge.indexer build --from-hermes-config
python -m hermes_local_knowledge.indexer search 'backup runbook' --from-hermes-config --limit 8
python -m hermes_local_knowledge.indexer evaluate --from-hermes-config --json
python -m hermes_local_knowledge.indexer evaluate --from-hermes-config --json --details
```

`evaluate` replays positive `usage.sqlite` feedback labels against the current
index and reports exact plus parent-equivalent top-k metrics. Parent-equivalent
metrics only relax `skill_support_doc` hits to their owning parent skill. Add
`--details` when you need per-query expected IDs, ranks, and top result IDs.

To compare search quality across historical git refs, use the development helper
from a source checkout:

```bash
python scripts/compare_historical_query_versions.py \
  --usage-db ~/.hermes/local_knowledge/usage.sqlite \
  v0.2.14 v0.2.18 WORKTREE
```

Use `WORKTREE` to evaluate the current working tree, including uncommitted code.
The helper builds an isolated index per ref, replays the supplied historical
feedback DB, and prints a metrics table by default. Use `--json --details` for
per-query output.

The CLI also has an install/config smoke check:

```bash
python -m hermes_local_knowledge.cli doctor
python -m hermes_local_knowledge.cli doctor --rebuild --query 'backup runbook'
```

## Development

```bash
python -m pip install -e '.[test]'
python -m pytest
python -m ruff check .
python -m mypy
python scripts/check_version_policy.py --base-ref origin/main
```

The version policy check keeps `plugin.yaml`, `pyproject.toml`, and
`hermes_local_knowledge/__init__.py` synchronized. CI also requires a version
bump when release-relevant plugin/package files change.

For optional mutation testing, install the mutation extra and run `mutmut` locally:

```bash
python -m pip install -e '.[test,mutation]'
python -m mutmut run
python -m mutmut results
```

`mutmut` is configured in `pyproject.toml` to mutate only `hermes_local_knowledge`, copy `plugin.yaml` into the mutant workspace for metadata tests, and skip the Hermes plugin install smoke test. Treat the full run as a slower local/scheduled quality check rather than a default fast-test gate; use `--max-children` or targeted mutant names when iterating locally.

Module layout:

- `indexer.py` and `plugin.py` are compatibility wrappers/public entry points.
- `scanners.py`, `storage.py`, `search.py`, and `cli.py` implement index collection, persistence, lookup, and CLI behavior.
- `schemas.py`, `runtime.py`, `telemetry.py`, and `handlers.py` implement Hermes plugin schemas, configuration, usage tracking, and tool handlers.
- `models.py`, `constants.py`, `paths.py`, `text_utils.py`, and `tooling.py` hold shared data structures/helpers.

The full test suite includes a Hermes plugin install smoke test. Install Hermes Agent first if `hermes` is not already on `PATH`:

```bash
python -m pip install hermes-agent
```

The tests verify:

- artifact scanning and SQLite/JSONL generation;
- state directory separation from source directory;
- configurable layout and entity hints;
- native Hermes plugin registration handlers;
- feedback/usage-report closed loop;
- config/env resolution.

## Repository hygiene and security

This repo includes baseline GitHub hygiene for a public reusable plugin:

- `LICENSE` with MIT terms;
- contributor guidance and a code of conduct;
- CI on Python 3.11 and 3.12;
- Dependabot config for GitHub Actions and Python packaging metadata, with update cooldowns;
- security scans for Gitleaks, actionlint, Semgrep, zizmor, ShellCheck, gated pip-audit, and CodeQL for public-repo code scanning;
- issue/PR templates and CODEOWNERS.

See [`CONTRIBUTING.md`](CONTRIBUTING.md), [`SECURITY.md`](SECURITY.md), and [`docs/github-security.md`](docs/github-security.md) for contribution expectations, vulnerability reporting, local validation, and manual GitHub settings.

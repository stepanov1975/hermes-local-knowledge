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

The plugin also has lifecycle hooks for **tool OKFs**: compact, generated routing notes for Hermes tools that have actually been used locally. The post-tool hook queues safe structural candidates, and the session-finalize hook can generate a bounded batch through Hermes' host-owned `ctx.llm` interface. Automatic OKF generation is a vital part of the plugin's intended functionality: without it, search still works, but the plugin cannot automatically create the tool-routing knowledge that lets coverage improve from real usage. Completing an OKF marks the index stale so the next normal lookup rebuilds it and makes the new `tool_okf` searchable.

## Install

From the public git repo:

```bash
hermes plugins install https://github.com/stepanov1975/hermes-local-knowledge.git --enable
```

SSH also works when your host has GitHub SSH keys configured:

```bash
hermes plugins install git@github.com:stepanov1975/hermes-local-knowledge.git --enable
```

### Required installer conversation: automatic OKF generation

Automatic OKF generation is a vital part of this plugin, but it is an explicit opt-in because it invokes the active model and consumes additional tokens. An AI agent performing an installation **must not declare the installation complete** until it has done all of the following:

1. Explain that functionality is degraded while `local_knowledge.okf.auto_generate` is disabled: lookup of existing artifacts still works, but the plugin will not automatically turn real tool usage into new tool-routing OKFs.
2. Explain that enabling it makes one bounded model call at session finalization, processes at most the configured candidate limit, and may extend finalization by up to `max_generation_seconds`.
3. Ask the user explicitly: **"Enable automatic OKF generation now? (Recommended; uses additional model tokens.)"**
4. Only after the user agrees, run:

   ```bash
   hermes config set local_knowledge.okf.enabled true
   hermes config set local_knowledge.okf.auto_generate true
   ```

If it is already enabled, report that fact instead of asking again. If the user declines, leave it disabled and clearly report that the installation is operating in degraded/manual-OKF mode.

### Install the routing skill

The plugin registers tools, but a skill tells Hermes **when** to use them. Without the routing skill, Hermes can still call `knowledge_search` when explicitly asked, but it is much less likely to use the router proactively for local runbooks, scripts, cron jobs, MCP wrappers, or custom skills.

Install the bundled skill into the target Hermes home/profile for proactive routing:

```bash
hermes local-knowledge install-router-skill
```

The command is cross-platform, installs the skill bundled with the current plugin version, and is idempotent. Use `--hermes-home <path>` to target a non-active profile. For machine-readable installer output, add `--json`; `installed` and `current` are successful states. If a different skill already exists, the command stops with `conflict` instead of overwriting user changes. Review the difference before using `--force`.

The plugin also registers this same skill as a read-only, namespaced plugin skill for explicit loads:

```text
skill_view("local_knowledge:local-knowledge-router")
```

That namespaced skill is useful as a versioned fallback/reference, but it does **not** appear in Hermes' normal available-skill index. Install the normal skill above when you want Hermes to use local knowledge proactively.

From a source checkout before the plugin is enabled, use the standalone fallback:

```bash
python -m hermes_local_knowledge.cli install-router-skill
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

The native tools auto-build the index when it is missing, and every lookup accepts `rebuild=true`, but normal searches reuse the existing `index.sqlite`. For a real install, schedule a rebuild cron job. This matters because skills, scripts, runbooks, cron jobs, MCP config, and completed tool OKFs can change outside the current session; without a scheduled rebuild, agents may route from stale metadata unless they remember to request `rebuild=true`.

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
  okf:
    enabled: true
    auto_generate: true
    max_candidates_per_session: 2
    max_generation_seconds: 120
    min_use_count: 1
```

`source_root` is the high-signal directory being indexed. `state_dir` is generated local state and should not be committed. `exclude_dir_names` adds extra directory names to the built-in skip list (`.archive`, `worktrees`, `.worktrees`, `.git`, `__pycache__`, `node_modules`, `venv`, `.venv`, `.mypy_cache`, `.pytest_cache`, `htmlcov`, `logs`). Use YAML lists in `config.yaml`; when using `hermes config set` from the shell, comma-separated strings or bracket-list strings are accepted and normalized by the plugin.

`local_knowledge.okf.enabled` controls whether the plugin records safe, structural tool-use candidates. Full functionality requires `local_knowledge.okf.auto_generate: true` in Hermes config. With it disabled, lookup of existing artifacts still works, but the plugin is degraded because it cannot automatically create new tool-routing OKFs from real usage. The runtime default remains intentionally `false` so installation does not silently consume model tokens. The installer must follow the explicit disclosure-and-consent conversation in the Install section before changing it. If the user declines, leave `auto_generate` disabled and report that the installation is operating in degraded/manual-OKF mode.

When enabled, `on_session_finalize` claims at most `max_candidates_per_session` candidates and makes one bounded `ctx.llm.complete_structured` call with `max_generation_seconds` as its timeout. The plugin renders and validates the files itself; the model never receives terminal or file tools. The post-tool hook uses Hermes' canonical outcome fields and does not persist raw session transcripts, raw tool outputs, argument values, emails, OCR text, or private documents.

For compatibility with v0.3.0 configuration, `max_worker_seconds` is still accepted as a fallback when `max_generation_seconds` is not set.

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
| `tool_okf` | Generated OKF Markdown files under `<state_dir>/okfs/tools/*.md` |
| `cron_job` | `$HERMES_HOME/cron/jobs.json` |
| `mcp_server` | `$HERMES_HOME/config.yaml` `mcp_servers` entries, plus legacy `mcp.servers` entries |

## Generated state

The plugin writes:

```text
<state_dir>/index.sqlite
<state_dir>/index.jsonl
<state_dir>/usage.sqlite
<state_dir>/okf_queue.sqlite
<state_dir>/okfs/tools/*.md
<state_dir>/okf_generation.lock
<state_dir>/index_build.lock
<state_dir>/okf_index_dirty/ (possibly empty)
```

These are generated or local-only state. Do not commit them.

## Tool OKF generation

Tool OKFs are small routing artifacts for tools Hermes has actually used. They are not full skills and are not generated from raw tool output. The queue stores only privacy-safe metadata: tool name, toolset, a sanitized schema view, counters, error class, and argument shape. Schema values such as defaults, examples, descriptions, titles, summaries, `$comment`, and secret-like text are redacted before being stored or sent to the host-owned structured LLM call.

Manual inspection/drain workflow:

```bash
python -m hermes_local_knowledge.cli okf status --from-hermes-config --json
python -m hermes_local_knowledge.cli okf claim --from-hermes-config --limit 1 --json
python -m hermes_local_knowledge.cli okf validate --from-hermes-config \
  --claim-token <token> \
  --path ~/.hermes/local_knowledge/okfs/tools/<tool>.md \
  --json
python -m hermes_local_knowledge.cli okf complete --from-hermes-config \
  --claim-token <token> \
  --tool <tool> \
  --path ~/.hermes/local_knowledge/okfs/tools/<tool>.md \
  --json
```

Use `python -m hermes_local_knowledge.cli okf fail --from-hermes-config --claim-token <token> --tool <tool> --error <short-redacted-error>` to release a failed manual claim. Candidates move to terminal `error` state after the retry cap. `okf status` lists those candidates under `errors`; reset one for another bounded generation attempt with:

```bash
python -m hermes_local_knowledge.cli okf retry --from-hermes-config \
  --tool <tool> --json
```

`retry` only accepts terminal-error candidates. It clears generation-attempt state but preserves tool usage counters and schema metadata.

The validator requires generated OKFs to live under `<state_dir>/okfs/tools`, use `.md`, declare `artifact_type: tool_okf`, match the claimed tool/schema hash/target path, contain useful routing aliases or triggers, and avoid obvious secret assignments. Completing an OKF adds a token under `okf_index_dirty/`; the next normal lookup rebuilds the index and removes only the tokens covered by that successful build. Tokens added concurrently remain for the following lookup. Index rebuilds are serialized through a kernel advisory lock on `index_build.lock`, so an older scan cannot overwrite a newer index and consume its dirty tokens. The lock file may remain while idle; the kernel releases ownership automatically if a builder exits.

## Usage-history-informed behavior

This standalone shape keeps the lessons from the initial deployment:

- hyphenated human queries such as `manifest-backed backup` are split into safe SQLite FTS prefix terms;
- search ranking prefers exact/title/trigger hits so specific skills outrank generic helpers;
- operations/update wording is covered by artifact-level runbook search, not just script search;
- feedback and zero-result telemetry stays local and is summarized by `knowledge_usage_report` before changing ranking or source coverage;
- `knowledge_usage_report` separates live-root, pytest/probe, and other telemetry, suppresses resolved zero-result/negative-feedback candidates, and buckets legacy feedback ratings;
- both native tools and standalone CLI lookups write local usage events with plugin version, config source, index age/mtime, artifact counts by type, and build duration when a build occurs.
- OKF hooks record safe structural tool-use candidates and generate a bounded batch through `ctx.llm` at session finalization when the explicitly consented `local_knowledge.okf.auto_generate` setting is enabled.

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
hermes local-knowledge doctor
hermes local-knowledge doctor --json
hermes local-knowledge doctor --rebuild --query 'backup runbook'
```

`doctor` reports nonfatal warnings when the proactive router skill is missing or differs from the bundled version, and when automatic OKF generation is disabled. An installer agent must treat the latter as degraded/manual-OKF mode, explain the impact and bounded model cost, and ask for consent as specified in the Install section before declaring setup complete.

When running directly from an uninstalled source checkout, replace `hermes local-knowledge` with `python -m hermes_local_knowledge.cli`.

The OKF queue can also be managed from the same CLI:

```bash
python -m hermes_local_knowledge.cli okf status --from-hermes-config --json
python -m hermes_local_knowledge.cli okf claim --from-hermes-config --limit 1 --json
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
- tool OKF queueing, privacy redaction, host-owned structured generation, validation, and indexing;
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

# local_knowledge installed

Enable the plugin if you did not pass `--enable`:

```bash
hermes plugins enable local_knowledge
```

Install the routing skill too. The plugin registers the `knowledge_*` tools, but a normal installed skill tells Hermes when to use them proactively for local runbooks, scripts, cron jobs, MCP wrappers, and custom skills:

```bash
hermes local-knowledge install-router-skill
```

The command is cross-platform, installs the skill bundled with this plugin version, and is safe to rerun. An AI installer should add `--json` and treat `installed` or `current` as success. If it reports `conflict`, stop and review the existing skill; do not use `--force` unless replacing that customized file is intentional.

The plugin also registers the same file as the read-only namespaced skill `local_knowledge:local-knowledge-router` for explicit `skill_view(...)` loads. That does not replace installing the normal skill above, because plugin skills are not in the proactive available-skill index.

After adding the skill, start a fresh Hermes session or run `/reload-skills` and then `/new`/`/reset` so the router instructions enter the prompt.

Recommended pattern: set `source_root` to a high-signal local operational/customization repo (runbooks, helper scripts, custom skills). The plugin still indexes runtime `$HERMES_HOME/skills`, cron jobs, and MCP config separately, so `source_root` does not need to be the whole Hermes home.

Recommended config in `~/.hermes/config.yaml`:

```yaml
local_knowledge:
  source_root: ~/repos/<your-local-docs-or-customizations>  # high-signal directory to index
  state_dir: ~/.hermes/local_knowledge                      # generated sqlite/jsonl/usage state
  custom_skill_dirs: [custom_skills]                         # YAML list
  script_dirs: [scripts, hermes_home/scripts]                # YAML list
  include_markdown_docs: true
  exclude_dir_names: [build, dist]                            # extra dirs to skip (merged with built-in defaults)
  okf:
    enabled: true
    auto_generate: true                                      # full automatic OKF functionality; uses model tokens
```

Full functionality includes automatic tool-OKF generation and therefore requires `local_knowledge.okf.auto_generate: true`. The runtime default is intentionally `false` so installation does not silently consume model tokens. Before enabling it, an installer—especially an AI agent performing the installation—must warn the user that automatic generation invokes the active model at session finalization, consumes additional tokens, and can delay finalization by up to the configured generation timeout. If the user does not want that additional usage, leave `auto_generate` disabled; the core knowledge tools and manual OKF workflow remain available, but automatic generation will not run.

CLI-safe equivalent. `hermes config set` stores scalar strings; the plugin accepts comma-separated values for list-like settings:

```bash
hermes config set local_knowledge.source_root "$HOME/repos/your-local-docs-or-customizations"
hermes config set local_knowledge.state_dir "$HOME/.hermes/local_knowledge"
hermes config set local_knowledge.custom_skill_dirs custom_skills
hermes config set local_knowledge.script_dirs scripts,hermes_home/scripts
hermes config set local_knowledge.include_markdown_docs true
hermes config set local_knowledge.exclude_dir_names build,dist
hermes config set local_knowledge.okf.enabled true
hermes config set local_knowledge.okf.auto_generate true
```

You can omit `source_root` to index only this Hermes profile's runtime artifacts under `$HERMES_HOME`. If `$HERMES_HOME/hermes-agent` exists, the plugin warns because broad Hermes-home indexing can be noisy.

Create a scheduled rebuild for the index. The tools rebuild automatically only when the database is missing or a lookup uses `rebuild=true`; normal searches reuse the existing index. A cron rebuild keeps local skills, scripts, runbooks, cron jobs, and MCP config fresh for agents that do not know the source tree changed.

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

Successful runs are silent because the script prints nothing; failures still produce a cron alert.

Smoke check the install/config. CLI commands write to the same local `usage.sqlite` telemetry store, so smoke checks show up in `knowledge_usage_report` alongside native tool calls:

```bash
hermes local-knowledge doctor
hermes local-knowledge doctor --json
hermes local-knowledge doctor --rebuild --query "backup runbook"
```

`doctor` keeps missing/full-function options nonfatal, but reports whether the proactive router skill is installed and current and whether automatic OKF generation is enabled. Installer agents should resolve or explicitly explain these warnings before declaring setup complete.

Restart the gateway or start a new Hermes session for the tools to appear.
If you are already talking to Hermes through the gateway, use `/restart`; from a separate shell, run:

```bash
hermes gateway restart
```

For public installs, HTTPS does not require GitHub SSH keys:

```bash
hermes plugins install https://github.com/stepanov1975/hermes-local-knowledge.git --enable
```

SSH also works when your host has GitHub SSH keys configured:

```bash
hermes plugins install git@github.com:stepanov1975/hermes-local-knowledge.git --enable
```

The plugin provides `knowledge_search`, `knowledge_get`, `knowledge_neighbors`, `knowledge_feedback`, and `knowledge_usage_report`.

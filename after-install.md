# local_knowledge installed

Enable the plugin if you did not pass `--enable`:

```bash
hermes plugins enable local_knowledge
```

Install the routing skill too. The plugin registers the `knowledge_*` tools, but a skill tells Hermes when to use them proactively for local runbooks, scripts, cron jobs, MCP wrappers, and custom skills:

```bash
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
mkdir -p "$HERMES_HOME/skills/local-knowledge-router"
cp "$HERMES_HOME/plugins/local_knowledge/examples/local-knowledge-router-skill/SKILL.md" \
  "$HERMES_HOME/skills/local-knowledge-router/SKILL.md"
```

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
```

CLI-safe equivalent. `hermes config set` stores scalar strings; the plugin accepts comma-separated values for list-like settings:

```bash
hermes config set local_knowledge.source_root "$HOME/repos/your-local-docs-or-customizations"
hermes config set local_knowledge.state_dir "$HOME/.hermes/local_knowledge"
hermes config set local_knowledge.custom_skill_dirs custom_skills
hermes config set local_knowledge.script_dirs scripts,hermes_home/scripts
hermes config set local_knowledge.include_markdown_docs true
```

You can omit `source_root` to index only this Hermes profile's runtime artifacts under `$HERMES_HOME`. If `$HERMES_HOME/hermes-agent` exists, the plugin warns because broad Hermes-home indexing can be noisy.

Smoke check the install/config. CLI commands write to the same local `usage.sqlite` telemetry store, so smoke checks show up in `knowledge_usage_report` alongside native tool calls:

```bash
python -m hermes_local_knowledge.cli doctor
python -m hermes_local_knowledge.cli doctor --rebuild --query "backup runbook"
```

Restart the gateway or start a new Hermes session for the tools to appear.
If you are already talking to Hermes through the gateway, use `/restart`; from a separate shell, run:

```bash
hermes gateway restart
```

For private GitHub repos, install with SSH, for example:

```bash
hermes plugins install git@github.com:stepanov1975/hermes-local-knowledge.git --enable
```

The plugin provides `knowledge_search`, `knowledge_get`, `knowledge_neighbors`, `knowledge_feedback`, and `knowledge_usage_report`.

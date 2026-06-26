# local_knowledge installed

Enable the plugin if you did not pass `--enable`:

```bash
hermes plugins enable local_knowledge
```

Recommended config in `~/.hermes/config.yaml`:

```yaml
local_knowledge:
  source_root: ~/repos/<your-local-docs-or-customizations>  # directory to index
  state_dir: ~/.hermes/local_knowledge                      # generated sqlite/jsonl/usage state
  custom_skill_dirs: [skills]                               # optional; defaults to custom_skills
```

CLI-safe equivalent:

```bash
hermes config set local_knowledge.source_root "$HOME/repos/your-local-docs-or-customizations"
hermes config set local_knowledge.state_dir "$HOME/.hermes/local_knowledge"
hermes config set local_knowledge.custom_skill_dirs skills
hermes config set local_knowledge.include_markdown_docs true
```

You can omit `source_root` to index only this Hermes profile's runtime artifacts under `$HERMES_HOME`.

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

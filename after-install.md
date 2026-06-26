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
```

You can omit `source_root` to index only this Hermes profile's runtime artifacts under `$HERMES_HOME`.

Restart the gateway or start a new Hermes session for the tools to appear.

```bash
hermes gateway restart
```

The plugin provides `knowledge_search`, `knowledge_get`, `knowledge_neighbors`, `knowledge_feedback`, and `knowledge_usage_report`.

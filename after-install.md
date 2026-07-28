# `local_knowledge` installed

## 1. Enable the plugin

Skip this if installation used `--enable`:

```bash
hermes plugins enable local_knowledge
```

## 2. Obtain explicit model-token consent

Existing-artifact lookup works with automatic tool-OKF generation disabled. Automatic generation is off by default because a detached worker invokes the active Hermes model and consumes additional tokens.

Before changing the setting, explain that one worker claims at most `max_candidates_per_session` candidates (default `2`), makes one structured batch call when it has claims, and passes `max_generation_seconds` as the provider-request timeout. Provider retry/fallback policy can extend elapsed time and token use behind that host call, but session finalization does not wait for the worker.

Ask:

> Enable automatic OKF generation now? (Recommended; uses additional model tokens.)

Only after the user agrees:

```bash
hermes config set local_knowledge.okf.enabled true
hermes config set local_knowledge.okf.auto_generate true
```

If it is already enabled, report that. If the user declines, leave `auto_generate` disabled and report that search and manual OKF management work, but new routing notes will not be generated automatically.

## 3. Install the proactive router skill

```bash
hermes local-knowledge install-router-skill --json
```

`installed` and `current` are successful. If the result is `conflict`, review the existing skill before using `--force`.

The namespaced plugin skill `local_knowledge:local-knowledge-router` is available for explicit loads, but it is not in the proactive skill index and does not replace the normal installed skill.

## 4. Configure source and state

Use a high-signal local operations/customization tree as `source_root`; runtime skills, cron jobs, and MCP configuration are still read separately from `$HERMES_HOME`. Keep `state_dir` outside source control.

```yaml
local_knowledge:
  source_root: ~/repos/local-operations
  state_dir: ~/.hermes/local_knowledge
  custom_skill_dirs: [custom_skills]
  script_dirs: [scripts, hermes_home/scripts]
  include_markdown_docs: true
  exclude_dir_names: [build, dist]
  okf:
    enabled: true
    auto_generate: false  # set true only after the consent step
```

CLI-safe scalar/list form:

```bash
hermes config set local_knowledge.source_root "$HOME/repos/local-operations"
hermes config set local_knowledge.state_dir "$HOME/.hermes/local_knowledge"
hermes config set local_knowledge.custom_skill_dirs custom_skills
hermes config set local_knowledge.script_dirs scripts,hermes_home/scripts
hermes config set local_knowledge.include_markdown_docs true
hermes config set local_knowledge.exclude_dir_names build,dist
```

If `source_root` is omitted, it defaults to `$HERMES_HOME`; arbitrary root-level Markdown is then excluded by default to avoid a noisy broad scan.

## 5. Run the doctor

```bash
hermes local-knowledge doctor --json
hermes local-knowledge doctor --rebuild --query "backup runbook"
```

The doctor treats a missing/outdated router skill and disabled automatic OKF generation as nonfatal warnings. Resolve them or report the deliberate choice before calling installation complete.

From an unregistered source checkout, use:

```bash
python -m hermes_local_knowledge.cli doctor \
  --hermes-home "${HERMES_HOME:-$HOME/.hermes}" \
  --rebuild \
  --query "backup runbook"
```

## 6. Refresh explicitly when ordinary sources change

Managed lookups rebuild a missing, corrupt, older-format, or OKF-dirty index. They do not detect ordinary source-file, cron-registry, or MCP-config changes.

After such a change, pass `rebuild=true` to a native lookup or run:

```bash
python -m hermes_local_knowledge.cli build --from-hermes-config
```

No cron job is required. If an operator needs a fixed freshness interval, that explicit build command may be scheduled as an optional local policy.

## 7. Reload only what changed

- After installing/changing the router skill: run `/reload-skills`, then `/new` or `/reset`, or start a fresh session.
- After first enabling the plugin or updating loaded plugin code: restart the gateway from outside its running process, or use `/restart` from gateway chat.
- Configuration edits and index rebuilds do not by themselves require a gateway restart.

The plugin provides `knowledge_search`, `knowledge_get`, `knowledge_neighbors`, `knowledge_feedback`, and `knowledge_usage_report`.

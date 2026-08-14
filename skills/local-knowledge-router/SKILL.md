---
name: local-knowledge-router
description: Route local operational questions to indexed skills, scripts, runbooks, cron jobs, MCP servers, and docs before guessing paths or doing broad file search.
version: 1.0.1
author: Hermes Local Knowledge
license: MIT
requires_toolsets: [local_knowledge]
metadata:
  hermes:
    tags: [knowledge, routing, discovery, local-artifacts]
---

# Local Knowledge Router

## When to use

Use this skill before guessing paths or doing broad file searches when a user asks about local or private operational knowledge, including:

- installed or custom Hermes skills and their support docs;
- generated tool OKFs, runbooks, operational docs, and memory docs;
- helper scripts and automation entry points;
- cron jobs and scheduled maintenance;
- MCP servers and wrapper scripts;
- service workflows in the configured local source tree.

The plugin indexes whole artifacts. It identifies the first artifact to inspect; it does not replace reading that source.

## Workflow

1. Search for the user's intent:

   ```text
   knowledge_search(query="backup runbook", limit=8)
   knowledge_search(query="paperless review automation", limit=8)
   knowledge_search(query="siyuan mcp wrapper", limit=8)
   ```

2. Fetch the best artifact before acting:

   ```text
   knowledge_get(artifact_id="skill:example-skill", include_neighbors=true)
   ```

   Use `knowledge_neighbors` separately when graph context will help:

   ```text
   knowledge_neighbors(artifact_id="cron:daily-review", limit=20)
   ```

3. Inspect the routed source of truth:

   - `skill` → load it with `skill_view`.
   - `script` → read the script and help text before running it.
   - `tool_okf` → use it as routing context, then inspect the live tool schema/docs before high-impact calls.
   - `runbook`, `memory_doc`, `doc`, or `skill_support_doc` → read the file before changing systems.
   - `cron_job` → verify the live cron registry before mutating jobs.
   - `mcp_server` → inspect the live wrapper/config before troubleshooting.

4. Check freshness when it matters. Managed lookups rebuild an index that is missing, corrupt, older-format, or marked dirty by completed tool-OKF publication. They do **not** detect ordinary source-file, cron-registry, or MCP-config changes. When those changed recently or results look stale, force a rebuild:

   ```text
   knowledge_search(query="new helper script", limit=8, rebuild=true)
   ```

   `knowledge_get` and `knowledge_neighbors` also accept `rebuild=true`. An operator may optionally schedule the plugin's explicit CLI build command to enforce a freshness interval, but no schedule is required.

5. If mixed results look weak, retry once with a shorter core-intent query and the likely `artifact_type`. Current plugin revisions can perform one such retry automatically when a matching explicit `useful` route exists and its accepted query is no longer than the current query; the remembered artifact still has to be rediscovered in the live index. A newer matching rejection vetoes an older overlap route. When opt-in implicit feedback is enabled, mature same-turn evidence may supply a lower-priority route under the same safeguards: current-index promotion or one verified typed retry. Matching explicit routes take precedence.

6. Record clear lookup outcomes:

   ```text
   knowledge_feedback(event_id=<usage_event_id>, rating="useful", artifact_id="skill:example-skill")
   knowledge_feedback(query="missing workflow phrase", rating="missing", note="Expected the deployment runbook")
   ```

## Pitfalls

- Search results are routing hints, not proof; read the artifact before relying on it.
- If a query returns no results, retry once with broader domain synonyms before broad repository search.
- Do not include secrets or private document text in queries or feedback notes; telemetry is local but persistent.
- Treat generated `tool_okf` files as compact routing hints, not proof of current tool behavior.
- After installing this skill or enabling the plugin, start a fresh session (or reload skills and reset) so the toolset and instructions enter the prompt.

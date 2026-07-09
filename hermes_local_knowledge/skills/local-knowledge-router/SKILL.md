---
name: local-knowledge-router
description: Route local operational questions to indexed skills, scripts, runbooks, cron jobs, MCP servers, and docs before guessing paths or doing broad file search.
version: 1.0.0
author: Hermes Local Knowledge
license: MIT
requires_toolsets: [local_knowledge]
metadata:
  hermes:
    tags: [knowledge, routing, discovery, local-artifacts]
---

# Local Knowledge Router

## When to use

Use this skill before guessing paths or doing broad file searches when the user asks about local or private operational knowledge, including:

- installed/custom Hermes skills;
- generated tool OKFs, runbooks, operational docs, and memory docs;
- helper scripts and automation entry points;
- cron jobs and scheduled maintenance;
- MCP servers and wrapper scripts;
- service-specific workflows documented in the configured local knowledge source tree.

The local knowledge plugin indexes whole artifacts. Its purpose is to identify the first artifact the agent should inspect, not to replace reading the artifact itself.

## Workflow

1. Search for the user's intent with the native tool:

   ```text
   knowledge_search(query="backup runbook", limit=8)
   knowledge_search(query="paperless review automation", limit=8)
   knowledge_search(query="siyuan mcp wrapper", limit=8)
   ```

2. Fetch the best matching artifact before acting:

   ```text
   knowledge_get(artifact_id="skill:example-skill", include_neighbors=true)
   ```

3. Inspect the routed source of truth:

   - `skill` → load with `skill_view`.
   - `script` → read the script and help text before running it.
   - `tool_okf` → use it as routing context for when/how to use a tool, then inspect the live tool schema/docs before high-impact calls.
   - `runbook`, `memory_doc`, `doc`, or `skill_support_doc` → read the file before changing systems.
   - `cron_job` → verify the live cron registry before mutating jobs.
   - `mcp_server` → inspect the wrapper/config before troubleshooting MCP behavior.

4. Do not assume the index is fresh. The plugin auto-builds only when the database is missing; normal lookups reuse the existing index unless you pass `rebuild=true`. Use `rebuild=true` when relevant files changed recently or the index looks stale:

   ```text
   knowledge_search(query="new helper script", limit=8, rebuild=true)
   ```

   During installation, set up the `local_knowledge index rebuild` cron job from the plugin README/after-install notes so future agents route from current skills, scripts, runbooks, cron jobs, MCP config, and completed tool OKFs.

5. Record lookup quality when it is clear:

   ```text
   knowledge_feedback(event_id=<usage_event_id>, rating="useful", artifact_id="skill:example-skill")
   knowledge_feedback(query="missing workflow phrase", rating="missing", note="Expected the deployment runbook")
   ```

## Pitfalls

- Do not treat search results as proof. They are routing hints; read the artifact before relying on it.
- If a query returns no results, retry once with broader synonyms before falling back to broad repository search.
- Do not include secrets in queries or feedback notes; telemetry is stored locally.
- Treat generated `tool_okf` artifacts as routing hints only. They are compact aliases/triggers/pitfalls, not proof of current tool behavior.
- After installing this skill or enabling the plugin, restart Hermes or start a fresh session so the toolset and skill instructions enter the prompt.

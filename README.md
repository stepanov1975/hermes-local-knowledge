# Hermes Local Knowledge

[![CI](https://github.com/stepanov1975/hermes-local-knowledge/actions/workflows/ci.yml/badge.svg)](https://github.com/stepanov1975/hermes-local-knowledge/actions/workflows/ci.yml)
[![Security scans](https://github.com/stepanov1975/hermes-local-knowledge/actions/workflows/security.yml/badge.svg)](https://github.com/stepanov1975/hermes-local-knowledge/actions/workflows/security.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

## Goal

Hermes Local Knowledge is a reusable Hermes Agent plugin that routes a local question to the **whole artifact** the agent should inspect first: a skill, support document, script, runbook, cron job, MCP server, or generated tool routing note.

It is an artifact router, not chunk RAG. Search results are routing hints; the agent should still read the selected source before acting.

## Sources

`source_root` is the curated local tree to index. `$HERMES_HOME` remains a separate runtime source for skills, cron jobs, and MCP configuration.

| Artifact type | Source |
| --- | --- |
| `skill` | `SKILL.md` files under configured `custom_skill_dirs`, plus `$HERMES_HOME/skills` |
| `skill_support_doc` | Markdown support files under custom skills and runtime skill `references/`, `templates/`, `scripts/`, and `assets/` directories |
| `script` | Supported script files under configured `script_dirs` |
| `memory_doc`, `runbook`, `doc` | Markdown under `source_root` when `include_markdown_docs` is enabled; configured memory/runbook directories determine the type |
| `cron_job` | `$HERMES_HOME/cron/jobs.json` |
| `mcp_server` | `$HERMES_HOME/config.yaml` entries under `mcp_servers` or the accepted `mcp.servers` form |
| `tool_okf` | Validated generated notes under `<state_dir>/okfs/tools/*.md` |

Built-in excluded directory names are `.archive`, `.git`, `.mypy_cache`, `.pytest_cache`, `.venv`, `.worktrees`, `__pycache__`, `htmlcov`, `logs`, `node_modules`, `venv`, and `worktrees`. `exclude_dir_names` adds deployment-specific exclusions.

## Five tools

The plugin registers these native tools in the `local_knowledge` toolset:

| Tool | Purpose |
| --- | --- |
| `knowledge_search` | Find likely artifacts by intent, with an optional type filter and forced rebuild. |
| `knowledge_get` | Fetch one artifact by ID, optionally with graph neighbors. |
| `knowledge_neighbors` | Traverse conservative graph edges for an artifact. |
| `knowledge_feedback` | Record local lookup feedback such as `useful`, `missing`, `stale`, or `wrong_artifact`. |
| `knowledge_usage_report` | Summarize local usage, failures, zero-result queries, and feedback. |

The plugin also registers `pre_llm_call`, `post_tool_call`, `on_session_end`, and `on_session_finalize` hooks. When the configured source is available, Hermes hosts with cache-safe plugin prompt sections add one concise system-prompt hint for each new session: use `knowledge_search` for local tools and information. Older hosts use a deduplicated `pre_llm_call` hint as a compatibility fallback. In either case, `pre_llm_call` continues to bind optional same-turn implicit search-result feedback; the remaining lifecycle hooks support exact successful consumption through `knowledge_get`, `skill_view`, or `read_file` plus tool-OKF capture/generation while keeping correlation inside the plugin when Hermes dispatches local-knowledge tools through its deferred-tool bridge.

Implicit feedback is disabled by default. In a private controlled installation, it can learn a routing hint when the same artifact is consumed after distinct matching searches, with each supported consumer tied to that search's unassisted baseline in the same Hermes session, task, and turn:

```yaml
local_knowledge:
  implicit_feedback:
    enabled: true
    min_confirmations: 2
    max_generic_queries: 5
```

Only a recent search from the same Hermes session, task, and turn is eligible. `skill_view` and `read_file` must run in a later model request, after the search result is available, and count only when their successful result resolves by canonical source path to exactly one baseline artifact in the same current index snapshot. Repeated consumption from one search is deduplicated, and overly generic artifacts stop receiving implicit promotion. Explicit feedback remains authoritative, and implicit evidence is not used as evaluation ground truth.

## Install and model consent

Install and enable the directory plugin:

```bash
hermes plugins install https://github.com/stepanov1975/hermes-local-knowledge.git --enable
```

An SSH URL is also supported on hosts with GitHub SSH access:

```bash
hermes plugins install git@github.com:stepanov1975/hermes-local-knowledge.git --enable
```

The Python package exposes the `local_knowledge = hermes_local_knowledge.plugin` entry point for environments that install plugins into Hermes' Python environment.

### Automatic tool-OKF generation is opt-in

Existing-artifact lookup works without model calls. `local_knowledge.okf.auto_generate` defaults to `false`; while it is off, safe tool-use candidates can be recorded but are not automatically converted into new routing notes.

Before enabling automatic generation, an installer must explain that:

- a detached worker invokes the active Hermes model and consumes additional model tokens;
- one worker claims at most `max_candidates_per_session` candidates (default `2`) and makes one structured batch call when it has claims;
- `max_generation_seconds` is passed as the provider-request timeout, while provider retry/fallback policy can extend total elapsed time and token use behind that host call;
- session finalization does not wait for generation.

Then ask explicitly:

> Enable automatic OKF generation now? (Recommended; uses additional model tokens.)

Only after the user agrees, run:

```bash
hermes config set local_knowledge.okf.enabled true
hermes config set local_knowledge.okf.auto_generate true
```

If it is already enabled, report that instead of asking again. If the user declines, leave `auto_generate` disabled and report that existing lookup and manual OKF management remain available, but new tool-routing notes will not be generated automatically.

### Install the proactive router skill

The per-turn hook makes the primary search tool discoverable without loading a full routing skill. The bundled skill adds the detailed search/fetch/inspect workflow, freshness guidance, and feedback procedure:

```bash
hermes local-knowledge install-router-skill --json
```

`installed` and `current` are successful statuses. A different existing skill produces `conflict`; review it before choosing `--force`.

To use an intentionally customized proactive skill instead, configure its deployed runtime path before running doctor or installer commands:

```yaml
local_knowledge:
  router_skill_path: skills/note-taking/local-knowledge-router/SKILL.md
```

Relative paths resolve from `$HERMES_HOME`. The path must name an active `SKILL.md` under `$HERMES_HOME/skills`; that runtime path may be a symlink to a separately managed custom-skill repository. Doctor validates the skill's frontmatter name but does not require custom content to match the bundled copy. Invalid or missing configured custom skills appear as explicit doctor warnings while doctor retains its diagnostic-success exit status; the installer fails without modifying the configured target. `install-router-skill`, including `--force`, returns `current` with `router_skill_mode: custom` and does not overwrite a valid configured custom skill.

From a source checkout before plugin CLI registration is available:

```bash
python -m hermes_local_knowledge.cli install-router-skill --json
```

The plugin also exposes the same file as `local_knowledge:local-knowledge-router` for explicit `skill_view(...)` loads. That namespaced copy is not a substitute for installing the normal proactive skill. After installing or changing the skill, run `/reload-skills` and start a new/reset session, or start a fresh Hermes session.

## Configuration

Put non-secret settings in `$HERMES_HOME/config.yaml`:

```yaml
local_knowledge:
  source_root: ~/repos/local-operations
  state_dir: ~/.hermes/local_knowledge
  # Optional: use a deployed custom proactive skill instead of the bundled copy.
  # router_skill_path: skills/note-taking/local-knowledge-router/SKILL.md
  custom_skill_dirs: [custom_skills]
  script_dirs: [scripts, hermes_home/scripts]
  memory_dirs: [memory]
  runbook_dirs: [docs]
  include_markdown_docs: true
  exclude_dir_names: [build, dist]
  known_entities: [Hermes, GitHub, MCP, Cron]
  okf:
    enabled: true
    auto_generate: false  # change only after explicit model-token consent
    max_candidates_per_session: 2
    max_generation_seconds: 120
    min_use_count: 1
```

Canonical settings, aliases, and defaults:

| Canonical setting | Accepted alias/override | Default |
| --- | --- | --- |
| `source_root` | `root`; `LOCAL_KNOWLEDGE_ROOT` overrides config | `$HERMES_HOME` |
| `state_dir` | `index_dir`; `LOCAL_KNOWLEDGE_STATE_DIR` overrides config | `$HERMES_HOME/local_knowledge` |
| `hermes_home` | `HERMES_HOME`; explicit CLI `--hermes-home` selects a profile | Active Hermes home, otherwise `~/.hermes` |
| `router_skill_path` | — | `$HERMES_HOME/skills/local-knowledge-router/SKILL.md`; an explicit relative path resolves from `$HERMES_HOME` and selects custom-skill validation |
| `custom_skill_dirs` | — | `[custom_skills]` |
| `script_dirs` | — | `[scripts, hermes_home/scripts]` |
| `memory_dirs` | — | `[memory]` |
| `runbook_dirs` | — | `[docs]` |
| `known_entities` | `entities` | `[Hermes, GitHub, MCP, Cron]` |
| `include_markdown_docs` | — | `true` with an explicit source root; `false` when the root falls back to `$HERMES_HOME` |
| `exclude_dir_names` | — | `[]`, merged with built-in exclusions |
| `okf.enabled` | flat `okf_enabled` | `true` |
| `okf.auto_generate` | flat `okf_auto_generate` | `false` |
| `okf.max_candidates_per_session` | flat `okf_max_candidates_per_session` | `2` |
| `okf.max_generation_seconds` | flat `okf_max_generation_seconds`; `okf.max_worker_seconds` or flat `okf_max_worker_seconds` is a fallback when it is absent | `120` seconds |
| `okf.min_use_count` | flat `okf_min_use_count` | `1` |
| `implicit_feedback.enabled` | — | `false` |
| `implicit_feedback.min_confirmations` | — | `2` |
| `implicit_feedback.max_generic_queries` | — | `5` |

All nested `okf` keys also accept their flat `okf_*` form. YAML lists are preferred in `config.yaml`; comma-separated or bracket-list strings written by `hermes config set` are normalized.

Each invocation follows Hermes' context-local active profile, including hosts that keep multiple profile managers in one process. By default, each profile therefore uses its own config, runtime artifacts, index, telemetry, and learned feedback under `$HERMES_HOME/local_knowledge`. A shared `source_root` is safe when profiles should search the same curated tree, but keep `state_dir` profile-specific unless combined telemetry and feedback are explicitly intended. Process-level `LOCAL_KNOWLEDGE_ROOT` and `LOCAL_KNOWLEDGE_STATE_DIR` overrides still apply to every profile in that process.

When `source_root` is omitted, runtime skills, cron jobs, and MCP configuration are still indexed from `$HERMES_HOME`, but arbitrary root-level Markdown is not included by default. Generated state belongs outside a source repository and must not be committed.

## CLI

The primary standalone entry point is:

```bash
python -m hermes_local_knowledge.cli build --from-hermes-config
python -m hermes_local_knowledge.cli search 'backup runbook' --from-hermes-config --limit 8
python -m hermes_local_knowledge.cli get skill:backup-runbook --from-hermes-config --json
python -m hermes_local_knowledge.cli neighbors skill:backup-runbook --from-hermes-config --json
python -m hermes_local_knowledge.cli evaluate --from-hermes-config --json --details
python -m hermes_local_knowledge.cli okf status --from-hermes-config --json
python -m hermes_local_knowledge.cli doctor --hermes-home "${HERMES_HOME:-$HOME/.hermes}" --rebuild --query 'backup runbook'
```

Use `python -m hermes_local_knowledge.cli <command> --help` for manual OKF `claim`, `validate`, `complete`, `fail`, and `retry` operations.

`python -m hermes_local_knowledge.indexer ...` is preserved as a compatibility entry point to the same CLI. New documentation and automation should use `hermes_local_knowledge.cli`.

The installed `hermes local-knowledge` surface is intentionally smaller:

```bash
hermes local-knowledge install-router-skill
hermes local-knowledge doctor --rebuild --query 'backup runbook'
```

The host also uses an internal bounded worker subcommand for automatic OKF generation.

### Index freshness

Managed native lookups, and CLI `search`/`get`/`neighbors` with `--from-hermes-config` and no explicit `--db`, rebuild when the index is missing, corrupt, older than format 4, or marked dirty by completed OKF publication. A newer index format is rejected rather than overwritten.

Ordinary source-file, cron-registry, or MCP-config changes are **not** detected automatically. After those changes, either:

- pass `rebuild=true` to `knowledge_search`, `knowledge_get`, or `knowledge_neighbors`; or
- run `python -m hermes_local_knowledge.cli build --from-hermes-config` (or `doctor --rebuild`).

No rebuild cron is required. Operators who need a fixed freshness interval may optionally schedule the explicit build command. An explicit CLI `--db` is caller-owned: it is read as supplied and is not rebuilt automatically or used to consume shared OKF-dirty tokens.

## Generated state

The plugin writes local/generated state under `state_dir`:

```text
index.sqlite
index.jsonl
usage.sqlite
okf_queue.sqlite
okfs/tools/*.md
okf_worker.log
index_build.lock
index_build.sqlite
okf_index_dirty/
```

`index_build.lock` is the regular-file compatibility gate shared with v0.3.12 builders. New builders hold it and the SQLite transaction lock in `index_build.sqlite` for collection, validation, and publication. SQLite metadata binds `index.sqlite` to the exact `index.jsonl` bytes: a caught publication failure restores the prior pair, and a crash-split pair is classified as corrupt and rebuilt by the next managed lookup. `okf_index_dirty/` contains tokens that make the next managed lookup rebuild; a successful build removes only the tokens it covered.

None of these files belong in source control.

## OKF lifecycle

Tool OKFs are compact routing notes for tools Hermes has actually used. They are hints, not authoritative tool documentation.

1. When `okf.enabled` is true, `post_tool_call` records only a bounded structural projection: tool identity/toolset, a sanitized schema shape, argument shape, counters, and a redacted error class. It does not persist raw argument values, tool output, transcripts, emails, OCR text, private documents, schema descriptions/examples/defaults, or secret values.
2. When `okf.auto_generate` is true, `on_session_finalize` performs a read-only, tightly bounded queue check and launches a detached worker only when work is available.
3. The worker acquires one fixed lease of `max(300, 2 * max_generation_seconds + 120)` seconds and claims at most `max_candidates_per_session` rows. Lease and claim ownership are checked again before publication.
4. If rows were claimed, the worker makes exactly one `ctx.llm.complete_structured` batch call with the privacy-safe routing projection and a bounded same-toolset related-tool allowlist. The model receives no terminal or file tools.
5. Each result is identity-checked, rendered to a worker-unique temporary file, prevalidated, and published inside a short token/lease-fenced transaction. A stale worker cannot publish. Successful publication marks the managed index dirty.

Version 0.4.0 reads the current v0.3.12 queue shape by normalizing a selected claim's stored schema into the bounded routing projection. It does not promise a general migration ladder for arbitrary older private schemas.

## Feedback and evaluation

Lookup telemetry and feedback stay in `<state_dir>/usage.sqlite`. Tool handlers fail open for telemetry-only errors; explicit `knowledge_feedback` writes remain strict so callers know whether feedback was recorded. Do not put secrets or private document text in queries or feedback notes.

Managed searches may use one deterministic feedback prior when the index was built for the configured source root. Only the latest significant explicit rating for a query/artifact pair is eligible. Among ratings accepted by the current tool, only `useful` is positive; legacy persisted `great` rows remain positive compatibility input. A newer rejection for that route or matching current query suppresses an older overlap route. A matching artifact already present in current results may move to rank one. If it is absent, the plugin performs at most one retry with an accepted query no longer than the current query and the mapped artifact type, and promotes only the exact artifact when that live retry rediscovers it. Searches against an explicit caller-owned `--db` remain unassisted.

When opt-in implicit feedback is enabled and no explicit route matches, mature same-turn evidence may supply the lower-priority route. Consumption through `skill_view` and `read_file` is accepted only from a later model request when a successful call resolves to exactly one caller-visible baseline artifact in the same current index snapshot; same-request parallel calls and route-assisted-only results remain ineligible. It uses the same current-index promotion or one verified typed-retry path, and a matching explicit rejection can veto it.

`knowledge_usage_report` summarizes recent activity before changing ranking, triggers, source coverage, or graph edges. Its `current_native_search_quality` headline isolates live native searches from the current plugin version while excluding known probe queries. `event_cohorts` reports current searches, probes, hourly doctor runs, other CLI/native activity, and historical-version searches separately; `implicit_feedback_by_consumer` distinguishes learned `knowledge_get`, `skill_view`, and `read_file` signals from migrated legacy rows. The original aggregate fields remain available for operations.

`evaluate` is read-only and intentionally measures the unassisted index ranking to avoid training/evaluation leakage. It replays positive local feedback against the current index and reports exact Hit@k/MRR plus parent-equivalent metrics. Parent equivalence is deliberately limited to a `skill_support_doc` and its owning skill; generic graph neighbors are not treated as successful equivalents.

For historical comparisons from a source checkout:

```bash
python scripts/compare_historical_query_versions.py \
  --usage-db ~/.hermes/local_knowledge/usage.sqlite \
  v0.4.2 WORKTREE
```

The comparator opens live telemetry read-only, freezes one private source/runtime/OKF corpus, and builds each ref with that ref's own code. Nonnegative recorded explicit and enabled implicit feedback boundaries plus a matching index-corpus hash prove that the recorded event inputs are available. Implicit rows are accepted only when their exact successful search event has matching root, query, session, task, non-empty turn, and baseline-result membership. Searches recorded with implicit feedback disabled do not require unused implicit state. The report calls a ref replay event-time exact only when that ref's plugin version and index format also match the recorded event and the replay reproduces its recorded baseline/final pages plus route provenance; a changed candidate otherwise remains a counterfactual replay over exact inputs, not historical output. Migrated legacy rows without complete boundaries and same-turn provenance remain explicitly non-exact best-effort evidence. The machine report counts these evidence classes without exposing raw queries.

Correction-route acceptance uses `explicit_resolution` Hit@1 as the primary outcome, then `verified_event` Hit@1/MRR, while requiring no increase in negative-artifact exposure, no unaccepted production ordering changes, and no replay errors. The comparison assessment distinguishes `rejected`, `accepted_improved`, and `accepted_unchanged_or_insufficient_evidence`. Direct/legacy aggregate metrics remain useful coverage and trend signals, but are not sufficient by themselves to prove a correction. Raw queries and artifact IDs appear only with `--details` inside the owner-only evaluation directory.

## Verification

Operator checks:

```bash
hermes local-knowledge doctor
hermes local-knowledge doctor --json
hermes local-knowledge doctor --rebuild --query 'backup runbook'
```

`doctor` reports missing/outdated router skill state and disabled automatic OKF generation as nonfatal warnings. Resolve them or report the deliberate choice.

Development gates:

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest tests/test_public_contract.py tests/test_plugin.py -q -p no:cacheprovider
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q -p no:cacheprovider
python -m ruff check .
python -m mypy
python scripts/check_version_policy.py --base-ref origin/main
git diff --check
```

See [`CONTRIBUTING.md`](CONTRIBUTING.md), [`SECURITY.md`](SECURITY.md), and [`docs/github-security.md`](docs/github-security.md) for contribution, vulnerability-reporting, and repository-security guidance.

## Acknowledgments

This project benefits from people who contribute code, share ideas, or inspire features:

- [@xXLODXx](https://github.com/xXLODXx) — contributed the initial opt-in implicit usage-feedback implementation and its confirmation and generic-artifact gates in [PR #27](https://github.com/stepanov1975/hermes-local-knowledge/pull/27), adapting ideas from [hermes-skill-router](https://github.com/xXLODXx/hermes-skill-router).

## Owner map

- `config.py` — configuration models, aliases, defaults, and the single resolver.
- `implicit.py` — opt-in same-turn search-result consumption attribution.
- `artifacts.py` — whole-artifact models, source collection, privacy-safe metadata extraction, and graph edges.
- `index.py` — format-4 SQLite/JSONL publication, cross-version and SQLite build locking, managed rebuild classification, and deterministic search/get/neighbors.
- `telemetry.py` — local usage and feedback persistence/reporting.
- `routing.py` — bounded live-root explicit/implicit feedback matching, promotion, and one verified typed retry.
- `evaluation.py` — read-only feedback-label replay and exact/parent-equivalent metrics.
- `service.py` — one resolved configuration's managed index and telemetry lifecycle.
- `okf.py` — privacy-safe OKF queue, hooks, detached worker, validation, and fenced publication.
- `plugin.py` — Hermes registration for five tools, four hooks, the bundled skill, and installed CLI adapter; `register` is its public export.
- `cli.py` — primary standalone command surface and the smaller Hermes CLI adapter.
- `indexer.py` — thin compatibility facade exporting exactly `Artifact`, `Edge`, `IndexSettings`, `build_index`, `search_index`, `get_artifact`, `get_neighbors`, and `main`.
- `__init__.py` — package version.

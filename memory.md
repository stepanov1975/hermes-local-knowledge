# Repository memory

Durable development knowledge for `hermes-local-knowledge`, distilled from the July 5-6, 2026 improvement/review sessions. Read this before changing scanning, search, evaluation, plugin handlers, packaging, or release flow.

This file is not a changelog. It records why choices were made and which traps already cost time.

## Project purpose and current shape

- This is a reusable Hermes Agent plugin that routes local questions to **whole artifacts**: skills, skill support docs, scripts, memory docs, runbooks, cron jobs, and MCP server entries.
- It is an artifact router, not a chunk RAG system. The core question is: **which artifact should the agent inspect first?**
- Runtime package code is under `hermes_local_knowledge/`.
- `indexer.py` and `plugin.py` are compatibility/public entry points; keep their exports and monkeypatch seams stable unless there is a deliberate breaking change.
- Generated state is local-only and must not be committed: `index.sqlite`, `index.jsonl`, `usage.sqlite`, `knowledge/`, `state/`, caches, build outputs, mutation workspaces, and virtualenvs.
- Runtime dependencies are intentionally standard-library-only. Test/build dependencies live behind extras.

## Main root cause from the improvement sessions

The recurring routing failures were not caused by the absence of an LLM/neural ranker. They came from **text-poor operational artifacts** and **identifier mismatch**:

- wrapper scripts named `run.sh` or `*_mcp/run.sh` did not contain user-facing intent text;
- env/config identifiers such as `HOMEASSISTANT_URL`, `ha_mcp`, and script paths needed deterministic expansion to natural query terms;
- cron/MCP/script artifacts had enough structured metadata for routing, but the search path did not always use it early enough;
- broad prose docs/runbooks could satisfy many FTS terms and bury the actual operational script/cron/MCP artifact.

Historical evaluation showed the existing FTS path already had strong broad recall. Pure identifier/char retrieval helped some exact text-poor cases but would reduce overall Hit@10 if used as a replacement. The winning design is therefore a **hybrid deterministic pipeline**:

1. keep FTS as the primary broad-recall path;
2. enrich artifact metadata deterministically at scan time;
3. add secondary metadata/identifier candidate retrieval;
4. rerank deterministically with narrow operational intent rules;
5. preserve exact/quoted and skill-support behavior.

Do not replace this with a neural or LLM ranker unless the labeled data grows substantially and an offline evaluation proves it wins without regressing known-good queries.

## Evaluation lessons

### What worked

- Add a real offline evaluation harness rather than relying on anecdotes.
- Use positive `usage.sqlite` feedback as labels, but filter out stale labels whose artifacts no longer exist.
- Track both exact metrics and parent-equivalent metrics.
- Keep `evaluate` read-only. It must not write usage/feedback telemetry.
- Use curated regression cases in `tests/search_regression_cases.json` to protect historically good queries and resolved negative feedback.
- Use `evaluate --details` or `scripts/compare_historical_query_versions.py` when comparing search changes across historical git refs; do not rely on one-off temp helpers for reusable ranking evidence.
- Always run live configured smokes after ranking/index changes, not only unit tests.

### Historical version comparison traps

The reusable `scripts/compare_historical_query_versions.py` helper should evaluate each ref with that ref's own search implementation and an isolated output/state directory, but it must not accidentally evaluate a different corpus than the plugin would use.

Current invariant:

- If the user passes `--root`, pass that root explicitly to the ref build and set `LOCAL_KNOWLEDGE_ROOT` for that subprocess.
- If the user does **not** pass `--root`, do not synthesize a `--root` from runtime defaults and do not leak an ambient `LOCAL_KNOWLEDGE_ROOT` into the subprocess. The CLI treats `--root` as an explicit override; forcing it can change scanner defaults such as Markdown-doc inclusion.
- Always force `LOCAL_KNOWLEDGE_STATE_DIR` to the per-ref state directory so comparison builds cannot write into the live configured state.
- Set `PYTHONDONTWRITEBYTECODE=1` for ref build/evaluation subprocesses so `WORKTREE` comparisons do not create or update `__pycache__` in the checkout.
- Include hash suffixes in generated ref path names; sanitized refs such as `feature/a` and `feature-a` can otherwise collide.
- If a helper script is documented as reusable or needed from sdists, include it in `MANIFEST.in` and verify the sdist contains it.

### Parent-equivalence trap

A previous implementation treated arbitrary `related` graph links as symmetric equivalence. That was wrong: it could count peer skills or cron/script graph neighbors as a successful hit.

Current invariant:

- Parent-equivalent matching is type-aware.
- Only `skill_support_doc <-> owning parent skill` pairs count as equivalent.
- Do **not** treat generic `related` edges, cron-script edges, or peer skill links as equivalence.

### Metric-window trap

`*_at_10` metrics must use a top-10 window even if callers pass a different `max_k`. A bug let ranks beyond 10 affect `mrr_at_10` when `max_k > 10`. Keep tests covering this.

### Exact labels vs parent-equivalent labels

When support-doc diversity chooses a sibling support doc from the same parent, exact Hit@10 can drop while parent-equivalent Hit@10 remains correct. Do not tune ranking just to chase an old exact sibling label unless the selected result is actually worse for user routing.

## Scanner and metadata lessons

### Script indexing privacy and signal

A real issue was found: `scan_scripts()` indexed the first chunk of script body text. That was inefficient and risked persisting literal values such as token assignments into the local FTS table.

Current invariant:

- Script `search_text` should contain only safe routing metadata: title, summary, path parts, env variable **names**, code identifiers, CLI flags, and derived metadata terms.
- Do not index arbitrary script body literals.
- If a script must be discoverable by a phrase, put that phrase in a leading comment/docstring, filename/path, env var name, CLI flag, function/class name, or adjacent runbook/skill reference.

### Env-name extraction false positives

A reviewer caught that broad assignment matching treated ordinary locals like:

```python
ha = object()
```

as env/config names. Because `ha` maps to Home Assistant, that polluted unrelated scripts with false Home Assistant metadata.

Current invariant:

- Extract uppercase env-style names from shell-style assignments/accesses.
- Support explicit env APIs such as `os.environ[...]`, `os.environ.get(...)`, `os.getenv(...)`, and `process.env[...]` / `process.env.NAME`.
- Do not treat ordinary lower-case Python/JS locals as env names.
- Do not infer `Home Assistant` from generic `/home/...` path components.

### MCP env-value leakage

A reviewer caught that MCP scanning could extract paths from serialized `env` dictionaries, which could persist a secret env value if it looked path-like.

Current invariant:

- MCP artifacts may use env **names** for triggers/routing.
- MCP artifacts must not persist env **values** into summary, related paths, search text, or JSON output.
- Extract related paths only from command/args/url-like fields, not from env values.

### Runtime archive exclusion

Archived Hermes skills under `$HERMES_HOME/skills/.archive/` are recovery material, not active routing targets. The default excluded directory set includes `.archive` so archived/consolidated skills do not reappear in `knowledge_search` after a cleanup. Preserve this behavior with scanner tests whenever changing excluded-directory handling.

## Search/ranking lessons

### Strict-first early-return trap

Stage 3 initially let strict FTS results fill the requested limit and return early. That skipped fallback operational candidates even when the query explicitly asked for script/cron/MCP artifacts. Broad prose runbooks then outranked actionable operational artifacts.

Current invariant:

- If a query has operational intent (`script`, `cron`, `job`, `jobs`, `mcp`, `wrapper`), collect fallback metadata/OR candidates even when strict results already fill the limit.
- Apply final ordering after strict + fallback are combined.

### Operational priority must stay narrow

Several iterations over-promoted non-prose artifacts. The final rule must not become global type promotion.

Current invariant:

- Operational priority only applies when the query explicitly requests operational artifact types.
- Requested operational types are:
  - `script` -> script;
  - `cron`, `job`, `jobs` -> cron_job;
  - `mcp`, `wrapper` -> mcp_server and relevant wrapper scripts.
- Broad prose (`doc`, `runbook`, `memory_doc`) can be demoted behind **relevant requested operational types**.
- Generic fallback `skill` / `skill_support_doc` rows must not leapfrog stricter prose just because the query contains `script`, `cron`, or `mcp`.
- Script-only queries are special: strict skill/support-doc hits stay protected, and script rows need at least one non-operational/domain term when such terms exist. Plain `script` can still route to scripts.
- Cron/MCP promotion should require all non-operational specific terms, so wrong-domain cron/MCP rows do not leapfrog same-domain prose.

### Quoted/exact behavior

Quoted support-doc searches are fragile and important. Exact/quoted behavior should not be polluted by relaxed fallback rows.

Current invariant:

- Balanced quoted phrase queries disable parent lifting and operational priority.
- Pure quoted searches should return strict results only.
- Mixed quoted + extra-term queries may still use fallback when needed to preserve curated behavior.

### Support-doc diversity

Support docs are valuable long-tail hits, but they can flood the result list.

Current invariant:

- Support-doc diversity is applied globally after strict + fallback candidates are combined, not separately per candidate batch.
- The cap is per parent skill, not one support doc total across all parents.
- Parent skill should remain visible when a support doc matches.

### `artifact_type` filtering

A bug remained after SQL-level filtering: parent lifting could insert a parent `skill` into `artifact_type="skill_support_doc"` results.

Current invariant:

- Apply type filtering during SQL candidate collection.
- Disable support-doc parent lifting when an explicit `artifact_type` filter is active, or otherwise guarantee final rows all match the requested type.
- Tests should include `skill_support_doc`, not only `script`.

### Artifact identity ranking must be bounded

The v0.2.19 ranking fix addressed a separate failure mode: queries that effectively named an artifact by id, title, filename, basename, or path could still be buried behind many body/prose matches. The fix is an identity boost, not blanket filename supremacy.

Current invariant:

- Strong identity signals are exact compact id/title/basename/stem matches or all non-routing query terms present in id/title/basename/path.
- Generic one-token path overlap and partial filename overlap must not beat a result that fully satisfies the user's conceptual/content terms.
- FTS weighting may favor title/path/triggers and demote long prose, but FTS remains only one candidate source.
- Strong all-term metadata identity candidates must be merged before the final limit slice. Appending them after a full strict result page silently drops the identity hit.
- Identity metadata retrieval must require all non-routing identity terms and must not truncate long identity queries to the first few terms.
- Pure quoted searches should return before relaxed metadata fallback work; quoted-only behavior should remain strict.

## Regression tests that paid off

Keep or extend tests for these exact failure modes:

- sparse Paperless cron/script query returns script and cron near the top and before broad runbook;
- strict result list already full still collects fallback operational candidates;
- generic fallback skill/support doc does not leapfrog strict runbook for operational query;
- domainless generic fallback script does not leapfrog strict prose for `paperless invoice script`;
- script-only queries still protect strict skill/support-doc hits;
- MCP intent returns MCP server and wrapper before broad references;
- support-doc diversity applies across strict+fallback and is per parent;
- `artifact_type="skill_support_doc"` returns only support-doc rows;
- pure quoted phrase search does not backfill unrelated fallback results;
- MCP env values do not leak into artifact JSON/search;
- `*_at_10` metrics use a top-10 window;
- script body literals are not searchable through FTS;
- filename/path/title/id identity can enter a full strict result page and outrank body-only hits;
- support-doc parent lifting still works for identity-recovered support docs;
- long identity-like queries with more than eight terms require all identity terms, not only an early prefix;
- partial filename matches such as `backup.sh` do not outrank fuller conceptual/content matches such as `backup strategy`.

## Promising but not fully implemented hardening ideas

These came out of review but were not all implemented during the July 2026 pass:

- Add `expected_before` support to `tests/search_regression_cases.json` so historical cases can assert ordering, not just `expected_anywhere`.
- Add explicit negative tests proving wrong-domain cron and wrong-domain MCP rows do not get promoted globally.
- Add a multi-parent support-doc diversity test to prove the cap is per parent and not global.
- Add parent-equivalence tests involving support-doc related non-skill edges and cron/script graph edges, to guard against future over-broad equivalence.
- Run mutation testing periodically and triage high-signal survivors in search/evaluation/scanners; do not chase every survivor blindly.
- Consider sklearn/TF-IDF tooling only if the data grows; it was absent and not required for the successful deterministic implementation.

## Verification checklist used successfully

For code changes, run the full local gates unless the change is truly docs-only:

```bash
python -m pytest -q
python -m ruff check .
python -m mypy
python scripts/check_version_policy.py --base-ref origin/main
git diff --check
```

For ranking/indexing changes, also run configured smokes:

```bash
HERMES_HOME=/home/alex/.hermes python -m hermes_local_knowledge.cli build --from-hermes-config
python -m hermes_local_knowledge.cli evaluate --from-hermes-config --json
python -m hermes_local_knowledge.cli doctor --hermes-home /home/alex/.hermes --rebuild --query 'paperless review'
```

Some older/current command forms may reject `doctor --from-hermes-config`; use `doctor --hermes-home /home/alex/.hermes ...` if that happens.

For release/package changes, also verify:

```bash
python -m build --outdir "$tmpdist"
python -m twine check "$tmpdist"/*
python -m venv "$tmpvenv"
"$tmpvenv/bin/python" -m pip install "$tmpdist"/*.whl
"$tmpvenv/bin/python" - <<'PY'
import importlib.metadata as md
matches = [ep for ep in md.entry_points().select(group='hermes_agent.plugins') if ep.name == 'local_knowledge']
print([(ep.name, ep.value) for ep in matches])
module = matches[0].load()
print('register_callable', callable(getattr(module, 'register', None)))
PY
```

For deployed-plugin checks on Alex's machine:

```bash
HERMES_HOME=/home/alex/.hermes hermes plugins update local_knowledge
printf 'repo=' && git -C /home/alex/repos/hermes-local-knowledge rev-parse --short HEAD
printf 'plugin=' && git -C /home/alex/.hermes/plugins/local_knowledge rev-parse --short HEAD
HERMES_HOME=/home/alex/.hermes python -m hermes_local_knowledge.cli doctor --hermes-home /home/alex/.hermes --rebuild --query 'paperless review'
```

Then restart or reload the gateway from outside the running gateway process when native tools need the new code loaded.

## Release and deployment lessons

- Release-relevant changes require a synchronized version bump in `plugin.yaml`, `pyproject.toml`, and `hermes_local_knowledge/__init__.py`.
- The version-policy script compares against the base ref and also notices dirty/untracked local release-relevant paths.
- CI green is not enough if async reviewers may still post comments. Before release, read back PR reviews/review threads when a PR exists.
- A timed-out, cancelled, missing, or partial independent review is **not** a PASS. Rerun a bounded/split review or report that there is no independent approval.
- After publishing, verify GitHub checks, tag target, release assets, installed plugin SHA/version, and runtime smokes from the installed plugin directory.
- A running Hermes gateway may keep an old imported plugin module. `hermes gateway restart` is intentionally blocked from inside a gateway-run agent process because it would kill its own command. Use `/restart` from chat or run `hermes gateway restart` from an external shell. `systemctl --user reload hermes-gateway.service` can request the planned restart path, but verify logs/PID after the active turn drains.

## Tooling/session pitfalls already encountered

- Do not treat unsafe/blocked convenience commands as verification. One smoke that piped Python output into Python was blocked by the shell safety scanner; the raw JSON checked separately was the valid evidence.
- Avoid shell snippets with `&` when the scanner may interpret it as shell backgrounding; use simpler Python scripts or separate commands.
- Subagents must be told not to modify files when reviewing. One previous subagent created a scratch skill despite review intent; always check for unintended local changes after delegation.
- `skill_manage` may refuse externally owned runtime skill dirs. For this repo, preserve lessons in `memory.md` / `AGENTS.md` rather than relying only on runtime skill edits.

## Public-safety notes

This repo is public. Keep examples and memory public-safe:

- no tokens, secret values, credential material, session transcripts, or private document contents;
- avoid dumping local telemetry rows; summarize aggregate lessons only;
- paths and service names already present in tests/docs are acceptable when needed for reproducible smokes, but do not expose new private infrastructure details gratuitously.

# Repository memory

Durable product, ranking, privacy, and state rationale for `hermes-local-knowledge`. This is not a changelog or task backlog.

## Product boundary

- The plugin routes a local question to a **whole artifact**: a skill, skill support document, script, memory document, runbook, generic document, cron job, MCP server, or generated tool OKF.
- It answers “which artifact should be inspected first?” It does not replace reading that artifact and is not chunk RAG.
- Runtime dependencies remain standard-library-only. Generated state is local-only.
- Runtime configuration follows Hermes' context-local active profile before process-wide environment fallbacks; profile state remains isolated by default, while an explicitly shared `state_dir` intentionally combines telemetry and learned feedback.
- Stable boundaries are the `hermes_local_knowledge.plugin` entry point and `register`, the five documented tools/four hooks, the documented CLI/config behavior, and the eight names in `indexer.__all__`.

## Ownership

- `config.py` resolves all configuration and aliases.
- `implicit.py` owns strict same-turn search-result consumption attribution for opt-in implicit evidence.
- `artifacts.py` owns artifact/edge models, source collection, and safe routing metadata.
- `index.py` owns format-4 publication and deterministic retrieval.
- `telemetry.py` owns local usage/feedback data; `evaluation.py` owns read-only label replay.
- `routing.py` owns bounded explicit and implicit route selection and promotion.
- `service.py` composes one resolved configuration with managed index and telemetry lifecycles.
- `okf.py` owns safe tool-use capture, the durable queue, detached generation, validation, and fenced publication.
- `plugin.py` owns Hermes registration; `cli.py` owns command behavior; `indexer.py` is only the compatibility facade.

Private helper shapes are not product contracts. Preserve them only when a documented external behavior requires them.

## Retrieval rationale

### Hybrid deterministic recall

Text-poor operational artifacts often expose intent through filenames, paths, identifiers, environment names, CLI flags, cron metadata, or MCP metadata rather than prose. Broad documents can satisfy many lexical terms and obscure the actionable artifact.

The retrieval design therefore remains deterministic and hybrid:

1. FTS supplies primary broad recall.
2. Collection derives routing-safe metadata and identifier terms.
3. Identity/metadata candidates complement FTS.
4. One final rank key applies narrow intent and identity signals.
5. Exact/quoted, filter, parent-lifting, and diversity rules remain explicit.

An LLM/neural ranker is not justified without a larger labeled corpus and offline evidence that it improves current behavior without regressing known-good queries.

### Ranking invariants

- Strong identity evidence requires an exact compact identity or all non-routing identity terms in ID/title/basename/path. Partial filename overlap must not beat fuller conceptual evidence.
- Operational promotion applies only when the query asks for script, cron/job, MCP, or wrapper artifacts and the candidate satisfies the relevant domain terms. It is not global type promotion.
- Query-terminal runbook, skill, document/reference, and memory intent is a separate conservative transformation over one immutable legacy baseline from one SQLite read snapshot. A target qualifies only with a complete configured entity label in its own ID/title/path and a distinct topic term; body-only and sibling-derived identity never qualify. Resolve family ambiguity across the complete index independently of output limit, then stably move only the exact target and its owner while preserving unrelated order.
- Script-only queries protect strict skill/support-document hits. Cron/MCP candidates must not leapfrog same-domain prose on weak overlap.
- Pure quoted queries stay strict. Quoted phrases preserve phrase order and do not trigger parent lifting or operational promotion.
- An explicit `artifact_type` filter returns only that type.
- Support-document diversity is global after candidate combination, capped per parent, and keeps the owning skill visible when appropriate.

## Privacy and source rationale

- Script FTS text contains only routing-safe metadata: title/summary, path components, environment **names**, code identifiers, CLI flags, and derived terms. Arbitrary script body literals are not indexed.
- Lower-case local variables are not environment names. Generic home-directory path text is not evidence for Home Assistant or another entity.
- MCP environment names may aid routing; MCP environment values must never enter summaries, paths, FTS, JSONL, or related edges. Credential-like process arguments and URL userinfo are redacted before persistence.
- Tool-OKF capture stores only bounded structural projections and counters/error classes. Raw argument values, tool outputs, transcripts, emails, OCR/private document text, schema descriptions/examples/defaults, and secret values stay outside the queue and model packet.
- Generated OKF prose is human routing context, not positive lexical evidence. Positive evidence comes from deterministic identity, aliases, and triggers; negative boundaries and related tools do not become FTS triggers.
- Runtime `.archive` skill content is recovery material and remains excluded from active routing.
- Public repository files contain no credentials, private telemetry rows, transcripts, or private document content.

## Index and generated-state rationale

The authoritative derived index is format 4. `index.jsonl` is a validated companion export; it omits internal `search_text`.

- Classify persisted indexes as missing, corrupt, older, current, or newer.
- Managed lookups rebuild missing, corrupt, older, and OKF-dirty state. A newer format is rejected so an older runtime cannot downgrade it.
- Ordinary source-file, cron-registry, and MCP-config changes are not inferred from index age or source mtimes. They require `rebuild=true`, an explicit build, or an optional operator schedule.
- Index construction, validation, and publication hold the v0.3.12-compatible regular-file gate at `<state_dir>/index_build.lock`, then the new-process SQLite transaction lock at `<state_dir>/index_build.sqlite`.
- Build SQLite and JSONL in temporary files, validate corpus IDs/schema/edges/metadata, then publish a hash-bound recoverable pair. Restore the prior JSONL on a caught SQLite publication failure; classify a crash-split pair as corrupt so managed lookup rebuilds it.
- Dirty markers are tokenized. A successful build removes only the tokens observed before that build; concurrently created tokens remain for the next managed lookup.
- Explicit CLI `--db` paths are caller-owned and are never rebuilt implicitly or allowed to consume shared dirty markers.

Generated/local state includes `index.sqlite`, `index.jsonl`, `usage.sqlite`, `okf_queue.sqlite`, `okfs/tools/*.md`, `okf_worker.log`, `index_build.lock`, `index_build.sqlite`, and `okf_index_dirty/`. None belongs in source control.

## Tool-OKF lifecycle rationale

Automatic generation is optional and defaults off because it spends model tokens. Candidate capture and existing-artifact lookup remain useful without it; enabling generation requires explicit disclosure and consent.

- `post_tool_call` records a safe structural candidate and never performs model work.
- `on_session_finalize` only performs a read-only, tightly timeout-bounded work check and detached launch. Session closure must not wait for generation.
- The worker takes one fixed lease of `max(300, 2 * max_generation_seconds + 120)`, claims a bounded batch, and makes exactly one structured call when claims exist.
- The generation packet is a bounded schema/argument-shape projection with a claim-time same-toolset allowlist. Candidate batching is an execution optimization, not semantic evidence between candidates.
- Each generated item must match the claimed tool, toolset, schema hash, generator version, output path, and allowlist. Temporary-file validation precedes a short lease/claim-fenced publication transaction; ownership loss makes the result non-publishable.
- Successful publication marks the index dirty. Stale-claim recovery may complete a still-claimed canonical file only after validating it against that exact claim.
- Generator versioning is independent of package/index format and can requeue completed artifacts without erasing pending/error lifecycle state.
- Version 0.4.0 reads the current v0.3.12 queue shape by normalizing selected claims into the bounded routing projection. There is no promise to transform arbitrary historical queue schemas.

## Feedback and evaluator rationale

- Lookup usage, failures, zero-result queries, and explicit ratings stay in `usage.sqlite` and are summarized locally before changing ranking or source coverage. User-search quality defaults to the live current-version native-search cohort; probes, CLI maintenance, other native activity, and historical versions remain visible as separate operational cohorts.
- Telemetry-only failures must not break lookup. Explicit feedback writes remain strict so callers know whether a rating was stored.
- Evaluation is read-only and must not emit usage events or mutate feedback.
- Explicit useful feedback may provide one bounded current-index-root routing prior, but it is not permanent truth. Legacy persisted `great` rows remain positive compatibility input even though the current tool rejects that rating. The newest significant query/artifact rating wins; a newer rejection for the route or matching current query vetoes an older overlap route. A retry must be no longer than the current query, must use the artifact's mapped type, and must rediscover the exact artifact before promotion. Explicit caller-owned indexes remain unassisted.
- Opt-in implicit evidence comes only from a recent same-session/task/turn consumption of an artifact returned by that search's unassisted baseline page. `knowledge_get` uses the explicit artifact ID; `skill_view` and `read_file` require a successful later model request whose canonical source path resolves to exactly one baseline artifact in the same current index snapshot. Same-request parallel calls and route-assisted-only results are ineligible. One search/artifact pair is deduplicated, confirmations must come from distinct search events, and evidence spread across too many query shapes is treated as generic. Implicit routing loses to matching explicit routes, uses the same current-index promotion or one verified typed-retry path, can be vetoed by matching explicit rejection, and is not evaluation ground truth.
- Keep feedback lookup bounded and fail-open: scan only a recent capped window through the root/order index and use a short read timeout so optional routing cannot add multi-second lock delays.
- Evaluation deliberately uses the unassisted index ranking so the same feedback rows are not both training data and evaluation labels. Ignore stale labels whose artifacts no longer exist.
- Historical replay treats implicit state as exact input only when the recorded root-scoped high-water, effective settings, and same-turn baseline provenance are available. Searches recorded with implicit feedback disabled do not require unused implicit state; incomplete legacy rows remain explicitly non-exact evidence.
- Report exact Hit@k/MRR and parent-equivalent metrics. Parent equivalence is limited to a `skill_support_doc` and its owning skill; peer skills, cron/script links, keyword overlap, and other graph edges are context rather than equivalence.
- `*_at_10` metrics use an actual top-10 window regardless of a larger retrieval limit.
- Curated regression cases protect exact/quoted behavior, identity recovery, operational intent, support-document diversity, type filters, privacy boundaries, and known historical routing wins.

Verification should prove public contracts first, then the full suite/static/version gates. Ranking or persistence changes additionally require configured build/search/evaluation smokes because isolated unit fixtures cannot establish corpus behavior or state safety.

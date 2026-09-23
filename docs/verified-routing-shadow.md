# Verified routing: opt-in shadow mode

This feature measures whether independently investigated routes are worth reusing.
It never changes search ranking, tool results, feedback ratings, or indexed artifacts.
It is **disabled by default**, independently of tool-OKF generation and implicit feedback.

## Enablement and provider boundary

Review the storage and token-use notice before opting in:

```yaml
local_knowledge:
  verified_routing:
    mode: shadow  # default: off; no promotion mode exists
    max_cases_per_worker: 1  # range 1–2
    max_model_calls_per_case: 12  # range 4–24, applicability + investigator + verifier combined
    max_worker_seconds: 300  # range 30–1200
    max_age_days: 30  # range 1–90
```

An invalid mode resolves to `off`. Off means this feature does not retain requests,
create its database/log, launch workers, or call a model. Other existing plugin
features remain controlled by their own settings. Installation and upgrade do not enable it.

**Private data:** opting in retains bounded original user-request text separately
from the model-generated search query and optional immediate-lookup fields, exact
attribution, the complete ordered baseline IDs (up to the native search maximum of
30), a fingerprint of their indexed routing metadata, recurrence counters, current
source identity/hash/citation evidence, baseline-coverage judgments, and outcomes.
The queue is capped at 500 cases; once full, new cases are skipped rather than
silently evicting evidence. State is namespaced by resolved profile home and source
root even if profiles share a `state_dir`. This is not the structural-only tool-OKF
queue. No conversation histories, assistant/tool transcripts, raw tool outputs,
credentials, or private evaluation labels are intentionally captured. Nothing
here is indexed or published. Keep state outside version control.

**Provider disclosure:** the detached worker sends this pre-search packet, a bounded
shortlist of prior packets when checking reuse, and selected current operational
Markdown source content to the profile's configured Hermes model provider. This
consumes additional model tokens; it is not local-only inference unless that
provider is local. Bounded text can still contain sensitive information: this is
not a general-purpose secret redactor. Do not opt in for conversations or source
trees that must not reach that provider. Search tool responses and ordering are
unchanged; only the optional input schema has grown.

## Immediate lookup and context provenance

Native `knowledge_search` accepts an optional `lookup` object. Each supplied field
must be a nonempty string within its character bound; unknown fields, wrong types,
and oversized values abstain from shadow capture, not truncate silently. Ordinary
search still works independently of these fields.

| Field | Maximum characters | Meaning |
| --- | ---: | --- |
| `intent` | 600 | What this lookup must find now, separately from the parent task |
| `target` | 200 | Host/service/source-applicability target |
| `operation` | 200 | Information need, such as inventory or locating a tracker |
| `context` | 1000 | Concise pre-search constraints/facts and their stated origin |

For example, a broad maintenance request can legitimately lead to:

```json
{
  "query": "Atlas inventory maintenance tracker",
  "lookup": {
    "intent": "Locate host inventory and current maintenance progress sources",
    "target": "Atlas",
    "operation": "inventory and progress lookup",
    "context": "Assistant-selected retrieval target for the maintenance investigation; live status not verified."
  }
}
```

The host's bounded `pre_llm_call.user_message` remains `user_request`, labeled
`host_pre_llm_user_message`. The separate `lookup_context.lookup.fields` object is
labeled `assistant_supplied_not_authority`, with timing `search_arguments_pre_result`.
No `conversation_history` window is captured. These are provenance labels, not
trust upgrades: a clear assistant target is sufficient to route read-only documents,
including when the current user message is a terse continuation with explicit
lookup intent. It is not proof of environment state, user facts, or execution
permission. Missing task arguments or authorization for a later operation are not
reasons to reject an otherwise applicable document. Actual ambiguity/conflicting
constraints that change source coverage are reasons to abstain.

When fields are absent, the query is the immediate-lookup hint; the original
request remains separate scope/context. A generic/terse request with no adequate
explicit intent still abstains. Do not put secrets, transcript excerpts or copied
tool output into these fields. They describe already available pre-search context;
no later user clarification or tool evidence is added to a captured case.

## Lifecycle and verification contract 2

1. The pre-LLM hook binds request text to exact profile/session/task/turn identity.
   A bounded cross-thread map supports Hermes hooks running in copied contexts.
2. Successful native `knowledge_search` capture joins the exact usage receipt and
   its unassisted baseline. It retains the whole returned page, not a 12-item prefix.
   Missing attribution/context abstains; no model call occurs on this path.
3. End-of-turn marks work ready, then performs a short read-only work check and
   wakes a detached finite supervisor. Teardown retains the same wake as a fallback.
   Neither hook waits for model work or performs inference. The supervisor starts
   separate host workers, each retaining the configured batch/case limits.
4. Before any model call, the worker deterministically reads the **complete**
   baseline through the existing bounded Markdown reader (see eligibility below).
   Ineligible baselines close without applicability or acquisition inference.
   For eligible cases, the worker considers at most three lexically/shared-source shortlisted routes
   from the latest 100 same-profile/root/filter `ai_verified` cases. Shortlisting
   is **candidate generation, never a semantic verdict**. Eligible candidates need
   contract 2, unexpired verification and unchanged readable source dependencies.
   If candidates fit the evidence bounds, one applicability call compares current
   sources and the same captured task/context/baseline packet used by acquisition.
5. Applicable prior sources produce an asynchronous `would_reuse` case with
   `ai_applicable` provenance and a `would_reuse_semantic` counter, not a new
   `ai_verified` acquisition. Paraphrases, different parent tasks, and changed
   requested answer quantities may qualify when the same sources cover the lookup.
   Different hosts/operations do not qualify when coverage differs. Applicability
   retains the original verification age and does not seed further semantic chains.
6. Rejection/no candidate falls through to ordinary bounded source investigation
   and a fresh independent verifier. Acquisition has its own source/search budget;
   old candidate reads cannot starve it. The applicability call still counts toward
   the unchanged overall case model-call budget. The worker can search outside the
   original shortlist, but cannot execute operational tools or edit sources.
7. Both verifier and applicability must review **every** baseline ID. Useful items
   must be retained or demonstrably covered by read/cited equivalent sources.
   Losing a useful tracker, substituting a generic workflow for inventory facts,
   missing review entries, or potentially useful unread evidence vetoes acceptance.
   Clearly irrelevant metadata can justify `not_useful` without a full source read;
   this is not a claim that all 30 artifacts were read. `unknown` always vetoes.
8. Identical normalized request/query/filter plus the same lookup packet, ordered
   baseline and current metadata fingerprint may take the cheap exact path using
   the new contract and existing source identity/hash/age gate. No model runs
   inline. Changed baselines/context or old contracts cannot bypass coverage checks.
   A changed baseline fingerprint during worker processing also blocks publication.

Contract 2 adds one optional queue column (`lookup_context`, default `{}`), migrated
on a normal shadow write. Reports are read-only and do not migrate. Older stored
`ai_verified` outcomes remain historical evidence but cannot be reused by this
contract; legacy queued cases without the packet close unresolved without inference.
New exact observations can acquire a contract-2 case. There is no bulk rewrite.

Acquisition remains capped at six searches and 32 candidate metadata records.
Applicability reserves separate bounded metadata allowances for the complete
baseline (at most 30 records) and stored-route receipts (at most 32), then unions
them without further candidate admission. Rejected routes restore tentative
admissions instead of consuming the next route's allowance. Both evidence phases
retain the limits of eight whole readable sources, 24 KB per source and 96 KB
aggregate source text.
If the combined stored-route dependencies and complete baseline do not fit these
limits, applicability is skipped without inference and acquisition retains its
separate allowance. Investigator responses request at most 1,800
output tokens; verifier/applicability responses request at most 4,000 to accommodate
complete baseline reviews. These are output limits, not total token budgets.
The per-case invocation and per-batch time limits include all stages, but
host/provider retry and fallback policy can extend elapsed time and token use
behind each host call. Late work cannot publish after losing its lease. Interrupted
calls remain explicitly ambiguous rather than being automatically retried.

### Conservative baseline eligibility (0.5.3b5)

Before **any detached model call**, the complete captured baseline must be readable
as registered operational Markdown within the existing eight-source, 24,000-byte
per-source and 96,000-byte aggregate reader limits. Supported types remain `skill`,
`skill_support_doc`, `runbook`, `memory_doc`, and `doc`; scripts and `tool_okf` are
not newly readable. No new configuration or expanded model/source allowance is
introduced. An empty baseline can still proceed to bounded acquisition/search.

A refusal closes the case as `unresolved` with a stable reason such as
`ineligible_baseline_unsupported_source`, `ineligible_baseline_missing_source`,
`ineligible_baseline_source_unavailable`, `ineligible_baseline_source_too_large`,
`ineligible_baseline_source_count_budget`, or `ineligible_baseline_source_bytes_budget`.
The first refusal in baseline order determines the case reason; the bounded receipt
retains attempted reads/refusals across the full baseline. Unregistered paths,
empty sources and type mismatches likewise veto. Source or metadata changes and
worker time/lease failures remain distinct from eligibility/model abstention.

This is an **intentionally conservative scope restriction, not a semantic judgment**:
even an irrelevant unsupported or oversized baseline item skips the whole case.
The worker neither drops unknown entries nor pretends they are `not_useful`. It
may skip cases that a future, separately designed evidence strategy could solve.
Eligible model decisions still require the complete baseline coverage review and
unknown coverage still vetoes acceptance. Preflight identities are rechecked before
model dispatch and acceptance; this is not an atomic filesystem snapshot. Preflight
content stays in memory and is not added to model packets or persisted. Existing
cheap exact-reuse observations retain their contract/source/age checks and do not
call a model; historical results are not bulk rewritten.

### Structural diagnostics and abstention categories

The additive `cases.diagnostics` JSON column defaults to `{}` and migrates on the
next normal shadow write. It records a versioned structural receipt: eligibility
and reason, at most 64 read attempts (known artifact IDs capped at 600 characters,
stage and stable outcome/refusal code), truncation/count metadata, bounded
stage/action counts and an investigator abstention category. Counters saturate at
9,999. Unknown model-supplied read IDs are blanked, not stored as arbitrary prose.
These are worker read attempts, including cached reads and verifier refreshes,
not a transcript or an exhaustive trace of freshness-validation disk I/O.

The optional unresolved category is one of `unspecified`, `insufficient_sources`,
`ambiguous_lookup`, `conflicting_evidence`, `baseline_coverage`, or
`no_applicable_route`. Missing, unknown or malformed categories become `unspecified`,
so older model responses remain valid. Categories are **model-reported**, not proof
of cause. A model abstention still has reason `insufficient_evidence`; deterministic
ineligibility, provider failure, time/call limits and source changes remain separate.
No raw response, rationale, query history or source content is added to diagnostics.
The inherited private original request/lookup queue boundary is unchanged.

Receipts are checkpointed before/after calls and on caught success/failure under
the existing lease/claim fence. A lost lease cannot overwrite a newer owner;
a hard-killed worker may leave only its last checkpoint, never a fabricated trace.
`routing-report --json` exposes aggregate `reasons` and `diagnostics` (eligibility,
fixed action counts, model abstention categories, retained refusal counts and
truncated-case counts), not per-case source IDs or text. Refusal totals cover the
retained prefix, not attempts beyond truncation. Old schemas/rows count as
`unavailable`; read-only reporting does not migrate them or infer historical actions.

Synthetic fake-provider tests exercise these contracts, including zero-call skips
and eligible success/reuse. They do not establish real-model efficacy or historical
refusal causes, production savings or improved live routing.

### Finite scheduling and recovery boundary

Each supervisor has **900 seconds (15 minutes) total elapsed time and at most 16
child launches**, including cheap legacy/baseline rejections and failed batches.
These fixed allowances never replenish, and children never launch successors or
supervisors. This permits multi-batch backlogs (including an eight-case backlog)
to progress without more messages, but is not a promise to drain an arbitrarily
large or slow queue. Each child retains `max_cases_per_worker`,
`max_model_calls_per_case`, `max_worker_seconds`, and its original fixed lease.
Consequently one wake may spend more tokens than one batch: up to 16 batches,
subject to the total time limit. Model-call limits do not bound provider retries
or total tokens/billing. Normal search results still never change.

A separate `supervisor.sqlite` transaction lock in the private profile/root queue
namespace excludes other supervisors without holding a queue transaction. This
uses standard-library SQLite on POSIX and Windows. Duplicate wakes while an owner
is active exit immediately; timestamped wakes delayed until after exhaustion
cannot reopen that allowance. Before an idle exit the owner releases its lock and
rechecks the queue, so a wake racing that exit is not lost; reacquisition keeps the
same remaining launch/time allowance. A genuinely later external turn/teardown may start
a new finite allowance. `supervisor.log` records only the last structural exit
reason (`idle`, `launch_budget`, `time_budget`, `disabled_or_changed`, or
`supervisor_error`), never task/source/provider text. These are generated private
state files, not source-controlled artifacts.

A surviving supervisor waits through live worker leases (normally 360 seconds at
the default settings), then lets the existing worker recovery close expired
claims as `interrupted_ambiguous` before processing remaining ready cases. No
ambiguous model call is replayed. Waiting consumes the same elapsed allowance; it
does not consume child launches. At the elapsed deadline the supervisor kills and
reaps its direct child, if any, and exits. This also caps a supervised child whose
configured worker timeout is longer than the remaining supervisor allowance; a
manual `routing-worker` retains its existing one-batch behavior.

**Recovery requires the supervisor to survive.** Supervisor death, machine reboot,
exhausted budgets, or a failed launch leave remaining work for a later eligible
turn/teardown or manual worker invocation. No cron, system service, permanent
daemon, startup recovery, or recursive supervisor chain is installed.

## Operator commands

```bash
python -m hermes_local_knowledge.cli routing-report --hermes-home /path/to/profile --json
hermes local-knowledge routing-worker --hermes-home /path/to/profile
```

The worker uses host-owned model access and only operates in shadow mode; it is not
a standalone inference client. `routing-report` is available only through the
standalone Python CLI. Reports expose counters/statuses, not request/source prose by
default. Disabling the mode stops new capture/launches but does not delete evidence
or kill an existing worker. The supervisor rereads configuration between children
and while waiting on leases, stopping further launches when shadow is off or the
queue namespace changes. Wait for bounded work to exit before freezing a snapshot.

## What this does not prove

- Capture requires the host's exact pre-LLM/post-tool identities. Stateless calls,
  remote MCP calls, or hosts missing either hook abstain. Adapter abstentions are
  not counted by `routing-report`; an empty report does not prove hooks ran.
- Lexical/shared-source candidate generation can miss paraphrases with no overlap
  and viable routes outside its bounded window; source limits can force abstention.
- AI baseline relevance/equivalence and applicability judgments can be wrong.
  Scripted regression tests establish contract mechanics, not semantic efficacy.
- `ai_verified` and `ai_applicable` are not human gold labels, execution permission,
  proof of live environment facts, or proof the operational task succeeded.
- Unchanged hashes can belong to superseded documents. Existing all-read-source
  freshness dependencies (including near misses) can also invalidate useful routes.
- Exact `would_reuse` observations and asynchronous `would_reuse_semantic` outcomes
  are distinct evidence. Neither is a claim of savings or improved normal search;
  ordinary searches still run and all acquisition/applicability spend is real.

Start with an isolated profile and synthetic operational sources. Inspect captures,
coverage, completion, drift, recurrence and cost before proposing live promotion.
Installation, enablement and gateway restart remain separate operator decisions.

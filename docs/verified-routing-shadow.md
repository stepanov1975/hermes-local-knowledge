# Verified routing: opt-in shadow mode

Shadow mode measures whether independently investigated routes are worth reusing.
Shadow never changes search ranking, tool results, feedback ratings, or indexed artifacts.
The separate opt-in [cached applicability veto](#cached-applicability-veto) can cancel
explicit promotions; the shadow contracts below do not authorize those vetoes.
It is **disabled by default**, independently of tool-OKF generation and implicit feedback.

## Enablement and provider boundary

Review the storage and token-use notice before opting in:

```yaml
local_knowledge:
  verified_routing:
    mode: shadow  # default: off; allowed: off, shadow, veto (see below)
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
Markdown/script source content to the profile's configured Hermes model provider. This
consumes additional model tokens; it is not local-only inference unless that
provider is local. Bounded text can still contain sensitive information: this is
not a general-purpose secret redactor. Do not opt in for conversations or source
trees that must not reach that provider. In shadow mode, search tool responses and
ordering are unchanged; only the optional input schema has grown.

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
4. The worker considers at most three lexically/shared-source shortlisted routes
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

Contract 2 adds the optional queue column `lookup_context` (default `{}`). Version
0.5.3b5 also adds `diagnostics` (default `{}`), migrated on a normal shadow write. Reports are read-only and do not migrate. Older stored
`ai_verified` outcomes remain historical evidence but cannot be reused by this
contract; legacy queued cases without the packet close unresolved without inference.
New exact observations can acquire a contract-2 case. There is no bulk rewrite.

Acquisition remains capped at six searches and 32 candidate metadata records.
Applicability reserves separate bounded metadata allowances for the complete
baseline (at most 30 records) and stored-route receipts (at most 32), then unions
them without further candidate admission. Each stored route is checked against
its complete current receipts independently, then fitting sets are packed by
source count and selected byte cost (shortlist order breaks ties). A rejected or
costly set cannot consume the next individually checked set's allowance. Common
IDs with different excerpt bounds cannot overwrite an admitted receipt. This is
bounded capacity selection, not a semantic relevance verdict; some viable routes
can still be omitted when their union does not fit. Both model evidence phases
retain limits of eight sources, 24 KB per selected source and 96 KB aggregate
selected source text. Independent candidate checking does additional bounded
local reads; it does not consume acquisition's separate evidence allowance.
The applicability phase may read baseline items only while they fit; unread items
remain explicit metadata/refusals. Investigator responses request at most 1,800
output tokens; verifier/applicability responses request at most 4,000 to accommodate
complete baseline reviews. These are output limits, not total token budgets.
The per-case invocation and per-batch time limits include all stages, but
host/provider retry and fallback policy can extend elapsed time and token use
behind each host call. Late work cannot publish after losing its lease. Interrupted
calls remain explicitly ambiguous rather than being automatically retried.

### Exact source inspection and coverage limits

Candidate metadata includes `read_status` and, when available, `file_bytes`.
Availability, unsupported types, confinement refusals, and size requirements are
selection advice, **not an all-baseline readability gate**. Every baseline ID
remains in the task and must receive a semantic review. A source that is clearly
irrelevant by metadata need not be read, including scripts or large documents.
Potentially useful unread evidence remains `unknown` and vetoes acceptance.

Ordinary `read` inspects a complete UTF-8 source up to 24,000 bytes. The investigator
can request `{action:"read_excerpt", id:"exact ID", start_line:201, end_line:280}`
for an exact inclusive range of at most 160 lines and 24,000 selected bytes from a
file up to 1,000,000 bytes. Registered Markdown types and scripts with `.py`, `.sh`,
`.bash`, `.cjs`, `.mjs`, or `.js` suffixes are supported under the existing confined
roots/exclusions and descriptor-pinned source opener. Scripts are never executed.
Obvious credential assignments, private-key headers and credential URLs in scripts
refuse the source rather than rewriting lines. This conservative guard may reject
non-secret assignments and is not a comprehensive secret detector; other sensitive
source text can still reach the configured provider under the opt-in disclosure.

An excerpt carries its original line numbers, exact bounds, total line/file sizes,
`complete` flag and SHA-256 of the **entire** bounded file. Citations must fall
inside inspected lines. Verifier refresh, exact reuse and semantic applicability
re-read the same bounds and check the whole-file hash, identity and age. Changes
outside an excerpt invalidate its receipt too. No source body is stored in the
queue or indexed, and these ranges are not retrieval chunks or chunk RAG.

Use `{action:"locate_source", id:"exact ID", query:"literal text"}` to locate
relevant sections in a selected file before choosing ranges. Navigation returns
up to 32 exact match locations and total line/file sizes, not source prose or
citable evidence. Matching is case-sensitive; query length is limited to 200
characters and navigation to six requests per investigation. A changed file hash
between navigation and inspection refuses the read.

Up to eight exact ranges per source accumulate within the 24,000-byte selected
source and 96,000-byte case limits. Overlaps are deduplicated; citations spanning
unread gaps are refused. An ordinary read after an excerpt attempts the full
source rather than silently returning partial evidence. Verifier refresh re-reads
the selected ranges; it independently judges them but cannot navigate to new
ranges during its single verdict call. To retain/equate a partially inspected
useful baseline, the review must explicitly say
`evidence_scope:"useful_evidence_only"`. This is a semantic judgment that the
inspected lines preserve evidence useful to this lookup, **not complete content
coverage**. The verifier must reject when omitted content could change the route
or coverage. Files above the scan limit, long lines above the selected byte limit,
unsupported types and unresolved coverage can still force abstention. Scripted
tests prove provenance/contract mechanics, not the correctness of those judgments.

### Bounded structural diagnostics

Private case receipts retain up to 64 read attempts (known indexed ID, fixed phase
and reason), capped stage/action counts, a fixed investigator abstention category,
and an explicit truncation flag. Unknown model-supplied IDs are not retained.
No free-text abstention rationale, source content, search text or model response is
added to diagnostics. Reports aggregate fixed keys/reasons, reject malformed
values and count legacy/malformed envelopes as unavailable; a bad receipt does
not hide the remaining operational totals. Receipts are written with the existing
claim/lease fence before calls and at case exit. An interrupted provider call
remains ambiguous. Reports do not migrate or rewrite old state.

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
or total tokens/billing. Shadow-mode search results still never change.

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

## Cached applicability veto

Candidate version 0.5.4b1 adds a separate opt-in behavior to the same private queue
and fenced background workers. The shadow lifecycle above remains shadow-only;
its route investigation, baseline coverage judgments and `would_reuse` outcomes
cannot cancel a promotion. In particular, metadata-only `baseline_review.not_useful`
is never rejection evidence. Neither installation nor upgrade enables vetoes.

```yaml
local_knowledge:
  verified_routing:
    mode: veto
    max_age_days: 30
```

Only an **actual successful explicit feedback promotion**, including a filtered
retry, is eligible. The private packet binds the exact original host task,
immediate query, effective filter/limit, pre-search `lookup` fields, resolved
profile/source/state namespace, selected feedback/artifact identity, and the
complete unassisted baseline and promoted metadata fingerprints. New endorsements
cannot inherit an old veto. Already-first and implicit routes are not cancelled.

Public tool-execution middleware conveys exact host scope around the real native
handler, including deferred dispatch, without serializing downstream tools. Its
scope resets after success or failure. Missing/unsupported identity, observer
races or unavailable original task mean abstention, not latest-task guessing.
Older hosts without middleware, direct CLI searches and caller-owned indexes
cannot borrow guessed host task authority. Caller-owned indexes remain unassisted.

The worker reads only the selected promoted source through the existing bounded
source reader, optionally using literal location and exact excerpts for larger
supported sources. It returns `applicable`, `inapplicable` or `uncertain`, never a
replacement route. A negative verdict requires checked read-source citations
positively establishing incompatible target/scope; an excerpt cannot establish
absence. Missing source/task information, recency alone, historical requests and
harmless edits are not semantic rejection evidence.

Search performs no model/network work: it consumes only the exact completed
source-only promotion receipt. `max_age_days`, current metadata, all source
locators/whole-file hashes and citation bounds must still validate. A valid
`inapplicable` cancels the promotion and restores the **entire** unassisted page,
not a truncated substitute. The rejected artifact remains ordinarily retrievable.
Usage records the caller-visible final page and `applicability_vetoed`, retaining
attempted feedback/artifact IDs and routing high-waters. Positive/uncertain,
missing, expired, corrupt, legacy, mismatched or changed evidence keeps incumbent
routing. Invalid completed evidence is captured/requeued asynchronously; changed
bytes invalidate receipts but are not themselves a veto.

Veto opt-in retains private task/lookup and promotion packets and sends selected
source content to the configured provider under the same disclosure above. It
uses the existing case/call/time/lease budgets and consumes additional preparation
tokens; source checks also cost bounded local reads. No positive AI routes,
semantic gains, token savings or correctness of AI judgments are established by
scripted controls. The normal off default and shadow-only result parity remain.

**Rollback:** set `local_knowledge.verified_routing.mode` to `off` to stop new
capture/vetoes/launches, or `shadow` to retain shadow diagnostics without vetoes.
Neither deletes private state nor kills already bounded work; allow it to finish
before snapshotting. No live enablement or deployment is part of this candidate.

## Operator commands

```bash
python -m hermes_local_knowledge.cli routing-report --hermes-home /path/to/profile --json
hermes local-knowledge routing-worker --hermes-home /path/to/profile
```

The worker uses host-owned model access in `shadow` or `veto` mode; it is not
a standalone inference client. `routing-report` is available only through the
standalone Python CLI. Reports expose counters/statuses, not request/source prose by
default. Disabling the mode stops new capture/launches but does not delete evidence
or kill an existing worker. The supervisor rereads configuration between children
and while waiting on leases, stopping further launches when the mode is off or the
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

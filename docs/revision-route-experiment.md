# Offline revision-aware promotion experiment

This is a **one-off experiment**, not a plugin feature or deployment recommendation.
The rejected runner and its harness tests are retained privately, outside this
repository; only this conclusion and portable relevance controls are committed.
The private implementation uses the pinned v0.5.3 managed service, FTS, feedback
selector, typed retry and native search adapter on disposable state.
Inconsistent inputs fail rather than rebuilding the frozen corpus.
It does not register host hooks, launch workers or call models.

## Private packet and CLI

Prepare a frozen JSON packet **outside this public checkout**, before scoring.
Each case names a frozen, current-format `state` containing `index.sqlite`,
`index.jsonl`, and optionally `usage.sqlite`. Its index must have been built for
exactly `root`. Keep source paths unchanged: root-scoped feedback is not remapped.
The runner SQLite-backs up inputs read-only into a temporary directory next to the
report; all native telemetry writes go there. It deletes that directory afterward.
Use frozen private inputs, not a concurrently changing production database.

```json
{
  "max_extra_ms": 2.0,
  "cases": [{
    "id": "controlled-meaningful-edit",
    "category": "meaningful",
    "provenance": "AI_constructed_controlled_revision",
    "task": "Recover a current-format synthetic archive",
    "root": "/private/frozen/source",
    "state": "/private/frozen/state",
    "query": "quartz archive restore",
    "type": null,
    "limit": 8,
    "acceptable": ["doc:quartz-archive-restore.md"],
    "harmful": ["doc:legacy-notes.md"],
    "receipts": [{
      "feedback_id": 17,
      "artifact_id": "doc:legacy-notes.md",
      "endorsed_source": "/private/revisions/legacy-before.md",
      "current_source": "/private/frozen/source/legacy-notes.md"
    }]
  }]
}
```

The IDs above are illustrative; use IDs returned by the actual frozen index.
`id`, `category`, `provenance`, `root`, `state`, `query`, `acceptable` and `harmful`
are required; `task` is optional label rationale, `type` defaults to no filter,
`limit` defaults to 8, and `receipts` defaults to empty. Native limit coercion,
query quoting and explicit filtering remain production-owned. A receipt binds
**exact explicit feedback ID + artifact ID**, not merely an artifact. The current
source must resolve to that artifact's exact indexed file (`SKILL.md` within the
indexed directory for a skill). The prior file is an
operator-prepared revision receipt, not proof of an actual historical endorsement.
Hash both revision files during preparation, outside timed calls. Missing files
produce unknown hashes and preserve incumbent results. Duplicate or malformed
bindings and wrong current-source paths fail the case.

```bash
PYTHONDONTWRITEBYTECODE=1 python /private/implementation/scripts/revision_route_experiment.py \
  /private/frozen/packet.json --output /private/results/report.json --rounds 8
```

Output must not exist and must be outside the checkout (including symlink targets).
The detailed report is owner-only (0600); stdout gives aggregate counts. Exit `0`
means bounded improvement on supplied labels, **not deployment approval**; exit
`2` means rejected/shelved. Invalid top-level inputs fail the invocation; per-case
preparation/search failures appear as error types, reject the assessment and do
not abort other cases. Do not publish private packets, reports or stdout containing
private case IDs.

## Exact candidate

Capture the full first unassisted `search_index` page while the real service runs.
After routing, only `promoted_existing` or `promoted_retry` with an explicit
feedback ID and exact bound receipt can trigger. If both prepared hashes are known
and differ, candidate returns **that captured page**, not a recomputed page or a
new route. It neither removes the artifact from candidate generation nor chooses
an alternate route. Already-first, no route, failed verification, implicit-only,
missing receipt and unchanged/unknown hash cases retain incumbent results.
A newer endorsement is unaffected by an older receipt, even for the same artifact.

This post-routing experiment **does not avoid the incumbent typed retry**. Native
telemetry records the production decision alongside the experimental final IDs in
throwaway state; it is not a proposed production telemetry schema. No original
usage row is rewritten or used as an experimental gate switch.

## Evidence and decision

Freeze source-grounded acceptable/harmful-primary judgments and provenance before
running. Include meaningful changes, harmless edits, historical intent, unchanged
older files, renewed endorsement, unknown/missing receipts, exact binding controls,
quotes, filters and limits. Constructed labels demonstrate failure modes, not
historical effectiveness or human judgments. Use actual-history cases without
receipts only as a disclosed compatibility lane; do not manufacture old receipts
from today's hashes.

Report includes complete normalized native baseline/candidate payloads (transient
usage/event IDs removed), actual first IDs, each selected
feedback ID/outcome, eligibility/trigger reason, exact gain/regression counts,
changed-first flags, provenance counts, packet/runner/input/source hashes and
state and receipt-file preservation checks. Receipt-file status is null when no
receipt files were supplied; other indexed sources are not verified by that check.
All first-result regressions (including harmless
and historical), changed harmful primaries, unjudged changed primaries, unstable
baseline-vs-baseline controls, preparation/search errors and preservation failures
reject the candidate. No gain also rejects it: route suppression alone is not a win.

Timing includes one first-service-call sample per case and repeated warm baseline/candidate pairs
in alternating order. Both lanes share capture/diagnostic instrumentation. Report
raw samples and medians for managed search, post-routing gate, native adapter,
actual native JSON serialization, and total response time. Adapter time includes
normal telemetry writes and result projection; total includes harness patching and
JSON decoding, not host transport/scheduling. The gate is measured separately,
with no file reads on timed paths. Candidate-minus-baseline warm median exceeding
packet `max_extra_ms` (default 2 ms, freeze before scoring) rejects that case.
This is a directional local noise budget, not statistical proof of no latency cost;
first-service-call timing does not imply a new process, cleared OS caches or cold SQLite pages.

## Portable relevance regressions

The public tests exercise real FTS and explicit promotion after harmless edits and
retirement for current use. The retired-format historical source remains useful
for the stated task; a byte mismatch alone must not reject that relevance evidence.
They require the target to be present below first place before promotion.

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest \
  tests/test_revision_route_regressions.py -q -p no:cacheprovider
python -m ruff check tests/test_revision_route_regressions.py
python -m mypy tests/test_revision_route_regressions.py
```

The private runner's separate twenty-test harness exercises the veto, full native
payloads, timing, binding, renewal, implicit-only behavior, typed retry, quote/type/
limit controls, frozen-input rejection and qualified preservation reporting.
Synthetic Quartz sources reproduce a meaningful-edit win **and** harmless/historical
regressions, so that quality packet legitimately rejects the byte-change veto. That
quality assertion uses a loose timing budget to avoid mixing scheduler jitter with
the semantic oracle. Other tests exercise explicit renewal/binding/hash controls,
real implicit promotion preservation, real typed retry, quote/type/limit behavior,
unjudged outcomes, private CLI serialization and untouched original source/state.
No production package file or version metadata changes are needed.

## Result of the bounded pilot

The executed packet contained eight constructed development/control cases and three
real-query current-corpus compatibility cases. All eleven completed without case
errors: one first-result gain, two harmful first-result regressions, and eight
unchanged first results. The three real cases had no historical revision receipts
and therefore supplied compatibility evidence only; none triggered the veto.

The meaningful-change case improved, but the harmless-edit and historical-intent
cases regressed. **Reject the unconditional byte-change veto for deployment.**
Revision identity can bound verification evidence; it does not establish whether
changed instructions remain useful. A future applicability-aware candidate needs
its own source-backed hypothesis and authorization, not an exception tuned to
these development cases.

The final timing run also exceeded the pre-frozen two-millisecond response budget
in two cases, including a no-trigger compatibility case. The gate itself was cheap,
but this run does not establish negligible end-to-end overhead; the relevance
regressions are independently sufficient to reject the candidate.

Focused tests, the full repository suite, Ruff, mypy and version-policy gates were
exercised. Full-suite host integration requires a supported Hermes installation in
the test interpreter. Avoid adding an entire Hermes checkout to `PYTHONPATH`, which
can shadow this repository's `tests` namespace. A strict editable installation of
a disposable host copy supplies the declared packages without exposing its tests.
Private packets, native payloads, timing samples and reports remain outside source
control. No production ranking, feedback, configuration or deployment was changed.

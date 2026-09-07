# Bounded search-change evaluation

The decision is whether a candidate helps users inspect the right whole artifact
first without introducing worse routes. Passing mechanical tests or leaving a
large historical corpus unchanged is not, by itself, evidence of improvement.
Use the smallest evaluation that can settle the proposed change; no fixed case
quota or new evaluator framework is required.

## 1. Define and freeze the decision

Before inspecting candidate outcomes, record the user-visible failure, the narrow
proposed change, and its acceptance gate. Assemble a small set containing:

- positive cases where the candidate should help, including existing known wins;
- difficult cases such as the right type but wrong task or environment;
- controls for quoted searches, explicit type filters, conflicting hints, and
  missing target types, as relevant to the candidate.

Record each task, exact query/filter/limit, acceptable first results, and clearly
harmful first results from source evidence. Multiple artifacts may be acceptable.
Do not invent a historical/replacement relationship without evidence. Freeze the
cases, expectations, code revision/diff, and corpus snapshot identity before the
comparison. Development cases that informed the hypothesis remain development
evidence, not an independent holdout. Keep selected failures; do not rewrite
queries after seeing results to obtain a passing gate.

## 2. State where judgments came from

Distinguish historical user tasks from constructed tasks, and human judgments
from AI judgments and deterministic synthetic assertions. For AI judgments,
disclose who authored the tasks and whether the judge had already seen rankings.
Another AI checking arithmetic or sources does not make the labels human gold or
establish independent relevance validation.

When assistant judgment is authorized, decide straightforward relevance from
artifact evidence. Verify factual uncertainty against sources; ask the user only
when personal preferences or meaningful tradeoffs remain unclear. Do not ask for
rubber-stamp approval of obvious matches. If a specifically human-labeled
benchmark is needed, use the separate [human finalizer](lean-human-benchmark.md)
with its explicit human decisions; never feed AI labels into it as human approval.

## 3. Compare once, then inspect the changes

Run incumbent and candidate on the same frozen inputs through their actual search
paths. A post-retrieval experiment may transform the exact incumbent page, but
record that boundary rather than implying a full production implementation.
Evaluation must not write live telemetry, feedback, or index state. Existing
[feedback replay and historical comparison tools](../README.md#feedback-and-evaluation)
provide complementary evidence; extend them only when a concrete decision needs
it. Keep private snapshots, tasks, labels, and reports outside this public repo.

Report separately:

- total cases, eligible cases, actual candidate triggers, and changed rankings;
- gains and regressions, inspecting every changed first result against the task;
- unchanged cases (which are not improvements);
- unjudged results (which cannot silently count as acceptable);
- uncovered cases where the intended failure mode was not exercised.

A zero-trigger historical corpus can show compatibility on those queries, not
whether the new rule is useful. An unchanged control can pass its no-change
assertion; an unchanged challenge that never exercised its intended distinction
remains uncovered. Preserve result membership and unrelated relative order where
the candidate promises a stable promotion, and check full control lists rather
than only their first result.

## 4. Stop with a decision and a reusable regression

Accept only when the named benefit reproduces, no clearly worse or misleading
primary result is introduced, relevant controls/invariants pass, and the change
is small enough to justify its benefit. Unjudged or uncovered material cases
limit the conclusion; they are not permission to pass. Otherwise reject, shelve,
or make one evidence-backed narrowing with a fresh bounded check. Do not expand
the evaluation indefinitely to rescue the hypothesis. Evaluation approval never
supplies deployment authorization.

Preserve a discovered failure as a portable synthetic regression using existing
test helpers, not private artifact names or copied telemetry. For example,
`test_mixed_quote_script_to_write_keeps_isolated_guidance_first` in
`tests/test_search_contract.py` protects guidance needed to **write** a script:
a terminal `script` token must not blindly promote an existing script for the
wrong environment. The fixture requires that distractor to remain in the result
page, so absence cannot make the protection pass vacuously. Verify sensitivity
by temporarily applying the bad promotion outside the committed code and seeing
the regression fail, then run the normal test unchanged and require it to pass.
This proves protection against that specific mistake, not general search quality.

Run the focused regression, relevant existing tests, and the repository's
[development gates](../CONTRIBUTING.md#development-setup), then review the exact
diff. Tests and documentation can be the useful outcome of a rejected experiment;
do not check in its failed ranking rule or one-off private replay machinery.

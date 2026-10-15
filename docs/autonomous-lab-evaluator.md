# Trusted coverage-aware public-search evaluator (#68)

This operator-invoked controller compares **exact Git commits** in separate fresh
containers. It neither launches workers nor schedules, promotes or deploys anything.
The runtime remains the existing immutable #65 image, with official Hermes source
revision `90194c64ff99933ec3b9ecfd9295b06e6c11e038`. No new image or model call is needed.

## Inputs and invocation

Use a trusted checkout containing `scripts.lab_evaluator`, and an explicit Python
interpreter. Keep every benchmark, corpus and attempt path private and outside Git.
The attempt path must not exist. The controller uses only the standard library and
existing Docker; it refuses a missing image before launching anything.

```sh
python -m scripts.lab_evaluator \
  --plugin-repo /path/to/candidate-git-repository \
  --incumbent '<exact-40-hex-incumbent-commit>' \
  --candidate '<exact-40-hex-candidate-commit>' \
  --image 'sha256:<existing-immutable-lab-runtime-image-id>' \
  --corpus /private/corpus --manifest-sha256 '<approved-manifest-sha256>' \
  --frozen /private/completed-benchmark/frozen.json \
  --frozen-sha256 '<approved-frozen-file-sha256>' \
  --policy /private/coverage-policy.frozen.json \
  --policy-sha256 '<approved-policy-file-sha256>' \
  --development /private/completed-benchmark/worker-development/development.json \
  --development-sha256 '<approved-development-file-sha256>' \
  --composite-id '<approved-composite-id>' \
  --attempt /private/evaluation-001 --name lk-lab-evaluation-001
```

Supply independently approved pins, not hashes recomputed from possibly changed
files at invocation. `--smoke-cases-per-split 2` runs a small genuine native smoke
using the same frozen benchmark and full source inventory. A smoke is always
incomplete and **cannot** return score eligibility, even if its scores improve.
Do not infer full-campaign quality or latency from a smoke.

The loader explicitly supports `private-baseline-plus-coverage-supplement/v1`
with `source-appropriate-coverage-minima/v1`. It does not alter the older #67
universal-five preparation validators. It verifies file pins, corpus membership
and bytes, one-to-one native/logical mapping, coverage minima, all case IDs and
queries, target membership, development-only export and the partition rule:
for each origin sort by SHA256 UTF-8 `(origin + '\n' + case_id)`, then case ID;
use zero development cases for zero questions, otherwise
`min(3, max(1, n - 1))`, with the remainder held out. Repeated target citations
are preserved unchanged and scored as membership in the acceptable target set;
they are not ambiguous native/logical mappings. No cases or labels are repaired.
The source-appropriate exceptions and zero/no-heldout origins remain disclosed.
Labels are AI-validated synthetic, including disclosed parent-authored fallback,
not human gold. Unlisted returned targets are unjudged, not proven irrelevant.
No new generation, calibration, target expansion or source refresh occurs here.

## Fixed protocol `lk-coverage-public-search-v1`

1. Export the exact Git object twice into a new candidate workspace and installed
   plugin directory. Omit only the five enumerated evaluator/controller files
   from both disposable exports (recorded in `runner.json`); the exact candidate
   runtime package is untouched. Comparator source/tests never enter a candidate
   mount, including when evaluating later commits containing this controller.
   Never copy a dirty worktree or production profile. Mount the
   same verified complete corpus read-only. Build the candidate's own index using
   the established #66 adapter and verify every mapped artifact, SQLite integrity,
   official discovery/dispatcher and effective no-learning configuration.
2. Reuse the original non-root, read-only-root, capability/resource-limited,
   `--network none` execution boundary. Add only read-only mounts for the existing
   baseline adapter, query-only native probe and three controller-selected public
   test files. The original four-bind verifier is not loosened. No authentication,
   Docker socket, benchmark, expected targets or comparator code is mounted.
3. Run controller-supplied `test_public_contract.py` and `test_plugin.py` with the
   image's pinned test interpreter against the exported candidate package. Record
   exact trusted test hashes and output. Actual official-Hermes integration uses
   its unchanged interpreter and native `model_tools.handle_function_call`.
4. Make exactly three unscored native warmups, in this order:
   `local knowledge evaluator warmup alpha`, `local knowledge evaluator warmup beta`,
   `local knowledge evaluator warmup gamma`. Refuse any collision with scored
   questions. Store warmups separately; do not include them in percentile samples.
5. Execute development then validation, each lexicographically ordered by the
   unchanged case ID. Send only a sequence number and the unchanged query over
   stdin to the probe. Make exactly one public `knowledge_search` with `limit=5`,
   `rebuild=false` per question. No retries, rewriting, feedback or label learning.
6. Measure `perf_counter_ns` on the **controller**, immediately before sending the
   query through receipt of the complete matching response frame. This includes
   transport, dispatch, search and any query-time model work; excludes index
   construction and trusted tests. The timing is conservatively wider than the
   native call. Compute nearest-rank p95: sorted sample `ceil(0.95*N)-1`, pooling
   both scored splits after warmup. Individual readiness/response reads have a
   300-second transport timeout; no whole-experiment elapsed allowance exists.
   Complete diagnostic lines share a cumulative 4 MiB controller-memory allowance
   across all queries and the bounded final EOF drain. The current incomplete
   frame is independently capped at 4 MiB. Excess diagnostics fail the run rather
   than silently truncate or exhaust controller memory; retained diagnostics stay
   in the private attempt even on failure.
7. Score actual returned top-five IDs through the frozen one-to-one logical map.
   Report acceptable-target Hit@5 and Hit@1 per split, separately from exact-origin
   Hit@5. Reports include integer hit counts, denominators and explicit `_rate`
   proportions. Neither origin association nor graph adjacency establishes relevance.
   Malformed, duplicate, unknown-ID and error results fail closed, without retry.
8. Verify no feedback/implicit-feedback rows or generation queue, unchanged seeds,
   sources, configuration, trusted tests and both plugin exports after execution.
   Official Hermes secures the disposable config to mode 0600; a scoped existing
   setup helper restores only the stopped disposable export's controller ownership
   for host-side verification. Remove the exact owned container and read back absence.

## Evidence and verdict

The attempt root is mode 0700. Controller-owned `private-cases.jsonl` (0600),
`warmups.json`, runner/test identities, image and before/after Docker inspections,
logs and aggregate reports stay **outside all candidate mounts**. Candidate output
contains only its own native/index integration diagnostics, not held-out labels
or comparisons. Stdout contains aggregate scores/verdicts only. Execution errors
produce a noneligible aggregate with the failing role and error class; detailed
private evidence remains in the failed attempt. Never expose private per-case
reports to implementation workers. Workers may receive only the existing
`worker-development` export and aggregate validation metrics.

Eligibility requires complete runs, strict validation Hit@5 improvement, no Hit@1
loss on either split, p95 <= 1 second for both measured revisions, no errors or
source/identity drift, passing trusted tests and native official integration.
Equality is rejected. `promoted` is always false: independent review and the actual
lab-promotion action belong to #69. Full-campaign acceptance remains a separate
operator-controlled run; do not remove smoke restrictions to manufacture a verdict.

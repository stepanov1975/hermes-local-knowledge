# Lean human benchmark

Use `scripts/lean_human_benchmark.py` to make a bounded local deployment decision about one deterministic ranking change. It is intentionally not a population-level retrieval study.

Private task text, annotations, mappings, indexes, decisions, rankings, and reports must remain outside this public repository. Generated JSON is written with mode `0600` under mode-`0700` directories, and the writer rejects output paths inside this repository.
On POSIX systems, an existing output directory must already have mode `0700`; the evaluator never changes permissions on caller-owned directories.

## Contract

The script is the single authoritative validator. It binds a finalized benchmark to:

- the blinded case packet;
- its private item-to-artifact mapping;
- both draft annotation inputs;
- the frozen index;
- the exact human-review packet.

Finalization and replay use one private snapshot of the supplied frozen index for hashing and every subsequent read, so an atomic index rebuild cannot mix snapshots under one recorded hash. Replay always loads ranking code from the reviewed checkout and requires a result limit of at least three because the report includes Hit@3.
The two draft annotation inputs must have distinct annotator identities, case filters must name a supported index artifact type, finalized item labels are validated before scoring, and an output path may not alias any command input.

Every case must have an explicit human decision. `accepted_proposal` accepts the deterministic draft proposal for that case; `edited` accepts the proposal except for explicit `none_needed` or item-field overrides. Finalization rejects missing or extra cases, unknown item handles, mapping drift, index drift, unresolved decisions, and mapped artifacts absent from the frozen index.

The draft proposal is deliberately conservative and is never human gold by itself:

- relevance: maximum of the two draft grades;
- canonical/current: true only when both drafts say `canonical_owner` and `current`;
- harmful as primary: true when either draft says harmful;
- `none_needed`: set only when both drafts agree.

## Commands

```bash
PRIVATE="${XDG_STATE_HOME:-$HOME/.local/state}/hermes-local-knowledge/lean-benchmark"

python scripts/lean_human_benchmark.py prepare \
  --packet "$PRIVATE/calibration-packet-v1.json" \
  --mapping "$PRIVATE/calibration-mapping-private.json" \
  --rater-a "$PRIVATE/rater-a.json" \
  --rater-b "$PRIVATE/rater-b.json" \
  --index "$PRIVATE/index.sqlite" \
  --output "$PRIVATE/review.json"

python scripts/lean_human_benchmark.py finalize \
  --review "$PRIVATE/review.json" \
  --decisions "$PRIVATE/decisions.json" \
  --packet "$PRIVATE/calibration-packet-v1.json" \
  --mapping "$PRIVATE/calibration-mapping-private.json" \
  --rater-a "$PRIVATE/rater-a.json" \
  --rater-b "$PRIVATE/rater-b.json" \
  --index "$PRIVATE/index.sqlite" \
  --output "$PRIVATE/benchmark.json"

python scripts/lean_human_benchmark.py replay \
  --benchmark "$PRIVATE/benchmark.json" \
  --index "$PRIVATE/index.sqlite" \
  --output "$PRIVATE/incumbent.json"

python scripts/lean_human_benchmark.py compare \
  --benchmark "$PRIVATE/benchmark.json" \
  --baseline "$PRIVATE/incumbent.json" \
  --authority "$PRIVATE/authority.json" \
  --index "$PRIVATE/index.sqlite" \
  --output "$PRIVATE/report.json"
```

## Candidate boundary

The included candidate is intentionally narrow. It only uses high-confidence, policy-eligible `superseded_by` relationships from historical, plan, or retired artifacts. When both source and successor already appear in an incumbent result, it moves the successor before the source and preserves membership and unrelated ordering. It does not inject artifacts, interpret broad role labels, or automatically authorize release.

The `compare` command first replays the bound frozen index and requires an exact match with the supplied incumbent rankings. It then generates the candidate internally from the bound authority input; it never accepts an externally supplied candidate ordering. The report binds the exact benchmark, verified baseline, authority, and generated candidate hashes, then compares acceptable top-1/top-3, canonical-current top-1, harmful top-1, unjudged top-1, and `none_needed` behavior. Every changed top-1 case still requires manual inspection. A report never produces an automatic release verdict.

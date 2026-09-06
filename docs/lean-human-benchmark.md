# Lean human benchmark finalizer

Use `scripts/lean_human_benchmark.py` to turn a blinded case packet into one compact, explicitly human-labeled benchmark. The script is a source-checkout development tool; it does not change plugin behavior or ship a ranking policy.

## Scope

The finalizer does two things:

1. `prepare` creates a blinded review template.
2. `finalize` reconstructs that template from the original inputs and materializes labels only when every case and item has an explicit human decision.

It deliberately does **not** merge LLM draft annotations, generate a reranker, replay search, calculate metrics, or make a release decision. Candidate evaluation belongs in the existing evaluation path and should be added only for a candidate that can fix a named retrieval failure.

## Private-file boundary

Packets, mappings, review files, frozen indexes, and benchmarks can contain private task text or local artifact identities. Every CLI input and output must therefore resolve outside every Git-registered worktree of this public repository.

On POSIX systems:

- input files must grant no group or other permissions;
- each input's parent directory must be owner-only;
- generated directories use mode `0700`;
- generated files use mode `0600`;
- the frozen index is copied once into a mode-`0700` temporary directory and read from that exact snapshot.

The script fails before reading or copying private inputs on Windows because this dependency-free tool cannot guarantee a restrictive DACL.

## Human-label contract

`prepare` validates exact packet/mapping case and item coverage, verifies mapped artifact metadata against the frozen index, and emits no artifact IDs. It leaves these fields unset for every case and item:

- case `none_needed` and nonempty `rationale`;
- item `relevance` (`0` through `3`);
- item `canonical_current` (`true` or `false`);
- item `harmful_if_primary` (`true` or `false`).

Set a nonempty top-level `reviewer` and fill every field. Do not remove or edit copied task, query, context, or item-card fields.

`finalize` reloads the original packet, mapping, and frozen index; reconstructs all static review content; requires exact case/item coverage and explicit labels; and rejects source, identity, metadata, or static-field drift. The benchmark records canonical JSON content hashes for the packet, mapping, and completed review, plus the exact byte SHA-256 of the frozen index snapshot.

## Commands

```bash
PRIVATE="${XDG_STATE_HOME:-$HOME/.local/state}/hermes-local-knowledge/lean-benchmark"

python scripts/lean_human_benchmark.py prepare \
  --packet "$PRIVATE/packet.json" \
  --mapping "$PRIVATE/mapping.json" \
  --index "$PRIVATE/index.sqlite" \
  --output "$PRIVATE/review.json"

# Fill reviewer, rationale, none_needed, and every item label in review.json.

python scripts/lean_human_benchmark.py finalize \
  --review "$PRIVATE/review.json" \
  --packet "$PRIVATE/packet.json" \
  --mapping "$PRIVATE/mapping.json" \
  --index "$PRIVATE/index.sqlite" \
  --output "$PRIVATE/benchmark.json"
```

The output is human gold for this bounded local benchmark only. It is not evidence that any candidate is better, and it never authorizes deployment by itself.

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
- every generated directory component uses mode `0700`; an already-existing output parent must already be mode `0700`;
- generated files use mode `0600`;
- the frozen index is copied once into a mode-`0700` temporary directory and read from that exact snapshot.

The script fails before reading or copying private inputs on Windows because this dependency-free tool cannot guarantee a restrictive DACL.

## Human-label contract

`prepare` validates exact packet/mapping case and item coverage, verifies mapped artifact metadata against the frozen index, copies the packet's nonempty labeling instructions into the review, and emits neither artifact IDs nor item-card text. It leaves these fields unset for every case and item:

- case `none_needed`;
- item `relevance` (`0` through `3`);
- item `canonical_current` (`true` or `false`);
- item `harmful_if_primary` (`true` or `false`).

Packet authors must provide only deliberately redacted or synthetic `user_request` and `search_query` inputs. `preceding_context` and the full item cards are accepted as source-only inputs so the packet remains hash-bound and can be consulted privately during review, but none of their message, summary, description, locator, citation, or excerpt text is copied into a generated review or benchmark. `prepare` replaces each packet item identifier with an HMAC-derived opaque review handle and adds a 1-based `item_number`; correlate a review row with the original private packet only by case and item number. Put any source context that must appear in the review into a deliberately redacted or synthetic `user_request` instead.

Set a top-level `reviewer` identifier using 1–64 ASCII letters, digits, `.`, `_`, `@`, or `-`, then fill every decision field. The review accepts no free-form human text. Do not remove or edit the copied instructions, task, query, item numbers, or opaque item handles.

`finalize` reloads the original packet, mapping, and frozen index; reconstructs all static review content; requires exact case/item coverage and explicit labels; and rejects source, identity, metadata, or static-field drift. The benchmark records canonical JSON content hashes for the packet, mapping, and completed review, plus the exact byte SHA-256 of the frozen index snapshot.

## Commands

```bash
PRIVATE="${XDG_STATE_HOME:-$HOME/.local/state}/hermes-local-knowledge/lean-benchmark"

python scripts/lean_human_benchmark.py prepare \
  --packet "$PRIVATE/packet.json" \
  --mapping "$PRIVATE/mapping.json" \
  --index "$PRIVATE/index.sqlite" \
  --output "$PRIVATE/review.json"

# Fill reviewer, none_needed, and every item label in review.json.

python scripts/lean_human_benchmark.py finalize \
  --review "$PRIVATE/review.json" \
  --packet "$PRIVATE/packet.json" \
  --mapping "$PRIVATE/mapping.json" \
  --index "$PRIVATE/index.sqlite" \
  --output "$PRIVATE/benchmark.json"
```

The output is human gold for this bounded local benchmark only. It is not evidence that any candidate is better, and it never authorizes deployment by itself.

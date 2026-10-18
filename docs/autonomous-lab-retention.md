# Protected post-finalization retention (#71)

`scripts.lab_retention` is a standard-library-only **post-finalization disposal**
module. It is separate from the existing execution-safety lifecycle: sessions,
evaluations and full checks already remove containers and restore ownership
before publishing reports. Retention does not rewrite that lifecycle, recover an
attempt, replay inference, launch work, or change the incumbent.

After controller death, explicit execution recovery first establishes quiescence
of all identity-matched workers/helpers, then restores only the exact setup-receipt
exports from sandbox ownership before settlement or a successor generation.
It does not guess export roots or follow links. Restoration failure preserves
pending/recovery protection; retention does not replace that recovery step.

Full-check containers use an init reaper so orphaned test descendants do not
accumulate against the unchanged 256-PID allowance. The read-only/no-network
worker boundary and non-executable temporary mounts remain unchanged.

## Bounded inventory and reads

Every cleanup, including saved-plan retries and expiry, first checks the whole
attempt with a streaming metadata-only walk. Limits are **50,000 entries**,
**64 path components**, **128 MiB per regular file** and **2 GiB aggregate
logical file size**. Empty directories and hardlinked names count. Sparse logical
size is checked before opening content; reads use fixed chunks and reject growth
or identity/type replacement. Raw, disposable and protected evaluator evidence
are included. Each later inventory/hash/validation pass has its own finite
aggregate allowance, so this is not a claim that cleanup reads at most 2 GiB total.
The file limit accommodates supported 64 MiB command streams rather than applying
the smaller source-workspace allowance to raw evidence.

Budget violations retain evidence and return fixed actionable retention blockers;
they never silently truncate an inventory or expand an immutable disposal plan.
Oversized attempts require operator inspection. There is no automatic migration
or permission to remove frozen inputs.

## Integration boundary

- `cleanup_finished(campaign: Path, *, now: float) -> dict` requires the caller to
  hold the existing campaign flock. Before reading the manifest or mutating any
  evidence, it requires the registered lock fd and checks its device/inode against
  this campaign’s `lock` file (as `run_under_existing_lock` does). Missing, foreign
  or replaced lock identities fail closed. It does **not** acquire another flock.
- `cleanup_campaign(campaign: Path, *, now: float) -> dict` is the lock-taking
  standalone Python wrapper. Never nest it inside a held campaign lock.
- `now` is an explicit finite nonnegative Unix timestamp. The caller supplies its
  clock; tests use deterministic timestamps.
- Results contain `cleaned` readback receipts and `blocked` entries with fixed
  reasons. Per-attempt missing/drifted evidence is retained and reported, not
  treated as a successful best-effort deletion. Campaign-level malformed state,
  unknown space and low space can raise instead. The automation owner must pause
  and send its alert **before cleanup**, not treat disposal as low-disk recovery.

Only manifest report pointers with the exact report SHA-256, matching attempt
identity, a finalized outcome and no reconciliation flag qualify. A pending
attempt is ineligible even when it has a published error report. Unreferenced
attempts and archived orphan reports are never disposal targets. Reproducible
report, previous-incumbent, initial-incumbent, current-incumbent and candidate
revisions must resolve to exact commits in the campaign bare repository. Added
`refs/lab/retained/<revision>` refs are read back and never removed; existing Git
refs and the incumbent manifest remain unchanged.

Exact resource receipts must exist. `lab_recovery.inspect_resources` establishes
identity/quiescence; running, foreign and unknown resources block cleanup.
Identity-matched stopped resources may be settled with `settle_resources`, then
all exact names must read back absent. No global container listing, force removal,
Docker prune, or volume sweep is introduced. A daemon failure is not absence.
Conclusive request/response and tool completion is required before any disposal.
Legacy attempts without resource receipts are conservatively retained.

## What can be disposed

The first eligible invocation durably writes and reads back
`attempt/retention-plan.json`. It enumerates individual entries (kind, inode,
device, file hash or link target) under only these fixed paths:

- `candidate.index` at the attempt root;
- `implementation/{work,home,inputs,scratch,output,candidate.index}`;
- `review/{work,home,inputs,scratch,output,candidate.index}`, including numbered
  recovery generations;
- `{evaluation-incumbent,evaluation-candidate,full-checks}/{work,home}`, including
  numbered recovery generations;
- `full-checks/check-inputs/{exact-candidate,tools}`, including generations.

It never deletes the whole attempt, private directory, generation, or arbitrary
paths found in a worker result. Evaluator `output/` contains native-baseline,
coverage and index-identity evidence and therefore **survives**. Evaluator
`runner.json` is identity metadata, not a runner sandbox; `trusted-tests/` is the
controller-pinned source copy named by its hashes. Both survive for reproducibility.
The structural `full-checks/check-inputs/inputs.json` also survives. Frozen corpus/benchmark,
check-tool bundle, campaign config, manifest, reports, orphan archives,
controller/checkpoint, completion, resource, cleanup, result and usage records
remain protected. Configured inputs that overlap a proposed disposal root block
cleanup rather than being removed.

Top-level paths and all their ancestors must not be symlinks. Internal sandbox
symlinks are explicitly inventoried and unlinked without following their targets.
Special files (including FIFOs and sockets), replacements and unplanned new
children block cleanup. Ordinary regular hardlinks are allowed: retention owns
only the enumerated disposable names, not every link to their inodes. Unlinking
an owned alias leaves any outside/protected alias and its file bytes/inode intact;
retention never writes or truncates the shared file. Every remaining planned name
still must match its recorded kind, device, inode and content hash. Link counts
are deliberately not a plan invariant: deleting one alias can lower the count
before an interrupted disposal resumes from the same immutable plan. This is a
deletion-only policy; candidate workspace import/write validation continues to
reject hardlinks. Deletion proceeds leaf-first with nonrecursive unlink/rmdir and
parent-directory fsync; no recursively following wildcard remover is used.
This assumes the trusted controller owns its receipts and the campaign flock
fences controller activity; it is not a filesystem isolation boundary against a
concurrent malicious host administrator.

## Raw retention and durable evidence

Raw files are kept for exactly **at least seven days from the durable plan's
`finalized_at`**, never from mtime. Existing publication has no durable wall-clock
finalization timestamp, so the first verified post-finalization observation starts
the clock conservatively; this can retain older attempts longer than seven days.
Retries never reset that start. At `now >= raw_delete_after`, only the originally
enumerated raw files are eligible:

- session `private/request-N.json`, `wire-N.json`, `response-N.json`;
- session `private/tool-N-N.stdout` and `.stderr`;
- evaluator `transport.stdout` and `.stderr`, `trusted-tests.stdout` and `.stderr`;
- evaluator `private-cases.jsonl` and `warmups.json` (per-query responses/timings);
- full-check `full_pytest`, `ruff`, `mypy` `.stdout` and `.stderr` streams;
- the same named raw stream copies under controller-owned `review-proof/`,
  including numbered recovery generations. Their hash/size and check/input/output/cleanup
  metadata remain protected; the duplicate raw bytes do not bypass seven-day expiry.

Before securing the plan, every request/wire must have a response and every tool
intent must have its receipt, with resources conclusively quiescent. The durable
plan preserves per-file hashes, response usage (unknown is not zero), report
usage, settled-completion evidence, exact resource observations and hashes of
all structural/unknown regular attempt evidence outside the disposal set. Evaluator
`aggregate.json`, `runner.json`, `trusted-tests.output.json`, `transport.stderr.json`
and `cleanup.json` remain hash-protected alongside output identity reports. Tool
JSON and `.output.json`, status/result/session metadata, native stderr metadata
and unknown private files are retained; these structural records may include
bounded previews and are not claimed to be a purge of all raw-derived content.

Restart reuses the immutable plan; missing planned entries can represent a
completed deletion, but drifted identities, new children or structural evidence
changes block further disposal. Partial raw-pair deletion after expiry does not
reclassify the remainder as an ambiguous model call: completion was already
secured in the plan. `retention-result.json` records the plan hash, report hash,
disposable completion and raw-expiry completion after readback. Interruption
before that receipt is safe to retry. If persistence fails before the plan, no
files have been deleted. Existing plans are immutable: a plan written by an older
version is not expanded to newly recognized raw names; those files remain protected
until an explicitly reviewed migration (none is performed here).

## Verification scope

`tests/test_lab_retention.py` uses actual temporary files and bare Git repositories
with synthetic finalized reports. Exact Docker inspection is replaced by owned
fixture readback; no real Docker or provider commands execute, and no original
campaign/shared artifacts are touched. These tests prove retention behavior, not
an original-campaign cleanup or real Docker integration run.

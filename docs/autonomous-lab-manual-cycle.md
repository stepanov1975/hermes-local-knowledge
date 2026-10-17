# Manual experiment and lab-incumbent cycle (#69)

This is an explicitly invoked private lab workflow, not a scheduler, release or
production-deployment command. The initial incumbent is the deliberately pinned
plugin revision, **not** the controller checkout's HEAD. Keep corpus, questions,
targets, partitions, timing protocol and image fixed for the campaign.

## Trust boundary

The trusted controller retains the benchmark, validation labels, evaluator,
Docker access and lab-only model authentication. A model session receives only
approved plugin source, the frozen corpus, the development-only export and safe
prior experiment reports. Its only execution tool runs a shell command **inside**
a disposable, non-root, network-disabled Docker container. Model commands are
never interpreted by a host shell. No model credentials, Docker socket, production
profile, broad host mount or hidden validation cases enter the sandbox.

Inference uses the unmodified official Hermes auxiliary adapter in the separate
controller environment. Implementation and independent review are distinct
`gpt-6.1-sol` / `openai-codex` / `high` conversations. The adapter must inspect
emitted requests and record actual returned usage; constructor arguments alone
are not proof of effective settings. Missing usage is unknown, not zero. There is
no whole-experiment elapsed cutoff or separate monetary/token allowance. Provider
request timeouts are transport failures, not a campaign-duration policy. Auth,
quota or ambiguous inference failure pauses instead of silently retrying or
falling back.

Implementation edits must be made directly in writable `/work/plugin`, the only
candidate-import workspace. `/scratch` and `/output` are writable auxiliary
locations, not automatic import channels. Scratch checks and advisory lessons must
identify the source path/digest they exercised, not claim verification of another
tree. Set `no_change=true` only when the imported workspace has no candidate edits.

The review sandbox receives a hash-bound, deliberately **filtered projection** of
the stopped/frozen candidate read-only, plus safe execution evidence. It does not
receive the complete Git tree or the full trusted-check source overlay. It has
independent writable scratch state, not the
implementation session's memory. Review checks benchmark shortcuts, official
compatibility and agreement between the draft report and actual execution.
Neither a worker's claimed test result nor the model's approval substitutes for
controller-owned verification.

## Reviewer projection and proof contract (#71)

`review-evidence.json` (`lab-review-evidence/v1`) explicitly identifies the full
candidate commit/tree separately from admitted files. `projection` contains each
admitted and omitted path's SHA-256, Git object ID and Git mode, the positive
`lab_agent.permitted/v1` policy and controller policy-source digest. Omitted
members carry a policy reason; their code bytes remain excluded. The canonical
projection digest binds this manifest. Git archive modes are exported as canonical
0644/0755 rather than archive-dependent group-write bits. Before dispatch and after
completed review, the controller checks the actual admitted bytes, complete file
inventory and permission modes, and rejects source or evidence drift.

The only additional bind is session-local `/inputs/review-proof`, read-only.
It contains `checks.json`, the full-check `inputs.json` manifest and the three
checks' named `.output.json`, `.cleanup.json`, `.stdout` and `.stderr` files.
Each file is size/SHA-256 bound; `checks.json` also binds the per-check receipt
hashes, commands, exact full revision, image, controller, candidate/overlay hashes
and input-manifest hash. Raw logs are streamed to bounded controller files and
copied intact, never inlined into the prompt. Any stdout/stderr previews inside
output JSON are **incomplete previews**, not substitutes for the full streams.
Trusted checks disable pytest traceback/captured-output/warning excerpts and short
failure summaries (older pinned pytest can include assertion text there), plus
Ruff snippets and mypy pretty excerpts. Progress/count diagnostics and exit codes
remain, without exporting controller implementation lines. Controller-owned raw
stream copies in `review-proof/` follow the same seven-day expiry as the originals;
projection, hash/size, input/output/check and cleanup metadata remain protected.
Check receipts must agree on worker ID, exit code, byte sizes and confirmed
cleanup. Missing, changed, extra, symlinked or special proof files fail closed.

Full trusted pytest/Ruff/mypy ran against full Git source **plus controller-owned
tests/scripts/pyproject.toml**, not just this admitted projection. `inputs.json`
provides candidate, effective overlay and tool digest maps, without those excluded
code bytes. Runtime package digests must still match the candidate. These are
controller-supplied receipts to challenge for content and correspondence, not an
independent attestation or a direction to trust pass booleans.

The deliberately small runnable reviewer scope is:

```sh
/opt/lab-tools/bin/python -m pytest -q -p no:cacheprovider tests/test_config.py::test_verified_routing_defaults_are_off_and_immutable
```

This tests admitted configuration-default behavior; it is not a full compatibility
or suite claim. The declared node is exercised by an offline filtered-export
regression; pinned-image execution is a separate integration acceptance gate.
Other admitted tests can import omitted scripts, so full-suite collection and
full Git-tree reconstruction are intentionally unsupported here. Reviewers should
run that node, inspect admitted changes and actual full-check evidence, and reject
incomplete or contradictory proof rather than approving a different tree.

Neither implementation sessions nor their writable scope inherit proof mounts.
The proof copy never mounts a campaign parent, original pilot, private case/warmup
or held-out-label data, authentication, controller implementation source or frozen
hidden files. Filenames/digests for omitted source and overlay are metadata only.
A synthetic test fixture is explicitly labeled synthetic and does not establish
production checks or real reviewer approval. Historical rejected pilot reports
and their `approved=false` decisions remain immutable; this offline repair does
not authorize another model call, recovery replay or promotion.

## Durable state and outcome

Create the experiment record before launching a session. Keep a hypothesis,
strategy, references to related attempts, changes, reproducible revision,
executed checks, measured results, outcome and lessons for **every** attempt.
A no-change outcome is legitimate. Before scoring, full checks or review, the
controller requires the explicit `no_change` decision to agree with imported
file differences in both directions. A contradiction produces a durable error
report with a fixed `delivery_validation` reason, retained usage/cleanup evidence
and unchanged incumbent; auxiliary output files are not silently applied.
Repeating an apparently unchanged failed strategy needs a stated new premise or
materially different implementation.

Freeze and validate the candidate before tests, trusted public-search evaluation
and independent review. Bind evidence to its exact revision and campaign inputs;
edits invalidate affected gates. The accepted incumbent changes only after all
required score, test, official-integration and independent-review gates pass.
Equality, incomplete evidence, regression or rejection leaves it unchanged.

Publish an immutable report and reproducible candidate before atomically replacing
the single authoritative incumbent manifest under the campaign lock. The next
fresh run reads that manifest; it does not rebuild the fixed runtime image.
Interruption must leave either the previous valid authority or the complete new
one, never a half-published incumbent. An unfinished attempt remains visible and
is recovered under the same non-expiring campaign flock by `run` (or explicitly
by `recover`). A recovery invocation never starts a second attempt in that call.

### Restart recovery (#70)

A durable draft/checkpoint precedes implementation and each execution boundary.
Exact container/helper names, image, command and bind intents are persisted before
Docker creation; observed IDs are persisted before start. Docker launch clients
inherit the campaign flock so a surviving create/start/exec client also fences
new controllers. Recovery inspects all named resources, including #66 evaluation
helpers and #69 session/full-check workers. An unavailable daemon is not absence.
A live original (including an idle sleep container) is preserved and pauses with
`original_worker_still_running`; recovery never kills it to make room. Only
identity-matched stopped containers can be removed, without force.

Completed implementation/result checkpoints continue the **same attempt**.
A persisted final response may become a result only after conclusive quiescence,
owned cleanup/ownership restoration and exact source validation. A settled partial
session without a final result is finalized interrupted rather than replaying its
tools/conversation. A request/wire receipt without its response pauses permanently
until evidence is reconciled; elapsed time, worker removal and cleanup receipts do
not establish remote inference completion. No inference or tool request is replayed.
Missing legacy checkpoints, ownership uncertainty and input/source/candidate drift
also pause with fixed safe reasons in private `recovery-alert.json`.

Recovery reruns both evaluations and all full checks in fresh generation-specific
paths, obtains generation-bound clearance and a new independent review. Old review
sessions/usage remain evidence, not gate authorization. An accepted report orphaned
before manifest replacement is archived by hash and **not** installed blindly;
refreshed gates must pass before a replacement report/manifest can promote. Already
published history is never rewritten or duplicated; the previous incumbent remains
authoritative until the atomic manifest publication.

`status` reads durable checkpoint/alert data without creating a lock, inspecting
Docker, or modifying the campaign. `recover` operates only on a pending attempt;
when none exists it returns status without launching anything. After a recovery
pause, an explicit `recover` may re-inspect evidence; it cannot bypass an ambiguous
model call, auth/quota pause or disk pause. This is invocation-driven recovery, not
a watchdog or scheduler, and alerts are local files, not Telegram delivery.

## Operator invocation and fixed-image prerequisites

Use the trusted checkout and a separate, explicit Python environment containing
unmodified official `hermes-agent==0.19.0` plus the repository's test dependencies.
`python -m scripts.lab_experiment --help` exposes `init`, `run`, `recover` and `status`.

The private initialization JSON is flat: `schema: private-manual-lab/v1`, `repo`,
`initial_incumbent`, `image`, the nine pinned benchmark input fields documented by
the evaluator, plus `check_tools` and `check_tools_sha256`. Never derive the
incumbent from HEAD or silently recompute approved benchmark hashes.

The original fixed image lacks jq, which existing release-shell tests require.
Prepare a small real-tool supplement with `freeze_check_tools(target, sources)`:
its explicit sources are `bin/jq`, `lib/libjq.so.1` and `lib/libonig.so.5`. It copies
only those files and records source paths and byte hashes. `init` freezes a
campaign-owned copy. This is a pinned read-only test input, not an image rebuild
or host-directory mount. The full-suite probe checks real jq positive, negative
and malformed-input behavior before testing. A changed bundle blocks execution.

```sh
python -m scripts.lab_experiment init --campaign /private/new-campaign --config /private/approved-config.json
python -m scripts.lab_experiment status --campaign /private/new-campaign
```

For `run`, invoke the explicit private interpreter from an empty environment with
only PATH, HOME, HERMES_HOME, CODEX_HOME, TMPDIR and PYTHONDONTWRITEBYTECODE (and the
optional runtime-injected LC_CTYPE). The exact lab-only values are enforced by
`lab_model_client.check_environment`: HOME is the lab-auth-client directory,
HERMES_HOME is the lab-auth directory, CODEX_HOME is its separate `.codex` path,
TMPDIR is the designated Hermes scratch directory and PYTHONDONTWRITEBYTECODE is
`1`. Do not borrow a production grant or copy a production profile. Then execute
`python -m scripts.lab_experiment run --campaign /private/new-campaign` from the
trusted checkout. The controller prints a safe report projection; detailed
receipts remain private.

Full verification uses two separate read-only trees: the exact candidate Git
export for the original install smoke, and a trusted tests/scripts overlay for
the complete test suite. The install smoke's assertions and default-on official
security scanner are unchanged. Hashes prove that test-only controller source is
not accidentally scanned as plugin payload and actual candidate files are not
stripped. Pytest, Ruff and mypy all execute inside fresh network-disabled
containers. Seven existing optional/platform skips are disclosed, not expanded
to hide failures.

Candidate-dependent stdout/stderr from the sandbox, full-check runner and
evaluator's trusted-test subprocess stream to private files with bounded previews.
A shared combined 64 MiB per-command storage guard stops the exact worker if a
producer exceeds it; this is an output resource guard, not an elapsed-time or
token allowance. Normal full output remains in the raw private stream files;
short log projections are previews, not complete diagnostics.
The native evaluator transport separately drains stderr concurrently into the
private `transport.stderr` file, capped at the same 64 MiB ceiling (exactly the
ceiling is allowed). Monitoring continues while trusted tests run, while native
Frames are quiet and through final EOF. Overflow fails the evaluation, stops only
the inspected attempt worker and records `transport.stderr.json` plus cleanup
receipts; it cannot yield a complete evaluation. Stdout framing, native query
order, scoring and timing are unchanged.

Before each `run`, before attempt records/workspace exports, the controller samples
`statvfs` on the **campaign/lab filesystem**. An experiment-local joined monitor
rechecks every 60 seconds throughout setup, sessions, evaluations, trusted checks
and quiet provider/subprocess waits. No scheduler/daemon is installed. Available
space is unprivileged `f_bavail / (f_blocks - f_bfree + f_bavail)`, the df-consistent
usable-capacity basis excluding reserved blocks; comparisons use exact integers,
not rounded display percentages. Exactly 10% is allowed. Below 10%, or an invalid/
unavailable filesystem sample, fails closed, durably pauses the campaign and emits
a safe structured `lab_disk_paused` alert on stderr and in `disk-alert.json`.
Only registered current-attempt worker IDs with matching names and lab labels may
be stopped; no global Docker listing/kill/prune is used. Pending/history/incumbent
publication is fenced against the monitor, and no later phase, provider request
or promotion is permitted after detection. Cleanup/ownership restoration still
runs. A blocked in-flight provider request may remain blocked and bill usage, but
worker stopping and durable pause/alert do **not** wait for its response; returned
usage (or unknown usage) is retained on normal unwinding.

The manual runner itself has no Telegram delivery or new authentication. The
optional [hourly controller/outbox](autonomous-lab-hourly.md) can forward structured
alerts through an explicitly configured existing messaging channel.
While a provider call is blocked, inspect the files from a separate terminal:

```sh
python -m scripts.lab_experiment status --campaign /private/new-campaign
python -m json.tool /private/new-campaign/disk-alert.json
```

Free space on that filesystem, inspect exact-worker cleanup/status receipts and
reconcile any pending attempt before explicitly resuming. The controller never
auto-unpauses/retries. These alerts are local files/stderr, **not guaranteed remote
notifications**. As with other durable state, an already unwritable filesystem or
host/process failure can prevent persistence; retain the pending attempt and
investigate rather than treating missing receipts as success.

Usage and explicit cleanup completion survive source-validation failures. A
missing or false cleanup receipt retains the pending attempt for investigation.

## Verification and interpretation

Exercise at least one real implementation/review attempt. Controlled synthetic
accepted/rejected/publication tests prove controller behavior, **not** a natural
benchmark improvement. No benchmark win is required to establish a functional
pipeline and none may be fabricated.

The benchmark is AI-authored/AI-reviewed synthetic evidence, not human gold.
Unlisted returned targets are unjudged, not established irrelevant. Before a
natural-promotion claim, materially unknown top-five results require symmetric
source review for incumbent and candidate under the frozen scoring contract;
do not silently change labels, claim exhaustive judgments or expose held-out
per-case results to implementation workers.

Keep private corpus/benchmark files, raw model output, per-case execution receipts,
credentials and identifying telemetry outside this public repository and its
issues. Safe aggregates and source implementation may be published only when
explicitly requested. Lab promotion is not production deployment. Hourly
operation, production updates, gateway restarts and public pushes remain separate
authorization decisions.

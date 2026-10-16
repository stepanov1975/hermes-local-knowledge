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

The review sandbox receives the exact stopped/frozen candidate read-only plus
safe execution evidence. It has independent writable scratch state, not the
implementation session's memory. Review checks benchmark shortcuts, official
compatibility and agreement between the draft report and actual execution.
Neither a worker's claimed test result nor the model's approval substitutes for
controller-owned verification.

## Durable state and outcome

Create the experiment record before launching a session. Keep a hypothesis,
strategy, references to related attempts, changes, reproducible revision,
executed checks, measured results, outcome and lessons for **every** attempt.
A no-change outcome is legitimate. Repeating an apparently unchanged failed
strategy needs a stated new premise or materially different implementation.

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
blocks an unverified duplicate launch; general interrupted-worker recovery is
tracked separately in #70.

## Operator invocation and fixed-image prerequisites

Use the trusted checkout and a separate, explicit Python environment containing
unmodified official `hermes-agent==0.19.0` plus the repository's test dependencies.
`python -m scripts.lab_experiment --help` exposes `init`, `run` and `status`.

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

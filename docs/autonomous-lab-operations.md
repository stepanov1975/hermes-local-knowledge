# Autonomous lab operations

## Implemented boundary: native container pilot

The first lab slice is an **operator-invoked, model-free pilot**, not the complete
campaign controller. It does not schedule jobs, generate questions, score a
campaign, launch an implementation agent, or deploy a plugin to production.

Run all commands from a clean checkout of this repository. Use an explicitly
selected Python interpreter; do not assume virtualenv activation survives tool
invocations. The controller itself uses only the Python standard library and
requires access to the existing Docker engine.

### Build and freeze the runtime

```sh
docker build -f docker/autonomous_lab/Dockerfile -t lk-lab-runtime:pilot docker/autonomous_lab
docker build -f docker/autonomous_lab/network.Dockerfile -t lk-lab-network:pilot docker/autonomous_lab
docker image inspect lk-lab-runtime:pilot lk-lab-network:pilot
```

Supply the resulting **immutable image IDs/digests**, not these mutable tags, to
the controller. Retain those identities and the build evidence for the campaign;
do not rebuild the environment between experiments. Package and OS repositories
can change, so a later rebuild is a newly identified environment, not necessarily
a bit-identical reproduction of the earlier build.

The runtime Dockerfile extends the published official Hermes image
`nousresearch/hermes-agent@sha256:648e4daf033bf2de209435fd9dd61ee3e11103c1f7e1ab469bffbfb99acc263f`.
Its recorded official source revision is
`90194c64ff99933ec3b9ecfd9295b06e6c11e038`. The lab adds a probe and a separate test
tool environment, without altering the official Hermes interpreter or source.
That image is distinct from the exploratory source revision proposed during
planning. Plugin revisions are exported from exact Git commits at launch; the
plugin is not baked into this fixed image.

### Prepare a small reviewed corpus

Create a private directory containing 1–16 real, credential-free Markdown files
and `manifest.json`. This is deliberately a small pilot: do not mistake it for a
full-corpus freeze. Each manifest item has these fields:

```json
{"path": "artifact.md", "sha256": "<actual SHA-256 of the copied file>", "review": "<actual credential/content review conclusion>"}
```

The manifest is an array. Files must be flat, distinct regular Markdown files,
not symlinks, and no larger than 256 KiB. Additional unapproved files are rejected.
The operator must actually review the contents: a nonempty `review` field is an
attestation, not an automated secret detector. Keep corpus contents, returned
results and execution evidence outside this public repository.

### Start a fresh pilot

Replace the values below with real immutable identities and approved private
paths. The attempt directory must **not already exist**, even after a failed run.
The parent directory must exist and be writable by the operator.

```sh
python -m scripts.autonomous_lab \
  --attempt /private/lab/pilot-001 \
  --plugin-repo /path/to/plugin-checkout \
  --plugin-commit '<exact 40-character commit>' \
  --corpus /private/lab/reviewed-pilot-corpus \
  --image 'sha256:<actual immutable runtime image ID>' \
  --network-image 'sha256:<actual immutable network-helper image ID>' \
  --name lk-lab-pilot-001 \
  --query 'a real question supported by the reviewed artifacts' \
  --blocked '<known reachable homelab IPv4>:<service port>'
```

For meaningful negative network evidence, first establish that the specified
homelab service is reachable from the host. The probe opens a TCP connection only;
it does not log in to or mutate that service.

The controller supplies only three fresh writable directories (clean Hermes
state, plugin workspace and output) plus the approved read-only corpus. It does
not copy production configuration, jobs, memory, indexes, credentials or SSH keys.
Docker daemon readback checks the worker's exact mounts, user, capabilities,
read-only root filesystem and resource limits before starting it.

Workers run as UID/GID 10000 with all capabilities dropped, no privilege escalation,
no Docker socket and no host namespace sharing. Rootful Docker access stays with
the trusted host controller. Two fixed-code trusted helpers perform scoped file
ownership setup and network-policy setup; neither executes candidate code. The
network helper has only the required `NET_ADMIN` capability, and its namespace is
shared with the unprivileged worker, not with the host.

The network policy denies IPv6, private/link-local destinations, the host bridge,
Docker's embedded DNS and arbitrary public destinations. It allows only TCP 443
to controller-resolved public IPv4 addresses for `chatgpt.com` and
`auth.openai.com`. Provider names resolve through the helper's fixed hosts file;
there is no worker DNS egress. The allowlist is **IP/port based**, not a general
application-layer URL firewall; shared CDN addresses are not exclusive proof of
hostname confinement. Provider addresses are refreshed only at a fresh launch.
There is no elapsed-time allowance on the worker; infrastructure readiness and
individual network probes have bounded timeouts.

### Read the evidence and prove reset

The private attempt directory retains controller-owned image identities,
Docker inspection, resolved provider addresses and firewall evidence **outside**
all worker mounts. Worker observations are under `output/`:

- `native-search.json`: actual result of one public `knowledge_search`, limit five,
  dispatched through official Hermes plugin discovery and `model_tools`.
- `native-feedback.json`: real native feedback used only to demonstrate state
  isolation in this pilot; do not use this probe as the later campaign evaluator.
- `pilot.json`: plugin/dispatcher provenance, clean initial state, corpus hashes,
  provider TLS/HTTP observations and blocked-network observations.
- `checkpoint.json`: output retained after container removal.

Run a second pilot with a **different new attempt path and name**, using the same
pinned environment and plugin commit. The probe requires that no previous index,
memory, feedback canary, auth store, cron directory or session database existed at
startup. It intentionally leaves memory/feedback/index state in its own attempt
so the second run proves that such state is not inherited.

On normal completion the controller removes its exact labeled worker/helper and
network, reads back their absence, and retains output hashes in `cleanup.json`.
No broad Docker prune is performed. Interrupted ownership/recovery and retention
are later implementation slices; preserve evidence and investigate a failed
cleanup rather than treating absence of a successful response as proof of removal.

## Complete-corpus freeze and offline native baseline

`scripts.lab_corpus.freeze_corpus` captures the complete configured scanner input
set in memory, checks source identity at the capture boundary, then materializes
only reviewed/projected bytes. Supply the resolved source root, scanner home,
state directory, index settings, a new private destination, a safe parser for the
source configuration (for example `yaml.safe_load` from the operator test
environment), and a `capture_audit` callback calling `collector_audit` with the
**pinned deployed plugin**. The callback must not build or open the production
index. Keep the returned capture/review report controller-private; write it with
`write_private_report` outside the corpus directory.

- Source inventory and actual eligibility are separate: the private report binds
  original input hashes, policy/version and projected hashes; the collector audit
  supplies the complete expected artifact inventory and scanner implementation
  identity. File counts are not artifact counts.
- Local structural projections remove credential-bearing values, including Python
  compound expressions and environment lookup defaults. Pure environment-name
  references remain available. Known credential syntax is scrubbed from prose;
  unresolved opaque-value findings or invalid projections block preparation.
  This is a versioned, test-backed policy, not a universal secret detector.
- Runtime data is allowlisted and inert: environment/header values and unrelated
  configuration are discarded. Cron/MCP projections are kept under the read-only
  scanner input tree, never the execution `HERMES_HOME`; no production integration
  or job is activated. Routing labels and commands remain subject to local review.
- Canonical aliases and generated Markdown notes are retained. Credentials are
  not made safe by title-only replacements, a nonempty review string, or dropping
  a difficult artifact. Independently inspect policy and actual projected inputs
  before allowing a new campaign to feed model workers.
- Post-capture live changes do not invalidate an already captured snapshot. Changes
  during capture, unexpected membership, ambiguous mappings and frozen-manifest
  drift are failures. A changed live corpus requires a separately prepared campaign,
  not an implicit refresh of an existing benchmark.

The resulting corpus contains `source/`, `scanner/`, generated-note `seed/`,
`mapping.json` and `manifest.json` (scanner settings, captured expected-artifact
inventory and file identities). Symlinks are reconstructed only to approved canonical
locations. Files/directories are non-writable and every worker mounts the complete
corpus read-only. Original-only source identities and review records stay outside
worker mounts and model inputs.

### Replay the deployed baseline twice

Use the returned `projected_manifest_sha256` to bind the corpus; supply the exact
deployed plugin commit and the **existing** immutable lab runtime image. Choose a
new private attempt directory and a unique name for each replay:

```sh
python -m scripts.lab_corpus_baseline \
  --corpus /private/lab/campaign/corpus \
  --manifest-sha256 '<actual projected manifest SHA-256>' \
  --plugin-repo /path/to/plugin-checkout \
  --plugin-commit '<exact deployed 40-character commit>' \
  --image 'sha256:<existing immutable runtime image ID>' \
  --attempt /private/lab/baseline-001 \
  --name lk-lab-baseline-001
```

Run again with a different new attempt/name and identical immutable inputs. The
trusted runner is copied into a dedicated read-only mount outside candidate code.
The controller verifies all mounts, UID/GID, capability, filesystem and resource
settings by Docker readback before starting; baseline workers use `--network none`
and receive no provider credentials. The optional YAML parser is loaded from the
already pinned lab test environment, without changing official Hermes source or
interpreter contents or rebuilding the fixed image.

Each replay builds a fresh SQLite/JSONL index, checks SQLite integrity and compares
**every** collected artifact's logical identity with the captured inventory. It
performs a real public `knowledge_search` through official plugin discovery and
`model_tools` for each represented artifact type, limit five, with feedback and
automatic generation disabled. These are reproducibility probes, not the later
five-question benchmark or promotion metrics. Fresh home/workspace state and
unchanged generated-note seeds are asserted.

Retain controller inspection/runner/image identities, `output/native-baseline.json`
(including search evidence), `output/coverage.json`, `output/index-identity.json`,
index hashes and `cleanup.json` privately. The controller
removes only its exact labeled worker and reads back absence while keeping the
corpus and evidence. No schedule or production deployment is activated by these
commands; model generation, candidate scoring and hourly recovery are later slices.

The pilot's provider test is **unauthenticated TLS/HTTP reachability**, not a
successful model inference. A provider HTTP rejection can still establish network
reachability. Separate lab-only model authentication and real Luna/Sol execution
must pass before model-backed stages can be considered complete. Never put model
credentials in an image, corpus, public issue, report or chat.

## Evidence-grounded synthetic benchmark preparation (#67)

`python -m scripts.lab_benchmark` is a trusted-controller-only preparation CLI,
not a scoring engine, implementation worker, daemon, scheduler or production
change. It uses the unmodified official `hermes-agent==0.19.0` public auxiliary
client with two explicit roles: **generation** uses `gpt-6-luna` / `openai-codex`
with requested wire reasoning `xhigh`; **judgment** uses `gpt-6.1-sol` /
`openai-codex` with requested wire reasoning `high`. Both use empty tools and the
adapter's stateless Codex Responses contract (`store=false`, streaming, SDK
retries zero). The sender inspects emitted inference metadata and records the
role, exact prompt hash, requested model/effort and accepted wire settings without
credentials or headers. No fallback model/provider is allowed. Successful
calls establish acceptance of the requested adapter/wire contract; the shim does
not preserve server model/reasoning echoes or detailed cached/reasoning usage.
This controller-host access does **not** establish worker network isolation.

Use a candidate-bound Python 3.12 environment with the official Hermes package
and `-e '.[test]'`. Verify the absolute interpreter and imported plugin path.
The sender rejects inherited environment variables outside the documented clean
allowlist. The official runtime credential resolver performs serialized,
controller-owned lab-only refresh; the returned credential mapping is discarded.
A separate shared lab-grant file lock prevents simultaneous benchmark senders.
No production grant, `.env`, SSH key or other profile is read/copied. Auth/quota
errors pause; receipts contain only safe error class/status, never provider bodies.

### Commands and scope

From the reviewed candidate checkout, replace all placeholders with the approved
private paths. Keep the same source modules for the lifetime of this campaign:

```sh
env -i PATH='<candidate-venv>/bin:/usr/bin:/bin' \
 HOME='/home/alex/.local/share/lk-autonomous-lab-execution/lab-auth-client' \
 HERMES_HOME='/home/alex/.local/share/lk-autonomous-lab-execution/lab-auth' \
 CODEX_HOME='/home/alex/.local/share/lk-autonomous-lab-execution/lab-auth-client/.codex' \
 TMPDIR='/home/alex/.hermes/cache/scratch' PYTHONDONTWRITEBYTECODE=1 \
 '<candidate-venv>/bin/python' -m scripts.lab_benchmark prepare \
 --corpus '<approved-frozen-corpus>' --manifest-sha256 '<approved-projected-manifest-SHA256>' \
 --campaign '<new-private-campaign>' --representative
```

`--representative` selects one median-source-size artifact in **each** corpus
family for a bounded pilot. `--artifact <exact-logical-ID>` selects individual
origins and can be repeated. These are incomplete campaigns, never full freezes.
**Omit both selection options for complete generation over every mapped artifact.**
Run `status` with the same corpus/hash/campaign arguments for aggregate progress;
run `freeze` only once every artifact has five accepted cases. Freeze refuses
partial quotas and verifies case IDs, exact evidence and received generation/
judgment receipt bindings before publishing. Repeat commands resume existing work;
there is no automatic corpus refresh or original-source access.

### Evidence, rejection and durable recovery

The corpus manifest, mapping and each source hash are verified. Packets use only
scrubbed projection bytes, frozen locators and logical identities. Short sources
are supplied completely; larger sources receive structural-heading/function/flag
windows plus evenly spaced coverage, including the end. The selection limit is
24,000 characters per source, not a reason to exclude a large artifact. Selection
prioritizes owning skills and explicit quoted skill names, tool references and
file references from the frozen origin text. For operational origins (cron, MCP,
script and tool notes), six additional identity-domain leads follow, ordered by
origin-term overlap and frozen locator. Generic update terms cannot displace a
declared domain skill/report. These are context leads, not relevance judgments.
A `missing_evidence` rejection or fewer drafts than requested expands subsequent
rounds to the remaining frozen domain leads, without a live resnapshot. Per-source
span selection stays fixed. **Relations and origin association never establish
acceptable-target correctness.**
For MCP-server origins, frozen tool notes with an exact matching qualified MCP
namespace are selected before other leads and listed as `origin_capability_sources`.
This establishes registration membership, not target correctness. The server's
registration quote and a matching tool's documented task may jointly establish
origin association; an unrelated namespace is never membership evidence.
All corpus/draft/accepted text is explicitly untrusted data and no tools execute.
Text-poor runtime artifacts select only the specific projected JSON fragment;
character offsets are then into its documented canonical JSON, not the original
registry. Initial `evidence.json` and each `round-*/evidence.json` record exact
supplied spans, offsets/basis, fragment, source and excerpt hashes; each call
receipt stores the exact provider prompt, including accepted cases and cumulative
rejected drafts/reasons. Freeze verifies each case against its own round packet.
Together these disclose precisely what selected evidence/accepted cases/drafts
were sent. No unfiltered corpus blob or original-only capture identity is sent.
This selection is not a guarantee that all useful content was supplied; an
insufficient artifact blocks for operator review rather than forcing labels.

Structural checks require supplied target identities and exact quote substrings.
A separate real Sol/high call semantically judges origin association independently
from proposed-target correctness, realism and distinct user goal against prior
accepted cases and earlier accepted drafts. Imperfect but recognizable wording
is acceptable: these are practical Hit@5 routing queries, not polished answers.
Both positive and negative `origin_association` verdicts must bind the exact
origin logical ID and cite a nonempty literal quote from its actual source.
A supplied neighbor is not the origin. Another artifact may still be the correct
target, and useful neighbor targets cannot rescue an off-origin question.

Every proposed target receives its own cited correctness assessment, and at
least one genuinely useful first read is required. Sol may return constructive
`additional_targets`, each with exact logical identity, span, literal quote and
usefulness rationale. Only structurally valid, source-supported alternatives
are materialized alongside the unchanged generated targets. The original draft
and judgment remain in their raw receipts; case identity/readback binds the
exact combined target list to both. Missing an alternative does not by itself
reject an otherwise usable question; this positive set is not claimed exhaustive.
An unlisted actual result is unjudged, not automatically irrelevant. Before
future promotion claims, material unknown top-five results must be source-reviewed
symmetrically for incumbent and candidate, using the same frozen scoring labels.

Off-origin tasks, incorrect targets, fabricated capabilities, obviously duplicate
goals and incoherent verdicts remain invalid. Missing or wrong-origin citations
and contradictory decisions are malformed validation, not regeneration feedback.
Quote validity is structural, not semantic correctness. Target quotes establish
usefulness as a first read of the whole artifact, not possession of complete answer
text. Distinctness means different supported user goals, not different capabilities;
cosmetic capability-identification paraphrases do not fill the five-question quota.
Rejected drafts retain structured categories (`invalid_schema`, `invalid_quote`,
`duplicate_intent`, `origin_mismatch`, `unsupported_target`, `missing_evidence`)
and reasons; regeneration receives that feedback plus accepted intents on resume.
Labels are **AI-validated synthetic**, not human labels or observed retrieval success. Accepted cases survive rejected regeneration rounds. A bad
judgment schema retries only judgment, retaining the received drafts. Structural
validation is per draft: one invalid quotation does not discard valid siblings.
Only structurally valid drafts reach semantic judgment, while rejected drafts
retain their own categorized feedback. Freeze validates each accepted draft
against its original generation receipt without requiring invalid siblings to pass.

The practical default is at most three **new generation rounds per artifact per
invocation**, and three new malformed-judgment retries for a retained draft batch;
`--rounds` changes this operational policy. This is not a contractual regeneration
cap or spending allowance. Quota failure leaves a private artifact/reason blocker.
The operator may resume more rounds, inspect the supplied evidence, or decide
that a separately identified preparation protocol/campaign is needed. Do not
omit the artifact, manufacture cases, relabel on origin alone or change frozen
sources. There is no independently enforced token allowance; aggregate receipts
report provider usage actually surfaced by received responses.

Requests are checkpointed before dispatch; raw returned drafts and usage are
atomically fsynced before parsing/judging. Completed received calls are replayed
locally, never silently repeated. A crash/transport failure before a durable
response is fundamentally ambiguous. Default resume pauses. After checking that
no controller is alive, `--retry-uncertain` explicitly acknowledges a possible
duplicate provider charge and preserves prior attempts; it is not exactly-once
provider delivery. Failures with no returned usage have unknown provider cost.
The campaign lock prevents overlapping controllers for the same campaign.

### Identity, partition and worker protection

Digest encoding is frozen: SHA-256 of UTF-8 JSON with `sort_keys=true`,
`ensure_ascii=false` and separators `(',', ':')`. Protocol identity binds prompts,
model/settings, selection and partition rules **and the two preparation module
file hashes**. Campaign ID hashes `[protocol_digest, corpus_manifest_sha256]`.
Case ID hashes `[campaign_id, origin_logical_id, question, intent, targets]`.
An independent golden vector is:

```text
bytes: ["campaign","origin","question","intent",[]]
SHA256: ddc83dcabcc747b76df43748796c24fb92fa3f701491789fbd8430057f69574f
```

The complete freeze sorts case IDs per origin, places the first three in
development and the last two in hidden validation, and records the fixed rule
and source/campaign identities before any scoring. `frozen.json` is the atomic
controller authority; `freeze-receipt.json` binds the complete and development
payload hashes/counts. Only `worker-development/development.json` is exported to
a worker. **Mount only that development directory plus the approved read-only
corpus, never the campaign root, `artifacts/`, model receipts, `frozen.json` or
controller reports.** Validation questions, targets, judgments and per-case
failures remain controller-private. CLI stdout is aggregate only.

All generated data belongs outside Git: new directories mode 0700, files 0600.
Preserve the corpus, campaign, accepted work, disclosure and call receipts. Source
module changes invalidate campaign identity instead of silently reinterpreting
accepted labels. Protocol `lk-synthetic-routing-v5` records Luna/xhigh generation
and Sol/high judgment roles, source-bound positive/negative origin verdicts,
verified constructive target additions and pragmatic question quality. Start a
new campaign directory; v1–v4 receipts and labels remain historical and cannot
resume or freeze as v5 labels. Calibration controls are diagnostic AI evidence,
not human gold, blind holdout accuracy, or permission to import old labels. This
trusted controller targets the explicitly selected Alex host; these controller
paths do not change the reusable plugin's runtime portability. A completed pilot demonstrates the pipeline, not the whole
1,742-artifact quota; #67 remains open until complete generation and full 3/2
freeze are actually verified.

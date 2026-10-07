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

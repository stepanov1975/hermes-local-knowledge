# Autonomous Local Knowledge development lab — approved design

Status: approved design, including the user-approved benchmark amendments recorded in [#67](https://github.com/stepanov1975/hermes-local-knowledge/issues/67). Implementation proceeds only under separately authorized tickets; scheduling and production deployment require separate authorization. This document is the canonical design; [the summary](autonomous-development-lab-summary.md) is a shorter reading aid.

The current continuation authorizes ticket #70 source changes and controlled
interruption/recovery proofs only. It does not authorize another real model
trial, resuming the original paused campaign, a public push, scheduling, or any
production change.

## Purpose

Run repeatable candidate improvements against a frozen local artifact corpus, preserving a useful incumbent and experimental evidence without interfering with production.

## Accepted boundaries

The optional private lab controller/process orchestration is Linux-only (`flock`, `/proc`, inherited file descriptors, directory fsync and POSIX permissions); its explicitly listed orchestration tests are excluded before collection on other platforms, while portable lab tests and the core plugin's Windows test lane remain enabled.

See [ADR 0001](adr/0001-autonomous-lab-boundaries.md) and the [domain vocabulary](../CONTEXT.md).

- Start from a deliberately pinned plugin revision. The installed plugin and development checkout differ; do not silently select development HEAD.
- Use Luna as a token-saving default for question generation, not a mandatory dependency. The assistant may directly author source-grounded questions when generation stalls, with explicit authorship rather than invented Luna receipts. Validate all questions against source evidence; the final quota and unresolved-artifact policy are specified below.
- Keep sources, final questions, targets, and evaluator rules fixed within a campaign.
- Permit changes to plugin code, indexing/enrichment logic, and plugin-owned configuration. Permit general candidate-generated enrichment, not benchmark answer tables.
- Separate agent-visible development cases from evaluator-controlled validation cases.
- Report exact originating-artifact Hit@5 as well as evidence-validated target results. Do not automatically equate arbitrary graph neighbors with correct targets.
- Require strictly higher validation Hit@5 over evidence-validated acceptable targets and no Hit@1 decrease on either the development or validation split. Equal scores do not promote; one additional correct validation result can qualify without claiming universal or statistical superiority.
- Require warm public-plugin-search p95 latency at most one second, including any query-time model work but excluding index construction. Measure candidate and incumbent with the same fixed protocol.
- Require tests, installation/integration checks, official-Hermes compatibility, and independent review. The controller owns scoring and the promotion decision.
- Automatically promoted revisions become lab incumbents only. Production stays unchanged without separate approval.
- Use fresh Docker containers per experiment, built from a pinned lab image containing unmodified official Hermes and development/test tools. This replaces the earlier VM requirement. Supply the fixed corpus read-only and copy the current incumbent into a fresh writable workspace; the image need not be rebuilt after each plugin promotion. Give each attempt clean Hermes state, with durable checkpoints/reports outside the disposable container.
- Keep the controller/evaluator outside worker access and evaluate the exact candidate revision in a separate clean container. Do not give workers privileged mode, the Docker socket, production Hermes data, SSH keys, broad host mounts, or production service credentials. Use non-root workers, resource limits without an elapsed-time cutoff, and enforced/tested network restrictions permitting necessary model access while blocking homelab services. Model authentication is supplied separately rather than baked into the image. Containers share the host kernel and are not VM-equivalent security boundaries.
- Each hourly tick starts at most one experiment when idle. Skip ticks while busy, never overlap, never accumulate a backlog, and wait for the next hourly tick after completion rather than recursively starting workers.
- Implementation uses `gpt-6.1-sol` / `openai-codex`, reasoning `high`; the user confirmed this exact mapping from the requested label `sol 6.1`. Independent review uses the same model and reasoning level in a separate session. No time limit: the proposed 45-minute allowance is rejected. No separately enforced monetary/token allowance; record provider-reported usage and pause with an alert on exhausted quota or persistent authentication failure.
- Default to five valid, distinct realistic question intents per artifact, with documented source-appropriate exceptions for genuinely sparse or empty sources. Freeze exceptions before further outcomes; retain every corpus item and accepted case, and never lower quotas after failed attempts. If a frozen quota remains unmet, report it rather than omit the artifact or force labels. Distinctness concerns a materially different practical user outcome, not wording or another source heading; an outcome explicitly requested by an accepted question is already covered.
- Freeze deterministic partitions per origin: order retained cases by SHA-256 of UTF-8(`origin + "\n" + case_id`), breaking ties by `case_id`. For zero cases, allocate zero development cases; otherwise allocate `min(3, max(1, n - 1))` development cases and reserve the remainder for validation. This preserves three/two for five-case origins. Disclose zero-question origins and origins without held-out cases; do not imply per-origin validation where none exists. The coverage-aware format is not automatically compatible with legacy universal-five consumers.
- Question authors and validators receive routing-relevant source evidence rather than only filenames, with credentials removed and runtime configuration reduced to safe scanner inputs. The corpus remains local; selected evidence is sent to the configured authoring and judgment model providers. Snapshot helpers that copy runtime configuration verbatim must not transfer production credentials into the lab or activate copied production jobs.
- Workers see development cases and experiment reports. Validation question text, labels, and per-case failures remain hidden; expose aggregate validation metrics only.
- Score one public plugin search per fixed question, using its actual top five, a candidate-built index over the same frozen sources, and clean feedback state. No conversational rewriting, repeated search attempts, or learning from evaluation searches.
- Check installation/integration in disposable Hermes in addition to standalone evaluation.

## Required experiment handoff

Every agent run must leave a durable experiment report for the next run, including rejected and incomplete attempts, not only wins. Reports and benchmark details are private local experiment data, not public repository material.

The report records:

- Starting incumbent revision and campaign identity.
- Hypothesis and strategy, with references to relevant earlier attempts.
- What was actually changed or tried; patch/revision references when available.
- Executed checks and measured results, distinguished from untested claims.
- Promotion, rejection, failure, or incomplete outcome and the supporting evidence.
- Failure explanation, remaining uncertainties, and suggested next avenues.
- Any reason to revisit a previously failed strategy: new evidence, a changed premise, or a materially different implementation.

The report is submitted by the implementing agent, checked by an independent review session against the patch and execution evidence, and completed by the controller with authoritative scores and the promotion decision. Review checks benchmark-specific shortcuts, compatibility, and whether the report matches what was actually executed. A rejected report/candidate remains useful evidence for the next agent.

Later agents consult the attempt history before choosing a strategy. Avoid repeating the same failed strategy unchanged; do not permanently prohibit an entire approach merely because one implementation failed.

### Interruption recovery

Create the durable experiment record before launching the worker and maintain checkpoints and a draft report outside the disposable container. After interruption, establish whether the original worker is alive before starting anything else. Preserve the attempt and resume at the next eligible hourly tick when feasible; otherwise finalize an interrupted report. Rerun required verification before promotion. Pause and notify when recovery or worker ownership is unclear, without imposing an elapsed-time cutoff.

### Retention

Keep the frozen corpus, benchmark, compact experiment reports, and reproducible candidate commits/patches for the campaign's lifetime. Protect active work and the incumbent. Remove completed disposable containers, workspaces, and derived indexes only after securing their report and reproducible revision. Retain raw execution logs for seven days. If available disk space is insufficient, pause and notify rather than deleting protected benchmark or experiment-history data.

### Notifications

Notify through the user's established Telegram Cron destination for promotions, blockers, and operational failures. Ordinary non-improving experiment reports remain local for subsequent agents; do not send hourly no-gain notifications or attach them to the Installation conversation.

### Campaign lifecycle

Continue the hourly campaign until manually paused, without automatically stopping after a number of non-improving attempts. An agent may report no justified new experiment rather than force a change or repeat a failed strategy unchanged. Never refresh the frozen corpus or benchmark automatically; a new snapshot starts a new campaign and requires a new baseline.

## Selected host

Run the lab on the current Hermes host, with isolated worker containers and resource limits. Read-only inspection found an accessible existing Docker daemon, approximately 64 GiB filesystem headroom and 10 GiB available RAM. These are point-in-time observations, not reserved capacity. Do not use or remediate the nearly-full playground as part of this project.

Use the existing rootful Docker engine, controlled only by the trusted external controller. Workers receive neither the Docker socket nor host privileges; no new rootless daemon, dedicated user, or administrator-assisted installation is part of the selected route. Shared-host/shared-kernel risk is accepted alongside resource limits and enforced mount/network boundaries.

## Approval scope and implementation prerequisites

The original shared-understanding confirmation approved finalizing the design documents only. Subsequent implementation permissions and acceptance amendments are recorded in the separately authorized implementation tickets, including #67's source-appropriate coverage and optional-Luna authoring decisions. Design acceptance or completion of an earlier ticket does not itself authorize another worker, hourly scheduling, or production deployment. Verify image/runtime compatibility, provider access, and resource sizing within the authorized implementation scope.

## Engineering details to resolve against the chosen environment

These are implementation choices to verify rather than requests for the user to supply retrievable facts:

- Pin the actual deployed plugin revision as the initial baseline and record the official Hermes revision used by the lab.
- Freeze all configured scanner families, including dereferenced skill sources, support documents, scripts, safe runtime cron/MCP projections, and existing generated tool notes; do not copy live state or credentials wholesale.
- Record a frozen source manifest and stable logical artifact identities so relocation or a candidate's changed ID representation does not corrupt labels. Verify corpus coverage, mapping uniqueness, and baseline reproduction before running improvements.
- Choose and record a fixed warm-up, timing, percentile, case-order, and validation-partition protocol before any candidate is evaluated.
- Restrict worker edit scope without assuming Git worktrees or Hermes profiles are security boundaries. Treat candidate execution as untrusted even when invoked by the evaluator.
- Keep platform orchestration outside the candidate's editable plugin worktree and preserve the private-data/public-code boundary.

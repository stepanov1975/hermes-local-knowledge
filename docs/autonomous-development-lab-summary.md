# Local Knowledge development lab — design summary

Status: approved design, including the benchmark amendments approved in [#67](https://github.com/stepanov1975/hermes-local-knowledge/issues/67). Implementation requires separate, ticket-scoped approval; this summary is not a deployment-status record.

## 1. Repeatable environment

Run on this Hermes host using its existing Docker engine. Build a pinned lab image containing unmodified official Hermes and development/test tools. Start fresh containers for implementation, evaluation, and independent review.

Keep the runtime and frozen corpus fixed. Supply the latest lab incumbent's plugin revision separately in a fresh writable workspace, so improvements accumulate without rebuilding the image after every win. Start each new experiment with clean Hermes memory, feedback, and index state; preserved checkpoints are available for interruption recovery.

Only the trusted external controller controls Docker. Workers are non-root, unprivileged, resource-limited, and have no Docker socket, production Hermes directory, broad host mounts, SSH keys, or production service credentials. Restrict networking to necessary model access and block homelab services. Supply model authentication separately, never in the image. Containers share the host kernel: this is practical isolation, not VM-equivalent security.

## 2. Frozen synthetic benchmark

Copy and freeze the complete configured artifact corpus, preserving logical artifact identity and coverage. Remove credentials from runtime projections and model inputs; never activate copied production jobs.

Default to five valid, distinct, realistic questions per artifact, with documented source-appropriate exceptions for sparse or empty sources frozen before further outcomes. Retain every source and accepted case; failures do not reduce quotas. Luna saves tokens but is optional: the assistant may author questions directly, with honest provenance and the same source/AI validation. Unmet frozen quotas remain explicit rather than forcing labels or dropping sources.

Freeze partitions per origin by SHA-256 of UTF-8(`origin + "\n" + case_id`), then `case_id`. An empty origin has no development cases; otherwise allocate `min(3, max(1, n - 1))` development cases and reserve the rest for validation. This gives three/two for five-case origins; disclose origins with no questions or no validation cases. Workers see development cases and experiment history, but not validation questions, labels, or per-case failures. Disclose aggregate validation metrics only. Selected source evidence is sent to configured authoring/judgment providers; the corpus stays local. AI labels are not human gold.

Keep source artifacts, questions, targets, partitions, and scoring rules immutable during a campaign. Agents may improve plugin code, general indexing/enrichment methods, and plugin-owned configuration—not benchmark answer tables.

## 3. Agents and hourly operation

Use `gpt-6.1-sol` / `openai-codex`, reasoning `high`, for implementation and an independent review session.

No elapsed-time limit and no separate monetary/token cap. Record provider-reported usage; pause and alert on exhausted quota or persistent authentication failure.

Each hourly tick starts at most one experiment when idle. Skip busy ticks; never overlap or queue a backlog. Start the next experiment at the next eligible tick, from the latest incumbent and previous reports.

Continue until manually paused. Allow a no-change report when no justified new experiment exists. Never refresh the frozen corpus automatically: a new snapshot starts a new campaign with a new baseline.

## 4. Evaluation and promotion

Score one public plugin search per fixed question, using its actual returned top five and a candidate-built index over the same frozen sources. Use clean feedback state; no conversational query rewriting, repeated attempts, or learning from evaluation searches.

Promote only when all conditions pass:

- Validation Hit@5 over evidence-validated acceptable targets strictly increases.
- Hit@1 does not decrease on either development or validation.
- Warm public-search p95 latency is at most one second, including query-time model work but excluding index construction.
- Tests, installation/integration checks on official Hermes, and independent review pass.

Also report exact originating-artifact Hit@5, but do not make it the primary gate. Equal scores do not promote. A one-question gain can qualify; this does not establish universal or statistical superiority.

The external controller owns scoring and the promotion decision. Run the exact candidate revision in a separate clean evaluation container. Review checks shortcuts, compatibility, and whether the report matches execution evidence.

Promotion updates the lab incumbent only. Production deployment requires separate approval.

## 5. Reports, recovery, and retention

Create the experiment record before launching the worker. Every run leaves a durable report, including rejected, failed, interrupted, and no-change attempts. Record its hypothesis, prior related attempts, actual changes, reproducible revision, executed checks, measured results, outcome, and lessons. The controller appends authoritative scores and the promotion decision.

Future agents consult the history and do not repeat a failed strategy unchanged. They may revisit it with new evidence or a materially different implementation.

Preserve checkpoints and draft reports outside disposable containers. After interruption, establish whether the old worker is alive before launching anything. Resume at the next eligible tick when feasible; otherwise finalize an interrupted report. Pause and notify if recovery is unclear. Reverify before promotion; there is no elapsed-time cutoff.

Keep the frozen corpus, benchmark, reports, and reproducible candidate revisions for the campaign's lifetime. Protect active work and the incumbent. Remove completed disposable workspaces/indexes only after securing their evidence. Keep raw logs for seven days. Pause on insufficient disk space rather than delete protected data.

## 6. Notifications and implementation boundary

Notify your established Telegram Cron destination for promotions, blockers, and operational failures. Ordinary no-gain reports stay local for subsequent agents; no hourly notification spam.

Final design confirmation and implementation authorization are separate decisions. Actual implementation and verification status belongs to the separately authorized GitHub tickets; completion of benchmark preparation does not enable an improvement worker, cron job, or production deployment.

## Approval scope

The original confirmation authorized documentation only; later ticket-scoped instructions govern implementation. This summary is a reading aid; the canonical design and vocabulary are maintained in the repository. Do not infer worker launch, cron scheduling, or production deployment authority from design acceptance or another ticket's completion.

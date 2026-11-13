# Local Knowledge

Local Knowledge routes local questions to whole artifacts worth inspecting. The development lab compares candidate improvements against a fixed benchmark without changing production.

## Language

**Artifact**:
A whole local knowledge source that can be selected for inspection, such as a skill, support document, script, runbook, cron job, MCP server, or tool routing note.
_Avoid_: Chunk, answer

**Frozen corpus**:
The fixed collection of artifact sources used throughout one benchmark campaign.
_Avoid_: Live corpus

**Benchmark campaign**:
A sequence of experiments sharing one frozen corpus, benchmark, partition, and scoring contract.
_Avoid_: Individual run

**Synthetic benchmark**:
A fixed collection of AI-generated questions and evidence-validated relevance labels, with AI judgment provenance explicitly identified.
_Avoid_: Human gold, golden database

**Originating artifact**:
The artifact from which a benchmark question was generated; origin alone does not establish that it is a relevant destination.
_Avoid_: Automatically correct artifact

**Acceptable target**:
An artifact established by source evidence as an appropriate destination for a benchmark question.
_Avoid_: Arbitrary neighbor, automatic equivalent

**Development set**:
The benchmark questions and targets available to improvement agents when diagnosing and developing changes.

**Validation set**:
The reserved benchmark questions and targets controlled by the evaluator rather than exposed as development cases.
_Avoid_: Permanently unbiased test set

**Lab image**:
The fixed packaged runtime shared by experiments within a benchmark campaign.
_Avoid_: Incumbent, corpus snapshot

**Candidate**:
A specific proposed plugin revision and configuration submitted for comparison with the incumbent.

**Incumbent**:
The accepted lab candidate that supplies the comparison baseline and starting point for subsequent experiments.
_Avoid_: Production version

**Lab promotion**:
Acceptance of a candidate as the new incumbent without deploying it to production.
_Avoid_: Deployment, release

**Experiment report**:
The durable account of a run's hypothesis, attempted changes, evidence, outcome, and lessons for subsequent agents.
_Avoid_: Success claim, transcript dump

# Tool observation transport

The plugin uses the public `ctx.register_middleware("tool_execution", callback)`
API on hosts that provide it. This path is verified on **unmodified official
Hermes v2026.9.14, commit `345cd2b057a452236de401d3534b8502a7465e8d`**.
It needs no host patches or private host APIs. Five native tools, their responses,
configuration aliases, prompt hint, implicit feedback, OKF and opt-in shadow mode
remain available. Middleware replaces, rather than supplements, `post_tool_call`.
The other three hooks remain registered; the manifest also advertises the legacy
post-tool hook because older hosts still use it.

## Execution and lifecycle

Each registration owns one lazy daemon observer thread and an in-memory queue of
**256 reservations**, including active tools and the currently executing consumer.
Admission is non-blocking. A reservation is taken **before** calling `next_call`.
The downstream callable receives its original argument object exactly once;
its result object or exception propagates unchanged. Projection, queue and
consumer failures never retry the tool. Tool execution remains parallel; only
plugin bookkeeping is serial.

The observer visits reservations in admission order. Pre-LLM bookkeeping and
session-end/finalization notifications use the same queue, so an **accepted**
lifecycle notification cannot overtake a previously admitted tool that is still
running. Finalization hooks enqueue and return; their return value is not a
synchronous worker-launch acknowledgement. No model work runs in the observer;
existing lifecycle consumers may launch the existing detached workers.

This deliberately simple ordering has head-of-line blocking: one slow tool or
consumer delays observation for every session using that registration. It does
not delay other tools. An accepted drain waits for the admission high-water mark
at the time of the drain, including active producers, not for later submissions.
A best-effort process-exit handler closes admission and waits at most one second.
On hosts exposing public `ctx.on_unload` (including official v2026.9.14), plugin
unload also closes admission and drains for at most one second. Pending work is
not cancelled if that deadline expires; the worker exits after it completes.
Independently, an empty queue retires its thread after 30 idle seconds and removes
its bound exit handler, releasing unloaded registrations even without host
teardown. The next admission starts a new worker; retirement and admission share
one lock, and neither active producers nor consumers count as idle. Restart keeps
the registration's counters and dedup window. A permanently hung producer or
consumer can still retain its observer; safe retirement does not abandon work.
Session hooks do not close the process queue. Work entering after a lifecycle
reservation belongs after that boundary; unadmitted/full tools have no fence.

## Evidence and privacy

Each receipt is serialized before enqueue completion, limited to **64 KiB**.
It retains the five host correlation IDs and exclusion flags, copied ContextVars,
and a snapshot of the entire resolved plugin configuration. Worker-time profile
changes do not redirect accepted work. IDs and locators are rejected when too
long, never prefix-truncated into false matches.

Receipts contain only the evidence existing consumers require:

- bounded search query/type and optional lookup fields, and usage receipt IDs;
- exact artifact ID for `knowledge_get`;
- an absolute-path argument plus a content-*presence* marker for `read_file`, or
  the successful canonical `_source_path` for `skill_view`;
- structural schema, its original digest, positional argument shape and normalized
  success/error classification for OKF. Raw schema descriptions/defaults/examples,
  argument values and arbitrary output bodies are not retained for OKF;
- bounded current user-request text only when shadow mode is enabled. No history
  window is copied. Existing shadow privacy/provider disclosures still apply.

These purpose-specific query/path/lookup/request fields are private local evidence,
not anonymized data. They are distinct from arbitrary raw arguments or output.
Receipts disappear after consumption; existing consumer databases keep their own
already-documented projections. Asynchronous bookkeeping can lag the next lookup;
this is not a synchronous feedback-visibility guarantee.

Synchronous argument-shape capture reads at most eight children per container,
with a shared 256-node traversal budget (including repeated references). Tool
results are decoded only within a 65,536-character JSON envelope budget. For
`read_file` and `skill_view`, strings up to 1,048,576 characters can additionally
be scanned for complete, valid JSON `content` string values (at most 4096 string
tokens). Those values are replaced with empty strings before decoding the entire
remaining envelope within the normal budget. Invalid syntax, duplicate keys and
non-JSON constants are rejected; no prefix/suffix success inference is used.
Bodies are never retained in receipts. Large valid file/skill results can therefore
supply exact consumption evidence without decoding their content into a tree.

Malformed, unsupported-shape/type and over-budget results preserve a structural
OKF use with **unknown** outcome: neither success nor error counters increase,
and no successful implicit consumption is inferred. A raised tool exception
remains an execution error. Schema/argument projection or receipt serialization
failures can still discard the observation, never the original tool result.

## Deduplication, overload and failure

Complete profile/root/state + tool + session/task/turn/API-request/tool-call
identity suppresses replay within the **latest 4096 distinct admitted identities**
per registration. A suppressed receipt does not increment OKF counters or invoke
implicit/shadow capture again. The window is bounded FIFO, not a durable ledger.
Missing IDs are counted as `unkeyed` and separately as `missing_<field>`; they
are not deduplicated. Missing session/task/turn/API-request identity additionally
increments `attribution_skipped`: structural OKF capture remains eligible, but
implicit and shadow attribution are skipped. IDs are never invented from ambient
state. Missing only tool-call ID prevents replay suppression, not an otherwise
exact consumer join. Eviction, reload, other processes and restarts can admit a
replay again.

On official v2026.9.14, both execute-code host dispatch seams omit session, turn,
API-request and tool-call IDs while retaining task ID. Their completions therefore
have this explicit attribution degradation. This plugin cannot reconstruct those
missing host identities or count callbacks never supplied by a host.

Complete deduplication identity is recorded before consumer dispatch: partial
failures are not retried automatically, because that could duplicate earlier effects.

Full or closed queues reject new observations, never tool execution. Oversized or
unprojectable receipts are discarded. Lifecycle reservations can also be rejected
if observer thread construction/start fails (`enqueue_error`), without failing
the host hook or leaving an occupied slot; later submissions can retry startup.
Full queues can also reject lifecycle work; a later lifecycle notification or
manual worker invocation may be
needed to wake durable work. Accepted work is never evicted to make room. A hung
producer/consumer can prevent draining; interpreter termination can lose pending
work. There is **no lossless, crash-recovery, durable or exactly-once guarantee**.

The `hermes_local_knowledge.observer` logger emits structural warnings for `full`,
`closed`, `oversize`, `config_error`, `enqueue_error`, `consumer_error`,
`unkeyed`, `missing_<field>`, `attribution_skipped` and `drain_timeout`.
Projection categories distinguish `schema_projection_error`,
`argument_projection_error`, `call_projection_error`, `result_projection_error`
and `receipt_serialization_error`. `result_unknown` is accompanied by one of
`result_budget`, `result_malformed`, `result_type` or `result_shape`. It logs the
first occurrence and powers of two to avoid overload log storms, without raw arguments/results or
exception text. The registration's internal observer `stats()` reports accepted,
completed, delivered, duplicate, discarded, pending and error/rejection counters;
absent counter keys mean zero. `delivered` means the callback returned, not that
all of its independently fail-open database writes succeeded. Existing consumer
logs remain relevant. Counters are process-local diagnostics, not usage-report
rows or a public persistence schema.

## Older hosts

When `register_middleware` is unavailable, registration explicitly warns that it
is using the **legacy inline best-effort hook**. This preserves old functionality
with the existing bounded structural projection, not a reliable delivery queue.
Host single-flight/timeout rules can omit concurrent callbacks, the plugin cannot
count callbacks the host never delivered, and there is no active-producer fence or
new replay window on this path. No core modification is required or recommended.
Use official Hermes v2026.9.14 or newer for the supported middleware transport;
capability detection is not a claim that every older release has been verified.

## Verification

`tests/test_observer.py` exercises parallel execution, original object/exception
semantics, queue bounds and draining, replay, copied contexts and resolved config,
consumer failure, privacy projections, implicit get/file/skill attribution, OKF
argument shape and counters, shadow capture and lifecycle ordering.

`tests/official_observer_smoke.py` is an opt-in offline real-AIAgent test. Supply an
unmodified official checkout, dependency site-packages and an empty private home:

```sh
PYTHONDONTWRITEBYTECODE=1 python -S tests/official_observer_smoke.py \
  /path/to/official-hermes /path/to/dependency-site-packages /path/to/private-home
```

Use a clean environment without credentials. Verify the official Git commit and
clean status before and after. The script blocks network and child-process calls,
uses synthetic provider messages (no inference), exercises direct/deferred calls
and a six-call direct batch, compares all five IDs, and checks imported module
provenance. It also sends large real `read_file`/`skill_view` results through the
registered plugin's real implicit and OKF database consumers, then exercises both
execute-code dispatch seams to assert explicit missing attribution without false
implicit feedback. These seam calls do not launch an execute-code sandbox. Reused
dependency site-packages are not an independently locked clean host install. This does not cover paid provider conversations, connector batches,
remote workers, delegated processes or native Windows execution.

# OKF Compatibility Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Correct all six v0.3 OKF review findings and obtain a clean independent review.

**Architecture:** Keep candidate collection synchronous and privacy-safe, move generation to a true session-finalize hook, and use one structured `ctx.llm` call whose output the plugin validates and writes. Remove the obsolete detached-agent worker path.

**Tech Stack:** Python 3.11+, SQLite, pytest, Hermes `PluginContext`, standard library only at runtime.

## Global Constraints

- Preserve standard-library-only runtime dependencies.
- Preserve `plugin.py` and `indexer.py` compatibility exports.
- Expected bad input must produce bounded JSON-compatible/boolean failure behavior.
- Version metadata must be synchronized at `0.3.1`.
- Do not persist raw arguments, results, transcripts, or schema descriptions/examples.

---

### Task 1: Queue normalization and recovery

**Files:**
- Modify: `hermes_local_knowledge/okf.py`
- Test: `tests/test_okf.py`

**Interfaces:**
- Produces: idempotent `_safe_arg_shape_from_json_text()` and `recover_stale_claims(state_dir, stale_after_seconds, max_attempts, now=None) -> int`.

- [ ] Add tests proving canonical argument shapes do not change across repeated connections and legacy raw argument JSON is sanitized once.
- [ ] Run the tests and confirm the canonical-shape test fails by showing nested shape metadata.
- [ ] Implement canonical-shape detection/normalization without weakening legacy redaction.
- [ ] Add stale-claim recovery tests for retryable and exhausted rows.
- [ ] Run them and confirm recovery is absent.
- [ ] Implement one transactional stale-claim recovery update and rerun `tests/test_okf.py`.

### Task 2: Hermes hook outcome compatibility

**Files:**
- Modify: `hermes_local_knowledge/hooks.py`
- Test: `tests/test_hooks.py`

**Interfaces:**
- Produces: `_classify_hook_outcome(kwargs) -> tuple[bool, str | None, str | None]` with result parsing only as a legacy fallback.

- [ ] Add tests for `ok`, `timeout`, `blocked`, and legacy result-only payloads.
- [ ] Run them and confirm plain-text timeout is misclassified.
- [ ] Implement status-first classification and rerun the focused tests.

### Task 3: Structured session-finalize generation

**Files:**
- Modify: `hermes_local_knowledge/hooks.py`
- Modify: `hermes_local_knowledge/plugin.py`
- Modify: `plugin.yaml`
- Modify: `hermes_local_knowledge/cli.py`
- Delete: `hermes_local_knowledge/okf_worker.py`
- Test: `tests/test_hooks.py`
- Test: `tests/test_cli_okf.py`
- Delete: `tests/test_okf_worker.py`

**Interfaces:**
- Consumes: `recover_stale_claims` and existing queue claim/validate/complete APIs.
- Produces: a hook registered as `on_session_finalize` that calls `ctx.llm.complete_structured` once for a bounded batch.

- [ ] Replace subprocess-oriented hook tests with failing tests for finalize registration, one structured LLM call, bounded claims, validated writes, exception release, and stale recovery.
- [ ] Confirm the new tests fail against the subprocess implementation.
- [ ] Implement structured batch generation, deterministic Markdown rendering, validation, atomic writes, and claim completion/failure.
- [ ] Remove the subprocess worker implementation and obsolete CLI/config surface.
- [ ] Rerun hook, CLI, and queue tests.

### Task 4: Frontmatter summary correctness

**Files:**
- Modify: `hermes_local_knowledge/text_utils.py`
- Test: `tests/test_indexer.py`

**Interfaces:**
- Produces: `first_heading_or_paragraph(text)` that skips the complete opening frontmatter block.

- [ ] Add a test asserting a generated OKF summary comes from its heading/body, not `artifact_type`.
- [ ] Run it and confirm the current frontmatter parser fails.
- [ ] Fix delimiter handling and rerun focused indexer tests.

### Task 5: Documentation, versioning, and verification

**Files:**
- Modify: `README.md`
- Modify: `plugin.yaml`
- Modify: `pyproject.toml`
- Modify: `hermes_local_knowledge/__init__.py`
- Modify tests/config examples that reference removed worker settings.

- [ ] Update documentation for `on_session_finalize`, `ctx.llm`, and the retained manual queue workflow.
- [ ] Bump all version locations to `0.3.1`.
- [ ] Run the complete test/static/package verification bundle.
- [ ] Run a current Hermes upstream registration smoke.
- [ ] Dispatch an independent read-only reviewer against `origin/main..WORKTREE`.
- [ ] Fix every actionable reviewer finding with regression-first tests and repeat review until clean.
- [ ] Independently rerun final verification and confirm a clean working tree except intended changes.

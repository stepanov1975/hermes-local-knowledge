# OKF Compatibility Fixes Design

## Goal

Make the v0.3 tool-OKF feature conform to current Hermes plugin lifecycle and LLM APIs, preserve privacy-safe queue data, recover cleanly from interrupted generation, and index useful summaries.

## Approved scope

The implementation covers every issue from the v0.3 compatibility review:

1. Make stored argument-shape normalization idempotent.
2. Recover stale claimed candidates and enforce the attempt limit.
3. Prefer Hermes `post_tool_call` outcome fields over result-body inference.
4. Generate at `on_session_finalize`, not turn-scoped `on_session_end`.
5. Parse Markdown frontmatter without using metadata as the summary.
6. Replace the detached `hermes chat` subprocess with `ctx.llm.complete_structured`.

## Architecture

The post-tool hook remains a cheap observer that writes only bounded structural metadata. At session finalization, the plugin recovers stale claims, claims at most `max_candidates_per_session` rows, and makes one host-owned structured LLM call through the current plugin context. The model returns bounded fields rather than arbitrary files; the plugin renders Markdown, validates it, writes it atomically, and completes or releases each claim.

The manual `okf status/claim/validate/complete/fail` commands remain available. The automated subprocess protocol, worker module, worker-only environment guard, toolset/source configuration, and `drain-prompt` command are removed because they become dead surface.

## Error handling and privacy

- A structured-call exception marks every claimed row failed through the existing bounded retry policy.
- Stale claims are returned to `pending`, or moved to `error` when attempts are exhausted, before the finalization gate checks for work.
- The LLM receives only the existing candidate packet: sanitized schema, sanitized argument shape, counters, tool identity, and claim metadata.
- The plugin validates model output against the claimed tool and schema hash, renders fixed frontmatter itself, and never gives the model terminal or file tools.

## Compatibility

- `plugin.py` and `indexer.py` remain compatibility entry points.
- `_on_session_end` remains importable as a deprecated compatibility alias but is no longer registered with Hermes.
- The manifest declares the hooks actually registered: `post_tool_call` and `on_session_finalize`.
- Version metadata is bumped together to `0.3.1`.

## Verification

Each defect receives a regression test that is observed failing before implementation. Final gates are pytest, Ruff, mypy, version policy, `git diff --check`, package build/twine validation, current-upstream `PluginContext` registration smoke, and an independent read-only reviewer with no actionable findings.

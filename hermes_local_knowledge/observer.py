"""Bounded, process-local bookkeeping; never a durable delivery queue.

Reservations are ordered at middleware entry, before downstream execution. A
lifecycle reservation therefore cannot overtake an admitted in-flight producer.
Only the observer waits for a slow tool; downstream tool execution stays parallel.
"""
from __future__ import annotations

import atexit
import json
import logging
import re
import threading
import time
from collections import Counter, OrderedDict, deque
from collections.abc import Callable, Mapping
from contextvars import Context, copy_context
from dataclasses import dataclass
from typing import Any

from . import okf, shadow
from .config import _OBSERVER_CONFIG, resolve_config

logger = logging.getLogger(__name__)
IDENTITY = ("session_id", "task_id", "turn_id", "api_request_id", "tool_call_id")
MAX_RECEIPT_BYTES = 65536
MAX_RESULT_CHARS = 65536
MAX_CONTENT_SCAN_CHARS = 1048576
# Lex only complete JSON strings, with bounded input and no per-character
# backtracking stack. The JSON decoder still validates the entire envelope.
_JSON_STRING = re.compile(r'"(?:[^"\\\x00-\x1f]++|\\(?:["\\/bfnrt]|u[0-9a-fA-F]{4}))*+"')
IDLE_SECONDS = 30.0


def _text(value: Any, limit: int) -> str:
    # Reject rather than truncate correlation identities and source locators.
    return value if isinstance(value, str) and len(value) <= limit else ""


def context_fields(source: Mapping[str, Any]) -> dict[str, Any]:
    return {**{key: _text(source.get(key), 128) for key in IDENTITY},
            "parent_session_id": bool(source.get("parent_session_id")),
            "worker_generated": bool(source.get("worker_generated"))}


def _schema_snapshot(schema: Any) -> Any:
    """Admit only a bounded JSON tree before either projection or raw hashing.

    Count shared children on every occurrence, not once per object identity:
    both the projector and JSON encoder expand them. Copying also prevents a
    later registry mutation from invalidating the admission bounds. Reject the
    receipt rather than hashing a truncated schema as if it were complete.
    """
    nodes_left = 4096
    chars_left = MAX_RECEIPT_BYTES

    def copy(value: Any, depth: int) -> Any:
        nonlocal nodes_left, chars_left
        nodes_left -= 1
        if nodes_left < 0 or depth > 32:
            raise ValueError("schema exceeds observer budget")
        if type(value) is str:
            chars_left -= len(value)
            if chars_left < 0:
                raise ValueError("schema exceeds observer budget")
            return value
        if type(value) is dict:
            result = {}
            for key, child in value.items():
                if type(key) is not str:
                    raise ValueError("schema is not a JSON tree")
                result[copy(key, depth + 1)] = copy(child, depth + 1)
            return result
        if type(value) is list:
            return [copy(child, depth + 1) for child in value]
        if value is None or type(value) in (bool, float):
            return value
        if type(value) is int and value.bit_length() <= 256:
            return value
        # Do not invoke arbitrary iterators or schema_hash's default=str.
        raise ValueError("schema is not a bounded JSON tree")

    return copy(schema, 0)


class _CallProjectionError(ValueError):
    """Fixed category only; never retain schema/argument exception text."""


def project_call(args: Any, metadata: Mapping[str, Any]) -> dict[str, Any]:
    """Copy only purpose-specific evidence, not arbitrary tool args/output."""
    name = _text(metadata.get("tool_name"), 240)
    try:
        toolset, schema = okf._tool_metadata(name)
        schema = _schema_snapshot(schema)
        capture = {"toolset": _text(toolset, 240) or None,
                   "schema": okf.project_routing_schema(schema),
                   "schema_hash": okf.schema_hash(schema)}
    except Exception:
        raise _CallProjectionError("schema_projection_error") from None
    source = args if isinstance(args, dict) else {}
    try:
        capture["arg_shape"] = okf.safe_arg_shape(source)
    except Exception:
        raise _CallProjectionError("argument_projection_error") from None
    selected: dict[str, Any] = {}
    if name == "knowledge_search":
        selected = {key: _text(source.get(key), limit) for key, limit in
                    (("query", 4000), ("artifact_type", 128))}
        lookup = source.get("lookup")
        if lookup is not None:
            try:
                selected["lookup"] = shadow._lookup_context(lookup)["lookup"]["fields"]
            except ValueError:
                # Preserve rejection without retaining invalid values or turning
                # supplied invalid context into an absent (valid) lookup.
                selected["lookup"] = False
    elif name == "knowledge_get":
        selected["artifact_id"] = _text(source.get("artifact_id"), 4096)
    elif name == "read_file":
        selected["path"] = _text(source.get("path"), 4096)
    return {**context_fields(metadata), "tool_name": name, "args": selected,
            "_okf_capture": capture}


def _result_envelope(result: str, name: str) -> str:
    """Elide only validated content strings; never parse an unbounded JSON tree.

    This is not a prefix/suffix success heuristic: all other bytes must fit the
    normal decoder budget, and the compacted envelope must parse in full.
    """
    if len(result) <= MAX_RESULT_CHARS:
        return result
    if name not in {"read_file", "skill_view"} or len(result) > MAX_CONTENT_SCAN_CHARS:
        raise ValueError("result_budget")
    parts: list[str] = []
    end = 0
    size = 0
    previous = None
    cursor = 0
    count = 0
    # Advance only past complete tokens. Never retry a failed string at an
    # escaped quote inside it: overlapping suffix searches are quadratic.
    while (start := result.find('"', cursor)) != -1:
        if count >= 4096:
            raise ValueError("result_budget")
        match = _JSON_STRING.match(result, start)
        if match is None:
            raise ValueError("invalid result string")
        cursor = match.end()
        count += 1
        if (previous is not None and previous.end() - previous.start() == 9
                and previous.group() == '"content"'
                and result[previous.end():match.start()].strip() == ":"):
            size += match.start() - end + 2
            if size > MAX_RESULT_CHARS:
                raise ValueError("result_budget")
            parts.extend((result[end:match.start()], '""'))
            end = match.end()
        previous = match
    if size + len(result) - end > MAX_RESULT_CHARS:
        raise ValueError("result_budget")
    parts.append(result[end:])
    return "".join(parts)


def _result_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("duplicate result key")
    return result


def _invalid_constant(value: str) -> Any:
    raise ValueError("non-JSON constant")


def project_result(payload: dict[str, Any], result: Any, failed: bool) -> None:
    parsed = None
    reason = "result_type"
    if isinstance(result, str):
        try:
            envelope = _result_envelope(result, payload["tool_name"])
        except ValueError:
            reason = "result_budget"
        else:
            try:
                parsed = json.loads(envelope, object_pairs_hook=_result_object,
                                    parse_constant=_invalid_constant)
                reason = "result_shape"
            except (ValueError, RecursionError):
                reason = "result_malformed"
    success: bool | None = None
    error_type = None
    if failed:
        success, error_type = False, "execution_error"
    elif isinstance(parsed, dict):
        success = not (parsed.get("success") is False or bool(parsed.get("error")))
        error_type = None if success else "tool_error"
    payload["status"] = "unknown" if success is None else "success" if success else "error"
    payload["error_type"] = error_type
    if success is None:
        payload["_result_diagnostic"] = reason
    selected: dict[str, Any] = {"success": success}
    if success is True and isinstance(parsed, dict):
        receipt = parsed.get("usage_event_id")
        if isinstance(receipt, int) and not isinstance(receipt, bool) and receipt > 0:
            selected["usage_event_id"] = receipt
        if payload["tool_name"] == "skill_view" and parsed.get("success") is True:
            selected["_source_path"] = _text(parsed.get("_source_path"), 4096)
        if payload["tool_name"] == "read_file" and isinstance(parsed.get("content"), str):
            selected["content"] = ""
    payload["result"] = json.dumps(selected)


@dataclass
class _Slot:
    context: Context
    kind: str
    ready: bool = False
    payload: str | None = None


class Observer:
    """One serial consumer per registration, with bounded admission and dedup.

    Capacity includes active producers and the currently executing consumer.
    Replays with complete host identity are suppressed within a bounded in-memory
    window. Failed consumers are not retried, since they may have partially written.
    """

    def __init__(self, consume: Callable[..., Any], *, capacity: int = 256,
                 dedup_capacity: int = 4096) -> None:
        if capacity < 1 or dedup_capacity < 1:
            raise ValueError("observer limits must be positive")
        self.consume = consume
        self.capacity = capacity
        self.dedup_capacity = dedup_capacity
        self._condition = threading.Condition()
        self._queue: deque[_Slot] = deque()
        self._seen: OrderedDict[tuple[str, ...], None] = OrderedDict()
        self._counts: Counter[str] = Counter()
        self._closed = False
        self._thread: threading.Thread | None = None

    def stats(self) -> dict[str, int]:
        with self._condition:
            return {**self._counts, "pending": len(self._queue)}

    def _notice(self, reason: str) -> None:
        with self._condition:
            self._counts[reason] += 1
            count = self._counts[reason]
        # Log first and powers of two: overload must not create unbounded logs.
        if count & (count - 1) == 0:
            logger.warning("Local knowledge observer %s (count=%d)", reason, count)

    def reserve(self, kind: str) -> _Slot | None:
        with self._condition:
            if self._closed or len(self._queue) >= self.capacity:
                self._notice("closed" if self._closed else "full")
                return None
            context = copy_context()
            slot = _Slot(context, kind)
            self._queue.append(slot)
            if self._thread is None:
                try:
                    thread = threading.Thread(target=self._run, name="local-knowledge-observer", daemon=True)
                    thread.start()
                except Exception:
                    self._queue.pop()
                    self._notice("enqueue_error")
                    return None
                self._thread = thread
                atexit.register(self.close, 1.0)
            self._counts["accepted"] += 1
            self._condition.notify_all()
        try:
            # Freeze all custom settings, not just HERMES_HOME; no worker-time
            # config reload or process-global environment mutation.
            context.run(_OBSERVER_CONFIG.set, resolve_config())
        except Exception:
            self.finish(slot, None)
            self._notice("config_error")
            return None
        return slot

    def finish(self, slot: _Slot, payload: dict[str, Any] | None) -> None:
        serialized = None
        try:
            if payload is not None:
                serialized = json.dumps(payload, ensure_ascii=True)
                if len(serialized) > MAX_RECEIPT_BYTES:
                    serialized = None
                    self._notice("oversize")
        except Exception:
            self._notice("receipt_serialization_error")
        finally:
            with self._condition:
                slot.payload = serialized
                slot.ready = True
                self._condition.notify_all()

    def middleware(self, args: Any, next_call: Callable[..., Any], **metadata: Any) -> Any:
        slot = None
        payload = None
        try:
            slot = self.reserve("post")
            if slot is not None:
                payload = project_call(args, metadata)
        except _CallProjectionError as error:
            self._notice(str(error))
        except Exception:
            self._notice("call_projection_error")
        result: Any = None
        failed = True
        try:
            result = next_call(args)  # single use, original object, no retries
            failed = False
            return result
        finally:
            if slot is not None:
                try:
                    if payload is not None:
                        project_result(payload, result, failed)
                        if payload["status"] == "unknown":
                            self._notice("result_unknown")
                            self._notice(payload.pop("_result_diagnostic"))
                except Exception:
                    payload = None
                    self._notice("result_projection_error")
                try:
                    self.finish(slot, payload)
                except Exception:
                    self._notice("enqueue_error")

    def submit(self, kind: str, payload: dict[str, Any]) -> None:
        slot = self.reserve(kind)
        if slot is not None:
            self.finish(slot, payload)

    def _deliver(self, slot: _Slot) -> None:
        if slot.payload is None:
            with self._condition:
                self._counts["discarded"] += 1
            return
        payload = json.loads(slot.payload)
        if slot.kind == "post":
            cfg = resolve_config()
            ids = tuple(payload[key] for key in IDENTITY)
            if all(ids):
                key = (str(cfg.hermes_home), str(cfg.source_root), str(cfg.state_dir),
                       payload["tool_name"], *ids)
                if key in self._seen:
                    with self._condition:
                        self._counts["duplicate"] += 1
                    return
                self._seen[key] = None
                if len(self._seen) > self.dedup_capacity:
                    self._seen.popitem(last=False)
            else:
                self._notice("unkeyed")
                for field, value in zip(IDENTITY, ids):
                    if not value:
                        self._notice("missing_" + field)
            # Tool-call ID is needed for replay suppression, not the downstream
            # exact session/task/turn/request joins. Never infer those joins.
            payload["_observer_attributed"] = all(ids[:4])
            if not payload["_observer_attributed"]:
                self._notice("attribution_skipped")
        self.consume(slot.kind, **payload)
        with self._condition:
            self._counts["delivered"] += 1

    def _run(self) -> None:
        while True:
            with self._condition:
                if not self._queue:
                    self._condition.wait_for(lambda: self._queue or self._closed,
                                             timeout=IDLE_SECONDS)
                    if not self._queue:
                        # Admission and retirement share this lock: a racing
                        # reservation either wakes us or starts a new worker.
                        atexit.unregister(self.close)
                        self._thread = None
                        return
                # A reserved producer (or executing consumer) is not idle.
                self._condition.wait_for(lambda: self._queue[0].ready)
                slot = self._queue[0]
            try:
                slot.context.run(self._deliver, slot)
            except Exception:
                # Never log private exception text or retry partial effects.
                self._notice("consumer_error")
            finally:
                with self._condition:
                    self._queue.popleft()
                    self._counts["completed"] += 1
                    self._condition.notify_all()

    def drain(self, timeout: float = 2.0) -> bool:
        """Wait for work admitted before this call, including active producers."""
        with self._condition:
            target = self._counts["accepted"]
            done = self._condition.wait_for(lambda: self._counts["completed"] >= target,
                                            timeout=max(0, timeout))
        if not done:
            self._notice("drain_timeout")
        return done

    def close(self, timeout: float = 2.0) -> bool:
        deadline = time.monotonic() + timeout
        with self._condition:
            self._closed = True
            thread = self._thread
            self._condition.notify_all()
        done = self.drain(timeout)
        if thread is not None:
            thread.join(max(0, deadline - time.monotonic()))
        return done

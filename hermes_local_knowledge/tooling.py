"""Hermes tool-result compatibility helpers."""
from __future__ import annotations

from collections.abc import Callable
from importlib import import_module
import json
from typing import cast


ToolResultFunc = Callable[..., str]


def _fallback_tool_error(message: object, **extra: object) -> str:
    payload: dict[str, object] = {"error": str(message)}
    payload.update(extra)
    return json.dumps(payload, ensure_ascii=False)


def _fallback_tool_result(data: object | None = None, **kwargs: object) -> str:
    return json.dumps(data if data is not None else kwargs, ensure_ascii=False)


tool_error: ToolResultFunc = _fallback_tool_error
tool_result: ToolResultFunc = _fallback_tool_result

try:  # Hermes runtime path
    _registry = import_module("tools.registry")
except Exception:  # pragma: no cover - lets direct tests run outside Hermes
    pass
else:  # pragma: no cover - covered only inside Hermes runtime
    tool_error = cast(ToolResultFunc, getattr(_registry, "tool_error"))
    tool_result = cast(ToolResultFunc, getattr(_registry, "tool_result"))

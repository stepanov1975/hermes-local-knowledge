"""Hermes tool-result compatibility helpers."""
from __future__ import annotations

import json


def tool_error(message, **extra) -> str:
    payload = {"error": str(message)}
    payload.update(extra)
    return json.dumps(payload, ensure_ascii=False)


def tool_result(data=None, **kwargs) -> str:
    return json.dumps(data if data is not None else kwargs, ensure_ascii=False)


try:  # Hermes runtime path
    from tools.registry import tool_error as tool_error, tool_result as tool_result  # type: ignore[no-redef]
except Exception:  # pragma: no cover - lets direct tests run outside Hermes
    pass

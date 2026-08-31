"""Shared narrow parser for the frontmatter subset used by local knowledge."""

from __future__ import annotations

import json
import re
from typing import Any


def _parse_frontmatter_scalar(value: str) -> str:
    clean = value.strip()
    if len(clean) >= 2 and clean.startswith('"') and clean.endswith('"'):
        try:
            parsed = json.loads(clean)
        except (TypeError, ValueError):
            pass
        else:
            if isinstance(parsed, str):
                return parsed
    if len(clean) >= 2 and clean.startswith("'") and clean.endswith("'"):
        return clean[1:-1].replace("''", "'")
    return clean


def _parse_bracket_list(value: str) -> list[str]:
    clean = value.strip()
    if clean.startswith("[") and clean.endswith("]"):
        try:
            parsed = json.loads(clean)
        except (TypeError, ValueError):
            clean = clean[1:-1]
        else:
            if isinstance(parsed, list) and all(isinstance(item, str) for item in parsed):
                return [item.strip() for item in parsed if item.strip()]
            return []
    return [_parse_frontmatter_scalar(item) for item in clean.split(",") if item.strip()]


def _parse_frontmatter(text: str) -> dict[str, Any]:
    if not text.startswith("---"):
        return {}
    frontmatter: dict[str, Any] = {}
    current_key: str | None = None
    for line in text.splitlines()[1:]:
        stripped = line.strip()
        indent = len(line) - len(line.lstrip())
        if stripped == "---":
            break
        if not stripped or stripped.startswith("#"):
            continue
        list_item = re.match(r"^[-*]\s+(.+)$", stripped)
        if list_item and current_key:
            current_value = frontmatter.get(current_key)
            if isinstance(current_value, list):
                current_value.append(_parse_frontmatter_scalar(list_item.group(1)))
            continue
        match = re.match(r"^([A-Za-z0-9_.-]+):\s*(.*)$", stripped)
        if not match:
            continue
        key, value = match.groups()
        if indent and current_key:
            current_value = frontmatter.get(current_key)
            if current_value == []:
                current_value = {}
                frontmatter[current_key] = current_value
            if isinstance(current_value, dict):
                current_value[key] = _parse_frontmatter_scalar(value)
                continue
        current_key = key
        value = value.strip()
        if not value:
            frontmatter[key] = []
        elif value.startswith("[") and value.endswith("]"):
            frontmatter[key] = _parse_bracket_list(value)
        else:
            frontmatter[key] = _parse_frontmatter_scalar(value)
    return frontmatter

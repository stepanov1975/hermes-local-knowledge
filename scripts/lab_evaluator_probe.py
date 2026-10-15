"""Read-only native transport, mounted alone with the existing #66 adapter.

No expected targets, split identities, scoring code or benchmark files enter
this process. A request is just a query plus a monotonically increasing number.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import sys
from typing import Any

import baseline  # type: ignore[import-not-found]


def emit(value: dict) -> None:
    print("LK68 " + json.dumps(value, separators=(",", ":")), flush=True)


def searches(dispatch: Any, mapping: dict) -> None:
    emit({"ready": True, "mapping_count": len(mapping)})
    for number, line in enumerate(sys.stdin):
        request = json.loads(line)
        assert set(request) == {"sequence", "query"} and request["sequence"] == number
        assert isinstance(request["query"], str) and request["query"].strip()
        response = dispatch("knowledge_search", {"query": request["query"], "limit": 5, "rebuild": False},
                            task_id="lk-lab-evaluation", tool_call_id="evaluation-" + str(number))
        emit({"sequence": number, "response": json.loads(response)})
    state = Path(os.environ["HERMES_HOME"]) / "knowledge"
    with sqlite3.connect(f"file:{state / 'usage.sqlite'}?mode=ro", uri=True) as connection:
        for table in ("feedback", "implicit_feedback"):
            assert connection.execute("SELECT count(*) FROM " + table).fetchone() == (0,)
    assert not (state / "okf_queue.sqlite").exists()


if __name__ == "__main__":
    baseline.native(searches)

from __future__ import annotations

from pathlib import Path

from tools.plugin_guard import format_scan_report, scan_plugin  # type: ignore[import-not-found]


REPO_ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    result = scan_plugin(REPO_ROOT, source="release-candidate")
    print(format_scan_report(result))
    if result.verdict != "safe":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
"""Trusted scanner preparation; review gates are not automated secret certification.

Original hashes and review records belong outside worker/model access. This
module never opens an index, constructs a service, or copies a runtime home.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
import ast
import io
import math
import re
import tokenize
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from hermes_local_knowledge.artifacts import (
    _mcp_secret_name, _sanitize_mcp_arg_value, _sanitize_mcp_url,
)
from scripts.compare_historical_query_versions import scanner_tree_inventory

Review = Callable[[str, str], str]
CRON_STRINGS = frozenset({"id", "name", "prompt", "schedule_display", "script",
                          "state", "last_status", "updated_at", "created_at"})
CRON_LISTS = frozenset({"skills", "enabled_toolsets"})


def _unreviewed(_field: str, _value: str) -> str:
    raise ValueError("Scanner content requires explicit credential/content review")


def project_runtime(config: Mapping[str, Any], registry: Any,
                    review: Review = _unreviewed) -> tuple[dict[str, Any], dict[str, Any]]:
    """Allowlist consumed fields; discard env values and all runtime activation.

    The callback must actually review ALL retained strings, including commands,
    positional arguments and prose. Existing MCP sanitizers are defense in
    depth, not authority to approve unknown values. Never echo a rejected value.
    The result belongs in a separate inert scanner home, NOT HERMES_HOME.
    """
    def text(field: str, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("Unsupported scanner string shape")
        result = review(field, value)
        if not isinstance(result, str):
            raise ValueError("Review must return a string")
        return result

    projected: dict[str, Any] = {}
    legacy = config.get("mcp", {})
    if not isinstance(legacy, dict):
        raise ValueError("Unsupported MCP namespace")
    for namespace, servers in (("mcp.servers", legacy.get("servers", {})),
                               ("mcp_servers", config.get("mcp_servers", {}))):
        if not isinstance(servers, dict):
            raise ValueError("Unsupported MCP server shape")
        if not servers:
            continue
        output = {}
        for name, data in servers.items():
            if not isinstance(name, str) or not isinstance(data, dict):
                raise ValueError("Unsupported MCP server entry")
            checked_name = text(namespace + ".name", name)
            if checked_name != name:
                raise ValueError("Review cannot change scanner identity")
            prefix = namespace + "." + name
            item: dict[str, Any] = {}
            for key in ("command", "url", "base_url"):
                if key in data and data[key] is not None:
                    value = data[key]
                    if not isinstance(value, str):
                        raise ValueError("Unsupported MCP command/URL shape")
                    item[key] = text(prefix + "." + key,
                                     _sanitize_mcp_url(value) if key != "command" else value)
            if data.get("args") is not None:
                args = data["args"]
                if not isinstance(args, list) or not all(isinstance(v, str) for v in args):
                    raise ValueError("Unsupported MCP args shape")
                safe_args = []
                redact_next = False
                for index, arg in enumerate(args):
                    safe_args.append("<redacted>" if redact_next else
                                     text(prefix + f".args.{index}", _sanitize_mcp_arg_value(arg)))
                    redact_next = arg.startswith("-") and "=" not in arg and _mcp_secret_name(arg)
                item["args"] = safe_args
            if data.get("env") is not None:
                env = data["env"]
                if not isinstance(env, dict) or not all(isinstance(k, str) for k in env):
                    raise ValueError("Unsupported MCP env shape")
                # Values are never traversed, coerced, passed to review or copied.
                names = [text(prefix + ".env-name", key) for key in env]
                if names != list(env):
                    raise ValueError("Review cannot change environment names")
                item["env"] = {key: "" for key in names}
            output[name] = item
        if namespace == "mcp.servers":
            projected["mcp"] = {"servers": output}
        else:
            projected[namespace] = output

    jobs = registry.get("jobs", []) if isinstance(registry, dict) else registry
    if not isinstance(jobs, list):
        raise ValueError("Unsupported cron registry shape")
    projected_jobs = []
    for index, job in enumerate(jobs):
        if not isinstance(job, dict):
            raise ValueError("Unsupported cron row")
        row: dict[str, Any] = {"enabled": False}
        for key in CRON_STRINGS | CRON_LISTS | {"schedule"}:
            if key not in job or job[key] is None:
                continue
            value = job[key]
            field = f"cron.{index}.{key}"
            if key in {"created_at", "updated_at"} and isinstance(value, (int, float)) and not isinstance(value, bool):
                if text(field, str(value)) != str(value):
                    raise ValueError("Review cannot change timestamp semantics")
                row[key] = value
            elif key in CRON_LISTS:
                if isinstance(value, str):
                    row[key] = text(field, value)
                elif isinstance(value, list):
                    row[key] = [text(field + f".{i}", v) for i, v in enumerate(value)]
                else:
                    raise ValueError("Unsupported cron list shape")
            elif key == "schedule" and isinstance(value, dict):
                # Preserve the scanner's dict-string semantics; review names and strings.
                schedule: dict[str, Any] = {}
                for name, entry in value.items():
                    if not isinstance(name, str) or text(field + ".name", name) != name:
                        raise ValueError("Unsupported schedule name")
                    if isinstance(entry, str):
                        schedule[name] = text(field + "." + name, entry)
                    elif entry is None or isinstance(entry, (bool, int, float)):
                        schedule[name] = entry
                    else:
                        raise ValueError("Unsupported schedule value")
                row[key] = schedule
            else:
                row[key] = text(field, value)
        projected_jobs.append(row)
    return projected, {"jobs": projected_jobs}


def _hash(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def source_manifest(source: Path, scanner: Path, state: Path,
                    settings: Mapping[str, Any], *,
                    captured: dict[str, bytes] | None = None) -> dict[str, dict[str, Any]]:
    """One private inventory with deliberate link closure and original identities.

    Configured source roots outside source are unsupported rather than silently
    omitted by the reusable inventory helper. Generated notes are recursive;
    directory links in generated notes are rejected instead of followed.
    """
    source, scanner, state = (p.resolve(strict=True) for p in (source, scanner, state))
    roots = {"source": source, "scanner/skills": scanner / "skills", "seed/okfs": state / "okfs"}
    for key in ("custom_skill_dirs", "script_dirs", "memory_dirs", "runbook_dirs"):
        for raw in settings.get(key, []):
            target = (source / raw).resolve(strict=True)
            if not target.is_relative_to(source):
                raise ValueError("Configured scanner root escapes supported source layout")
    output: dict[str, dict[str, Any]] = {}

    def logical(path: Path) -> str:
        target = path.resolve(strict=True)
        for name, root in sorted(roots.items(), key=lambda pair: len(pair[1].parts), reverse=True):
            if target.is_relative_to(root.resolve()):
                return name + "/" + target.relative_to(root.resolve()).as_posix()
        raise ValueError("Scanner source link has an uncaptured target")

    def record(name: str, kind: str, path: Path) -> None:
        if any(p.startswith(".env") or p in {"auth.json", "id_rsa", "id_ed25519"} for p in path.parts):
            raise ValueError("Credential-file scanner input requires scope decision")
        target = path.resolve(strict=True)
        item: dict[str, Any] = {"kind": kind, "path": str(path), "canonical": logical(path)}
        if path.is_symlink():
            item["link"] = os.readlink(path)
        stat = target.stat()
        item["identity"] = [stat.st_dev, stat.st_ino]
        item["mode"] = stat.st_mode & 0o777
        if target.is_file() and kind != "name_only":
            payload = stable_read(target)
            if captured is not None:
                captured[name] = payload
            after = target.stat()
            if (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns) != (
                    after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
                raise ValueError("Scanner source drift during hashing")
            item["sha256"] = _hash(payload)
            item["size"] = len(payload)
        output[name] = item

    inventories = (("source", scanner_tree_inventory(source, settings=settings, state_root=state)),
                   ("scanner/skills", scanner_tree_inventory(scanner / "skills", settings=settings,
                                                              runtime_skills=True)))
    for family, inventory in inventories:
        for relative, (kind, path) in inventory.items():
            record(family + "/" + relative, kind, path)
    notes = state / "okfs"
    if not notes.is_dir():
        raise ValueError("Configured generated-note root missing")
    for directory, dirs, files in os.walk(notes, followlinks=False):
        parent = Path(directory)
        if any((parent / d).is_symlink() for d in dirs):
            raise ValueError("Generated-note directory link requires scope decision")
        for name in files:
            p = parent / name
            if p.suffix == ".md":
                record("seed/okfs/" + p.relative_to(notes).as_posix(), "readable", p)
    # Selected runtime files are hashed, NOT copied or parsed into model context.
    for name in ("config.yaml", "cron/jobs.json"):
        p = scanner / name
        if not p.resolve(strict=True).is_relative_to(scanner):
            raise ValueError("Selected runtime projection escapes scanner home")
        before = p.stat()
        payload = stable_read(p)
        if captured is not None:
            captured["scanner/" + name] = payload
        after = p.stat()
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            raise ValueError("Selected runtime source drift")
        output["scanner/" + name] = {"kind": "projection", "path": str(p),
                                   "sha256": _hash(payload), "size": len(payload)}
    return dict(sorted(output.items()))


def verify_source_manifest(expected: Mapping[str, Any], source: Path, scanner: Path,
                           state: Path, settings: Mapping[str, Any]) -> None:
    if dict(expected) != source_manifest(source, scanner, state, settings):
        raise ValueError("Scanner source/manifest drift")


def require_reviews(manifest: Mapping[str, Any], reviews: Mapping[str, Any]) -> None:
    """Reject incomplete/stale review before any frozen-data materialization.

    A review is an actual content attestation bound to original AND projected
    bytes, not a token regex result. Projected files remain controller-private
    until all entries pass. Original hashes never belong in the worker manifest.
    """
    wanted = {key for key, item in manifest.items() if "sha256" in item}
    if set(reviews) != wanted:
        raise ValueError("Missing or unexpected scanner content reviews")
    for key in sorted(wanted):
        item = reviews[key]
        if not isinstance(item, dict) or not item.get("review") or item.get("original_sha256") != manifest[key]["sha256"]:
            raise ValueError("Missing or stale scanner content review")
        path = Path(item["projected_path"])
        if not path.is_file() or path.is_symlink() or _hash(path.read_bytes()) != item.get("projected_sha256"):
            raise ValueError("Projected scanner content drift")
        path.read_text(encoding="utf-8", errors="strict")


def write_private_report(path: Path, report: Mapping[str, Any]) -> None:
    """Create a new owner-only report; refuse overwrite of prior evidence."""
    with path.open("x", encoding="utf-8") as output:
        path.chmod(0o600)
        json.dump(dict(report), output, indent=2, sort_keys=True)
        output.write("\n")


# Policy is intentionally conservative: unresolved credential-bearing VALUES are
# removed locally. No raw value, snippet or credential hash enters a review log.
POLICY = "lk-corpus-structural-scrub-v2"
MARKER = "LK_REDACTED"
_SECRET = re.compile(r"(?i)(?:password|passwd|secret|token|api[_-]?key|authorization|credential|client[_-]?secret|cookie)")
_SHELL_ENV_REFERENCE = re.compile(r"\$\{?[A-Z_][A-Z0-9_]*\}?")
_ENV_API_REFERENCES = frozenset({"os.getenv", "os.environ", "process.env"})
_ASSIGN = re.compile(r"(?i)((?:[\w.-]*(?:password|passwd|secret|token|api[_-]?key|authorization|credential|cookie)[\w.-]*)[\"']?\s*(?:=|:)\s*)([\"'])(.*?)(\2)")
_BARE = re.compile(r"(?i)((?:[\w.-]*(?:password|passwd|secret|token|api[_-]?key|authorization|credential|cookie)[\w.-]*)\s*(?:=|:)\s*)([^\s,;\]}\"'&<>`()]+)")
_OPTION = re.compile(r"(?i)(--[\w-]*(?:password|passwd|secret|token|api[_-]?key|credential)[\w-]*\s+)(?:([\"'])(.*?)(\2)|([^\s,;\]}\"'`]+))")
_HEADER = re.compile(r"(?i)((?:authorization|proxy-authorization|x-api-key|cookie|set-cookie)\s*:\s*)([^\r\n\"'`]+)")
_URL = re.compile(r"https?://[^\s\"'<>`]+")
_KEYBLOCK = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S)
_TOKEN = re.compile(r"(?<![\w])(?:[A-Za-z0-9_+/=-]{24,})(?![\w])")
_KNOWN = re.compile(r"(?<![\w])(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{16,}|AKIA[A-Z0-9]{16}|eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)")


def stable_read(path: Path) -> bytes:
    """Descriptor-bound regular bytes, bounded retry; never output values."""
    for _ in range(3):
        with path.open("rb") as stream:
            before = os.fstat(stream.fileno())
            data = stream.read()
            after = os.fstat(stream.fileno())
        current = path.stat()
        def keys(s: os.stat_result) -> tuple[int, ...]:
            return (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
        if keys(before) == keys(after) == keys(current):
            return data
    raise ValueError("Unstable scanner capture (metadata only)")


def _entropy(value: str) -> float:
    return -sum((n / len(value)) * math.log2(n / len(value)) for n in Counter(value).values())


def _is_environment_reference(value: str) -> bool:
    """Classify a complete reference, never evaluate an API or expand a value."""
    return value in _ENV_API_REFERENCES or _SHELL_ENV_REFERENCE.fullmatch(value) is not None


def _scrub_text(text: str, counts: Counter) -> str:
    text = text.replace("<redacted>", MARKER)
    def substitute(pattern: re.Pattern, callback: Callable) -> None:
        nonlocal text
        def apply(match: re.Match) -> str:
            replacement = callback(match)
            if replacement != match.group():
                counts[pattern.pattern[:24]] += 1
            return replacement
        text = pattern.sub(apply, text)
    substitute(_KEYBLOCK, lambda m: MARKER)
    substitute(_URL, lambda m: _sanitize_mcp_url(m.group()).replace("<redacted>", MARKER))
    substitute(_HEADER, lambda m: m[1] + MARKER if MARKER not in m[2] else m.group())
    substitute(_ASSIGN, lambda m: m[1] + m[2] + MARKER + m[4]
               if MARKER not in m[3] and not _is_environment_reference(m[3]) else m.group())
    substitute(_BARE, lambda m: m[1] + MARKER
               if MARKER not in m[2] and not _is_environment_reference(m[2])
               and m[2] not in ('"', "'", '[', '{') else m.group())
    substitute(_OPTION, lambda m: m[1] + (m[2] or "") + MARKER + (m[2] or "")
               if MARKER not in (m[3] or m[5] or "") and not _is_environment_reference(m[3] or m[5] or "") else m.group())
    substitute(_KNOWN, lambda m: MARKER)
    # Noncredential hashes and identifiers are contextual evidence, not an
    # automatic exemption: explicit credential contexts were removed above.
    def triage(m: re.Match) -> str:
        value = m.group()
        if re.fullmatch(r"[a-fA-F0-9]{40}|[a-fA-F0-9]{64}", value):
            counts["noncredential_digest_context"] += 1
            return value
        if _entropy(value) >= 4.2 and any(c.isdigit() for c in value) and any(c.islower() for c in value):
            return MARKER
        return value
    substitute(_TOKEN, triage)
    return text


def scrub_content(text: str, *, suffix: str = "") -> tuple[str, dict[str, Any]]:
    """Structural Python literal projection + contextual prose/span removal.

    Environment lookups/names are retained; literal placeholder examples are
    removed in credential contexts just like real values. Python is reparsed;
    unresolved f-string/compound credential literals are removed as a whole
    expression, never rewritten into syntactically broken pseudo-code.
    """
    counts: Counter = Counter()
    if suffix == ".py":
        try:
            tree = ast.parse(text)
        except SyntaxError:
            # Already-invalid scanner inputs remain inputs; do not certify or
            # repair parsing. Their complete text still undergoes span removal.
            safe = _scrub_text(text, counts)
            return safe, {"policy": POLICY, "parse": "source-invalid", "transformations": dict(counts)}
        secret_spans: set[tuple[int, int]] = set()
        lines = text.encode("utf-8").splitlines(keepends=True)
        offsets = [0]
        for line in lines:
            offsets.append(offsets[-1] + len(line))
        def mark(node: ast.expr) -> None:
            if isinstance(node, ast.Call) and ast.unparse(node.func) in {"os.getenv", "os.environ.get", "getenv", "environ.get"}:
                # Keep the environment NAME, but not a literal fallback value.
                for fallback in node.args[1:]:
                    mark(fallback)
                for keyword in node.keywords:
                    if keyword.arg != "key":
                        mark(keyword.value)
                return
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and _is_environment_reference(node.value):
                counts["reviewed-environment-reference"] += 1
                return
            if not any(isinstance(part, (ast.Constant, ast.JoinedStr)) for part in ast.walk(node)):
                return
            assert node.end_lineno is not None and node.end_col_offset is not None
            secret_spans.add((offsets[node.lineno-1] + node.col_offset,
                              offsets[node.end_lineno-1] + node.end_col_offset))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if any(_SECRET.search(ast.unparse(t)) for t in targets) and node.value is not None:
                    mark(node.value)
            elif isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values):
                    if isinstance(key, ast.Constant) and isinstance(key.value, str) and _SECRET.search(key.value) and value is not None:
                        mark(value)
            elif isinstance(node, ast.keyword) and node.arg and _SECRET.search(node.arg):
                mark(node.value)
        data = text.encode("utf-8")
        outer_spans: list[tuple[int, int]] = []
        for start, end in sorted(secret_spans, key=lambda span: (span[0], -span[1])):
            if not outer_spans or start >= outer_spans[-1][1]:
                outer_spans.append((start, end))
        for start, end in reversed(outer_spans):
            data = data[:start] + repr(MARKER).encode() + data[end:]
            counts["structural_literals"] += 1
        text = data.decode("utf-8")
        tokens = []
        for token in tokenize.generate_tokens(io.StringIO(text).readline):
            if token.type in (tokenize.STRING, tokenize.COMMENT):
                replacement = _scrub_text(token.string, counts)
                token = token._replace(string=replacement)
            tokens.append(token)
        safe = tokenize.untokenize(tokens)
        try:
            ast.parse(safe)
        except SyntaxError:
            raise ValueError("Projection changed valid script parsing (no values)") from None
        parse = "valid-preserved"
    else:
        safe = _scrub_text(text, counts)
        parse = "not-python"
    # Idempotent residual check catches incomplete removals. Structural checks
    # are rerun by callers on projected files, not merely a review string.
    residual_counts: Counter = Counter()
    residual_inputs = ([t.string for t in tokenize.generate_tokens(io.StringIO(safe).readline)
                        if t.type in (tokenize.STRING, tokenize.COMMENT)]
                       if suffix == ".py" else [safe])
    if any(_scrub_text(value, residual_counts) != value for value in residual_inputs):
        raise ValueError("Unresolved credential-span residual (no values)")
    return safe, {"policy": POLICY, "parse": parse,
                  "structural_literals": counts.get("structural_literals", 0),
                  "transformations": dict(counts), "residual": "clear-under-policy"}


def frozen_manifest(corpus: Path) -> dict[str, Any]:
    records: dict[str, Any] = {}
    for directory, dirs, files in os.walk(corpus, followlinks=False):
        parent = Path(directory)
        for name in sorted(dirs + files):
            p = parent / name
            rel = p.relative_to(corpus).as_posix()
            if rel == "manifest.json":
                continue
            if p.is_symlink():
                if not p.resolve(strict=True).is_relative_to(corpus.resolve()):
                    raise ValueError("Frozen link closure escaped corpus")
                records[rel] = {"link": os.readlink(p)}
            elif p.is_file():
                records[rel] = {"sha256": _hash(p.read_bytes()), "size": p.stat().st_size}
    return records


def verify_frozen(corpus: Path, expected_manifest_hash: str | None = None) -> None:
    manifest_bytes = (corpus / "manifest.json").read_bytes()
    if expected_manifest_hash is not None and _hash(manifest_bytes) != expected_manifest_hash:
        raise ValueError("Frozen corpus manifest integrity failure")
    expected = json.loads(manifest_bytes)
    if frozen_manifest(corpus) != expected["inputs"]:
        raise ValueError("Frozen integrity/manifest drift")


def freeze_corpus(source: Path, scanner: Path, state: Path, settings: Mapping[str, Any],
                  destination: Path, *, parse_config: Callable[[str], Any] = json.loads,
                  capture_audit: Callable[[], Any] | None = None) -> dict[str, Any]:
    """Capture then project; subsequent production changes are future campaigns.

    Raw bytes exist only in local memory, never in a materialized corpus/report.
    Optional pinned collector audit executes in the short capture phase, before
    stability verification. No production service/index/telemetry is constructed.
    Returned provenance is private; worker manifest contains projected hashes only.
    """
    if destination.exists():
        raise ValueError("Frozen destination already exists")
    captured: dict[str, bytes] = {}
    manifest = source_manifest(source, scanner, state, settings, captured=captured)
    audit = capture_audit() if capture_audit else None
    verify_source_manifest(manifest, source, scanner, state, settings)
    # From this boundary onward only captured bytes are used.
    config = parse_config(captured["scanner/config.yaml"].decode("utf-8"))
    registry = json.loads(captured["scanner/cron/jobs.json"])
    field_reviews = {}
    def review(field: str, value: str) -> str:
        safe, outcome = scrub_content(value)
        field_reviews[field] = outcome
        return safe
    projected_config, projected_registry = project_runtime(config, registry, review)
    projected_runtime = {"scanner/config.yaml": projected_config,
                         "scanner/cron/jobs.json": projected_registry}
    del config, registry
    destination.mkdir(mode=0o700)
    reviews = {}
    for key, item in manifest.items():
        target = destination / key
        target.parent.mkdir(parents=True, exist_ok=True)
        if item["kind"] == "symlink":
            continue
        if item["kind"] == "name_only":
            payload, outcome = b"", {"policy": POLICY, "reason": "scanner-consumes-name-only"}
        elif key in projected_runtime:
            safe = json.dumps(projected_runtime[key], sort_keys=True, indent=2) + "\n"
            payload = safe.encode()
            outcome = {"policy": POLICY, "reason": "allowlisted-inert-fields", "residual": "structural-fields-projected"}
        else:
            safe, outcome = scrub_content(captured[key].decode("utf-8"), suffix=target.suffix)
            payload = safe.encode()
        target.write_bytes(payload)
        if "sha256" in item:
            reviews[key] = {"original_sha256": item["sha256"], "projected_sha256": _hash(payload),
                            "policy_sha256": _hash(Path(__file__).read_bytes()), "outcome": outcome}
    # Preserve canonical aliases; links are relative and remain portable.
    for key, item in manifest.items():
        if item["kind"] == "symlink":
            target = destination / key
            canonical = destination / item["canonical"]
            if not canonical.exists():
                raise ValueError("Uncaptured canonical alias target")
            target.symlink_to(os.path.relpath(canonical, target.parent), target_is_directory=canonical.is_dir())
    coverage = None
    if audit is not None:
        coverage = collector_audit(destination / "source", destination / "scanner",
                                   destination / "seed", settings)
        expected = {(a["id"], a["type"], a["source"]) for a in audit["artifacts"]}
        actual = {(a["id"], a["type"], a["source"]) for a in coverage["artifacts"]}
        if expected != actual:
            raise ValueError(f"Captured/projected artifact parity mismatch: missing={len(expected-actual)} extra={len(actual-expected)}")
        mapping = []
        identities = set()
        for a in coverage["artifacts"]:
            locator = artifact_locator(a["path"], destination / "source", destination / "scanner", destination / "seed")
            logical = _hash(json.dumps([a["type"], a["source"], locator,
                                      a["id"] if a["type"] == "script" else ""]).encode())
            if logical in identities:
                raise ValueError("Ambiguous logical artifact mapping")
            identities.add(logical)
            path = destination / locator.split("#", 1)[0]
            if a["type"] == "skill":
                path = path / "SKILL.md"
            if not path.is_file():
                raise ValueError("Logical artifact target missing")
            mapping.append({**a, "logical_id": logical, "locator": locator,
                            "content_sha256": _hash(path.read_bytes())})
        (destination / "mapping.json").write_text(json.dumps(mapping, sort_keys=True, indent=2) + "\n")
    worker = {"policy": POLICY, "settings": dict(settings), "inputs": frozen_manifest(destination)}
    (destination / "manifest.json").write_text(json.dumps(worker, sort_keys=True, indent=2) + "\n")
    # Read-only data is additionally enforced at the Docker mount boundary.
    for directory, dirs, files in os.walk(destination, followlinks=False):
        for name in files:
            p = Path(directory) / name
            if not p.is_symlink():
                p.chmod(0o444)
        Path(directory).chmod(0o555)
    verify_frozen(destination)
    return {"snapshot_semantics": "captured-inputs", "source_manifest": manifest,
            "reviews": reviews, "field_reviews": field_reviews, "capture_audit": audit, "projected_coverage": coverage,
            "projected_manifest_sha256": _hash((destination / "manifest.json").read_bytes())}



def artifact_locator(display: str, source: Path, scanner: Path, seed: Path) -> str:
    path, separator, fragment = display.partition("#")
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = source / p
    for prefix, anchor in (("seed", seed), ("scanner", scanner), ("source", source)):
        if p.is_relative_to(anchor):
            return prefix + "/" + p.relative_to(anchor).as_posix() + (separator + fragment if separator else "")
    raise ValueError("Artifact locator outside captured roots")


def collector_audit(source: Path, scanner: Path, seed: Path, settings: Mapping[str, Any]) -> dict[str, Any]:
    """Pinned collector eligibility/dedup metadata, never source text or queries."""
    from hermes_local_knowledge import artifacts as owner
    from hermes_local_knowledge.config import IndexSettings
    s = IndexSettings(**dict(settings))
    candidates = [*owner._skill_candidates(source, scanner, s),
                  *owner._custom_support_candidates(source, s),
                  *owner._runtime_support_candidates(source, scanner, s),
                  *owner._script_candidates(source, s),
                  *owner._source_markdown_candidates(source, s, (seed / "okfs",)),
                  *owner._cron_candidates(source, scanner, s),
                  *owner._mcp_candidates(source, scanner, s),
                  *owner._tool_okf_candidates(seed / "okfs", source, s)]
    eligible = owner.collect_artifacts(source, scanner, s, okf_root=seed / "okfs")
    ids = set()
    identities = set()
    dedup = []
    for candidate in sorted(candidates, key=lambda c: (c.priority, c.artifact.source or "", c.artifact.path, c.artifact.id)):
        a = candidate.artifact
        reason = "duplicate-id" if a.id in ids else "canonical-source-alias" if candidate.source_identity in identities else None
        if reason:
            dedup.append({"id": a.id, "type": a.type, "path": a.path, "reason": reason})
        else:
            ids.add(a.id)
            identities.add(candidate.source_identity)
    if ids != {a.id for a in eligible}:
        raise ValueError("Collector exhaustive audit disagrees")
    return {"artifacts": [{"id": a.id, "type": a.type, "path": a.path, "source": a.source} for a in eligible],
            "counts": dict(Counter(a.type for a in eligible)), "candidate_count": len(candidates),
            "dedup": dedup, "collector_origin": str(Path(owner.__file__).resolve()),
            "collector_sha256": _hash(Path(owner.__file__).read_bytes())}

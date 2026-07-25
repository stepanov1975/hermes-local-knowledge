# Minimal Review Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Correct every confirmed review defect with small, test-first changes appropriate for a private-host Hermes plugin.

**Architecture:** Keep the current modules and public wrappers. Add only narrow local helpers for MCP sanitization, staged OKF validation, equivalence-family matching, and SQLite file URIs; use explicit collision errors instead of an ID migration. Preserve SQLite as the runtime index and JSONL as diagnostic output.

**Tech Stack:** Python 3.11+, standard-library runtime, SQLite FTS5, pytest, Ruff, mypy, setuptools/build, Twine.

## Global Constraints

- This plugin runs on a private host for private use. Corporate-grade threat modeling and generalized secret-detection frameworks are out of scope.
- Credential filtering must cover only obvious credential-bearing forms while preserving useful MCP routing metadata.
- Prefer focused helpers and explicit errors over new abstractions, migrations, background services, or runtime dependencies.
- Preserve whole-artifact routing; do not introduce chunk retrieval.
- Preserve `hermes_local_knowledge/indexer.py` and `hermes_local_knowledge/plugin.py` exports, CLI compatibility, and monkeypatch seams.
- Environment variable names may be indexed; environment variable values must never be indexed.
- FTS remains the primary broad-recall path and pure quoted queries remain strict-only.
- Parent-equivalent evaluation remains limited to owning-skill and `skill_support_doc` families.
- Every production behavior change starts with a focused test that fails for the expected reason.
- Bump `plugin.yaml`, `pyproject.toml`, and `hermes_local_knowledge/__init__.py` together from `0.3.10` to `0.3.11`.
- Work in the current checkout as explicitly approved; do not create a worktree.

---

### Task 1: Make MCP routing metadata safe and portable

**Files:**
- Modify: `hermes_local_knowledge/scanners.py:377-470`
- Modify: `hermes_local_knowledge/text_utils.py:228-230`
- Modify: `hermes_local_knowledge/scanners.py:573-595`
- Test: `tests/test_indexer.py`

**Interfaces:**
- Consumes: existing `parse_mcp_servers_fallback()`, `scan_mcp_servers()`, `extract_paths()`, and `collect_artifacts()` behavior.
- Produces: private scanner helpers `_sanitize_mcp_args(args: Any) -> str` and `_sanitize_mcp_url(value: str) -> str`; expanded `extract_paths(text: str) -> list[str]`; explicit `ValueError` for duplicate artifact IDs.

- [ ] **Step 1: Add focused failing scanner tests**

Add these behavior tests to `tests/test_indexer.py`, using its existing `write()` and `build_fixture()` helpers:

```python
def test_scan_mcp_servers_redacts_only_obvious_credentials(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    hermes_home = tmp_path / "home"
    root.mkdir()
    write(
        hermes_home / "config.yaml",
        """mcp_servers:
  demo:
    command: npx
    args: [demo-package, --profile, local, --api-key, TEST_ONLY_SECRET, --token=SECOND_SECRET]
    url: https://user:URL_SECRET@example.invalid/api/v1?token=QUERY_SECRET#fragment
""",
    )

    artifact = lci.scan_mcp_servers(root, hermes_home)[0]
    persisted = "\n".join([artifact.summary, artifact.search_text, " ".join(artifact.triggers)])

    assert "demo-package" in persisted
    assert "--profile local" in persisted
    assert "example.invalid/api/v1" in persisted
    for secret in ("TEST_ONLY_SECRET", "SECOND_SECRET", "URL_SECRET", "QUERY_SECRET"):
        assert secret not in persisted


def test_mcp_fallback_parses_block_args_and_env_names_without_values(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "repo"
    hermes_home = tmp_path / "home"
    root.mkdir()
    write(
        hermes_home / "config.yaml",
        """mcp_servers:
  home:
    command: npx
    args:
      - @acme/home-assistant-mcp
      - --stdio
    env:
      HOMEASSISTANT_URL: private-value
""",
    )
    monkeypatch.setattr(lci_scanners, "load_yaml_if_available", lambda _path: None)

    artifact = lci.scan_mcp_servers(root, hermes_home)[0]

    assert "@acme/home-assistant-mcp" in artifact.search_text
    assert "HOMEASSISTANT_URL" in artifact.search_text
    assert "private-value" not in artifact.search_text


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (r"C:\\Users\\alex\\tools\\run.py", r"C:\\Users\\alex\\tools\\run.py"),
        (r"\\\\server\\share\\run.py", r"\\\\server\\share\\run.py"),
        ("/Users/alex/tools/run.py", "/Users/alex/tools/run.py"),
        ("/opt/tools/run.sh", "/opt/tools/run.sh"),
    ],
)
def test_extract_paths_supports_cross_platform_absolute_paths(raw: str, expected: str) -> None:
    assert expected in lci.extract_paths(f"wrapper {raw}")
    assert lci.extract_paths(f"https://example.invalid/api/{Path(expected).name}") == []


def test_collect_artifacts_rejects_duplicate_ids_instead_of_dropping_one(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    hermes_home = tmp_path / "home"
    write(root / "scripts" / "foo-bar.py", "# first\n")
    write(root / "scripts" / "foo_bar.py", "# second\n")
    hermes_home.mkdir()

    with pytest.raises(ValueError, match=r"duplicate artifact id script:scripts-foo-bar-py"):
        lci.collect_artifacts(root, hermes_home)
```

- [ ] **Step 2: Run the new tests and verify RED**

Run:

```powershell
$env:PYTHONPATH='C:\Users\alex\AppData\Local\Temp\codex-hermes-review-deps-20260725'
& 'C:\Users\alex\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m pytest -q tests/test_indexer.py -k 'redacts_only_obvious_credentials or fallback_parses_block_args or cross_platform_absolute_paths or rejects_duplicate_ids'
```

Expected: credential strings remain visible, fallback metadata is missing, non-Linux paths are absent, and duplicate IDs do not raise.

- [ ] **Step 3: Implement minimal MCP sanitization and fallback parsing**

In `scanners.py`, add a small token predicate and sanitizers near the fallback parser:

```python
_MCP_SECRET_WORDS = {"token", "secret", "password", "authorization", "credential"}
_MCP_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b([A-Za-z0-9_-]*(?:token|secret|password|authorization|credential|api[_-]?key)[A-Za-z0-9_-]*)=([^\s]+)"
)


def _mcp_secret_option(value: str) -> bool:
    name = value.split("=", 1)[0].lstrip("-").lower()
    parts = set(re.findall(r"[a-z0-9]+", name))
    return bool(parts & _MCP_SECRET_WORDS) or "apikey" in name.replace("-", "").replace("_", "") or {
        "api",
        "key",
    } <= parts


def _sanitize_mcp_args(args: Any) -> str:
    values = [str(item) for item in args] if isinstance(args, list) else [str(args)] if args else []
    output: list[str] = []
    redact_next = False
    for value in values:
        if redact_next:
            output.append("<redacted>")
            redact_next = False
        elif value.startswith("-") and "=" in value and _mcp_secret_option(value):
            output.append(f"{value.split('=', 1)[0]}=<redacted>")
        else:
            output.append(value)
            redact_next = value.startswith("-") and _mcp_secret_option(value)
    return " ".join(output)
```

Use `urllib.parse.urlsplit`/`urlunsplit` in `_sanitize_mcp_url()` to discard user information, query, and fragment while retaining scheme, hostname, optional port, and path. Apply the existing secret-assignment substitution to `command`. Use only sanitized values in summary, related paths, metadata terms, triggers, entities, and `search_text`.
Use `_MCP_SECRET_ASSIGNMENT.sub(lambda match: f"{match.group(1)}=<redacted>", command)` for the command string; do not add content- or entropy-based scanning.

Extend `parse_mcp_servers_fallback()` with one `current_container` state. When `args:` or `env:` has no scalar value, initialize `[]` or `{}`; append `- value` rows under `args`; record only mapping keys under `env` with an empty string value.

Inside the existing server loop, implement the nested cases exactly as follows after calculating `value_indent`:

```python
nested_indent = value_indent + 2
value_match = re.match(rf"^\s{{{value_indent}}}([A-Za-z0-9_-]+):\s*(.*)$", raw_line)
if current and value_match:
    key, raw_value = value_match.groups()
    value = raw_value.strip().strip("'\"")
    if key == "args" and not value:
        servers[current][0][key] = []
        current_container = "args"
    elif key == "env" and not value:
        servers[current][0][key] = {}
        current_container = "env"
    else:
        servers[current][0][key] = value
        current_container = None
    continue
if current and current_container == "args":
    item = re.match(rf"^\s{{{nested_indent}}}-\s*(.+)$", raw_line)
    if item:
        servers[current][0]["args"].append(item.group(1).strip().strip("'\""))
        continue
if current and current_container == "env":
    env_key = re.match(rf"^\s{{{nested_indent}}}([A-Za-z0-9_-]+):", raw_line)
    if env_key:
        servers[current][0]["env"][env_key.group(1)] = ""
        continue
current_container = None
```

- [ ] **Step 4: Extend path extraction without URL leakage**

In `text_utils.py`, replace the Linux-home-only expression with bounded token matching for drive paths, UNC paths, `~/`, and absolute POSIX paths. Require an absolute POSIX slash to occur at the start or after whitespace so `https://host/api` is not treated as a local path. Strip the same trailing punctuation as today.

Use this bounded expression rather than a filesystem crawler:

```python
_LOCAL_PATH = re.compile(
    r'(?:[A-Za-z]:[\\/][^\s\'"`]+|\\\\[^\s\'"`]+|~[\\/][^\s\'"`]+|(?<![A-Za-z0-9:/])/(?!/)[^\s\'"`]+)'
)


def extract_paths(text: str) -> list[str]:
    return unique_preserve_order(match.rstrip("`.,);]") for match in _LOCAL_PATH.findall(text))
```

When `resolve_related()` computes a basename in `scanners.py`, use `PureWindowsPath(clean).name` for drive/UNC/backslash paths and `Path(clean).name` otherwise.

- [ ] **Step 5: Reject duplicate artifact IDs**

Replace last-write-wins assignment with:

```python
deduped: dict[str, Artifact] = {}
for artifact in artifacts:
    previous = deduped.get(artifact.id)
    if previous is not None and previous.path != artifact.path:
        raise ValueError(f"duplicate artifact id {artifact.id}: {previous.path} and {artifact.path}")
    deduped[artifact.id] = artifact
```

- [ ] **Step 6: Verify GREEN and run the complete scanner/search test file**

Run the focused command from Step 2, then:

```powershell
& 'C:\Users\alex\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m pytest -q tests/test_indexer.py -p no:cacheprovider
```

Expected: all tests pass with only environment-specific skips.

- [ ] **Step 7: Commit Task 1**

```powershell
git add hermes_local_knowledge/scanners.py hermes_local_knowledge/text_utils.py tests/test_indexer.py
git commit -m "fix: harden MCP artifact scanning"
```

---

### Task 2: Correct quoted search and parent-equivalent metrics

**Files:**
- Modify: `hermes_local_knowledge/text_utils.py:249-255`
- Modify: `hermes_local_knowledge/evaluation.py:170-177`
- Modify: `scripts/compare_historical_query_versions.py:116-130`
- Test: `tests/test_indexer.py`

**Interfaces:**
- Consumes: `fts_query(query: str, operator: str = "AND") -> str` and the existing `Mapping[str, set[str]]` equivalence map.
- Produces: FTS5 phrase expressions for balanced quotes and `_equivalence_family(artifact_id, equivalents) -> set[str]` in both evaluators.

- [ ] **Step 1: Add failing quoted-phrase and sibling-equivalence tests**

Add this search regression:

```python
def test_quoted_phrase_requires_adjacent_terms(tmp_path: Path) -> None:
    db_path = tmp_path / "index.sqlite"
    adjacent = lci.Artifact(
        id="runbook:adjacent",
        type="runbook",
        title="Adjacent",
        path="docs/adjacent.md",
        summary="alpha beta",
        search_text="alpha beta",
    )
    separated = lci.Artifact(
        id="runbook:separated",
        type="runbook",
        title="Separated",
        path="docs/separated.md",
        summary="alpha intervening words beta",
        search_text="alpha intervening words beta",
    )
    lci.build_sqlite(db_path, [adjacent, separated], [])

    assert [row["id"] for row in lci.search_index(db_path, '"alpha beta"', limit=5)] == [
        "runbook:adjacent"
    ]
```

Extend `test_parent_equivalent_metrics_only_count_support_doc_parent_pairs()` with:

```python
siblings = lci.evaluate_search_labels(
    {"query": {"skill_support_doc:child-a"}},
    lambda _query, _limit: ["skill_support_doc:child-b"],
    parent_equivalents={
        "skill:parent": {"skill_support_doc:child-a", "skill_support_doc:child-b"},
        "skill_support_doc:child-a": {"skill:parent"},
        "skill_support_doc:child-b": {"skill:parent"},
    },
)
assert siblings.parent_equiv_hit_at_1 == 1.0
```

Add this direct regression for the historical helper's duplicated comparator:

```python
def test_compare_helper_parent_equivalence_counts_sibling_support_docs() -> None:
    helper = load_compare_helper()
    equivalents = {
        "skill:parent": {"skill_support_doc:child-a", "skill_support_doc:child-b"},
        "skill_support_doc:child-a": {"skill:parent"},
        "skill_support_doc:child-b": {"skill:parent"},
    }
    assert helper.matches_parent(
        "skill_support_doc:child-b",
        {"skill_support_doc:child-a"},
        equivalents,
    ) is True
```

- [ ] **Step 2: Run focused tests and verify RED**

```powershell
& 'C:\Users\alex\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m pytest -q tests/test_indexer.py -k 'quoted_phrase_requires_adjacency or parent_equivalent_metrics_only_count or historical_parent_equivalent_siblings'
```

Expected: the separated-term artifact is returned and sibling equivalence scores 0.

- [ ] **Step 3: Implement phrase-aware FTS construction**

In `fts_query()`, scan balanced single- or double-quoted spans with `re.finditer`. Convert quoted content to normalized `query_terms()` and emit one FTS component such as `"alpha beta"`; convert text outside quotes to the current `term*` components. Join components with ` OR ` only when `operator.upper() == "OR"`, otherwise with a space. Unbalanced quotes remain ordinary punctuation and preserve existing behavior.

Use this direct implementation:

```python
def fts_query(query: str, *, operator: str = "AND") -> str:
    components: list[str] = []
    cursor = 0
    quoted = re.compile(r'(?<!\w)(["\'])([^"\'\n]+)\1(?!\w)')
    for match in quoted.finditer(query):
        components.extend(f"{term}*" for term in query_terms(query[cursor : match.start()]))
        phrase_terms = query_terms(match.group(2))
        if phrase_terms:
            components.append(f'"{" ".join(phrase_terms)}"')
        cursor = match.end()
    components.extend(f"{term}*" for term in query_terms(query[cursor:]))
    separator = " OR " if operator.upper() == "OR" else " "
    return separator.join(components)
```

- [ ] **Step 4: Implement equivalence-family intersection in both evaluators**

Use the same exact helper logic in `evaluation.py` and the self-contained historical script:

```python
def _equivalence_family(artifact_id: str, equivalents: Mapping[str, set[str]]) -> set[str]:
    return {artifact_id, *equivalents.get(artifact_id, set())}


def _matches_with_parent_equivalence(...):
    result_family = _equivalence_family(result_id, parent_equivalents)
    return any(result_family & _equivalence_family(expected_id, parent_equivalents) for expected_id in expected_ids)
```

Do not consult generic graph edges.

- [ ] **Step 5: Verify GREEN and the full indexer suite**

Run the focused command from Step 2, then `pytest -q tests/test_indexer.py -p no:cacheprovider` with the bundled Python path.

- [ ] **Step 6: Commit Task 2**

```powershell
git add hermes_local_knowledge/text_utils.py hermes_local_knowledge/evaluation.py scripts/compare_historical_query_versions.py tests/test_indexer.py
git commit -m "fix: preserve quoted and parent-equivalent semantics"
```

---

### Task 3: Make OKF generation collision-safe and validate before publication

**Files:**
- Modify: `hermes_local_knowledge/hooks.py:212-265`
- Modify: `hermes_local_knowledge/okf.py:887-947`
- Test: `tests/test_hooks.py`
- Test: `tests/test_okf.py`

**Interfaces:**
- Consumes: existing `okf_file_path()`, `validate_okf_file()`, `mark_candidate_error()`, and `mark_candidate_done()` lifecycle.
- Produces: backward-compatible `validate_okf_file(..., expected_path: Path | None = None)` and randomized staged publication in `_write_and_complete_item()`.

- [ ] **Step 1: Add failing staged-validation, rollback, and collision tests**

Add a `test_validate_okf_file_accepts_staged_content_for_claimed_final_path()` to `tests/test_okf.py`: seed and claim a candidate, write valid rendered content to a random `.md` file under `okf_dir`, call validation with `path=staged` and `expected_path=okf_file_path(...)`, and assert valid. Also assert the same call without `expected_path` remains invalid because manual validation still requires the claimed final target.
Import `pytest` and `hooks` in `tests/test_okf.py`; import `pytest` in `tests/test_hooks.py`.

Use this complete setup:

```python
def test_validate_okf_file_accepts_staged_content_for_claimed_final_path(tmp_path: Path) -> None:
    schema = {"type": "object"}
    okf.upsert_tool_candidate(tmp_path, tool_name="demo", toolset="demo", schema=schema, args={})
    row = okf.claim_candidates(tmp_path, limit=1, min_use_count=1)[0]
    item = {
        "tool": "demo",
        "schema_hash": row["schema_hash"],
        "title": "Demo tool",
        "aliases": ["route requests through demo"],
        "triggers": ["use demo for matching operations"],
        "when_not_to_use": [],
        "related_tools": [],
        "body": "Use demo for its matching operation.",
    }
    staged = okf.okf_dir(tmp_path) / ".demo.random.md"
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_text(hooks._render_okf(item, toolset="demo"), encoding="utf-8")
    target = okf.okf_file_path(tmp_path, "demo")

    assert okf.validate_okf_file(
        tmp_path,
        claim_token=str(row["claim_token"]),
        path=staged,
        expected_path=target,
    )["valid"] is True
    assert okf.validate_okf_file(
        tmp_path,
        claim_token=str(row["claim_token"]),
        path=staged,
    )["valid"] is False
```

Add two tests to `tests/test_hooks.py` using existing `configure()` and candidate helpers:

```python
def test_write_and_complete_restores_previous_file_when_validation_raises(tmp_path: Path, monkeypatch) -> None:
    _repo, _home, state_dir = configure(tmp_path, monkeypatch, enabled=True, auto_generate=True)
    schema = {"type": "object"}
    okf.upsert_tool_candidate(state_dir, tool_name="demo", toolset="demo", schema=schema, args={})
    row = okf.claim_candidates(state_dir, limit=1, min_use_count=1)[0]
    path = okf.okf_file_path(state_dir, "demo")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"previous")
    item = {
        "tool": "demo",
        "schema_hash": row["schema_hash"],
        "title": "Demo tool",
        "aliases": ["route requests through demo"],
        "triggers": ["use demo for matching operations"],
        "when_not_to_use": [],
        "related_tools": [],
        "body": "Use demo for its matching operation.",
    }
    monkeypatch.setattr(okf, "validate_okf_file", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("validation failed")))

    with pytest.raises(RuntimeError, match="validation failed"):
        hooks._write_and_complete_item(SimpleNamespace(state_dir=state_dir), row=row, item=item)

    assert path.read_bytes() == b"previous"
    assert list(path.parent.glob(f".{path.name}.*.md")) == []


def test_write_and_complete_rejects_colliding_tool_slug_without_overwrite(tmp_path: Path, monkeypatch) -> None:
    _repo, _home, state_dir = configure(tmp_path, monkeypatch, enabled=True, auto_generate=True)
    rows = []
    for tool_name in ("foo-bar", "foo_bar"):
        okf.upsert_tool_candidate(state_dir, tool_name=tool_name, toolset="demo", schema={"type": "object"}, args={})
        rows.append(okf.claim_candidates(state_dir, limit=1, min_use_count=1)[0])

    def item(row: dict[str, object]) -> dict[str, object]:
        tool_name = str(row["tool_name"])
        return {
            "tool": tool_name,
            "schema_hash": row["schema_hash"],
            "title": f"Tool OKF: {tool_name}",
            "aliases": [f"route requests through {tool_name}"],
            "triggers": [f"use {tool_name} for matching operations"],
            "when_not_to_use": [],
            "related_tools": [],
            "body": f"Use {tool_name} for its matching operation.",
        }

    cfg = SimpleNamespace(state_dir=state_dir)
    assert hooks._write_and_complete_item(cfg, row=rows[0], item=item(rows[0])) is True
    assert hooks._write_and_complete_item(cfg, row=rows[1], item=item(rows[1])) is False

    rendered = okf.okf_file_path(state_dir, "foo-bar").read_text(encoding="utf-8")
    assert 'tool: "foo-bar"' in rendered
    assert okf.queue_counts(state_dir) == {"done": 1, "pending": 1}
```

Use literal candidate rows and generation items matching the shapes already used by `test_session_finalize_generates_bounded_okf_with_host_llm`; do not mock queue state.

- [ ] **Step 2: Run focused tests and verify RED**

```powershell
& 'C:\Users\alex\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m pytest -q tests/test_okf.py tests/test_hooks.py -k 'staged_content or restores_previous_file or colliding_tool_slug'
```

Expected: `expected_path` is rejected as an unknown argument, validation exceptions leave the published file, and colliding tools overwrite.

- [ ] **Step 3: Extend validation for private staged files**

Change the signature to:

```python
def validate_okf_file(
    state_dir: Path,
    *,
    claim_token: str,
    path: Path,
    expected_path: Path | None = None,
) -> dict[str, Any]:
```

Continue reading and validating content from `path`. Require both the staged path and expected target to remain under `okf_dir`; compare the claimed target against `(expected_path or path).resolve()`. Preserve the current return payload and all callers that omit `expected_path`.

- [ ] **Step 4: Implement randomized staging and deterministic restoration**

In `_write_and_complete_item()`:

1. If the final file exists, parse its frontmatter before writing. When its non-empty `tool` differs from the exact claimed tool name, call `mark_candidate_error(..., error="generated target collision")` and return `False`.
2. Create a closed `tempfile.NamedTemporaryFile(prefix=f".{path.name}.", suffix=".md", dir=path.parent, delete=False)`.
3. Write rendered text to that random path and call `validate_okf_file(..., path=temp_path, expected_path=path)`.
4. On invalid content, mark the candidate error without publishing.
5. On valid content, `os.replace(temp_path, path)` and set `published = True`.
6. If `mark_candidate_done()` returns false or any later exception occurs, call `_restore_file(path, previous)`.
7. In `finally`, unlink the random staging path.

Do not add scanner-to-queue attestation or a new persistence layer.

- [ ] **Step 5: Verify GREEN and the complete OKF/hook suites**

Run the focused command from Step 2, then:

```powershell
& 'C:\Users\alex\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m pytest -q tests/test_okf.py tests/test_hooks.py tests/test_cli_okf.py -p no:cacheprovider
```

- [ ] **Step 6: Commit Task 3**

```powershell
git add hermes_local_knowledge/hooks.py hermes_local_knowledge/okf.py tests/test_hooks.py tests/test_okf.py
git commit -m "fix: validate OKFs before publication"
```

---

### Task 4: Keep index generations and SQLite file URIs consistent

**Files:**
- Modify: `hermes_local_knowledge/storage.py:327-346,411-414`
- Modify: `hermes_local_knowledge/indexer.py:137-161`
- Modify: `hermes_local_knowledge/evaluation.py:130-145`
- Modify: `scripts/compare_historical_query_versions.py:49-100`
- Test: `tests/test_indexer.py`

**Interfaces:**
- Consumes: the existing compatibility `indexer.build_index()` monkeypatch seams and `storage.build_index()` lock/retry behavior.
- Produces: `readonly_sqlite_uri(path: Path) -> str` in `storage.py`, used by runtime/evaluation; equivalent local construction in the standalone historical script.

- [ ] **Step 1: Add failing publication-order and URI-path tests**

Add this parametrized publication regression:

```python
@pytest.mark.parametrize(
    ("builder", "module"),
    [(lci.build_index, lci), (lci_storage.build_index, lci_storage)],
)
def test_sqlite_publication_failure_preserves_previous_jsonl(
    tmp_path: Path,
    monkeypatch,
    builder,
    module,
) -> None:  # type: ignore[no-untyped-def]
    root, hermes_home = build_fixture(tmp_path)
    output_dir = tmp_path / "state"
    builder(root, output_dir, hermes_home)
    before = (output_dir / "index.jsonl").read_bytes()
    write(root / "docs" / "new.md", "# New generation\n")
    monkeypatch.setattr(module, "build_sqlite", lambda *_args, **_kwargs: (_ for _ in ()).throw(PermissionError("blocked")))

    with pytest.raises(PermissionError, match="blocked"):
        builder(root, output_dir, hermes_home)

    assert (output_dir / "index.jsonl").read_bytes() == before
```

Add:

```python
@pytest.mark.parametrize("marker", ["state#one", "state%one"])
def test_readonly_sqlite_uris_escape_special_state_paths(tmp_path: Path, marker: str) -> None:
    db_path = tmp_path / marker / "index.sqlite"
    artifact = lci.Artifact(
        id="skill:alpha",
        type="skill",
        title="Alpha",
        path="custom_skills/alpha/SKILL.md",
        summary="Alpha",
        search_text="alpha",
    )
    lci.build_sqlite(db_path, [artifact], [])
    assert lci.get_artifact(db_path, "skill:alpha") is not None
```

Create a usage database under the same special-character parent with `create_usage_db(usage_db)` and assert `lci.load_positive_feedback_labels(usage_db)` returns its existing literal labels. Add:

```python
def test_historical_helper_escapes_sqlite_uri_paths(tmp_path: Path) -> None:
    helper = load_compare_helper()
    db_path = tmp_path / "state#one" / "index.sqlite"
    artifact = lci.Artifact(
        id="skill:alpha",
        type="skill",
        title="Alpha",
        path="custom_skills/alpha/SKILL.md",
        summary="Alpha",
        search_text="alpha",
    )
    lci.build_sqlite(db_path, [artifact], [])
    assert helper.artifact_ids(db_path) == {"skill:alpha"}
```

- [ ] **Step 2: Run focused tests and verify RED**

```powershell
& 'C:\Users\alex\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m pytest -q tests/test_indexer.py -k 'sqlite_publication_failure or readonly_sqlite_uris_escape or historical_helper_escapes'
```

Expected: JSONL changes before the injected failure and `#` paths read the wrong SQLite URI.

- [ ] **Step 3: Publish SQLite before JSONL in both builders**

In both `storage.build_index()` and compatibility `indexer.build_index()`, retain all existing locking and newer-format checks but order the final calls as:

```python
build_sqlite(output_dir / "index.sqlite", artifacts, edges)
write_jsonl(output_dir / "index.jsonl", artifacts)
```

- [ ] **Step 4: Centralize encoded runtime SQLite URIs**

In `storage.py` add:

```python
def readonly_sqlite_uri(path: Path) -> str:
    return f"{path.expanduser().resolve().as_uri()}?mode=ro"
```

Use it in `connect_readonly()`. Import and use it in `evaluation.py` for the usage database. In the historical helper, which must remain self-contained per ref, use the same `path.expanduser().resolve().as_uri()` expression locally.

- [ ] **Step 5: Verify GREEN and run all index/evaluation tests**

Run the focused command from Step 2 and then `pytest -q tests/test_indexer.py -p no:cacheprovider`.

- [ ] **Step 6: Commit Task 4**

```powershell
git add hermes_local_knowledge/storage.py hermes_local_knowledge/indexer.py hermes_local_knowledge/evaluation.py scripts/compare_historical_query_versions.py tests/test_indexer.py
git commit -m "fix: publish consistent index generations"
```

---

### Task 5: Make diagnostics, JSON CLI errors, and source packages truthful

**Files:**
- Modify: `hermes_local_knowledge/cli.py:614-689,757-990`
- Modify: `tests/test_cli_install.py`
- Modify: `tests/test_indexer.py`
- Modify: `MANIFEST.in`
- Modify: `pyproject.toml`
- Create: `tests/test_packaging.py`

**Interfaces:**
- Consumes: existing `_emit_payload()`, `_record_cli_usage()`, doctor check payloads, and setuptools `MANIFEST.in`.
- Produces: `_emit_cli_json_error(command: str, message: str) -> None`; complete JSON-mode failure envelopes; self-contained source distribution.

- [ ] **Step 1: Add failing doctor and JSON CLI tests**

In `tests/test_cli_install.py`, add:

```python
@pytest.mark.parametrize("auto_generate", [False, True])
def test_doctor_rejects_disabled_okf_queue_even_when_generation_is_enabled(
    tmp_path: Path,
    capsys,
    auto_generate: bool,
) -> None:  # type: ignore[no-untyped-def]
    hermes_home = tmp_path / "hermes_home"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(
        "local_knowledge:\n  okf:\n    enabled: false\n"
        f"    auto_generate: {str(auto_generate).lower()}\n",
        encoding="utf-8",
    )

    assert lci_cli.main(["doctor", "--hermes-home", str(hermes_home), "--json"]) == 0
    check = doctor_checks(stdout_json(capsys))["okf_auto_generate"]
    assert check["ok"] is False
    assert "local_knowledge.okf.enabled true" in str(check["detail"])
    if not auto_generate:
        assert "local_knowledge.okf.auto_generate true" in str(check["detail"])
```

In `tests/test_indexer.py`, add:

```python
def test_cli_get_missing_artifact_emits_json_error(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "index.sqlite"
    lci.build_sqlite(db_path, [], [])
    assert lci.main(["get", "skill:missing", "--db", str(db_path), "--json"]) == 1
    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out) == {
        "command": "get",
        "error": "Artifact not found: skill:missing",
        "success": False,
    }


@pytest.mark.parametrize("command", ["search", "get", "neighbors", "evaluate"])
def test_cli_json_mode_serializes_operational_errors(command: str, tmp_path: Path, capsys) -> None:
    missing = tmp_path / "missing.sqlite"
    argv = {
        "search": ["search", "alpha", "--db", str(missing), "--json"],
        "get": ["get", "skill:alpha", "--db", str(missing), "--json"],
        "neighbors": ["neighbors", "skill:alpha", "--db", str(missing), "--json"],
        "evaluate": ["evaluate", "--db", str(missing), "--json"],
    }[command]
    assert lci.main(argv) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["success"] is False
    assert payload["command"] == command
    assert payload["error"]
```

- [ ] **Step 2: Add a failing behavioral sdist test**

Add `build>=1,<2` to the test extra only. Create `tests/test_packaging.py` that runs:

```python
import os
import subprocess
import sys
import tarfile
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_sdist_contains_files_required_by_shipped_tests(tmp_path: Path) -> None:
    dist_dir = tmp_path / "dist"
    subprocess.run(
        [sys.executable, "-m", "build", "--sdist", "--no-isolation", "--outdir", str(dist_dir)],
        cwd=PROJECT_ROOT,
        check=True,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    archive_path = next(dist_dir.glob("*.tar.gz"))
    with tarfile.open(archive_path, "r:gz") as archive:
        members = {
            name.split("/", 1)[1]
            for name in archive.getnames()
            if "/" in name
        }

    required = {
        "plugin.yaml",
        "after-install.md",
        "scripts/__init__.py",
        "scripts/check_version_policy.py",
        "scripts/compare_historical_query_versions.py",
        "skills/local-knowledge-router/SKILL.md",
        "examples/local-knowledge-router-skill/SKILL.md",
        "tests/test_version_policy.py",
    }
    assert required <= members
```

This exercises produced package behavior rather than grepping manifest text.

- [ ] **Step 3: Run focused tests and verify RED**

```powershell
$env:PYTHONPATH='C:\Users\alex\AppData\Local\Temp\codex-hermes-review-deps-20260725'
& 'C:\Users\alex\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m pytest -q tests/test_cli_install.py tests/test_indexer.py tests/test_packaging.py -k 'doctor_rejects_disabled or json_mode_serializes or missing_artifact_emits or sdist'
```

Expected: doctor falsely passes, CLI raises/prints stderr, and the built sdist lacks required members.

- [ ] **Step 4: Correct doctor's two-flag check**

Set `okf_ready = cfg.okf.enabled and cfg.okf.auto_generate`. Build a short list of missing commands:

```python
recommendations = []
if not cfg.okf.enabled:
    recommendations.append("hermes config set local_knowledge.okf.enabled true")
if not cfg.okf.auto_generate:
    recommendations.append("hermes config set local_knowledge.okf.auto_generate true")
```

Use `okf_ready` as the check value and join only the needed recommendations in its detail.

- [ ] **Step 5: Serialize all JSON-mode CLI errors**

Add:

```python
def _emit_cli_json_error(command: str, message: str) -> None:
    _emit_payload({"success": False, "command": command, "error": message}, json_output=True)
```

In the existing search/get/neighbors exception handlers, keep telemetry recording. Preserve the special newer-index payload. For every other exception, if `args.json`, emit `f"{type(exc).__name__}: {exc}"` and return 1; otherwise re-raise. For a missing artifact, emit the literal not-found message when JSON mode is active and retain stderr in human mode. Wrap evaluation with the same JSON-only handling.

- [ ] **Step 6: Make the sdist self-contained**

Set `MANIFEST.in` to include the existing lines plus:

```text
include plugin.yaml
include after-install.md
include scripts/__init__.py
include scripts/check_version_policy.py
recursive-include skills *.md
recursive-include examples *.md
```

Do not add unrelated repository files.

- [ ] **Step 7: Verify GREEN and run CLI/package tests**

Run the focused command from Step 3, then:

```powershell
& 'C:\Users\alex\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m pytest -q tests/test_cli_install.py tests/test_indexer.py tests/test_packaging.py -p no:cacheprovider
```

- [ ] **Step 8: Commit Task 5**

```powershell
git add hermes_local_knowledge/cli.py tests/test_cli_install.py tests/test_indexer.py tests/test_packaging.py MANIFEST.in pyproject.toml
git commit -m "fix: make diagnostics and source packages truthful"
```

---

### Task 6: Version, changelog, and complete verification

**Files:**
- Modify: `plugin.yaml`
- Modify: `pyproject.toml`
- Modify: `hermes_local_knowledge/__init__.py`
- Modify: `CHANGELOG.md`
- Test: complete repository and package gates

**Interfaces:**
- Consumes: all behavior completed in Tasks 1-5.
- Produces: synchronized release version `0.3.11` and a verified wheel/sdist pair.

- [ ] **Step 1: Update synchronized version metadata and changelog**

Set all three version locations to `0.3.11`. Prepend this concise changelog entry:

```markdown
## [0.3.11] - 2026-07-25

### Fixed

- Kept obvious MCP credential arguments out of routing metadata while preserving useful local identifiers and paths.
- Rejected artifact and tool-OKF slug collisions instead of silently overwriting whole artifacts.
- Validated generated OKFs before atomic publication and kept failed index generations consistent.
- Corrected quoted search, parent-equivalent evaluation, cross-platform MCP wrapper paths, JSON diagnostics, SQLite special-character paths, and source-distribution contents.
```

Add `[0.3.11]: https://github.com/stepanov1975/hermes-local-knowledge/compare/v0.3.10...v0.3.11` beside the existing release links.

- [ ] **Step 2: Run the full test suite**

```powershell
$env:PYTHONPATH='C:\Users\alex\AppData\Local\Temp\codex-hermes-review-deps-20260725'
$env:PYTHONDONTWRITEBYTECODE='1'
& 'C:\Users\alex\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m pytest -q -p no:cacheprovider
```

Expected: exit 0 with only documented environment skips.

- [ ] **Step 3: Run lint, type, version-policy, and whitespace gates**

```powershell
& 'C:\Users\alex\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m ruff check .
& 'C:\Users\alex\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m mypy --no-incremental --platform linux
& 'C:\Users\alex\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' scripts/check_version_policy.py --base-ref origin/main
git diff --check origin/main..HEAD
```

Expected: all exit 0; mypy reports no issues in the configured files; version metadata reports `0.3.11` and the required bump.

- [ ] **Step 4: Build and inspect release artifacts**

Create fresh temporary output and virtual-environment paths outside the repository. Run:

```powershell
$packageRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("hermes-package-" + [guid]::NewGuid().ToString("N"))
$distDir = Join-Path $packageRoot "dist"
$venvDir = Join-Path $packageRoot "venv"
New-Item -ItemType Directory -Path $distDir -Force | Out-Null
& 'C:\Users\alex\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m build --outdir $distDir
& 'C:\Users\alex\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m twine check "$distDir\*"
& 'C:\Users\alex\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m venv $venvDir
& "$venvDir\Scripts\python.exe" -m pip install --no-deps "$distDir\hermes_local_knowledge-0.3.11-py3-none-any.whl"
```

From outside the checkout, verify the installed distribution version is `0.3.11`, the `hermes_agent.plugins` entry point is `local_knowledge = hermes_local_knowledge.plugin`, `register` is callable, and the module path is under the isolated virtual environment.

- [ ] **Step 5: Verify the unpacked sdist**

Extract `hermes_local_knowledge-0.3.11.tar.gz` to a fresh temporary directory. With the review dependencies on `PYTHONPATH`, run pytest from the extracted source root and expect exit 0 with only environment-specific skips.

```powershell
$sdistRoot = Join-Path $packageRoot "sdist"
New-Item -ItemType Directory -Path $sdistRoot -Force | Out-Null
tar -xf "$distDir\hermes_local_knowledge-0.3.11.tar.gz" -C $sdistRoot
$sdistSource = Join-Path $sdistRoot "hermes_local_knowledge-0.3.11"
$env:PYTHONPATH='C:\Users\alex\AppData\Local\Temp\codex-hermes-review-deps-20260725'
$env:PYTHONDONTWRITEBYTECODE='1'
Push-Location $sdistSource
try {
    & 'C:\Users\alex\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m pytest -q -p no:cacheprovider
} finally {
    Pop-Location
}
```

- [ ] **Step 6: Run configured Hermes smokes when available**

Check `Get-Command hermes`. If unavailable, record the smoke as unavailable, not failed. If available, run the repository-prescribed build/evaluate/doctor commands against the configured Hermes home without exposing local telemetry or secrets.

- [ ] **Step 7: Commit Task 6**

```powershell
git add plugin.yaml pyproject.toml hermes_local_knowledge/__init__.py CHANGELOG.md
git commit -m "chore: release version 0.3.11"
```

- [ ] **Step 8: Request final whole-branch review**

Generate a review package from the implementation base through `HEAD`. The reviewer must verify all specification items, KISS scope, compatibility surfaces, test quality, version policy, and absence of generated/local state. Any Important or Critical finding receives one consolidated fix wave followed by one scoped re-review.

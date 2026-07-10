"""Command-line interface for the local knowledge indexer."""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Sequence

from . import __version__
from .constants import DEFAULT_ROOT
from .evaluation import evaluate_index_against_feedback_report
from .models import IndexSettings
from . import okf
from .paths import default_output_dir, hermes_home_from_env
from .runtime import RuntimeConfig, _runtime_config
from .search import search_index
from .storage import (
    artifact_type_counts,
    build_index,
    get_artifact,
    get_neighbors,
    index_build_lock,
    index_metadata,
)
from .telemetry import _record_usage


ROUTER_SKILL_RELATIVE_PATH = Path("skills") / "local-knowledge-router" / "SKILL.md"


def print_results(rows: Sequence[dict[str, Any]]) -> None:
    for row in rows:
        print(f"{row['id']} [{row['type']}] {row['title']}")
        print(f"  path: {row['path']}")
        print(f"  summary: {row['summary']}")
        if row.get("edge_kind"):
            print(f"  edge: {row['edge_kind']} ({row.get('edge_evidence', '')})")
        if row.get("triggers"):
            print(f"  triggers: {', '.join(row['triggers'][:12])}")
        print()


def _resolved(path: Path) -> Path:
    return path.expanduser().resolve()


def _bundled_router_skill_path() -> Path:
    return (Path(__file__).resolve().parent / ROUTER_SKILL_RELATIVE_PATH).resolve()


def _installed_router_skill_path(hermes_home: Path) -> Path:
    lexical_home = Path(os.path.abspath(hermes_home.expanduser()))
    return lexical_home / ROUTER_SKILL_RELATIVE_PATH


def _router_target_safety_error(hermes_home: Path, target: Path) -> str | None:
    if target.is_symlink():
        return "router skill target is a symbolic link; refusing to follow it"
    resolved_home = hermes_home.expanduser().resolve()
    resolved_parent = target.parent.resolve()
    try:
        resolved_parent.relative_to(resolved_home)
    except ValueError:
        return "router skill target parent resolves outside the Hermes home"
    return None


def _atomic_write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw_temp_path = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temp_path = Path(raw_temp_path)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def _print_warnings(warnings: Sequence[str]) -> None:
    for warning in warnings:
        print(f"WARNING: {warning}", file=sys.stderr)


def _cfg_metadata(cfg: RuntimeConfig, db_path: Path | None = None) -> dict[str, Any]:
    return {
        "plugin_version": __version__,
        "root": str(cfg.source_root),
        "source_root_source": cfg.source_root_source,
        "state_dir": str(cfg.state_dir),
        "state_dir_source": cfg.state_dir_source,
        "include_markdown_docs_source": cfg.include_markdown_docs_source,
        "db_path": str(db_path or (cfg.state_dir / "index.sqlite")),
        "warnings": list(cfg.warnings),
    }


def _usage_db_for_state_dir(state_dir: Path) -> Path:
    return state_dir / "usage.sqlite"


def _record_cli_usage(
    cfg: RuntimeConfig | None,
    *,
    tool: str,
    success: bool,
    query: str = "",
    artifact_id: str = "",
    artifact_type: str = "",
    limit_value: int | None = None,
    rebuild_requested: bool = False,
    rebuilt: bool | None = None,
    error: str = "",
    result_count: int | None = None,
    top_ids: list[str] | None = None,
    top_types: list[str] | None = None,
    latency_ms: int | None = None,
    db_path: Path | None = None,
    index_meta: dict[str, Any] | None = None,
    usage_db_path: Path | None = None,
) -> int | None:
    root = cfg.source_root if cfg is not None else None
    metadata = _cfg_metadata(cfg, db_path) if cfg is not None else {"plugin_version": __version__}
    metadata.update(index_meta or {})
    if usage_db_path is None:
        if cfg is None:
            usage_db_path = (db_path.parent / "usage.sqlite") if db_path is not None else None
        else:
            usage_db_path = _usage_db_for_state_dir(cfg.state_dir)
    return _record_usage(
        root,
        tool=tool,
        success=success,
        query=query,
        artifact_id=artifact_id,
        artifact_type=artifact_type,
        limit_value=limit_value,
        rebuild_requested=rebuild_requested,
        rebuilt=rebuilt,
        error=error,
        result_count=result_count,
        top_ids=top_ids,
        top_types=top_types,
        latency_ms=latency_ms,
        db_path=db_path,
        client="cli",
        index_metadata=metadata,
        usage_db_path=usage_db_path,
    )


def add_common_db_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="SQLite index path (default: <hermes-home>/local_knowledge/index.sqlite, or configured state_dir with --from-hermes-config)",
    )
    parser.add_argument("--hermes-home", type=Path, default=None, help="Hermes home directory")
    parser.add_argument(
        "--from-hermes-config",
        action="store_true",
        help="read local_knowledge settings from <hermes-home>/config.yaml like the native plugin does",
    )


def add_okf_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--hermes-home", type=Path, default=None, help="Hermes home directory")
    parser.add_argument(
        "--from-hermes-config",
        action="store_true",
        help="read local_knowledge settings from <hermes-home>/config.yaml like the native plugin does",
    )
    parser.add_argument("--state-dir", type=Path, default=None, help="OKF queue/state directory override")
    parser.add_argument("--json", action="store_true", help="emit JSON")


def _add_doctor_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--hermes-home", type=Path, default=None, help="Hermes home directory")
    parser.add_argument("--rebuild", action="store_true", help="build the configured index as part of the check")
    parser.add_argument("--query", default=None, help="optional smoke search query to run against the index")
    parser.add_argument("--limit", type=int, default=5, help="smoke search result limit")
    parser.add_argument("--json", action="store_true", help="emit JSON")


def _add_install_router_skill_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--hermes-home", type=Path, default=None, help="Hermes home directory")
    parser.add_argument(
        "--force",
        action="store_true",
        help="replace a different existing local-knowledge-router skill",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON")


def setup_hermes_cli(parser: argparse.ArgumentParser) -> None:
    """Register the small setup/diagnostic surface under ``hermes local-knowledge``."""
    subparsers = parser.add_subparsers(dest="local_knowledge_command", required=True)
    install_parser = subparsers.add_parser(
        "install-router-skill",
        help="install the proactive router skill into the active Hermes profile",
    )
    _add_install_router_skill_args(install_parser)
    doctor_parser = subparsers.add_parser(
        "doctor",
        help="check plugin configuration and installation health",
    )
    _add_doctor_args(doctor_parser)


def handle_hermes_cli(args: argparse.Namespace) -> int:
    """Dispatch the Hermes-native CLI adapter through the standalone CLI."""
    command = str(args.local_knowledge_command)
    argv = [command]
    if args.hermes_home is not None:
        argv.extend(["--hermes-home", str(args.hermes_home)])
    if command == "install-router-skill":
        if args.force:
            argv.append("--force")
    elif command == "doctor":
        if args.rebuild:
            argv.append("--rebuild")
        if args.query is not None:
            argv.extend(["--query", str(args.query)])
        argv.extend(["--limit", str(args.limit)])
    if args.json:
        argv.append("--json")
    status = main(argv)
    if status:
        raise SystemExit(status)
    return status


def _add_okf_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    okf_parser = subparsers.add_parser("okf", help="manage generated tool OKF candidate queue")
    okf_subparsers = okf_parser.add_subparsers(dest="okf_command", required=True)

    status_parser = okf_subparsers.add_parser("status", help="show OKF queue status")
    status_parser.add_argument("--limit", type=int, default=10, help="candidate preview limit per state")
    add_okf_common_args(status_parser)

    claim_parser = okf_subparsers.add_parser("claim", help="claim pending OKF candidates")
    claim_parser.add_argument("--limit", type=int, default=1)
    claim_parser.add_argument("--min-use-count", type=int, default=None)
    claim_parser.add_argument("--claim-token", default=None)
    add_okf_common_args(claim_parser)

    validate_parser = okf_subparsers.add_parser("validate", help="validate an authored OKF file")
    validate_parser.add_argument("--claim-token", required=True)
    validate_parser.add_argument("--path", type=Path, required=True)
    add_okf_common_args(validate_parser)

    complete_parser = okf_subparsers.add_parser("complete", help="mark a claimed OKF candidate complete")
    complete_parser.add_argument("--claim-token", required=True)
    complete_parser.add_argument("--tool", required=True)
    complete_parser.add_argument("--path", type=Path, required=True)
    add_okf_common_args(complete_parser)

    fail_parser = okf_subparsers.add_parser("fail", help="mark a claimed OKF candidate failed")
    fail_parser.add_argument("--claim-token", required=True)
    fail_parser.add_argument("--tool", required=True)
    fail_parser.add_argument("--error", required=True)
    add_okf_common_args(fail_parser)

    retry_parser = okf_subparsers.add_parser("retry", help="reset an exhausted OKF candidate for retry")
    retry_parser.add_argument("--tool", required=True)
    add_okf_common_args(retry_parser)

def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    build_parser = subparsers.add_parser("build", help="build index.sqlite and index.jsonl")
    build_parser.add_argument("--root", type=Path, default=None, help="source directory to index (default: current directory)")
    build_parser.add_argument("--hermes-home", type=Path, default=None, help="Hermes home directory")
    build_parser.add_argument("--output-dir", type=Path, default=None, help="output directory (default: <hermes-home>/local_knowledge)")
    build_parser.add_argument(
        "--from-hermes-config",
        action="store_true",
        help="read local_knowledge source_root, state_dir, and scanner settings from <hermes-home>/config.yaml",
    )

    search_parser = subparsers.add_parser("search", help="search artifacts")
    search_parser.add_argument("query", help="search query")
    search_parser.add_argument("--limit", type=int, default=10)
    search_parser.add_argument("--json", action="store_true", help="emit JSON")
    add_common_db_arg(search_parser)

    get_parser = subparsers.add_parser("get", help="show one artifact by id")
    get_parser.add_argument("artifact_id")
    get_parser.add_argument("--json", action="store_true", help="emit JSON")
    add_common_db_arg(get_parser)

    neighbors_parser = subparsers.add_parser("neighbors", help="show graph neighbors for one artifact")
    neighbors_parser.add_argument("artifact_id")
    neighbors_parser.add_argument("--json", action="store_true", help="emit JSON")
    add_common_db_arg(neighbors_parser)

    evaluate_parser = subparsers.add_parser(
        "evaluate",
        help="replay positive feedback labels against the current search index",
    )
    evaluate_parser.add_argument("--usage-db", type=Path, default=None, help="usage.sqlite path (default: index directory)")
    evaluate_parser.add_argument("--json", action="store_true", help="emit JSON")
    evaluate_parser.add_argument("--details", action="store_true", help="include per-query ranks and top result IDs")
    add_common_db_arg(evaluate_parser)

    doctor_parser = subparsers.add_parser(
        "doctor",
        aliases=["smoke"],
        help="check runtime config, paths, index presence, and optional smoke build/search",
    )
    _add_doctor_args(doctor_parser)

    install_skill_parser = subparsers.add_parser(
        "install-router-skill",
        help="install the bundled router skill into the active Hermes profile",
    )
    _add_install_router_skill_args(install_skill_parser)

    _add_okf_parser(subparsers)
    return parser.parse_args(argv)


def _build_config_from_args(args: argparse.Namespace) -> RuntimeConfig:
    if args.from_hermes_config:
        cfg = _runtime_config(args.hermes_home)
        updates: dict[str, Any] = {}
        if args.root is not None:
            updates["source_root"] = _resolved(args.root)
            updates["source_root_source"] = "cli"
            updates["warnings"] = ()
            if cfg.include_markdown_docs_source == "default":
                updates["index_settings"] = replace(cfg.index_settings, include_markdown_docs=True)
                updates["include_markdown_docs_source"] = "cli"
        if args.output_dir is not None:
            updates["state_dir"] = _resolved(args.output_dir)
            updates["state_dir_source"] = "cli"
        return replace(cfg, **updates) if updates else cfg

    hermes_home = _resolved(args.hermes_home) if args.hermes_home is not None else _resolved(hermes_home_from_env())
    source_root = _resolved(args.root) if args.root is not None else _resolved(DEFAULT_ROOT)
    state_dir = _resolved(args.output_dir) if args.output_dir is not None else _resolved(default_output_dir(hermes_home))
    return RuntimeConfig(
        source_root,
        hermes_home,
        state_dir,
        IndexSettings(),
        source_root_source="cli" if args.root is not None else "cwd",
        state_dir_source="cli" if args.output_dir is not None else "default",
    )


def _db_from_args(args: argparse.Namespace) -> tuple[Path, tuple[str, ...], RuntimeConfig]:
    if args.from_hermes_config:
        cfg = _runtime_config(args.hermes_home)
        db_path = _resolved(args.db) if args.db is not None else cfg.state_dir / "index.sqlite"
        if args.db is not None:
            cfg = replace(cfg, state_dir=db_path.parent, state_dir_source="cli")
        return db_path, cfg.warnings, cfg
    hermes_home = _resolved(args.hermes_home) if args.hermes_home is not None else _resolved(hermes_home_from_env())
    db_path = _resolved(args.db) if args.db is not None else default_output_dir(hermes_home) / "index.sqlite"
    cfg = RuntimeConfig(
        _resolved(DEFAULT_ROOT),
        hermes_home,
        db_path.parent,
        IndexSettings(),
        source_root_source="cwd",
        state_dir_source="cli" if args.db is not None else "default",
    )
    return db_path, (), cfg


def _build_index_locked(build_index_fn, cfg: RuntimeConfig):  # type: ignore[no-untyped-def]
    with index_build_lock(cfg.state_dir):
        return build_index_fn(cfg.source_root, cfg.state_dir, cfg.hermes_home, cfg.index_settings)


def _refresh_default_index_if_dirty(
    db_path: Path,
    cfg: RuntimeConfig,
    *,
    build_index_fn,
    explicit_db: bool,
) -> bool:  # type: ignore[no-untyped-def]
    if explicit_db:
        return False
    default_db_path = (cfg.state_dir / "index.sqlite").resolve()
    if db_path.resolve() != default_db_path:
        return False
    dirty_tokens = okf.index_dirty_tokens(cfg.state_dir)
    if db_path.exists() and not dirty_tokens:
        return False
    _build_index_locked(build_index_fn, cfg)
    for token in dirty_tokens:
        token.unlink(missing_ok=True)
    return True


def _okf_config_from_args(args: argparse.Namespace) -> RuntimeConfig:
    cfg = _runtime_config(args.hermes_home) if args.from_hermes_config else RuntimeConfig(
        _resolved(DEFAULT_ROOT),
        _resolved(args.hermes_home) if args.hermes_home is not None else _resolved(hermes_home_from_env()),
        default_output_dir(_resolved(args.hermes_home) if args.hermes_home is not None else _resolved(hermes_home_from_env())),
        IndexSettings(),
        source_root_source="cwd",
        state_dir_source="default",
    )
    if args.state_dir is not None:
        cfg = replace(cfg, state_dir=_resolved(args.state_dir), state_dir_source="cli")
    return cfg


def _emit_payload(payload: dict[str, Any], *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        for key, value in payload.items():
            if isinstance(value, (dict, list)):
                print(f"{key}: {json.dumps(value, ensure_ascii=False, sort_keys=True)}")
            else:
                print(f"{key}: {value}")


def _install_router_skill_payload(hermes_home: Path, *, force: bool) -> tuple[dict[str, Any], int]:
    source = _bundled_router_skill_path()
    target = _installed_router_skill_path(hermes_home)
    base: dict[str, Any] = {
        "source": str(source),
        "target": str(target),
    }
    try:
        if not source.is_file():
            return {
                **base,
                "success": False,
                "status": "error",
                "error": "bundled local-knowledge-router skill is missing",
            }, 1
        bundled_content = source.read_bytes()

        safety_error = _router_target_safety_error(hermes_home, target)
        if safety_error is not None:
            return {
                **base,
                "success": False,
                "status": "conflict",
                "force_required": False,
                "error": safety_error,
            }, 1

        if target.exists():
            if not target.is_file():
                return {
                    **base,
                    "success": False,
                    "status": "conflict",
                    "force_required": False,
                    "error": "router skill target exists but is not a regular file",
                }, 1
            existing_content = target.read_bytes()
            if existing_content == bundled_content:
                return {
                    **base,
                    "success": True,
                    "status": "current",
                    "overwritten": False,
                    "message": "normal local-knowledge-router skill is already current",
                }, 0
            if not force:
                return {
                    **base,
                    "success": False,
                    "status": "conflict",
                    "force_required": True,
                    "error": (
                        "a different local-knowledge-router skill already exists; "
                        "review it before rerunning with --force"
                    ),
                }, 1
            overwritten = True
        else:
            overwritten = False

        safety_error = _router_target_safety_error(hermes_home, target)
        if safety_error is not None:
            return {
                **base,
                "success": False,
                "status": "conflict",
                "force_required": False,
                "error": safety_error,
            }, 1
        _atomic_write_bytes(target, bundled_content)
    except OSError as exc:
        return {
            **base,
            "success": False,
            "status": "error",
            "error": f"failed to install router skill: {type(exc).__name__}: {exc}",
        }, 1
    return {
        **base,
        "success": True,
        "status": "installed",
        "overwritten": overwritten,
        "message": "installed normal local-knowledge-router skill",
    }, 0


def _okf_status_payload(cfg: RuntimeConfig, *, limit: int) -> dict[str, Any]:
    pending = okf.pending_candidates(cfg.state_dir, limit=max(1, limit), min_use_count=cfg.okf.min_use_count)
    errors = okf.error_candidates(cfg.state_dir, limit=max(1, limit))
    return {
        "success": True,
        "state_dir": str(cfg.state_dir),
        "okf_dir": str(okf.okf_dir(cfg.state_dir)),
        "queue_db": str(okf.okf_queue_db_path(cfg.state_dir)),
        "counts": okf.queue_counts(cfg.state_dir),
        "pending": [okf.candidate_packet(row, cfg.state_dir) for row in pending],
        "errors": [okf.candidate_packet(row, cfg.state_dir) for row in errors],
    }


def _handle_okf_command(args: argparse.Namespace) -> int:
    cfg = _okf_config_from_args(args)
    _print_warnings(cfg.warnings)
    if args.okf_command == "status":
        _emit_payload(_okf_status_payload(cfg, limit=args.limit), json_output=args.json)
        return 0
    if args.okf_command == "claim":
        min_use_count = cfg.okf.min_use_count if args.min_use_count is None else max(1, int(args.min_use_count))
        claimed = okf.claim_candidates(
            cfg.state_dir,
            limit=max(1, int(args.limit)),
            min_use_count=min_use_count,
            claim_token=args.claim_token,
        )
        packets = [okf.candidate_packet(row, cfg.state_dir) for row in claimed]
        payload = {
            "success": True,
            "state_dir": str(cfg.state_dir),
            "claim_token": packets[0].get("claim_token") if packets else None,
            "count": len(packets),
            "candidates": packets,
        }
        _emit_payload(payload, json_output=args.json)
        return 0
    if args.okf_command == "validate":
        payload = okf.validate_okf_file(cfg.state_dir, claim_token=args.claim_token, path=args.path)
        payload["success"] = bool(payload["valid"])
        _emit_payload(payload, json_output=args.json)
        return 0 if payload["valid"] else 1
    if args.okf_command == "complete":
        validation = okf.validate_okf_file(cfg.state_dir, claim_token=args.claim_token, path=args.path)
        if validation.get("tool") != args.tool:
            validation.setdefault("errors", []).append("--tool does not match OKF frontmatter tool")
            validation["valid"] = False
        if not validation["valid"]:
            validation["success"] = False
            _emit_payload(validation, json_output=args.json)
            return 1
        marked = okf.mark_candidate_done(cfg.state_dir, tool_name=args.tool, claim_token=args.claim_token, okf_path=args.path)
        payload = {"success": marked, "tool": args.tool, "path": str(args.path), "claim_token": args.claim_token}
        if not marked:
            payload["errors"] = ["no claimed candidate was marked done"]
        _emit_payload(payload, json_output=args.json)
        return 0 if marked else 1
    if args.okf_command == "fail":
        marked = okf.mark_candidate_error(cfg.state_dir, tool_name=args.tool, claim_token=args.claim_token, error=args.error)
        payload = {"success": marked, "tool": args.tool, "claim_token": args.claim_token}
        if not marked:
            payload["errors"] = ["no claimed candidate was marked failed"]
        _emit_payload(payload, json_output=args.json)
        return 0 if marked else 1
    if args.okf_command == "retry":
        retried = okf.retry_error_candidate(cfg.state_dir, tool_name=args.tool)
        payload = {"success": retried, "tool": args.tool}
        if not retried:
            payload["errors"] = ["candidate is missing or not in terminal error state"]
        _emit_payload(payload, json_output=args.json)
        return 0 if retried else 1
    return 1


def _doctor_payload(
    args: argparse.Namespace,
    *,
    build_index_fn=build_index,
    search_index_fn=search_index,
) -> tuple[dict[str, Any], int]:
    cfg = _runtime_config(args.hermes_home)
    db_path = cfg.state_dir / "index.sqlite"
    payload: dict[str, Any] = {
        "success": True,
        "plugin_version": __version__,
        "hermes_home": str(cfg.hermes_home),
        "source_root": str(cfg.source_root),
        "source_root_source": cfg.source_root_source,
        "state_dir": str(cfg.state_dir),
        "state_dir_source": cfg.state_dir_source,
        "include_markdown_docs_source": cfg.include_markdown_docs_source,
        "db_path": str(db_path),
        "warnings": list(cfg.warnings),
        "checks": [],
    }
    errors: list[str] = []

    def check(name: str, ok: bool, detail: str, *, fatal: bool = False) -> None:
        payload["checks"].append({"name": name, "ok": ok, "detail": detail, "fatal": fatal})
        if fatal and not ok:
            errors.append(detail)

    check("hermes_home_exists", cfg.hermes_home.exists(), str(cfg.hermes_home), fatal=True)
    check("source_root_exists", cfg.source_root.exists(), str(cfg.source_root), fatal=True)
    check("state_dir_parent_exists", cfg.state_dir.parent.exists(), str(cfg.state_dir.parent), fatal=False)
    check("index_exists", db_path.exists(), str(db_path), fatal=False)

    bundled_router_skill = _bundled_router_skill_path()
    installed_router_skill = _installed_router_skill_path(cfg.hermes_home)
    router_skill_installed = installed_router_skill.is_file()
    install_command = "hermes local-knowledge install-router-skill"
    check(
        "router_skill_installed",
        router_skill_installed,
        str(installed_router_skill)
        if router_skill_installed
        else f"missing {installed_router_skill}; run `{install_command}`",
        fatal=False,
    )
    router_skill_matches = False
    if router_skill_installed and bundled_router_skill.is_file():
        try:
            router_skill_matches = installed_router_skill.read_bytes() == bundled_router_skill.read_bytes()
        except OSError:
            router_skill_matches = False
    check(
        "router_skill_matches_plugin",
        router_skill_matches,
        "normal router skill matches the bundled plugin version"
        if router_skill_matches
        else (
            "normal router skill differs from the bundled plugin version; "
            f"review it before running `{install_command} --force`"
            if router_skill_installed
            else "cannot compare until the normal router skill is installed"
        ),
        fatal=False,
    )
    check(
        "okf_auto_generate",
        cfg.okf.auto_generate,
        "enabled; automatic OKF generation uses additional model tokens"
        if cfg.okf.auto_generate
        else (
            "disabled; full automatic OKF generation is unavailable. After the user accepts "
            "additional model tokens, run `hermes config set local_knowledge.okf.auto_generate true`"
        ),
        fatal=False,
    )

    if args.rebuild and not errors:
        dirty_tokens = okf.index_dirty_tokens(cfg.state_dir)
        try:
            build_started = time.perf_counter()
            artifacts, edges = _build_index_locked(build_index_fn, cfg)
        except Exception as exc:
            payload["rebuilt"] = False
            check("rebuild_failed", False, f"{type(exc).__name__}: {exc}", fatal=True)
        else:
            payload["rebuilt"] = True
            payload["build_duration_ms"] = int((time.perf_counter() - build_started) * 1000)
            payload["artifact_count"] = len(artifacts)
            payload["artifact_counts_by_type"] = artifact_type_counts(artifacts)
            payload["edge_count"] = len(edges)
            for token in dirty_tokens:
                token.unlink(missing_ok=True)
            check("index_exists_after_rebuild", db_path.exists(), str(db_path), fatal=True)
    else:
        payload["rebuilt"] = False

    query = str(args.query or "").strip()
    if query:
        if errors:
            warning = "smoke query skipped because an earlier doctor check failed"
            payload["warnings"].append(warning)
        elif db_path.exists():
            try:
                rows = search_index_fn(db_path, query, limit=max(1, min(50, int(args.limit))))
            except Exception as exc:
                check("smoke_search_failed", False, f"{type(exc).__name__}: {exc}", fatal=True)
            else:
                payload["smoke_query"] = query
                payload["smoke_result_count"] = len(rows)
                payload["smoke_top_ids"] = [str(row.get("id")) for row in rows[:5]]
        else:
            warning = "smoke query skipped because index.sqlite is missing; rerun with --rebuild"
            payload["warnings"].append(warning)
            check("smoke_query_index_exists", False, warning, fatal=True)

    if errors:
        payload["success"] = False
        payload["errors"] = errors
    payload.update(index_metadata(db_path))
    return payload, 0 if payload["success"] else 1


def _print_doctor(payload: dict[str, Any]) -> None:
    print("local_knowledge doctor")
    print(f"  Hermes home: {payload['hermes_home']}")
    print(f"  Source root: {payload['source_root']} ({payload['source_root_source']})")
    print(f"  State dir:   {payload['state_dir']} ({payload['state_dir_source']})")
    print(f"  Index DB:    {payload['db_path']}")
    for check in payload["checks"]:
        status = "ok" if check["ok"] else "WARN" if not check["fatal"] else "ERROR"
        print(f"  {status}: {check['name']} - {check['detail']}")
    if payload.get("rebuilt"):
        print(f"  Built {payload.get('artifact_count', 0)} artifacts and {payload.get('edge_count', 0)} edges")
    if payload.get("smoke_query"):
        print(
            f"  Smoke query {payload['smoke_query']!r}: "
            f"{payload.get('smoke_result_count', 0)} result(s)"
        )
        if payload.get("smoke_top_ids"):
            print(f"  Top IDs: {', '.join(payload['smoke_top_ids'])}")
    _print_warnings(payload.get("warnings", []))


def main(
    argv: Sequence[str] | None = None,
    *,
    build_index_fn=build_index,
    search_index_fn=search_index,
    get_artifact_fn=get_artifact,
    get_neighbors_fn=get_neighbors,
) -> int:
    args = parse_args(argv)
    if args.command == "install-router-skill":
        cfg = _runtime_config(args.hermes_home)
        payload, status = _install_router_skill_payload(cfg.hermes_home, force=bool(args.force))
        _emit_payload(payload, json_output=bool(args.json))
        return status
    if args.command == "okf":
        return _handle_okf_command(args)
    if args.command == "build":
        cfg = _build_config_from_args(args)
        db_path = cfg.state_dir / "index.sqlite"
        started = time.perf_counter()
        _print_warnings(cfg.warnings)
        dirty_tokens = okf.index_dirty_tokens(cfg.state_dir)
        try:
            artifacts, edges = _build_index_locked(build_index_fn, cfg)
        except Exception as exc:
            message = f"cli_build failed: {type(exc).__name__}: {exc}"
            _record_cli_usage(
                cfg,
                tool="cli_build",
                success=False,
                rebuild_requested=True,
                rebuilt=False,
                error=message,
                latency_ms=int((time.perf_counter() - started) * 1000),
                db_path=db_path,
                index_meta=index_metadata(db_path),
            )
            raise
        build_duration_ms = int((time.perf_counter() - started) * 1000)
        for token in dirty_tokens:
            token.unlink(missing_ok=True)
        counts = artifact_type_counts(artifacts)
        meta = {
            **index_metadata(db_path),
            "artifact_count": len(artifacts),
            "artifact_counts_by_type": counts,
            "edge_count": len(edges),
            "build_duration_ms": build_duration_ms,
        }
        _record_cli_usage(
            cfg,
            tool="cli_build",
            success=True,
            rebuild_requested=True,
            rebuilt=True,
            result_count=len(artifacts),
            latency_ms=build_duration_ms,
            db_path=db_path,
            index_meta=meta,
        )
        print(f"Built {len(artifacts)} artifacts and {len(edges)} edges")
        for artifact_type, count in sorted(counts.items()):
            print(f"  {artifact_type}: {count}")
        print(f"SQLite: {cfg.state_dir / 'index.sqlite'}")
        print(f"JSONL:  {cfg.state_dir / 'index.jsonl'}")
        return 0

    if args.command == "search":
        db_path, warnings, cfg = _db_from_args(args)
        started = time.perf_counter()
        _print_warnings(warnings)
        try:
            _refresh_default_index_if_dirty(
                db_path,
                cfg,
                build_index_fn=build_index_fn,
                explicit_db=args.db is not None,
            )
            rows = search_index_fn(db_path, args.query, limit=args.limit)
        except Exception as exc:
            message = f"cli_search failed: {type(exc).__name__}: {exc}"
            _record_cli_usage(
                cfg,
                tool="knowledge_search",
                success=False,
                query=args.query,
                limit_value=args.limit,
                error=message,
                latency_ms=int((time.perf_counter() - started) * 1000),
                db_path=db_path,
                index_meta=index_metadata(db_path),
            )
            raise
        _record_cli_usage(
            cfg,
            tool="knowledge_search",
            success=True,
            query=args.query,
            limit_value=args.limit,
            result_count=len(rows),
            top_ids=[str(row.get("id")) for row in rows[:5]],
            top_types=[str(row.get("type")) for row in rows[:5]],
            latency_ms=int((time.perf_counter() - started) * 1000),
            db_path=db_path,
            index_meta=index_metadata(db_path),
        )
        if args.json:
            print(json.dumps(rows, ensure_ascii=False, indent=2, sort_keys=True))
        else:
            print_results(rows)
        return 0

    if args.command == "get":
        db_path, warnings, cfg = _db_from_args(args)
        started = time.perf_counter()
        _print_warnings(warnings)
        try:
            _refresh_default_index_if_dirty(
                db_path,
                cfg,
                build_index_fn=build_index_fn,
                explicit_db=args.db is not None,
            )
            row = get_artifact_fn(db_path, args.artifact_id)
        except Exception as exc:
            message = f"cli_get failed: {type(exc).__name__}: {exc}"
            _record_cli_usage(
                cfg,
                tool="knowledge_get",
                success=False,
                artifact_id=args.artifact_id,
                error=message,
                latency_ms=int((time.perf_counter() - started) * 1000),
                db_path=db_path,
                index_meta=index_metadata(db_path),
            )
            raise
        if row is None:
            _record_cli_usage(
                cfg,
                tool="knowledge_get",
                success=False,
                artifact_id=args.artifact_id,
                error=f"Artifact not found: {args.artifact_id}",
                latency_ms=int((time.perf_counter() - started) * 1000),
                db_path=db_path,
                index_meta=index_metadata(db_path),
            )
            print(f"Artifact not found: {args.artifact_id}", file=sys.stderr)
            return 1
        _record_cli_usage(
            cfg,
            tool="knowledge_get",
            success=True,
            artifact_id=args.artifact_id,
            result_count=1,
            latency_ms=int((time.perf_counter() - started) * 1000),
            db_path=db_path,
            index_meta=index_metadata(db_path),
        )
        if args.json:
            print(json.dumps(row, ensure_ascii=False, indent=2, sort_keys=True))
        else:
            print_results([row])
        return 0

    if args.command == "neighbors":
        db_path, warnings, cfg = _db_from_args(args)
        started = time.perf_counter()
        _print_warnings(warnings)
        try:
            _refresh_default_index_if_dirty(
                db_path,
                cfg,
                build_index_fn=build_index_fn,
                explicit_db=args.db is not None,
            )
            rows = get_neighbors_fn(db_path, args.artifact_id)
        except Exception as exc:
            message = f"cli_neighbors failed: {type(exc).__name__}: {exc}"
            _record_cli_usage(
                cfg,
                tool="knowledge_neighbors",
                success=False,
                artifact_id=args.artifact_id,
                error=message,
                latency_ms=int((time.perf_counter() - started) * 1000),
                db_path=db_path,
                index_meta=index_metadata(db_path),
            )
            raise
        _record_cli_usage(
            cfg,
            tool="knowledge_neighbors",
            success=True,
            artifact_id=args.artifact_id,
            result_count=len(rows),
            top_ids=[str(row.get("id")) for row in rows[:5]],
            top_types=[str(row.get("type")) for row in rows[:5]],
            latency_ms=int((time.perf_counter() - started) * 1000),
            db_path=db_path,
            index_meta=index_metadata(db_path),
        )
        if args.json:
            print(json.dumps(rows, ensure_ascii=False, indent=2, sort_keys=True))
        else:
            print_results(rows)
        return 0

    if args.command == "evaluate":
        db_path, warnings, _cfg = _db_from_args(args)
        _print_warnings(warnings)
        usage_db_path = _resolved(args.usage_db) if args.usage_db is not None else db_path.parent / "usage.sqlite"
        report = evaluate_index_against_feedback_report(db_path, usage_db_path)
        metrics = report.as_dict() if args.details else report.metrics.as_dict()
        if args.json:
            print(json.dumps(metrics, ensure_ascii=False, indent=2, sort_keys=True))
        else:
            print("local_knowledge evaluation")
            print(f"  Index DB: {db_path}")
            print(f"  Usage DB: {usage_db_path}")
            for key, value in report.metrics.as_dict().items():
                if isinstance(value, float):
                    print(f"  {key}: {value:.3f}")
                else:
                    print(f"  {key}: {value}")
            if args.details:
                print("  cases:")
                for case in report.cases:
                    exact = case.exact_rank if case.exact_rank is not None else "miss"
                    parent = case.parent_equiv_rank if case.parent_equiv_rank is not None else "miss"
                    expected = ", ".join(case.expected_ids)
                    top_ids = ", ".join(case.top_ids)
                    print(
                        f"    - {case.query}: expected=[{expected}], exact={exact}, parent={parent}, top_ids=[{top_ids}]"
                    )
        return 0

    if args.command in {"doctor", "smoke"}:
        started = time.perf_counter()
        try:
            payload, status = _doctor_payload(
                args,
                build_index_fn=build_index_fn,
                search_index_fn=search_index_fn,
            )
        except Exception as exc:
            payload = {
                "success": False,
                "errors": [f"doctor failed: {type(exc).__name__}: {exc}"],
                "warnings": [],
                "checks": [],
            }
            status = 1
        doctor_db_path = Path(str(payload["db_path"])) if payload.get("db_path") else None
        doctor_usage_db_path = (
            Path(str(payload["state_dir"])) / "usage.sqlite" if payload.get("state_dir") else None
        )
        _record_cli_usage(
            None,
            tool="cli_doctor",
            success=status == 0,
            query=str(args.query or ""),
            rebuild_requested=bool(args.rebuild),
            rebuilt=bool(payload.get("rebuilt")) if "rebuilt" in payload else None,
            error="; ".join(str(item) for item in payload.get("errors", [])),
            result_count=payload.get("smoke_result_count"),
            top_ids=[str(item) for item in payload.get("smoke_top_ids", [])],
            latency_ms=int((time.perf_counter() - started) * 1000),
            db_path=doctor_db_path,
            index_meta=payload,
            usage_db_path=doctor_usage_db_path,
        )
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        elif "hermes_home" in payload:
            _print_doctor(payload)
            for error in payload.get("errors", []):
                print(f"ERROR: {error}", file=sys.stderr)
        else:
            for error in payload.get("errors", ["doctor failed"]):
                print(f"ERROR: {error}", file=sys.stderr)
        return status

    return 1


if __name__ == "__main__":
    raise SystemExit(main())

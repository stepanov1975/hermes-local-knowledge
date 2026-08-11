"""Application service for managed local-knowledge indexes and telemetry."""

from __future__ import annotations

import time
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import __version__, index
from .artifacts import Artifact, Edge
from .config import Config
from .evaluation import SearchEvaluationReport, evaluate_index_against_feedback_report
from .routing import (
    ROUTING_TRACE_METADATA_KEY,
    SearchRoutingTrace,
    apply_feedback_route,
    decide_feedback_route,
)
from .telemetry import _record_feedback, _record_usage, _usage_report

BuildIndexFn = Callable[..., tuple[list[Artifact], list[Edge]] | None]
SearchIndexFn = Callable[..., list[dict[str, Any]]]
GetArtifactFn = Callable[..., dict[str, Any] | None]
GetNeighborsFn = Callable[..., list[dict[str, Any]]]
IndexMetadataFn = Callable[[Path], dict[str, Any]]
IndexSourceRootFn = Callable[[Path], str | None]
RecordUsageFn = Callable[..., int | None]
RecordFeedbackFn = Callable[..., tuple[int, int]]
UsageReportFn = Callable[..., dict[str, Any]]
EvaluateFn = Callable[[Path, Path], SearchEvaluationReport]


class LocalKnowledgeService:
    """Own one resolved configuration and its managed index lifecycle."""

    def __init__(
        self,
        config: Config,
        *,
        build_index_fn: BuildIndexFn | None = None,
        search_index_fn: SearchIndexFn | None = None,
        get_artifact_fn: GetArtifactFn | None = None,
        get_neighbors_fn: GetNeighborsFn | None = None,
        index_metadata_fn: IndexMetadataFn | None = None,
        index_source_root_fn: IndexSourceRootFn | None = None,
        record_usage_fn: RecordUsageFn | None = None,
        record_feedback_fn: RecordFeedbackFn | None = None,
        usage_report_fn: UsageReportFn | None = None,
        evaluate_fn: EvaluateFn | None = None,
    ) -> None:
        self.config = config
        self._build_index_fn = build_index_fn or index.build_index
        self._search_index_fn = search_index_fn or index.search_index
        self._get_artifact_fn = get_artifact_fn or index.get_artifact
        self._get_neighbors_fn = get_neighbors_fn or index.get_neighbors
        self._index_metadata_fn = index_metadata_fn or index.index_metadata
        self._index_source_root_fn = index_source_root_fn or index.index_source_root
        self._record_usage_fn = record_usage_fn or _record_usage
        self._record_feedback_fn = record_feedback_fn or _record_feedback
        self._usage_report_fn = usage_report_fn or _usage_report
        self._evaluate_fn = evaluate_fn or evaluate_index_against_feedback_report

    @property
    def db_path(self) -> Path:
        return self.config.state_dir / "index.sqlite"

    @property
    def usage_db_path(self) -> Path:
        return self.config.state_dir / "usage.sqlite"

    def _base_metadata(self, db_path: Path) -> dict[str, Any]:
        return {
            "plugin_version": __version__,
            "root": str(self.config.source_root),
            "source_root_source": self.config.source_root_source,
            "state_dir": str(self.config.state_dir),
            "state_dir_source": self.config.state_dir_source,
            "include_markdown_docs_source": self.config.include_markdown_docs_source,
            "db_path": str(db_path),
            "warnings": list(self.config.warnings),
            "rebuilt": False,
            "expected_index_format_version": index.INDEX_FORMAT_VERSION,
        }

    def _metadata(self, db_path: Path) -> dict[str, Any]:
        metadata = self._base_metadata(db_path)
        metadata.update(self._index_metadata_fn(db_path))
        return metadata

    def _build(
        self,
        *,
        force: bool,
    ) -> tuple[tuple[list[Artifact], list[Edge]] | None, dict[str, Any]]:
        started = time.perf_counter()
        build_result = self._build_index_fn(
            self.config.source_root,
            self.config.state_dir,
            self.config.hermes_home,
            self.config.index_settings,
            force=force,
        )
        if force and build_result is None:
            raise RuntimeError("forced index rebuild returned no result")

        metadata = self._base_metadata(self.db_path)
        if build_result is not None:
            artifacts, edges = build_result
            metadata.update(
                {
                    "rebuilt": True,
                    "build_duration_ms": int((time.perf_counter() - started) * 1000),
                    "artifact_count": len(artifacts),
                    "artifact_counts_by_type": index.artifact_type_counts(artifacts),
                    "edge_count": len(edges),
                }
            )
        metadata.update(self._index_metadata_fn(self.db_path))
        return build_result, metadata

    def ensure_index(self) -> tuple[Path, dict[str, Any]]:
        """Ensure the managed index through the format-4 builder."""

        _build_result, metadata = self._build(force=False)
        return self.db_path, metadata

    def rebuild(self) -> tuple[list[Artifact], list[Edge], dict[str, Any]]:
        """Force one managed format-4 index build."""

        build_result, metadata = self._build(force=True)
        if build_result is None:  # Kept explicit for type narrowing and injected builders.
            raise RuntimeError("forced index rebuild returned no result")
        artifacts, edges = build_result
        return artifacts, edges, metadata

    def _prepare_query(
        self,
        *,
        rebuild: bool,
        db_path: Path | None,
        ensure: bool,
    ) -> tuple[Path, dict[str, Any]]:
        target = db_path if db_path is not None else self.db_path
        if db_path is not None or not ensure:
            return target, self._metadata(target)
        if rebuild:
            _artifacts, _edges, metadata = self.rebuild()
            return target, metadata
        return self.ensure_index()

    def search(
        self,
        query: str,
        *,
        limit: int,
        artifact_type: str | None = None,
        rebuild: bool = False,
        db_path: Path | None = None,
        ensure: bool = True,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        target, metadata = self._prepare_query(
            rebuild=rebuild,
            db_path=db_path,
            ensure=ensure,
        )
        baseline_rows = self._search_index_fn(
            target,
            query,
            limit=limit,
            artifact_type=artifact_type,
        )
        index_source_root = self._index_source_root_fn(target)
        if db_path is None and index_source_root == str(self.config.source_root):
            decision = decide_feedback_route(
                baseline_rows,
                usage_db_path=self.usage_db_path,
                root=self.config.source_root,
                query=query,
                artifact_type=artifact_type,
                db_path=target,
                limit=limit,
                search_index_fn=self._search_index_fn,
                min_confirmations=self.config.implicit_feedback.min_confirmations,
                max_generic_queries=self.config.implicit_feedback.max_generic_queries,
            )
        else:
            decision = apply_feedback_route(
                baseline_rows,
                route=None,
                feedback_max_id=None,
                db_path=target,
                limit=limit,
                search_index_fn=self._search_index_fn,
            )
        metadata[ROUTING_TRACE_METADATA_KEY] = SearchRoutingTrace(
            baseline_ids=tuple(str(row.get("id")) for row in baseline_rows),
            decision=decision,
        )
        return decision.rows, metadata

    def get(
        self,
        artifact_id: str,
        *,
        rebuild: bool = False,
        db_path: Path | None = None,
        ensure: bool = True,
    ) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        target, metadata = self._prepare_query(
            rebuild=rebuild,
            db_path=db_path,
            ensure=ensure,
        )
        return self._get_artifact_fn(target, artifact_id), metadata

    def neighbors(
        self,
        artifact_id: str,
        *,
        rebuild: bool = False,
        db_path: Path | None = None,
        ensure: bool = True,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        target, metadata = self._prepare_query(
            rebuild=rebuild,
            db_path=db_path,
            ensure=ensure,
        )
        return self._get_neighbors_fn(target, artifact_id), metadata

    def record_usage(self, *, tool: str, success: bool, **kwargs: Any) -> int | None:
        """Record lookup telemetry without allowing telemetry failures to escape."""

        usage_kwargs = dict(kwargs)
        usage_kwargs["usage_db_path"] = self.usage_db_path
        try:
            return self._record_usage_fn(
                self.config.source_root,
                tool=tool,
                success=success,
                **usage_kwargs,
            )
        except Exception:
            return None

    def feedback(
        self,
        *,
        rating: str,
        event_id: int | None,
        query: str,
        artifact_id: str,
        note: str,
        context: dict[str, str],
        expected_artifact_id: str = "",
        resolves_feedback_id: int | None = None,
        usage_started_at: float | None = None,
    ) -> tuple[int, int]:
        """Record strict feedback and its success event in one transaction."""

        artifact_exists_fn: Callable[[str], bool] | None = None
        if expected_artifact_id or resolves_feedback_id is not None:
            target, _metadata = self.ensure_index()

            def artifact_exists(candidate_id: str) -> bool:
                return self._get_artifact_fn(target, candidate_id) is not None

            artifact_exists_fn = artifact_exists

        return self._record_feedback_fn(
            self.config.source_root,
            rating=rating,
            event_id=event_id,
            query=query,
            artifact_id=artifact_id,
            note=note,
            context=context,
            expected_artifact_id=expected_artifact_id,
            resolves_feedback_id=resolves_feedback_id,
            artifact_exists=artifact_exists_fn,
            usage_started_at=usage_started_at,
            usage_db_path=self.usage_db_path,
        )

    def _enrich_usage_report_search_issues(
        self,
        report: dict[str, Any],
        *,
        limit: int,
    ) -> dict[str, Any]:
        """Re-check bounded live search issues without rebuilding or recording telemetry."""

        raw_candidates = report.get("search_issue_candidates")
        if raw_candidates is None:
            return report
        if not isinstance(raw_candidates, list):
            raw_candidates = []
        candidates = [row for row in raw_candidates[:limit] if isinstance(row, dict)]
        preserved_candidates = [
            row
            for row in report.get("improvement_candidates", [])
            if isinstance(row, dict) and not str(row.get("type") or "").startswith("feedback_")
        ]
        current_target: list[dict[str, Any]] = []
        without_current_target: list[dict[str, Any]] = []
        behaviorally_resolved: list[dict[str, Any]] = []
        correction_candidates: list[dict[str, Any]] = []
        active_feedback_candidates: list[dict[str, Any]] = []

        index_available = False
        try:
            index_available = (
                self.db_path.is_file()
                and self._index_source_root_fn(self.db_path) == str(self.config.source_root)
            )
        except (OSError, RuntimeError, sqlite3.Error, ValueError):
            index_available = False

        for source_row in candidates:
            row = dict(source_row)
            expected_id = str(row.get("expected_artifact_id") or "").strip()
            query = str(row.get("effective_query") or row.get("query") or "").strip()
            artifact_type = str(row.get("artifact_type") or "").strip() or None
            row.update(
                {
                    "current_replay_status": "unavailable",
                    "current_top_artifact_id": None,
                    "expected_artifact_current": None,
                }
            )
            if index_available and query:
                try:
                    expected_current = bool(
                        expected_id and self._get_artifact_fn(self.db_path, expected_id) is not None
                    )
                    current_rows = self._search_index_fn(
                        self.db_path,
                        query,
                        artifact_type=artifact_type,
                        limit=1,
                    )
                    current_top_id = (
                        str(current_rows[0].get("id") or "") if current_rows else None
                    )
                    row["expected_artifact_current"] = expected_current
                    row["current_top_artifact_id"] = current_top_id
                    row["current_replay_status"] = "checked"
                except (OSError, RuntimeError, sqlite3.Error, ValueError):
                    pass

            expected_current = row["expected_artifact_current"] is True
            is_rank_one = expected_current and row["current_top_artifact_id"] == expected_id
            if is_rank_one:
                row["current_replay_status"] = "behaviorally_resolved"
                behaviorally_resolved.append(row)
                continue
            if expected_current:
                current_target.append(row)
            else:
                without_current_target.append(row)

            if (
                expected_current
                and row.get("linkage_quality") == "verified_event"
                and row["current_replay_status"] == "checked"
            ):
                row["candidate_kind"] = "correction_candidate"
                row["type"] = "correction_candidate"
                correction_candidates.append(row)
            else:
                row["candidate_kind"] = (
                    "current_state_unavailable"
                    if row["current_replay_status"] == "unavailable"
                    else "coverage_or_ranking_triage"
                )
                row["type"] = f"feedback_{row.get('rating', 'negative')}"
            active_feedback_candidates.append(row)

        report["unresolved_negative_with_current_expected_target"] = current_target
        report["unresolved_negative_without_current_expected_target"] = without_current_target
        report["behaviorally_resolved_negative_feedback"] = behaviorally_resolved
        report["correction_candidates"] = correction_candidates
        report["improvement_candidates"] = (
            preserved_candidates + active_feedback_candidates
        )[:limit]
        report.pop("search_issue_candidates", None)
        return report

    def usage_report(self, *, days: int, limit: int) -> dict[str, Any]:
        report = self._usage_report_fn(
            self.config.source_root,
            days=days,
            limit=limit,
            usage_db_path=self.usage_db_path,
        )
        return self._enrich_usage_report_search_issues(report, limit=limit)

    def evaluate(
        self,
        *,
        db_path: Path | None = None,
        usage_db_path: Path | None = None,
    ) -> SearchEvaluationReport:
        """Evaluate labels read-only without emitting usage telemetry."""

        return self._evaluate_fn(
            db_path if db_path is not None else self.db_path,
            usage_db_path if usage_db_path is not None else self.usage_db_path,
        )

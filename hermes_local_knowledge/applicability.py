"""Exact cached cancellation of explicit promotions; no model work during search."""
from __future__ import annotations

import hashlib
import json
import time
from contextlib import closing
from dataclasses import replace
from typing import Any

from . import shadow
from .config import Config
from .routing import RouteDecision, RouteOutcome
from .shadow_sources import Evidence, checked_citations, identity, sources_current

PROMOTION_CONTRACT = 1


def binding(cfg: Config, *, request: str, query: str, artifact_type: str,
            limit: int, lookup: Any, baseline: list[dict[str, Any]],
            decision: RouteDecision) -> dict[str, Any]:
    shadow._lookup_context(lookup)  # Validate native field bounds without normalizing exact binding.
    ids = [str(row["id"]) for row in baseline]
    promoted = next(row for row in decision.rows if row["id"] == decision.artifact_id)
    return {"version": PROMOTION_CONTRACT, "user_request": request, "query": query,
            "artifact_type": artifact_type, "limit": limit,
            "lookup": lookup if lookup is not None else {},
            "namespace": [str(cfg.hermes_home.resolve()), str(cfg.source_root.resolve()),
                          str(cfg.state_dir.resolve())],
            "baseline_ids": ids, "baseline_fingerprint": _page_fingerprint(baseline),
            "feedback_id": decision.feedback_id, "artifact_id": decision.artifact_id,
            "promoted_fingerprint": _page_fingerprint([promoted])}


def _page_fingerprint(rows: list[dict[str, Any]]) -> str:
    packet = [[row["id"], [row.get(key) for key in ("type", "title", "path", "summary")]] for row in rows]
    return hashlib.sha256(json.dumps(packet, ensure_ascii=False).encode()).hexdigest()


def case_key(promotion: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(promotion, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def current_binding(cfg: Config, promotion: dict[str, Any]) -> bool:
    return (promotion["baseline_fingerprint"] == shadow._baseline_fingerprint(cfg, promotion["baseline_ids"])
            and promotion["promoted_fingerprint"] == shadow._baseline_fingerprint(cfg, [promotion["artifact_id"]]))


def valid_result(cfg: Config, result: dict[str, Any], promotion: dict[str, Any],
                 verified_at: float) -> bool:
    try:
        return _valid_result(cfg, result, promotion, verified_at)
    except Exception:
        return False


def _valid_result(cfg: Config, result: dict[str, Any], promotion: dict[str, Any],
                  verified_at: float) -> bool:
    if (result.get("provenance") != "ai_promotion_applicability"
            or result.get("promotion") != promotion
            or result.get("contract_version") != shadow.VERIFICATION_CONTRACT
            or result.get("verdict") not in {"applicable", "inapplicable", "uncertain"}
            or not 0 <= time.time() - verified_at <= cfg.verified_routing.max_age_days * 86400
            or not current_binding(cfg, promotion)):
        return False
    # Uncertainty is a completed non-veto, not negative evidence.
    if result["verdict"] == "uncertain":
        return True
    receipts = result.get("sources", [])
    if not sources_current(cfg, receipts):
        return False
    evidence = Evidence(cfg, promotion["artifact_type"])
    evidence.include([item["id"] for item in receipts])
    for receipt in receipts:
        evidence.read(receipt["id"], receipt=receipt)
    checked_citations(result.get("citations"), evidence.sources, [promotion["artifact_id"]])
    return (result["verdict"] != "inapplicable"
            or result.get("basis") == "scope_target_incompatibility")


def veto(cfg: Config, promotion: dict[str, Any]) -> bool:
    """Read only the exact receipt; errors/legacy/mismatch preserve incumbent results."""
    try:
        with closing(shadow._connect(cfg)) as conn:
            row = conn.execute("SELECT status,result,verified_at FROM cases WHERE id=?",
                               (case_key(promotion),)).fetchone()
        if row is None or row["status"] != "ai_verified":
            return False
        result = json.loads(row["result"])
        return valid_result(cfg, result, promotion, row["verified_at"]) and result["verdict"] == "inapplicable"
    except Exception:
        return False


def apply(cfg: Config, baseline: list[dict[str, Any]], decision: RouteDecision,
          *, query: str, artifact_type: str, limit: int) -> RouteDecision:
    if (cfg.verified_routing.mode != "veto" or decision.feedback_id is None
            or decision.outcome not in {RouteOutcome.PROMOTED_EXISTING, RouteOutcome.PROMOTED_RETRY}):
        return decision
    from .shadow_hooks import active_scope

    scope = active_scope(cfg)
    if scope is None:
        return decision
    try:
        promotion = binding(cfg, request=scope.request.text, query=query, artifact_type=artifact_type,
                            limit=limit, lookup=scope.lookup, baseline=baseline, decision=decision)
        if not current_binding(cfg, promotion):
            return decision
        scope.promotion = promotion  # Observer captures/requeues after downstream completion.
        if veto(cfg, promotion) and active_scope(cfg) is scope:
            return replace(decision, rows=baseline, outcome=RouteOutcome.APPLICABILITY_VETOED)
    except Exception:
        pass
    return decision


_INSTRUCTIONS = (
    shadow._CONTEXT +
    "Judge ONLY the selected explicit promotion's SOURCE APPLICABILITY. Do not propose a route, "
    "rank alternatives, or review baseline coverage. Metadata and baseline_review.not_useful are NOT "
    "rejection evidence. Historical requests and harmless edits can remain applicable. "
    "The selected source is read when available; for larger files use {action:'locate_source',id:'...',"
    "query:'literal substring'} then {action:'read_excerpt',id:'...',start_line:1,end_line:80}. "
    "Only the selected artifact may be read. Excerpts never establish absence. "
    "Return {verdict:'applicable'|'inapplicable'|'uncertain',basis:'scope_target_incompatibility'|"
    "'supported'|'insufficient_evidence',citations:[...]}. Inapplicable requires READ source citations "
    "positively establishing incompatible target/scope, not inferred missing facts or mere recency. "
    "Unread/unavailable/ambiguous evidence is uncertain. " + shadow._CITATIONS
)


def review(cfg: Config, *, llm: Any, row: dict[str, Any], owner: str,
           deadline: float, task: dict[str, Any]) -> dict[str, Any]:
    promotion = task["lookup_context"]["promotion"]
    if promotion.get("version") != PROMOTION_CONTRACT or not current_binding(cfg, promotion):
        raise ValueError("promotion_changed")
    artifact_id = promotion["artifact_id"]
    evidence = Evidence(cfg, row["artifact_type"], diagnostics=row["_diagnostics"], phase="applicability")
    evidence.include([artifact_id])
    evidence.try_read(artifact_id)
    for _ in range(cfg.verified_routing.max_model_calls_per_case):
        parsed = shadow._call(cfg, llm=llm, row=row, owner=owner, deadline=deadline,
                              stage="applicability", instructions=_INSTRUCTIONS,
                              packet={**task, **evidence.packet()})
        if not isinstance(parsed, dict):
            raise ValueError("invalid_applicability")
        action = parsed.get("action")
        if action in {"locate_source", "read_excerpt"}:
            if parsed.get("id") != artifact_id:
                raise ValueError("invalid_read_request")
            if action == "locate_source":
                query = parsed.get("query")
                if not isinstance(query, str):
                    raise ValueError("invalid_location_query")
                evidence.locate(artifact_id, query)
            else:
                start, end = parsed.get("start_line"), parsed.get("end_line")
                if type(start) is not int or type(end) is not int:
                    raise ValueError("invalid_read_range")
                evidence.try_read(artifact_id, start_line=start, end_line=end)
            continue
        verdict = parsed.get("verdict")
        if verdict not in {"applicable", "inapplicable", "uncertain"}:
            raise ValueError("invalid_applicability")
        citations = []
        if verdict != "uncertain":
            citations = checked_citations(parsed.get("citations"), evidence.sources, [artifact_id])
        if verdict == "inapplicable" and parsed.get("basis") != "scope_target_incompatibility":
            raise ValueError("insufficient_evidence")
        receipts = [identity(source) for source in evidence.sources.values()]
        if (receipts and not sources_current(cfg, receipts)) or not current_binding(cfg, promotion):
            raise ValueError("source_changed_during_verification")
        return {"provenance": "ai_promotion_applicability", "contract_version": shadow.VERIFICATION_CONTRACT,
                "promotion": promotion, "verdict": verdict, "basis": parsed.get("basis"),
                "citations": citations, "sources": receipts}
    raise ValueError("call_budget")

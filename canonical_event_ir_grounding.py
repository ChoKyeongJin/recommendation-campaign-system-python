"""Project GraphRAG context onto the approved Canonical Event IR vocabulary.

Graph retrieval may help a structuring model recognize which *already approved*
catalog symbols are relevant.  It may not introduce a source, field, value, SQL
fragment, or physical binding.  This module is deliberately a pure projection:
the caller supplies a resolved audience catalog and the returned prompt payload
contains canonical identifiers only.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

import audience_authority
import audience_runtime
import event_compiler
import event_ir
import plan_schema
from query_structurer import CampaignQueryPlanV4, validate_campaign_query_plan_v4
from query_structurer.semantic_ir import empty_semantic_ir


def _fold(value: Any) -> str:
    return "".join(str(value or "").split()).casefold()


def _contains_term(text: str, terms: Iterable[Any]) -> bool:
    folded = _fold(text)
    return any(_fold(term) and _fold(term) in folded for term in terms)


def _query_mentions_term(text: str, term: Any) -> bool:
    """Match a catalog alias without treating a syllable inside another word as evidence."""

    raw = str(term or "").strip()
    if not raw:
        return False
    # ``세`` is a useful age suffix (``30~44세``), but ordinary substring
    # matching also finds it in ``보여주세요``.  A one-syllable Hangul alias
    # therefore needs either numeric attachment or an actual token boundary.
    if len(raw) == 1 and "가" <= raw <= "힣":
        if raw == "세" and re.search(rf"\d\s*{re.escape(raw)}", text):
            return True
        return bool(
            re.search(
                rf"(?<![0-9A-Za-z가-힣]){re.escape(raw)}(?![0-9A-Za-z가-힣])",
                text,
                flags=re.IGNORECASE,
            )
        )
    if raw.isascii() and any(character.isalnum() for character in raw):
        return bool(
            re.search(
                rf"(?<![0-9A-Za-z_]){re.escape(raw)}(?![0-9A-Za-z_])",
                text,
                flags=re.IGNORECASE,
            )
        )
    return _fold(raw) in _fold(text)


def _query_mentions_any(text: str, terms: Iterable[Any]) -> bool:
    return any(_query_mentions_term(text, term) for term in terms)


def _node_text(node: Mapping[str, Any]) -> str:
    """Flatten the two GraphRAG node shapes for matching only.

    Raw text is never returned by :func:`project_canonical_event_ir_grounding`.
    In particular, SQL examples and schema columns can support a reverse lookup
    through the approved catalog, but cannot cross the projection boundary.
    """

    payload = node.get("payload")
    parts: list[str] = []
    for source in (node, payload if isinstance(payload, Mapping) else {}):
        for key in (
            "id",
            "type",
            "title",
            "name",
            "table_name",
            "text_for_embedding",
            "description",
            "text",
            "sql",
        ):
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                parts.append(value.strip())
    return "\n".join(parts)


def _source_table(
    source_id: str,
    catalog: Mapping[str, Any],
) -> str:
    if source_id == "subject":
        subject = catalog.get("subject")
        return str(subject.get("table") or "") if isinstance(subject, Mapping) else ""
    sources = catalog.get("sources")
    source = sources.get(source_id) if isinstance(sources, Mapping) else None
    return str(source.get("table") or "") if isinstance(source, Mapping) else ""


def _value_terms(declaration: Any) -> list[str]:
    if not isinstance(declaration, Mapping):
        return []
    aliases = declaration.get("aliases")
    return [str(item) for item in aliases] if isinstance(aliases, list) else []


_TARGETING_INTENTS = frozenset({"recommend_campaign", "find_user_segment"})


def is_registry_gap_repair_candidate(plan: Mapping[str, Any]) -> bool:
    """Whether one canonical-only grounding retry is allowed.

    A Graph lookup must never turn a genuine user omission, ambiguity, or
    unsupported capability into an executable plan.  The retry is therefore
    limited to the application-owned registry-gap state produced when every
    model issue was ``unsupported_semantics`` but declared execution assets
    contradicted that claim.
    """

    requirement = plan.get("audience_requirement")
    semantic_ir = plan.get("semantic_ir")
    issues = requirement.get("issues") if isinstance(requirement, Mapping) else None
    assets = plan.get("audience_execution_assets")
    event_expression = plan.get("event_expression")
    return bool(
        plan.get("intent") in _TARGETING_INTENTS
        and isinstance(requirement, Mapping)
        and requirement.get("expression") is None
        and isinstance(issues, Sequence)
        and not isinstance(issues, (str, bytes, bytearray))
        and len(issues) > 0
        and all(
            isinstance(issue, Mapping)
            and issue.get("code") == "unsupported_semantics"
            for issue in issues
        )
        and isinstance(semantic_ir, Mapping)
        and semantic_ir.get("status") == "needs_clarification"
        and semantic_ir.get("failure_kind") == "system_failure"
        and isinstance(assets, Sequence)
        and not isinstance(assets, (str, bytes, bytearray))
        and len(assets) > 0
        and all(isinstance(item, Mapping) and item.get("assets") for item in assets)
        and not isinstance(event_expression, Mapping)
        and plan.get("audience_authority") != "legacy"
        and has_empty_legacy_audience_surface(plan)
    )


def has_empty_legacy_audience_surface(plan: Mapping[str, Any]) -> bool:
    """Reject a repaired plan that also populated a second audience language.

    표면 목록은 :mod:`plan_schema` 가 소유한다(2026-08-04) — 여기 리터럴로 적혀 있던 동안
    같은 목록이 저장소에 둘이었고(다른 하나는 ``plan_validation`` 의 hybrid 가드), 한쪽만
    넓히면 "무엇이 두 번째 오디언스 언어인가"의 답이 호출 지점마다 달라진다.

    ``semantic_plan.nodes`` 만 여기서 직접 본다. 그 키는 canonical 레인에 **상시 존재**해서
    분류표에서는 ``audience=False`` 여야 하고(True 면 모든 canonical 요청이 충돌로 죽는다),
    "노드가 비었는가"는 키의 존재가 아니라 이 술어만의 판정이다.
    """

    def empty(value: Any) -> bool:
        if isinstance(value, Mapping):
            return all(empty(item) for item in value.values())
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            return all(empty(item) for item in value)
        return value in (None, "")

    semantic_plan = plan.get("semantic_plan")
    semantic_nodes = (
        semantic_plan.get("nodes") if isinstance(semantic_plan, Mapping) else None
    )
    surfaces = (*plan_schema.AUDIENCE_CONTAINERS, *sorted(plan_schema.audience_keys()))
    return bool(
        all(empty(plan.get(name)) for name in surfaces)
        and empty(semantic_nodes)
    )


def project_canonical_event_ir_grounding(
    query: str,
    context_nodes: Iterable[Mapping[str, Any]],
    catalog: Mapping[str, Any],
    *,
    allowed_fields: Iterable[str],
    allowed_sources: Iterable[str],
) -> dict[str, Any]:
    """Return only catalog/compiler-approved symbols supported by Graph context.

    A symbol needs both sides of evidence:

    * the user query mentions the catalog label, alias, or canonical value; and
    * a retrieved node mentions the catalog-owned physical binding.

    The physical side is used solely for the reverse lookup and is discarded.
    Consequently an unknown Graph column or a plausible SQL example can never
    expand runtime capability.
    """

    nodes = [node for node in context_nodes if isinstance(node, Mapping)]
    node_rows = [
        (str(node.get("id") or ""), _node_text(node))
        for node in nodes
    ]
    permitted_fields = {str(item) for item in allowed_fields}
    permitted_sources = {str(item) for item in allowed_sources}
    fields = catalog.get("fields")
    fields = fields if isinstance(fields, Mapping) else {}
    domains = catalog.get("value_domains")
    domains = domains if isinstance(domains, Mapping) else {}

    matched_fields: list[str] = []
    matched_values: dict[str, list[str]] = {}
    provenance: set[str] = set()
    for field_id, raw_declaration in fields.items():
        field_id = str(field_id)
        if field_id not in permitted_fields or not isinstance(raw_declaration, Mapping):
            continue
        source_id = str(raw_declaration.get("source") or field_id.partition(".")[0])
        if source_id != "subject" and source_id not in permitted_sources:
            continue
        table = _source_table(source_id, catalog)
        column = str(raw_declaration.get("column") or "")
        if not table or not column:
            continue

        domain_id = raw_declaration.get("value_domain")
        domain = domains.get(domain_id) if isinstance(domain_id, str) else None
        values = domain.get("values") if isinstance(domain, Mapping) else None
        field_terms = [
            field_id,
            str(raw_declaration.get("label") or ""),
            *(
                [str(item) for item in raw_declaration.get("aliases")]
                if isinstance(raw_declaration.get("aliases"), list)
                else []
            ),
        ]
        value_hits: list[str] = []
        if isinstance(values, Mapping):
            for canonical, value_declaration in values.items():
                terms = [str(canonical), *_value_terms(value_declaration)]
                if _query_mentions_any(query, terms):
                    value_hits.append(str(canonical))
        if not _query_mentions_any(query, field_terms):
            continue

        supporting = [
            node_id
            for node_id, text in node_rows
            if _contains_term(text, (table,)) and _contains_term(text, (column,))
        ]
        if not supporting:
            continue
        matched_fields.append(field_id)
        if value_hits:
            matched_values[field_id] = sorted(set(value_hits))
        provenance.update(node_id for node_id in supporting if node_id)

    sources = catalog.get("sources")
    sources = sources if isinstance(sources, Mapping) else {}
    matched_sources: list[str] = []
    for source_id, declaration in sources.items():
        source_id = str(source_id)
        if source_id not in permitted_sources or not isinstance(declaration, Mapping):
            continue
        terms = [
            source_id,
            str(declaration.get("label") or ""),
            *(
                [str(item) for item in declaration.get("aliases")]
                if isinstance(declaration.get("aliases"), list)
                else []
            ),
        ]
        if not _query_mentions_any(query, terms):
            continue
        table = str(declaration.get("table") or "")
        time_column = str(declaration.get("time_column") or "")
        supporting = [
            node_id
            for node_id, text in node_rows
            if table
            and _contains_term(text, (table,))
            and (not time_column or _contains_term(text, (time_column,)))
        ]
        if not supporting:
            continue
        matched_sources.append(source_id)
        provenance.update(node_id for node_id in supporting if node_id)

    # A matched measure can require catalog-declared structural fields that are
    # not themselves user predicates.  For example, a ranked measure needs its
    # entity key to group and semi-join the selected rows.  Those dependencies
    # are admitted only when the trusted relation recipe names them and every
    # symbol is already present in the compiler allowlists; Graph text cannot
    # invent or widen either set.
    projected_fields = set(matched_fields)
    projected_sources = set(matched_sources)
    recipes = catalog.get("relation_recipes")
    if isinstance(recipes, Mapping):
        for recipe in recipes.values():
            metrics = recipe.get("metrics") if isinstance(recipe, Mapping) else None
            if not isinstance(metrics, Mapping):
                continue
            for declaration in metrics.values():
                if not isinstance(declaration, Mapping):
                    continue
                measure_field = str(declaration.get("measure_field") or "")
                if measure_field not in projected_fields:
                    continue
                source_id = str(declaration.get("source") or "")
                entity_field = str(declaration.get("entity_field") or "")
                if source_id in permitted_sources:
                    projected_sources.add(source_id)
                if entity_field in permitted_fields:
                    projected_fields.add(entity_field)

    return {
        "canonical_fields": sorted(projected_fields),
        "canonical_sources": sorted(projected_sources),
        "canonical_values": {
            field_id: matched_values[field_id]
            for field_id in sorted(matched_values)
        },
        # Provenance is audit-only.  The prompt renderer below intentionally
        # omits it because Graph IDs commonly contain physical column names.
        "provenance_node_ids": sorted(provenance),
    }


def render_canonical_event_ir_grounding_instruction(
    projection: Mapping[str, Any],
) -> str | None:
    """Render a non-authoritative, canonical-only retry instruction."""

    fields = [str(item) for item in projection.get("canonical_fields") or []]
    sources = [str(item) for item in projection.get("canonical_sources") or []]
    values_raw = projection.get("canonical_values")
    values = values_raw if isinstance(values_raw, Mapping) else {}
    if not fields and not sources:
        return None
    safe_payload = {
        "canonical_fields": fields,
        "canonical_sources": sources,
        "canonical_values": {
            str(field_id): [str(value) for value in field_values]
            for field_id, field_values in values.items()
            if isinstance(field_values, list)
        },
    }
    return "\n".join(
        [
            "[GraphRAG Canonical Event IR grounding]",
            "아래 항목은 원문을 다시 읽을 때 참고하는 비권위적 후보입니다.",
            "Audience Catalog와 Event IR compiler에 이미 승인된 canonical ID만 나열되어 있습니다.",
            "원문의 정확한 evidence span이 있는 조건만 사용하고, 후보라는 이유로 조건을 추가하지 마세요.",
            "SQL, 물리 테이블/컬럼, target_user, exclude, targeting IR을 만들지 마세요.",
            "최종 실행 조건은 audience_requirement.expression 하나의 Canonical Event IR로 완전하게 작성하세요.",
            "순위 semi Join의 left는 별도 member/subject Source가 아니라 canonical_sources의 metric Source를 correlation 생략으로 재사용하고, on 양쪽도 같은 canonical member-id FieldRef를 사용하세요.",
            json.dumps(safe_payload, ensure_ascii=False, sort_keys=True),
        ]
    )


@dataclass(frozen=True)
class GroundingServices:
    """Graph/LLM adapters kept outside the canonical admission boundary."""

    schema_retrieval_query: Any
    vector_search: Any
    keyword_search: Any
    merge_hits: Any
    expand_context: Any
    structure_plan: Any
    repair_model: Any
    write_log: Any


def admit_grounded_canonical_event_ir_repair(
    original: Mapping[str, Any],
    candidate: Any,
    *,
    projection: Mapping[str, Any],
    query: str,
    current_date: str | None,
) -> tuple[CampaignQueryPlanV4 | None, str]:
    """Admit a complete replacement plan, never a field-level Graph patch."""

    try:
        validated = validate_campaign_query_plan_v4(
            candidate,
            query=query,
            raw_query=query,
            require_semantic=True,
        )
    except Exception as exc:  # noqa: BLE001 - admission is fail-closed.
        return None, f"campaign_plan_validation_failed:{exc.__class__.__name__}"

    for key in ("intent", "campaign_constraints", "result_limit"):
        if validated.get(key) != original.get(key):
            return None, f"non_audience_field_changed:{key}"
    for key in (
        "raw_query",
        "original_query",
        "planning_query",
        "normalized_query",
        "literal_bindings",
    ):
        if validated.get(key) != original.get(key):
            return None, f"application_owned_field_changed:{key}"

    requirement = validated.get("audience_requirement")
    execution = validated.get("event_expression")
    semantic_ir = validated.get("semantic_ir")
    if not isinstance(requirement, Mapping):
        return None, "canonical_requirement_missing"
    if requirement.get("issues") != [] or not isinstance(
        requirement.get("expression"), Mapping
    ):
        return None, "canonical_requirement_not_complete"
    if not isinstance(execution, Mapping):
        return None, "event_expression_missing"
    if (
        execution.get("source") != "audience_requirement"
        or execution.get("expression") != requirement.get("expression")
    ):
        return None, "event_expression_projection_mismatch"
    receipts = execution.get("receipts")
    if not isinstance(receipts, list) or not receipts or any(
        not isinstance(receipt, Mapping) or receipt.get("status") != "compiled"
        for receipt in receipts
    ):
        return None, "event_expression_receipts_missing"
    if not (
        isinstance(semantic_ir, Mapping)
        and semantic_ir.get("status") in {"resolved", "policy_applied"}
        and semantic_ir.get("failure_kind") in (None, "")
    ):
        return None, "semantic_ir_not_resolved"
    if validated.get("unresolved") or validated.get("unresolved_source_conditions"):
        return None, "unresolved_conditions_remain"
    if validated.get("unsupported") or validated.get("audience_execution_assets"):
        return None, "failure_marker_remains"
    if not has_empty_legacy_audience_surface(validated):
        return None, "second_audience_language_populated"
    if not audience_authority.executes_event_ir(validated):
        return None, "event_ir_authority_missing"

    try:
        expression = event_ir.condition_from_dict(dict(requirement["expression"]))
        projected_fields = {
            str(item) for item in projection.get("canonical_fields", [])
        }
        projected_sources = {
            str(item) for item in projection.get("canonical_sources", [])
        }
        projected_values = projection.get("canonical_values")
        projected_values = (
            projected_values if isinstance(projected_values, Mapping) else {}
        )
        automatic_time_fields = {
            f"{source}.occurred_at" for source in projected_sources
        }
        if event_ir.field_names(expression) - projected_fields - automatic_time_fields:
            return None, "event_ir_field_outside_graph_projection"
        if event_ir.sources(expression) - projected_sources - {"subject"}:
            return None, "event_ir_source_outside_graph_projection"
        for atom, _negated in event_ir.iter_signed_atoms(expression):
            if not isinstance(atom, event_ir.Comparison):
                continue
            pair = next(
                (
                    (field, literal)
                    for field, literal in ((atom.left, atom.right), (atom.right, atom.left))
                    if isinstance(field, event_ir.FieldRef)
                    and isinstance(literal, event_ir.Literal)
                ),
                None,
            )
            if pair is None:
                continue
            field, literal = pair
            allowed_values = projected_values.get(field.name)
            if (
                isinstance(allowed_values, Sequence)
                and not isinstance(allowed_values, (str, bytes, bytearray))
                and allowed_values
                and isinstance(literal.value, str)
                and literal.value not in {str(value) for value in allowed_values}
            ):
                return None, "event_ir_value_outside_graph_projection"

        atom_evidence = [
            atom.evidence
            for atom, _negated in event_ir.iter_signed_atoms(expression)
            if atom.evidence is not None
        ]
        original_requirement = original.get("audience_requirement")
        original_issues = (
            original_requirement.get("issues")
            if isinstance(original_requirement, Mapping)
            else []
        )
        for issue in original_issues or []:
            evidence = issue.get("evidence") if isinstance(issue, Mapping) else None
            if not isinstance(evidence, Mapping):
                return None, "registry_gap_issue_evidence_missing"
            start, end = evidence.get("start"), evidence.get("end")
            if not isinstance(start, int) or not isinstance(end, int) or start >= end:
                return None, "registry_gap_issue_evidence_invalid"
            if not any(item.start < end and start < item.end for item in atom_evidence):
                return None, "registry_gap_issue_not_discharged"

        catalog = audience_runtime.resolve_audience_catalog()
        today = date.fromisoformat(current_date) if current_date else None
        capability = event_compiler.validate_compiler_capability(
            expression,
            context=catalog.compile_context(literals=True, today=today),
        )
    except Exception as exc:  # noqa: BLE001 - compiler admission is fail-closed.
        return None, f"event_ir_capability_check_failed:{exc.__class__.__name__}"
    if capability.status != event_compiler.CAPABILITY_SUPPORTED:
        return None, "event_ir_compiler_capability_unsupported"
    return validated, "accepted"


def repair_grounded_canonical_event_ir(
    original: CampaignQueryPlanV4,
    *,
    query: str,
    context: Any,
    graph: Any,
    collection: str,
    url: str,
    api_key: str | None,
    embedding_model_name: str,
    vector_top_k: int,
    keyword_top_k: int,
    graph_top_k: int,
    hops: int,
    llm_model: str,
    query_structurer: Any,
    services: GroundingServices,
) -> CampaignQueryPlanV4:
    """Ground one retry of the same canonical producer and fail closed."""

    if not (
        audience_authority.requires_event_ir(original)
        and is_registry_gap_repair_candidate(original)
    ):
        return original
    if query_structurer is not None:
        services.write_log(
            "canonical_event_ir_grounding_skipped",
            {"query": query, "detail": "injected_structurer_has_no_grounding_channel"},
        )
        return original

    schema_query = services.schema_retrieval_query(query)
    vector_hits: list[Any] = []
    keyword_hits: list[Any] = []
    retrieval_errors: list[str] = []
    try:
        vector_hits = services.vector_search(
            query=schema_query,
            collection=collection,
            url=url,
            api_key=api_key,
            embedding_model_name=embedding_model_name,
            limit=max(1, vector_top_k),
        )
    except Exception as exc:  # noqa: BLE001 - keyword grounding may still work.
        retrieval_errors.append(f"vector:{exc.__class__.__name__}")
    try:
        keyword_hits = services.keyword_search(
            graph=graph,
            query=query,
            limit=max(1, keyword_top_k),
        )
    except Exception as exc:  # noqa: BLE001 - vector grounding may still work.
        retrieval_errors.append(f"keyword:{exc.__class__.__name__}")
    try:
        hits = services.merge_hits([*vector_hits, *keyword_hits])
        context_nodes = services.expand_context(
            graph=graph,
            hits=hits,
            hops=hops,
            limit=max(1, graph_top_k),
        )
        catalog_snapshot = audience_runtime.catalog_snapshot()
        catalog = audience_runtime.resolve_audience_catalog()
        projection = project_canonical_event_ir_grounding(
            query,
            context_nodes,
            catalog_snapshot,
            allowed_fields=catalog.compiler_fields,
            allowed_sources=catalog.compiler_events,
        )
        instruction = render_canonical_event_ir_grounding_instruction(projection)
    except Exception as exc:  # noqa: BLE001 - preserve the honest registry gap.
        services.write_log(
            "canonical_event_ir_grounding_failed",
            {
                "query": query,
                "reason": f"projection_failed:{exc.__class__.__name__}",
                "retrieval_errors": retrieval_errors,
            },
        )
        return original

    services.write_log(
        "canonical_event_ir_grounding_projected",
        {
            "query": query,
            "canonical_fields": projection.get("canonical_fields", []),
            "canonical_sources": projection.get("canonical_sources", []),
            "canonical_values": projection.get("canonical_values", {}),
            "provenance_node_ids": projection.get("provenance_node_ids", []),
            "retrieval_errors": retrieval_errors,
        },
    )
    if instruction is None:
        return original

    candidate = services.structure_plan(
        query,
        context,
        llm_model,
        extra_instruction=instruction,
        model_override=services.repair_model(llm_model),
    )
    admitted, reason = admit_grounded_canonical_event_ir_repair(
        original,
        candidate,
        projection=projection,
        query=query,
        current_date=context.current_date,
    )
    services.write_log(
        "canonical_event_ir_grounding_admission",
        {"query": query, "accepted": admitted is not None, "reason": reason},
    )
    return admitted if admitted is not None else original


def mark_canonical_event_ir_lowering_failure(
    query_plan: dict[str, Any],
    reason: str,
    *,
    expression_key: str = "event_expression",
) -> None:
    """Quarantine a partial canonical expression and preserve user-owned failure."""

    query_plan.pop(expression_key, None)
    current = query_plan.get("semantic_ir")
    if isinstance(current, Mapping) and current.get("status") in {
        "needs_clarification",
        "unsupported",
    }:
        return
    query_plan["semantic_ir"] = empty_semantic_ir(
        status="needs_clarification",
        missing_fields=["audience.event_ir"],
        message=(
            "요청 조건을 Canonical Event IR로 완전하게 표현하지 못해 실행을 중단했습니다. "
            + reason
        ),
        failure_kind="system_failure",
    )


def event_expression_declares_member_state(expression: event_ir.Condition) -> bool:
    """Whether a positive state comparison already owns the default policy."""

    return any(
        not negated
        and isinstance(atom, event_ir.Comparison)
        and "subject.member_state" in event_ir.field_names(atom)
        for atom, negated in event_ir.iter_signed_atoms(expression)
    )


__all__ = [
    "GroundingServices",
    "admit_grounded_canonical_event_ir_repair",
    "event_expression_declares_member_state",
    "has_empty_legacy_audience_surface",
    "is_registry_gap_repair_candidate",
    "mark_canonical_event_ir_lowering_failure",
    "project_canonical_event_ir_grounding",
    "repair_grounded_canonical_event_ir",
    "render_canonical_event_ir_grounding_instruction",
]

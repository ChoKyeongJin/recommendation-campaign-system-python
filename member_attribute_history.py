"""등급/상태 시점·이력 조건의 **귀결 오케스트레이션**(원문을 읽지 않는다).

SemanticPlanV2 RelationPredicate 노드를 LegacyQueryPlanCompiler 가
`target_user.relational_operation` 슬롯으로 컴파일한 뒤, 이 모듈이 그 슬롯을
`relational_operations`(실행) 또는 `relational_ir`(정직한 차단)로 귀결하고,
resolved 분기에서만 원문 의무(member_state_history, 월 단위 temporal_recurrence)에
compiled 영수증을 발급한다.

2026-08-02 이행으로 여기서 사라진 것:
  - `detect_member_attribute_history` 호출(원문 정규식 감지 → 슬롯 생성)
  - `_drop_history_owned_missing_fields`(이력 capability 소유 결핍의 사후 삭제)
  - `_claim_transition_owned_grade_slots` 의 원문 '제외/빼' 검사
    (전이 소유 값 회수는 **노드 근거 스팬 겹침**으로 판정한다 — 아래 참고)

남은 것은 전부 슬롯·연산·스팬만 보는 순수 귀결이다.
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Callable, Mapping
from typing import Any

import compositional_targeting
import semantic_requirements


def row_owned_by_compiled_operation(
    item: Mapping[str, Any], query_plan: Mapping[str, Any]
) -> bool:
    """미해결 행이 검증된 이력 연산에 귀속되는지 — **근거 스팬 겹침**으로만 판정한다.

    예전에는 자유 한국어 문장을 토큰 정규식으로 훑어 귀속을 추정했다(런마다 다른 문장이
    나와 낱말 목록이 계속 늘었다). 이제 이력 연산은 SemanticPlan 노드에서 왔고 노드는
    자기 근거 구간을 갖는다 — 같은 구간을 근거로 삼은 미해결 행이면 그 연산 소유다.
    """
    operations = query_plan.get(compositional_targeting.PLAN_OPERATIONS_KEY) or []
    if not (
        operations
        and isinstance(operations[0], Mapping)
        and operations[0].get("status") == "resolved"
    ):
        return False
    owned = _relation_node_spans(query_plan)
    if not owned:
        return False
    span = item.get("source_span")
    bounds = _span_bounds(span)
    if bounds is None:
        return False
    start, end = bounds
    return any(node_start <= start and end <= node_end for node_start, node_end in owned)


def _relation_node_spans(query_plan: Mapping[str, Any]) -> list[tuple[int, int]]:
    """relation_predicate 노드가 청구한 원문 구간."""
    plan = query_plan.get("semantic_plan")
    nodes = plan.get("nodes") if isinstance(plan, Mapping) else None
    spans: list[tuple[int, int]] = []
    for node in nodes or []:
        if not isinstance(node, Mapping) or node.get("type") != "relation_predicate":
            continue
        start, end = node.get("source_start"), node.get("source_end")
        if isinstance(start, int) and isinstance(end, int) and start <= end:
            spans.append((start, end))
    return spans


def _span_bounds(span: Any) -> tuple[int, int] | None:
    if isinstance(span, Mapping):
        start, end = span.get("start"), span.get("end")
    elif isinstance(span, (list, tuple)) and len(span) == 2:
        start, end = span
    else:
        return None
    return (start, end) if isinstance(start, int) and isinstance(end, int) else None


def _claim_transition_owned_grade_slots(
    query_plan: dict[str, Any], operation: Mapping[str, Any]
) -> None:
    """전이 연산이 소유한 등급/상태 값을 다른 슬롯의 오배선에서 회수한다.

    '직전 상태는 골드'를 LLM 이 exclude.lifecycle=[gold] (골드 제외!)나
    target_user.lifecycle=[gold] (현재 골드)로 **함께** 방출하는 실사고(#19) — 그 값은
    전이의 출발값이지 제외/현재 필터가 아니다.

    귀속 판정은 근거 스팬으로 한다: 그 값의 V4 evidence 구간이 전이 노드의 구간 안에
    있을 때만 회수한다. 근거가 없으면 회수하지 않는다(fail-open — 오디언스 반전 위험이
    회수 실패보다 크다).
    """
    import plan_decisions

    if operation.get("aggregate") != "transition":
        return
    slot = operation.get("source_slot")
    slot = slot if isinstance(slot, Mapping) else {}
    from_value = slot.get("from_value")
    if not from_value:
        return
    owned_values = {value for value in (from_value, slot.get("to_value")) if value}
    node_spans = _relation_node_spans(query_plan)
    if not node_spans:
        return

    def _evidence_inside(path: str) -> bool:
        for item in query_plan.get("semantic_evidence") or []:
            if not isinstance(item, Mapping) or not str(item.get("path") or "").startswith(path):
                continue
            bounds = _span_bounds({"start": item.get("start"), "end": item.get("end")})
            if bounds and any(
                start <= bounds[0] and bounds[1] <= end for start, end in node_spans
            ):
                return True
        return False

    def _claim(container_key: str, *, remove: set[str]) -> None:
        container = query_plan.get(container_key)
        if not isinstance(container, dict):
            return
        lifecycle = container.get("lifecycle")
        if not isinstance(lifecycle, list) or not lifecycle:
            return
        removable = [value for value in lifecycle if value in remove]
        if not removable or not set(lifecycle) <= owned_values:
            return
        if not _evidence_inside(f"{container_key}.lifecycle"):
            return
        container["lifecycle"] = [value for value in lifecycle if value not in remove]
        for value in removable:
            plan_decisions.record(
                query_plan,
                filter_name="relational_operation:member_attribute_history",
                action=plan_decisions.CLAIM,
                slot=f"{container_key}.lifecycle:{value}",
                reason="전이 노드 근거 구간이 소유한 값 회수(직전/도착 등급은 제외·현재 필터가 아니다)",
                value=value,
            )

    _claim("exclude", remove=owned_values)
    _claim("target_user", remove={from_value})


def apply(
    query_plan: dict[str, Any],
    query: str,
    *,
    catalog_loader: Callable[[], Mapping[str, Any]],
) -> None:
    """컴파일러가 쓴 relational_operation 슬롯을 실행 IR 로 귀결한다(슬롯이 없으면 무동작)."""
    target_user = query_plan.get("target_user")
    if not isinstance(target_user, Mapping) or compositional_targeting.SLOT_KEY not in target_user:
        return
    try:
        catalog = catalog_loader()
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        # 카탈로그 손상은 이력 조건을 조용히 무시하는 것보다 명시 차단이 낫다.
        query_plan[compositional_targeting.PLAN_IR_KEY] = {
            "status": "unsupported",
            "message": f"속성 이력 카탈로그를 읽지 못했습니다: {exc.__class__.__name__}",
            "missing_fields": [],
        }
        return
    outcome = compositional_targeting.resolve_slot_to_operations(query_plan, catalog)
    if outcome != "resolved":
        return
    operations = query_plan.get(compositional_targeting.PLAN_OPERATIONS_KEY) or []
    operation = operations[0] if operations else {}
    _claim_transition_owned_grade_slots(query_plan, operation)
    # 영수증 가드: 이력 의무가 여러 절에 흩어져 있는데 연산은 하나뿐이면, 둘째 조건이 '컴파일됨'
    # 영수증과 함께 조용히 드롭된다(리뷰 실증) — 단일 절일 때만 발급한다(fail-close).
    history_clauses = {
        _clause_index(query, requirement.source_span)
        for requirement in semantic_requirements.capture_source_semantic_obligations(query)
        if requirement.base.get("name") == semantic_requirements.TEMPORAL_QUALIFIER_KIND
    }
    if len(history_clauses) > 1:
        return
    kinds = {semantic_requirements.TEMPORAL_QUALIFIER_KIND}
    value_filter = None
    requirement_filter = None
    if operation.get("aggregate") in ("count_distinct", "change_count"):
        # 월 그레인 다월 연산은 '모든 월/매월' 반복 의무도 보존한 채 컴파일된 것이다 —
        # 단, 그 의무의 절이 등급/상태 문맥일 때만('매월 구매' 같은 타 절 반복 의무는 남긴다).
        kinds.add("temporal_recurrence")
        value_filter = (
            lambda kind, value: kind != "temporal_recurrence"
            or (isinstance(value, Mapping) and value.get("bucket") == "month")
        )
        axis_re = _attribute_axis_pattern()
        requirement_filter = (
            lambda requirement: requirement.base.get("name") != "temporal_recurrence"
            or bool(axis_re.search(_clause_text(query, requirement.source_span)))
        )
    semantic_requirements.discharge_source_semantic_obligations(
        query_plan,
        query,
        kinds=kinds,
        status="compiled",
        compiler="compositional_targeting",
        evidence={"operation": copy.deepcopy(operation)},
        value_filter=value_filter,
        requirement_filter=requirement_filter,
    )


def _attribute_axis_pattern() -> re.Pattern[str]:
    """속성 축 표면형 정규식 — 낱말을 여기 나열하지 않고 카탈로그에서 파생한다."""
    import targeting_domain  # 지연 import(순환 없음)

    terms = targeting_domain.attribute_axis_terms()
    if not terms:
        return re.compile(r"(?!)")
    return re.compile("|".join(re.escape(term) for term in terms))


def _clause_bounds(query: str, span: Any) -> tuple[int, int]:
    if isinstance(span, Mapping):
        start = int(span.get("start") or 0)
    elif isinstance(span, (list, tuple)) and span:
        start = int(span[0])
    else:
        start = 0
    clause_start = max(
        query.rfind(",", 0, start), query.rfind(".", 0, start), query.rfind(";", 0, start)
    ) + 1
    following = [
        index for token in (",", ".", ";") if (index := query.find(token, start)) >= 0
    ]
    clause_end = min(following) if following else len(query)
    return clause_start, clause_end


def _clause_index(query: str, span: Any) -> int:
    return _clause_bounds(query, span)[0]


def _clause_text(query: str, span: Any) -> str:
    start, end = _clause_bounds(query, span)
    return query[start:end]

"""Event IR 컴파일 실패의 좌표 — 어느 조건이, 어느 단계에서, 무슨 코드로 막혔는가.

이 파일이 지키는 것 둘.

1. **좌표에 코드가 있다.** ``code`` 가 없으면 ``plan_validation._marker`` 가 항목의 한국어
   **문장**(``reason``)을 정규화해 issue code 로 승격한다 — 닫힌 집합 밖의 코드가 사용자
   문구까지 흘러간다. 그 코드는 ``_status_for_validation_code`` 의 판정 입력이기도 하다.
2. **좌표가 sql_result 까지 간다.** plan 안에서만 살면 "어디서 막혔는가"는 저장된 JSONB 를
   직접 파야만 나온다.

**소유권 경계**: 읽을 수 없는 저장 표현과 표현 부재는 빌더가 아니라 plan_validation 이 소유한다
(각각 ``event_expression_schema_invalid`` / ``canonical_event_expression_missing``). 빌더가 같은
사실을 한 번 더 기록하면 한 실패에 소유자가 둘이 된다 —
``tests/test_query_pipeline_legacy_adapter.py`` 의 "빌더까지 내려가지 않는다"가 그 계약이고,
아래 첫 테스트가 좌표 쪽에서 같은 경계를 고정한다.
"""

from __future__ import annotations

import graph_rag
import plan_validation
import query_pipeline

# 파싱되는 저장 표현(지난달 구매). 컴파일 단계 실패를 재현할 때 쓴다.
_WIRE = {
    "type": "exists",
    "relation": {
        "type": "filter",
        "relation": {"type": "source", "name": "purchase"},
        "where": {
            "type": "time_filter",
            "field": {"type": "field", "name": "purchase.occurred_at"},
            "window": {"type": "interval", "start": "2026-07-01", "end_exclusive": "2026-08-01"},
        },
    },
    "evidence": {"text": "지난달 구매", "start": 0, "end": 6},
}


def _plan(expression: dict, *, source: str = "audience_requirement") -> dict:
    return {
        "intent": "find_user_segment",
        "original_query": "지난달 구매한 회원",
        "target_user": {},
        "exclude": {},
        "campaign_constraints": {},
        "event_expression": {"expression": expression, "source": source, "receipts": []},
    }


def _event_ir_items(plan: dict) -> list[dict]:
    return [
        item
        for item in (plan.get("unresolved_source_conditions") or [])
        if isinstance(item, dict) and item.get("source") == "event_ir"
    ]


def _build_with_failing_pipeline(plan: dict) -> None:
    def explode(payload, **kwargs):
        raise query_pipeline.QueryPipelineError("sql_compilation", "표현할 수 없습니다")

    original = graph_rag.query_pipeline.compile_audience_predicate
    try:
        graph_rag.query_pipeline.compile_audience_predicate = explode  # type: ignore[assignment]
        assert graph_rag.build_event_expression_sql_candidate(plan) is None
    finally:
        graph_rag.query_pipeline.compile_audience_predicate = original  # type: ignore[assignment]


def test_unreadable_stored_expression_is_owned_by_plan_validation_not_the_builder() -> None:
    """읽을 수 없는 표현의 소유자는 하나다 — 빌더는 좌표를 남기지 않는다."""
    plan = _plan({"type": "definitely_not_a_node"})

    assert graph_rag.build_event_expression_sql_candidate(plan) is None
    assert not _event_ir_items(plan), "빌더가 plan_validation 소유 실패를 두 번째로 기록했다"

    codes = {issue.code for issue in plan_validation.validate_executable_plan(plan).issues}
    assert "event_expression_schema_invalid" in codes


def test_absent_expression_is_owned_by_plan_validation_too() -> None:
    plan = {
        "intent": "find_user_segment",
        "target_user": {},
        "exclude": {},
        "campaign_constraints": {},
        "audience_requirement": {"expression": None, "issues": []},
    }
    assert graph_rag.build_event_expression_sql_candidate(plan) is None
    assert not _event_ir_items(plan)

    codes = {issue.code for issue in plan_validation.validate_executable_plan(plan).issues}
    assert "canonical_event_expression_missing" in codes


def test_compile_failure_carries_both_stage_and_code() -> None:
    """단계는 파이프라인의 어디인지를, 코드는 무엇이 실패했는지를 말한다."""
    plan = _plan(_WIRE)
    _build_with_failing_pipeline(plan)

    items = _event_ir_items(plan)
    assert items
    assert items[0]["stage"] == "sql_compilation"
    assert items[0]["code"] == "event_ir_compile_failed"
    assert items[0]["path"] == "plan.event_expression"


def test_the_code_not_the_korean_sentence_becomes_the_issue_code() -> None:
    """코드가 없으면 _marker 가 reason 문장을 코드로 승격한다 — 그 회귀를 막는다."""
    plan = _plan(_WIRE)
    _build_with_failing_pipeline(plan)

    isolated = {key: value for key, value in plan.items() if key != "event_expression"}
    result = plan_validation.validate_executable_plan(isolated)

    codes = {issue.code for issue in result.issues}
    assert "event_ir_compile_failed" in codes
    # 한국어 문장이 코드로 승격되지 않는다.
    assert not any("컴파일하지" in code for code in codes)


def test_both_failure_branches_share_one_recorded_shape() -> None:
    """기록 자리가 갈라지면 '어디서 막혔는가'가 경로마다 다른 답을 준다."""
    pipeline_plan = _plan(_WIRE)
    _build_with_failing_pipeline(pipeline_plan)

    # IR 스키마 분기는 canonical 권위에서 진입 가드가 먼저 걸리므로, 검증 컨텍스트를 주입해
    # 직접 호출한다(저장소의 직접 호출 관례).
    schema_plan = _plan({"type": "definitely_not_a_node"}, source="legacy_migration")
    validation = plan_validation.validate_executable_plan(schema_plan)
    token = graph_rag._SQL_VALIDATION_CONTEXT.set((id(schema_plan), validation))
    try:
        assert graph_rag.build_event_expression_sql_candidate(schema_plan) is None
    finally:
        graph_rag._SQL_VALIDATION_CONTEXT.reset(token)

    pipeline_item, schema_item = _event_ir_items(pipeline_plan)[0], _event_ir_items(schema_plan)[0]
    assert set(pipeline_item) == set(schema_item)
    assert schema_item["stage"] == "ir_schema"
    assert schema_item["code"] == "event_ir_schema_invalid"


def test_recording_the_same_coordinate_twice_does_not_duplicate() -> None:
    plan = _plan(_WIRE)
    _build_with_failing_pipeline(plan)
    _build_with_failing_pipeline(plan)
    assert len(_event_ir_items(plan)) == 1


def test_blocking_results_on_the_canonical_lane_carry_the_coordinate() -> None:
    """좌표가 차단 결과에 실린다 — plan 안에서만 살면 응답·로그에서 볼 수 없다.

    canonical 레인의 실패는 거의 항상 차단 결과로 끝난다(좌표가 남는 순간 plan_validation 이
    internal_invalid 를 내므로 최종 반환 dict 는 그 항목을 볼 일이 없다). 그래서 재는 것은
    최종 dict 가 아니라 **차단 결과 두 곳**이다.
    """
    plan = _plan(_WIRE)
    _build_with_failing_pipeline(plan)
    coordinate = _event_ir_items(plan)[0]

    validation = plan_validation.validate_executable_plan(plan)
    blocked = graph_rag._plan_validation_blocking_sql_result(validation, plan)
    assert coordinate in blocked["unresolved_source_conditions"]

    # 의미 게이트 차단 결과도 같은 좌표를 싣는다.
    plan["semantic_ir"] = {
        "status": "needs_clarification",
        "missing_fields": [],
        "message": "확인이 필요합니다.",
    }
    semantic_blocked = graph_rag._semantic_ir_blocking_sql_result(plan)
    assert semantic_blocked is not None
    assert coordinate in semantic_blocked["unresolved_source_conditions"]


def test_plan_validation_blocker_stays_callable_without_a_plan() -> None:
    """인자를 선택으로 둔 이유 — 기존 직접 호출(계약 테스트)이 그대로 돌아야 한다."""
    plan = _plan(_WIRE)
    _build_with_failing_pipeline(plan)
    validation = plan_validation.validate_executable_plan(plan)

    blocked = graph_rag._plan_validation_blocking_sql_result(validation)
    assert blocked["unresolved_source_conditions"] == []
    assert blocked["failure_reason"].startswith("plan_validation_")

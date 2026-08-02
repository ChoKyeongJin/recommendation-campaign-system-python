"""실패 응답의 정직성 계약.

실패는 "어떤 조건이·어디서·왜"를 말해야 한다. 이 모듈은 26종 프롬프트 감사(2026-08-02)에서
드러난 세 가지 뭉개짐을 회귀로 고정한다:
1) 주기 표지 정규식이 낱말 내부('구매주기'의 '매주')를 오탐해 fail-close 차단하던 결함
2) plan_validation internal_invalid 가 사유 없는 '미지원'으로만 노출되던 결함
3) semantic_ir 미확정 필드가 범용 문구('필수 비교 조건을 확인해 주세요')로 뭉개지던 결함
"""

from __future__ import annotations

import graph_rag
import plan_validation
import semantic_requirements


def _recurrence_kinds(query: str) -> list[str]:
    return [
        requirement.base.get("name")
        for requirement in semantic_requirements.capture_source_semantic_obligations(query)
        if requirement.base.get("name") == "temporal_recurrence"
    ]


def test_recurrence_marker_not_matched_inside_word() -> None:
    # '구매주기'의 '매주', '구매일'의 '매일'은 주기 표현이 아니다.
    assert _recurrence_kinds(
        "회원별 평균 구매주기가 30일 이내이고 다음 구매예정일이 지난 고객을 찾아줘"
    ) == []
    assert _recurrence_kinds("구매일 기준으로 최근 주문 고객을 찾아줘") == []


def test_recurrence_marker_still_matched_as_standalone_word() -> None:
    assert _recurrence_kinds("매주 구매한 회원을 찾아줘") == ["temporal_recurrence"]
    assert _recurrence_kinds("모든 월에 구매 이력이 있는 회원") == ["temporal_recurrence"]


def test_plan_validation_internal_invalid_exposes_reasons() -> None:
    validation = plan_validation.PlanValidationResult(
        status=plan_validation.INTERNAL_INVALID,
        issues=(
            plan_validation.PlanValidationIssue(
                code="semantic_ir_schema_invalid",
                status=plan_validation.INTERNAL_INVALID,
                path="semantic_ir",
            ),
        ),
        blocking_claim_ids=(),
        unresolved_span_ids=(),
    )
    result = graph_rag._plan_validation_blocking_sql_result(validation)
    assert result["failure_reason"] == "plan_validation_internal_invalid"
    # 사유 없는 침묵 금지: 질문·미확정 조건에 경로와 검증 코드가 실려야 한다.
    assert result["clarification_questions"]
    question = result["clarification_questions"][0]
    assert "semantic_ir" in question
    assert "semantic_ir_schema_invalid" in question
    assert result["missing_input_conditions"]
    # 내부 불량은 능력 부재 선언이 아니다 — '미지원'이 아니라 확인 필요.
    assert result["interpretation_status"] == "needs_clarification"


def test_plan_validation_unsupported_stays_unsupported() -> None:
    validation = plan_validation.PlanValidationResult(
        status=plan_validation.UNSUPPORTED,
        issues=(
            plan_validation.PlanValidationIssue(
                code="semantic_operation_unsupported",
                status=plan_validation.UNSUPPORTED,
                path="semantic_ir.operations[0]",
            ),
        ),
        blocking_claim_ids=(),
        unresolved_span_ids=(),
    )
    result = graph_rag._plan_validation_blocking_sql_result(validation)
    assert result["interpretation_status"] == "unsupported"
    assert "지원되지 않습니다" in result["clarification_questions"][0]


def test_semantic_ir_missing_fields_render_korean_labels() -> None:
    plan = {
        "semantic_ir": {
            "status": "needs_clarification",
            "missing_fields": ["inactivity_period.value", "threshold"],
        }
    }
    result = graph_rag._semantic_ir_blocking_sql_result(plan)
    assert result is not None
    question = result["clarification_questions"][0]
    assert "휴면·미활동 기간" in question
    assert "임계값" in question
    assert "필수 비교 조건을 확인해 주세요." != question
    labels = [item["label"] for item in result["missing_input_conditions"]]
    assert "휴면·미활동 기간" in labels


def test_semantic_ir_explicit_message_still_wins() -> None:
    plan = {
        "semantic_ir": {
            "status": "needs_clarification",
            "missing_fields": ["threshold"],
            "message": "구매 금액 기준을 알려 주세요.",
        }
    }
    result = graph_rag._semantic_ir_blocking_sql_result(plan)
    assert result is not None
    assert result["clarification_questions"] == ["구매 금액 기준을 알려 주세요."]

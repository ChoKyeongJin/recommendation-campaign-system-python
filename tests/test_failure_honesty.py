"""실패 응답의 정직성 계약.

실패는 "어떤 조건이·어디서·왜"를 말해야 한다. 이 모듈은 26종 프롬프트 감사(2026-08-02)에서
드러난 세 가지 뭉개짐을 회귀로 고정한다:
1) 주기 표지 정규식이 낱말 내부('구매주기'의 '매주')를 오탐해 fail-close 차단하던 결함
2) plan_validation internal_invalid 가 사유 없는 '미지원'으로만 노출되던 결함
3) semantic_ir 미확정 필드가 범용 문구('필수 비교 조건을 확인해 주세요')로 뭉개지던 결함
"""

from __future__ import annotations

import api
import failure_messages
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


# ── 관문 거부 근거 보존(P0-1) ─────────────────────────────────────────────────
# 실측(2026-08-03): 후보가 있는데 거부된 실패 행은 `generated_sql`·`error_detail` 두
# 이름 있는 컬럼이 전부 NULL 이었다. 근거는 사라진 게 아니라 `selected_candidate`
# JSONB 안에만 있어 운영 조회로는 보이지 않았다 — 그래서 원인을 영원히 알 수 없었다.


def _rejected_sql_result(**overrides: object) -> dict:
    """관문이 후보를 거부한 sql_result(실기록 shape 를 따른다)."""
    candidate = {
        "sql": "SELECT B.MEMBER_NO FROM CRM_MB_BASEINFO B",
        "validation": {
            "is_valid": False,
            "issues": [
                {
                    "code": "column_not_allowed",
                    "severity": "error",
                    "message": "Column is not present in schema catalog: crm_sl_orderdetailmall.qty",
                }
            ],
        },
        "coverage": {"is_satisfied": True, "missing_conditions": []},
        "is_eligible": False,
    }
    return {
        "sql": None,
        "blocked_sql": None,
        "selected": candidate,
        "candidates": [candidate],
        "candidate_count": 1,
        "is_success": False,
        "failure_reason": "sql_guard_failed",
        **overrides,
    }


def test_rejected_candidate_reasons_are_collected_by_structure() -> None:
    reasons = failure_messages.rejected_candidate_reasons(_rejected_sql_result()["selected"])
    assert reasons, "거부된 후보에 사유가 하나도 없으면 로그로 원인을 알 수 없다."
    assert any("column_not_allowed" in reason for reason in reasons)
    # 만족한 보고는 사유가 아니다 — 통과한 관문까지 실으면 진짜 원인이 묻힌다.
    assert not any(reason.startswith("coverage.") for reason in reasons)


def test_new_gate_reports_without_being_added_to_a_hand_list() -> None:
    """관문이 늘어도 사유는 자동으로 실린다 — 이름 목록을 손으로 들지 않기 때문이다.

    손 목록이면 새 관문의 거부가 다시 조용해진다(이 결함의 원래 모양이다).
    """
    candidate = {
        "sql": "SELECT 1",
        "brand_new_gate_validation": {
            "is_satisfied": False,
            "issues": [{"code": "future_gate_failed", "message": "아직 존재하지 않는 관문"}],
        },
    }
    reasons = failure_messages.rejected_candidate_reasons(candidate)
    assert any("future_gate_failed" in reason for reason in reasons)


def test_unsatisfied_report_without_items_still_names_the_gate() -> None:
    candidate = {"sql": "SELECT 1", "intent_scope": {"is_satisfied": False}}
    reasons = failure_messages.rejected_candidate_reasons(candidate)
    assert reasons == ["intent_scope: 불만족(사유 항목 없음)"]


def test_gate_rejection_reaches_the_named_failure_log_columns() -> None:
    """P0-1 의 가드: 후보가 1 이상인데 사유가 비면 red.

    이 불변식이 없으면 P1~P5 의 개선이 '원인을 알 수 없는 실패'로 나타나도 진단할 수 없다.
    """
    request = api.TargetSqlRequest(prompt="사료를 구매한 회원을 찾아줘", execute_sql=False)
    sql_result = _rejected_sql_result()
    payload = api._target_sql_failure_payload(
        request,
        {"sql_result": sql_result, "query_plan": {}},
        {"status": "no_verified_sql", "failure_reason": "sql_guard_failed", "sql": None},
        {},
    )

    assert payload is not None
    assert payload["failure_stage"] == "sql_generation"
    assert sql_result["candidate_count"] >= 1
    assert payload["generated_sql"], "거부된 후보 SQL 이 이름 있는 컬럼에 남지 않는다."
    assert payload["error_detail"], "후보가 있는데 거부 사유가 비었다 — 원인 진단이 불가능해진다."
    assert "column_not_allowed" in payload["error_detail"]


def test_blocked_sql_channel_is_preferred_when_present() -> None:
    """의미 검증에서 막힌 SQL 은 이미 blocked_sql 로 보존된다 — 그 채널을 먼저 읽는다."""
    sql_result = _rejected_sql_result(blocked_sql="SELECT /* blocked */ 1")
    evidence = failure_messages.rejected_candidate_evidence(sql_result)
    assert evidence["sql"] == "SELECT /* blocked */ 1"


def test_successful_result_gets_no_fabricated_rejection_detail() -> None:
    """출고에 성공한 결과에서 사유를 지어내지 않는다 — 이것은 실패 경로의 투영이다."""
    evidence = failure_messages.rejected_candidate_evidence(
        {"is_success": True, "selected": {"sql": "SELECT 1"}, "candidate_count": 1}
    )
    assert evidence == {"sql": None, "detail": None, "candidate_count": 0, "reasons": []}


def test_post_selection_gate_rejections_also_reach_the_columns() -> None:
    """후보 **선택 뒤에** 거부하는 관문의 사유도 남아야 한다.

    실측(2026-08-03 라이브): `semantic_verification_failed` 행은 거부된 SQL 은 남았는데
    `error_detail` 이 비었다 — 그 관문의 사유는 후보 안이 아니라 결과 최상위에 있기 때문이다.
    후보 안만 훑으면 '후보가 있는데 사유가 빈' 상태가 그대로 남는다.
    """
    sql_result = _rejected_sql_result(
        selected={"sql": "SELECT 1", "validation": {"is_valid": True, "issues": []}},
        blocked_sql="SELECT 1",
        failure_reason="semantic_verification_failed",
        semantic_invariants={
            "ran": True, "ok": False,
            "issues": [{"code": "window_domain_leak", "detail": "기간 조건이 SQL 에 없습니다"}],
        },
    )
    evidence = failure_messages.rejected_candidate_evidence(sql_result)
    assert evidence["sql"] == "SELECT 1"
    assert evidence["detail"], "선택 후 관문이 막았는데 사유가 비었다."
    assert "window_domain_leak" in evidence["detail"]


def test_no_candidate_means_no_fabricated_rejection() -> None:
    """후보가 아예 없던 요청(결핍·미지원)에는 거부 근거를 지어내지 않는다."""
    evidence = failure_messages.rejected_candidate_evidence({
        "is_success": False, "selected": None, "candidates": [], "candidate_count": 0,
        "blocked_sql": None,
        "input_validation": {"is_satisfied": False, "missing_conditions": [{"path": "x"}]},
    })
    assert evidence["detail"] is None
    assert evidence["sql"] is None


# 4) 오디언스 실행 권위가 닫힌 어휘 밖일 때 예외(HTTP 500)로 뭉개지던 결함.
#    권위를 읽는 첫 지점이 plan_validation 보다 앞이라, 검증기 안에서 접는 설계로는 못 막는다.


def test_invalid_audience_authority_is_a_named_failure_not_an_exception() -> None:
    plan = {
        "intent": "find_user_segment",
        "target_user": {},
        "exclude": {},
        "campaign_constraints": {},
        "audience_authority": "event-ir",  # 오타(하이픈)
    }
    result = graph_rag._audience_authority_blocking_sql_result(plan)

    assert result is not None
    assert result["failure_reason"] == "audience_authority_invalid"
    # 내부/저장 산출물의 불량이지 능력의 부재가 아니다.
    assert result["interpretation_status"] == "needs_clarification"
    assert api_status_of(result) == "needs_clarification"
    # 사유 없는 침묵 금지: 어느 값이 문제인지 운영자가 볼 수 있어야 한다.
    assert result["missing_input_conditions"][0]["path"] == "audience_authority"
    assert result["missing_input_conditions"][0]["label"] == "audience_authority_invalid"
    assert result["clarification_questions"]


def test_valid_audience_authority_values_are_not_blocked() -> None:
    for value in ("event_ir", "legacy"):
        plan = {"audience_authority": value}
        assert graph_rag._audience_authority_blocking_sql_result(plan) is None
    # 권위 미선언은 호환 경로가 읽는다 — 여기서 막지 않는다.
    assert graph_rag._audience_authority_blocking_sql_result({}) is None


def test_build_sql_result_does_not_raise_on_an_invalid_authority() -> None:
    query = "여성 회원"
    plan = {
        "intent": "find_user_segment",
        "target_user": {},
        "exclude": {},
        "campaign_constraints": {},
        "audience_authority": "EVENT-IR",
    }
    result = graph_rag.build_sql_result(
        graph_rag.nx.Graph(), query, plan, [], graph_rag.DEFAULT_SCHEMA_PATH,
        default_limit=100, original_query=query,
    )
    assert result["failure_reason"] == "audience_authority_invalid"
    assert result["sql"] is None


def test_the_user_message_does_not_send_them_back_to_their_conditions() -> None:
    """화면의 1차 표면은 message 다 — 배지가 정직해도 본문이 거짓이면 사용자는 본문을 따른다.

    이 실패는 조건을 **읽기 전에** 막힌다. 기본 문구("조건을 만족하는 검증된 SQL 이 없습니다")는
    사용자를 조건 쪽으로 보내는데, 고칠 곳은 저장된 실행 설정이라 아무리 고쳐 써도 안 풀린다.
    """
    plan = {"intent": "find_user_segment", "audience_authority": "event-ir"}
    result = graph_rag._audience_authority_blocking_sql_result(plan)
    message = graph_rag._describe_sql_failure(plan, result)

    assert message == result["clarification_questions"][0]
    assert "검증된 SQL" not in message, "조건 쪽으로 보내는 기본 문구가 그대로 나간다."


def api_status_of(sql_result: dict) -> str:
    return graph_rag._api_status(sql_result)

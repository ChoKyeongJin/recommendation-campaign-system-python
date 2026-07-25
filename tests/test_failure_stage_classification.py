"""실패 단계 분류(failure_reason → 파이프라인 단계) 회귀.

배경: 타겟 SQL 생성이 실패하면 화면에 늘 같은 안내("타겟 조건을 찾지 못해…")만 보여서, 사용자가
집합식 파싱에서 막혔는지 SQL 안전 검증에서 막혔는지 구분할 수 없었다. failure_reason 은 "왜"만
설명하므로, "어디서" 막혔는지를 파이프라인 단계로 승격해 api_response.failure_stage 로 노출한다.

실행(컨테이너): docker compose exec -w /app -e PYTHONPATH=/app api pytest tests/test_failure_stage_classification.py -q
"""

import graph_rag as g


def test_guard_failure_maps_to_sql_safety_stage():
    stage = g._classify_failure_stage("sql_guard_failed")
    assert stage["code"] == "sql_safety_validation"
    assert stage["label"] == "SQL 안전 검증"
    assert stage["reason"] == "sql_guard_failed"
    assert stage["order"] == 3 and stage["total"] == 6


def test_no_candidates_maps_to_condition_recognition_stage():
    # 집합식 파싱/조건 컴파일이 후보를 못 만들면 no_sql_candidates → 조건 인식 단계.
    stage = g._classify_failure_stage("no_sql_candidates")
    assert stage["code"] == "condition_recognition"
    assert stage["order"] == 1


def test_semantic_verification_maps_to_last_stage():
    stage = g._classify_failure_stage("semantic_verification_failed")
    assert stage["code"] == "semantic_verification"
    assert stage["order"] == stage["total"] == 6


def test_each_reason_has_a_distinct_stage_label():
    reasons = [
        "no_sql_candidates",
        "real_db_unsupported_conditions",
        "sql_guard_failed",
        "query_plan_conditions_missing",
        "intent_scope_mismatch",
        "semantic_verification_failed",
    ]
    labels = {g._classify_failure_stage(r)["label"] for r in reasons}
    # 위 6개는 서로 다른 단계 → 라벨이 6개 모두 구분돼야 "늘 같은 메시지" 문제가 풀린다.
    assert len(labels) == 6


def test_pipeline_is_ordered_and_marks_the_failed_stage():
    stage = g._classify_failure_stage("intent_scope_mismatch")
    pipeline = stage["pipeline"]
    assert [p["order"] for p in pipeline] == [1, 2, 3, 4, 5, 6]
    # 프론트 스텝퍼가 강조할 실패 단계가 pipeline 안에 order 로 표시돼 있어야 한다.
    failed = next(p for p in pipeline if p["order"] == stage["order"])
    assert failed["code"] == stage["code"] == "intent_scope"


def test_success_and_unknown_reasons_have_no_stage():
    assert g._classify_failure_stage(None) is None
    assert g._classify_failure_stage("some_new_unmapped_reason") is None


# ── 세부 라벨: 조건 확정 실패 안에서 집합식/계산식/의미해석 구분 ──────────────────
def _required_missing(path: str) -> dict:
    return {
        "failure_reason": "query_plan_required_conditions_missing",
        "missing_input_conditions": [{"path": path, "label": "x", "question": "무엇?"}],
        "clarification_questions": ["무엇?"],
    }


def test_set_expression_failure_refines_stage_label():
    # 집합식이 감지됐지만 컴파일 안 돼 막힌 경우 → 같은 '조건 인식' 순번에서 라벨만 '집합식 파싱'으로 세분.
    stage = g._classify_failure_stage(
        "query_plan_required_conditions_missing", _required_missing("set_expressions.seg1")
    )
    assert stage["label"] == "집합식 파싱"
    assert stage["order"] == 1  # 여전히 '조건 인식' 단계(순번 유지)
    # 스텝퍼에서 강조되는 실패 단계 칩의 라벨도 헤더와 일치해야 한다.
    failed_step = next(p for p in stage["pipeline"] if p["order"] == stage["order"])
    assert failed_step["label"] == "집합식 파싱"


def test_computed_metric_and_semantic_refinements():
    assert (
        g._classify_failure_stage(
            "query_plan_required_conditions_missing", _required_missing("computed_metrics.m1")
        )["label"]
        == "계산식 해석"
    )
    assert (
        g._classify_failure_stage(
            "query_plan_required_conditions_missing", _required_missing("semantic_resolutions.r1")
        )["label"]
        == "의미 해석 확정"
    )


def test_required_missing_without_typed_path_keeps_default_label():
    stage = g._classify_failure_stage(
        "query_plan_required_conditions_missing", _required_missing("target_user.something")
    )
    assert stage["label"] == "타겟 조건 인식"


def test_failure_message_names_the_set_expression_kind():
    msg = g._describe_sql_failure({}, _required_missing("set_expressions.seg1"))
    assert "집합식" in msg and "무엇?" in msg


def test_api_response_carries_failure_stage():
    api_response = g.build_recommendation_api_response(
        query="안녕",
        query_plan={"intent": "find_user_segment", "retrieval": {}},
        sql_result={"is_success": False, "failure_reason": "sql_guard_failed"},
        answer_response={},
    )
    assert api_response["failure_stage"]["code"] == "sql_safety_validation"
    # 성공 응답에는 단계가 없어야 한다(배지 미노출).
    ok = g.build_recommendation_api_response(
        query="q",
        query_plan={"intent": "find_user_segment", "retrieval": {}},
        sql_result={"is_success": True, "failure_reason": None},
        answer_response={"content": "ok"},
    )
    assert ok["failure_stage"] is None

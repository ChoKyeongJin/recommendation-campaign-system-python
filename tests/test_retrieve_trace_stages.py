"""트레이스 10단계 구조 회귀(build_retrieve_trace / build_partial_retrieve_trace).

배경: /api/targeting/trace 화면이 처리 파이프라인을 10단계(프롬프트 재작성 → … → 실행·결과)로
보여준다. 각 단계에 method(혼합=LLM/규칙=결정론)·status(ok/info/fail/skipped)를 붙이고, 오류 시
'오류 전까지'의 부분 트레이스를 만든다. 이 계약을 고정한다.

실행(컨테이너): docker compose exec -w /app -e PYTHONPATH=/app api pytest tests/test_retrieve_trace_stages.py -q
"""

import graph_rag as g


def _result(**overrides):
    query_plan = {
        "intent": "find_user_segment",
        "retrieval": {"scope_mode": "targeting", "targeting_query": "서울 VIP", "channel_query": "RCS", "query": "서울 VIP", "targeting_terms": ["서울", "vip"], "terms": []},
        "matched_terms": [{"matched_text": "VIP", "canonical": "vip"}],
        "semantic_resolutions": [],
        "dimension_filters": [{"prompt_label": "SIDO", "codes": ["서울"], "names": ["서울"]}],
        "set_expressions": [{"expression_id": "seg1", "ko_label": "세그먼트 집합식", "set_ast": {"type": "set_op", "op": "+"}}],
        "cart_context": False,
        "planning_query": "서울 VIP",
        "target_user": {"lifecycle": ["vip"], "aggregate_conditions": [{"metric_id": "purchase_amount"}]},
        "parser": {"type": "llm"},  # LLM 질의계획 경로(모델·프롬프트 배지 노출)
    }
    sql_result = {
        "candidates": [{"id": "tpl", "tables": ["CRM_MB_BASEINFO"], "guard_valid": True, "coverage_ok": True, "is_eligible": True, "validation": {}, "coverage": {}, "intent_scope": {}, "unmentioned_conditions": {}}],
        "condition_tokens": [{"path": "target_user.lifecycle"}],
        "required_conditions": [],
        "sql": "SELECT 1",
        "is_success": True,
        "failure_reason": None,
        "semantic_verification": {"ran": False},
        "target_connection": None,
        "target_dialect": "postgres",
    }
    base = {
        "query": "서울 VIP",
        "query_plan": query_plan,
        "sql_result": sql_result,
        "api_response": {"status": "success", "sql": "SELECT 1", "message": "ok"},
        "prompt_normalization": {"original": "서울 브이아이피", "normalized": "서울 VIP", "summary": "요약", "corrections": []},
        "graph_context": [{"id": "n1", "title": "T", "type": "schema_table", "seed_score": 1.0, "reasons": [], "path": []}],
        "vector_matches": [{"id": "v1", "score": 0.9}],
        "keyword_matches": [{"id": "k1", "score": 0.8}],
        "seed_matches": [{"id": "v1"}],
        "stage_log": [],
        "timings_ms": {},
        # 실제 전송된 LLM 질의계획 프롬프트(캡처). retrieve 가 result 상단에 담아 준다.
        "llm_query_plan_prompt": {"system": "SYS_PROMPT", "user": "[User Query]\n서울 VIP", "response": '{"intent":"find_user_segment"}'},
    }
    base.update(overrides)
    return base


_EXPECTED_METHODS = ["혼합", "혼합", "혼합", "혼합", "규칙", "규칙", "혼합", "혼합", "혼합", "규칙"]


def test_trace_has_ten_stages_with_expected_methods():
    trace = g.build_retrieve_trace(_result())
    stages = trace["stages"]
    assert [s["step"] for s in stages] == list(range(1, 11))
    assert [s["method"] for s in stages] == _EXPECTED_METHODS


def test_stages_carry_tech_name_and_refs():
    trace = g.build_retrieve_trace(_result())
    stages = trace["stages"]
    # 모든 단계에 기술명이 있어야 한다(화면 mono 라인).
    assert all(s.get("tech_name") for s in stages)
    # 3단계(질의 계획 수립)는 프롬프트/데이터/모델 참조를 함께 노출한다.
    refs3 = trace["stages"][2]["refs"]
    names = {r["name"] for r in refs3}
    kinds = {r["kind"] for r in refs3}
    assert "query_plan_system.txt" in names and "targeting_lexicon.json" in names
    assert {"프롬프트", "데이터", "모델"} <= kinds
    # 5단계(값 해석)는 규칙 전용이라 모델 참조가 없어야 한다.
    assert all(r["kind"] != "모델" for r in trace["stages"][4]["refs"])


def test_trace_marks_only_input_used_refs_without_removing_refs():
    result = _result()
    result["prompt_normalization"]["mode"] = "llm_rewrite"

    trace = g.build_retrieve_trace(result)
    stage1_refs = {ref["name"]: ref for ref in trace["stages"][0]["refs"]}
    stage3_refs = {ref["name"]: ref for ref in trace["stages"][2]["refs"]}
    stage5_refs = {ref["name"]: ref for ref in trace["stages"][4]["refs"]}
    stage6_refs = {ref["name"]: ref for ref in trace["stages"][5]["refs"]}

    # 전체 참조는 보존하고, 이번 입력에 사용된 재작성 프롬프트만 강조 대상으로 표시한다.
    assert set(stage1_refs) == {
        "prompt_rewrite_system.txt",
        "prompt_normalize_system.txt (보수 모드)",
        "gpt-4o-mini",
    }
    assert stage1_refs["prompt_rewrite_system.txt"]["used"] is True
    assert stage1_refs["prompt_normalize_system.txt (보수 모드)"]["used"] is False

    # LLM Query Plan·정규화 매칭·디멘션·집합식으로 실제 기여한 참조를 구분한다.
    assert stage3_refs["query_plan_system.txt"]["used"] is True
    assert stage3_refs["query_plan_user.txt"]["used"] is True
    assert stage3_refs["normalization_rules.sample.json"]["used"] is True
    assert stage3_refs["business_policies.sample.json"]["used"] is False
    assert stage5_refs["dimension_catalog.sample.json"]["used"] is True
    assert stage5_refs["member_target_filters.json"]["used"] is True
    assert all(ref["used"] is True for ref in stage6_refs.values())


def test_partial_trace_stages_keep_refs():
    trace = g.build_partial_retrieve_trace("x", {"prompt_normalization": 10.0}, "boom")
    # 부분 트레이스의 각 단계도 참조 자산을 유지한다(오류 화면에서도 근거를 보여줌).
    assert trace["stages"][2]["refs"]


def test_stage3_shows_prompt_to_query_plan_json():
    # 3단계는 원문 → 계획 문장 → Query Plan JSON 변환을 그대로 보여준다.
    trace = g.build_retrieve_trace(_result())
    joined = "\n".join(trace["stages"][2].get("details", []))
    assert "계획 문장" in joined
    assert "Query Plan JSON" in joined
    assert '"intent"' in joined and "find_user_segment" in joined
    assert '"lifecycle"' in joined  # target_user 슬롯이 JSON 으로 노출


def test_stage3_shows_actual_llm_prompt_when_llm_used():
    trace = g.build_retrieve_trace(_result())
    joined = "\n".join(trace["stages"][2].get("details", []))
    assert "실제 LLM 프롬프트" in joined
    assert "user 프롬프트" in joined and "[User Query]" in joined
    assert "LLM 응답" in joined


def test_stage3_honest_badge_when_rules_parser():
    # 규칙 파싱이면 모델/프롬프트 배지를 떼고 method=규칙, 'LLM 미사용' 을 명시한다.
    res = _result()
    res["query_plan"]["parser"] = {"type": "rules"}
    res["llm_query_plan_prompt"] = None
    s3 = g.build_retrieve_trace(res)["stages"][2]
    assert s3["method"] == "규칙"
    assert all(r["kind"] not in ("모델", "프롬프트") for r in s3["refs"])
    assert any("LLM 미사용" in line for line in s3["details"])


def test_stage8_shows_ast_and_generation_mechanism():
    res = _result()
    res["sql_result"]["generation_source"] = "sql_template"
    trace = g.build_retrieve_trace(res)
    s8 = trace["stages"][7]
    assert "SelectAst" in s8["tech_name"]                      # AST 사용 명시
    assert "결정론 조건빌더" in (s8.get("summary") or "")       # 빌더 방식 표기
    assert any(r["kind"] == "코드" for r in s8["refs"])         # sql_ast.py 참조

    res2 = _result()
    res2["sql_result"]["generation_source"] = "llm_generated"
    s8b = g.build_retrieve_trace(res2)["stages"][7]
    assert "LLM 폴백" in (s8b.get("summary") or "")             # LLM 폴백 방식 구분


def test_set_expression_surfaced_at_stage_6():
    trace = g.build_retrieve_trace(_result())
    stage6 = trace["stages"][5]
    assert stage6["name"] == "집합식 파싱" and stage6["status"] == "info"
    assert any("세그먼트 집합식" in line for line in stage6.get("details", []))


def test_graph_hits_at_stage_7_and_success_at_9():
    trace = g.build_retrieve_trace(_result())
    assert trace["stages"][6]["hits"]  # 7단계에 그래프 노드
    assert trace["stages"][8]["status"] == "ok"  # 9단계 안전 검증 통과


def test_stage_10_pending_until_execution_applied():
    trace = g.build_retrieve_trace(_result())
    assert trace["stages"][9]["status"] == "skipped"  # 실행 전엔 대기
    g.apply_execution_to_trace(trace, {"is_success": True, "targeting_result": {"target_customer_count": 718}})
    stage10 = trace["stages"][9]
    assert stage10["status"] == "ok" and "718" in stage10["summary"]
    assert all(ref["used"] is True for ref in stage10["refs"])


def test_failure_reflected_at_stage_9():
    res = _result()
    res["sql_result"].update({"sql": None, "is_success": False, "failure_reason": "sql_guard_failed", "candidates": []})
    res["api_response"] = {"status": "no_verified_sql", "sql": None, "message": "fail"}
    trace = g.build_retrieve_trace(res)
    stage9 = trace["stages"][8]
    assert stage9["status"] == "fail"
    assert "SQL 안전 검증" in stage9["summary"]  # failure_stage 라벨 반영


def test_trace_diagnoses_reference_mapping_gap_from_sql_result():
    res = _result()
    res["sql_result"].update(
        {
            "sql": None,
            "is_success": False,
            "failure_reason": "real_db_unsupported_conditions",
            "unsupported_condition_labels": ["관심 브랜드"],
        }
    )
    trace = g.build_retrieve_trace(res)

    diagnosis = trace["failure_diagnosis"]
    assert diagnosis["category"] == "reference_data_gap"
    assert diagnosis["confidence"] == "high"
    assert "관심 브랜드" in "\n".join(diagnosis["evidence"])


def test_trace_diagnoses_guard_failure_as_implementation_review():
    res = _result()
    res["sql_result"].update(
        {"sql": None, "is_success": False, "failure_reason": "sql_guard_failed"}
    )
    trace = g.build_retrieve_trace(res)

    diagnosis = trace["failure_diagnosis"]
    assert diagnosis["category"] == "implementation_or_policy_review"
    assert diagnosis["confidence"] == "high"


def test_partial_trace_diagnoses_invalid_reference_json():
    trace = g.build_partial_retrieve_trace("x", {}, "target_sql_trace_failed:JSONDecodeError")

    diagnosis = trace["failure_diagnosis"]
    assert diagnosis["category"] == "reference_data_error"
    assert diagnosis["label"] == "참조 JSON 형식 오류"


def test_execution_failure_diagnosis_overrides_successful_sql_trace():
    trace = g.build_retrieve_trace(_result())
    g.apply_execution_to_trace(
        trace,
        {
            "is_success": False,
            "mode": "postgres_read_only",
            "failure_reason": "postgres_execution_failed",
            "error": "ConnectionError: database unavailable",
        },
    )

    assert trace["failure_diagnosis"]["category"] == "infrastructure_or_configuration"


def test_partial_trace_marks_failing_stage_and_skips_rest():
    # 정규화+분리까지 완료, Query Plan 에서 실패 → 3단계 fail, 이후 skipped.
    trace = g.build_partial_retrieve_trace("x", {"prompt_normalization": 10.0, "prompt_scopes": 2.0}, "boom")
    assert trace["partial"] is True
    statuses = {s["step"]: s["status"] for s in trace["stages"]}
    assert statuses[1] == "ok" and statuses[2] == "ok"
    assert statuses[3] == "fail"
    assert all(statuses[step] == "skipped" for step in range(4, 11))
    assert "boom" in trace["stages"][2]["details"][0]

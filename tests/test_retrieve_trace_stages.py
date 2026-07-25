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


def test_partial_trace_stages_keep_refs():
    trace = g.build_partial_retrieve_trace("x", {"prompt_normalization": 10.0}, "boom")
    # 부분 트레이스의 각 단계도 참조 자산을 유지한다(오류 화면에서도 근거를 보여줌).
    assert trace["stages"][2]["refs"]


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


def test_failure_reflected_at_stage_9():
    res = _result()
    res["sql_result"].update({"sql": None, "is_success": False, "failure_reason": "sql_guard_failed", "candidates": []})
    res["api_response"] = {"status": "no_verified_sql", "sql": None, "message": "fail"}
    trace = g.build_retrieve_trace(res)
    stage9 = trace["stages"][8]
    assert stage9["status"] == "fail"
    assert "SQL 안전 검증" in stage9["summary"]  # failure_stage 라벨 반영


def test_partial_trace_marks_failing_stage_and_skips_rest():
    # 정규화+분리까지 완료, Query Plan 에서 실패 → 3단계 fail, 이후 skipped.
    trace = g.build_partial_retrieve_trace("x", {"prompt_normalization": 10.0, "prompt_scopes": 2.0}, "boom")
    assert trace["partial"] is True
    statuses = {s["step"]: s["status"] for s in trace["stages"]}
    assert statuses[1] == "ok" and statuses[2] == "ok"
    assert statuses[3] == "fail"
    assert all(statuses[step] == "skipped" for step in range(4, 11))
    assert "boom" in trace["stages"][2]["details"][0]

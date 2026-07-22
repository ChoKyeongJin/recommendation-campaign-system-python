"""타겟팅 SQL 신뢰도 스코어러(confidence.py) 회귀 테스트.

결정론 산정 규약을 고정한다:
  - 문서/스키마에서 확인된 조건은 verified(confirmed) 이고 근거에 파일/rule_id 출처가 붙는다.
  - free-text 상품 LIKE 등 확정 불가 조건은 inferred + 경고 + 낮은 점수.
  - 사용자 요청에 없는데 주입된 기본 게이트(정상 회원)는 경고로 표시.
네트워크/DB 없이 plain dict 입력만으로 검증한다.

실행: docker compose exec -w /app -e PYTHONPATH=/app api pytest tests/test_confidence_scoring.py -q
"""

import confidence as c


def _candidate(sql: str, *, source="sql_template", probe=None, coverage=None):
    return {
        "sql": sql,
        "source": source,
        "validation": {"is_valid": True, "issues": [], "tables": ["CRM_MB_BASEINFO"]},
        "coverage": coverage or {"is_satisfied": True, "required_count": 2, "matched_count": 2, "missing_conditions": []},
        "unmentioned_conditions": {"is_satisfied": True},
        "cardinality_probe": probe,
        "dropped_conditions": [],
    }


def test_verified_conditions_cite_sources_and_score_high():
    query_plan = {
        "intent": "find_user_segment",
        "target_user": {"gender": "female", "lifecycle": ["vip"]},
        "exclude": {},
        "matched_terms": [
            {"canonical": "female", "rule_id": "gender_female", "matched_text": "여성"},
            {"canonical": "vip", "rule_id": "vip", "matched_text": "VIP"},
        ],
    }
    sql = ("SELECT DISTINCT B.MEMBER_NO FROM CRM_MB_BASEINFO B "
           "WHERE B.GENDER_CD = 'GENDER_CD.FEMALE' AND B.EMART_GRADE_CD = 'MEM_GRADE_CD.VIP' "
           "AND B.MEMBER_STATE_CD = 'MEMBER_STATE_CD.NORMAL'")
    probe = {"from_clause": "CRM_MB_BASEINFO B", "predicates": [
        {"sql": "B.GENDER_CD = 'GENDER_CD.FEMALE'", "injected_default": False},
        {"sql": "B.MEMBER_STATE_CD = 'MEMBER_STATE_CD.NORMAL'", "injected_default": True},
    ]}
    result = c.score_targeting_confidence(query_plan, _candidate(sql, probe=probe))

    assert result["overall_score"] >= 85
    assert result["level"] == "높음"
    by_key = {cond["key"]: cond for cond in result["conditions"]}

    gender = by_key["gender"]
    assert gender["verified"] is True
    refs = " ".join(ev["ref"] for ev in gender["evidence"])
    assert "member_target_filters.json" in refs
    assert "schema_catalog.json" in refs
    assert "normalization_rules" in refs  # 요청 문구 출처(rule_id)

    # 주입된 정상회원 게이트는 경고로 표시된다.
    gate = by_key["active_state_gate"]
    assert any("기본 정책" in w for w in gate["warnings"])


def test_free_text_purchase_is_inferred_and_warned():
    query_plan = {
        "intent": "find_user_segment",
        "target_user": {"purchase_object": "기저귀"},
        "exclude": {},
        "matched_terms": [],
    }
    sql = ("SELECT DISTINCT B.MEMBER_NO FROM CRM_SL_ORDERDETAILMALL D "
           "JOIN CRM_CM_PRODUCT P ON D.PRODUCT_ID = P.PRODUCT_ID "
           "JOIN CRM_MB_BASEINFO B ON D.MEMBER_NO = B.MEMBER_NO WHERE P.PRODUCT_NAME LIKE N'%기저귀%'")
    result = c.score_targeting_confidence(query_plan, _candidate(sql))

    cond = result["conditions"][0]
    assert cond["verified"] is False
    assert any(ev["kind"] == "inferred" for ev in cond["evidence"])
    assert cond["warnings"], "free-text 조건은 경고가 있어야 한다"
    assert result["overall_score"] < 85


def test_llm_generated_source_caps_confidence():
    query_plan = {"intent": "find_user_segment", "target_user": {"gender": "female"}, "exclude": {},
                  "matched_terms": [{"canonical": "female", "rule_id": "gender_female", "matched_text": "여성"}]}
    sql = "SELECT DISTINCT B.MEMBER_NO FROM CRM_MB_BASEINFO B WHERE B.GENDER_CD = 'GENDER_CD.FEMALE'"
    result = c.score_targeting_confidence(query_plan, _candidate(sql, source="llm_generated"))
    assert result["overall_score"] <= 75
    assert any("LLM" in w for w in result["warnings"])


def test_render_report_has_required_sections():
    query_plan = {"intent": "find_user_segment", "target_user": {"gender": "female"}, "exclude": {},
                  "matched_terms": [{"canonical": "female", "rule_id": "gender_female", "matched_text": "여성"}]}
    sql = "SELECT DISTINCT B.MEMBER_NO FROM CRM_MB_BASEINFO B WHERE B.GENDER_CD = 'GENDER_CD.FEMALE'"
    result = c.score_targeting_confidence(query_plan, _candidate(sql))
    report = c.render_confidence_report(result)
    assert "전체 신뢰도:" in report
    assert "신뢰도 수준:" in report
    assert "조건별 근거:" in report


def test_render_markdown_structure():
    query_plan = {
        "intent": "find_user_segment",
        "target_user": {"gender": "female"},
        "exclude": {},
        "matched_terms": [{"canonical": "female", "rule_id": "gender_female", "matched_text": "여성"}],
    }
    sql = ("SELECT DISTINCT B.MEMBER_NO FROM CRM_MB_BASEINFO B "
           "WHERE B.GENDER_CD = 'GENDER_CD.FEMALE' AND B.MEMBER_STATE_CD = 'MEMBER_STATE_CD.NORMAL'")
    probe = {"from_clause": "CRM_MB_BASEINFO B", "predicates": [
        {"sql": "B.GENDER_CD = 'GENDER_CD.FEMALE'", "injected_default": False},
        {"sql": "B.MEMBER_STATE_CD = 'MEMBER_STATE_CD.NORMAL'", "injected_default": True},
    ]}
    result = c.score_targeting_confidence(query_plan, _candidate(sql, probe=probe))
    md = c.render_confidence_markdown(result)

    # 헤더(점수·수준 배지) + 축 점수 표 + 조건 리스트 + 근거 코드참조 + 경고 콜아웃
    assert md.startswith("### ")
    assert "타겟팅 신뢰도" in md and f"{result['overall_score']}점" in md
    assert "| 평가 축 | 점수 |" in md
    assert "**조건별 신뢰도·근거**" in md
    assert "**성별: 여성**" in md
    assert "`member_target_filters.json: eq_filters[female]`" in md  # 출처를 코드로 표기
    assert "✅ 확인" in md  # verified 배지
    assert "⚠️ **경고**" in md  # 주입 게이트 경고


def test_render_markdown_marks_inferred_free_text():
    query_plan = {"intent": "find_user_segment", "target_user": {"purchase_object": "기저귀"}, "exclude": {}, "matched_terms": []}
    sql = "SELECT DISTINCT B.MEMBER_NO FROM CRM_SL_ORDERDETAILMALL D JOIN CRM_MB_BASEINFO B ON D.MEMBER_NO = B.MEMBER_NO WHERE P.PRODUCT_NAME LIKE N'%기저귀%'"
    result = c.score_targeting_confidence(query_plan, _candidate(sql))
    md = c.render_confidence_markdown(result)
    assert "🟠 추론" in md  # verified=False 배지
    assert "🤖 추론" in md  # inferred 근거

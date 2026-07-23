"""등급 서열 임계(grade threshold) 타겟 회귀.

배경: '골드 등급 이상'처럼 등급명 뒤 '이상/이하/초과/미만'이 붙는 서열 조건이 파싱되지 않아
SQL 에서 등급 조건이 통째로 빠지거나(영문 'GOLD' 미매칭), 정규화가 경계 등급만 등가(`= GOLD`)로
잡아 '이상' 의미가 무시됐다.

고정 내용:
  - '<등급> 이상/이하'는 rank(welcome1<family2<silver3<gold4<vip5)로 확장돼 같은 컬럼
    EMART_GRADE_CD IN (...) 으로 컴파일된다(2개 이상). 결과가 1개면 `=`.
  - 이상/이하는 경계 등급 포함(>=,<=), 초과/미만은 경계 제외(>,<).
  - 임계가 등급 조건을 소유한다: 정규화가 넣은 경계 등급 등가도 걷어내고 계산 집합으로 교체
    (초과/미만에서 경계 등급이 새지 않게).
  - 임계 표지가 없는 단일 등급('골드 등급 회원')은 기존 단일 등가 그대로.
  - 등급명은 한글 synonym('골드')·영문 코드값('GOLD')·canonical 접미어 제거형('gold') 모두 매칭.
  - 서열/값은 member_target_filters.json eq_filters grade 카테고리(rank/value)가 소유.

실행(컨테이너): docker compose exec -w /app -e PYTHONPATH=/app api pytest tests/test_grade_threshold_targeting.py -q
"""

import graph_rag as g


def _plan(query: str) -> dict:
    plan = g.build_query_plan(query, parser="rules")
    g._promote_unknown_intent_for_target_signal(plan)
    return plan


def _grade_sql(query: str) -> str:
    cand = g.build_member_targets_sql_candidate(_plan(query))
    assert cand is not None, query
    line = [row.strip() for row in cand["sql"].splitlines() if "EMART_GRADE_CD" in row and "WHERE" in row.upper() or ("EMART_GRADE_CD" in row and "IN (" in row) or ("EMART_GRADE_CD =" in row)]
    return " ".join(line)


def test_gold_or_above_expands_to_in():
    plan = _plan("골드 등급 이상 회원")
    assert plan["target_user"]["lifecycle"] == ["gold_grade", "vip"]
    compiled = g.compile_member_target_conditions(plan)
    joined = " AND ".join(compiled["predicates"])
    assert "B.EMART_GRADE_CD IN ('MEM_GRADE_CD.GOLD', 'MEM_GRADE_CD.VIP')" in joined


def test_english_grade_token_matches():
    # 영문 대문자 'GOLD' 도 코드값 토큰으로 매칭된다(한글 synonym 밖).
    plan = _plan("GOLD 이상 등급이면서 최근 로그인한 지 30일 이내인 회원을 찾아줘")
    assert "gold_grade" in plan["target_user"]["lifecycle"]
    assert "vip" in plan["target_user"]["lifecycle"]
    compiled = g.compile_member_target_conditions(plan)
    joined = " AND ".join(compiled["predicates"])
    assert "B.EMART_GRADE_CD IN ('MEM_GRADE_CD.GOLD', 'MEM_GRADE_CD.VIP')" in joined
    # 최근 로그인 창과 AND 결합.
    assert "B.LAST_LOGIN_DATE >=" in joined


def test_silver_or_above_three_grades():
    compiled = g.compile_member_target_conditions(_plan("실버 이상 회원 보여줘"))
    joined = " AND ".join(compiled["predicates"])
    assert "B.EMART_GRADE_CD IN ('MEM_GRADE_CD.SILVER', 'MEM_GRADE_CD.GOLD', 'MEM_GRADE_CD.VIP')" in joined


def test_vip_or_above_is_single_equality():
    # 최상위 등급 이상은 자기 하나 → IN 이 아니라 등가.
    compiled = g.compile_member_target_conditions(_plan("VIP 이상 회원"))
    joined = " AND ".join(compiled["predicates"])
    assert "B.EMART_GRADE_CD = 'MEM_GRADE_CD.VIP'" in joined


def test_exclusive_over_excludes_boundary():
    # '골드 초과'는 골드 제외 → VIP 만. 정규화가 넣은 골드 등가가 새지 않아야 한다.
    plan = _plan("골드 초과 회원")
    assert plan["target_user"]["lifecycle"] == ["vip"]
    compiled = g.compile_member_target_conditions(plan)
    joined = " AND ".join(compiled["predicates"])
    assert "B.EMART_GRADE_CD = 'MEM_GRADE_CD.VIP'" in joined
    assert "MEM_GRADE_CD.GOLD" not in joined


def test_at_or_below_includes_boundary():
    compiled = g.compile_member_target_conditions(_plan("실버 이하 등급 회원"))
    joined = " AND ".join(compiled["predicates"])
    assert "B.EMART_GRADE_CD IN ('MEM_GRADE_CD.WELCOME', 'MEM_GRADE_CD.FAMILY', 'MEM_GRADE_CD.SILVER')" in joined


def test_exclusive_under_excludes_boundary():
    plan = _plan("골드 미만 회원")
    assert "gold_grade" not in plan["target_user"]["lifecycle"]
    compiled = g.compile_member_target_conditions(plan)
    joined = " AND ".join(compiled["predicates"])
    assert "MEM_GRADE_CD.GOLD" not in joined
    assert "B.EMART_GRADE_CD IN ('MEM_GRADE_CD.WELCOME', 'MEM_GRADE_CD.FAMILY', 'MEM_GRADE_CD.SILVER')" in joined


def test_single_grade_without_threshold_is_equality():
    # 임계 표지 없는 단일 등급은 확장하지 않는다.
    plan = _plan("골드 등급 회원")
    assert plan["target_user"]["lifecycle"] == ["gold_grade"]
    compiled = g.compile_member_target_conditions(plan)
    joined = " AND ".join(compiled["predicates"])
    assert "B.EMART_GRADE_CD = 'MEM_GRADE_CD.GOLD'" in joined


def test_amount_threshold_not_misread_as_grade():
    # 등급명이 없는 '이상'(금액/횟수)은 등급 임계로 오탐하지 않는다.
    plan = _plan("누적 구매 금액 10만원 이상 회원")
    grade_canonicals = {"welcome_grade", "family_grade", "silver_grade", "gold_grade", "vip"}
    assert not (grade_canonicals & set(plan["target_user"].get("lifecycle") or []))

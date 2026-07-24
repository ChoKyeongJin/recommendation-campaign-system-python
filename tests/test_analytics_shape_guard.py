"""집계·grain 정합성 가드(validate_analytics_shape) 회귀.

배경: 허용목록·조인키 가드는 '문법은 그럴듯하나 집계/grain 이 틀린' SQL(특히 LLM 폴백)을 못 잡는다.
이 가드는 괄호 내용을 지운 outer 스켈레톤만 보고, 서브쿼리(EXISTS/IN)·함수 인자는 오탐 없이 제외한
채 흔한 구조 오류를 잡는다. 확실한 오류만 error(후보 탈락), 의심 형태는 warning(고지)로 둔다.

핵심 계약: 결정론 빌더가 만든 정상 SQL 에는 error 를 내지 않는다(오탐 0).

실행(컨테이너): docker compose exec -w /app -e PYTHONPATH=/app api pytest tests/test_analytics_shape_guard.py -q
"""

import pytest

import graph_rag as g
from sql_guard import validate_analytics_shape


def _codes(sql: str, severity: str) -> list[str]:
    return [i["code"] for i in validate_analytics_shape(sql)["issues"] if i["severity"] == severity]


# 결정론 빌더가 실제로 만드는 정상 SQL 프롬프트 — error 가 하나도 없어야 한다(오탐 0).
_CLEAN_PROMPTS = [
    "서울 거주 30대 여성",
    "첫 구매 고객",
    "한 번도 구매 안 한 회원",
    "최근 90일 누적 구매 금액 10만원 이상 고객",
    "생수를 구매한 고객",
    "장바구니에 담고 아직 안 산 회원",
    "장바구니에 3개 이상 상품을 담은 회원",
    "쿠폰을 사용한 여성 회원",
    "구매횟수가 많은 고객 상위 100명",
    "많이 산 사람 상위 50명",
]


@pytest.mark.parametrize("prompt", _CLEAN_PROMPTS)
def test_deterministic_builder_sql_has_no_errors(prompt):
    plan = g.build_query_plan(prompt, parser="rules")
    g._promote_unknown_intent_for_target_signal(plan)
    candidate = g.build_sql_template_candidate(plan)
    assert candidate is not None, f"{prompt!r}: SQL 미생성"
    result = validate_analytics_shape(candidate["sql"])
    errors = [i for i in result["issues"] if i["severity"] == "error"]
    assert errors == [], f"{prompt!r}: 오탐 {errors}"


def test_subquery_aggregates_are_not_flagged():
    # 집계가 서브쿼리 안에만 있으면 outer 는 깨끗하다 — 오탐 금지(핵심 계약).
    sql = (
        "SELECT DISTINCT B.MEMBER_NO AS CUST_ID FROM CRM_MB_BASEINFO B "
        "WHERE B.MEMBER_ID IN (SELECT M FROM T GROUP BY M HAVING SUM(AMT) >= 100000) "
        "AND EXISTS (SELECT 1 FROM O WHERE O.M = B.MEMBER_NO)"
    )
    assert validate_analytics_shape(sql) == {"is_valid": True, "issues": []}


def test_aggregate_in_where_is_error():
    sql = "SELECT B.MEMBER_NO FROM CRM_MB_BASEINFO B WHERE COUNT(B.X) > 5"
    assert "agg_in_where" in _codes(sql, "error")
    assert validate_analytics_shape(sql)["is_valid"] is False


def test_aggregate_with_bare_column_without_group_by_is_error():
    sql = "SELECT B.MEMBER_NO, SUM(O.AMT) FROM CRM_MB_BASEINFO B JOIN T O ON O.M = B.MEMBER_NO"
    assert "agg_without_group_by" in _codes(sql, "error")


def test_single_aggregate_without_bare_column_is_ok():
    # 비집계 컬럼이 없으면 GROUP BY 없이도 정상(단일 집계).
    sql = "SELECT COUNT(*) FROM CRM_MB_BASEINFO B"
    assert validate_analytics_shape(sql)["is_valid"] is True
    assert _codes(sql, "error") == []


def test_aggregate_with_group_by_is_ok():
    sql = "SELECT B.MEMBER_NO, COUNT(*) FROM T O JOIN CRM_MB_BASEINFO B ON O.M = B.MEMBER_NO GROUP BY B.MEMBER_NO"
    assert _codes(sql, "error") == []


def test_distinct_with_aggregate_is_warning():
    sql = (
        "SELECT DISTINCT B.MEMBER_NO, COUNT(O.ID) FROM CRM_MB_BASEINFO B "
        "JOIN T O ON O.M = B.MEMBER_NO GROUP BY B.MEMBER_NO"
    )
    assert "distinct_with_aggregate" in _codes(sql, "warning")
    assert validate_analytics_shape(sql)["is_valid"] is True  # warning 은 후보를 탈락시키지 않는다


def test_join_without_grain_control_is_warning():
    sql = "SELECT B.MEMBER_NO FROM CRM_MB_BASEINFO B JOIN ORDERS O ON O.M = B.MEMBER_NO WHERE O.AMT > 0"
    assert "join_without_grain_control" in _codes(sql, "warning")


def test_exists_subquery_join_does_not_trigger_grain_warning():
    # EXISTS 서브쿼리는 outer JOIN 이 아니므로 grain 경고 대상이 아니다(빌더의 표준 패턴).
    sql = (
        "SELECT DISTINCT B.MEMBER_NO FROM CRM_MB_BASEINFO B "
        "WHERE EXISTS (SELECT 1 FROM O WHERE O.M = B.MEMBER_NO)"
    )
    assert _codes(sql, "warning") == []


def test_empty_or_blank_sql_is_valid():
    assert validate_analytics_shape("") == {"is_valid": True, "issues": []}
    assert validate_analytics_shape("   ") == {"is_valid": True, "issues": []}

"""쿼리 성능 튜닝(정적 자문) 회귀 — analyze_query_performance.

생성된 타겟팅 SQL 의 실행 함정(선행 와일드카드 LIKE, 캐스트 조인, 컬럼 함수 래핑, NOT EXISTS 안티조인)을
정적으로 진단하고 권장 인덱스를 제안한다. SQL 을 바꾸지 않는 비차단 자문이다.

실행(컨테이너): docker compose exec -w /app -e PYTHONPATH=/app api pytest tests/test_query_performance_tuning.py -q
"""

import pytest

from sql_guard import analyze_query_performance


def _codes(sql: str) -> set[str]:
    return {f["code"] for f in analyze_query_performance(sql)["findings"]}


def _index_ddls(sql: str) -> list[str]:
    return [idx["ddl"] for idx in analyze_query_performance(sql)["recommended_indexes"]]


MEMBER_CAMPAIGN_SQL = (
    "SELECT DISTINCT B.MEMBER_NO AS CUST_ID FROM CRM_MB_BASEINFO B "
    "WHERE B.GENDER_CD = 'GENDER_CD.FEMALE' AND B.AGE >= 30 AND B.AGE <= 39 "
    "AND (B.LAST_LOGIN_DATE IS NOT NULL AND LEN(B.LAST_LOGIN_DATE) = 8 "
    "AND B.LAST_LOGIN_DATE >= CONVERT(CHAR(8), DATEADD(DAY, -30, GETDATE()), 112)) "
    "AND NOT EXISTS (SELECT 1 FROM MCS_CAMP_MBR_RSPN_FT R WHERE TRY_CAST(R.MBR_NO AS BIGINT) = B.MEMBER_NO AND R.BUY_RSPN_YN = 'Y') "
    "AND B.SIDO IN ('서울') AND B.MEMBER_STATE_CD = 'MEMBER_STATE_CD.NORMAL'"
)

PURCHASE_HISTORY_SQL = (
    "SELECT DISTINCT B.MEMBER_NO AS CUST_ID "
    "FROM CRM_SL_ORDERDETAILMALL D "
    "INNER JOIN CRM_CM_PRODUCT P ON D.PRODUCT_ID = P.PRODUCT_ID "
    "INNER JOIN CRM_MB_BASEINFO B ON D.MEMBER_NO = B.MEMBER_NO "
    "WHERE (P.CATEGORY LIKE N'%기저귀%' OR P.PRODUCT_NAME LIKE N'%기저귀%') "
    "AND B.GENDER_CD = 'GENDER_CD.FEMALE' AND B.SIDO IN ('서울') AND B.MEMBER_STATE_CD = 'MEMBER_STATE_CD.NORMAL'"
)


def test_detects_cast_join_and_anti_join():
    codes = _codes(MEMBER_CAMPAIGN_SQL)
    assert "cast_in_join_predicate" in codes
    assert "anti_join_subquery" in codes
    assert "function_on_filter_column" in codes  # LEN(B.LAST_LOGIN_DATE)


def test_recommends_index_for_anti_join_correlated_key():
    ddls = " ".join(_index_ddls(MEMBER_CAMPAIGN_SQL))
    # 안티조인 상관 컬럼(MBR_NO) + 로컬 필터(BUY_RSPN_YN) 복합 인덱스 제안.
    assert "ON MCS_CAMP_MBR_RSPN_FT (MBR_NO, BUY_RSPN_YN)" in ddls


def test_recommends_member_composite_index():
    ddls = " ".join(_index_ddls(MEMBER_CAMPAIGN_SQL))
    assert "ON CRM_MB_BASEINFO (" in ddls
    # 동등/IN 컬럼이 선행, 범위(AGE)가 후행.
    idx = next(i for i in analyze_query_performance(MEMBER_CAMPAIGN_SQL)["recommended_indexes"] if i["table"] == "CRM_MB_BASEINFO")
    assert idx["columns"][-1] == "AGE"
    assert "GENDER_CD" in idx["columns"] and "SIDO" in idx["columns"]
    # 함수로 감싼 LAST_LOGIN_DATE 는 선행 인덱스 후보에서 제외된다(non-sargable).
    assert "LAST_LOGIN_DATE" not in idx["columns"]


def test_detects_leading_wildcard_like():
    codes = _codes(PURCHASE_HISTORY_SQL)
    assert "leading_wildcard_like" in codes


def test_join_query_targets_member_table_not_from_alias():
    # 조인 쿼리라도 필터가 가장 많이 걸린 구동 테이블(CRM_MB_BASEINFO)에 복합 인덱스를 제안한다.
    idxs = analyze_query_performance(PURCHASE_HISTORY_SQL)["recommended_indexes"]
    assert any(i["table"] == "CRM_MB_BASEINFO" for i in idxs)


def test_no_false_positive_on_clean_sql():
    sql = "SELECT B.MEMBER_NO FROM CRM_MB_BASEINFO B WHERE B.GENDER_CD = 'M'"
    result = analyze_query_performance(sql)
    assert result["findings"] == []


def test_sargable_date_range_not_flagged_as_function():
    # 상수 쪽(GETDATE)을 CONVERT 로 감싼 sargable 범위는 함수-온-컬럼으로 오탐하지 않는다.
    sql = ("SELECT B.MEMBER_NO FROM CRM_MB_BASEINFO B "
           "WHERE B.LAST_LOGIN_DATE >= CONVERT(CHAR(8), DATEADD(DAY, -30, GETDATE()), 112)")
    assert "function_on_filter_column" not in _codes(sql)


def test_empty_sql_is_safe():
    assert analyze_query_performance("") == {"findings": [], "recommended_indexes": []}
    assert analyze_query_performance("   ") == {"findings": [], "recommended_indexes": []}

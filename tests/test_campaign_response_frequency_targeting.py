"""캠페인 반응 '횟수' 임계값 타겟팅 회귀 (campaign_response_frequency).

배경: "최근 3개월 캠페인 중 두 번 이상 반응한 회원"이 캠페인 반응 팩트가 아니라 주문 집계
(CRM_SL_ORDERHEADERMALL HAVING COUNT(DISTINCT ORDER_ID) >= 2)로 새던 문제를 고쳤다.
'캠페인'+'반응'+횟수 임계어가 함께 오면 반응 팩트(MCS_CAMP_MBR_RSPN_FT)를 캠페인 마스터
(Z_CAMPAIGN)와 조인해 회원별 반응 캠페인 수를 세고(HAVING COUNT(DISTINCT 캠페인)) 임계값과
비교한다. '최근 N개월'은 반응 팩트에 범용 반응일자가 없어 Z_CAMPAIGN.CAMP_SDATE 창으로 건다.

실행(컨테이너): docker compose exec -w /app -e PYTHONPATH=/app api pytest tests/test_campaign_response_frequency_targeting.py -q
"""

import pytest

import graph_rag as g


def _plan(query: str) -> dict:
    plan = g.build_query_plan(query, parser="rules")
    g._promote_unknown_intent_for_target_signal(plan)
    return plan


def _sql(query: str) -> str:
    cand = g.build_sql_template_candidate(_plan(query))
    assert cand is not None, f"{query!r}: SQL 미생성"
    return cand["sql"]


# ── 파싱: '캠페인'+'반응'+횟수 임계어일 때만 campaign_response_frequency 로 잡힌다 ──────────
@pytest.mark.parametrize("query,count,operator", [
    ("최근 3개월 캠페인 중 두 번 이상 반응한 회원만 조회해줘", 2, ">="),
    ("캠페인에 2회 이상 반응한 회원", 2, ">="),
    ("최근 6개월 캠페인 세 번 이상 반응한 고객", 3, ">="),
    ("캠페인에 3회 초과 반응한 회원", 3, ">"),
])
def test_frequency_parsed(query, count, operator):
    freq = _plan(query)["target_user"].get("campaign_response_frequency")
    assert isinstance(freq, dict), query
    assert freq["count"] == count
    assert freq["operator"] == operator


def test_window_parsed_to_days():
    freq = _plan("최근 3개월 캠페인 중 두 번 이상 반응한 회원")["target_user"]["campaign_response_frequency"]
    assert freq["window_days"] == 90  # 3개월 = 90일


def test_no_window_when_absent():
    freq = _plan("캠페인에 두 번 이상 반응한 회원")["target_user"]["campaign_response_frequency"]
    assert freq["window_days"] is None


def test_purchase_count_not_stolen():
    # '구매 2회 이상'(주문 집계)은 캠페인 반응 횟수로 오인하면 안 된다('캠페인'/'반응' 문맥 부재).
    plan = _plan("구매를 두 번 이상 한 회원")
    assert plan["target_user"].get("campaign_response_frequency") is None


# ── SQL: 반응 팩트⨝캠페인 마스터 집계로 컴파일된다 ─────────────────────────────────────
def test_frequency_sql_shape():
    sql = _sql("최근 3개월 캠페인 중 두 번 이상 반응한 회원만 조회해줘")
    # 주문 테이블이 아니라 반응 팩트로 추출한다.
    assert "MCS_CAMP_MBR_RSPN_FT R" in sql
    assert "CRM_SL_ORDERHEADERMALL" not in sql
    # 캠페인 마스터 조인 + 대상군 + 유효 캠페인 + 최근 3개월 창.
    assert "INNER JOIN Z_CAMPAIGN ZC" in sql
    assert "R.CGRP_TYPE_CD = 'T'" in sql
    assert "ISNULL(ZC.CANCEL_YN, 'N') = 'N'" in sql
    assert "ZC.CAMP_SDATE >= CONVERT(CHAR(8), DATEADD(DAY, -90, GETDATE()), 112)" in sql
    # 반응 정의(오퍼/구매 반응) + 회원별 반응 캠페인 수 >= 2.
    assert "R.OFFR_RSPN_YN = 'Y' OR R.BUY_RSPN_YN = 'Y'" in sql
    assert "HAVING COUNT(DISTINCT CONCAT(R.CAMP_ID, ':', R.CAMP_EXEC_NO)) >= 2" in sql
    # MBR_NO(nvarchar)↔MEMBER_NO(bigint) 캐스트 조인.
    assert "TRY_CAST(O.MBR_NO AS BIGINT) = B.MEMBER_NO" in sql


def test_frequency_combines_with_member_attribute():
    sql = _sql("최근 3개월 캠페인 두 번 이상 반응한 VIP 여성")
    assert "HAVING COUNT(DISTINCT CONCAT(R.CAMP_ID, ':', R.CAMP_EXEC_NO)) >= 2" in sql
    assert "B.GENDER_CD = 'GENDER_CD.FEMALE'" in sql
    assert "B.EMART_GRADE_CD = 'MEM_GRADE_CD.VIP'" in sql


def test_builder_registered_before_order_count():
    builders = g._sql_target_builders()
    assert g.build_campaign_response_frequency_targets_sql_candidate in builders
    assert builders.index(g.build_campaign_response_frequency_targets_sql_candidate) < builders.index(
        g.build_order_count_targets_sql_candidate
    )


def test_config_loaded_from_registry():
    # campaign_response_targets 가 기본값 화이트리스트에 있어 JSON 의 campaign_join/날짜 컬럼이 로드된다.
    cfg = g._MEMBER_TARGET_FILTERS.get("campaign_response_targets", {})
    assert cfg.get("campaign_join", {}).get("table") == "Z_CAMPAIGN"
    assert cfg.get("campaign_date_column") == "CAMP_SDATE"

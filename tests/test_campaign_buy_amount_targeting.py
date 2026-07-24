"""캠페인 '귀속 구매금액' 임계값 타겟팅 회귀 (campaign_buy_amount).

배경: "캠페인 구매금액이 20만원 이상인 회원"이 전 생애 주문 합(CRM_SL_ORDERHEADERMALL
HAVING SUM(PAYMENT_AMT)) + 구매반응 EXISTS 로 컴파일돼 캠페인과 금액이 연결되지 않았다.
반응 팩트(MCS_CAMP_MBR_RSPN_FT)에는 캠페인 귀속 구매금액 BUY_AMT 가 (캠페인, 회원) 단위로
실려 있어, 캠페인 문맥이 붙은 구매금액은 회원별 HAVING SUM(BUY_AMT) op N 으로 걸어야 의미가
맞다. 파서가 campaign_buy_amount 를 세우면 이중 파싱된 누적 구매 금액 조건과 리던던트
구매반응 EXISTS 를 걷어내고, 캠페인 팩트 집계 빌더(반응 횟수와 공유)가 SQL 을 만든다.

실행(컨테이너): docker compose exec -w /app -e PYTHONPATH=/app api pytest tests/test_campaign_buy_amount_targeting.py -q
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


# ── 파싱: 캠페인 문맥 + 금액 임계값일 때만 campaign_buy_amount 로 잡힌다 ────────────────
@pytest.mark.parametrize("query,amount,operator", [
    ("캠페인 구매금액이 20만원 이상인 회원을 추출해줘", 200000, ">="),
    ("캠페인 구매 금액 20만원 이상 회원", 200000, ">="),
    ("캠페인 결제금액 50만원 초과 고객", 500000, ">"),
    ("캠페인을 통해 20만원 이상 구매한 회원", 200000, ">="),
    ("캠페인을 보고 10만원 이상 결제한 고객", 100000, ">="),
    ("캠페인 반응 구매금액 30만원 이하 회원", 300000, "<="),
])
def test_buy_amount_parsed(query, amount, operator):
    buy = _plan(query)["target_user"].get("campaign_buy_amount")
    assert isinstance(buy, dict), query
    assert buy["amount"] == amount
    assert buy["operator"] == operator


def test_window_parsed_to_days():
    buy = _plan("최근 3개월 캠페인 구매금액 20만원 이상 회원")["target_user"]["campaign_buy_amount"]
    assert buy["window_days"] == 90


@pytest.mark.parametrize("query", [
    # 캠페인 문맥 없는 누적 금액은 주문 집계(aggregate_conditions) 소유다.
    "누적 구매 금액이 20만원 이상인 회원",
    "구매금액 20만원 이상 회원",
    # 캠페인과 지표 사이에 '누적' 수식어가 끼면 캠페인 귀속 금액이 아니다.
    "캠페인을 받은 고객 중 누적 구매금액 20만원 이상",
    # 횟수 임계(번/회)는 반응 '횟수'(campaign_response_frequency) 소유다.
    "캠페인에 2회 이상 반응한 회원",
])
def test_not_stolen(query):
    assert _plan(query)["target_user"].get("campaign_buy_amount") is None, query


def test_lifetime_aggregate_still_parsed_without_campaign_context():
    plan = _plan("누적 구매 금액이 20만원 이상인 회원")
    metric_ids = [c.get("metric_id") for c in plan["target_user"].get("aggregate_conditions", [])]
    assert "purchase_amount" in metric_ids


# ── 이중 파싱 정리: 같은 어구의 누적 금액 조건·구매반응 EXISTS 를 걷어낸다 ───────────────
def test_prunes_duplicate_lifetime_aggregate_and_buy_response():
    plan = _plan("캠페인 구매금액이 20만원 이상인 회원을 추출해줘")
    tu = plan["target_user"]
    assert isinstance(tu.get("campaign_buy_amount"), dict)
    assert not [
        c for c in tu.get("aggregate_conditions", [])
        if c.get("metric_id") == "purchase_amount" and float(c.get("threshold", -1)) == 200000.0
    ]
    assert not [
        r for r in tu.get("campaign_responses", []) if r.get("canonical") == "buy_response"
    ]


# ── SQL: 반응 팩트⨝캠페인 마스터 회원별 SUM(BUY_AMT) 집계로 컴파일된다 ──────────────────
def test_buy_amount_sql_shape():
    sql = _sql("캠페인 구매금액이 20만원 이상인 회원을 추출해줘")
    # 전 생애 주문 합이 아니라 반응 팩트 귀속 금액으로 추출한다.
    assert "MCS_CAMP_MBR_RSPN_FT R" in sql
    assert "CRM_SL_ORDERHEADERMALL" not in sql
    assert "HAVING SUM(R.BUY_AMT) >= 200000" in sql
    # 구매반응 행으로 좁힌다(BUY_AMT 는 구매반응 행에 실린다).
    assert "R.BUY_RSPN_YN = 'Y'" in sql
    # 캠페인 마스터 조인 + 대상군 + 유효 캠페인.
    assert "INNER JOIN Z_CAMPAIGN ZC" in sql
    assert "R.CGRP_TYPE_CD = 'T'" in sql
    assert "ISNULL(ZC.CANCEL_YN, 'N') = 'N'" in sql
    # MBR_NO(nvarchar)↔MEMBER_NO(bigint) 캐스트 조인.
    assert "TRY_CAST(O.MBR_NO AS BIGINT) = B.MEMBER_NO" in sql


def test_buy_amount_combines_with_member_attribute():
    sql = _sql("캠페인 구매금액 20만원 이상인 VIP 여성")
    assert "HAVING SUM(R.BUY_AMT) >= 200000" in sql
    assert "B.GENDER_CD = 'GENDER_CD.FEMALE'" in sql
    assert "B.EMART_GRADE_CD = 'MEM_GRADE_CD.VIP'" in sql


def test_buy_amount_window_in_sql():
    sql = _sql("최근 3개월 캠페인 구매금액 20만원 이상 회원")
    assert "ZC.CAMP_SDATE >= CONVERT(CHAR(8), DATEADD(DAY, -90, GETDATE()), 112)" in sql


def test_combined_with_response_frequency():
    # 횟수 + 귀속 금액이 함께 오면 하나의 집계 서브쿼리에서 HAVING AND 로 결합한다.
    sql = _sql("최근 3개월 캠페인에 2회 이상 반응하고 캠페인 구매금액 20만원 이상인 회원")
    assert "HAVING COUNT(DISTINCT CONCAT(R.CAMP_ID, ':', R.CAMP_EXEC_NO)) >= 2" in sql
    assert "SUM(R.BUY_AMT) >= 200000" in sql
    # 횟수 조건이 있으면 행 스코프는 일반형 '반응'(오퍼/구매) 정의를 유지한다.
    assert "R.OFFR_RSPN_YN = 'Y' OR R.BUY_RSPN_YN = 'Y'" in sql


# ── 소유권: 캠페인 팩트 집계 빌더가 campaign_buy_amount 를 소유한다 ─────────────────────
def test_builder_owns_campaign_buy_amount():
    for builder, owned in g._sql_target_builder_registry():
        if builder is g.build_campaign_response_frequency_targets_sql_candidate:
            assert "campaign_buy_amount" in owned
            break
    else:
        pytest.fail("캠페인 팩트 집계 빌더가 레지스트리에 없음")


def test_exists_builder_defers_to_buy_amount():
    plan = {
        "intent": "find_user_segment",
        "target_user": {
            "campaign_responses": [{"canonical": "campaign_contact", "predicate": "R.CNCT_SCS_YN = 'Y'"}],
            "campaign_buy_amount": {"operator": ">=", "amount": 200000, "window_days": None},
        },
    }
    assert g.build_campaign_response_targets_sql_candidate(plan) is None

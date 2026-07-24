"""장바구니 유형(정기배송/픽업 등) 타겟팅 회귀.

배경: "정기배송 상품을 장바구니에 담은 회원을 보여줘"가 결정론 빌더에서 후보를 못 만들어 LLM
자유생성으로 떨어졌고, 그 결과가

    ... AND C.PRODUCT_ID IN (SELECT PRODUCT_ID FROM CRM_CM_PRODUCT WHERE PRODUCT_TYPE = 'subscription')

였다. CRM_CM_PRODUCT 에 PRODUCT_TYPE 컬럼은 없고(PRODUCT_TYPE_CD 가 있으나 실값이
'PRODUCT_TYPE_CD.GENERAL' 단일값), 'subscription' 이라는 저장값도 없다 — 상품 마스터에는 정기배송
구분이 아예 없다. 실DB에서 정기배송을 구분하는 컬럼은 카트 라인의
ODS_MALL_OMS_CART.CART_TYPE_CD('CART_TYPE_CD.REGULARDELIVERY', 실측 3,155행/2,273명)뿐이다.

고정 내용:
  - cart_type 파서: 장바구니 어휘가 있을 때만 cart_types(레지스트리) 동의어를 매칭한다.
  - 카트 템플릿이 CART_TYPE_CD 등가 술어로 컴파일하고, 캠페인 목적(objective=subscription)이
    같은 단어에서 유추돼도 템플릿이 비켜서지 않는다.
  - KEEP_YN='Y'(미결제 보관)는 미결제/이탈 표현이 실제로 있을 때만 건다 — 정기배송 라인은 실데이터에서
    전건 KEEP_YN='N' 이라, 묻지도 않은 보관 조건을 붙이면 결과가 0명이 된다.

실행(컨테이너): docker compose exec -w /app -e PYTHONPATH=/app api pytest tests/test_cart_type_targeting.py -q
"""

import graph_rag as g


def _plan(query: str) -> dict:
    plan = g.build_query_plan(query, parser="rules")
    g._promote_unknown_intent_for_target_signal(plan)
    return plan


def _sql(query: str) -> str:
    candidate = g.build_sql_template_candidate(_plan(query))
    assert candidate is not None, f"SQL 후보가 없다: {query}"
    return candidate["sql"]


def test_regular_delivery_cart_compiles_to_cart_type_cd():
    plan = _plan("정기배송 상품을 장바구니에 담은 회원을 보여줘.")
    assert plan["target_user"]["cart_type"]["value"] == "CART_TYPE_CD.REGULARDELIVERY"
    sql = _sql("정기배송 상품을 장바구니에 담은 회원을 보여줘.")
    assert "A.CART_TYPE_CD = 'CART_TYPE_CD.REGULARDELIVERY'" in sql
    assert "ODS_MALL_OMS_CART A" in sql and "A.CART_ID = B.MEMBER_ID" in sql


def test_no_hallucinated_product_type_column():
    # 상품 마스터에 유형 컬럼이 없다 — 여기로 새면 sql_guard 도 못 막는 '존재하지 않는 컬럼' SQL 이 된다.
    sql = _sql("장바구니에 정기배송 상품을 담은 고객")
    assert "PRODUCT_TYPE" not in sql
    assert "CRM_CM_PRODUCT" not in sql


def test_keep_yn_not_forced_when_only_type_asked():
    # '담은'만 물었으므로 보관 상태로 좁히지 않는다(정기배송 라인은 전건 KEEP_YN='N' → 0명 방지).
    sql = _sql("장바구니에 정기배송 상품을 담은 고객")
    assert "KEEP_YN" not in sql
    assert "'regular_delivery_cart' AS target_segment" in sql


def test_keep_yn_restored_when_abandonment_asked():
    # 미결제/이탈을 실제로 물으면 다시 보관 상태로 좁힌다(조건을 걸 수 있으면 0명이어도 건다).
    sql = _sql("장바구니에 정기배송 상품을 담고 결제하지 않은 회원")
    assert "A.KEEP_YN = 'Y'" in sql
    assert "A.CART_TYPE_CD = 'CART_TYPE_CD.REGULARDELIVERY'" in sql
    assert "'cart_abandoner' AS target_segment" in sql


def test_cart_type_combines_with_member_attribute():
    sql = _sql("장바구니에 정기배송 상품을 담은 여성 회원")
    assert "A.CART_TYPE_CD = 'CART_TYPE_CD.REGULARDELIVERY'" in sql
    assert "B.GENDER_CD = 'GENDER_CD.FEMALE'" in sql


def test_cart_type_narrows_aggregate_lines():
    # 개수 임계값과 함께 오면 집계 대상 라인도 유형으로 좁혀야 한다(조용한 조건 소실 방지).
    sql = _sql("장바구니에 정기배송 상품을 3개 이상 담은 회원")
    assert "CART_TYPE_CD = 'CART_TYPE_CD.REGULARDELIVERY'" in sql
    assert "COUNT(DISTINCT CART_PRODUCT_NO) >= 3" in sql


def test_cart_type_requires_cart_context():
    # '정기배송 신청한 회원'은 주문 유형 이야기다 — 장바구니 어휘 없이 카트 조건으로 승격하면 안 된다.
    assert _plan("정기배송 신청한 회원")["target_user"]["cart_type"] is None
    assert _plan("픽업으로 주문한 고객")["target_user"]["cart_type"] is None


def test_pickup_cart_type_uses_registry_value():
    # 값은 코드가 아니라 레지스트리(cart_targets.cart_types)가 소유한다 — 새 유형은 JSON 한 줄로 는다.
    plan = _plan("장바구니에 매장 픽업 상품을 담은 회원")
    assert plan["target_user"]["cart_type"]["value"] == "CART_TYPE_CD.PICKUP"


def test_plain_cart_query_keeps_unpaid_semantics():
    # 유형 없는 기존 카트 오디언스는 그대로 미결제 보관(KEEP_YN='Y')이다(회귀 방지).
    sql = _sql("장바구니에 담고 결제하지 않은 회원")
    assert "A.KEEP_YN = 'Y'" in sql
    assert "CART_TYPE_CD" not in sql

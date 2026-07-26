"""장바구니 수량/개수 임계값(cart_aggregate) 회귀 코퍼스.

배경: 카트 개수/수량 임계값이 전용 정규식(_CART_COUNT)으로 파싱돼 이상(>=)·명시 개수만 잡고
'초과(>)'·범위('2개에서 5개 사이')·정확값('정확히 3개')·'종' 단위를 놓쳤다. 놓친 카트 질의는
조용히 주문 상세(CRM_SL_ORDERDETAILMALL) 누적 수량으로 새거나(오배정) 임계값이 통째로 사라져
전 카트 보유자를 반환했다. 이제 카트도 공용 _parse_amount_comparison 문법에 위임한다
([[shared-comparison-grammar]]).

지표는 QTY('담은 수량', schema_catalog important=true)다 — SET_QTY('세트 수량')가 아니다:
정기배송/세트 구성품 라인에서 SET_QTY 는 BOM 수량으로 부풀어(실데이터 avg 6.19) '몇 개 담았나'를 왜곡한다.

실행: docker compose exec -w /app -e PYTHONPATH=/app python pytest tests/test_cart_quantity_targeting.py -q
"""

import graph_rag as g


def _plan(query: str) -> dict:
    plan = g.build_query_plan(query)
    g._promote_unknown_intent_for_target_signal(plan)
    return plan


def _agg(query: str):
    return _plan(query).get("target_user", {}).get("cart_aggregate")


def _sql(query: str) -> str:
    cand = g.build_sql_template_candidate(_plan(query))
    assert cand is not None, f"{query!r}: 후보 없음"
    return cand["sql"]


def test_line_count_ge_one():
    # '1개 이상 담은' = 라인 존재. 개수 단위(개)만 있고 '수량' 표지 없음 → 종류 수(COUNT DISTINCT).
    assert _agg("장바구니에 상품을 1개 이상 담은 회원을 찾아줘.") == {
        "metric": "cart_line_count", "operator": ">=", "threshold": 1,
    }


def test_quantity_ge_five():
    # '상품 수량' 표지 → SUM(QTY).
    assert _agg("장바구니 상품 수량이 5개 이상인 고객을 추출해줘.") == {
        "metric": "cart_quantity", "operator": ">=", "threshold": 5,
    }
    assert "SUM(QTY) >= 5" in _sql("장바구니 상품 수량이 5개 이상인 고객을 추출해줘.")


def test_exceed_ten_is_gt_not_dropped():
    # 회귀: '10개를 초과해 담은'이 단위-연산자 사이 조사(를)+'초과'로 미매칭 → 후보 없음이었다.
    agg = _agg("장바구니에 10개를 초과해 담은 회원을 보여줘.")
    assert agg["operator"] == ">" and agg["threshold"] == 10
    assert "COUNT(DISTINCT CART_PRODUCT_NO) > 10" in _sql("장바구니에 10개를 초과해 담은 회원을 보여줘.")


def test_quantity_range_between_two_and_five():
    # 회귀: 범위 미지원 → 카트 질의가 주문 상세 테이블 누적 수량으로 오배정됐다.
    q = "장바구니 수량이 2개에서 5개 사이인 고객을 찾아줘."
    agg = _agg(q)
    assert agg["metric"] == "cart_quantity"
    assert agg.get("comparisons") == [[">=", 2], ["<=", 5]]
    sql = _sql(q)
    assert "SUM(QTY) >= 2 AND SUM(QTY) <= 5" in sql
    assert "CRM_SL_ORDERDETAILMALL" not in sql  # 주문 테이블로 새지 않는다


def test_exact_three_is_equals():
    # 회귀: '정확히 … 3개'가 미매칭 → 주문 상세 테이블로 오배정됐다.
    q = "장바구니에 정확히 상품 3개를 담은 회원을 추출해줘."
    agg = _agg(q)
    assert agg["operator"] == "=" and agg["threshold"] == 3
    assert "CRM_SL_ORDERDETAILMALL" not in _sql(q)


def test_distinct_types_jong_unit_not_dropped():
    # 회귀: '종' 단위 미인식 → 임계값이 사라지고 전 카트 보유자를 반환했다.
    q = "장바구니에 담긴 상품 종류가 3종 이상인 고객을 보여줘."
    assert _agg(q) == {"metric": "cart_line_count", "operator": ">=", "threshold": 3}
    assert "COUNT(DISTINCT CART_PRODUCT_NO) >= 3" in _sql(q)


def test_same_product_ge_two_uses_max_qty():
    assert _agg("같은 상품을 장바구니에 2개 이상 담은 고객을 추출해줘.") == {
        "metric": "cart_same_product_quantity", "operator": ">=", "threshold": 2,
    }
    assert "MAX(QTY) >= 2" in _sql("같은 상품을 장바구니에 2개 이상 담은 고객을 추출해줘.")


def test_regular_delivery_quantity_combines_cart_type():
    # 유형(정기배송)과 수량 임계가 함께 → CART_TYPE_CD 필터 + SUM(QTY) HAVING.
    sql = _sql("정기배송 장바구니 상품 수량이 3개 이상인 고객을 찾아줘.")
    assert "CART_TYPE_CD = 'CART_TYPE_CD.REGULARDELIVERY'" in sql
    assert "SUM(QTY) >= 3" in sql


def test_quantity_metric_uses_qty_never_set_qty():
    # 지표 컬럼은 QTY('담은 수량') — SET_QTY('세트 수량') 금지.
    for q in (
        "장바구니 상품 수량이 5개 이상인 고객을 추출해줘.",
        "장바구니 수량이 2개에서 5개 사이인 고객을 찾아줘.",
        "같은 상품을 장바구니에 2개 이상 담은 고객을 추출해줘.",
    ):
        assert "SET_QTY" not in _sql(q), q

"""상품/주문 집계 지표 분리 회귀 코퍼스(주문 건수·상품 수량·서로 다른 상품 수·구매/할인 금액을 스펙
기반 점수화로 구분). 문장별 하드코딩 없이 aggregate_targets.metrics 스펙(units/hint_terms/anti_hint_terms)
과 aggregation_scope/scope 로 일반화한다.

각 케이스는 최종 SQL 만이 아니라 metric_id / aggregation / aggregation_scope / scope / operator / value /
SQL 집계식 / silent fallback 여부를 함께 검증한다.

실행(컨테이너): docker compose exec -w /app -e PYTHONPATH=/app api pytest tests/test_product_metric_resolution.py -q
"""

import pytest

import graph_rag as g


def _plan(query: str) -> dict:
    plan = g.build_query_plan(query)
    g._promote_unknown_intent_for_target_signal(plan)
    return plan


def _conds(query: str):
    return _plan(query)["target_user"].get("aggregate_conditions") or []


def _one(query: str) -> dict:
    conds = _conds(query)
    assert len(conds) == 1, f"{query!r}: 조건 {len(conds)}개 (1개 기대)"
    return conds[0]


def _sql(query: str) -> str:
    candidate = g.build_sql_template_candidate(_plan(query))
    assert candidate is not None, f"{query!r}: 후보 없음"
    return candidate["sql"]


# ── PRODUCT_METRIC_001~010 ─────────────────────────────────────────────────

def test_pm001_per_order_item_quantity():
    c = _one("한 주문에서 상품을 5개 이상 구매한 회원을 찾아줘.")
    assert c["metric_id"] == "total_item_quantity" and c["operator"] == ">=" and c["threshold"] == 5
    assert c["aggregation_scope"] == "per_order"
    sql = _sql("한 주문에서 상품을 5개 이상 구매한 회원을 찾아줘.")
    assert "SUM(ORDER_QTY) >= 5" in sql and "GROUP BY MEMBER_NO, ORDER_ID" in sql


def test_pm002_cumulative_item_quantity():
    c = _one("누적 상품 구매 수량이 10개를 초과한 고객을 추출해줘.")
    assert c["metric_id"] == "total_item_quantity" and c["operator"] == ">" and c["threshold"] == 10
    assert c.get("aggregation_scope", "per_member") == "per_member"
    assert "SUM(ORDER_QTY) > 10" in _sql("누적 상품 구매 수량이 10개를 초과한 고객을 추출해줘.")


def test_pm003_same_product_quantity():
    c = _one("동일 상품을 3개 이상 구매한 회원을 보여줘.")
    assert c["metric_id"] == "total_item_quantity" and c["threshold"] == 3
    assert c["aggregation_scope"] == "per_product"
    assert "GROUP BY MEMBER_NO, PRODUCT_ID" in _sql("동일 상품을 3개 이상 구매한 회원을 보여줘.")


def test_pm004_per_order_item_quantity_hanbeone():
    # '한 번에'가 한글 수사 정규화로 '1번'이 돼 주문 건수로 오인되던 것을 고정(grain 표지를 먼저 뗀다).
    c = _one("한 번에 상품을 10개 이상 주문한 고객을 찾아줘.")
    assert c["metric_id"] == "total_item_quantity" and c["threshold"] == 10 and c["aggregation_scope"] == "per_order"


def test_pm005_distinct_product_count():
    c = _one("구매 상품 종류가 5개 이상인 회원을 추출해줘.")
    assert c["metric_id"] == "distinct_product_count" and c["operator"] == ">=" and c["threshold"] == 5
    assert "COUNT(DISTINCT PRODUCT_ID) >= 5" in _sql("구매 상품 종류가 5개 이상인 회원을 추출해줘.")


def test_pm006_distinct_product_count_jong_unit():
    # '10종'의 '종' 단위 미인식으로 후보 없음이던 것을 고정.
    c = _one("서로 다른 상품을 10종 이상 구매한 고객을 보여줘.")
    assert c["metric_id"] == "distinct_product_count" and c["threshold"] == 10


def test_pm007_brand_scope_placeholder_clarifies():
    # '특정 브랜드'는 값 미지정 → 조용히 전체 집계로 폴백하지 않고 clarification.
    plan = _plan("특정 브랜드 상품을 누적 5개 이상 구매한 회원을 찾아줘.")
    assert (plan.get("unsupported") or {}).get("reason") == "scope_value_unspecified"
    assert g.build_sql_template_candidate(plan) is None


def test_pm007b_concrete_brand_scope_compiles():
    # 구체적 브랜드명이면 scope 를 지표 조건과 결합한다(상품 마스터 조인 + BRAND_NAME 필터).
    c = _one("나이키 브랜드 상품을 2종 이상 구매한 회원")
    assert c["metric_id"] == "distinct_product_count" and c["scope"] == {"brand": "나이키"}
    sql = _sql("나이키 브랜드 상품을 2종 이상 구매한 회원")
    assert "INNER JOIN CRM_CM_PRODUCT P ON D.PRODUCT_ID = P.PRODUCT_ID" in sql
    assert "P.BRAND_NAME = '나이키'" in sql
    assert "COUNT(DISTINCT D.PRODUCT_ID) >= 2" in sql


def test_pm008_category_scope_placeholder_clarifies():
    plan = _plan("특정 카테고리 구매 금액이 100,000원 이상인 고객을 추출해줘.")
    assert (plan.get("unsupported") or {}).get("reason") == "scope_value_unspecified"


def test_pm009_discount_amount():
    c = _one("할인 금액이 20,000원 이상 적용된 주문이 있는 회원을 보여줘.")
    assert c["metric_id"] == "discount_amount" and c["operator"] == ">=" and c["threshold"] == 20000
    assert "SUM(DC_AMT) >= 20000" in _sql("할인 금액이 20,000원 이상 적용된 주문이 있는 회원을 보여줘.")


def test_pm010_quantity_and_amount_not_cross_contaminated():
    # 결제 금액 조건은 깨끗이 잡히고 수량 절이 금액 조건을 오염시키지 않는다(모호 '많지만'은 임계값 없음).
    conds = _conds("상품 수량은 많지만 총 결제 금액은 50,000원 이하인 고객을 찾아줘.")
    assert ("purchase_amount", "<=", 50000.0) in [(c["metric_id"], c["operator"], c["threshold"]) for c in conds]
    # 금액 조건에 상품 수량 임계값(오염)이 붙지 않는다.
    assert all(not (c["metric_id"] == "purchase_amount" and c["operator"] in (">", ">=")) for c in conds)


# ── 복수 조건 분리(명시 임계값) ─────────────────────────────────────────────

def test_clause_split_two_metrics():
    conds = _conds("상품 수량은 10개 이상이지만 결제 금액은 50,000원 이하인 고객")
    pairs = [(c["metric_id"], c["operator"], c["threshold"]) for c in conds]
    assert ("total_item_quantity", ">=", 10.0) in pairs
    assert ("purchase_amount", "<=", 50000.0) in pairs


# ── silent fallback 금지(핵심 회귀) ─────────────────────────────────────────

def test_item_count_not_order_count():
    # '상품 5개'는 order_count 로 자동 해석되지 않는다.
    assert _one("상품 5개 이상 구매한 고객")["metric_id"] == "total_item_quantity"


def test_order_count_still_by_count_unit():
    # '회/번/건' 단위는 여전히 주문 건수(order_count) — 수량과 구분된다.
    assert _one("5회 이상 구매한 고객")["metric_id"] == "order_count"
    assert _one("주문 5건 이하인 고객")["metric_id"] == "order_count"


def test_non_purchase_counts_ignored():
    # 도메인 문맥(구매/상품) 없으면 집계 조건으로 잡지 않는다.
    assert _conds("2회 이상 방문한 고객") == []
    assert _conds("자녀가 2명 이상인 고객") == []


def test_ambiguous_metric_requests_clarification():
    # 도메인 문맥은 있으나 지표를 확정 못 하는 애매한 개수는 조용히 폴백하지 않고 clarification.
    plan = _plan("구매 3세트 이상인 고객")  # '세트'는 어떤 지표에도 등록되지 않은 단위
    reason = (plan.get("unsupported") or {}).get("reason")
    # 미해석이면 clarification, 아니면 조건 없음(빈) — 어느 쪽이든 order_count/quantity 로 조용히 폴백하면 안 된다.
    conds = plan["target_user"].get("aggregate_conditions") or []
    assert not conds or reason == "metric_not_resolved"


# ── 스펙만으로 신규 지표 등록(파이썬 분기 없이) ──────────────────────────────

# ── 상품명 없는 '전상품 대상' 수량 조건은 상품 마스터를 조인하지 않는다 ──────────────

@pytest.mark.parametrize(
    "query",
    [
        # 실제 실패 프롬프트(/target-sql 호출 재현). 캠페인 이름의 '다구매'가 '다'+'구매'로 쪼개져
        # 상품 6컬럼 LIKE N'%다%' 가 붙고 결과가 0명이 되던 버그.
        "최근 90일 구매 상품 수량이 총 5개 이상인 회원을 추출해서 다구매 고객 캠페인을 만들어줘.",
        "상품을 5개 이상 다구매한 고객 캠페인",
    ],
)
def test_no_product_name_means_no_product_master_join(query):
    # 주문상세 행 자체가 상품 라인이므로 '전상품 대상' 조건에 상품 마스터 조인은 불필요하다.
    assert _plan(query)["target_user"].get("purchase_object") is None
    sql = _sql(query)
    normalized = " ".join(sql.upper().split())
    assert "CRM_CM_PRODUCT" not in normalized
    # 상품명 매칭 컬럼(레지스트리 소유)이 하나도 등장하지 않아야 한다 — 컬럼이 없으면 LIKE 도 없다.
    for column in g._MEMBER_TARGET_FILTERS["purchase_product_match_columns"]:
        assert column.upper() not in normalized, f"{column} 이 상품명 없는 SQL 에 붙었다"
    assert "LIKE" not in normalized
    # 수량 조건은 그대로 유지된다.
    assert "HAVING" in normalized and "SUM(ORDER_QTY) >= 5" in normalized


def test_new_metric_via_spec_only(monkeypatch):
    # aggregate_targets.metrics 에 스펙만 추가하면 파이썬 분기 없이 새 지표가 해석·컴파일된다.
    cfg = g._MEMBER_TARGET_FILTERS["aggregate_targets"]
    original = cfg["metrics"]
    patched = dict(original)
    patched["return_quantity"] = {
        "semantic_type": "sum", "agg": "SUM", "column": "RETURN_QTY", "table": "CRM_SL_ORDERDETAILMALL",
        "ko_label": "반품 수량", "synonyms": ["반품 수량", "반품수량"],
        "units": ["개", "수량"], "hint_terms": ["반품"], "anti_hint_terms": ["금액", "종류", "횟수"],
    }
    monkeypatch.setitem(cfg, "metrics", patched)
    c = _one("반품 수량이 3개 이상인 고객")
    assert c["metric_id"] == "return_quantity" and c["threshold"] == 3
    assert "SUM(RETURN_QTY) >= 3" in _sql("반품 수량이 3개 이상인 고객")

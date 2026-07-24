"""범용 집계 조건 타겟(build_aggregate_targets_sql_candidate) 회귀 테스트.

배경: "최근 90일 누적 구매 금액 100만 원 이상" 같은 '기간 내 집계 임계값' 조건은 파싱·윈도우·인텐트
어느 층에서도 지원되지 않아 조용히 드롭됐다. 이제 aggregate_targets 레지스트리(member_target_filters.json)
기반으로 프롬프트에서 {지표, 연산자, 임계값, 기간}을 뽑고(_apply_aggregate_condition_filter), 주문 테이블
회원별 집계 서브쿼리(GROUP BY MEMBER_NO HAVING agg(col) op threshold)로 컴파일한다. 성별/등급/지역 등
회원 속성은 compile_member_target_conditions 로 같은 SQL 에 AND 결합한다.

LLM 없이 결정론 경로(rules)만 검증한다.

실행(컨테이너): docker compose exec -w /app -e PYTHONPATH=/app api pytest tests/test_aggregate_condition_targets.py -q
"""

import graph_rag as g


def _conditions(query):
    return g.build_query_plan(query)["target_user"].get("aggregate_conditions") or []


# --- 값/기간 파서 ---

def test_korean_amount_parsing():
    assert g._parse_korean_amount("100", "만") == 1_000_000
    assert g._parse_korean_amount("1", "억") == 100_000_000
    assert g._parse_korean_amount("3", "천만") == 30_000_000
    assert g._parse_korean_amount("5", "") == 5
    assert g._parse_korean_amount("1,000,000", "") == 1_000_000
    assert g._parse_korean_amount("abc", "") is None


def test_recent_window_days_parsing():
    assert g._parse_recent_window_days("최근 90일 구매") == 90
    assert g._parse_recent_window_days("최근 3개월") == 90
    assert g._parse_recent_window_days("최근 2주 내") == 14
    assert g._parse_recent_window_days("구매 금액 이상") is None


# --- 조건 추출 ---

def test_extracts_amount_threshold_with_window():
    conditions = _conditions("최근 90일 누적 구매 금액 100만 원 이상 고객")
    assert len(conditions) == 1
    c = conditions[0]
    assert c["metric_id"] == "purchase_amount"
    assert c["operator"] == ">=" and c["threshold"] == 1_000_000 and c["window_days"] == 90


def test_extracts_count_threshold_without_window():
    conditions = _conditions("구매 횟수 5건 이상 고객")
    assert len(conditions) == 1
    c = conditions[0]
    assert c["metric_id"] == "order_count" and c["operator"] == ">=" and c["threshold"] == 5
    assert c["window_days"] is None


def test_operator_variants():
    assert _conditions("구매 금액 10만원 이하 고객")[0]["operator"] == "<="
    assert _conditions("구매 금액 10만원 초과 고객")[0]["operator"] == ">"
    assert _conditions("구매 금액 10만원 미만 고객")[0]["operator"] == "<"


def test_ranking_phrasing_does_not_trigger_aggregate():
    # "구매 금액이 많은/높은 고객"은 임계값 조건이 아니라 랭킹 표현 — 집계 조건으로 잡히면 안 된다.
    assert _conditions("구매 금액이 많은 고객") == []
    assert _conditions("누적 구매금액이 높은 고객 상위 100명") == []


# --- 빌더 SQL ---

def test_builder_compiles_aggregate_subquery_and_combines_member_conditions():
    plan = g.build_query_plan("서울 거주 고객, VIP 등급 이상 고객, 최근 90일 누적 구매 금액 100만 원 이상 고객")
    assert plan["intent"] == "find_user_segment"
    candidate = g.build_aggregate_targets_sql_candidate(plan)
    assert candidate is not None
    sql = candidate["sql"]
    # 집계 서브쿼리(기간 창 + HAVING)
    assert "HAVING SUM(PAYMENT_AMT) >= 1000000" in sql
    assert "DATEADD(DAY, -90, GETDATE())" in sql
    assert "GROUP BY MEMBER_NO" in sql
    # 회원 속성 AND 결합
    assert "B.SIDO IN ('서울')" in sql
    assert "B.EMART_GRADE_CD = 'MEM_GRADE_CD.VIP'" in sql
    # 집계 조건은 커버되므로 미고지(dropped) 대상이 아니다.
    assert "target_user.aggregate_conditions" not in candidate["dropped_conditions"]


def test_builder_count_metric_uses_count_distinct_and_no_date_when_no_window():
    plan = g.build_query_plan("구매 횟수 5건 이상 고객")
    sql = g.build_aggregate_targets_sql_candidate(plan)["sql"]
    assert "HAVING COUNT(DISTINCT ORDER_ID) >= 5" in sql
    assert "DATEADD" not in sql  # 기간 창 없음


def test_builder_returns_none_without_conditions():
    plan = g.build_query_plan("20대 여성 고객")
    assert g.build_aggregate_targets_sql_candidate(plan) is None


# --- 지표명 없는 개수 임계값('2개/3번/2회 이상 구매') → 주문 건수(order_count) 집계 ---
# 배경: '2019년 1월에 2개 이상 상품 구입한 사람'은 (1) '이상'/'2개'/'상품'이 상품명 LIKE 로 새고,
# (2) 개수 임계값이 통째로 드롭됐다. 이제 상품명은 추출에서 빠지고, 개수 임계값은 order_count HAVING 으로
# 컴파일되며, 절대 구매창(2019년 1월)이 있으면 그 기간 주문만 센다.

def test_bare_count_threshold_maps_to_order_count():
    conditions = _conditions("2019년 1월에 2개 이상 상품 구입한 사람")
    assert len(conditions) == 1
    c = conditions[0]
    assert c["metric_id"] == "order_count" and c["operator"] == ">=" and c["threshold"] == 2


def test_bare_count_threshold_operator_and_unit_variants():
    assert _conditions("3번 이상 구매한 고객")[0]["threshold"] == 3
    assert _conditions("2회 이상 구입한 회원")[0]["operator"] == ">="
    assert _conditions("주문 5건 이하인 고객")[0]["operator"] == "<="


def test_bare_count_threshold_not_double_counted_with_metric_noun():
    # '구매 횟수 5건 이상'(지표명 명시형)은 order_count 하나만 — 개수 임계값 파서가 중복 추가하면 안 된다.
    conditions = _conditions("구매 횟수 5건 이상 고객")
    assert [c["metric_id"] for c in conditions] == ["order_count"]


def test_bare_count_threshold_yields_to_cart_and_response_tracks():
    # 장바구니/반응 개수 임계값은 각 전용 트랙 소유 — 주문 건수(order_count)로 새면 안 된다.
    assert _conditions("장바구니에 3개 이상 담은 고객") == []
    assert _conditions("최근 3개월 캠페인 중 2번 이상 반응한 고객") == []


def test_bare_count_threshold_ignores_non_purchase_counts():
    # 구매 동사가 없으면 개수 임계값을 구매 건수로 확정하지 않는다(오탐 방지).
    assert _conditions("2회 이상 방문한 고객") == []
    assert _conditions("자녀가 2명 이상인 고객") == []


def test_builder_count_threshold_with_absolute_date_window():
    # 절대 구매창(2019년 1월)이 함께 잡히면 그 기간 주문만 세어 HAVING COUNT(DISTINCT ORDER_ID) 로 건다.
    plan = g.build_query_plan("2019년 1월에 2개 이상 상품 구입한 사람")
    candidate = g.build_sql_template_candidate(plan)
    assert candidate is not None and candidate["id"] == "sql_template:aggregate_targets"
    sql = candidate["sql"]
    assert "HAVING COUNT(DISTINCT ORDER_ID) >= 2" in sql
    assert "ORDER_DATE BETWEEN '20190101' AND '20190131'" in sql
    # 오추출 재발 방지: '이상'/'상품'이 상품명 LIKE 로 새지 않는다.
    assert "N'%이상%'" not in sql and "N'%상품%'" not in sql


def test_builder_count_threshold_combines_member_attributes():
    plan = g.build_query_plan("2019년 1월에 2건 이상 구매한 30대 여성 고객")
    sql = g.build_aggregate_targets_sql_candidate(plan)["sql"]
    assert "HAVING COUNT(DISTINCT ORDER_ID) >= 2" in sql
    assert "ORDER_DATE BETWEEN '20190101' AND '20190131'" in sql
    assert "B.AGE >= 30" in sql and "B.AGE <= 39" in sql
    assert "B.GENDER_CD = 'GENDER_CD.FEMALE'" in sql

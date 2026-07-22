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

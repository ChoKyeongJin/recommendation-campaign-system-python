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


def test_exact_equals_marker_sets_equality_operator():
    # 연산자어 없는 '정확히/딱 N'은 등호로 확정한다(모호한 맨숫자는 제외).
    金 = _conditions("구매 금액이 정확히 10만원인 고객")
    assert 金 and 金[0]["metric_id"] == "purchase_amount" and 金[0]["operator"] == "=" and 金[0]["threshold"] == 100_000
    회 = _conditions("정확히 3회 구매한 고객")
    assert 회 and 회[0]["metric_id"] == "order_count" and 회[0]["operator"] == "=" and 회[0]["threshold"] == 3
    assert _conditions("구매 횟수 딱 5번인 고객")[0]["operator"] == "="
    # 마커 없는 맨숫자는 모호하므로 등호로 넘겨짚지 않는다.
    assert _conditions("3회 구매한 고객") == []


def test_aggregate_shares_comparison_grammar():
    # 공용 비교 문법 이관으로 aggregate/count 도 '보다 많은'·동사형·범위를 얻는다.
    assert _conditions("구매 금액이 5만원보다 많은 고객")[0]["operator"] == ">"
    assert _conditions("구매금액이 10만원을 초과하는 고객")[0]["operator"] == ">"
    assert _conditions("3회보다 많이 구매한 고객")[0] == {
        "metric_id": "order_count", "operator": ">", "threshold": 3, "window_days": None, "label": "구매 횟수"
    }
    # 범위 → 같은 지표 두 조건(>=lo, <=hi).
    rng = _conditions("구매 금액 10만원에서 50만원 사이인 고객")
    assert [(c["operator"], c["threshold"]) for c in rng] == [(">=", 100_000), ("<=", 500_000)]


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

def test_bare_item_count_maps_to_item_quantity():
    # '2개 이상 상품 구입'의 '개'는 품목 수량이다 → total_item_quantity(SUM(ORDER_QTY)). 예전엔 order_count
    # (주문 건수)로 오해석했으나, 스펙 기반 리졸버가 단위 '개'를 상품 수량으로 라우팅한다.
    conditions = _conditions("2019년 1월에 2개 이상 상품 구입한 사람")
    assert len(conditions) == 1
    c = conditions[0]
    assert c["metric_id"] == "total_item_quantity" and c["operator"] == ">=" and c["threshold"] == 2


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


def test_builder_item_quantity_with_absolute_date_window():
    # 절대 구매창(2019년 1월)이 함께 잡히면 그 기간 상품 수량만 SUM(ORDER_QTY) 로 집계한다('개'=수량).
    plan = g.build_query_plan("2019년 1월에 2개 이상 상품 구입한 사람")
    candidate = g.build_sql_template_candidate(plan)
    assert candidate is not None and candidate["id"] == "sql_template:aggregate_targets"
    sql = candidate["sql"]
    assert "HAVING SUM(ORDER_QTY) >= 2" in sql
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


# --- 롤링 윈도우(최근 N일) 보존: '최근 90일 3회'가 '전체 기간 3회'로 조용히 왜곡되던 회귀 고정 ---
# 배경: 지표명 없는 개수 임계값 경로(_apply_purchase_count_threshold_filter)가 window_days=None 을
# 하드코딩해, 롤링 윈도우가 통째로 소실됐다(의미 왜곡). 이제 그 경로도 _parse_recent_window_days 를
# 호출해 window_days 를 보존하고, 빌더가 ORDER_DATE 창을 HAVING 앞 WHERE 로 건다.

def test_recent_window_preserved_on_bare_count_condition():
    # query plan 층: window_days 가 90 으로 보존돼야 한다(None 아님).
    conditions = _conditions("최근 90일 동안 3회 이상 구매한 회원")
    assert len(conditions) == 1
    c = conditions[0]
    assert c["metric_id"] == "order_count" and c["operator"] == ">=" and c["threshold"] == 3
    assert c["window_days"] == 90


def test_recent_window_reaches_sql_on_bare_count_condition():
    # SQL 층: 롤링 윈도우가 ORDER_DATE 하한으로 실제 반영돼야 한다(전체 기간 집계 방지).
    plan = g.build_query_plan("최근 90일 동안 3회 이상 구매한 회원")
    sql = g.build_aggregate_targets_sql_candidate(plan)["sql"]
    assert "HAVING COUNT(DISTINCT ORDER_ID) >= 3" in sql
    assert "ORDER_DATE >=" in sql and "DATEADD(DAY, -90, GETDATE())" in sql


def test_recent_window_preserved_on_metric_noun_condition():
    # 지표명 명시형('구매 횟수') 경로도 롤링 윈도우를 보존한다.
    c = _conditions("최근 30일 구매 횟수 5회 이상 고객")[0]
    assert c["metric_id"] == "order_count" and c["window_days"] == 30


# --- 달력 기간(올해/지난달)은 롤링 윈도우와 별도 타입: 조용히 드롭하지 않고 표식+명시 경고 ---
# 배경: '올해'·'지난달'은 '지금으로부터 N일'(롤링)과 성격이 다른 고정 달력 구간이다. 집계 SQL 반영은
# 별도 작업으로 미뤄져 있으나, 그렇다고 전체 기간으로 조용히 계산하면 '올해 10회'가 '평생 10회'로
# 왜곡된다. 그래서 조건에 calendar_period 표식을 남기고 _deterministic_dropped_conditions 가 명시 경고한다.

def _warnings(query):
    plan = g.build_query_plan(query)
    g._promote_unknown_intent_for_target_signal(plan)
    return g._deterministic_dropped_conditions(query, plan)


def test_calendar_period_tagged_not_silently_dropped():
    c = _conditions("올해 주문 횟수가 10회 이상인 고객")[0]
    assert c["metric_id"] == "order_count" and c["operator"] == ">=" and c["threshold"] == 10
    # 롤링 윈도우가 아니므로 window_days 는 없고, 달력 기간 표식이 남는다.
    assert c["window_days"] is None
    assert c["calendar_period"] == "current_year"


def test_calendar_period_variants_map_to_tokens():
    assert _conditions("지난달 구매 횟수 3회 이상 고객")[0]["calendar_period"] == "previous_month"
    assert _conditions("작년 구매 금액 100만원 이상 고객")[0]["calendar_period"] == "last_year"


def test_calendar_period_emits_explicit_warning():
    # 전체 기간 폴백을 조용히 하지 않는다 — 사람이 읽는 경고로 명시한다.
    warnings = _warnings("올해 주문 횟수가 10회 이상인 고객")
    assert any("올해" in w and "미반영" in w for w in warnings), warnings


def test_recent_window_not_flagged_as_calendar_period():
    # 롤링 윈도우는 달력 기간 표식을 달지 않고, 경고도 없다(정상 반영).
    c = _conditions("최근 90일 동안 3회 이상 구매한 회원")[0]
    assert "calendar_period" not in c
    assert _warnings("최근 90일 동안 3회 이상 구매한 회원") == []


def test_plain_count_condition_has_no_calendar_key():
    # 기간 표현이 전혀 없으면 calendar_period 키를 붙이지 않는다(조건 형태 불변 — 회귀 안전).
    c = _conditions("주문 5회 이상 한 회원")[0]
    assert "calendar_period" not in c and c["window_days"] is None


# --- 한글 수사('두 번'=2) 정규화: 숫자 임계값 파서에 걸리도록 표면 정규화 ---
# 배경: '두 번 이상 구매'·'정확히 두 번'은 숫자형(2회) 파서만 있어 통째로 드롭됐다. 개수 임계값 추출
# 경로에서 한글 수사+개수 단위(번/회/건/개)를 숫자로 로컬 정규화해 order_count 임계값으로 컴파일한다.

def test_korean_numeral_helper_maps_native_numbers():
    assert g._normalize_korean_count_numerals("두 번") == "2번"
    assert g._normalize_korean_count_numerals("세 개") == "3개"
    assert g._normalize_korean_count_numerals("다섯 번") == "5번"
    assert g._normalize_korean_count_numerals("열 회") == "10회"
    # 앞 음절이 한글이면(단어 일부) 치환하지 않는다.
    assert g._normalize_korean_count_numerals("가세 번호") == "가세 번호"


def test_native_numeral_count_threshold_extracted():
    c = _conditions("두 번 이상 구매한 고객")[0]
    assert c["metric_id"] == "order_count" and c["operator"] == ">=" and c["threshold"] == 2
    assert _conditions("세 번 이상 주문한 회원")[0]["threshold"] == 3


def test_native_numeral_exact_count_threshold():
    c = _conditions("정확히 두 번 구매한 회원")[0]
    assert c["metric_id"] == "order_count" and c["operator"] == "=" and c["threshold"] == 2


def test_native_numeral_metric_noun_path():
    c = _conditions("구매 횟수 다섯 번 이상 고객")[0]
    assert c["metric_id"] == "order_count" and c["operator"] == ">=" and c["threshold"] == 5


def test_native_numeral_does_not_break_no_purchase():
    # '한 번도 주문하지 않은'은 수사 정규화가 로컬이라 no_purchase 매칭을 깨지 않는다(가장 중요한 회귀).
    plan = g.build_query_plan("한 번도 주문하지 않은 회원")
    g._promote_unknown_intent_for_target_signal(plan)
    assert "no_purchase" in plan["target_user"].get("behaviors", [])
    assert _conditions("한 번도 주문하지 않은 회원") == []


def test_bare_native_numeral_without_operator_is_ambiguous():
    # '한 번 구매한 고객'(연산자/정확히 마커 없음)은 맨숫자처럼 모호 — 등호로 넘겨짚지 않는다.
    assert _conditions("한 번 구매한 고객") == []

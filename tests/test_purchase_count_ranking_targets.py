"""구매 건수 랭킹 타겟(build_purchase_count_ranking_sql_candidate) 회귀 테스트.

배경: "2019년 2월에 상품 많이 구입한 사람 10명" 같은 부사형(많이/자주 + 구매동사) 표현은 랭킹 의도인데,
'많이'가 상품명으로 오인식돼 PRODUCT_NAME LIKE N'%많이%' 로 새거나(오추출), member_metric_ranking
(월 스냅샷 전 기간 누적)에는 안 걸려 랭킹이 소실됐다. 이제 _apply_purchase_count_ranking_target 이
{top_n} 을 뽑고, build_purchase_count_ranking_sql_candidate 가 주문 상세(CRM_SL_ORDERDETAILMALL)를
회원별로 GROUP BY 해 COUNT(*) 내림차순 상위 N 명을 뽑는다. 구매 날짜 창(purchase_date)이 있으면 그
기간 주문만 센다. 성별/연령/등급 등 회원 속성은 compile_member_target_conditions 로 같은 SQL 에 AND 결합.

LLM 없이 결정론 경로(rules)만 검증한다.

실행(컨테이너): docker compose exec -w /app -e PYTHONPATH=/app api pytest tests/test_purchase_count_ranking_targets.py -q
"""

import graph_rag as g


def _ranking(query):
    return g.build_query_plan(query).get("purchase_count_ranking")


# --- 신호 추출 ---

def test_extracts_ranking_with_month_window_and_count():
    plan = g.build_query_plan("2019년 2월에 상품 많이 구입한 사람 10명")
    ranking = plan.get("purchase_count_ranking")
    assert isinstance(ranking, dict) and ranking["top_n"] == 10
    # 절대 월 창은 구매 날짜 타겟으로 함께 잡힌다(그 기간 주문만 센다).
    assert plan["target_user"]["purchase_date"]["from"] == "20190201"
    assert plan["target_user"]["purchase_date"]["to"] == "20190228"


def test_superlative_without_count_uses_default_top_n():
    # 최상급('가장 많이')은 개수(N명)가 없어도 랭킹으로 확정한다(기본 top_n).
    ranking = _ranking("가장 많이 구매한 고객")
    assert isinstance(ranking, dict) and ranking["top_n"] >= 1


def test_bought_verb_variants_trigger_ranking():
    assert _ranking("작년에 제일 많이 산 고객 5명") is not None
    assert _ranking("자주 주문한 회원 20명") is not None


# --- 오탐 방지(정밀도 가드) ---

def test_metric_noun_ranking_not_hijacked():
    # "구매횟수가 많은 고객"은 지표 명사 랭킹(member_metric_ranking) — 부사형 구매 랭킹으로 잡히면 안 된다.
    assert _ranking("구매횟수가 많은 고객 10명") is None


def test_plain_purchase_without_quantity_adverb_not_ranking():
    # 수량 부사도 개수/최상급도 없는 단순 구매는 랭킹이 아니다.
    assert _ranking("2019년 2월에 구매한 고객") is None


def test_ambiguous_without_count_or_superlative_not_ranking():
    # '많이 구매한 고객'(개수/최상급 없음)은 모호해 랭킹 확정하지 않는다.
    assert _ranking("많이 구매한 고객에게 쿠폰") is None


def test_negation_not_ranking():
    assert _ranking("많이 구매하지 않은 고객 10명") is None


def test_region_density_not_hijacked():
    # "많이 사는 동네"는 밀집 지역 랭킹 — 회원 구매 랭킹으로 오염되면 안 된다.
    assert _ranking("고객이 많이 사는 동네 상위 3곳") is None


# --- 빌더 SQL ---

def test_builder_emits_grouped_count_ranking_with_date_window():
    plan = g.build_query_plan("2019년 2월에 상품 많이 구입한 사람 10명")
    candidate = g.build_purchase_count_ranking_sql_candidate(plan)
    assert candidate is not None
    sql = candidate["sql"]
    assert "TOP 10 B.MEMBER_NO AS CUST_ID" in sql
    assert "COUNT(*) AS purchase_count" in sql
    assert "FROM CRM_SL_ORDERDETAILMALL D" in sql
    assert "INNER JOIN CRM_MB_BASEINFO B ON D.MEMBER_NO = B.MEMBER_NO" in sql
    assert "GROUP BY B.MEMBER_NO, B.EMART_GRADE_CD" in sql
    assert "ORDER BY COUNT(*) DESC" in sql
    assert "ORDER_DATE BETWEEN '20190201' AND '20190228'" in sql
    # '많이'가 상품명으로 새지 않는다(오추출 재발 방지).
    assert "N'%많이%'" not in sql


def test_builder_combines_member_attributes():
    plan = g.build_query_plan("2019년 2월에 많이 구입한 30대 여성 10명")
    sql = g.build_purchase_count_ranking_sql_candidate(plan)["sql"]
    assert "B.AGE >= 30" in sql and "B.AGE <= 39" in sql
    assert "TOP 10" in sql and "ORDER BY COUNT(*) DESC" in sql


def test_builder_returns_none_without_ranking_signal():
    plan = g.build_query_plan("2019년 2월에 구매한 고객")
    assert g.build_purchase_count_ranking_sql_candidate(plan) is None


# --- 디스패처 라우팅(핵심 회귀) ---

def test_dispatcher_routes_to_count_ranking_not_purchase_history():
    # 날짜(2019년 2월)만으로 purchase_history 로 뺏기지 않고 랭킹 템플릿이 선택돼야 한다.
    plan = g.build_query_plan("2019년 2월에 상품 많이 구입한 사람 10명에게 쿠폰 발송")
    candidate = g.build_sql_template_candidate(plan)
    assert candidate is not None
    assert candidate["id"] == "sql_template:purchase_count_ranking"
    assert "N'%많이%'" not in candidate["sql"]

"""'<지표>가 높은 고객' → 회원 단위 지표 랭킹 결정론 추출 회귀 코퍼스.

배경: "누적 구매 금액이 높은 고객" 같은 회원 단위 지표 랭킹 표현이 결정론 빌더로 커버되지 않아
LLM 폴백에 의존했고, LLM 이 실DB 에 없는 컬럼(CUMULATIVE_PURCHASE_AMOUNT)을 지어내던 문제를
고정한다. 정답은 지표 레지스트리(member_metrics.json)의 실컬럼(TOTAL_BUY_AMT 등)을 회원키로
조인해 지표값 내림차순 상위 N 명을 뽑는 build_member_metric_ranking_sql_candidate 이다.

'<지표>가 높은 지역'(그룹/지역 랭킹, region_density_target)과 '<지표>가 높은 고객'(회원 랭킹)이
서로 침범하지 않는지(트랙 분리)도 함께 고정한다.

실행(컨테이너): docker compose exec -w /app -e PYTHONPATH=/app api pytest tests/test_member_metric_ranking.py -q
"""

import graph_rag as g


def _plan(query: str) -> dict:
    plan = g.build_query_plan(query, parser="rules")
    # 실제 파이프라인(retrieve)과 동일하게 회원/랭킹 신호로 unknown intent 를 승격한다.
    g._promote_unknown_intent_for_target_signal(plan)
    return plan


# 회원 단위 지표 랭킹으로 잡혀야 하는 표현형 -> (metric_id, 기대 실컬럼).
RANKING_PHRASINGS = [
    ("누적 구매 금액이 높은 고객", "total_buy_amt", "TOTAL_BUY_AMT"),
    ("매출이 높은 고객", "total_buy_amt", "TOTAL_BUY_AMT"),
    ("매출이 가장 높은 회원", "total_buy_amt", "TOTAL_BUY_AMT"),
    ("구매금액이 많은 고객", "total_buy_amt", "TOTAL_BUY_AMT"),
    ("구매횟수가 많은 고객", "total_buy_cnt", "TOTAL_BUY_CNT"),
    ("객단가가 높은 고객", "mean_buy_amt", "MEAN_BUY_AMT"),
]

# 실DB 에 없는 환각 컬럼(회귀 방지): 어떤 표현형에서도 SQL 에 나오면 안 된다.
HALLUCINATED_COLUMNS = ["CUMULATIVE_PURCHASE_AMOUNT", "CUMULATIVE_PURCHASE"]


def test_ranking_phrasings_extract_target():
    for query, metric_id, _column in RANKING_PHRASINGS:
        plan = _plan(query)
        ranking = plan.get("member_metric_ranking")
        assert isinstance(ranking, dict), f"{query!r}: member_metric_ranking 미추출 -> {ranking}"
        assert ranking["metric_id"] == metric_id, f"{query!r} -> {ranking['metric_id']} (기대 {metric_id})"


def test_ranking_phrasings_select_metric_ranking_template():
    for query, _metric_id, column in RANKING_PHRASINGS:
        plan = _plan(query)
        candidate = g.build_sql_template_candidate(plan)
        assert candidate is not None, f"{query!r}: 후보 없음"
        assert candidate["id"] == "sql_template:member_metric_ranking", f"{query!r} -> {candidate['id']}"
        sql = candidate["sql"]
        # 실컬럼(지표 레지스트리)과 상위 N 정렬이 SQL 에 실제로 있어야 한다.
        assert column in sql, f"{query!r}: 실컬럼 {column} 미참조"
        assert "ORDER BY" in sql.upper(), f"{query!r}: 정렬(ORDER BY) 누락"
        assert "CRM_MB_MONTHCRMINFO" in sql, f"{query!r}: 지표 테이블 미참조"
        # 월 스냅샷 중복 방지(최신 월 한정)가 반드시 있어야 한다.
        assert "YYYYMM" in sql, f"{query!r}: 최신 월 한정(grain_filter) 누락"
        for bad in HALLUCINATED_COLUMNS:
            assert bad not in sql, f"{query!r}: 환각 컬럼 {bad} 재발"


def test_top_n_override_parsed():
    plan = _plan("매출이 높은 고객 상위 50명")
    assert plan["member_metric_ranking"]["top_n"] == 50
    candidate = g.build_sql_template_candidate(plan)
    assert "TOP 50" in candidate["sql"]


def test_member_attributes_combined_into_ranking_sql():
    # "30대 여성 중 매출 높은 고객" 은 성별/연령 술어가 같은 SQL 에 AND 결합돼야 한다.
    plan = _plan("30대 여성 중 매출이 높은 고객")
    candidate = g.build_sql_template_candidate(plan)
    assert candidate["id"] == "sql_template:member_metric_ranking"
    sql = candidate["sql"]
    assert "GENDER_CD" in sql, f"성별 조건 미결합 -> {sql}"
    assert "AGE" in sql, f"연령 조건 미결합 -> {sql}"
    assert "TOTAL_BUY_AMT" in sql


# '<지표>가 높은 지역'(그룹 랭킹)은 회원 랭킹으로 오염되면 안 된다(트랙 분리 회귀).
REGION_RANKING_PHRASINGS = [
    "매출이 높은 지역",
    "구매금액이 많은 동네",
]


def test_region_ranking_not_hijacked_by_member_ranking():
    for query in REGION_RANKING_PHRASINGS:
        plan = _plan(query)
        assert isinstance(plan.get("region_density_target"), dict), f"{query!r}: 지역 랭킹 미추출"
        assert plan.get("member_metric_ranking") is None, f"{query!r}: 회원 랭킹으로 오염됨"
        candidate = g.build_sql_template_candidate(plan)
        assert candidate["id"] == "sql_template:dense_region_targets", f"{query!r} -> {candidate['id']}"

"""랭킹/상위 N 결함 6종의 회귀 코퍼스(요구사항 1~6).

배경: '랭킹·상위 N' 질의에서 다음 결함을 구조적으로 고쳤다.
  1. 천 단위 콤마('상위 1,000명' → top_n=1 로 잘림) → [\\d,]+ 허용 + 콤마 제거.
  2. 그룹별 Top-N('지역별로 … N명씩')이 전역 TOP N 으로 붕괴 → ROW_NUMBER() OVER(PARTITION BY) 윈도.
  3. 지표 동의어('주문 횟수'/'평균 주문 금액')를 중앙 레지스트리(member_metrics.json)에 보강.
  4. 적립금/잔액 랭킹이 최상급 없이 비교어+개수('적립금이 높은 회원 100명')여도 동작.
  5. 주문 집계 지표의 상위/하위 퍼센트('구매 횟수 상위 5%') → TOP N PERCENT 일반화.
  6. 지역 단위 회원 수 랭킹('회원 수 많은 시군구 상위 N개') → GROUP BY + COUNT(DISTINCT).

라우팅: 그룹별 회원 Top-N > 지역 집계 랭킹 > 전역 회원 랭킹 순으로 별도 실행 경로. '지역별로 매출 높은
회원 10명씩'(회원 10명씩/지역) 과 '매출 높은 지역 상위 10개'(지역 10개)를 서로 다른 라우트로 분리한다.

실행: python -m pytest tests/test_group_and_percent_ranking_targeting.py -q
"""

import networkx as nx

import graph_rag as g


def _plan(query: str) -> dict:
    plan = g.build_query_plan(query, parser="rules")
    g._promote_unknown_intent_for_target_signal(plan)
    return plan


def _sql(query: str):
    plan = _plan(query)
    res = g.build_sql_result(
        graph=nx.Graph(), query=query, query_plan=plan, context_nodes=[],
        schema_path=g.DEFAULT_SCHEMA_PATH, default_limit=None, original_query=query,
    )
    return plan, res


# ── (1) 천 단위 콤마 ────────────────────────────────────────────────────────────────────

def test_thousands_comma_not_truncated_directive():
    assert g._detect_ranking_directive("상위 1,000명")["top_n"] == 1000
    assert g._detect_ranking_directive("하위 2,500명")["top_n"] == 2500
    assert g._detect_ranking_directive("top 1,000")["top_n"] == 1000


def test_thousands_comma_equivalent_to_no_comma():
    assert g._detect_ranking_directive("상위 1,000명")["top_n"] == g._detect_ranking_directive("상위 1000명")["top_n"]


def test_thousands_comma_reaches_sql_top():
    _plan_, res = _sql("캠페인 구매금액이 높은 회원 상위 1,000명을 보여줘.")
    assert res["sql"] is not None
    assert "TOP 1000" in res["sql"] and "TOP 1 " not in res["sql"]


# ── (3) 지표 동의어(주문 횟수 계열 / 평균 주문 금액 계열) ───────────────────────────────

def test_order_count_synonyms_resolve_to_total_buy_cnt():
    for phrase in ["주문 횟수", "주문 건수", "구매 횟수", "구매 건수", "주문 수", "구매 수"]:
        r = _plan(f"{phrase}가 많은 회원 100명").get("member_metric_ranking")
        assert r and r["metric_id"] == "total_buy_cnt", f"{phrase!r} -> {r}"


def test_average_amount_synonyms_resolve_to_mean_buy_amt():
    for phrase in ["평균 주문 금액", "평균 구매 금액", "평균 결제 금액", "객단가"]:
        r = _plan(f"{phrase}이 높은 회원 50명").get("member_metric_ranking")
        assert r and r["metric_id"] == "mean_buy_amt", f"{phrase!r} -> {r}"


# ── (4) 적립금/잔액 랭킹: 비교어 + 명시 개수 ─────────────────────────────────────────────

def test_balance_ranking_with_bare_comparison_and_count():
    high = _plan("적립금이 높은 회원 100명").get("member_metric_selection")
    assert high and high["mode"] == "top_n" and high["n"] == 100 and high["direction"] == "high"
    low = _plan("적립금이 적은 회원 100명").get("member_metric_selection")
    assert low and low["direction"] == "low"


def test_balance_ranking_without_count_is_not_forced():
    # 개수·퍼센트·상위/하위 없는 비교어 단독은 임의 개수를 붙이지 않는다(랭킹 미확정).
    assert _plan("적립금이 높은 회원").get("member_metric_selection") is None


# ── (5) 주문 집계 지표의 상위/하위 퍼센트 ───────────────────────────────────────────────

def test_order_metric_top_percent():
    _plan_, res = _sql("구매 횟수가 많은 상위 5% 회원")
    r = _plan_.get("member_metric_ranking")
    assert r["limit_type"] == "percent" and r["percent"] == 5.0
    assert res["sql"] is not None and "TOP 5 PERCENT" in res["sql"]


def test_order_metric_bottom_percent_direction():
    _plan_, res = _sql("평균 주문 금액이 낮은 하위 3% 회원")
    r = _plan_.get("member_metric_ranking")
    assert r["limit_type"] == "percent" and r["percent"] == 3.0 and r["direction"] == "low"
    assert "TOP 3 PERCENT" in res["sql"] and "MEAN_BUY_AMT ASC" in res["sql"]


def test_percent_boundary_rejected():
    # 0% 이하·100% 초과는 랭킹으로 확정하지 않는다(경계 정책).
    assert _plan("매출이 높은 상위 0% 회원").get("member_metric_ranking") is None
    assert _plan("매출이 높은 상위 150% 회원").get("member_metric_ranking") is None


# ── (2) 그룹별 Top-N ─────────────────────────────────────────────────────────────────────

GROUP_CASES = [
    ("지역별로 매출이 높은 회원 10명씩", "total_buy_amt", "TOTAL_BUY_AMT", "SIGUNGU", 10, "high"),
    ("시군구별 주문 횟수가 많은 회원 5명씩", "total_buy_cnt", "TOTAL_BUY_CNT", "SIGUNGU", 5, "high"),
    ("지역별 평균 주문 금액이 낮은 회원 20명씩", "mean_buy_amt", "MEAN_BUY_AMT", "SIGUNGU", 20, "low"),
]


def test_group_ranking_parsed_fields():
    for query, metric_id, _col, group_col, top_n, direction in GROUP_CASES:
        gr = _plan(query).get("group_ranking_target")
        assert gr is not None, f"{query!r}: group_ranking_target 미추출"
        assert gr["metric_id"] == metric_id
        assert gr["group_column"] == group_col
        assert gr["top_n"] == top_n
        assert gr["direction"] == direction


def test_group_ranking_sql_uses_partition_window():
    for query, _metric_id, column, group_col, top_n, direction in GROUP_CASES:
        _plan_, res = _sql(query)
        sql = res["sql"]
        assert sql is not None, f"{query!r}: SQL 없음"
        # 공통 윈도 빌더: base CTE 가 group_key(그룹식) 계산, ranked CTE 가 PARTITION BY group_key.
        assert "PARTITION BY base.group_key" in sql, f"{query!r}: PARTITION BY 누락"
        assert "ROW_NUMBER()" in sql
        assert f"row_num <= {top_n}" in sql
        assert column in sql
        # 그룹 컬럼(지역 축)이 base 에서 group_key 로 계산돼 있다.
        assert f"B.{group_col} AS group_key" in sql
        # 그룹 표시 컬럼이 최종 결과에 유지된다.
        assert "target_region" in sql
        # 방향 반영(ORDER BY 지표 ASC/DESC).
        assert f"base.{_metric_id} {'ASC' if direction == 'low' else 'DESC'}" in sql


def test_group_ranking_not_stolen_by_global_ranking():
    # 전역 회원 랭킹(member_metric_ranking)이 그룹 질의를 가로채면 안 된다.
    plan = _plan("지역별로 매출이 높은 회원 10명씩")
    assert plan.get("member_metric_ranking") is None
    assert plan.get("region_density_target") is None


def test_global_ranking_has_no_partition_by():
    # 그룹 표지 없는 전역 랭킹 SQL 에는 불필요한 PARTITION BY 가 없어야 한다.
    _plan_, res = _sql("매출이 높은 회원 100명")
    assert res["sql"] is not None
    assert "PARTITION BY" not in res["sql"]
    assert "TOP 100" in res["sql"]


def test_group_count_not_applied_as_global_limit():
    # 'N명씩'은 그룹당 개수라 전역 result_limit 로 새면 안 된다(전역 TOP 오적용 방지).
    plan = _plan("지역별로 매출이 높은 회원 10명씩")
    assert plan.get("result_limit") is None


# ── (6) 지역 단위 회원 수 랭킹 ───────────────────────────────────────────────────────────

def test_region_member_count_ranking_parsed():
    high = _plan("회원 수가 많은 시군구 상위 10개").get("region_member_count_target")
    assert high and high["column"] == "SIGUNGU" and high["top_n"] == 10 and high["direction"] == "high"
    low = _plan("회원 수가 적은 지역 하위 20개").get("region_member_count_target")
    assert low and low["direction"] == "low" and low["top_n"] == 20
    superlative = _plan("회원이 가장 많은 지역 5곳").get("region_member_count_target")
    assert superlative and superlative["top_n"] == 5


def test_region_member_count_sql_shape():
    _plan_, res = _sql("회원 수가 많은 시군구 상위 10개")
    sql = res["sql"]
    assert sql is not None
    assert "GROUP BY B.SIGUNGU" in sql
    assert "COUNT(DISTINCT B.MEMBER_NO)" in sql
    assert "TOP 10" in sql
    assert "member_count" in sql


def test_region_member_count_without_ranking_lists_all_groups():
    _plan_, res = _sql("시군구별 회원 수")
    sql = res["sql"]
    assert sql is not None
    assert "GROUP BY B.SIGUNGU" in sql
    # 순위 요청이 없으면 TOP 을 붙이지 않는다(전체 그룹).
    assert "TOP" not in sql


def test_purchase_word_does_not_trigger_single_syllable_region_granularity():
    for query in ("구매한 고객 수를 알려줘", "최근 30일 동안 구매한 고객 수", "주문한 회원 수"):
        plan = _plan(query)
        assert plan.get("region_member_count_target") is None
        assert plan.get("region_density_target") is None


def test_group_axis_suffix_is_not_treated_as_purchase_object():
    for axis in ("브랜드별", "카테고리별", "등급 별"):
        plan = {
            "aggregation_request": {"groupings": [{"field": {"column": "GROUP_KEY"}}]},
            "target_user": {"purchase_object": axis, "purchase_object_kind": "product"},
        }
        g._normalize_aggregation_axis_filters(plan)
        assert "purchase_object" not in plan["target_user"]
        assert "purchase_object_kind" not in plan["target_user"]


def test_real_purchase_object_survives_group_axis_normalization():
    plan = {
        "aggregation_request": {"groupings": [{"field": {"column": "BRAND_ID"}}]},
        "target_user": {"purchase_object": "기저귀", "purchase_object_kind": "product"},
    }
    g._normalize_aggregation_axis_filters(plan)
    assert plan["target_user"] == {"purchase_object": "기저귀", "purchase_object_kind": "product"}


# ── 라우팅 분리: 회원 대상 vs 지역 대상 ─────────────────────────────────────────────────

def test_member_grouped_vs_region_ranking_distinct_routes():
    # '지역별로 매출 높은 회원 10명씩' = 그룹별 회원 추출(PARTITION BY 윈도).
    member_route = _sql("지역별로 매출이 높은 회원 10명씩")[1]["sql"]
    assert "PARTITION BY" in member_route and "ROW_NUMBER()" in member_route
    # '매출 높은 지역 상위 10개' = 지역 랭킹(밀집 지역, GROUP BY 지역).
    region_plan = _plan("매출이 높은 지역 상위 10개")
    assert isinstance(region_plan.get("region_density_target"), dict)
    assert region_plan.get("group_ranking_target") is None


# ── 지원 외 그룹 축(등급/채널)만 명시 미지원(지역/성별/연령대는 실제 SQL 생성) ──────────────

def test_unsupported_group_axis_is_explicit_not_silent_global():
    # 등급/채널 등 미구현 축은 조용히 전역 랭킹으로 붕괴시키지 않고 명시 미지원으로 돌린다.
    _plan_, res = _sql("등급별 매출이 높은 회원 10명씩")
    assert res["sql"] is None
    assert res.get("unsupported_reason") == "group_ranking_axis_unsupported"


def test_gender_attribute_filter_not_misgated_as_group_axis():
    # '성별이 여성인'(속성 필터)은 그룹 축('성별로')이 아니므로 group_ranking_axis 로 오게이트하면 안 된다.
    plan = _plan("성별이 여성인 회원 상위 100명")
    assert (plan.get("unsupported") or {}).get("reason") != "group_ranking_axis_unsupported"


def test_region_density_column_no_double_prefix():
    # 회귀: 지역 랭킹 컬럼이 'M.B.SIGUNGU'/'B.B.SIGUNGU' 이중 접두어가 되지 않는다(맨 컬럼명 저장).
    sql = _sql("매출이 높은 지역 상위 10개")[1]["sql"]
    assert "B.B." not in sql and "M.B." not in sql
    assert "M.SIGUNGU" in sql

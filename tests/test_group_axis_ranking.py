"""그룹별 회원 Top-N 축 일반화(지역/성별/연령대) + 오탐 방지 + 기간 경계 + rules/auto 동등성 회귀.

후속 작업 목표: 그룹별 Top-N 을 지역뿐 아니라 성별·연령대까지 **실제 SQL**(ROW_NUMBER() OVER PARTITION BY)로
지원하고, 축을 하드코딩 분기 복제가 아니라 공통 그룹 축 resolver(_resolve_group_axis)로 일반화한다.

검증:
  - group_axis / per_group / 그룹 SQL 식(PARTITION BY) / 지표 / 방향 / top_n / 대상 엔티티
  - 지역/성별/연령대 모두 실제 SQL 생성(미지원 처리 아님), 전역 Top-N 으로 조용히 붕괴하지 않음
  - 일반 문장('행동별로'·'특별로'·'개별로'·'상품별로가 아니라')의 그룹 마커 오탐 없음
  - 기간 스코프('최근 3개월/2025년/지난달')는 최신 월 스냅샷 랭킹으로 오라우팅되지 않고 명시 미지원
  - rules 경로와 auto 경로의 그룹 슬롯 의미 동등성 + LLM 오라벨 교정 가드

주의: 실제 LLM(OpenAI) API 통합 테스트는 미실행이다(키 없음). auto 경로는 키가 없으면 결정론 필터로
degrade 하므로, 여기서는 결정론 필터가 두 경로에서 동일하게 그룹 축을 확정/교정하는지만 검증한다.

실행: python -m pytest tests/test_group_axis_ranking.py -q
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


# (query, axis, group_expr_fragment, select_alias, metric_id, metric_col, top_n, direction)
AXIS_CASES = [
    ("지역별로 매출이 높은 회원 10명씩", "region", "B.SIGUNGU", "target_region", "total_buy_amt", "TOTAL_BUY_AMT", 10, "high"),
    ("시군구별 구매 횟수가 많은 회원 5명씩", "region", "B.SIGUNGU", "target_region", "total_buy_cnt", "TOTAL_BUY_CNT", 5, "high"),
    ("성별로 구매 횟수가 많은 회원 10명씩", "gender", "B.GENDER_CD", "gender", "total_buy_cnt", "TOTAL_BUY_CNT", 10, "high"),
    ("성별로 평균 주문 금액이 낮은 회원 5명씩", "gender", "B.GENDER_CD", "gender", "mean_buy_amt", "MEAN_BUY_AMT", 5, "low"),
    ("연령대별 매출이 높은 회원 10명씩", "age_group", "CASE WHEN B.AGE", "age_group", "total_buy_amt", "TOTAL_BUY_AMT", 10, "high"),
    ("나이대별 주문 횟수가 적은 회원 20명씩", "age_group", "CASE WHEN B.AGE", "age_group", "total_buy_cnt", "TOTAL_BUY_CNT", 20, "low"),
]


def test_group_axis_parsed_intent_fields():
    for query, axis, _expr, _alias, metric_id, _col, top_n, direction in AXIS_CASES:
        gr = _plan(query).get("group_ranking_target")
        assert gr is not None, f"{query!r}: group_ranking_target 미추출"
        assert gr["group_axis"] == axis, f"{query!r}: axis={gr['group_axis']} (기대 {axis})"
        assert gr["target_entity"] == "member"
        assert gr["per_group"] is True
        assert gr["limit_type"] == "count"
        assert gr["metric_id"] == metric_id
        assert gr["top_n"] == top_n
        assert gr["direction"] == direction


def test_group_axis_generates_real_partition_window_sql():
    for query, axis, expr_fragment, select_alias, _metric_id, metric_col, top_n, direction in AXIS_CASES:
        _plan_, res = _sql(query)
        sql = res["sql"]
        assert sql is not None, f"{query!r}: 미지원 처리됨(실제 SQL 필요)"
        # 공통 윈도 빌더: 축별 그룹식이 base.group_key 로 주입되고 PARTITION BY group_key 로 순위.
        assert expr_fragment in sql, f"{query!r}: 그룹식 {expr_fragment} 누락"
        assert "PARTITION BY base.group_key" in sql
        assert "ROW_NUMBER()" in sql
        assert f"AS {select_alias}" in sql, f"{query!r}: 그룹 표시 컬럼 {select_alias} 누락"
        assert f"row_num <= {top_n}" in sql
        assert metric_col in sql
        assert f"base.{_metric_id} {'ASC' if direction == 'low' else 'DESC'}" in sql
        # 전역 Top-N 으로 붕괴하지 않았다(그룹당 제한이지 전역 TOP 아님).
        assert "TOP" not in sql.upper().split("ROW_NUMBER")[0] or "PERCENT" not in sql


def test_group_axis_no_silent_global_collapse():
    for query, _axis, _expr, _alias, _mid, _col, _n, _dir in AXIS_CASES:
        plan = _plan(query)
        # 그룹 질의가 전역 회원 랭킹/지역 밀집으로 새면 안 된다.
        assert plan.get("member_metric_ranking") is None, f"{query!r}: 전역 랭킹으로 붕괴"
        assert plan.get("region_density_target") is None


def test_age_group_uses_central_age_band_from_config():
    # 연령대 CASE 는 config(group_ranking_axes.age_group.age_band) 중앙 정의를 쓴다 — 하드코딩 아님.
    resolver = g._resolve_group_axis("age_group")
    assert resolver is not None and "CASE WHEN B.AGE" in resolver.group_expr
    # config 밴드 라벨이 식에 반영된다.
    assert "20대" in resolver.group_expr and "60대 이상" in resolver.group_expr


def test_null_group_excluded_by_policy():
    # 성별/연령대 NULL·미분류 회원은 그룹 정책상 제외된다(명시 정책).
    assert "B.GENDER_CD IS NOT NULL" in _sql("성별로 매출이 높은 회원 5명씩")[1]["sql"]
    assert "B.AGE IS NOT NULL" in _sql("연령대별 매출이 높은 회원 5명씩")[1]["sql"]


# ── 그룹 마커 오탐 방지 ─────────────────────────────────────────────────────────────────

FALSE_POSITIVES = [
    "행동별로 회원을 분류해줘",
    "상품별로가 아니라 전체 매출을 보여줘",
    "특별로 관리되는 회원",
    "개별로 조회해줘",
]


def test_group_marker_no_false_positive():
    for query in FALSE_POSITIVES:
        plan = _plan(query)
        assert plan.get("group_ranking_target") is None, f"{query!r}: 그룹 마커 오탐"
        assert g._detect_group_axis(query) is None, f"{query!r}: 축 오탐 -> {g._detect_group_axis(query)}"


def test_gender_attribute_not_group_axis():
    # '성별이 여성인'(속성)은 그룹 축('성별로')이 아니다.
    assert g._detect_group_axis("성별이 여성인 회원 상위 100명") is None


# ── 기간 vs 최신 월 스냅샷 라우팅 경계 ───────────────────────────────────────────────────

def test_plain_metric_ranking_uses_snapshot():
    # 기간 없는 지표 랭킹은 최신 월 스냅샷(YYYYMM=MAX) 경로를 유지한다.
    plan, res = _sql("구매 횟수가 많은 회원 100명")
    assert plan.get("member_metric_ranking") is not None
    assert "YYYYMM = (SELECT MAX(YYYYMM)" in res["sql"]


def test_period_scoped_ranking_not_routed_to_snapshot():
    for query in [
        "최근 3개월 구매 횟수가 많은 회원 100명",
        "2025년 구매 금액이 높은 회원 50명",
        "지난달 주문 횟수가 많은 회원 20명",
    ]:
        plan, res = _sql(query)
        # 스냅샷 랭킹으로 조용히 보내지 않는다.
        assert plan.get("member_metric_ranking") is None, f"{query!r}: 스냅샷 랭킹으로 오라우팅"
        assert res["sql"] is None
        assert res.get("unsupported_reason") == "period_scoped_ranking_unsupported", f"{query!r} -> {res.get('unsupported_reason')}"


# ── period_scoped_ranking 게이트: 구조 기반(정렬키 지표) 판정 회귀 ───────────────────────────
# 원칙: 게이트는 원문 키워드 공존(기간어+지표어+상위N)이 아니라, 파싱 구조(정렬키 지표 결합 여부)로 판단한다.
# 진짜 기간 스코프 랭킹 = 기간 스코프 + 기간에 결합된 지표 정렬키(ORDER BY) 존재. 임계값으로만 소비된 지표는
# 정렬키가 아니고, '상위 N'은 단순 result_limit 캡이다.

# 게이트를 발동하지 않아야 하는 케이스(정렬키 없음 → SQL 생성) — (query, 기대 aggregate metric_id 집합)
_THRESHOLD_CAP_PASS_CASES = [
    ("최근 6개월 동안 캠페인에 반응했고, 구매 횟수 10회 이상, 평균 주문 금액 50,000원 이상인 고객 상위 100명",
     {"order_count", "average_order_amount"}),
    ("최근 6개월 동안 캠페인에 반응했고, 구매 횟수 10회 이상인 고객 100명만",
     {"order_count"}),
    ("구매 횟수 10회 이상, 평균 주문 금액 50,000원 이상인 고객 상위 100명",
     {"order_count", "average_order_amount"}),
]


def test_threshold_capped_audience_produces_sql_not_ranking_gate():
    for query, expected_metrics in _THRESHOLD_CAP_PASS_CASES:
        plan, res = _sql(query)
        aggs = plan.get("target_user", {}).get("aggregate_conditions") or []
        assert {a["metric_id"] for a in aggs} >= expected_metrics, f"{query!r}: 임계 지표 소실 {aggs}"
        # 정렬키가 결합되지 않았다 → member_metric_ranking 과결합 없음, ORDER BY 없음, 게이트 미발동.
        assert g._resolve_ranking_sort_metric_id(query) is None, f"{query!r}: 정렬키 오검출"
        assert plan.get("member_metric_ranking") is None, f"{query!r}: 랭킹 과결합"
        assert plan.get("unsupported") is None, f"{query!r}: 오탐 {plan.get('unsupported')}"
        assert res.get("unsupported_reason") is None
        assert res["sql"] is not None, f"{query!r}: SQL 미생성"
        assert plan.get("result_limit") == 100
        assert "ORDER BY" not in (res["sql"] or ""), f"{query!r}: 정렬키 없는데 ORDER BY 생성"
        assert "HAVING" in (res["sql"] or ""), f"{query!r}: HAVING 임계 소실"


# 게이트를 발동해야 하는 진짜 기간 스코프 지표 랭킹(정렬키가 기간에 결합됨).
_PERIOD_RANKING_BLOCK_CASES = [
    "최근 6개월 동안 구매 횟수가 가장 많은 고객 상위 100명",
    "최근 6개월 평균 주문 금액 기준 상위 100명",
    "최근 6개월 구매 금액 순으로 고객 100명",
]


def test_actual_period_scoped_ranking_still_blocked():
    for query in _PERIOD_RANKING_BLOCK_CASES:
        plan, res = _sql(query)
        assert g._resolve_ranking_sort_metric_id(query) is not None, f"{query!r}: 정렬키 미검출"
        assert plan.get("member_metric_ranking") is None
        assert res["sql"] is None, f"{query!r}: 차단돼야 하는데 SQL 생성"
        assert res.get("unsupported_reason") == "period_scoped_ranking_unsupported", f"{query!r} -> {res.get('unsupported_reason')}"


def test_mixed_threshold_and_ranking_metric_blocked():
    # 절대 임계 조건과 실제 지표 랭킹이 함께 존재 → aggregate_conditions 가 있어도 평균 주문 금액이 정렬키이므로 차단.
    query = "최근 6개월 구매 횟수 10회 이상인 고객 중 평균 주문 금액이 높은 상위 100명"
    plan, res = _sql(query)
    # member_metrics 레지스트리 기준 '평균 주문 금액' 지표 id 는 mean_buy_amt(aggregate_targets 의
    # average_order_amount 와 별개 레지스트리)다 — 정렬키가 이 지표로 결합돼야 한다.
    assert g._resolve_ranking_sort_metric_id(query) == "mean_buy_amt", "정렬키는 평균 주문 금액이어야 한다"
    assert plan.get("target_user", {}).get("aggregate_conditions"), "임계 조건도 존재해야 한다"
    assert res.get("unsupported_reason") == "period_scoped_ranking_unsupported"


def test_ranking_sort_metric_detector_boundaries():
    # 랭킹 어구('기준/순/많은/높은/상위')에 결합된 지표만 정렬키다.
    assert g._resolve_ranking_sort_metric_id("구매 횟수가 가장 많은 고객 상위 100명") == "total_buy_cnt"
    assert g._resolve_ranking_sort_metric_id("평균 주문 금액 기준 상위 100명") == "mean_buy_amt"
    assert g._resolve_ranking_sort_metric_id("구매 금액 순으로 고객 100명") is not None
    # 임계값('N 이상')에만 결합된 지표는 정렬키가 아니다 → None(단순 캡).
    assert g._resolve_ranking_sort_metric_id("구매 횟수 10회 이상, 평균 주문 금액 5만원 이상인 상위 100명") is None
    assert g._resolve_ranking_sort_metric_id("구매 횟수 10회 이상인 고객 100명만") is None


# ── rules / auto 경로 동등성 + LLM 오라벨 교정 가드 ──────────────────────────────────────
# 주의: 실제 OpenAI API 통합은 미실행(키 없음). auto 는 키 없으면 결정론 필터로 degrade 한다.

def _auto_plan(query: str) -> dict:
    plan = g.build_query_plan(query, parser="auto")
    g._promote_unknown_intent_for_target_signal(plan)
    return plan


def test_rules_auto_group_slot_semantics_equivalent():
    fields = ("group_axis", "per_group", "metric_id", "direction", "limit_type", "top_n", "target_entity")
    for query, *_rest in AXIS_CASES:
        r = _plan(query).get("group_ranking_target")
        a = _auto_plan(query).get("group_ranking_target")
        assert r is not None and a is not None, f"{query!r}: 한 경로에서 그룹 슬롯 없음(rules={bool(r)}, auto={bool(a)})"
        assert {k: r.get(k) for k in fields} == {k: a.get(k) for k in fields}, f"{query!r}: rules/auto 슬롯 불일치"


def test_deterministic_guard_corrects_llm_global_ranking():
    # LLM(auto)이 그룹 질의를 전역 랭킹으로 잘못 채워 반환한 상황을 fixture 로 재현 — 결정론 그룹 파서가
    # 그룹으로 교정하고 잘못된 전역 슬롯을 제거한다(두 슬롯 공존 시 커버리지 충돌로 조용히 탈락 방지).
    plan = {
        "intent": "find_user_segment",
        "target_user": {},
        "member_metric_ranking": {  # LLM 오라벨: 그룹을 무시한 전역 랭킹
            "metric_id": "total_buy_amt", "metric_label": "매출", "top_n": 10, "direction": "high", "limit_type": "count",
        },
    }
    g._apply_group_ranking_target("지역별로 매출이 높은 회원 10명씩", plan)
    assert isinstance(plan.get("group_ranking_target"), dict)
    assert plan["group_ranking_target"]["group_axis"] == "region"
    assert plan.get("member_metric_ranking") is None  # 교정: 전역 슬롯 제거


def test_deterministic_parser_uses_query_axis_not_llm_axis():
    # 축은 LLM 플랜이 아니라 질의에서 판정한다 — LLM 이 엉뚱한 축을 줘도 질의의 성별/연령대를 따른다.
    plan = {"intent": "find_user_segment", "target_user": {},
            "group_ranking_target": None}
    g._apply_group_ranking_target("성별로 구매 횟수가 많은 회원 10명씩", plan)
    assert plan["group_ranking_target"]["group_axis"] == "gender"

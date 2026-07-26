"""A+F 회귀 테스트: average_order_amount 유효 SQL 생성 + 무효 집계(SUM(None) 등) validation 차단.

배경(#1~5): 객단가/평균 주문 금액이 aggregate_targets 에서 `expression`(agg+column 아님)으로 정의됐는데
집계 빌더가 agg+column 만 처리해 `HAVING SUM(None)` 무효 SQL 을 만들었고, SQL 가드가 이를 못 잡아
**무효 SQL 이 성공 후보로 출고**됐다. 이제:
 - 지표 소스를 명확히 분리: 회원 요약 컬럼(MEAN_BUY_AMT, 기간창 없을 때 우선) / 집계식(expression, 기간창
   반영) / agg+column. 셋 다 해석 불가면 후보를 무효(unsupported)로 표시하고 다른 트랙으로 폴백하지 않는다.
 - expression 은 `{t}` 자리표시자로 서브쿼리 별칭과 일치시키고, 미해석 별칭/리터럴 None 은 렌더 단계에서 거부.
 - SQL validation(validate_analytics_shape)이 SUM(None)/AVG(None)/COUNT(None)/빈 인자/리터럴 None 을
   서브쿼리 포함 전체에서 error 로 차단 → 무효 SQL 이 success 후보로 출고되지 않는다.

실행(컨테이너): docker compose exec -w /app -e PYTHONPATH=/app api pytest tests/test_average_order_amount_and_agg_guard.py -q
"""

import copy

import networkx as nx

import graph_rag as g
import sql_guard as sg


def _plan(query: str) -> dict:
    plan = g.build_query_plan(query)
    g._promote_unknown_intent_for_target_signal(plan)
    return plan


def _sql(query: str) -> str:
    candidate = g.build_sql_template_candidate(_plan(query))
    assert candidate is not None, f"{query!r}: 후보 없음"
    return candidate["sql"]


# --- average_order_amount 유효 SQL(회원 요약 컬럼: 기간창 없음) ---

def test_aov_threshold_uses_member_summary_column():
    sql = _sql("객단가가 30,000원 이상인 고객을 추출해줘.")
    assert "MEAN_BUY_AMT >= 30000" in sql
    assert "CRM_MB_MONTHCRMINFO" in sql
    assert "SUM(None)" not in sql and "None" not in sql


def test_aov_threshold_synonym_and_operator_variants():
    assert "MEAN_BUY_AMT > 100000" in _sql("객단가가 100,000원을 초과하는 고객을 추출해줘.")
    assert "MEAN_BUY_AMT < 50000" in _sql("평균 주문 금액이 50,000원 미만인 회원을 추출해줘.")


# --- average_order_amount 유효 SQL(집계식: 기간창 있음) ---

def test_aov_with_window_uses_expression_bare_columns():
    sql = _sql("최근 90일 객단가가 200,000원 이상인 고객")
    # {t} 자리표시자가 alias-less 서브쿼리에 맞춰 접두어 없이 렌더된다(별칭 미해석 없음).
    assert "HAVING SUM(PAYMENT_AMT) / NULLIF(COUNT(DISTINCT ORDER_ID), 0) >= 200000" in sql
    assert "ORDER_DATE >=" in sql and "DATEADD(DAY, -90, GETDATE())" in sql
    assert "{t}" not in sql and "None" not in sql


# --- 평균 대비 비교(직전 게이트) ---

def test_aov_average_comparison_still_unsupported():
    plan = _plan("평균 결제 금액이 평균보다 높은 회원을 찾아줘.")
    assert (plan.get("unsupported") or {}).get("reason") == "average_comparison_metric_unsupported"
    assert g.build_sql_template_candidate(plan) is None


# --- 렌더 헬퍼: 별칭 일치/미해석 별칭·리터럴 None 거부 ---

def test_expression_render_placeholder_and_guards():
    assert g._render_aggregate_expression("SUM({t}PAYMENT_AMT)", "") == "SUM(PAYMENT_AMT)"
    # 미해석 별칭(리터럴 OH.)·리터럴 None·미치환 자리표시자는 거부(None).
    assert g._render_aggregate_expression("SUM(OH.PAYMENT_AMT)", "") is None
    assert g._render_aggregate_expression("SUM(None)", "") is None
    assert g._render_aggregate_expression("SUM({x}COL)", "") is None


# --- F: SQL validation 이 무효 집계를 error 로 차단 ---

def test_validation_blocks_invalid_aggregate_forms():
    invalid = [
        "SELECT MEMBER_NO FROM T GROUP BY MEMBER_NO HAVING SUM(None) >= 5",
        "SELECT x FROM T HAVING AVG(None) > 1",
        "SELECT x FROM T HAVING COUNT(None) > 1",
        "SELECT x FROM T HAVING SUM() > 1",
        "SELECT None FROM T",
    ]
    for sql in invalid:
        result = sg.validate_analytics_shape(sql)
        assert result["is_valid"] is False, sql
        assert any(i["code"] in ("invalid_aggregate_argument", "unresolved_identifier_none") for i in result["issues"]), sql


def test_validation_allows_valid_aggregate_forms():
    valid = [
        "SELECT MEMBER_NO, COUNT(*) FROM T GROUP BY MEMBER_NO",
        "SELECT MEMBER_NO FROM T GROUP BY MEMBER_NO HAVING SUM(PAYMENT_AMT) >= 5",
        "SELECT MEMBER_NO FROM T GROUP BY MEMBER_NO HAVING SUM(PAYMENT_AMT) / NULLIF(COUNT(DISTINCT ORDER_ID), 0) >= 5",
    ]
    for sql in valid:
        assert sg.validate_analytics_shape(sql)["is_valid"] is True, sql


# --- 무효 지표(컬럼/식/요약 없음, 별칭 미해석)는 성공 후보로 출고되지 않고 미지원 표시 ---

def test_unresolvable_metric_not_emitted_as_success():
    cfg = g._MEMBER_TARGET_FILTERS["aggregate_targets"]
    saved = copy.deepcopy(cfg["metrics"]["average_order_amount"])
    # 컬럼/식/요약 어느 소스도 없는 무효 지표.
    cfg["metrics"]["average_order_amount"] = {"ko_label": "평균 주문 금액", "synonyms": ["객단가", "평균 주문 금액"]}
    try:
        plan = _plan("객단가가 30,000원 이상인 고객")
        assert g.build_sql_template_candidate(plan) is None
        assert (plan.get("unsupported") or {}).get("reason") == "unresolved_aggregate_column"
    finally:
        cfg["metrics"]["average_order_amount"] = saved


def test_bad_alias_expression_not_emitted_as_success():
    cfg = g._MEMBER_TARGET_FILTERS["aggregate_targets"]
    saved = copy.deepcopy(cfg["metrics"]["average_order_amount"])
    # {t} 대신 리터럴 별칭(OH.)을 박은 설정 — 서브쿼리에 없는 별칭이라 무효.
    cfg["metrics"]["average_order_amount"] = {
        "ko_label": "평균 주문 금액", "synonyms": ["객단가", "평균 주문 금액"],
        "expression": "SUM(OH.PAYMENT_AMT) / NULLIF(COUNT(DISTINCT OH.ORDER_ID), 0)",
    }
    try:
        plan = _plan("최근 30일 객단가가 30,000원 이상인 고객")  # 기간창 → expression 경로
        assert g.build_sql_template_candidate(plan) is None
        assert (plan.get("unsupported") or {}).get("reason") == "unresolved_aggregate_column"
    finally:
        cfg["metrics"]["average_order_amount"] = saved

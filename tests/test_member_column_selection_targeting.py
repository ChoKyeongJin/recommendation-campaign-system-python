"""회원 컬럼(잔액) 선택 전략 타겟 — 상위 N 명 / 상위 N% / 평균 대비.

배경: '예치금이 가장 많은 100명', '상위 5%', '평균보다 높은'은 WHERE 임계값으로 표현 못 하는
정렬·퍼센타일·평균 비교라, 잔액 임계 파서(balance_conditions)가 소유를 포기하고(오답 방지) 이 전용
경로가 정렬(TOP/PERCENT)·비상관 AVG 서브쿼리로 뽑는다(예치금은 CRM_MB_BASEINFO 컬럼 → 조인 없음).

실행(컨테이너): docker compose exec -w /app -e PYTHONPATH=/app api pytest tests/test_member_column_selection_targeting.py -q
"""

import graph_rag as g


def _plan(query: str) -> dict:
    plan = g.build_query_plan(query, parser="rules")
    g._promote_unknown_intent_for_target_signal(plan)  # 실 파이프라인 단계(신호 있으면 조회 의도로 승격)
    return plan


def _sql(query: str) -> str:
    candidate = g.build_sql_template_candidate(_plan(query))
    assert candidate is not None, f"{query!r}: SQL 미생성(후보 없음)"
    return candidate["sql"]


def test_top_n_by_balance():
    plan = _plan("예치금이 가장 많은 회원 100명을 추출해줘")
    sel = plan["member_metric_selection"]
    assert sel["mode"] == "top_n" and sel["n"] == 100 and sel["direction"] == "high"
    sql = _sql("예치금이 가장 많은 회원 100명을 추출해줘")
    assert "TOP 100 " in sql
    assert "ORDER BY B.DEPOSIT_BALANCE_AMT DESC" in sql
    # 선택 전략이면 WHERE 임계값(balance_conditions)은 잡히지 않는다(상호배타).
    assert not plan["target_user"].get("balance_conditions")


def test_top_percent_by_balance():
    sql = _sql("예치금 보유액 기준 상위 5% 회원을 찾아줘")
    assert "TOP 5 PERCENT " in sql
    assert "ORDER BY B.DEPOSIT_BALANCE_AMT DESC" in sql


def test_above_average_balance_uses_avg_subquery():
    sql = _sql("예치금 잔액이 평균보다 높은 고객을 보여줘")
    assert "B.DEPOSIT_BALANCE_AMT > (SELECT AVG(DEPOSIT_BALANCE_AMT) FROM CRM_MB_BASEINFO" in sql
    # 비상관 서브쿼리라 별칭을 쓰지 않는다(별칭 허용목록에서 걸리지 않게).
    assert "B2" not in sql


def test_below_average_and_bottom_n_direction():
    assert "B.DEPOSIT_BALANCE_AMT < (SELECT AVG" in _sql("예치금 잔액이 평균보다 낮은 고객")
    bottom = _sql("예치금이 가장 적은 50명")
    assert "TOP 50 " in bottom and "ORDER BY B.DEPOSIT_BALANCE_AMT ASC" in bottom


def test_average_inclusive_boundary():
    # '평균 이상'은 경계 포함(>=), '평균보다 높은'은 배타(>).
    assert "B.CARROT_BALANCE_AMT >= (SELECT AVG" in _sql("적립금이 전체 회원 평균 이상인 고객을 찾아줘")
    assert "B.DEPOSIT_BALANCE_AMT > (SELECT AVG" in _sql("예치금 잔액이 평균보다 높은 고객")


def test_carrot_balance_ranking():
    # 적립금(CARROT_BALANCE_AMT)도 같은 경로로 동작한다.
    sql = _sql("적립금이 가장 많은 200명")
    assert "TOP 200 " in sql and "ORDER BY B.CARROT_BALANCE_AMT DESC" in sql


def test_plain_threshold_is_not_selection():
    # '예치금 10만원 이상'은 선택 전략이 아니라 WHERE 임계값이다(경계 혼동 방지).
    plan = _plan("예치금 10만원 이상 회원")
    assert plan.get("member_metric_selection") is None
    assert plan["target_user"].get("balance_conditions")

"""'2018, 2019년도에 총금액이 10만 원 이상인 회원' 회귀 — 나열형 연도 창이 통째로 사라지던 사고.

배경(실제 /target-sql 호출에서 재현된 3중 결함):
  1. 타겟 절과 채널 절이 분리되면(retrieval_scope="targeting") 창의 소속을 판정하던 표면어('구매')가
     채널 절('구매 촉진 캠페인')로 잘려나가, 타겟 절에는 창만 남고 주인이 없어 purchase_date 가 아예
     안 잡혔다 → 기간 필터 없는 전 기간 SUM(PAYMENT_AMT) 집계가 출고됐다.
  2. 게이트를 통과해도 창은 '가장 좁은 것 하나'만 살아남아 2019년이 조용히 사라졌다.
  3. 원문의 베어 연도('2018,')는 '년' 접미어가 없어 달력 문법이 아예 읽지 못했다.
  4. 결정론 검증(dropped_signal_warnings)은 기간 family 를 보지 않아 이 드롭을 잡지 못했다.

고정 내용: 위 네 가지 + '귀속할 수 없는 창은 조용히 버리지 않고 경고한다'(fail-close).

실행(컨테이너): docker compose exec -w /app -e PYTHONPATH=/app api pytest tests/test_multi_year_window_targeting.py -q
"""

import calendar_window as cw
import graph_rag as g


PROMPT = "2018, 2019년도에 총금액이 10만 원 이상인 회원을 대상으로 구매촉진 캠페인을 만들어줘."
# 프롬프트 재작성기가 만드는 문장(응답 normalized_query) + 실제 plan 이 세워지는 타겟 절.
REWRITTEN = "2018년 및 2019년에 총금액이 10만 원 이상인 회원을 대상으로 구매 촉진 캠페인을 진행합니다."
TARGETING_CLAUSE = "2018년 및 2019년에 총금액이 10만 원 이상인 회원을 대상으로"


def _plan(query: str) -> dict:
    return g.build_query_plan(query, parser="rules")


def test_bare_year_enumeration_is_parsed():
    """'2018,' 처럼 '년'이 생략된 앞쪽 연도도 뒤쪽 '년'을 상속해 창이 된다."""
    assert [w["label"] for w in cw.parse_calendar_window_group(PROMPT)] == ["2018년", "2019년"]


def test_window_group_keeps_every_year():
    """나열은 한 조건의 여러 구간이므로 전부 살아남는다(가장 좁은 하나로 줄지 않는다)."""
    for query in (PROMPT, REWRITTEN):
        purchase_date = _plan(query)["target_user"]["purchase_date"]
        assert purchase_date["windows"] == [
            {"from": "20180101", "to": "20181231"},
            {"from": "20190101", "to": "20191231"},
        ]


def test_window_survives_when_purchase_word_is_in_the_other_clause():
    """타겟 절만으로 plan 을 세워도(구매 표면어가 채널 절로 잘려나가도) 창이 남는다.

    창의 소속은 표면어가 아니라 plan 이 이미 요구하는 팩트(주문 집계 조건)로 정해진다."""
    target_user = _plan(TARGETING_CLAUSE)["target_user"]
    assert "구매" not in TARGETING_CLAUSE  # 표면어 게이트로는 잡을 수 없는 문장임을 고정
    assert target_user["purchase_date"]["from"] == "20180101"
    assert target_user["purchase_date"]["to"] == "20191231"
    assert target_user["aggregate_conditions"][0]["metric_id"] == "purchase_amount"


def test_generated_sql_restricts_orders_to_both_years():
    """집계 서브쿼리가 두 해의 주문만 세도록 ORDER_DATE 로 한정된다(전 기간 합계 금지)."""
    plan = _plan(TARGETING_CLAUSE)
    sql = g.build_aggregate_targets_sql_candidate(plan)["sql"]
    assert "ORDER_DATE BETWEEN '20180101' AND '20191231'" in sql  # 맞닿은 두 해는 한 구간으로 병합
    assert "SUM(PAYMENT_AMT) >= 100000" in sql


def test_disjoint_windows_compile_to_or_of_ranges():
    """맞닿지 않은 나열은 병합하지 않고 구간마다 BETWEEN 을 OR 로 묶는다(사이 기간 유입 금지)."""
    plan = _plan("2019년 1월과 3월에 구매한 고객")
    sql = g.build_purchase_history_targets_sql_candidate(plan)["sql"]
    assert ("(D.ORDER_DATE BETWEEN '20190101' AND '20190131' "
            "OR D.ORDER_DATE BETWEEN '20190301' AND '20190331')") in sql
    assert "20190201" not in sql  # 2월은 들어오지 않는다


def test_unclaimable_window_is_reported_not_dropped_silently():
    """소속이 모호한 창(다른 도메인 날짜 앵커가 있음)은 귀속하지 않되, 결정론 경고로 고지한다."""
    query = "2018년에 가입하고 총금액이 10만 원 이상인 회원"
    plan = _plan(query)
    assert plan["target_user"].get("purchase_date") is None  # 잘못 건 조건은 드롭보다 나쁘다
    assert g._deterministic_dropped_conditions(query, plan) == ["기간 '2018년' 조건"]


def test_no_warning_when_window_is_represented():
    """창이 plan 에 반영됐으면 경고하지 않는다(오탐 금지)."""
    assert g._deterministic_dropped_conditions(PROMPT, _plan(PROMPT)) == []

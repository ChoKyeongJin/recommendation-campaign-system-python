"""구매 날짜(purchase_date) 타겟 + 상품명에서 날짜 토큰 분리 회귀.

배경: '2024년 3월에 구매한 고객'처럼 날짜가 '구매' 바로 앞에 오면, 상품 구매 이력 추출 정규식이
날짜('3월에')를 상품명(purchase_object)으로 잡아 상품 LIKE 를 무의미하게 만들었다. 날짜는 상품이
아니라 '구매 날짜' 조건(ORDER_DATE 창)이 정답이다.

고정 내용:
  - _sanitize_purchase_object 가 날짜 토큰을 상품 후보에서 제외한다(날짜만 있으면 purchase_object=None).
  - 절대 날짜/기간은 target_user.purchase_date {from,to}(YYYYMMDD)로 잡힌다.
  - build_purchase_history_targets_sql_candidate 가 ORDER_DATE BETWEEN 을 실제 SQL 에 반영한다.
  - 연도 없는 'M월'·'최근 N일'은 구매 날짜로 잡지 않는다(오탐 방지).

실행(컨테이너): docker compose exec -w /app -e PYTHONPATH=/app api pytest tests/test_purchase_date_targeting.py -q
"""

import graph_rag as g


def _plan(query: str) -> dict:
    plan = g.build_query_plan(query, parser="rules")
    g._promote_unknown_intent_for_target_signal(plan)
    return plan


def test_date_only_not_put_into_product():
    tu = _plan("2024년 3월에 구매한 고객")["target_user"]
    assert tu.get("purchase_object") is None
    assert tu.get("purchase_date") == {"from": "20240301", "to": "20240331", "label": "2024년 3월 구매"}


def test_product_and_date_coexist():
    tu = _plan("2024년 3월에 기저귀 구매한 고객")["target_user"]
    assert tu.get("purchase_object") == "기저귀"
    assert tu["purchase_date"]["from"] == "20240301"
    assert tu["purchase_date"]["to"] == "20240331"


def test_single_day_and_iso_forms():
    a = _plan("2024년 3월 15일에 구매한 고객")["target_user"]["purchase_date"]
    assert a["from"] == "20240315" and a["to"] == "20240315"
    b = _plan("2024-03-15 구매한 고객")["target_user"]["purchase_date"]
    assert b["from"] == "20240315" and b["to"] == "20240315"


def test_year_only_range():
    d = _plan("2023년에 구매한 고객")["target_user"]["purchase_date"]
    assert d["from"] == "20230101" and d["to"] == "20231231"


def test_leap_and_month_end():
    feb = _plan("2024년 2월에 구매한 고객")["target_user"]["purchase_date"]  # 윤년
    assert feb["to"] == "20240229"
    apr = _plan("2023년 4월에 구매한 고객")["target_user"]["purchase_date"]  # 30일 달
    assert apr["to"] == "20230430"


def test_month_without_year_not_captured():
    tu = _plan("3월에 구매한 고객")["target_user"]
    assert tu.get("purchase_date") is None
    assert tu.get("purchase_object") is None  # '3월에'가 상품으로도 새지 않아야 한다


def test_relative_window_not_captured_as_purchase_date():
    tu = _plan("최근 30일 구매한 고객")["target_user"]
    assert tu.get("purchase_date") is None


def test_non_purchase_date_ignored():
    # 구매/구입/주문 신호가 없으면 날짜를 구매 날짜로 잡지 않는다.
    tu = _plan("2024년 3월에 가입한 고객")["target_user"]
    assert tu.get("purchase_date") is None


def test_date_like_token_classification():
    assert g._is_date_like_token("3월") is True
    assert g._is_date_like_token("2024년") is True
    assert g._is_date_like_token("15일") is True
    assert g._is_date_like_token("20240301") is True
    assert g._is_date_like_token("2024-03") is True
    # 숫자로 시작해도 날짜가 아닌 상품 토큰은 유지되어야 한다.
    assert g._is_date_like_token("3m") is False
    assert g._sanitize_purchase_object("3m마스크") == "3m마스크"


def test_builder_emits_order_date_between():
    plan = _plan("2024년 3월에 구매한 고객")
    cand = g.build_purchase_history_targets_sql_candidate(plan)
    assert cand is not None
    assert "D.ORDER_DATE BETWEEN '20240301' AND '20240331'" in cand["sql"]
    # 상품 조건이 없으므로 상품 LIKE 는 없어야 한다.
    assert "LIKE" not in cand["sql"]


def test_builder_combines_product_and_date():
    plan = _plan("2024년 3월에 기저귀 구매한 고객")
    cand = g.build_purchase_history_targets_sql_candidate(plan)
    assert cand is not None
    assert "%기저귀%" in cand["sql"]
    assert "D.ORDER_DATE BETWEEN '20240301' AND '20240331'" in cand["sql"]


def test_first_and_second_half_periods():
    first = _plan("2024년 상반기에 구매한 고객")["target_user"]["purchase_date"]
    assert first["from"] == "20240101" and first["to"] == "20240630"
    second = _plan("2024년 하반기 구매 고객")["target_user"]["purchase_date"]
    assert second["from"] == "20240701" and second["to"] == "20241231"


def test_quarter_periods():
    q1 = _plan("2024년 1분기에 구매한 고객")["target_user"]["purchase_date"]
    assert q1["from"] == "20240101" and q1["to"] == "20240331"
    q4 = _plan("2023년 4분기 구매")["target_user"]["purchase_date"]
    assert q4["from"] == "20231001" and q4["to"] == "20231231"
    # 'N사분기' 표기도 동일하게 해석
    q2 = _plan("2024년 2사분기 구매")["target_user"]["purchase_date"]
    assert q2["from"] == "20240401" and q2["to"] == "20240630"


def test_half_quarter_needs_year_and_purchase_signal():
    # 연도 없는 '상반기'는 모호 → 미해석
    assert _plan("상반기에 구매한 고객")["target_user"].get("purchase_date") is None
    # 구매 신호 없는 날짜는 구매 날짜로 잡지 않음
    assert _plan("2024년 상반기 캠페인 기획")["target_user"].get("purchase_date") is None


def test_bare_half_without_updown_falls_back_to_year():
    # 그냥 '반기'(상/하 없음)는 반기 범위를 만들지 않지만, 명시된 연도(2024년)는 연 전체로 남는다.
    d = _plan("2024년 반기 구매 고객")["target_user"].get("purchase_date")
    assert d == {"from": "20240101", "to": "20241231", "label": "2024년 구매"}


def test_bare_year_still_whole_year():
    d = _plan("2024년에 구매한 고객")["target_user"]["purchase_date"]
    assert d["from"] == "20240101" and d["to"] == "20241231"

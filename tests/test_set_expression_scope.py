"""집합식(set expression) 오탐 회귀 코퍼스.

버그: "20대 여성 고객을 대상으로 화장품 할인 캠페인" 처럼 "대상으로"(그냥 '누구를 타겟한다'는 뜻)만
있는 문장을 집합식 파서가 age_20s * female 교집합으로 잘못 해석 → 실DB 미지원 집합식이 돼 연령·성별이
통째로 사라지고 SQL 이 막혔다. 실제 집합 연산(포함/남기고/제외/교집합 등)이 있을 때만 집합식이어야 한다.

실행(컨테이너): docker compose exec api pytest tests/test_set_expression_scope.py -q
"""

import graph_rag as g


def _plan(prompt):
    # rules 경로(OPENAI 없이)로 결정론적으로 파싱한다.
    return g.build_query_plan(prompt)


def test_daesangeuro_alone_is_not_a_set_expression():
    # "대상으로"만 있고 집합 연산 표지가 없으면 집합식이 아니어야 하고, 연령·성별이 살아 있어야 한다.
    plan = _plan("20대 여성 고객을 대상으로 화장품 할인 혜택을 제공하는 캠페인")
    assert plan["set_expressions"] == []
    assert plan["target_user"]["gender"] == "female"
    assert plan["target_user"]["age_min"] == 20
    assert plan["target_user"]["age_max"] == 29


def test_plain_audience_conjunction_is_not_a_set_expression():
    plan = _plan("20대 여성 고객 대상 화장품 할인 캠페인을 만들어줘")
    assert plan["set_expressions"] == []
    assert plan["target_user"]["gender"] == "female"
    assert plan["target_user"]["age_min"] == 20


def test_genuine_refinement_is_still_a_set_expression():
    # "대상으로 하되 … 만 포함"은 진짜 정제(교집합) 집합식이어야 한다.
    plan = _plan("20대를 대상으로 하되 여성만 포함")
    assert len(plan["set_expressions"]) == 1


def test_genuine_difference_is_still_a_set_expression():
    plan = _plan("VIP 고객에서 휴면 고객을 제외")
    assert len(plan["set_expressions"]) == 1

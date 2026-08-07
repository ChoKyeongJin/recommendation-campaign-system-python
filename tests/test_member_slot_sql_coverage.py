"""회원 슬롯의 필수 SQL 조건 — 생성부와 검증부가 같은 문자열을 쓰는가.

이 파일이 지키는 것은 하나다: **플랜에 있는 회원 조건은 SQL 에 남거나, 아니면 실패한다.**
가운데(조건이 사라진 SQL 이 '성공'으로 출고되는 상태)가 없어야 한다.

왜 이 부류가 따로 필요한가 — 아래 슬롯들은 ``compile_member_target_conditions`` 나 전용 빌더가
소비하지만 필수조건이 없었다. 그래서 그 컴파일러가 호출되지 않는 경로(권위가 ``event_ir`` 이면
``build_event_expression_sql_candidate`` 가 회원 컴파일러를 통째로 건너뛴다)에서 조건이 **아무 표시
없이** 사라졌고, 커버리지 게이트조차 통과했다. 조건이 사라진 사실이 SQL 모양에도 응답에도 남지
않는 유일한 부류였다.

토큰을 생성부와 같은 함수로 만들기 때문에, 이 테스트들은 '문자열이 우연히 겹치는가'가 아니라
'두 벌이 갈라졌는가'를 잰다.
"""

from __future__ import annotations

import graph_rag
from query_structurer import StructuringContext


_FIXED_CONTEXT = StructuringContext(
    current_date="2026-08-04",
    timezone="Asia/Seoul",
    current_datetime="2026-08-04T09:00:00+09:00",
)

# 회원 컴파일러가 호출되지 **않은** SQL. 권위가 event_ir 인 경로가 실제로 내는 모양이다
# (Event IR 술어만 있고 회원 술어는 없다).
_EVENT_IR_ONLY_SQL = (
    "SELECT DISTINCT B.MEMBER_NO FROM CRM_MB_BASEINFO B "
    "WHERE NOT EXISTS (SELECT 1 FROM CRM_SL_ORDERHEADERMALL O WHERE O.MEMBER_NO = B.MEMBER_NO)"
)

# 슬롯 하나만 든 최소 플랜. 값은 전부 소비부의 수용 조건을 만족하는 형태다.
_MEMBER_SLOT_PLANS: dict[str, dict] = {
    "target_user.age_exclude_ranges": {"target_user": {"age_exclude_ranges": [[20, 29], [40, 49]]}},
    "target_user.birthday_target": {"target_user": {"birthday_target": {"granularity": "month"}}},
    "target_user.coupon_usage_thresholds": {
        "target_user": {"coupon_usage_thresholds": [{"operator": "gte", "value": 2}]}
    },
    "target_user.cart_quantity_missing": {"target_user": {"cart_quantity_missing": True}},
    "target_user.signup_target": {"target_user": {"signup_target": {"days": 30}}},
}


def _member_sql(plan: dict) -> str:
    """회원 컴파일러가 실제로 내는 술어로 만든 SQL(생성부 산출물)."""
    compiled = graph_rag.compile_member_target_conditions(plan)
    predicates = compiled["predicates"] or ["1 = 1"]
    return "SELECT B.MEMBER_NO FROM CRM_MB_BASEINFO B WHERE " + " AND ".join(predicates)


def _conditions_for(plan: dict, path_prefix: str) -> list[dict]:
    return [
        condition
        for condition in graph_rag.required_sql_conditions(plan)
        if condition["path"] == path_prefix or condition["path"].startswith(path_prefix + "[")
    ]


def _coverage(plan: dict, sql: str) -> dict:
    return graph_rag.validate_sql_condition_coverage(sql, graph_rag.required_sql_conditions(plan))


def test_a_required_condition_always_comes_with_a_verification_token() -> None:
    """필수조건과 검증 토큰은 **같은 슬롯 집합**에서 나와야 한다.

    이 불변식은 이미 코드에 있다 — build_sql_result 에 "필수조건이 있는데 검증 토큰이 하나도
    없으면 후보 생성 전에 종료" 게이트가 있다. 그래서 필수조건만 늘리면 그 게이트가 **정상
    요청을 막는다**. 실측(구현 중): 토큰 없이 필수조건만 넣었더니 '이번 달 생일인 고객'이
    KeyError('output_contract') 로 500 이 됐다(그 줄은 오래 도달 불가였다가 드러난 잠복 결함이다).

    슬롯을 새로 열 때 한쪽만 늘리는 것을 여기서 막는다.
    """
    plans = dict(_MEMBER_SLOT_PLANS)
    plans["member_metric_selection"] = {
        "member_metric_selection": {"column": "DEPOSIT_BAL", "mode": "top_n", "n": 100}
    }
    for path, plan in plans.items():
        required = graph_rag.required_sql_conditions(plan)
        tokens = graph_rag.build_verified_condition_tokens(plan)
        assert required, f"{path}: 필수조건이 없다"
        assert tokens, (
            f"{path}: 필수조건은 {len(required)}개인데 검증 토큰이 0개다 — "
            "build_sql_result 의 토큰 게이트가 이 요청을 통째로 막는다"
        )


def test_every_member_slot_produces_a_required_condition() -> None:
    """슬롯이 플랜에 있으면 필수조건이 **반드시** 하나 이상 생긴다.

    이 단언이 이 파일의 래칫이다 — 슬롯 하나가 필수조건 없이 다시 추가되면 여기서 걸린다.
    """
    for path, plan in _MEMBER_SLOT_PLANS.items():
        assert _conditions_for(plan, path), f"{path} 에 필수조건이 없다 — 조건이 조용히 사라질 수 있다"


def test_member_metric_selection_produces_a_required_condition() -> None:
    """전용 빌더 소유 슬롯(plan 최상위)도 같은 규칙을 따른다."""
    plan = {"member_metric_selection": {"column": "DEPOSIT_BAL", "mode": "top_n", "n": 100}}
    assert _conditions_for(plan, "member_metric_selection")


def test_generator_output_satisfies_its_own_required_conditions() -> None:
    """생성부가 낸 SQL 은 검증부를 통과한다 — 두 벌이 갈라지면 여기서 red."""
    for path, plan in _MEMBER_SLOT_PLANS.items():
        assert _coverage(plan, _member_sql(plan))["is_satisfied"], f"{path}: 생성부 SQL 이 자기 필수조건을 못 만족한다"


def test_member_metric_selection_builder_output_satisfies_its_condition() -> None:
    plan = {"member_metric_selection": {"column": "DEPOSIT_BAL", "mode": "top_n", "n": 100}}
    candidate = graph_rag.build_member_column_selection_sql_candidate(plan)
    assert candidate is not None
    assert _coverage(plan, candidate["sql"])["is_satisfied"]


def test_sql_without_member_predicates_is_not_covered() -> None:
    """회원 컴파일러를 건너뛴 SQL 은 **불만족**이다.

    이것이 이 부류의 존재 이유다. 이 단언이 뒤집히면 조건이 빠진 SQL 이 다시 성공으로 출고된다.
    """
    for path, plan in _MEMBER_SLOT_PLANS.items():
        assert not _coverage(plan, _EVENT_IR_ONLY_SQL)["is_satisfied"], (
            f"{path}: 조건이 빠진 SQL 이 커버리지를 통과했다"
        )


def test_member_metric_selection_is_not_covered_when_another_builder_wins() -> None:
    """전용 빌더가 지면 선택 전략이 통째로 사라진다 — 그 상태가 성공이 되면 안 된다."""
    plan = {"member_metric_selection": {"column": "DEPOSIT_BAL", "mode": "top_n", "n": 100}}
    assert not _coverage(plan, _EVENT_IR_ONLY_SQL)["is_satisfied"]


def test_birthday_month_rejects_the_day_predicate() -> None:
    """'이달 생일'과 '오늘 생일'은 SUBSTRING 자릿수로만 갈린다 — 뒤바뀜을 none_terms 가 잡는다."""
    month_plan = {"target_user": {"birthday_target": {"granularity": "month"}}}
    day_plan = {"target_user": {"birthday_target": {"granularity": "today"}}}

    assert not _coverage(month_plan, _member_sql(day_plan))["is_satisfied"]
    assert not _coverage(day_plan, _member_sql(month_plan))["is_satisfied"]


def test_each_excluded_age_range_is_required_separately() -> None:
    """구간마다 술어가 하나씩이므로 조건도 구간마다 하나 — 한 구간만 남은 SQL 은 불만족이다."""
    plan = _MEMBER_SLOT_PLANS["target_user.age_exclude_ranges"]
    conditions = _conditions_for(plan, "target_user.age_exclude_ranges")
    assert len(conditions) == 2

    partial_sql = (
        "SELECT B.MEMBER_NO FROM CRM_MB_BASEINFO B "
        f"WHERE {conditions[0]['all_terms'][0]}"
    )
    assert not _coverage(plan, partial_sql)["is_satisfied"]


def test_coupon_threshold_without_a_compilable_predicate_is_not_required() -> None:
    """소비부가 술어를 못 만드는 항목은 요구하지도 않는다(요구만 하고 못 내는 상태 금지)."""
    plan = {"target_user": {"coupon_usage_thresholds": [{"operator": "unknown_op", "value": 2}]}}
    compiled = graph_rag.compile_member_target_conditions(plan)
    if not compiled["predicates"]:
        assert not _conditions_for(plan, "target_user.coupon_usage_thresholds")


def test_single_slot_requests_still_ship_sql_end_to_end(member_slot_gate_lifted: None) -> None:
    """회귀 방어: 슬롯 하나짜리 요청이 **게이트를 걷으면 여전히 SQL 을 낸다**.

    필수조건을 새로 요구하는 변경은 성격상 오탐 위험이 있다 — 지금 성공하던 요청을 실패로
    뒤집을 수 있다. 위 단위 테스트들은 커버리지 계산만 재므로 그 뒤집힘을 못 본다.

    2026-08-07 legacy 레인 폐쇄 이후 이 테스트가 재는 것은 "제품이 SQL 을 낸다"가 아니라
    "회원 슬롯 컴파일이 온전하다"이다(픽스처 docstring 참조). 폐쇄된 상태에서 같은 플랜이
    실제로 막힌다는 것은 `tests/test_canonical_legacy_audience_conflict.py` 가 고정한다.
    """
    # 실행 가능한 플랜 골격을 채운다 — intent 가 없으면 어떤 슬롯이든(대조군 gender 포함)
    # no_sql_candidates 로 끝나므로, 골격 없이 재면 이 테스트는 항상 red 인 채 아무것도 못 잰다.
    control = {"intent": "find_user_segment", "exclude": {}, "campaign_constraints": {}}
    baseline = graph_rag.build_sql_result(
        graph_rag.nx.Graph(), "x", {**control, "target_user": {"gender": "female"}}, [],
        graph_rag.DEFAULT_SCHEMA_PATH, default_limit=100, original_query="x",
        structuring_context=_FIXED_CONTEXT,
    )
    assert baseline["is_success"], "대조군이 실패한다 — 이 테스트의 전제가 깨졌다"

    for path, plan in _MEMBER_SLOT_PLANS.items():
        result = graph_rag.build_sql_result(
            graph_rag.nx.Graph(), "x", {**control, **plan}, [], graph_rag.DEFAULT_SCHEMA_PATH,
            default_limit=100, original_query="x", structuring_context=_FIXED_CONTEXT,
        )
        assert result["is_success"], (
            f"{path}: 정상 요청이 실패로 뒤집혔다 — failure_reason={result.get('failure_reason')}"
        )
        assert result["sql"]


def test_missing_output_contract_does_not_raise() -> None:
    """토큰 게이트는 output_contract 키가 없는 평범한 플랜에서도 예외를 내지 않는다.

    그 줄이 대괄호 인덱싱이던 동안은 KeyError → 처리되지 않는 500 이었다.
    """
    plan = {
        "intent": "find_user_segment",
        "target_user": {"interests": ["definitely_not_a_compilable_interest"]},
        "exclude": {},
        "campaign_constraints": {},
    }
    assert "output_contract" not in plan
    result = graph_rag.build_sql_result(
        graph_rag.nx.Graph(), "x", plan, [], graph_rag.DEFAULT_SCHEMA_PATH,
        default_limit=100, original_query="x",
    )
    assert result["is_success"] is False
    assert result["failure_reason"]
